"""Runs 79Miners/Coldkeys/collect_coldkeys_ips.py on a schedule and diffs each
run against the previous one, so the dashboard can surface coldkeys/IPs that
are newly seen on the netuid-79 metagraph. collect_coldkeys_ips.py itself
stays an untouched, standalone, reusable tool - this module only adds the
"notice what's new" layer on top of it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

COLDKEYS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Coldkeys"))
COLLECT_SCRIPT = os.path.join(COLDKEYS_DIR, "collect_coldkeys_ips.py")
SETTINGS_PATH = os.path.join(COLDKEYS_DIR, "settings.json")
OUT_DIR = os.path.join(COLDKEYS_DIR, "out")

COLLECT_TIMEOUT_SECONDS = 120.0
DEFAULT_NETUID = 79


def _configured_netuid() -> int:
    """Which subnet settings.json currently points at.

    collect_coldkeys_ips.py names its output subnet<netuid>_coldkeys_ips.json,
    so these paths have to follow the same setting. Hardcoding a netuid here
    meant that changing settings.json left the watcher reading a stale file as
    both 'before' and 'after' — every run then diffed identical data and
    reported "nothing new" forever, with no error to show for it.
    """
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return int(json.load(f).get("netuid", DEFAULT_NETUID))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Could not read netuid from %s (%s); assuming %d",
                       SETTINGS_PATH, exc, DEFAULT_NETUID)
        return DEFAULT_NETUID


def _collection_path(netuid: Optional[int] = None) -> str:
    n = _configured_netuid() if netuid is None else netuid
    return os.path.join(OUT_DIR, f"subnet{n}_coldkeys_ips.json")


def _summary_path(netuid: Optional[int] = None) -> str:
    # Per-netuid too, so switching subnets doesn't compare against another
    # subnet's summary.
    n = _configured_netuid() if netuid is None else netuid
    return os.path.join(OUT_DIR, f"new_since_last_run_subnet{n}.json")


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _diff(previous: Optional[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
    cur_coldkeys = set(current["coldkeys"])
    cur_ips = set(current["ip_addresses"])
    prev_coldkeys = set(previous["coldkeys"]) if previous else None
    prev_ips = set(previous["ip_addresses"]) if previous else None

    # No baseline yet (first run ever) - nothing is "new", this run just
    # becomes the starting point everything after it gets compared against.
    new_coldkeys = sorted(cur_coldkeys - prev_coldkeys) if prev_coldkeys is not None else []
    new_ips = sorted(cur_ips - prev_ips) if prev_ips is not None else []

    return {
        "run_at": current["timestamp"],
        "is_first_run": previous is None,
        "new_coldkeys": new_coldkeys,
        "new_ips": new_ips,
        "new_coldkey_to_ips": {
            ck: current["coldkey_to_ip_addresses"].get(ck, []) for ck in new_coldkeys
        },
        "total_coldkeys": len(cur_coldkeys),
        "total_ips": len(cur_ips),
        "total_miners": current.get("total_miners"),
        "netuid": current.get("subnet"),
    }


async def run_collect_and_diff() -> Dict[str, Any]:
    """Runs collect_coldkeys_ips.py as a subprocess (keeps bittensor out of
    the uvicorn process, same reasoning as the miner-status refresh worker),
    then diffs the fresh output against whatever was on disk before the run."""
    # Resolved once up front: if settings.json changed mid-run, "before" and
    # "after" must still refer to the same subnet's file.
    netuid = _configured_netuid()
    collection_path = _collection_path(netuid)
    previous = _load_json(collection_path)

    python_path = sys.executable or "python3"
    proc = await asyncio.create_subprocess_exec(
        python_path, COLLECT_SCRIPT,
        cwd=COLDKEYS_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=COLLECT_TIMEOUT_SECONDS)
    output = stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(output or f"collect_coldkeys_ips.py exited {proc.returncode}")

    current = _load_json(collection_path)
    if current is None:
        raise RuntimeError(
            f"collect_coldkeys_ips.py finished but produced no {os.path.basename(collection_path)} "
            "— does settings.json's netuid match what it wrote?"
        )

    summary = _diff(previous, current)
    _save_json(_summary_path(netuid), summary)
    logger.info(
        "Coldkey/IP watch (netuid %s): %d new coldkey(s), %d new IP(s) "
        "(total %d coldkeys, %d IPs)",
        netuid, len(summary["new_coldkeys"]), len(summary["new_ips"]),
        summary["total_coldkeys"], summary["total_ips"],
    )
    return summary


def get_latest_summary() -> Optional[Dict[str, Any]]:
    return _load_json(_summary_path())


def get_latest_collection() -> Optional[Dict[str, Any]]:
    return _load_json(_collection_path())
