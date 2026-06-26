#!/usr/bin/env python3
"""Auto-switch owned miners to FreeAllAgent when too many books are near volume limit."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from miner_params_sync import (
    fetch_uid_to_ip,
    load_params,
    params_path,
    save_and_sync_params,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SWITCH_LOG_DIR = os.getenv(
    "VOLUME_SWITCH_LOG_DIR",
    os.path.join(PROJECT_ROOT, "volume_agent_switches"),
)

FREE_ALL_AGENT_NAME = os.getenv("VOLUME_SWITCH_AGENT_NAME", "FreeAllAgent")
HOT_BOOK_SWITCH_THRESHOLD = int(os.getenv("HOT_BOOK_AUTO_SWITCH_THRESHOLD", "20"))
AUTO_SWITCH_ENABLED = os.getenv("HOT_BOOK_AUTO_SWITCH", "1").lower() not in (
    "0",
    "false",
    "no",
    "off",
)
IMMUNITY_PERIOD_BLOCKS = 7200
AUTO_SWITCH_EXEMPT_AGENTS: set = {
    a.strip()
    for a in os.getenv("VOLUME_SWITCH_EXEMPT_AGENTS", "RandomMakerAgentHarvester").split(",")
    if a.strip()
}


def _find_agent_index(agents: List[dict], name: str) -> Optional[int]:
    for idx, agent in enumerate(agents):
        if agent.get("name") == name:
            return idx
    return None


def _append_switch_log(entry: dict) -> None:
    os.makedirs(SWITCH_LOG_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    path = os.path.join(SWITCH_LOG_DIR, f"{day}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def switch_uid_to_free_all(
    uid: int,
    hot_book_count: int,
    metrics: Optional[dict] = None,
    uid_to_ip: Optional[Dict[str, str]] = None,
) -> dict:
    """Switch one owned miner to FreeAllAgent, save JSON locally, and rsync."""
    result: Dict[str, Any] = {
        "uid": uid,
        "hot_book_count": hot_book_count,
        "switched": False,
        "skipped": False,
        "reason": None,
        "saved_locally": False,
        "rsynced": False,
    }
    if not os.path.isfile(params_path(uid)):
        result["skipped"] = True
        result["reason"] = "no_params_file"
        return result

    data = load_params(uid)
    if not data:
        result["skipped"] = True
        result["reason"] = "read_failed"
        return result

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        result["skipped"] = True
        result["reason"] = "missing_agents"
        return result

    try:
        current_index = int(data.get("agent_index", 0))
    except (TypeError, ValueError):
        result["skipped"] = True
        result["reason"] = "invalid_agent_index"
        return result

    free_index = _find_agent_index(agents, FREE_ALL_AGENT_NAME)
    if free_index is None:
        result["skipped"] = True
        result["reason"] = "free_all_agent_not_found"
        return result

    if current_index == free_index:
        result["skipped"] = True
        result["reason"] = "already_free_all"
        return result

    previous_name = agents[current_index].get("name") if 0 <= current_index < len(agents) else None
    if previous_name in AUTO_SWITCH_EXEMPT_AGENTS:
        result["skipped"] = True
        result["reason"] = "exempt_agent"
        return result
    data["agent_index"] = free_index

    sync_result = save_and_sync_params(uid, data, uid_to_ip=uid_to_ip)
    result.update(
        {
            "switched": True,
            "previous_agent": previous_name,
            "previous_agent_index": current_index,
            "new_agent_index": free_index,
            "new_agent": FREE_ALL_AGENT_NAME,
            "saved_locally": sync_result.get("saved_locally", False),
            "local_path": sync_result.get("local_path"),
            "miner_ip": sync_result.get("miner_ip"),
            "rsynced": sync_result.get("rsynced", False),
            "rsync_message": sync_result.get("rsync_message"),
            "remote_path": sync_result.get("remote_path"),
        }
    )
    if metrics:
        result["max_book_volume"] = metrics.get("max_book_volume")
        result["book_count"] = metrics.get("book_count")

    from agent_history import record_agent_switch

    record_agent_switch(
        uid,
        current_index,
        previous_name,
        free_index,
        FREE_ALL_AGENT_NAME,
        source="auto_volume",
        miner_ip=result.get("miner_ip"),
        rsynced=result.get("rsynced"),
        saved_locally=result.get("saved_locally"),
        rsync_message=result.get("rsync_message") if not result.get("rsynced") else None,
        extra={
            "hot_book_count": hot_book_count,
            "max_book_volume": metrics.get("max_book_volume") if metrics else None,
        },
    )

    log_entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **result}
    _append_switch_log(log_entry)
    logger.info(
        "Auto-switched UID %s to %s (hot books=%s, was %s) saved=%s rsync=%s -> %s",
        uid,
        FREE_ALL_AGENT_NAME,
        hot_book_count,
        previous_name,
        result["saved_locally"],
        result["rsynced"],
        result.get("miner_ip"),
    )
    if not result["rsynced"]:
        logger.error("UID %s rsync failed after switch: %s", uid, result.get("rsync_message"))
    return result


def maybe_switch_hot_miners_to_free_all(metrics: Dict[str, Dict[str, Any]]) -> List[dict]:
    """Switch owned miners with hot_book_count > threshold to FreeAllAgent."""
    if not AUTO_SWITCH_ENABLED:
        return []

    from miner_history import list_my_miner_uids

    uid_to_ip = fetch_uid_to_ip()
    results: List[dict] = []
    for uid in list_my_miner_uids():
        uid_metrics = metrics.get(str(uid), {})
        try:
            hot_count = int(uid_metrics.get("hot_book_count") or 0)
        except (TypeError, ValueError):
            hot_count = 0
        if hot_count <= HOT_BOOK_SWITCH_THRESHOLD:
            continue
        try:
            blocks_since = int(uid_metrics.get("blocks_since_registration") or IMMUNITY_PERIOD_BLOCKS)
        except (TypeError, ValueError):
            blocks_since = IMMUNITY_PERIOD_BLOCKS
        if blocks_since < IMMUNITY_PERIOD_BLOCKS:
            logger.info(
                "Skipping auto-switch for UID %s: immune (%s/%s blocks)",
                uid,
                blocks_since,
                IMMUNITY_PERIOD_BLOCKS,
            )
            continue
        results.append(switch_uid_to_free_all(uid, hot_count, uid_metrics, uid_to_ip))
    return results
