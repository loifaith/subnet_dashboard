#!/usr/bin/env python3
"""Daily JSON cache for subnet 79 miner display metrics."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MINER_STATUS_DIR = os.getenv(
    "MINER_STATUS_DIR",
    os.path.join(PROJECT_ROOT, "miner_status"),
)
NETUID = 79


def _today_str() -> str:
    return date.today().isoformat()


def daily_status_path(day: Optional[str] = None) -> str:
    day = day or _today_str()
    return os.path.join(MINER_STATUS_DIR, f"{day}.json")


def prev_epoch_path() -> str:
    return os.path.join(MINER_STATUS_DIR, "prev_epoch_metrics.json")


def load_prev_epoch_metrics() -> Optional[Dict[str, Any]]:
    path = prev_epoch_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_prev_epoch_metrics(metrics: Dict[str, Any]) -> None:
    """Snapshot the current metrics as prev-epoch before they are overwritten."""
    os.makedirs(MINER_STATUS_DIR, exist_ok=True)
    path = prev_epoch_path()
    tmp = f"{path}.tmp"
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.write("\n")
    os.replace(tmp, path)


def load_daily_status(day: Optional[str] = None) -> Optional[dict]:
    path = daily_status_path(day)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("metrics"), dict):
            return data
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Failed to read miner status cache %s: %s", path, exc)
    return None


def save_daily_status(
    metrics: Dict[str, Dict[str, Any]],
    day: Optional[str] = None,
    current_block: Optional[int] = None,
) -> str:
    os.makedirs(MINER_STATUS_DIR, exist_ok=True)
    existing = load_daily_status(day)
    if existing and existing.get("metrics"):
        _save_prev_epoch_metrics(existing["metrics"])
    day = day or _today_str()
    payload: Dict[str, Any] = {
        "date": day,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "netuid": NETUID,
        "book_volume_limit": 500_000,
        "book_volume_warn_ratio": 0.98,
        "metrics": metrics,
    }
    if current_block is not None:
        payload["current_block"] = current_block
    path = daily_status_path(day)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)
    logger.info("Saved miner status cache: %s (%s uids)", path, len(metrics))
    return path


def filter_metrics(
    metrics: Dict[str, Dict[str, Any]], uids: Optional[list[int]] = None
) -> Dict[str, Dict[str, Any]]:
    if not uids:
        return metrics
    return {str(uid): metrics.get(str(uid), {}) for uid in uids}


def merge_reg_costs_from_cache(
    metrics: Dict[str, Dict[str, Any]], netuid: int = NETUID
) -> int:
    """Apply cached reg costs to metrics rows. Returns number of UIDs updated."""
    from get_reg_cost import load_reg_cost_cache

    cache = load_reg_cost_cache(netuid)
    if not cache:
        return 0
    updated = 0
    for row in metrics.values():
        if not isinstance(row, dict):
            continue
        reg_block = row.get("reg_block")
        if reg_block is None:
            continue
        cost = cache.get(str(reg_block))
        if not cost:
            continue
        row["reg_cost_rao"] = cost.get("rao")
        row["reg_cost_tao"] = cost.get("tao")
        updated += 1
    return updated


def _apply_reg_costs(
    metrics: Dict[str, Dict[str, Any]],
    netuid: int = NETUID,
    current_block: Optional[int] = None,
    fetch_missing: bool = True,
) -> None:
    """Attach reg_cost_tao/rao from cache; optionally fetch missing blocks from chain."""
    merge_reg_costs_from_cache(metrics, netuid)
    if not fetch_missing:
        return

    from get_reg_cost import fetch_reg_costs_by_block

    reg_blocks = [
        row["reg_block"]
        for row in metrics.values()
        if isinstance(row, dict) and row.get("reg_block") is not None
    ]
    if not reg_blocks:
        return
    block_costs = fetch_reg_costs_by_block(
        netuid,
        reg_blocks,
        current_block=current_block,
        max_new=48,
    )
    for row in metrics.values():
        if not isinstance(row, dict):
            continue
        cost = block_costs.get(row.get("reg_block"))
        if cost:
            row["reg_cost_rao"] = cost["rao"]
            row["reg_cost_tao"] = cost["tao"]


# Book-level fields that depend on the high-cardinality agent_gauges query and
# can be missing for a cycle when that upstream query returns nothing.
_BOOK_CARRY_FIELDS = (
    "pnl_books_pos",
    "pnl_books_neg",
    "hot_book_count",
    "book_count",
    "max_book_volume",
)


def _carry_forward_book_fields(metrics: Dict[str, Any]) -> None:
    """Fill in book-level fields the current refresh couldn't fetch.

    When the book query fails, fetch_agent_display_metrics_with_warnings leaves
    these fields absent rather than zeroing them. Copy the last known values
    from the previous snapshot so the dashboard keeps showing real numbers
    instead of flashing to blank/zero for a cycle.
    """
    if all(f in vals for vals in metrics.values() for f in _BOOK_CARRY_FIELDS):
        return  # nothing missing
    prev = load_daily_status()
    if not prev:
        return
    prev_metrics = prev.get("metrics", {})
    carried = 0
    for uid_str, vals in metrics.items():
        prev_vals = prev_metrics.get(uid_str)
        if not prev_vals:
            continue
        for field in _BOOK_CARRY_FIELDS:
            if field not in vals and field in prev_vals:
                vals[field] = prev_vals[field]
                carried += 1
    if carried:
        logger.warning(
            "Book-level metrics missing this cycle; carried forward %d field "
            "values from the previous snapshot", carried
        )


def refresh_daily_status() -> dict:
    from fee_rate import (
        append_fee_regime_history,
        build_fee_regime_summary,
        fetch_fee_rates,
        fetch_uid_weighted_fees,
    )
    from uid_status_checker import (
        fetch_agent_display_metrics_with_warnings,
        fetch_uid_registration_info,
    )

    metrics = fetch_agent_display_metrics_with_warnings()
    _carry_forward_book_fields(metrics)
    reg_info, current_block = fetch_uid_registration_info()
    for uid_str, reg_fields in reg_info.items():
        metrics.setdefault(uid_str, {}).update(reg_fields)
    _apply_reg_costs(metrics, current_block=current_block)

    maker_by_book, taker_by_book = fetch_fee_rates()
    fee_summary = build_fee_regime_summary(maker_by_book, taker_by_book)
    uid_fees = fetch_uid_weighted_fees(maker_by_book=maker_by_book, taker_by_book=taker_by_book)
    fee_history_path = append_fee_regime_history(fee_summary, uid_fees=uid_fees)

    path = save_daily_status(metrics, current_block=current_block)
    from miner_history import append_my_miners_history

    history_path = append_my_miners_history(metrics, current_block=current_block)

    # Every file this function exists to write is already on disk by here, so the
    # switcher — an optional side effect — must not be able to fail the refresh.
    # A TypeError in its module-level env parsing did exactly that from 2026-07-20
    # to 2026-08-12: the caller saw an exception, logged "Miner status cache refresh
    # failed" every 900s, and never reached its cache-invalidate and SSE publish,
    # even though the data had been written correctly.
    try:
        from volume_agent_switcher import maybe_switch_hot_miners_to_free_all

        agent_switches = maybe_switch_hot_miners_to_free_all(metrics)
    except Exception as exc:  # noqa: BLE001 - report, never propagate
        logger.error("Volume agent switcher skipped: %s", exc, exc_info=True)
        agent_switches = {"error": str(exc)}
    return {
        "path": path,
        "history_path": history_path,
        "fee_history_path": fee_history_path,
        "agent_switches": agent_switches,
        "date": _today_str(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "current_block": current_block,
        "uid_count": len(metrics),
        "book_volume_limit": 500_000,
        "book_volume_warn_ratio": 0.98,
        "metrics": metrics,
    }


def get_cached_metrics_payload(uids: Optional[list[int]] = None, day: Optional[str] = None) -> Optional[dict]:
    cached = load_daily_status(day)
    if not cached:
        return None
    metrics = filter_metrics(cached.get("metrics", {}), uids)
    merge_reg_costs_from_cache(metrics)
    prev = load_prev_epoch_metrics()
    return {
        "metrics": metrics,
        "prev_metrics": filter_metrics(prev.get("metrics", {}), uids) if prev else {},
        "updated_at": cached.get("updated_at"),
        "date": cached.get("date"),
        "source": "file",
        "current_block": cached.get("current_block"),
        "book_volume_limit": cached.get("book_volume_limit", 500_000),
        "book_volume_warn_ratio": cached.get("book_volume_warn_ratio", 0.98),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = refresh_daily_status()
    print(f"Saved {result['uid_count']} UIDs to {result['path']}")
