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
import secrets
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
# Multiplexed, like the params push (app.py's MINER_PARAMS_RSYNC_SSH_OPTS) — and deliberately
# on the SAME ControlPath, so both share one master per box and the params keepalive worker
# keeps them warm for free. Every RegBot probe used to pay a full TCP/KEX/auth handshake:
# `get_active_runs` fans out one ssh per server preset, so on the overseas boxes (125-160ms
# RTT) that was ~10 round-trips × 18 servers on every poll. ControlMaster=auto still falls
# back to a fresh connection if no master is up, so a cold start behaves exactly as before.
SSH_OPTS = [
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "GSSAPIAuthentication=no",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/.mp-cm-%C",
    "-o", "ControlPersist=600",
]

# Server presets: dashboard "Server" selection -> (ip, wallet). Keep in sync
# with SERVER_NICKNAMES in app.py / miners.html.
# port_prefix is the axon port band the dashboard pre-fills for this server
# (creg79.py --port-prefix; X is a wildcard digit). It's only a default — the
# Add Miner / Multi-Server Register forms let you override it per run, and
# clearing the field falls back to run79.sh's own default.
SERVER_PRESETS: List[Dict[str, str]] = [
    {
        "name": "Server_1",
        "ip": "65.109.34.53",
        "wallet": "n001",
        "port_prefix": "808X,809X"
    },
    {
        "name": "Server3",
        "ip": "65.109.87.169",
        "wallet": "bittensor005",
        "port_prefix": "809X"
    },
    {
        "name": "Server4",
        "ip": "178.16.143.193",
        "wallet": "bittensor006",
        "port_prefix": "809X"
    },
    {
        "name": "Server5",
        "ip": "144.91.111.126",
        "wallet": "bittensor007",
        "port_prefix": "809X"
    },
    {
        "name": "Server6",
        "ip": "65.109.65.81",
        "wallet": "wolf003",
        "port_prefix": "809X"
    },
    {
        "name": "Server7",
        "ip": "84.32.34.89",
        "wallet": "wolf002",
        "port_prefix": "809X"
    },
    {
        "name": "Server_2",
        "ip": "65.109.69.29",
        "wallet": "n002",
        "port_prefix": "808X,809X"
    },
]
_PRESETS_BY_NAME = {p["name"]: p for p in SERVER_PRESETS}

# Fallback dispatch-agent list if the reference params file is unavailable.
_DEFAULT_AGENT_OPTIONS = ["WarriorAgent", "RandomMakerAgentGamma", "FreeAllAgent"]

REGLIMIT_MIN, REGLIMIT_MAX = 0.0001, 5.0
MAX_COUNT_MIN, MAX_COUNT_MAX = 1, 10

# Axon port band pattern for creg79.py --port-prefix: digits with X as a
# wildcard digit (79XX = 7900-7999), comma-separated to overflow into later
# bands ("809X,808X"). This value is interpolated into a shell script that
# runs as root on the miner box, so the character class is deliberately
# restrictive — digits, X and commas only, nothing that could break out of
# the assignment. creg79.py itself validates the semantics (band ordering,
# width) and errors clearly if the pattern is nonsense.
PORT_PREFIX_RE = re.compile(r"^[0-9X]{1,4}(,[0-9X]{1,4})*$")

RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

# Preset names are embedded verbatim in run_id (f"{server}_{stamp}_{suffix}"),
# which then becomes the remote log/pid/script filenames. A name that doesn't
# match RUN_ID_RE therefore can't launch at all — and it fails late, at launch
# time, one queue item at a time, so a batch happily accepts the server and
# then burns every one of its slots on "Invalid run_id". A trailing space in
# "Server3 " did exactly that. Checked here so a bad preset is a startup
# crash instead of a silent per-run failure.
_BAD_PRESET_NAMES = [p["name"] for p in SERVER_PRESETS if not RUN_ID_RE.match(p["name"])]
if _BAD_PRESET_NAMES:
    raise RuntimeError(
        f"SERVER_PRESETS names unusable in a run_id: {_BAD_PRESET_NAMES!r} — "
        f"allowed characters are {RUN_ID_RE.pattern} (check for stray whitespace)"
    )


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


