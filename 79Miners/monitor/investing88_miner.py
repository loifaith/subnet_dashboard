#!/usr/bin/env python3
"""Fetch SN88 miner details from db.investing88.ai."""

from __future__ import annotations

import ast
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

INVESTING88_API = "https://db.investing88.ai/api"
INVESTING88_DASHBOARD = "https://db.investing88.ai/"
REQUEST_TIMEOUT = 45

ASSET_LABELS = {
    0: "Tao / Alpha",
    1: "US Equities",
}


def _get_json(path: str, params: Optional[dict] = None) -> Any:
    url = f"{INVESTING88_API}/{path.lstrip('/')}"
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
    return data


def _parse_allocations(raw: Any) -> Dict[str, float]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = ast.literal_eval(str(raw))
        except (SyntaxError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, float] = {}
    for key, val in data.items():
        if key == "_":
            continue
        try:
            out[str(key)] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def _pick_score_item(items: List[dict], uid: int, hotkey: Optional[str] = None) -> Optional[dict]:
    uid_str = str(uid)
    matches = [row for row in items if str(row.get("uid")) == uid_str]
    if not matches:
        return None
    if hotkey:
        prefix = hotkey[:6]
        for row in matches:
            if str(row.get("hotkey", "")).startswith(prefix):
                return row
    return matches[0]


def _normalize_score(row: dict) -> dict:
    asset = row.get("asset")
    try:
        asset_int = int(asset)
    except (TypeError, ValueError):
        asset_int = None
    return {
        "uid": row.get("uid"),
        "hotkey": row.get("hotkey"),
        "date": row.get("date"),
        "rank": row.get("rank"),
        "asset": asset_int,
        "asset_label": ASSET_LABELS.get(asset_int, f"Asset {asset}") if asset_int is not None else None,
        "days": row.get("days"),
        "last": row.get("last"),
        "value": row.get("value"),
        "swap": row.get("swap"),
        "score": row.get("score"),
        "apy": row.get("apy"),
        "lsr": row.get("lsr"),
        "mar": row.get("mar"),
        "risk_percent": row.get("riskPercent"),
        "odds_percent": row.get("oddsPercent"),
        "daily_percent": row.get("dailyPercent"),
        "yield_percent": row.get("yieldPercent"),
    }


def _normalize_holdings(rows: List[list]) -> List[dict]:
    out: List[dict] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 10:
            continue
        symbol = str(row[3] or "").strip() or "CASH"
        try:
            out.append(
                {
                    "symbol": symbol,
                    "date": row[2],
                    "close": float(row[5]),
                    "quantity": float(row[6]),
                    "value": float(row[7]),
                    "weight": float(row[9]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda r: r.get("weight") or 0, reverse=True)
    return out


def _normalize_strategy(rows: List[list]) -> List[dict]:
    out: List[dict] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            continue
        try:
            asset = int(row[5])
        except (TypeError, ValueError):
            asset = None
        raw_alloc = row[7] if len(row) > 7 else None
        if isinstance(raw_alloc, str) and raw_alloc.startswith("delayed_view_"):
            allocations: Dict[str, float] = {}
        else:
            allocations = _parse_allocations(raw_alloc)
        out.append(
            {
                "date": row[2],
                "time": row[3],
                "asset": asset,
                "asset_label": ASSET_LABELS.get(asset, f"Asset {asset}") if asset is not None else None,
                "value": float(row[6]) if row[6] is not None else None,
                "allocations": allocations,
            }
        )
    out.sort(key=lambda r: (r.get("date") or "", r.get("time") or ""), reverse=True)
    return out


def _normalize_pnl(payload: dict) -> List[dict]:
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        return []
    out: List[dict] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "date": row.get("date"),
                "asset": row.get("asset"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "fund_close": row.get("fundClose"),
            }
        )
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def build_dashboard_url(uid: int, hotkey: Optional[str], rank: Optional[int], score_date: Optional[str]) -> str:
    params = [f"uid={uid}"]
    if hotkey:
        params.append(f"hotkey={hotkey[:6]}")
    if rank is not None:
        params.append(f"rank={rank}")
    if score_date:
        params.append(f"date={score_date}")
    return INVESTING88_DASHBOARD + "?" + "&".join(params)


def fetch_miner_details(
    uid: int,
    hotkey: Optional[str] = None,
    rank: Optional[int] = None,
    *,
    use_cache: bool = True,
    force_live: bool = False,
) -> dict:
    """Return structured miner data; prefer local daily cache unless force_live."""
    if use_cache and not force_live:
        try:
            sn88_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "88Miners")
            if sn88_dir not in sys.path:
                sys.path.insert(0, sn88_dir)
            from monitor.investing88_cache import load_cached_miner

            cached = load_cached_miner(uid)
            if cached and cached.get("found"):
                return cached
        except Exception as exc:
            logger.debug("Investing88 cache read failed uid=%s: %s", uid, exc)

    score_payload = _get_json("score", params={"uid": uid})
    items = score_payload.get("items") if isinstance(score_payload, dict) else score_payload
    if not isinstance(items, list):
        items = []
    score_row = _pick_score_item(items, uid, hotkey)
    if score_row is None:
        return {
            "uid": uid,
            "found": False,
            "error": "Miner not found on Investing88 dashboard",
        }

    score = _normalize_score(score_row)
    hotkey_prefix = score.get("hotkey") or (hotkey[:6] if hotkey else None)

    holdings: List[dict] = []
    strategy_history: List[dict] = []
    pnl_history: List[dict] = []
    errors: List[str] = []

    try:
        holdings = _normalize_holdings(_get_json(f"close/{uid}"))
    except Exception as exc:
        logger.warning("Investing88 close fetch failed uid=%s: %s", uid, exc)
        errors.append(f"holdings: {exc}")

    try:
        strategy_history = _normalize_strategy(_get_json(f"strat/{uid}"))
    except Exception as exc:
        logger.warning("Investing88 strat fetch failed uid=%s: %s", uid, exc)
        errors.append(f"strategy: {exc}")

    try:
        pnl_history = _normalize_pnl(_get_json(f"pnl/{uid}"))
    except Exception as exc:
        logger.warning("Investing88 pnl fetch failed uid=%s: %s", uid, exc)
        errors.append(f"pnl: {exc}")

    latest_strategy = strategy_history[0] if strategy_history else None
    dashboard_url = build_dashboard_url(
        uid,
        hotkey or hotkey_prefix,
        rank if rank is not None else score.get("rank"),
        score.get("date"),
    )

    return {
        "uid": uid,
        "found": True,
        "hotkey_prefix": hotkey_prefix,
        "score": score,
        "latest_strategy": latest_strategy,
        "strategy_history": strategy_history[:12],
        "holdings": holdings,
        "pnl_history": pnl_history[:30],
        "dashboard_url": dashboard_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "live",
        "errors": errors,
    }
