#!/usr/bin/env python3
"""
SN-79 RegBot orchestration: trigger Paul-SuperRegBot/run79.sh on a remote
miner server from the dashboard.

For the selected server, this generates a fresh copy of the run79.sh template
(79Miners/RegBot/run79.sh) with DISPATCH_AGENT / REGLIMIT / MAX_COUNT baked in
(WALLET comes from the server preset, not user input), scp's it to
Paul-SuperRegBot/dashboard_runs/ on that server, and launches it detached
(nohup, output redirected to a matching .log file) so registration + the
eventual pm2 miner launch can run for as long as they need without blocking
the HTTP request.

Every trigger is appended to a local JSONL audit log (regbot_runs.jsonl) —
this spends real TAO (up to REGLIMIT per hotkey, MAX_COUNT hotkeys), so we
keep a record of who/what was launched, when, and with which parameters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_ROOT = os.path.dirname(os.path.dirname(_HERE))  # .../subnet_dashboard
TEMPLATE_PATH = os.path.join(DASHBOARD_ROOT, "79Miners", "RegBot", "run79.sh")
AUDIT_LOG_PATH = os.path.join(DASHBOARD_ROOT, "79Miners", "RegBot", "regbot_runs.jsonl")

REMOTE_REGBOT_DIR = "/root/79/Paul-SuperRegBot"
REMOTE_RUNS_SUBDIR = "dashboard_runs"
# Where creg79.py --launch-miner writes each new run_miner_<uid>.json on the
# registering server (creg79.py already scp's this back here on its own —
# this is a belt-and-suspenders pull for if that push ever fails silently).
REMOTE_MINER_REPO = "/root/79/sn-79"
LOCAL_PARAMS_DIR = os.path.join(DASHBOARD_ROOT, "79Miners", "params")
SSH_USER = "root"
SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]

# Server presets: dashboard "Server" selection -> (ip, wallet). Keep in sync
# with SERVER_NICKNAMES in app.py / miners.html.
SERVER_PRESETS: List[Dict[str, str]] = [
    {"name": "Server1", "ip": "65.108.232.96", "wallet": "bittensor001"},
    {"name": "Server2", "ip": "65.108.108.139", "wallet": "bittensor002"},
    {"name": "Server3", "ip": "65.109.87.169", "wallet": "bittensor005"},
    {"name": "Server4", "ip": "178.16.143.193", "wallet": "bittensor006"},
    {"name": "Server5", "ip": "144.91.111.126", "wallet": "bittensor007"},
    {"name": "Server6", "ip": "65.109.65.81", "wallet": "wolf003"},
    {"name": "Server7", "ip": "84.32.34.89", "wallet": "wolf002"},
]
_PRESETS_BY_NAME = {p["name"]: p for p in SERVER_PRESETS}

# Fallback dispatch-agent list if the reference params file is unavailable.
_DEFAULT_AGENT_OPTIONS = ["WarriorAgent", "RandomMakerAgentGamma", "FreeAllAgent"]

REGLIMIT_MIN, REGLIMIT_MAX = 0.0001, 5.0
MAX_COUNT_MIN, MAX_COUNT_MAX = 1, 10

RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


class RegBotError(ValueError):
    """User-facing validation error (bad server/agent/reglimit/max_count)."""


def get_server_presets() -> List[Dict[str, str]]:
    return [dict(p) for p in SERVER_PRESETS]


LOCAL_WALLETS_DIR = "/root/.bittensor/wallets"


def _wallet_coldkey_address(wallet: str) -> Optional[str]:
    """Read a wallet's public coldkey ss58 address from its local coldkeypub.txt
    (creg79.py rsyncs each wallet here on registration, so these stay current)."""
    path = os.path.join(LOCAL_WALLETS_DIR, wallet, "coldkeypub.txt")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("ss58Address")
    except (OSError, json.JSONDecodeError, TypeError):
        return None


async def get_server_balance(server: str) -> Dict[str, Any]:
    """Free TAO balance of the coldkey behind this server's preset wallet."""
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    wallet = preset["wallet"]
    address = _wallet_coldkey_address(wallet)
    if not address:
        raise RegBotError(
            f"Could not resolve coldkey address for wallet '{wallet}' "
            f"(missing {LOCAL_WALLETS_DIR}/{wallet}/coldkeypub.txt)"
        )
    from get_reg_cost import get_balance_for_address

    result = await asyncio.to_thread(get_balance_for_address, address)
    if result is None:
        raise RuntimeError(f"Failed to fetch balance for wallet '{wallet}'")
    return {"server": server, "wallet": wallet, **result}


