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
    {"name": "Server8", "ip": "88.216.195.45", "wallet": "wolf001"},
    {"name": "Server9", "ip": "65.109.104.253", "wallet": "wolf004"},
    {"name": "Server10", "ip": "65.108.0.181", "wallet": "wolf005"},
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
    """Free TAO balance + total staked (SN79 alpha) of the coldkey behind
    this server's preset wallet."""
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
    from get_reg_cost import get_balance_and_stake_for_address

    result = await asyncio.to_thread(get_balance_and_stake_for_address, address, 79)
    if result is None:
        raise RuntimeError(f"Failed to fetch balance for wallet '{wallet}'")
    return {"server": server, "wallet": wallet, **result}


BALANCE_CACHE_PATH = os.path.join(DASHBOARD_ROOT, "79Miners", "RegBot", "balance_cache.json")


async def refresh_balance_cache() -> Dict[str, Any]:
    """Fetch every server's coldkey balance concurrently and persist to
    BALANCE_CACHE_PATH, so reads can be served from disk instead of a live
    chain call each time. One wallet failing (e.g. missing coldkeypub.txt)
    doesn't block the others — it's just dropped with a logged warning."""
    results = await asyncio.gather(
        *[get_server_balance(p["name"]) for p in SERVER_PRESETS],
        return_exceptions=True,
    )
    balances: Dict[str, Any] = {}
    for preset, result in zip(SERVER_PRESETS, results):
        if isinstance(result, Exception):
            logger.warning("Balance refresh failed for %s: %s", preset["name"], result)
            continue
        balances[preset["name"]] = result
    payload = {"balances": balances, "updated_at": datetime.now(timezone.utc).isoformat()}

    os.makedirs(os.path.dirname(BALANCE_CACHE_PATH), exist_ok=True)
    tmp_path = f"{BALANCE_CACHE_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, BALANCE_CACHE_PATH)
    return payload


