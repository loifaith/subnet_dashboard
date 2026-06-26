#!/usr/bin/env python3
"""Append-only agent switch history for owned subnet 79 miners."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TOKYO = ZoneInfo("Asia/Tokyo")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_HISTORY_DIR = os.getenv(
    "AGENT_HISTORY_DIR",
    os.path.join(PROJECT_ROOT, "agent_history"),
)


def uid_history_path(uid: int) -> str:
    return os.path.join(AGENT_HISTORY_DIR, f"{uid}.jsonl")


def record_agent_switch(
    uid: int,
    previous_agent_index: int,
    previous_agent: Optional[str],
    new_agent_index: int,
    new_agent: Optional[str],
    source: str = "manual",
    *,
    miner_ip: Optional[str] = None,
    rsynced: Optional[bool] = None,
    saved_locally: Optional[bool] = None,
    rsync_message: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Append one agent switch event. Returns path or None if unchanged."""
    if previous_agent_index == new_agent_index:
        return None

    now_utc = datetime.now(timezone.utc)
    now_tokyo = now_utc.astimezone(TOKYO)
    entry: Dict[str, Any] = {
        "switched_at_utc_iso": now_utc.isoformat(),
        "switched_at": now_tokyo.strftime("%Y-%m-%d %H:%M:%S"),
        "switched_at_tz": "Asia/Tokyo",
        "uid": uid,
        "source": source,
        "previous_agent_index": previous_agent_index,
        "previous_agent": previous_agent,
        "new_agent_index": new_agent_index,
        "new_agent": new_agent,
    }
    if miner_ip:
        entry["miner_ip"] = miner_ip
    if rsynced is not None:
        entry["rsynced"] = rsynced
    if saved_locally is not None:
        entry["saved_locally"] = saved_locally
    if rsync_message:
        entry["rsync_message"] = rsync_message
    if extra:
        entry.update(extra)

    os.makedirs(AGENT_HISTORY_DIR, exist_ok=True)
    path = uid_history_path(uid)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    logger.info(
        "Recorded agent switch UID %s: %s (%s) -> %s (%s) [%s]",
        uid,
        previous_agent,
        previous_agent_index,
        new_agent,
        new_agent_index,
        source,
    )
    return path


def load_agent_history(uid: int, limit: Optional[int] = 50) -> List[dict]:
    path = uid_history_path(uid)
    if not os.path.isfile(path):
        return []
    entries: List[dict] = []
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
                if isinstance(row, dict):
                    entries.append(row)
    except OSError as exc:
        logger.warning("Failed to read agent history %s: %s", path, exc)
        return []
    entries.sort(
        key=lambda row: row.get("switched_at_utc_iso") or row.get("switched_at") or "",
        reverse=True,
    )
    if limit is not None and limit > 0:
        entries = entries[:limit]
    return entries


def build_agent_history_response(uid: int, limit: Optional[int] = 50) -> dict:
    entries = load_agent_history(uid, limit=limit)
    return {
        "uid": uid,
        "count": len(entries),
        "entries": entries,
    }