# ---- chain queries run OUT OF PROCESS -------------------------------------------------
# substrate's metadata decode is pure Python and holds the GIL for seconds at a time, so a
# chain query made inside the API process — even in a thread — starves the asyncio loop, and
# every unrelated request queues behind it. Measured on this box: an agent Confirm, which
# touches no chain at all, waited 13.7s on the 300s balance sweep. chain_cli.py does the same
# work in a child process with its own GIL, and answers every address over ONE connection
# instead of opening one per address.
CHAIN_CLI = os.path.join(_HERE, "chain_cli.py")


async def run_chain_cli(args: List[str], timeout: float) -> Dict[str, Any]:
    """Run a chain query and return its payload. Never raises — a failure comes back as
    {"ok": False, "error": ...} so callers have one shape to handle.

    Prefers chain_daemon.py, which answers over a Unix socket from a connection it
    already holds open. The child-process path below pays ~1.8s of fixed setup
    (`import bittensor` 0.77s, then connect + runtime-metadata decode ~1.04s) before
    it can run a query that itself takes 0.02-0.4s. Measured through the daemon:
    reg-cost 2.2s -> 0.001s (served from its block-paced poller, no chain call at
    all), balances for 12 coldkeys 3.10s -> 0.31s.

    The daemon is strictly an optimisation. chain_client.call() returns None when it
    cannot be reached — a missing socket costs one failed connect() — and we drop
    straight through to the subprocess, which behaves exactly as it always did. A
    daemon that *answers* with ok=False is a real chain error and is returned as-is,
    so a genuine failure is not quietly retried down a second path.
    """
    import sys

    if args:
        from chain_client import call as chain_daemon_call

        payload = await chain_daemon_call(args[0], list(args[1:]), timeout=timeout)
        if payload is not None:
            return payload

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable or "python3",
            CHAIN_CLI,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_HERE,
        )
    except Exception as exc:
        return {"ok": False, "error": f"could not start chain_cli: {exc}"}
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"ok": False, "error": f"chain_cli timed out after {timeout}s"}
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": err[-300:]}
    # Some bittensor versions print banners to stdout, so the payload is the last JSON line.
    for line in reversed(stdout.decode("utf-8", "replace").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": "chain_cli produced no JSON"}


def _server_coldkey(server: str) -> tuple[str, str]:
    """(wallet, coldkey ss58) for a server preset. Raises if either is unresolvable."""
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
    return wallet, address


async def get_server_balance(server: str) -> Dict[str, Any]:
    """Free TAO balance + total staked (SN79 alpha) of the coldkey behind
    this server's preset wallet."""
    wallet, address = _server_coldkey(server)
    payload = await run_chain_cli(["balances", "79", address], timeout=120.0)
    result = payload.get("balances", {}).get(address) if payload.get("ok") else None
    if result is None:
        why = payload.get("error") or payload.get("errors", {}).get(address) or "no result"
        raise RuntimeError(f"Failed to fetch balance for wallet '{wallet}': {why}")
    return {"server": server, "wallet": wallet, **result}


BALANCE_CACHE_PATH = os.path.join(DASHBOARD_ROOT, "79Miners", "RegBot", "balance_cache.json")


async def refresh_balance_cache() -> Dict[str, Any]:
    """Fetch every server's coldkey balance and persist to BALANCE_CACHE_PATH, so reads are
    served from disk instead of a live chain call each time. One wallet failing (e.g. a
    missing coldkeypub.txt) doesn't block the others — it's dropped with a logged warning.

    ONE child, ONE connection, ALL addresses. This used to fan out one `to_thread` per preset:
    18 threads each opening their own substrate connection, every 300s. The connect is where
    the metadata decode happens, so that was 18 GIL-hogging decodes at once inside the API
    process — the single worst source of request latency on this service (13.7s stalls, ending
    exactly when this cache file was written). The presets share only 12 distinct coldkeys, so
    de-duplicating first also cuts the chain work by a third.
    """
    wanted: Dict[str, str] = {}  # server name -> address
    for preset in SERVER_PRESETS:
        try:
            _, address = _server_coldkey(preset["name"])
        except RegBotError as exc:
            logger.warning("Balance refresh skipped for %s: %s", preset["name"], exc)
            continue
        wanted[preset["name"]] = address

    balances: Dict[str, Any] = {}
    if wanted:
        addresses = list(dict.fromkeys(wanted.values()))
        payload = await run_chain_cli(["balances", "79", *addresses], timeout=300.0)
        if not payload.get("ok"):
            logger.warning("Balance refresh failed: %s", payload.get("error"))
        by_address = payload.get("balances", {}) or {}
        errors = payload.get("errors", {}) or {}
        for server, address in wanted.items():
            result = by_address.get(address)
            if result is None:
                logger.warning(
                    "Balance refresh failed for %s: %s",
                    server,
                    errors.get(address) or payload.get("error") or "no result",
                )
                continue
            preset = _PRESETS_BY_NAME.get(server, {})
            balances[server] = {"server": server, "wallet": preset.get("wallet"), **result}
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
    port_prefix: Optional[str] = None,
) -> Dict[str, str]:
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    if port_prefix is not None and not PORT_PREFIX_RE.match(port_prefix):
        raise RegBotError(
            f"port_prefix '{port_prefix}' is invalid — use digits with X as a "
            "wildcard digit (e.g. 79XX, 809X), comma-separated for multiple bands"
        )
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
    port_prefix: Optional[str] = None,
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
    # Only rewritten when a band was actually requested — left alone, the
    # template's own PORT_PREFIX default applies, so existing behaviour is
    # unchanged for callers that don't care. Note the template uses `-` not
    # `:-` (an explicitly empty value means "no band"), so the pattern here
    # has to match that single dash.
    if port_prefix is not None:
        content = sub(
            r'^PORT_PREFIX="\$\{PORT_PREFIX-[^}]*\}"',
            f'PORT_PREFIX="${{PORT_PREFIX-{port_prefix}}}"',
            content,
        )
    return content


