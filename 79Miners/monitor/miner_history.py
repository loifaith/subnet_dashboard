#!/usr/bin/env python3
"""Append-only performance history for owned subnet 79 miners."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MINER_HISTORY_DIR = os.getenv(
    "MINER_HISTORY_DIR",
    os.path.join(PROJECT_ROOT, "miner_history"),
)
MINER_PARAMS_DIR = os.path.join(PROJECT_ROOT, "79Miners", "params")

HISTORY_METRIC_KEYS = (
    "kappa",
    "real_pnl",
    "unrl_pnl",
    "volume",
    "vol_mkr",
    "vol_tkr",
    "hot_book_count",
    "book_count",
    "max_book_volume",
    "incentive",
)


def _today_str() -> str:
    return date.today().isoformat()


def list_my_miner_uids() -> List[int]:
    uids: List[int] = []
    if not os.path.isdir(MINER_PARAMS_DIR):
        return uids
    for name in os.listdir(MINER_PARAMS_DIR):
        if not (name.startswith("run_miner_") and name.endswith(".json")):
            continue
        try:
            uids.append(int(name[len("run_miner_") : -len(".json")]))
        except ValueError:
            continue
    return sorted(uids)


def daily_history_path(day: Optional[str] = None) -> str:
    day = day or _today_str()
    return os.path.join(MINER_HISTORY_DIR, f"{day}.jsonl")


def _extract_history_metrics(uid_metrics: Dict[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for key in HISTORY_METRIC_KEYS:
        if key in uid_metrics and uid_metrics[key] is not None:
            row[key] = uid_metrics[key]
    return row


def append_my_miners_history(
    metrics: Dict[str, Dict[str, Any]],
    current_block: Optional[int] = None,
    my_uids: Optional[List[int]] = None,
    snapshot_at: Optional[datetime] = None,
) -> Optional[str]:
    """Append one snapshot line for owned miners. Returns path or None if empty."""
    my_uids = my_uids if my_uids is not None else list_my_miner_uids()
    if not my_uids:
        return None

    miners: Dict[str, Dict[str, Any]] = {}
    for uid in my_uids:
        uid_metrics = metrics.get(str(uid))
        if not uid_metrics:
            continue
        row = _extract_history_metrics(uid_metrics)
        if row:
            miners[str(uid)] = row

    if not miners:
        return None

    snapshot_at = snapshot_at or datetime.now(timezone.utc)
    payload = {
        "snapshot_at": snapshot_at.isoformat(),
        "current_block": current_block,
        "miner_count": len(miners),
        "miners": miners,
    }

    os.makedirs(MINER_HISTORY_DIR, exist_ok=True)
    path = daily_history_path(snapshot_at.date().isoformat())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")
    logger.info("Appended miner history snapshot: %s (%s miners)", path, len(miners))
    return path


def _iter_history_days(days: int, end_day: Optional[str] = None) -> List[str]:
    end = date.fromisoformat(end_day) if end_day else date.today()
    return [(end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def _read_history_file(path: str) -> List[dict]:
    snapshots: List[dict] = []
    if not os.path.isfile(path):
        return snapshots
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("miners"), dict):
                    snapshots.append(row)
    except OSError as exc:
        logger.warning("Failed to read miner history %s: %s", path, exc)
    return snapshots


def load_history_snapshots(
    days: int = 7,
    day: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    if day:
        day_list = [day]
    else:
        day_list = _iter_history_days(max(1, days), day)

    snapshots: List[dict] = []
    for d in day_list:
        snapshots.extend(_read_history_file(daily_history_path(d)))
    snapshots.sort(key=lambda row: row.get("snapshot_at") or "")
    if limit is not None and limit > 0:
        snapshots = snapshots[-limit:]
    return snapshots


def build_uid_history(
    uid: int,
    days: int = 7,
    day: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    uid_str = str(uid)
    points: List[dict] = []
    for snapshot in load_history_snapshots(days=days, day=day, limit=limit):
        miner = snapshot.get("miners", {}).get(uid_str)
        if not miner:
            continue
        points.append(
            {
                "snapshot_at": snapshot.get("snapshot_at"),
                "current_block": snapshot.get("current_block"),
                **miner,
            }
        )
    return {
        "uid": uid,
        "days": days if not day else 1,
        "day": day,
        "point_count": len(points),
        "points": points,
    }


def list_history_days(max_days: int = 30) -> List[str]:
    if not os.path.isdir(MINER_HISTORY_DIR):
        return []
    days: List[str] = []
    for name in os.listdir(MINER_HISTORY_DIR):
        if name.endswith(".jsonl"):
            days.append(name[: -len(".jsonl")])
    days.sort(reverse=True)
    if max_days > 0:
        days = days[:max_days]
    return days
