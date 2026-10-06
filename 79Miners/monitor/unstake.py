#!/usr/bin/env python3
"""
SN-79 unstake orchestration: runs the existing unstake_env.py tool (a
sibling repo at UNSTAKE_ENV_DIR, same host as this dashboard) to unstake one
of the RegBot server coldkeys, either leaving the freed TAO on that coldkey
("unstake" mode) or sweeping it to a destination address in the same step
("unstake_transfer" mode). Runs as a local subprocess — no SSH — since both
live on the same box (95.216.27.87).

This moves real, irreversible TAO. Every real (non-dry-run) execution is
appended to an audit log for accountability.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from regbot import SERVER_PRESETS, DASHBOARD_ROOT

logger = logging.getLogger(__name__)

_PRESETS_BY_NAME = {p["name"]: p for p in SERVER_PRESETS}

UNSTAKE_ENV_DIR = "/root/work/bittensor/bittensor-reg-script/unstake-transfer"
UNSTAKE_ENV_SCRIPT = os.path.join(UNSTAKE_ENV_DIR, "unstake_env.py")
UNSTAKE_ENV_PYTHON = os.path.join(UNSTAKE_ENV_DIR, ".venv", "bin", "python")
DEFAULT_NETUID = 79

# unstake_multiple submits ONE extrinsic PER hotkey, sequentially, each with
# wait_for_inclusion=True and wait_for_finalization=True — for a wallet with
# many hotkeys this can genuinely take many minutes (observed: a 120s
# timeout killed a real run partway through, after it had already unstaked
# some hotkeys but not all). A dry-run only reads chain state, so it stays
# fast regardless of hotkey count.
PREVIEW_TIMEOUT_SECONDS = 60.0
RUN_TIMEOUT_SECONDS = 1800.0

AUDIT_LOG_PATH = os.path.join(DASHBOARD_ROOT, "79Miners", "Unstake", "unstake_runs.jsonl")
LOG_DIR = os.path.join(DASHBOARD_ROOT, "79Miners", "Unstake", "logs")

# Real runs (not preview) are tracked here so the UI can poll progress instead
# of blocking one fetch for up to RUN_TIMEOUT_SECONDS — same run_id/poll shape
# as RegBot's trigger_run()/tail_log().
_active_runs: Dict[str, Dict[str, Any]] = {}
_run_tasks: Dict[str, "asyncio.Task[None]"] = {}

# Loose SS58 sanity check (base58 alphabet, plausible length) — not full
# checksum validation. Just enough to catch an obvious typo/garbage paste
# before spending an extrinsic fee (or worse, sending real TAO) toward a
# doomed or wrong destination. The chain itself is the final arbiter.
_SS58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{46,48}$")


MODES = ("unstake", "unstake_transfer", "transfer")
# Modes that move TAO off the coldkey, and therefore require a destination.
# "transfer" sweeps only the free balance already sitting there (no unstake).
TRANSFER_MODES = ("unstake_transfer", "transfer")

# Where each coldkey's TAO is swept by default. Only a pre-fill for the forms —
# every destination is still shown in the confirm dialog and can be overridden
# per run. Keyed by wallet name, since that's what identifies the coldkey.
DEFAULT_DESTINATIONS: Dict[str, str] = {
    "bittensor001": "5Ehjf6NtQXDNGUKKPQrFwYUnYnYw8giVarJerUXiDmCzg1jk",
    "bittensor002": "5Ehjf6NtQXDNGUKKPQrFwYUnYnYw8giVarJerUXiDmCzg1jk",
    "bittensor005": "5DUeuL8A8NukziacaDkztXEGnC3kJrEQvXC1ujmidt5GT6br",
    "bittensor006": "5DUeuL8A8NukziacaDkztXEGnC3kJrEQvXC1ujmidt5GT6br",
    "bittensor007": "5DUeuL8A8NukziacaDkztXEGnC3kJrEQvXC1ujmidt5GT6br",
    "wolf001": "5GRUugodxkeVw1F9yL2Dc4t1saobDE1VLoW7LRy6513CXiN5",
    "wolf002": "5GRUugodxkeVw1F9yL2Dc4t1saobDE1VLoW7LRy6513CXiN5",
    # w003 and w005 sweep somewhere different from the other wolf coldkeys.
    "wolf003": "5DaBPf7w764Y5n6VNRE1aMPQdmkubNjRXqvu1heUQFuSSbUp",
}


class UnstakeError(ValueError):
    """User-facing validation error (bad server/mode/destination)."""


def get_unstake_server_presets() -> List[Dict[str, str]]:
    presets = []
    for p in SERVER_PRESETS:
        entry = dict(p)
        entry["default_destination"] = DEFAULT_DESTINATIONS.get(p["wallet"])
        presets.append(entry)
    return presets


def _validate(
    server: str, mode: str, destination: Optional[str], amount: Optional[float] = None
) -> Dict[str, str]:
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise UnstakeError(f"Unknown server '{server}'")
    if mode not in MODES:
        raise UnstakeError(f"mode must be one of {MODES}")
    if mode in TRANSFER_MODES and (not destination or not _SS58_RE.match(destination)):
        raise UnstakeError("Destination must be a valid SS58 address")
    if amount is not None:
        if mode != "transfer":
            raise UnstakeError("amount only applies to 'transfer' mode")
        if not isinstance(amount, (int, float)) or amount <= 0:
            raise UnstakeError("amount must be a positive number of TAO")
    return preset


def _read_log(log_path: str) -> str:
    if not os.path.isfile(log_path):
        return ""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        return f.read().strip()


async def _run_unstake_env(
    coldkey: str,
    netuid: int,
    mode: str,
    destination: Optional[str],
    dry_run: bool,
    timeout: float,
    log_path: Optional[str] = None,
    amount: Optional[float] = None,
) -> Dict[str, Any]:
    """log_path=None (preview): buffer output in memory, return it all at once
    when the short-lived dry-run finishes — same as before. log_path set (a
    real run): stream stdout straight to that file so a poller can tail it
    while the run is still in progress, up to RUN_TIMEOUT_SECONDS long."""
    cmd = [
        UNSTAKE_ENV_PYTHON, UNSTAKE_ENV_SCRIPT, mode,
        "--coldkey", coldkey,
        "--netuid", str(netuid),
    ]
    if mode in TRANSFER_MODES:
        cmd += ["--destination", destination]
    # Omitted entirely when unset, so unstake_env.py falls back to sweeping the
    # whole balance — its own default, not one restated here.
    if amount is not None:
        cmd += ["--amount", str(amount)]
    if dry_run:
        cmd.append("--dry-run")

    # Without this, Python block-buffers stdout when it isn't a TTY (the norm
    # once redirected to a file/pipe) — prints would sit in an internal
    # buffer and only appear at process exit, defeating the point of tailing
    # a growing log file while the run is still in progress.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    log_file = open(log_path, "wb") if log_path else None

    # start_new_session=True puts this child in its own process group,
    # detached from the dashboard's. Without it, a `pm2 restart dashboard`
    # (e.g. to deploy an unrelated code change) sends its termination signal
    # to the whole process group — which reaches this child too, killing an
    # in-flight real unstake mid-extrinsic (observed in practice: a restart
    # SIGINT'd a run partway through its 5th hotkey, surfacing as a scary
    # KeyboardInterrupt traceback even though the on-chain work had actually
    # already gone through). Isolating the process group means the dashboard
    # can restart freely without ever touching a real, already-running
    # unstake — same reasoning as RegBot's setsid detachment.
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_file if log_file else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=UNSTAKE_ENV_DIR,  # unstake_env.py's load_dotenv() finds .env via cwd
            start_new_session=True,  # own process group — see below
            env=env,
        )
        try:
            if log_file:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                output = _read_log(log_path)
            else:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                output = stdout.decode("utf-8", "replace").strip()
            return {
                "success": proc.returncode == 0,
                "output": output,
                "returncode": proc.returncode,
                "timed_out": False,
            }
        except asyncio.TimeoutError:
            # Killing the client-side wrapper does NOT undo whatever it already
            # submitted to the chain — hotkeys unstaked one at a time, so some
            # may have genuinely completed before the kill. Capture whatever
            # partial output already exists instead of discarding it, since
            # it's the only record of exactly what happened.
            proc.kill()
            if log_file:
                await proc.wait()
                partial = _read_log(log_path)
            else:
                partial_stdout, _ = await proc.communicate()
                partial = partial_stdout.decode("utf-8", "replace").strip()
            note = (
                f"[dashboard] No response from unstake_env.py after {timeout:.0f}s — "
                "killed the wrapper process. This does NOT mean nothing happened: "
                "hotkeys are unstaked one at a time, each waiting for on-chain "
                "finalization, so large wallets can genuinely take this long. "
                "Whatever is shown above (if anything) completed and is real, "
                "on-chain. It's safe to re-run the same action — it only acts on "
                "hotkeys still above the stake threshold, so it picks up exactly "
                "where this left off."
            )
            return {
                "success": False,
                "output": (partial + "\n\n" + note) if partial else note,
                "returncode": None,
                "timed_out": True,
            }
    finally:
        if log_file:
            log_file.close()


def _append_audit_log(entry: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


async def preview_unstake(
    server: str, mode: str, destination: Optional[str] = None, netuid: int = DEFAULT_NETUID,
    amount: Optional[float] = None,
) -> Dict[str, Any]:
    """Dry-run: shows exactly what would happen (hotkeys to unstake, free
    balance, and — for unstake_transfer — the sendable amount) without
    submitting anything to the chain."""
    preset = _validate(server, mode, destination, amount)
    result = await _run_unstake_env(
        preset["wallet"], netuid, mode, destination, dry_run=True,
        timeout=PREVIEW_TIMEOUT_SECONDS, amount=amount,
    )
    return {
        "server": server,
        "wallet": preset["wallet"],
        "netuid": netuid,
        "mode": mode,
        "destination": destination,
        "amount": amount,
        **result,
    }


def get_active_unstake_runs() -> List[Dict[str, Any]]:
    return [run for run in _active_runs.values() if run["status"] == "RUNNING"]


def get_active_unstake_runs_view() -> List[Dict[str, Any]]:
    """Same as get_active_unstake_runs() but formatted for display — adds the
    log's last line, drops internal fields (log_path). Covers both solo runs
    and in-flight batch items, since a batch item is just a regular run under
    the hood — one active-run bar shows both cases."""
    return [
        {
            "server": run["server"],
            "run_id": run["run_id"],
            "wallet": run["wallet"],
            "mode": run["mode"],
            "destination": run["destination"],
            "started_at": run["started_at"],
            "last_line": _last_line(_read_log(run["log_path"])),
        }
        for run in get_active_unstake_runs()
    ]


def _server_busy(server: str) -> bool:
    # Per-server, not global — separate coldkeys are independent, so running
    # unstakes on different servers concurrently (multi-server batch) is
    # fine. Only a second run on the SAME coldkey while one is still in
    # flight is a real conflict (double-submit risk).
    return any(r["server"] == server for r in get_active_unstake_runs())


async def start_unstake_run(
    server: str,
    mode: str,
    destination: Optional[str] = None,
    netuid: int = DEFAULT_NETUID,
    triggered_by: str = "dashboard",
    amount: Optional[float] = None,
) -> Dict[str, Any]:
    """The real thing — unstakes (and, for unstake_transfer, sends) real TAO.
    Irreversible. Launches as a background task and returns immediately with
    a run_id, so the UI can poll get_unstake_run_status() for live progress
    instead of blocking one request for up to RUN_TIMEOUT_SECONDS (30 min) —
    same shape as RegBot's trigger_run()/tail_log()."""
    if _server_busy(server):
        raise UnstakeError(
            f"An unstake run is already in progress for {server} — wait for it to finish before starting another."
        )
    preset = _validate(server, mode, destination, amount)
    os.makedirs(LOG_DIR, exist_ok=True)
    run_id = f"{server}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    log_path = os.path.join(LOG_DIR, f"{run_id}.log")

    run: Dict[str, Any] = {
        "run_id": run_id,
        "server": server,
        "wallet": preset["wallet"],
        "mode": mode,
        "destination": destination,
        "amount": amount,
        "netuid": netuid,
        "log_path": log_path,
        "status": "RUNNING",
        "success": None,
        "timed_out": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    _active_runs[run_id] = run
    _run_tasks[run_id] = asyncio.create_task(
        _do_unstake_run(run_id, preset, mode, destination, netuid, triggered_by, amount)
    )
    return dict(run)


async def _do_unstake_run(
    run_id: str,
    preset: Dict[str, str],
    mode: str,
    destination: Optional[str],
    netuid: int,
    triggered_by: str,
    amount: Optional[float] = None,
) -> None:
    run = _active_runs[run_id]
    try:
        result = await _run_unstake_env(
            preset["wallet"], netuid, mode, destination,
            dry_run=False, timeout=RUN_TIMEOUT_SECONDS, log_path=run["log_path"],
            amount=amount,
        )
        run["success"] = result["success"]
        run["timed_out"] = result["timed_out"]

        # Always logged and always followed by a balance-cache refresh, even
        # on timeout/failure — a non-success result here does not mean
        # nothing happened on-chain (see _run_unstake_env).
        entry = {
            "triggered_at": run["started_at"],
            "triggered_by": triggered_by,
            "server": run["server"],
            "wallet": preset["wallet"],
            "netuid": netuid,
            "mode": mode,
            "destination": destination,
            "amount": amount,
            **result,
        }
        _append_audit_log(entry)
        logger.info(
            "Unstake run: server=%s wallet=%s mode=%s destination=%s success=%s timed_out=%s",
            run["server"], preset["wallet"], mode, destination,
            result["success"], result.get("timed_out"),
        )
        try:
            from regbot import refresh_balance_cache

            await refresh_balance_cache()  # balance/stake may have changed regardless of outcome
        except Exception as exc:
            logger.warning("Post-unstake balance cache refresh failed: %s", exc)
    except Exception as exc:
        logger.exception("Unstake run %s crashed", run_id)
        run["success"] = False
        try:
            with open(run["log_path"], "a", encoding="utf-8") as f:
                f.write(f"\n[dashboard] Unexpected error: {exc}\n")
        except OSError:
            pass
    finally:
        run["status"] = "FINISHED"
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        _run_tasks.pop(run_id, None)


def get_unstake_run_status(run_id: str) -> Dict[str, Any]:
    run = _active_runs.get(run_id)
    if not run:
        raise UnstakeError(f"Unknown run '{run_id}'")
    return {
        "run_id": run_id,
        "server": run["server"],
        "mode": run["mode"],
        "destination": run["destination"],
        "amount": run.get("amount"),
        "status": run["status"],
        "success": run.get("success"),
        "timed_out": run.get("timed_out", False),
        "started_at": run["started_at"],
        "finished_at": run.get("finished_at"),
        "output": _read_log(run["log_path"]),
    }


BATCH_POLL_INTERVAL_SECONDS = 5.0

# Multi-server batch: one coldkey per server, all launched in parallel — see
# start_unstake_batch() docstring for why this differs from RegBot's batch
# (which serializes on purpose).
_active_batches: Dict[str, Dict[str, Any]] = {}
_batch_tasks: Dict[str, "asyncio.Task[None]"] = {}


def _last_line(text: str) -> Optional[str]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else None


def get_active_unstake_batch() -> Optional[Dict[str, Any]]:
    for batch in _active_batches.values():
        if batch["status"] == "RUNNING":
            return batch
    return None


def get_unstake_batch_status(batch_id: str) -> Dict[str, Any]:
    batch = _active_batches.get(batch_id)
    if not batch:
        raise UnstakeError(f"Unknown batch '{batch_id}'")
    return batch


async def start_unstake_batch(
    items: List[Dict[str, Any]], triggered_by: str = "dashboard"
) -> Dict[str, Any]:
    """Unstakes (and, per-item, transfers) multiple coldkeys at once — one
    server per item, all launched together. Unlike RegBot's batch, there's no
    shared price limit to protect by serializing: each coldkey's unstake is
    fully independent, so running them at the same time is both what was
    asked for and the technically correct call here."""
    if get_active_unstake_batch() is not None:
        raise UnstakeError(
            "An unstake batch is already running — wait for it to finish before starting another."
        )
    if not items:
        raise UnstakeError("Select at least one server.")

    seen_servers = set()
    for it in items:
        if it["server"] in seen_servers:
            raise UnstakeError(f"Server '{it['server']}' selected more than once.")
        seen_servers.add(it["server"])
        _validate(it["server"], it["mode"], it.get("destination"), it.get("amount"))
        if _server_busy(it["server"]):
            raise UnstakeError(f"An unstake run is already in progress for {it['server']}.")

    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    queue = [
        {
            "server": it["server"],
            "mode": it["mode"],
            "destination": it.get("destination"),
            "amount": it.get("amount"),
            "status": "PENDING",
            "run_id": None,
            "success": None,
            "timed_out": False,
            "last_line": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
        }
        for it in items
    ]
    batch: Dict[str, Any] = {
        "batch_id": batch_id,
        "status": "RUNNING",
        "queue": queue,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    _active_batches[batch_id] = batch
    _batch_tasks[batch_id] = asyncio.create_task(_run_unstake_batch(batch_id, triggered_by))
    return batch


async def _run_one_batch_item(item: Dict[str, Any], batch_id: str) -> None:
    item["status"] = "RUNNING"
    item["started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        run = await start_unstake_run(
            item["server"], item["mode"], item.get("destination"),
            amount=item.get("amount"),
            triggered_by=f"batch:{batch_id}",
        )
    except Exception as exc:
        item["status"] = "LAUNCH_FAILED"
        item["error"] = str(exc)
        item["finished_at"] = datetime.now(timezone.utc).isoformat()
        return

    item["run_id"] = run["run_id"]
    while True:
        status = get_unstake_run_status(run["run_id"])
        item["last_line"] = _last_line(status["output"])
        if status["status"] == "FINISHED":
            item["status"] = "DONE"
            item["success"] = status["success"]
            item["timed_out"] = status["timed_out"]
            item["finished_at"] = datetime.now(timezone.utc).isoformat()
            return
        await asyncio.sleep(BATCH_POLL_INTERVAL_SECONDS)


async def _run_unstake_batch(batch_id: str, triggered_by: str) -> None:
    batch = _active_batches[batch_id]
    try:
        await asyncio.gather(
            *[_run_one_batch_item(item, batch_id) for item in batch["queue"]]
        )
    finally:
        batch["status"] = "DONE"
        batch["finished_at"] = datetime.now(timezone.utc).isoformat()
        _batch_tasks.pop(batch_id, None)


def list_recent_runs(limit: int = 50) -> List[Dict[str, Any]]:
    if not os.path.isfile(AUDIT_LOG_PATH):
        return []
    rows: List[Dict[str, Any]] = []
    with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:][::-1]