def _make_run_id() -> str:
    # Timestamp for readability + a random suffix for uniqueness. The suffix is
    # not decorative: run_id determines the remote log/pid/script paths, and the
    # timestamp only has second resolution, so two runs launched on the same
    # server within one second would otherwise share all three files — the
    # second run's pidfile would clobber the first's, making kill_run target
    # the wrong process and tail_log show the wrong log. Concurrent batches on
    # one server make that collision likely rather than theoretical.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{secrets.token_hex(3)}"


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
    port_prefix: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a run79.sh for this server+params, scp it over, launch it detached.

    run_id may be pre-assigned by the caller so the run's identity (and hence
    its remote log/pid paths) is known and persistable *before* the launch is
    attempted — that's what lets a batch tell, after a crash mid-launch,
    exactly which run to look for instead of guessing."""
    preset = _validate_inputs(
        server, dispatch_agent, reglimit, max_count, dispatch_agent_id, port_prefix
    )
    ip, wallet = preset["ip"], preset["wallet"]

    script_content = _build_script(
        dispatch_agent, reglimit, max_count, wallet, dispatch_agent_id, port_prefix
    )
    if run_id is not None and not RUN_ID_RE.match(run_id):
        raise RegBotError("Invalid run_id")
    run_id = run_id or f"{server}_{_make_run_id()}"
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
        "port_prefix": port_prefix,
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

# The orchestrator that walks a batch's queue is just an asyncio task in the
# dashboard process, so anything that restarts the dashboard (a deploy, a pm2
# restart, a crash) used to kill it mid-queue and silently drop every PENDING
# item — the already-launched remote runs kept going and registered fine, so
# from the outside it looked like "the batch just stopped registering". That
# happened for real on 2026-08-03 (batch_20260803T013020Z: 4 items registered,
# restart at 03:13:24Z, remaining items never launched). State is mirrored to
# this file on every transition so a restart can pick the queue back up.
BATCH_STATE_PATH = os.path.join(DASHBOARD_ROOT, "79Miners", "RegBot", "active_batch.json")


def _save_batch_state() -> None:
    """Mirror in-memory batches to disk. Never raises — losing persistence is
    bad, but not a reason to take down a batch that is otherwise running."""
    try:
        os.makedirs(os.path.dirname(BATCH_STATE_PATH), exist_ok=True)
        tmp_path = f"{BATCH_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"batches": list(_active_batches.values())}, f, indent=2)
        os.replace(tmp_path, BATCH_STATE_PATH)  # atomic: never a half-written file
    except OSError as exc:
        logger.warning("Could not persist batch state: %s", exc)


def _build_batch_queue(server_configs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Round-robin one attempt per server per round (in the given server
    order), dropping a server once its count is exhausted, until every
    server's count is met. Each queue item is one single-hotkey attempt,
    carrying that server's own reglimit."""
    remaining = [
        {
            "server": sc["server"],
            "reglimit": sc["reglimit"],
            "port_prefix": sc.get("port_prefix"),
            "left": int(sc["count"]),
        }
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
                    "port_prefix": r["port_prefix"],
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


def get_active_batches() -> List[Dict[str, Any]]:
    """Every batch still working, newest first. Several can run at once —
    including on the same server (an explicit choice; see start_batch)."""
    running = [b for b in _active_batches.values() if b["status"] in ("RUNNING", "CANCELLING")]
    return sorted(running, key=lambda b: b.get("created_at") or "", reverse=True)


def get_active_batch() -> Optional[Dict[str, Any]]:
    """Most recently started active batch, or None. Kept for callers that only
    ever deal with one at a time."""
    active = get_active_batches()
    return active[0] if active else None


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
    — each server can carry its own reglimit.

    Batches run concurrently, including several targeting the same server. That
    is deliberate, but it means the same coldkey can have two registrations in
    flight at once, so the wallet drains at twice the rate its per-batch reg
    limits would suggest — check the balance against the combined spend of
    everything running, not just this batch."""
    for sc in server_configs:
        if sc["server"] not in _PRESETS_BY_NAME:
            raise RegBotError(f"Unknown server '{sc['server']}'")
        if not isinstance(sc["count"], int) or sc["count"] < 0:
            raise RegBotError("count must be a non-negative integer")
        # Reuses the exact same per-server validation trigger_run() applies
        # on launch (agent name shape, reglimit range) so a bad batch is
        # rejected up front rather than partway through the queue.
        _validate_inputs(
            sc["server"], dispatch_agent, sc["reglimit"], 1,
            dispatch_agent_id, sc.get("port_prefix"),
        )

    queue = _build_batch_queue(server_configs)
    if not queue:
        raise RegBotError("Nothing to register — every count is zero")

    # Random suffix for the same reason run_id has one: batches are keyed by
    # this in _active_batches, and now that several can start concurrently two
    # created in the same second would otherwise collide — the second silently
    # replacing the first, orphaning its queue.
    batch_id = (
        f"batch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f"_{secrets.token_hex(2)}"
    )
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
    _save_batch_state()
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
        # Persisted immediately so a restart landing between this flag and the
        # loop noticing it doesn't come back up and resume a cancelled batch.
        _save_batch_state()
    return batch


_FINALISED_ITEM_STATUSES = ("DONE", "CANCELLED", "LAUNCH_FAILED")


async def _remote_run_exists(server: str, run_id: str) -> bool:
    """Did this exact run ever land on the box? Because run_id is assigned (and
    persisted) before the launch, a batch resuming after a crash can ask about
    its own specific run rather than inferring from whatever happens to be
    live — which is what makes concurrent runs on one server safe to resume.
    Raises when the server can't be reached: 'no answer' must not be read as
    'never launched', since relaunching a run that did start double-registers
    and double-spends."""
    preset = _PRESETS_BY_NAME.get(server)
    if not preset:
        raise RegBotError(f"Unknown server '{server}'")
    log_path = _remote_run_path(run_id, "log")
    pid_path = _remote_run_path(run_id, "pid")
    cmd = [
        "ssh", *SSH_OPTS, f"{SSH_USER}@{preset['ip']}",
        f'if [ -f "{log_path}" ] || [ -f "{pid_path}" ]; then echo YES; else echo NO; fi',
    ]
    try:
        rc, out, err = await _run_subprocess(cmd, timeout=15)
    except asyncio.TimeoutError:
        raise RegBotError(f"{server} timed out — cannot confirm whether run {run_id} launched")
    if rc != 0:
        raise RegBotError(
            f"{server} unreachable ({err.strip() or out.strip()}) — "
            f"cannot confirm whether run {run_id} launched"
        )
    return "YES" in out


async def _run_batch(batch_id: str) -> None:
    batch = _active_batches[batch_id]
    queue = batch["queue"]
    interrupted = False
    try:
        for index, item in enumerate(queue):
            if batch["status"] == "CANCELLING":
                if item["status"] not in _FINALISED_ITEM_STATUSES:
                    item["status"] = "CANCELLED"
                    _save_batch_state()
                continue

            # Already settled on an earlier pass — only possible when resuming
            # a batch after a restart. Re-running it would double-register.
            if item["status"] in _FINALISED_ITEM_STATUSES:
                continue

            batch["current_index"] = index

            # Resuming an item that was already in flight: ask whether its own
            # run actually landed on the box. Only skip the launch when the
            # answer is a definite yes — anything else (never landed) falls
            # through and launches under the same pre-assigned run_id, and an
            # unreachable server raises rather than guessing.
            needs_launch = True
            if item["status"] == "RUNNING" and item.get("run_id"):
                try:
                    if await _remote_run_exists(item["server"], item["run_id"]):
                        logger.info(
                            "Batch %s: re-attaching to run %s on %s",
                            batch_id, item["run_id"], item["server"],
                        )
                        needs_launch = False
                    else:
                        logger.info(
                            "Batch %s: run %s never landed on %s, launching it",
                            batch_id, item["run_id"], item["server"],
                        )
                except Exception as exc:
                    logger.error("Batch %s: cannot resolve %s: %s", batch_id, item["server"], exc)
                    item["status"] = "LAUNCH_FAILED"
                    item["error"] = str(exc)
                    item["finished_at"] = datetime.now(timezone.utc).isoformat()
                    _save_batch_state()
                    continue

            if needs_launch:
                # run_id is assigned and persisted BEFORE the launch, so if the
                # dashboard dies mid-launch the resumed batch still knows which
                # run to look for (see _remote_run_exists).
                item["status"] = "RUNNING"
                item["run_id"] = item.get("run_id") or f"{item['server']}_{_make_run_id()}"
                item["started_at"] = datetime.now(timezone.utc).isoformat()
                _save_batch_state()
                try:
                    await trigger_run(
                        item["server"],
                        batch["dispatch_agent"],
                        item["reglimit"],
                        1,
                        triggered_by=f"batch:{batch_id}",
                        dispatch_agent_id=batch.get("dispatch_agent_id"),
                        port_prefix=item.get("port_prefix"),
                        run_id=item["run_id"],
                    )
                except Exception as exc:
                    logger.error(
                        "Batch %s: launch failed for %s: %s", batch_id, item["server"], exc
                    )
                    item["status"] = "LAUNCH_FAILED"
                    item["error"] = str(exc)
                    item["finished_at"] = datetime.now(timezone.utc).isoformat()
                    _save_batch_state()
                    continue

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
                    _save_batch_state()
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
                    _save_batch_state()
                    break
                await asyncio.sleep(BATCH_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        # The dashboard is shutting down mid-queue. Deliberately leave the
        # batch marked RUNNING on disk: writing a "DONE" here would record a
        # completion that never happened and stop the next startup from
        # resuming the items still waiting to launch.
        interrupted = True
        logger.info("Batch %s: interrupted by shutdown, state left for resume", batch_id)
        raise
    finally:
        if not interrupted:
            if batch["status"] == "CANCELLING":
                batch["status"] = "CANCELLED"
            else:
                # A batch where nothing ever launched is a failure, not a
                # completion. Reporting DONE there reads as "everything
                # registered" while in fact nothing did — which is exactly how
                # a broken run79.sh template looked on 2026-08-03: HTTP 200,
                # every item LAUNCH_FAILED, batch "DONE", zero registrations.
                succeeded = sum(1 for i in batch["queue"] if i["status"] == "DONE")
                batch["status"] = "DONE" if succeeded else "FAILED"
            batch["finished_at"] = datetime.now(timezone.utc).isoformat()
        _batch_tasks.pop(batch_id, None)
        _save_batch_state()


async def resume_interrupted_batches() -> Dict[str, Any]:
    """Called once on dashboard startup: reload persisted batches and restart
    the orchestrator for any that were still running when the process died.
    Items already DONE are left alone and in-flight ones are re-attached to,
    so resuming never re-registers anything (see _run_batch)."""
    if not os.path.isfile(BATCH_STATE_PATH):
        return {"resumed": []}
    try:
        with open(BATCH_STATE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read persisted batch state: %s", exc)
        return {"resumed": []}

    resumed: List[str] = []
    for batch in saved.get("batches", []):
        batch_id = batch.get("batch_id")
        if not batch_id:
            continue
        _active_batches[batch_id] = batch
        if batch.get("status") in ("RUNNING", "CANCELLING"):
            pending = sum(1 for i in batch["queue"] if i["status"] == "PENDING")
            in_flight = sum(1 for i in batch["queue"] if i["status"] == "RUNNING")
            logger.info(
                "Resuming batch %s after restart: %d pending, %d in flight",
                batch_id, pending, in_flight,
            )
            _batch_tasks[batch_id] = asyncio.create_task(_run_batch(batch_id))
            resumed.append(batch_id)
    return {"resumed": resumed}
