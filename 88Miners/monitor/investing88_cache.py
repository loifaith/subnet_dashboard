#!/usr/bin/env python3
"""Daily local cache for Investing88 (SN88) miner data."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SN88_DIR = os.path.join(PROJECT_ROOT, "88Miners")
MONITOR_79 = os.path.join(PROJECT_ROOT, "79Miners", "monitor")
if SN88_DIR not in sys.path:
    sys.path.insert(0, SN88_DIR)
if MONITOR_79 not in sys.path:
    sys.path.insert(0, MONITOR_79)

from const import (  # noqa: E402
    API_BASE_HTTPS,
    CACHE_DIR,
    DASHBOARD_API_BASE,
    SYNC_TZ,
)
from investing88_miner import (  # noqa: E402
    ASSET_LABELS,
    _get_json,
    _normalize_holdings,
    _normalize_pnl,
    _normalize_score,
    _normalize_strategy,
    _pick_score_item,
    build_dashboard_url,
)

API_TIMEOUT = 120
SYNC_WORKERS = int(os.getenv("INVESTING88_SYNC_WORKERS", "8"))


def _today_tokyo() -> str:
    return datetime.now(ZoneInfo(SYNC_TZ)).date().isoformat()


def cache_path(day: Optional[str] = None) -> str:
    day = day or _today_tokyo()
    return os.path.join(CACHE_DIR, f"{day}.json")


def latest_cache_path() -> str:
    return os.path.join(CACHE_DIR, "latest.json")


def _api_get(path: str) -> Any:
    url = f"{API_BASE_HTTPS}/{path.lstrip('/')}"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
    return data


def _normalize_days_rows(rows: List[list]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            continue
        try:
            uid = int(row[0])
        except (TypeError, ValueError):
            continue
        try:
            asset = int(row[4])
        except (TypeError, ValueError):
            asset = None
        out[str(uid)] = {
            "uid": uid,
            "hotkey": row[1],
            "date": row[2],
            "rank": row[3],
            "asset": asset,
            "asset_label": ASSET_LABELS.get(asset) if asset is not None else None,
            "days": row[5],
            "last": row[6],
            "cash": row[7],
        }
    return out


def _build_miner_payload(uid: int, score_row: dict) -> dict:
    score = _normalize_score(score_row)
    hotkey_prefix = score.get("hotkey")
    errors: List[str] = []

    try:
        holdings = _normalize_holdings(_get_json(f"close/{uid}"))
    except Exception as exc:
        holdings = []
        errors.append(f"holdings: {exc}")

    try:
        strategy_history = _normalize_strategy(_get_json(f"strat/{uid}"))
    except Exception as exc:
        strategy_history = []
        errors.append(f"strategy: {exc}")

    try:
        pnl_history = _normalize_pnl(_get_json(f"pnl/{uid}"))
    except Exception as exc:
        pnl_history = []
        errors.append(f"pnl: {exc}")

    return {
        "uid": uid,
        "found": True,
        "hotkey_prefix": hotkey_prefix,
        "score": score,
        "latest_strategy": strategy_history[0] if strategy_history else None,
        "strategy_history": strategy_history[:12],
        "holdings": holdings,
        "pnl_history": pnl_history[:30],
        "dashboard_url": build_dashboard_url(
            uid,
            hotkey_prefix,
            score.get("rank"),
            score.get("date"),
        ),
        "errors": errors,
    }


def _sync_one_uid(uid: int, score_row: dict) -> tuple[str, dict]:
    try:
        payload = _build_miner_payload(uid, score_row)
        payload["source"] = "sync"
        return str(uid), payload
    except Exception as exc:
        logger.warning("Investing88 sync failed uid=%s: %s", uid, exc)
        return str(uid), {
            "uid": uid,
            "found": False,
            "error": str(exc),
            "source": "sync",
        }


def refresh_investing88_cache(day: Optional[str] = None) -> dict:
    """Fetch all Investing88 miner data and save locally."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    day = day or _today_tokyo()
    t0 = time.time()
    logger.info("Investing88 cache sync started for %s", day)

    ratio = _api_get("ratio")
    days_rows = _api_get("days")
    days_by_uid = _normalize_days_rows(days_rows if isinstance(days_rows, list) else [])

    score_payload = _get_json("score")
    items = score_payload.get("items") if isinstance(score_payload, dict) else score_payload
    if not isinstance(items, list):
        items = []

    score_by_uid: Dict[str, dict] = {}
    uids: List[int] = []
    score_date: Optional[str] = None
    for row in items:
        if not isinstance(row, dict):
            continue
        try:
            uid = int(row.get("uid"))
        except (TypeError, ValueError):
            continue
        score_by_uid[str(uid)] = row
        uids.append(uid)
        if score_date is None and row.get("date"):
            score_date = str(row["date"])

    miners: Dict[str, dict] = {}
    workers = min(SYNC_WORKERS, max(1, len(uids)))
    if uids:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_sync_one_uid, uid, score_by_uid[str(uid)]): uid
                for uid in uids
            }
            for fut in as_completed(futures):
                uid_str, payload = fut.result()
                miners[uid_str] = payload

    now_utc = datetime.now(timezone.utc)
    now_tokyo = now_utc.astimezone(ZoneInfo(SYNC_TZ))
    payload = {
        "date": day,
        "score_date": score_date,
        "updated_at": now_utc.isoformat(),
        "updated_at_tokyo": now_tokyo.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_tz": SYNC_TZ,
        "ratio": ratio,
        "days": days_by_uid,
        "miner_count": len(miners),
        "found_count": sum(1 for m in miners.values() if m.get("found")),
        "miners": miners,
        "sources": {
            "api": API_BASE_HTTPS,
            "dashboard": DASHBOARD_API_BASE,
        },
        "sync_seconds": round(time.time() - t0, 1),
    }

    path = cache_path(day)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)

    latest_tmp = f"{latest_cache_path()}.tmp"
    with open(latest_tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(latest_tmp, latest_cache_path())

    logger.info(
        "Investing88 cache saved: %s (%s miners, %.1fs)",
        path,
        len(miners),
        payload["sync_seconds"],
    )
    return {"path": path, **payload}


def load_cache_payload(day: Optional[str] = None) -> Optional[dict]:
    candidates = []
    if day:
        candidates.append(cache_path(day))
    candidates.append(latest_cache_path())
    if not day:
        candidates.append(cache_path(_today_tokyo()))
    if os.path.isdir(CACHE_DIR):
        for name in sorted(os.listdir(CACHE_DIR), reverse=True):
            if name.endswith(".json") and name != "latest.json":
                candidates.append(os.path.join(CACHE_DIR, name))

    seen = set()
    for path in candidates:
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("miners"), dict):
                data["_cache_path"] = path
                return data
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to read Investing88 cache %s: %s", path, exc)
    return None