def get_dispatch_agent_options() -> List[str]:
    """Known DispatcherAgent sub-agent names, sourced from a reference params file."""
    params_dir = os.path.join(DASHBOARD_ROOT, "79Miners", "params")
    try:
        for name in sorted(os.listdir(params_dir)):
            if not (name.startswith("run_miner_") and name.endswith(".json")):
                continue
            with open(os.path.join(params_dir, name), encoding="utf-8") as f:
                data = json.load(f)
            agents = data.get("agents")
            if isinstance(agents, list) and agents:
                names = [a.get("name") for a in agents if isinstance(a.get("name"), str)]
                if names:
                    return names
    except OSError:
        pass
    return list(_DEFAULT_AGENT_OPTIONS)


def _validate_inputs(server: str, dispatch_agent: str, reglimit: float, max_count: int) -> Dict[str, str]:
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    if not dispatch_agent or not re.match(r"^[A-Za-z0-9_]+$", dispatch_agent):
        raise RegBotError("dispatch_agent must be a plain agent name (letters/digits/underscore)")
    if not (REGLIMIT_MIN <= reglimit <= REGLIMIT_MAX):
        raise RegBotError(f"reglimit must be between {REGLIMIT_MIN} and {REGLIMIT_MAX}")
    if not (MAX_COUNT_MIN <= max_count <= MAX_COUNT_MAX):
        raise RegBotError(f"max_count must be between {MAX_COUNT_MIN} and {MAX_COUNT_MAX}")
    return preset


def _build_script(dispatch_agent: str, reglimit: float, max_count: int, wallet: str) -> str:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        content = f.read()

    def sub(pattern: str, replacement: str, text: str) -> str:
        new_text, n = re.subn(pattern, replacement, text, count=1, flags=re.M)
        if n != 1:
            raise RegBotError(f"Template substitution failed for pattern: {pattern}")
        return new_text

    # The template's `cd "$(dirname "$0")"` assumes it lives directly in
    # Paul-SuperRegBot/ alongside .venv and creg79.py. We instead scp generated
    # copies into Paul-SuperRegBot/dashboard_runs/, so that would cd into the
    # wrong directory and creg79.py/.venv wouldn't resolve. Hardcode the real
    # directory so the script works regardless of where the file itself sits.
    content = sub(
        r'^cd "\$\(dirname "\$0"\)" \|\| exit 1',
        f'cd "{REMOTE_REGBOT_DIR}" || exit 1',
        content,
    )

    content = sub(
        r'^DISPATCH_AGENT="\$\{DISPATCH_AGENT:-[^}]*\}"',
        f'DISPATCH_AGENT="${{DISPATCH_AGENT:-{dispatch_agent}}}"',
        content,
    )
    content = sub(
        r'^REGLIMIT="\$\{REGLIMIT:-[^}]*\}"',
        f'REGLIMIT="${{REGLIMIT:-{reglimit}}}"',
        content,
    )
    content = sub(
        r'^MAX_COUNT="\$\{MAX_COUNT:-[^}]*\}"',
        f'MAX_COUNT="${{MAX_COUNT:-{max_count}}}"',
        content,
    )
    content = sub(
        r'^WALLET="\$\{WALLET:-[^}]*\}"(.*)$',
        f'WALLET="${{WALLET:-{wallet}}}"\\1',
        content,
    )
    return content