def load_balance_cache() -> Optional[Dict[str, Any]]:
    if not os.path.isfile(BALANCE_CACHE_PATH):
        return None
    try:
        with open(BALANCE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


DISPATCH_EXAMPLE_PATH = os.path.join(DASHBOARD_ROOT, "79Miners", "params", "dispatch_example.json")


def get_dispatch_agent_options() -> List[Dict[str, Any]]:
    """Every DispatcherAgent sub-agent entry from the reference roster
    (dispatch_example.json), sourced from that file rather than an arbitrary
    miner's params — it's the canonical, complete list of every agent id.

    Each entry carries its `agent_id` (the value creg79.py --dispatch-index
    selects by — see trigger_run) alongside a `name`. Some agent_ids share the
    same underlying agent name but carry different tuned params (e.g. two
    GammaPrimeAgent variants at agent_id 42 and 43) — those collide if
    resolved by name alone (creg79.py's --dispatch-agent matches the FIRST
    same-named entry, silently making later ones unreachable), so entries are
    kept one-per-agent_id here and given a disambiguated `label` ("GammaPrimeAgent 1",
    "GammaPrimeAgent 2", ...) instead of being deduped by name."""
    try:
        with open(DISPATCH_EXAMPLE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        agents = data.get("agents")
    except (OSError, json.JSONDecodeError, TypeError):
        agents = None

    if not isinstance(agents, list) or not agents:
        return [{"name": n, "agent_id": None, "label": n} for n in _DEFAULT_AGENT_OPTIONS]

    raw = [
        {"name": a.get("name"), "agent_id": a.get("agent_id")}
        for a in agents
        if isinstance(a, dict) and isinstance(a.get("name"), str)
    ]
    name_counts: Dict[str, int] = {}
    for e in raw:
        name_counts[e["name"]] = name_counts.get(e["name"], 0) + 1

    seen_counters: Dict[str, int] = {}
    result: List[Dict[str, Any]] = []
    for e in raw:
        name = e["name"]
        if name_counts[name] > 1:
            seen_counters[name] = seen_counters.get(name, 0) + 1
            label = f"{name} {seen_counters[name]}"
        else:
            label = name
        result.append({"name": name, "agent_id": e["agent_id"], "label": label})
    return result


DISPATCH_AGENT_ID_MIN, DISPATCH_AGENT_ID_MAX = 0, 999


def _validate_inputs(
    server: str,
    dispatch_agent: str,
    reglimit: float,
    max_count: int,
    dispatch_agent_id: Optional[int] = None,
) -> Dict[str, str]:
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    if not dispatch_agent or not re.match(r"^[A-Za-z0-9_]+$", dispatch_agent):
        raise RegBotError("dispatch_agent must be a plain agent name (letters/digits/underscore)")
    if dispatch_agent_id is not None and not (
        DISPATCH_AGENT_ID_MIN <= dispatch_agent_id <= DISPATCH_AGENT_ID_MAX
    ):
        raise RegBotError(
            f"dispatch_agent_id must be between {DISPATCH_AGENT_ID_MIN} and {DISPATCH_AGENT_ID_MAX}"
        )
    if not (REGLIMIT_MIN <= reglimit <= REGLIMIT_MAX):
        raise RegBotError(f"reglimit must be between {REGLIMIT_MIN} and {REGLIMIT_MAX}")
    if not (MAX_COUNT_MIN <= max_count <= MAX_COUNT_MAX):
        raise RegBotError(f"max_count must be between {MAX_COUNT_MIN} and {MAX_COUNT_MAX}")
    return preset


def _build_script(
    dispatch_agent: str,
    reglimit: float,
    max_count: int,
    wallet: str,
    dispatch_agent_id: Optional[int] = None,
) -> str:
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
    # DISPATCH_INDEX (agent_id), not name, is what actually disambiguates
    # entries that share the same agent name but different tuned params
    # (e.g. two GammaPrimeAgent variants) — see get_dispatch_agent_options.
    # --dispatch-agent (name) wins over --dispatch-index if both are passed
    # to creg79.py, so the template only uses DISPATCH_AGENT when this is
    # empty (see run79.sh's DISPATCH_ARG selection).
    content = sub(
        r'^DISPATCH_INDEX="\$\{DISPATCH_INDEX:-[^}]*\}"',
        f'DISPATCH_INDEX="${{DISPATCH_INDEX:-'
        f'{"" if dispatch_agent_id is None else int(dispatch_agent_id)}}}"',
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
    server: str,
    dispatch_agent: str,
    reglimit: float,
    max_count: int,
    triggered_by: str = "dashboard",
    dispatch_agent_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate a run79.sh for this server+params, scp it over, launch it detached."""
    preset = _validate_inputs(server, dispatch_agent, reglimit, max_count, dispatch_agent_id)
    ip, wallet = preset["ip"], preset["wallet"]

    script_content = _build_script(dispatch_agent, reglimit, max_count, wallet, dispatch_agent_id)
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
        "dispatch_agent_id": dispatch_agent_id,
        "reglimit": reglimit,
        "max_count": max_count,
        "remote_script": remote_script,
        "remote_log": remote_log,
        "remote_pid": remote_pid,
    }
    _append_audit_log(entry)
    logger.info(
        "RegBot run triggered: server=%s wallet=%s agent=%s(id=%s) reglimit=%s max_count=%s run_id=%s",
        server, wallet, dispatch_agent, dispatch_agent_id, reglimit, max_count, run_id,
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

    # Trust the log over the pid: creg79.py can finish every requested
    # registration and still not exit cleanly afterward (see
    # _tail_last_line's docstring), so a live pid doesn't necessarily mean
    # there's still real work happening.
    #
    # Strictly < 0, not <= 0: creg79.py's loop only truly exits once
    # count > max_count, at which point Remaining = max_count - count is
    # always exactly -1. Remaining == 0 happens one registration EARLIER
    # (count == max_count) while the loop's own condition (count <=
    # max_count) is still true — i.e. at least one more hotkey attempt is
    # still pending for any max_count >= 2. Treating 0 as "finished" was a
    # real off-by-one bug: it prematurely marked multi-hotkey runs done
    # after their second-to-last registration.
    remaining_matches = _REMAINING_COUNT_RE.findall(body)
    if remaining_matches and int(remaining_matches[-1]) < 0:
        status = "FINISHED"

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
_REMAINING_COUNT_RE = re.compile(r"Remaining count:\s*(-?\d+)")


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
    """Return the true last log line, how many hotkeys this run has actually
    registered, and whether the registration loop has genuinely finished.

    Both come from creg79.py's own "Remaining count: N" line, which it
    prints exactly once per successful registration — right after that
    hotkey's miner launch + telegram notification — regardless of chain
    price movement. This is more reliable than the "X/Y registered" price-
    poll line (the previous source for this), which only reprints when the
    price CHANGES, so it can go stale and under-report the true count —
    observed in practice still showing N-1 after the Nth (final)
    registration had already landed.

    "finished" is True once the last such N is STRICTLY NEGATIVE (creg79.py
    computes it as max_count - count post-increment; the loop only truly
    exits once count > max_count, at which point N is always exactly -1).
    N == 0 is NOT finished — it happens one registration earlier, while the
    loop's own condition still holds, i.e. at least one more attempt is
    pending for any max_count >= 2. This can be true even while the OS
    process is still alive: creg79.py doesn't always exit cleanly right
    after finishing its work (observed: a run sits alive for hours
    post-completion with no further log output, likely a lingering
    connection thread), so a raw `kill -0` on the pid can keep reporting
    RUNNING long after there's no more useful work happening. Callers should
    treat `finished` as the authoritative "is this run actually done"
    signal, not the raw pid check.
    """
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        return None
    remote_log = _remote_run_path(run_id, "log")
    cmd = [
        "ssh", *SSH_OPTS, f"{SSH_USER}@{preset['ip']}",
        f"tail -n 1 {remote_log} 2>/dev/null; echo '---REGCOUNT---'; "
        f"grep -c 'Remaining count:' {remote_log} 2>/dev/null; echo '---LASTREMAIN---'; "
        f"grep -oE 'Remaining count: -?[0-9]+' {remote_log} 2>/dev/null | tail -n 1",
    ]
    try:
        rc, out, err = await _run_subprocess(cmd, timeout=10)
    except asyncio.TimeoutError:
        return None
    if rc != 0:
        return None
    out = _ANSI_RE.sub("", out)
    last_part, _, rest = out.partition("---REGCOUNT---")
    reg_count_part, _, last_remain_part = rest.partition("---LASTREMAIN---")
    last_line = last_part.strip() or None
    if last_line is None:
        return None
    try:
        registered_count = int(reg_count_part.strip())
    except ValueError:
        registered_count = None
    match = _REMAINING_COUNT_RE.search(last_remain_part)
    finished = bool(match) and int(match.group(1)) < 0
    return {"last_line": last_line, "registered_count": registered_count, "finished": finished}


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
        row["_finished"] = bool(info and info.get("finished"))

    # A run whose log says every requested hotkey already registered is done
    # even if its process is still alive (see _tail_last_line) — drop it from
    # the active list so the bar doesn't keep showing a pill for it.
    active = [row for row in pending if not row.pop("_finished")]

    unreachable = [c["server"] for c in checks if not c["reachable"]]
    return {"active": active, "unreachable_servers": unreachable}


# ---- Multi-server batch registration ----
#
# Registers a set of hotkeys spread across several servers, one hotkey at a
# time, in strict round-robin order across servers (dropping a server once
# it hits its target count) — e.g. {Server1: 3, Server2: 2, Server3: 4}
# becomes the launch order Server1, Server2, Server3, Server1, Server2,
# Server3, Server1, Server3, Server3.
#
# This is a DELIBERATE choice over just launching each server's full target
# count concurrently (which is what happens today if you trigger_run() each
# server separately): only one server is ever actively attempting at a time,
# so a step can legitimately sit for a long time (minutes to hours) waiting
# for that one server's reglimit to clear — the other servers in the batch
# are idle meanwhile. That trade-off (strict, controlled turn-taking over
# maximum throughput) was an explicit user choice, not an oversight.
#
# Only one batch can be active at a time — the round-robin premise doesn't
# mean anything if two independent batches were racing each other.

_active_batches: Dict[str, Dict[str, Any]] = {}
_batch_tasks: Dict[str, "asyncio.Task[None]"] = {}
BATCH_POLL_INTERVAL_SECONDS = 10.0


def _build_batch_queue(server_configs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Round-robin one attempt per server per round (in the given server
    order), dropping a server once its count is exhausted, until every
    server's count is met. Each queue item is one single-hotkey attempt,
    carrying that server's own reglimit."""
    remaining = [
        {"server": sc["server"], "reglimit": sc["reglimit"], "left": int(sc["count"])}
        for sc in server_configs
        if int(sc["count"]) > 0
    ]
    queue: List[Dict[str, Any]] = []
    seq_counters: Dict[str, int] = {}
    while any(r["left"] > 0 for r in remaining):
        for r in remaining:
            if r["left"] > 0:
                seq_counters[r["server"]] = seq_counters.get(r["server"], 0) + 1
                queue.append({
                    "server": r["server"],
                    "seq": seq_counters[r["server"]],
                    "reglimit": r["reglimit"],
                    "status": "PENDING",
                    "run_id": None,
                    "last_line": None,
                    "synced_uids": [],
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                })
                r["left"] -= 1
    return queue


def get_active_batch() -> Optional[Dict[str, Any]]:
    for batch in _active_batches.values():
        if batch["status"] == "RUNNING":
            return batch
    return None


def get_batch_status(batch_id: str) -> Dict[str, Any]:
    batch = _active_batches.get(batch_id)
    if not batch:
        raise RegBotError(f"Unknown batch '{batch_id}'")
    return batch


async def start_batch(
    server_configs: List[Dict[str, Any]],
    dispatch_agent: str,
    dispatch_agent_id: Optional[int] = None,
    triggered_by: str = "dashboard",
) -> Dict[str, Any]:
    """server_configs: [{"server": "Server1", "count": 3, "reglimit": 0.1051}, ...]
    — each server can carry its own reglimit."""
    if get_active_batch() is not None:
        raise RegBotError(
            "A registration batch is already running — cancel it before starting another."
        )
    for sc in server_configs:
        if sc["server"] not in _PRESETS_BY_NAME:
            raise RegBotError(f"Unknown server '{sc['server']}'")
        if not isinstance(sc["count"], int) or sc["count"] < 0:
            raise RegBotError("count must be a non-negative integer")
        # Reuses the exact same per-server validation trigger_run() applies
        # on launch (agent name shape, reglimit range) so a bad batch is
        # rejected up front rather than partway through the queue.
        _validate_inputs(sc["server"], dispatch_agent, sc["reglimit"], 1, dispatch_agent_id)

    queue = _build_batch_queue(server_configs)
    if not queue:
        raise RegBotError("Nothing to register — every count is zero")

    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    batch: Dict[str, Any] = {
        "batch_id": batch_id,
        "server_configs": server_configs,
        "dispatch_agent": dispatch_agent,
        "dispatch_agent_id": dispatch_agent_id,
        "queue": queue,
        "current_index": 0,
        "status": "RUNNING",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "triggered_by": triggered_by,
    }
    _active_batches[batch_id] = batch
    _batch_tasks[batch_id] = asyncio.create_task(_run_batch(batch_id))
    return batch


async def cancel_batch(batch_id: str) -> Dict[str, Any]:
    batch = _active_batches.get(batch_id)
    if not batch:
        raise RegBotError(f"Unknown batch '{batch_id}'")
    if batch["status"] == "RUNNING":
        # _run_batch's loop checks this flag between poll ticks (at most
        # BATCH_POLL_INTERVAL_SECONDS later) and kills the in-flight attempt
        # before stopping — this just requests that, doesn't do it inline.
        batch["status"] = "CANCELLING"
    return batch


async def _run_batch(batch_id: str) -> None:
    batch = _active_batches[batch_id]
    queue = batch["queue"]
    try:
        for index, item in enumerate(queue):
            if batch["status"] == "CANCELLING":
                item["status"] = "CANCELLED"
                continue
            batch["current_index"] = index
            item["status"] = "RUNNING"
            item["started_at"] = datetime.now(timezone.utc).isoformat()

            try:
                run_entry = await trigger_run(
                    item["server"],
                    batch["dispatch_agent"],
                    item["reglimit"],
                    1,
                    triggered_by=f"batch:{batch_id}",
                    dispatch_agent_id=batch.get("dispatch_agent_id"),
                )
            except Exception as exc:
                logger.error("Batch %s: launch failed for %s: %s", batch_id, item["server"], exc)
                item["status"] = "LAUNCH_FAILED"
                item["error"] = str(exc)
                item["finished_at"] = datetime.now(timezone.utc).isoformat()
                continue

            item["run_id"] = run_entry["run_id"]

            while True:
                if batch["status"] == "CANCELLING":
                    try:
                        await kill_run(item["server"], item["run_id"])
                    except Exception as exc:
                        logger.warning(
                            "Batch %s: failed to kill in-flight run %s: %s",
                            batch_id, item["run_id"], exc,
                        )
                    item["status"] = "CANCELLED"
                    item["finished_at"] = datetime.now(timezone.utc).isoformat()
                    break
                try:
                    log_data = await tail_log(item["server"], item["run_id"], lines=50)
                except Exception as exc:
                    logger.warning(
                        "Batch %s: status poll failed for %s: %s", batch_id, item["run_id"], exc
                    )
                    await asyncio.sleep(BATCH_POLL_INTERVAL_SECONDS)
                    continue

                log_lines = (log_data.get("log") or "").splitlines()
                item["last_line"] = log_lines[-1] if log_lines else None

                if log_data["status"] == "FINISHED":
                    item["status"] = "DONE"
                    item["synced_uids"] = log_data.get("synced_uids", [])
                    item["finished_at"] = datetime.now(timezone.utc).isoformat()
                    break
                await asyncio.sleep(BATCH_POLL_INTERVAL_SECONDS)
    finally:
        batch["status"] = "CANCELLED" if batch["status"] == "CANCELLING" else "DONE"
        batch["finished_at"] = datetime.now(timezone.utc).isoformat()
        _batch_tasks.pop(batch_id, None)