def cache_exists_for_today() -> bool:
    return os.path.isfile(cache_path(_today_tokyo())) or os.path.isfile(latest_cache_path())


def load_cached_miner(uid: int, day: Optional[str] = None) -> Optional[dict]:
    cached = load_cache_payload(day)
    if not cached:
        return None
    miner = cached.get("miners", {}).get(str(uid))
    if not isinstance(miner, dict):
        return None
    out = dict(miner)
    out["cache_date"] = cached.get("date")
    out["cache_updated_at"] = cached.get("updated_at")
    out["cache_updated_at_tokyo"] = cached.get("updated_at_tokyo")
    out["source"] = "file"
    days_row = (cached.get("days") or {}).get(str(uid))
    if days_row:
        out["days_info"] = days_row
    return out


def get_cache_status() -> dict:
    cached = load_cache_payload()
    if not cached:
        return {"has_cache": False, "date": None}
    return {
        "has_cache": True,
        "date": cached.get("date"),
        "score_date": cached.get("score_date"),
        "updated_at": cached.get("updated_at"),
        "updated_at_tokyo": cached.get("updated_at_tokyo"),
        "miner_count": cached.get("miner_count"),
        "found_count": cached.get("found_count"),
        "path": cached.get("_cache_path"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = refresh_investing88_cache()
    print(
        f"Saved {result['found_count']}/{result['miner_count']} miners to {result['path']} "
        f"in {result['sync_seconds']}s"
    )