def _make_run_id() -> str:
    # epoch-seconds based; timestamps are supplied by the caller (FastAPI request time)
    # to keep this module free of direct time.time()/datetime.now() side effects in tests.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _remote_run_path(run_id: str, ext: str) -> str:
    return f"{REMOTE_REGBOT_DIR}/{REMOTE_RUNS_SUBDIR}/run79_{run_id}.{ext}"


async def _run_subprocess(cmd: List[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _append_audit_log(entry: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


async def trigger_run(
    server: str, dispatch_agent: str, reglimit: float, max_count: int, triggered_by: str = "dashboard"
) -> Dict[str, Any]:
    """Generate a run79.sh for this server+params, scp it over, launch it detached."""
    preset = _validate_inputs(server, dispatch_agent, reglimit, max_count)
    ip, wallet = preset["ip"], preset["wallet"]

    script_content = _build_script(dispatch_agent, reglimit, max_count, wallet)
    run_id = f"{server}_{_make_run_id()}"
    script_name = f"run79_{run_id}.sh"
    remote_dir = f"{REMOTE_REGBOT_DIR}/{REMOTE_RUNS_SUBDIR}"
    remote_script = f"{remote_dir}/{script_name}"
    remote_log = _remote_run_path(run_id, "log")
    remote_pid = _remote_run_path(run_id, "pid")

    local_tmp = f"/tmp/{script_name}"
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script_content)
    try:
        os.chmod(local_tmp, 0o700)

        mkdir_cmd = ["ssh", *SSH_OPTS, f"{SSH_USER}@{ip}", f"mkdir -p {remote_dir}"]
        rc, out, err = await _run_subprocess(mkdir_cmd, timeout=20)
        if rc != 0:
            raise RuntimeError(f"mkdir on {server} failed: {err.strip() or out.strip()}")

        scp_cmd = ["scp", *SSH_OPTS, local_tmp, f"{SSH_USER}@{ip}:{remote_script}"]
        rc, out, err = await _run_subprocess(scp_cmd, timeout=30)
        if rc != 0:
            raise RuntimeError(f"scp to {server} failed: {err.strip() or out.strip()}")
    finally:
        try:
            os.remove(local_tmp)
        except OSError:
            pass

    # chmod +x, then launch fully detached so the SSH command returns
    # immediately regardless of how long the registration itself ends up
    # taking (it polls the chain price and can legitimately run for hours).
    #
    # Everything is wrapped in ONE subshell, backgrounded as a single atomic
    # unit (`(...) &`), with `exec` replacing that subshell with setsid on
    # success — this is the standard robust ssh-detach idiom. The earlier
    # version chained `cmd1 && cmd2 && cmd3 &` followed by separate `disown`/
    # `echo` statements; in a *non-interactive* shell (no job control unless
    # `set -m` is set), plain `disown` is a no-op, so bash still implicitly
    # waited for that backgrounded job before letting the script reach its
    # own end — meaning the launch command never actually returned early, and
    # every trigger's 20s timeout fired even though the underlying process
    # (kept alive by setsid) was running fine. `set -m` + `disown -a` fixes
    # `disown` itself too, as defense in depth.
    #
    # PID capture: the process must report its OWN pid from inside itself
    # (via an inner `sh -c 'echo $$ ...; exec SCRIPT'`), NOT via `$!` from the
    # outer shell right after backgrounding. `setsid CMD` forks internally
    # whenever its caller is a process-group leader (which `set -m` makes the
    # backgrounded subshell) — in that case setsid's own process exits right
    # after forking off the real, longer-lived child, so `$!` captured from
    # outside goes stale within seconds even though the actual work keeps
    # running. `$$` written from inside the process that then `exec`s into
    # the real script is correct regardless of whether setsid forked to get
    # there, since `exec` preserves the pid.
    inner = f"echo $$ > {remote_pid}; exec {remote_script}"
    launch_cmd = (
        f"set -m; "
        f"(chmod +x {remote_script} && cd {REMOTE_REGBOT_DIR} && exec setsid sh -c '{inner}') "
        f"</dev/null >{remote_log} 2>&1 & "
        f"disown -a; echo LAUNCHED"
    )
    ssh_launch = ["ssh", *SSH_OPTS, f"{SSH_USER}@{ip}", launch_cmd]
    rc, out, err = await _run_subprocess(ssh_launch, timeout=20)
    if rc != 0 or "LAUNCHED" not in out:
        raise RuntimeError(f"launch on {server} failed: {err.strip() or out.strip()}")

    entry = {
        "run_id": run_id,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "triggered_by": triggered_by,
        "server": server,
        "ip": ip,
        "wallet": wallet,
        "dispatch_agent": dispatch_agent,
        "reglimit": reglimit,
        "max_count": max_count,
        "remote_script": remote_script,
        "remote_log": remote_log,
        "remote_pid": remote_pid,
    }
    _append_audit_log(entry)
    logger.info(
        "RegBot run triggered: server=%s wallet=%s agent=%s reglimit=%s max_count=%s run_id=%s",
        server, wallet, dispatch_agent, reglimit, max_count, run_id,
    )
    return entry


# run_id -> list of UIDs already pulled for it, so tail_log()'s polling
# doesn't re-scp on every 4s refresh once a run has finished.
_synced_uids_cache: Dict[str, List[str]] = {}


async def sync_new_params(server: str, run_id: str) -> List[str]:
    """Pull run_miner_<uid>.json for each hotkey THIS run newly registered.

    The new UID(s) are identified precisely from creg79.py's own
    "Stamped miner_ip=... into run_miner_<uid>.json" log line — NOT by file
    mtime. creg79.py's default --propagate-agents behavior rewrites (and
    fleet-wide re-syncs) EVERY existing miner's control file on the server
    whenever any one dispatcher miner is launched, so a time-based "what
    changed since this run started" check would false-positive on unrelated,
    already-registered miners.

    creg79.py already scp's the new file(s) back here on its own by default —
    this is a redundant pull in case that push ever fails silently.
    """
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    ip = preset["ip"]
    remote_log = _remote_run_path(run_id, "log")

    grep_cmd = [
        "ssh", *SSH_OPTS, f"{SSH_USER}@{ip}",
        f"grep -oE 'into run_miner_[0-9]+\\.json' {remote_log} 2>/dev/null "
        f"| grep -oE '[0-9]+' | sort -un",
    ]
    rc, out, err = await _run_subprocess(grep_cmd, timeout=20)
    if rc != 0:
        raise RuntimeError(f"grep on {server} failed: {err.strip() or out.strip()}")
    new_uids = [u.strip() for u in out.splitlines() if u.strip()]

    os.makedirs(LOCAL_PARAMS_DIR, exist_ok=True)
    synced: List[str] = []
    for uid in new_uids:
        remote_path = f"{REMOTE_MINER_REPO}/run_miner_{uid}.json"
        local_path = os.path.join(LOCAL_PARAMS_DIR, f"run_miner_{uid}.json")
        scp_cmd = ["scp", *SSH_OPTS, f"{SSH_USER}@{ip}:{remote_path}", local_path]
        rc, out, err = await _run_subprocess(scp_cmd, timeout=20)
        if rc != 0:
            logger.warning(
                "Failed to pull run_miner_%s.json from %s: %s", uid, server, err.strip() or out.strip()
            )
            continue
        synced.append(uid)
    if synced:
        logger.info(
            "RegBot synced params for newly-registered UID(s) %s from %s (run %s)", synced, server, run_id
        )
    return sorted(synced, key=int)


async def kill_run(server: str, run_id: str) -> Dict[str, Any]:
    """Terminate an active run's whole process group (TERM, then KILL if it
    doesn't stop within 2s). setsid makes the tracked pid its own process
    group leader, so `kill -SIG -<pid>` (negative pid) reaches the run79.sh
    shell AND its creg79.py child together — a plain `kill <pid>` would only
    hit the shell, orphaning creg79.py to keep running.

    This only stops the registration wait-loop. Any miner it already
    launched via pm2 before being killed is a separate, independent process
    and is unaffected — this cannot un-register or reclaim a spent hotkey.
    """
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    if not RUN_ID_RE.match(run_id):
        raise RegBotError("Invalid run_id")
    ip = preset["ip"]
    remote_pid = _remote_run_path(run_id, "pid")

    cmd = [
        "ssh", *SSH_OPTS, f"{SSH_USER}@{ip}",
        f'pid=$(cat {remote_pid} 2>/dev/null); '
        f'if [ -z "$pid" ]; then echo NO_PID; exit 0; fi; '
        f'if ! kill -0 "$pid" 2>/dev/null; then echo ALREADY_STOPPED; exit 0; fi; '
        f'kill -TERM -"$pid" 2>/dev/null; sleep 2; '
        f'if ! kill -0 "$pid" 2>/dev/null; then echo KILLED; exit 0; fi; '
        f'kill -KILL -"$pid" 2>/dev/null; sleep 1; '
        f'if kill -0 "$pid" 2>/dev/null; then echo KILL_FAILED; else echo KILLED_FORCE; fi',
    ]
    rc, out, err = await _run_subprocess(cmd, timeout=20)
    result = out.strip()
    if rc != 0:
        raise RuntimeError(f"kill on {server} failed: {err.strip() or result}")
    logger.info("RegBot kill run=%s server=%s result=%s", run_id, server, result)
    return {"server": server, "run_id": run_id, "result": result or "UNKNOWN"}


async def tail_log(server: str, run_id: str, lines: int = 200) -> Dict[str, Any]:
    """Tail the remote log for a run, and report whether the process is still running."""
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    if not RUN_ID_RE.match(run_id):
        raise RegBotError("Invalid run_id")
    ip = preset["ip"]
    remote_log = _remote_run_path(run_id, "log")
    remote_pid = _remote_run_path(run_id, "pid")

    lines = max(1, min(int(lines), 2000))
    # `kill -0 <pid>` (not `pgrep -f <script name>`) — a pgrep pattern search
    # would match the very SSH command running the check, since its own text
    # necessarily contains the script name (see trigger_run's launch_cmd).
    cmd = [
        "ssh", *SSH_OPTS, f"{SSH_USER}@{ip}",
        f"tail -n {lines} {remote_log} 2>&1; "
        f"echo '---STATUS---'; "
        f"kill -0 \"$(cat {remote_pid} 2>/dev/null)\" 2>/dev/null && echo RUNNING || echo FINISHED",
    ]
    rc, out, err = await _run_subprocess(cmd, timeout=20)
    if rc != 0 and "---STATUS---" not in out:
        raise RuntimeError(f"log fetch from {server} failed: {err.strip() or out.strip()}")

    if "---STATUS---" in out:
        body, _, status_part = out.rpartition("---STATUS---")
    else:
        body, status_part = out, ""
    status = "RUNNING" if "RUNNING" in status_part else "FINISHED"

    synced_uids: List[str] = _synced_uids_cache.get(run_id, [])
    sync_error: Optional[str] = None
    if status == "FINISHED" and run_id not in _synced_uids_cache:
        try:
            synced_uids = await sync_new_params(server, run_id)
            _synced_uids_cache[run_id] = synced_uids  # only cache on success —
            # a failed attempt shouldn't block a retry on the next manual refresh
        except Exception as exc:  # noqa: BLE001 — surface, don't fail the log fetch
            sync_error = str(exc)
            logger.warning("RegBot params sync failed for run %s: %s", run_id, exc)
            synced_uids = []

    return {
        "server": server,
        "run_id": run_id,
        "status": status,
        "log": body.strip(),
        "synced_uids": synced_uids,
        "sync_error": sync_error,
    }


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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_REGISTERED_RE = re.compile(r"(\d+)/(\d+)\s+registered")


def _run_id_started_at(run_id: str) -> Optional[str]:
    """run_id is '{server}_{YYYYMMDDTHHMMSSZ}' (server names have no underscore)."""
    _, _, ts = run_id.partition("_")
    try:
        return datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


async def _check_server_active(preset: Dict[str, str]) -> Dict[str, Any]:
    """List live run_ids (via .pid + kill -0) on one server. Never raises —
    an unreachable server is reported as such, not treated as a hard error."""
    ip = preset["ip"]
    remote_dir = f"{REMOTE_REGBOT_DIR}/{REMOTE_RUNS_SUBDIR}"
    cmd = [
        "ssh", *SSH_OPTS, f"{SSH_USER}@{ip}",
        f'd={remote_dir}; for f in "$d"/*.pid; do [ -f "$f" ] || continue; '
        f'pid=$(cat "$f" 2>/dev/null); '
        f'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then basename "$f" .pid; fi; done',
    ]
    try:
        rc, out, err = await _run_subprocess(cmd, timeout=12)
    except asyncio.TimeoutError:
        return {"server": preset["name"], "reachable": False, "run_ids": []}
    if rc != 0:
        return {"server": preset["name"], "reachable": False, "run_ids": []}
    prefix = "run79_"
    run_ids = [
        line.strip()[len(prefix):]
        for line in out.splitlines()
        if line.strip().startswith(prefix)
    ]
    return {"server": preset["name"], "reachable": True, "run_ids": run_ids}


async def _tail_last_line(server: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Return the true last log line plus the most recent "X/Y registered"
    count anywhere in the log. The true last line isn't always a poll line —
    a pm2 launch dumps pip-install/process-table noise that can push the last
    poll line back hundreds of lines — so this greps the whole file for the
    count rather than trusting a fixed-size tail."""
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        return None
    remote_log = _remote_run_path(run_id, "log")
    cmd = [
        "ssh", *SSH_OPTS, f"{SSH_USER}@{preset['ip']}",
        f"tail -n 1 {remote_log} 2>/dev/null; echo '---REGCOUNT---'; "
        f"grep -oE '[0-9]+/[0-9]+ registered' {remote_log} 2>/dev/null | tail -n 1",
    ]
    try:
        rc, out, err = await _run_subprocess(cmd, timeout=10)
    except asyncio.TimeoutError:
        return None
    if rc != 0:
        return None
    out = _ANSI_RE.sub("", out)
    last_part, _, reg_part = out.partition("---REGCOUNT---")
    last_line = last_part.strip() or None
    if last_line is None:
        return None
    match = _REGISTERED_RE.search(reg_part)
    registered_count = int(match.group(1)) if match else None
    return {"last_line": last_line, "registered_count": registered_count}


async def get_active_runs() -> Dict[str, Any]:
    """Scan every server preset for actively-running RegBot processes."""
    checks = await asyncio.gather(*[_check_server_active(p) for p in SERVER_PRESETS])

    audit_by_run = {row.get("run_id"): row for row in list_recent_runs(limit=500)}

    pending: List[Dict[str, Any]] = []
    for check in checks:
        if not check["reachable"] or not check["run_ids"]:
            continue
        for run_id in check["run_ids"]:
            meta = audit_by_run.get(run_id, {})
            pending.append({
                "server": check["server"],
                "run_id": run_id,
                "started_at": _run_id_started_at(run_id),
                "wallet": meta.get("wallet"),
                "dispatch_agent": meta.get("dispatch_agent"),
                "reglimit": meta.get("reglimit"),
                "max_count": meta.get("max_count"),
            })

    tail_info = await asyncio.gather(
        *[_tail_last_line(r["server"], r["run_id"]) for r in pending]
    )
    for row, info in zip(pending, tail_info):
        row["last_line"] = info["last_line"] if info else None
        row["registered_count"] = info["registered_count"] if info else None

    unreachable = [c["server"] for c in checks if not c["reachable"]]
    return {"active": pending, "unreachable_servers": unreachable}
