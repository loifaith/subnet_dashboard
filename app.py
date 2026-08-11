from fastapi import FastAPI, Request, HTTPException, Body, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from contextlib import asynccontextmanager, suppress
from typing import Optional, Any
from urllib.parse import quote
import uvicorn
import logging
import os
import sys
import json
import asyncio
import shlex
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import auth

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONITOR_DIR = os.path.join(os.path.dirname(__file__), "79Miners", "monitor")
SN88_MONITOR_DIR = os.path.join(os.path.dirname(__file__), "88Miners", "monitor")
if MONITOR_DIR not in sys.path:
    sys.path.insert(0, MONITOR_DIR)
if SN88_MONITOR_DIR not in sys.path:
    sys.path.insert(0, SN88_MONITOR_DIR)

MINER_STATUS_REFRESH_SECONDS = int(os.getenv("MINER_STATUS_REFRESH_SECONDS", "900"))
FEE_REGIME_CACHE_SECONDS = int(os.getenv("FEE_REGIME_CACHE_SECONDS", "300"))
REG_COST_CACHE_SECONDS = int(os.getenv("REG_COST_CACHE_SECONDS", "300"))
REGBOT_BALANCE_CACHE_SECONDS = int(os.getenv("REGBOT_BALANCE_CACHE_SECONDS", "300"))
COLDKEYS_WATCH_INTERVAL_SECONDS = int(os.getenv("COLDKEYS_WATCH_INTERVAL_SECONDS", "3600"))
INVESTING88_CACHE_SECONDS = int(os.getenv("INVESTING88_CACHE_SECONDS", "300"))
INVESTING88_SYNC_HOUR = int(os.getenv("INVESTING88_SYNC_HOUR", "19"))
INVESTING88_SYNC_INTERVAL_SECONDS = int(os.getenv("INVESTING88_SYNC_INTERVAL_SECONDS", "3600"))
INVESTING88_SYNC_TZ = os.getenv("INVESTING88_SYNC_TZ", "Asia/Tokyo")
UID_STATUS_CACHE_MAX = int(os.getenv("UID_STATUS_CACHE_MAX", "256"))
# Hourly alpha OHLCV per subnet, written by 88dashboard/collect_prices.py as
# <netuid>.json = {netuid, name, series: [[date, o, h, l, c, v], ...]}.
ALPHA_PRICE_DIR = os.getenv(
    "ALPHA_PRICE_DIR", "/root/work/bittensor/88/88dashboard/data/alpha"
)
# Window lengths in hours; None = the whole series.
ALPHA_PRICE_RANGES: dict[str, Optional[int]] = {
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
    "90d": 24 * 90,
    "1y": 24 * 365,
    "all": None,
}
ALPHA_PRICE_DEFAULT_RANGE = "7d"
# The header sparkline is ~130 CSS px wide, so anything past a few hundred points
# is sub-pixel detail; "all" would otherwise ship 9k points on every poll.
ALPHA_PRICE_MAX_POINTS = int(os.getenv("ALPHA_PRICE_MAX_POINTS", "240"))
# How often to refresh the warm SSH master connections to the miner boxes. Must be shorter
# than the SSH ControlPersist window (600s) so masters never expire between refreshes.
MINER_PARAMS_KEEPALIVE_SECONDS = int(os.getenv("MINER_PARAMS_KEEPALIVE_SECONDS", "300"))
SIM_ID_POLL_SECONDS = int(os.getenv("SIM_ID_POLL_SECONDS", "60"))
# @sn79_sim_id_bot, posting to the "SN79 Sim ID Alert" channel it's admin in.
SIM_ID_TELEGRAM_BOT_TOKEN = os.getenv(
    "SIM_ID_TELEGRAM_BOT_TOKEN", "8453130208:AAF4awFThe9zakRRlIH5-Ov2fki-55BNJBk"
)
SIM_ID_TELEGRAM_CHAT_ID = os.getenv("SIM_ID_TELEGRAM_CHAT_ID", "-1004372773110")
REFRESH_STATUS_SCRIPT = os.path.join(MONITOR_DIR, "miner_status_cache.py")
INVESTING88_SYNC_SCRIPT = os.path.join(SN88_MONITOR_DIR, "investing88_cache.py")
DASHBOARD_ROOT = os.path.dirname(__file__)
_fee_regime_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}
_reg_cost_cache: dict[int, dict[str, Any]] = {}
_investing88_cache: dict[int, dict[str, Any]] = {}
_sim_id_state: dict[str, Any] = {"last": None}
# netuid -> {"mtime": float, "rows": [(timestamp, high, low, close), ...]}. Keyed on
# mtime because collect_prices.py rewrites the file hourly; re-parsing 9k rows of
# JSON on every poll of an always-visible header widget is not worth it.
_alpha_price_cache: dict[int, dict[str, Any]] = {}


async def _run_status_refresh_subprocess(timeout: float = 600.0) -> None:
    """Run miner status refresh in a child process so bittensor never loads in uvicorn."""
    python_path = sys.executable or "python3"
    proc = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            python_path,
            REFRESH_STATUS_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=DASHBOARD_ROOT,
        ),
        timeout=10.0,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(err or out or f"refresh exited {proc.returncode}")
    if out:
        logger.info("Miner status refresh subprocess: %s", out.splitlines()[-1])
    elif err:
        logger.info("Miner status refresh subprocess stderr: %s", err.splitlines()[-1])


async def _miner_status_refresh_worker() -> None:
    while True:
        try:
            await _run_status_refresh_subprocess()
        except Exception as exc:
            logger.error("Miner status cache refresh failed: %s", exc)
        await asyncio.sleep(MINER_STATUS_REFRESH_SECONDS)


async def _run_investing88_sync_subprocess(timeout: float = 1800.0) -> None:
    """Run Investing88 daily cache sync in a child process."""
    python_path = sys.executable or "python3"
    proc = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            python_path,
            INVESTING88_SYNC_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=DASHBOARD_ROOT,
        ),
        timeout=10.0,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(err or out or f"investing88 sync exited {proc.returncode}")
    if out:
        logger.info("Investing88 sync subprocess: %s", out.splitlines()[-1])
    elif err:
        logger.info("Investing88 sync subprocess stderr: %s", err.splitlines()[-1])
    _investing88_cache.clear()


async def _investing88_daily_sync_worker() -> None:
    while True:
        await asyncio.sleep(INVESTING88_SYNC_INTERVAL_SECONDS)
        logger.info("Investing88 hourly sync starting (interval=%ss)", INVESTING88_SYNC_INTERVAL_SECONDS)
        try:
            await _run_investing88_sync_subprocess()
        except Exception as exc:
            logger.error("Investing88 cache sync failed: %s", exc)


async def _regbot_balance_refresh_worker() -> None:
    """Refresh every RegBot coldkey's balance to a file every
    REGBOT_BALANCE_CACHE_SECONDS, so /api/regbot/balance reads are served
    from disk instead of blocking on a live chain call per request."""
    from regbot import refresh_balance_cache

    while True:
        try:
            await refresh_balance_cache()
        except Exception as exc:
            logger.error("RegBot balance cache refresh failed: %s", exc)
        await asyncio.sleep(REGBOT_BALANCE_CACHE_SECONDS)


async def _coldkeys_watch_worker() -> None:
    """Collects netuid-79 coldkeys/IPs every COLDKEYS_WATCH_INTERVAL_SECONDS
    and diffs against the previous run, so the dashboard can flag ones seen
    for the first time."""
    from coldkeys_watch import run_collect_and_diff

    while True:
        try:
            await run_collect_and_diff()
        except Exception as exc:
            logger.error("Coldkey/IP watch run failed: %s", exc)
        await asyncio.sleep(COLDKEYS_WATCH_INTERVAL_SECONDS)


async def _sim_id_watch_worker() -> None:
    """Poll the live TAOS sim epoch and Telegram-alert on change.

    Forces a fresh check every cycle (bypassing get_sim_id's own 5-min cache)
    so a rotation is caught within SIM_ID_POLL_SECONDS instead of up to 5 min
    late. The first observation after startup only sets the baseline — it
    doesn't fire an alert, since that's not a real change, just this worker
    not having a prior value yet (avoids a spurious message on every restart).
    """
    from sim_uid import get_sim_id
    from telegram_notify import send_telegram_message

    while True:
        try:
            sim_id = await asyncio.to_thread(get_sim_id, True)
            last = _sim_id_state["last"]
            if sim_id and last is not None and sim_id != last:
                message = (
                    "🔄 <b>SN79 simulation updated</b>\n"
                    f"<code>{last}</code> → <code>{sim_id}</code>"
                )
                ok = await asyncio.to_thread(
                    send_telegram_message,
                    SIM_ID_TELEGRAM_BOT_TOKEN,
                    SIM_ID_TELEGRAM_CHAT_ID,
                    message,
                )
                if ok:
                    logger.info("Sim id changed %s -> %s, Telegram alert sent", last, sim_id)
                else:
                    logger.warning("Sim id changed %s -> %s, but Telegram alert failed to send", last, sim_id)
            if sim_id:
                _sim_id_state["last"] = sim_id
        except Exception as exc:
            logger.error("Sim id watch failed: %s", exc)
        await asyncio.sleep(SIM_ID_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_task = asyncio.create_task(_miner_status_refresh_worker())
    investing88_task = asyncio.create_task(_investing88_daily_sync_worker())
    sim_id_task = asyncio.create_task(_sim_id_watch_worker())
    keepalive_task = asyncio.create_task(_miner_master_keepalive_worker())
    regbot_balance_task = asyncio.create_task(_regbot_balance_refresh_worker())
    coldkeys_watch_task = asyncio.create_task(_coldkeys_watch_worker())

    async def _resume_regbot_batches() -> None:
        """A registration batch's orchestrator only lives in this process, so
        a restart used to silently strand every queue item that hadn't launched
        yet. Batch state is now persisted, so pick any interrupted one back up."""
        try:
            from regbot import resume_interrupted_batches

            result = await resume_interrupted_batches()
            if result["resumed"]:
                logger.info("Resumed interrupted batch(es): %s", ", ".join(result["resumed"]))
        except Exception as exc:
            logger.error("Could not resume interrupted batches: %s", exc)

    asyncio.create_task(_resume_regbot_batches())

    async def _bootstrap_investing88_cache() -> None:
        try:
            from investing88_cache import cache_exists_for_today

            if not cache_exists_for_today():
                logger.info("Investing88 cache missing; running bootstrap sync")
                await _run_investing88_sync_subprocess()
        except Exception as exc:
            logger.warning("Investing88 bootstrap sync skipped: %s", exc)

    asyncio.create_task(_bootstrap_investing88_cache())
    yield
    refresh_task.cancel()
    investing88_task.cancel()
    sim_id_task.cancel()
    keepalive_task.cancel()
    regbot_balance_task.cancel()
    coldkeys_watch_task.cancel()
    with suppress(asyncio.CancelledError):
        await refresh_task
        await investing88_task
        await sim_id_task
        await keepalive_task
        await regbot_balance_task
        await coldkeys_watch_task


app = FastAPI(title="SN71 Session Manager", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# Mount static files (for favicon and other assets)
app.mount("/asset", StaticFiles(directory="asset"), name="asset")

# Paths reachable without a valid session (login page + its POST, logout, static assets).
_AUTH_PUBLIC_PATHS = {"/login", "/logout"}
_AUTH_PUBLIC_PREFIXES = ("/asset/",)


def _safe_next_path(next_path: str) -> str:
    """Only allow same-site relative paths for post-login redirects (blocks open redirect)."""
    if next_path and next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/miners"


_LOCALHOST_IPS = {"127.0.0.1", "::1"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path in _AUTH_PUBLIC_PATHS or path.startswith(_AUTH_PUBLIC_PREFIXES):
        return await call_next(request)
    # Port 12800 is exposed directly with no reverse proxy in front of it (verified: nothing
    # in nginx's config proxies to it), so request.client.host is the real TCP peer, not a
    # spoofable header - trusting it for loopback callers (curl/scripts on this same box) is safe.
    if request.client and request.client.host in _LOCALHOST_IPS:
        request.state.user = "localhost"
        return await call_next(request)
    username = auth.verify_session_cookie(request.cookies.get(auth.SESSION_COOKIE_NAME))
    if not username:
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        next_qs = f"?next={quote(path + ('?' + request.url.query if request.url.query else ''))}"
        return RedirectResponse(url=f"/login{next_qs}", status_code=303)
    request.state.user = username
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/miners"):
    next = _safe_next_path(next)
    if auth.verify_session_cookie(request.cookies.get(auth.SESSION_COOKIE_NAME)):
        return RedirectResponse(url=next, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": next})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/miners"),
):
    next = _safe_next_path(next)
    if not auth.authenticate(username, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password.", "next": next},
            status_code=401,
        )
    response = RedirectResponse(url=next, status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE_NAME,
        auth.create_session_cookie(username),
        max_age=auth.SESSION_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return response


# Homepage: Miners page (default first load)
@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    return templates.TemplateResponse(request, "miners.html")

# Miners page (also at / for homepage)
@app.get("/miners", response_class=HTMLResponse)
async def miners_page(request: Request):
    return templates.TemplateResponse(request, "miners.html")

# --- Metagraph via Bittensor (configurable netuid, finney) ---
def _load_metagraph_from_bittensor(netuid: int = 71):
    """Blocking: load metagraph and return API-shaped dict for miners page. Uses lite=True for faster load."""
    import traceback
    try:
        from bittensor.core.metagraph import Metagraph
        network = "finney"
        logger.info(f"Creating Metagraph object for netuid={netuid}, network={network}")
        metagraph = Metagraph(netuid=netuid, network=network, lite=True, sync=True)
        logger.info(f"Metagraph loaded successfully with {len(metagraph.uids)} neurons")
        
        uids = metagraph.uids
        hotkeys = metagraph.hotkeys
        coldkeys = metagraph.coldkeys
        incentives = metagraph.I
        emissions = metagraph.E
        validator_permit = getattr(metagraph, "validator_permit", None)
        
        # Get immunity data
        current_block = metagraph.block
        last_update = metagraph.last_update
        immunity_period = 10000  # Default
        try:
            immunity_period = metagraph.subtensor.immunity_period(netuid=netuid)
            logger.info(f"Got immunity_period: {immunity_period} blocks")
        except Exception as e:
            logger.warning(f"Could not get immunity period, using default: {e}")
        
        n = len(uids)
        if validator_permit is None:
            validator_permit = [False] * n
        # Build same shape as subnet71 API for miners page
        hotkeyToUid = {}
        uidToHotkey = {}
        hotkeyToColdkey = {}
        coldkeyToHotkeys = {}
        emissions_dict = {}
        incentives_dict = {}
        isValidator = {}
        isImmune = {}
        minerAge = {}
        
        logger.info(f"Processing {n} neurons...")
        for i in range(n):
            hk = hotkeys[i]
            uid = int(uids[i].item()) if hasattr(uids[i], "item") else int(uids[i])
            ck = coldkeys[i]
            em = float(emissions[i].item()) if hasattr(emissions[i], "item") else float(emissions[i])
            inc = float(incentives[i].item()) if hasattr(incentives[i], "item") else float(incentives[i])
            val = bool(validator_permit[i].item()) if hasattr(validator_permit[i], "item") else bool(validator_permit[i])
            
            # Calculate immunity status
            last_update_block = int(last_update[i].item()) if hasattr(last_update[i], "item") else int(last_update[i])
            age_blocks = current_block - last_update_block
            is_immune = age_blocks < immunity_period
            
            hotkeyToUid[hk] = uid
            uidToHotkey[str(uid)] = hk
            hotkeyToColdkey[hk] = ck
            coldkeyToHotkeys.setdefault(ck, []).append(hk)
            emissions_dict[hk] = em
            incentives_dict[hk] = inc
            isValidator[hk] = val
            isImmune[hk] = is_immune
            minerAge[hk] = age_blocks
        
        immune_count = sum(1 for v in isImmune.values() if v)
        logger.info(f"Successfully processed metagraph: {n} neurons, {immune_count} immune")
        
        return {
            "hotkeyToUid": hotkeyToUid,
            "uidToHotkey": uidToHotkey,
            "hotkeyToColdkey": hotkeyToColdkey,
            "coldkeyToHotkeys": coldkeyToHotkeys,
            "emissions": emissions_dict,
            "incentives": incentives_dict,
            "isValidator": isValidator,
            "isImmune": isImmune,
            "minerAge": minerAge,
            "totalNeurons": n,
            "netuid": netuid,
            "currentBlock": current_block,
            "immunityPeriod": immunity_period,
            "error": None,
        }
    except Exception as e:
        error_msg = f"Error in _load_metagraph_from_bittensor: {e}"
        error_trace = traceback.format_exc()
        logger.error(error_msg)
        logger.error(f"Full traceback:\n{error_trace}")
        # Return error in response instead of raising
        return {
            "hotkeyToUid": {},
            "uidToHotkey": {},
            "hotkeyToColdkey": {},
            "coldkeyToHotkeys": {},
            "emissions": {},
            "incentives": {},
            "isValidator": {},
            "isImmune": {},
            "minerAge": {},
            "totalNeurons": 0,
            "netuid": netuid,
            "currentBlock": 0,
            "immunityPeriod": 10000,
            "error": str(e),
        }


MINER_PARAMS_DIR = os.path.join(os.path.dirname(__file__), "79Miners", "params")
# Canonical per-agent default params (dispatch_example.json). Bulk agent edits merge
# their modified params into the matching agent entry here as well as into every miner.
AGENT_DEFAULTS_FILE = os.getenv(
    "AGENT_DEFAULTS_FILE", "/root/79/Paul-SuperRegBot/myAgents/dispatch_example.json"
)
MINER_PARAMS_REMOTE_DIR = os.getenv("MINER_PARAMS_REMOTE_DIR", "/root/79/sn-79")
MINER_PARAMS_RSYNC_USER = os.getenv("MINER_PARAMS_RSYNC_USER", "root")
# The miner boxes are geographically spread (RTT 0.4ms for local Hetzner, but 125-160ms for
# the overseas ones). A params rsync is a ~4KB file, so its cost is NOT the transfer — it is
# the ~10-13 network round-trips of a fresh SSH handshake + rsync protocol, which at 160ms RTT
# is 2-3s. Connection multiplexing reuses one persistent SSH transport per box, so every sync
# after the first skips the TCP/KEX/auth round-trips (measured 3.0s -> 1.3s on the far boxes);
# GSSAPIAuthentication=no drops a pointless auth round-trip. ControlPersist keeps the master
# warm between confirms in an editing session. Still fully synchronous — just fewer round-trips.
MINER_PARAMS_RSYNC_SSH_OPTS = os.getenv(
    "MINER_PARAMS_RSYNC_SSH_OPTS",
    "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o GSSAPIAuthentication=no "
    "-o ControlMaster=auto -o ControlPath=/tmp/.mp-cm-%C -o ControlPersist=600",
)
DISPLAY_PARAM_KEYS = [
    "max_quantity",
    "min_quantity",
    "taker_ratio",
]
SERVER_NICKNAMES = {
    "65.108.232.96": "Server1",
    "65.108.108.139": "Server2",
    "65.109.87.169": "Server3",
    "178.16.143.193": "Server4",
    "144.91.111.126": "Server5",
    "65.109.65.81": "Server6",
    "84.32.34.89": "Server7",
    "88.216.195.45": "Server8",
    "65.109.104.253": "Server9",
    "65.108.0.181": "Server10",
    "65.21.129.168": "Server11",
    "135.181.138.51": "Server12",
}


def _miner_params_path(uid: int) -> str:
    return os.path.join(MINER_PARAMS_DIR, f"run_miner_{uid}.json")


def _coerce_param_value(original: Any, new_value: Any) -> Any:
    if isinstance(original, bool):
        if isinstance(new_value, bool):
            return new_value
        if isinstance(new_value, str):
            return new_value.strip().lower() in ("true", "1", "yes")
        return bool(new_value)
    if isinstance(original, int) and not isinstance(original, bool):
        # Accept booleans and numeric types; preserve fractional input when provided
        if isinstance(new_value, bool):
            return int(new_value)
        if isinstance(new_value, int):
            return int(new_value)
        if isinstance(new_value, float):
            # preserve fractional part if present
            if not new_value.is_integer():
                return float(new_value)
            return int(new_value)
        if isinstance(new_value, str):
            s = new_value.strip()
            # try integer first, then float
            try:
                return int(s)
            except ValueError:
                try:
                    f = float(s)
                    # if fractional part exists, return float so values like '1.5' are kept
                    if not f.is_integer():
                        return float(f)
                    return int(f)
                except ValueError:
                    # fallback: return original value unchanged
                    return original
    if isinstance(original, float):
        try:
            return float(new_value)
        except Exception:
            return original
    if isinstance(original, str):
        return str(new_value)
    return new_value


def _merge_params(current: dict, set_map: dict, remove: list) -> dict:
    """Merge a partial change into an existing params object, leaving other keys
    untouched. `remove` keys are dropped; `set_map` keys are added/overwritten
    (type-coerced to the existing value's type when the key already exists)."""
    out = dict(current) if isinstance(current, dict) else {}
    for key in remove:
        out.pop(key, None)
    for key, value in set_map.items():
        if key in current:
            out[key] = _coerce_param_value(current[key], value)
        else:
            out[key] = value
    return out


def _apply_defaults_bulk(agent_id: int, set_map: dict, remove: list) -> bool:
    """Merge a partial change into the dispatch_example.json entry whose agent_id
    matches. Returns True if an entry was found and the file rewritten."""
    path = AGENT_DEFAULTS_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    agents = data.get("agents")
    if not isinstance(agents, list):
        return False
    updated = False
    for agent in agents:
        if isinstance(agent, dict) and agent.get("agent_id") == agent_id:
            current = agent.get("params")
            agent["params"] = _merge_params(current if isinstance(current, dict) else {}, set_map, remove)
            updated = True
            break
    if updated:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    return updated


def _save_miner_params_file(uid: int, data: dict) -> str:
    os.makedirs(MINER_PARAMS_DIR, exist_ok=True)
    path = _miner_params_path(uid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


async def _rsync_miner_params(uid: int, local_path: str, miner_ip: str) -> tuple[bool, str]:
    """Push the params file to the miner box over SSH (atomic streaming copy, not rsync).

    For a single ~4KB file the transfer is nothing — the cost is network round-trips — and
    rsync's delta protocol adds ~5 extra round-trips over the plain SSH channel (measured
    ~1.2s vs ~0.5s per push on the 160ms-RTT boxes). So we stream the file straight to the
    miner: `ssh <opts> host 'cat > tmp && mv -f tmp final'` with the local file on stdin. The
    temp file is in the SAME directory as the target, so the closing `mv` is an atomic
    same-filesystem rename — the miner never reads a half-written file. Content is byte-
    identical to what rsync wrote (verified via sha256 on text + binary payloads); only the
    file mtime differs, which the miner doesn't use. The SSH opts carry ControlMaster
    multiplexing, so this reuses the warm master connection (see the keepalive worker).
    """
    if not miner_ip:
        return False, "miner_ip is missing from params file"
    final = f"{MINER_PARAMS_REMOTE_DIR}/run_miner_{uid}.json"
    # Unique temp name in the target's own directory: a concurrent or aborted push can't
    # clobber another and never leaves the live file half-written.
    tmp = f"{final}.{os.getpid()}.{uid}.tmp"
    remote_cmd = f"cat > {shlex.quote(tmp)} && mv -f {shlex.quote(tmp)} {shlex.quote(final)}"
    target = f"{MINER_PARAMS_RSYNC_USER}@{miner_ip}"
    cmd = ["ssh", *shlex.split(MINER_PARAMS_RSYNC_SSH_OPTS), target, remote_cmd]
    logger.info("Push miner params: ssh %s %s", target, remote_cmd)
    try:
        with open(local_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return False, f"could not read local params file: {exc}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=data), timeout=60.0)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        return False, "ssh push timed out after 60 seconds"
    except FileNotFoundError:
        return False, "ssh command not found"

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        return False, err or f"ssh push failed with exit code {proc.returncode}"

    return True, f"Synced to {final}"


async def _ssh_pm2_restart(uid: int, miner_ip: str) -> tuple[bool, str]:
    """Restart the miner's pm2 process (`79-miner-<uid>`) on its box over SSH.

    Reuses the same warm ControlMaster connection as the params push (see the keepalive
    worker), so this is a ~0.5s round trip on an already-open master. pm2 lives at a fixed
    path in the box's default non-interactive PATH (/usr/local/bin/pm2), so no login shell
    is needed. Best-effort: a returncode ≠ 0 (unknown process, box down) surfaces as an error.
    """
    if not miner_ip:
        return False, "miner_ip is missing"
    proc_name = f"79-miner-{uid}"
    target = f"{MINER_PARAMS_RSYNC_USER}@{miner_ip}"
    remote_cmd = f"pm2 restart {shlex.quote(proc_name)}"
    cmd = ["ssh", *shlex.split(MINER_PARAMS_RSYNC_SSH_OPTS), target, remote_cmd]
    logger.info("Restart miner pm2: ssh %s %s", target, remote_cmd)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        if proc is not None:
            with suppress(ProcessLookupError):
                proc.kill()
        return False, "ssh restart timed out after 60 seconds"
    except FileNotFoundError:
        return False, "ssh command not found"

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        return False, err or f"pm2 restart failed with exit code {proc.returncode}"

    return True, f"Restarted {proc_name} on {miner_ip}"


async def _ssh_pm2_restart_many(uids: list[int], miner_ip: str) -> tuple[bool, str]:
    """Restart every `79-miner-<uid>` on ONE box in a single `pm2 restart a b c …` call.

    Restarting all of a box's processes in one pm2 invocation (rather than one ssh per uid)
    reuses the warm master, avoids racing the pm2 daemon with concurrent CLI calls, and is
    far faster for a fan-out. Grouping is done by the caller (uids here all live on miner_ip).
    """
    if not miner_ip:
        return False, "miner_ip is missing"
    if not uids:
        return True, "no processes"
    names = [f"79-miner-{u}" for u in uids]
    target = f"{MINER_PARAMS_RSYNC_USER}@{miner_ip}"
    remote_cmd = "pm2 restart " + " ".join(shlex.quote(n) for n in names)
    cmd = ["ssh", *shlex.split(MINER_PARAMS_RSYNC_SSH_OPTS), target, remote_cmd]
    logger.info("Restart miner pm2 (batch of %d): ssh %s", len(names), target)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except asyncio.TimeoutError:
        if proc is not None:
            with suppress(ProcessLookupError):
                proc.kill()
        return False, "ssh restart timed out after 120 seconds"
    except FileNotFoundError:
        return False, "ssh command not found"

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        return False, err or f"pm2 restart failed with exit code {proc.returncode}"

    return True, f"Restarted {len(names)} process(es) on {miner_ip}"


def _resolve_miner_ip(uid: int) -> str:
    """The miner_ip recorded in the UID's params file, or '' when missing/unreadable."""
    path = _miner_params_path(uid)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            stored = (json.load(f) or {}).get("miner_ip", "")
    except (OSError, ValueError):
        return ""
    return stored.strip() if isinstance(stored, str) else ""


def _miner_ips() -> set[str]:
    """Distinct, non-empty miner IPs across every run_miner_*.json params file."""
    ips: set[str] = set()
    if not os.path.isdir(MINER_PARAMS_DIR):
        return ips
    for name in os.listdir(MINER_PARAMS_DIR):
        if not (name.startswith("run_miner_") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(MINER_PARAMS_DIR, name), "r", encoding="utf-8") as fh:
                ip = (json.load(fh) or {}).get("miner_ip")
        except (OSError, ValueError):
            continue
        if isinstance(ip, str) and ip.strip():
            ips.add(ip.strip())
    return ips


async def _warm_one_master(ip: str) -> None:
    """Open or refresh the persistent SSH master to one miner box (resets ControlPersist)."""
    target = f"{MINER_PARAMS_RSYNC_USER}@{ip}"
    cmd = ["ssh", *shlex.split(MINER_PARAMS_RSYNC_SSH_OPTS), target, "true"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=25.0)
    except asyncio.TimeoutError:
        if proc is not None:
            with suppress(ProcessLookupError):
                proc.kill()
    except Exception:
        pass


async def _miner_master_keepalive_worker() -> None:
    """Keep SSH master connections warm to every miner box so params pushes reuse them.

    ControlPersist keeps a master alive only for a while after its last use; with no traffic
    it expires, and then the next confirm pays the full ~2.5s cold SSH handshake again. This
    worker refreshes every box's master a little more often than ControlPersist, so a confirm
    to ANY miner (not just one edited moments ago) reuses a warm connection and finishes in
    ~0.5s. Runs once at startup, then every MINER_PARAMS_KEEPALIVE_SECONDS. Best-effort: an
    unreachable box just fails its refresh (and its confirms would fail anyway).
    """
    while True:
        try:
            ips = await asyncio.to_thread(_miner_ips)
            if ips:
                await asyncio.gather(*(_warm_one_master(ip) for ip in ips))
                logger.info("Warmed SSH masters to %d miner box(es)", len(ips))
        except Exception as exc:
            logger.warning("Miner SSH master keepalive failed: %s", exc)
        await asyncio.sleep(MINER_PARAMS_KEEPALIVE_SECONDS)


def _display_param_keys_for_agent(agent_name: str) -> list[str]:
    keys = list(DISPLAY_PARAM_KEYS)
    if agent_name and "Eater" in agent_name and "eat_quantity" not in keys:
        keys.append("eat_quantity")
    return keys


def _active_agent_pos(agents: list, agent_index: int) -> Optional[int]:
    """Return the list position of the active agent.

    agent_index identifies the agent by its `agent_id` field (not list
    position): find the entry whose agent_id == agent_index. Falls back to
    positional lookup for older files where agent_id isn't set. Returns None
    if nothing matches.
    """
    for i, agent in enumerate(agents):
        if isinstance(agent, dict) and agent.get("agent_id") == agent_index:
            return i
    if 0 <= agent_index < len(agents):
        return agent_index
    return None


def _select_active_agent(agents: list, agent_index: int) -> Optional[dict]:
    """Return the active agent entry (see _active_agent_pos for selection rule)."""
    pos = _active_agent_pos(agents, agent_index)
    return agents[pos] if pos is not None else None


def _read_miner_agent_summary(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        miner_ip = data.get("miner_ip") if isinstance(data.get("miner_ip"), str) else ""
        server = SERVER_NICKNAMES.get(miner_ip, miner_ip or None)
        agents = data.get("agents") or []
        agent_index = int(data.get("agent_index", 0))
        agent = _select_active_agent(agents, agent_index)
        if agent is not None:
            name = agent.get("name")
            if not isinstance(name, str):
                return None
            params = agent.get("params") or {}
            display_keys = _display_param_keys_for_agent(name)
            display_params = {
                key: params[key]
                for key in display_keys
                if key in params
            }
            return {
                "name": name,
                "params": display_params,
                "miner_ip": miner_ip,
                "server": server,
            }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


@app.get("/api/miner-params")
async def list_miner_params():
    uids = []
    agents = {}
    if os.path.isdir(MINER_PARAMS_DIR):
        for name in os.listdir(MINER_PARAMS_DIR):
            if name.startswith("run_miner_") and name.endswith(".json"):
                try:
                    uid = int(name[len("run_miner_") : -len(".json")])
                except ValueError:
                    continue
                uids.append(uid)
                path = os.path.join(MINER_PARAMS_DIR, name)
                summary = _read_miner_agent_summary(path)
                if summary:
                    agents[str(uid)] = summary
    return {"uids": sorted(uids), "agents": agents}


@app.get("/api/miner-params/{uid}")
async def get_miner_params(uid: int):
    path = _miner_params_path(uid)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"No params for UID {uid}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.put("/api/miner-params/{uid}")
async def update_miner_params(uid: int, payload: dict = Body(...)):
    path = _miner_params_path(uid)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"No params for UID {uid}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        raise HTTPException(status_code=400, detail="Invalid params file: missing agents list")

    try:
        previous_agent_index = int(data.get("agent_index", 0))
    except (TypeError, ValueError):
        previous_agent_index = 0

    agent_index = data.get("agent_index", 0)
    if "agent_index" in payload and payload["agent_index"] is not None:
        try:
            agent_index = int(payload["agent_index"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="agent_index must be an integer")
    else:
        try:
            agent_index = int(agent_index)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid agent_index in params file")
    # agent_index is the agent's agent_id, not its list position.
    active_pos = _active_agent_pos(agents, agent_index)
    if active_pos is None:
        raise HTTPException(status_code=400, detail=f"No agent with agent_id {agent_index}")

    data["agent_index"] = agent_index

    new_params = payload.get("params")
    if not isinstance(new_params, dict):
        raise HTTPException(status_code=400, detail="params must be an object")

    current_params = agents[active_pos].get("params", {})
    if not isinstance(current_params, dict):
        raise HTTPException(status_code=400, detail="Active agent params must be an object")

    # Replace the active agent's params with exactly what was sent, so the editor's
    # add/remove take effect: new keys are accepted (add), keys the caller omits are
    # dropped (remove), and keys that already exist are still type-coerced to match the
    # current value's type. Callers send the agent's full param set, so this is a
    # replace, not a merge.
    updated_params = {}
    for key, value in new_params.items():
        if key in current_params:
            updated_params[key] = _coerce_param_value(current_params[key], value)
        else:
            updated_params[key] = value

    agents[active_pos]["params"] = updated_params
    data["agents"] = agents

    sync_ip = payload.get("sync_ip")
    if isinstance(sync_ip, str):
        sync_ip = sync_ip.strip()
    else:
        sync_ip = ""
    if sync_ip:
        data["miner_ip"] = sync_ip
    miner_ip = sync_ip or data.get("miner_ip", "")

    local_path = _save_miner_params_file(uid, data)
    rsync_ok, rsync_message = await _rsync_miner_params(uid, local_path, miner_ip)

    from agent_history import record_agent_switch

    agent_switched = agent_index != previous_agent_index
    prev_pos = _active_agent_pos(agents, previous_agent_index)
    prev_agent = agents[prev_pos].get("name") if prev_pos is not None else None
    new_agent = agents[active_pos].get("name")
    source = "manual" if agent_switched else "params_update"

    # History records only the params actually modified in this edit — keys added or changed
    # vs the previous saved params, plus any removed keys — instead of the full param set,
    # mirroring the batch endpoint's diff-style record.
    changed_params = {
        key: value
        for key, value in updated_params.items()
        if key not in current_params or current_params[key] != value
    }
    removed_params = [key for key in current_params if key not in updated_params]
    history_extra = None
    if not agent_switched and (changed_params or removed_params):
        history_extra = {"params": changed_params}
        if removed_params:
            history_extra["removed"] = removed_params

    agent_history_path = record_agent_switch(
        uid,
        previous_agent_index,
        prev_agent,
        agent_index,
        new_agent,
        source=source,
        miner_ip=miner_ip or None,
        rsynced=rsync_ok,
        saved_locally=True,
        rsync_message=rsync_message if not rsync_ok else None,
        extra=history_extra,
    )

    return {
        "ok": True,
        "saved_locally": True,
        "rsynced": rsync_ok,
        "rsync_message": rsync_message,
        "remote_path": f"{MINER_PARAMS_REMOTE_DIR}/run_miner_{uid}.json",
        "miner_ip": miner_ip,
        "agent_switched": agent_switched,
        "agent_history_path": agent_history_path,
        "data": data,
    }


@app.post("/api/miner-restart/{uid}")
async def restart_miner(uid: int, payload: Optional[dict] = Body(default=None)):
    """Restart the miner's pm2 process (`79-miner-<uid>`) on its box over SSH.

    Resolves the target IP from an optional `{"ip": "..."}` body override, else the
    `miner_ip` recorded in the UID's params file. This only bounces the process — it does
    not touch params or history.
    """
    ip = ""
    if isinstance(payload, dict):
        raw = payload.get("ip") or payload.get("sync_ip")
        if isinstance(raw, str):
            ip = raw.strip()
    if not ip:
        ip = _resolve_miner_ip(uid)
    if not ip:
        raise HTTPException(status_code=400, detail=f"No miner IP known for UID {uid}")

    ok, message = await _ssh_pm2_restart(uid, ip)
    if not ok:
        raise HTTPException(status_code=502, detail=message)
    return {"ok": True, "uid": uid, "miner_ip": ip, "message": message}


@app.post("/api/miner-restart-bulk")
async def restart_miners_bulk(payload: dict = Body(...)):
    """Restart the pm2 process for many miners at once (fan-out of the single restart).

    Body: `{"uids": [10, 63, …]}`. Each UID's box IP is resolved from its params file; UIDs
    sharing a box are restarted in ONE `pm2 restart a b c …` call per box (one warm-master
    ssh each), all boxes in parallel. UIDs with no known IP are reported as failures.
    """
    raw = payload.get("uids") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="uids list required")
    uids: list[int] = []
    for u in raw:
        try:
            uids.append(int(u))
        except (TypeError, ValueError):
            continue
    uids = sorted(set(uids))
    if not uids:
        raise HTTPException(status_code=400, detail="no valid uids")
    if len(uids) > 500:
        raise HTTPException(status_code=400, detail="too many uids (max 500)")

    by_ip: dict[str, list[int]] = {}
    no_ip: list[int] = []
    for uid in uids:
        ip = _resolve_miner_ip(uid)
        if ip:
            by_ip.setdefault(ip, []).append(uid)
        else:
            no_ip.append(uid)

    ips = list(by_ip.keys())
    outcomes = await asyncio.gather(*(_ssh_pm2_restart_many(by_ip[ip], ip) for ip in ips))

    results: list[dict] = []
    restarted = 0
    for ip, (ok, message) in zip(ips, outcomes):
        box_uids = by_ip[ip]
        results.append({
            "ip": ip,
            "uids": box_uids,
            "ok": ok,
            "message": message if ok else None,
            "error": None if ok else message,
        })
        if ok:
            restarted += len(box_uids)
    for uid in no_ip:
        results.append({"ip": None, "uids": [uid], "ok": False, "message": None, "error": "no miner IP"})

    return {"ok": True, "count": len(uids), "restarted": restarted, "results": results}


@app.put("/api/agent-bulk")
async def update_agent_bulk(payload: dict = Body(...)):
    """Apply ONE partial params change to every miner currently running a given
    agent, plus to that agent's canonical defaults (dispatch_example.json).

    Only the modified params are touched on each miner — every other param on the
    miner is left as-is (a merge, not a replace). Optionally switches those miners
    to a different agent (agent_index != the target agent's id), carrying the merged
    params onto the newly-active agent.

    Body:
      target_agent: str   — select miners whose ACTIVE agent name == this
      agent_index: int    — agent_id to make active on each miner (== target's id
                            for a same-agent param edit; a different id switches)
      set:   {key: value} — params to add/overwrite (diff vs the agent's defaults)
      remove: [key, ...]  — params to delete
      sync:  bool         — rsync to the miner box (default True)
    """
    # uid-scoped mode: when an explicit `uids` list is given, apply the change to exactly
    # those miners (a dashboard selection / filtered subset) regardless of which agent each
    # runs, and DO NOT touch the canonical defaults. Otherwise select miners by active-agent
    # name (target_agent) and also update that agent's defaults.
    uids_raw = payload.get("uids")
    uid_scoped = isinstance(uids_raw, list) and len(uids_raw) > 0
    uid_set = None
    if uid_scoped:
        uid_set = set()
        for u in uids_raw:
            try:
                uid_set.add(int(u))
            except (TypeError, ValueError):
                continue
        if not uid_set:
            raise HTTPException(status_code=400, detail="uids must contain integers")

    target_agent = payload.get("target_agent")
    if not uid_scoped and (not isinstance(target_agent, str) or not target_agent):
        raise HTTPException(status_code=400, detail="target_agent (string) required")
    set_map = payload.get("set", {})
    remove = payload.get("remove", [])
    if not isinstance(set_map, dict):
        raise HTTPException(status_code=400, detail="set must be an object")
    if not isinstance(remove, list):
        raise HTTPException(status_code=400, detail="remove must be a list")
    remove = [str(k) for k in remove]

    if "agent_index" in payload and payload["agent_index"] is not None:
        try:
            agent_index = int(payload["agent_index"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="agent_index must be an integer")
    else:
        agent_index = None  # resolved per-miner below from its current active agent
    do_sync = payload.get("sync", True)

    # 1. Update the canonical defaults for the (possibly newly-selected) agent. When
    #    no explicit agent_index is given, fall back to the target agent's own id.
    #    Skipped entirely in uid-scoped mode — a subset edit must not move the defaults.
    defaults_updated = False
    if not uid_scoped:
        defaults_agent_id = agent_index
        if defaults_agent_id is None:
            try:
                with open(AGENT_DEFAULTS_FILE, "r", encoding="utf-8") as f:
                    dfl = json.load(f)
                for a in dfl.get("agents", []):
                    if isinstance(a, dict) and a.get("name") == target_agent:
                        defaults_agent_id = a.get("agent_id")
                        break
            except (OSError, json.JSONDecodeError):
                defaults_agent_id = None
        defaults_updated = (
            _apply_defaults_bulk(defaults_agent_id, set_map, remove)
            if defaults_agent_id is not None
            else False
        )

    # 2. Collect the target miners: an explicit uid set (uid-scoped), else every miner whose
    #    active agent name == target_agent.
    targets = []  # (uid, path, data, previous_agent_index)
    if os.path.isdir(MINER_PARAMS_DIR):
        for fn in os.listdir(MINER_PARAMS_DIR):
            if not (fn.startswith("run_miner_") and fn.endswith(".json")):
                continue
            try:
                uid = int(fn[len("run_miner_") : -len(".json")])
            except ValueError:
                continue
            if uid_scoped and uid not in uid_set:
                continue
            path = os.path.join(MINER_PARAMS_DIR, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            agents = data.get("agents")
            if not isinstance(agents, list) or not agents:
                continue
            try:
                prev_index = int(data.get("agent_index", 0))
            except (TypeError, ValueError):
                continue
            if not uid_scoped:
                active = _select_active_agent(agents, prev_index)
                if active is None or active.get("name") != target_agent:
                    continue
            targets.append((uid, path, data, prev_index))

    from agent_history import record_agent_switch

    async def apply_one(uid, path, data, prev_index):
        agents = data["agents"]
        new_index = agent_index if agent_index is not None else prev_index
        pos = _active_agent_pos(agents, new_index)
        if pos is None:
            return {"uid": uid, "ok": False, "error": f"no agent with agent_id {new_index}"}
        current = agents[pos].get("params")
        agents[pos]["params"] = _merge_params(current if isinstance(current, dict) else {}, set_map, remove)
        data["agent_index"] = new_index
        miner_ip = data.get("miner_ip", "") or ""
        local_path = _save_miner_params_file(uid, data)
        rsync_ok, rsync_message = (True, "sync skipped")
        if do_sync:
            rsync_ok, rsync_message = await _rsync_miner_params(uid, local_path, miner_ip)
        switched = new_index != prev_index
        prev_pos = _active_agent_pos(agents, prev_index)
        prev_agent = agents[prev_pos].get("name") if prev_pos is not None else None
        new_agent = agents[pos].get("name")
        record_agent_switch(
            uid,
            prev_index,
            prev_agent,
            new_index,
            new_agent,
            source="manual" if switched else "params_update",
            miner_ip=miner_ip or None,
            rsynced=rsync_ok,
            saved_locally=True,
            rsync_message=rsync_message if not rsync_ok else None,
            extra={"params": set_map, "removed": remove} if (set_map or remove) else None,
        )
        return {
            "uid": uid,
            "ok": True,
            "rsynced": rsync_ok,
            "rsync_message": rsync_message,
            "miner_ip": miner_ip,
            "agent_switched": switched,
        }

    results = await asyncio.gather(
        *[apply_one(*t) for t in targets], return_exceptions=True
    )
    miners = []
    for t, r in zip(targets, results):
        if isinstance(r, Exception):
            miners.append({"uid": t[0], "ok": False, "error": str(r)})
        else:
            miners.append(r)

    return {
        "ok": True,
        "agent": target_agent if not uid_scoped else None,
        "uid_scoped": uid_scoped,
        "agent_index": agent_index,
        "defaults_updated": defaults_updated,
        "count": len(miners),
        "updated": sum(1 for m in miners if m.get("ok")),
        "synced": sum(1 for m in miners if m.get("rsynced")),
        "miners": miners,
    }


# Cache metagraph per netuid; TTL 2 min. Use ?refresh=1 to force fresh load.
METAGRAPH_CACHE_TTL = 120
_metagraph_cache: dict = {}  # netuid -> {"data": ..., "timestamp": ...}

def _parse_netuid(raw: Optional[str], default: int = 71) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        n = int(raw.strip())
        if n < 0 or n > 255:
            return default
        return n
    except ValueError:
        return default

@app.get("/api/metagraph-data")
async def get_metagraph_data(request: Request):
    import time
    import asyncio
    import traceback
    try:
        raw_netuid = request.query_params.get("netuid")
        netuid = _parse_netuid(raw_netuid, 71)
        current_time = time.time()
        refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
        skip_cache = refresh

        cache_entry = _metagraph_cache.get(netuid)
        if not skip_cache and cache_entry and (current_time - cache_entry["timestamp"]) < METAGRAPH_CACHE_TTL:
            logger.info(f"Returning cached metagraph data for netuid={netuid}")
            return cache_entry["data"]

        # Load from Bittensor Metagraph (using subprocess to avoid asyncio/threading issues)
        logger.info(f"Loading fresh metagraph data for netuid={netuid}...")
        try:
            import subprocess
            import json
            
            # Call standalone script as subprocess with timeout
            python_path = sys.executable or "python3"
            script_path = os.path.join(os.path.dirname(__file__), "load_metagraph.py")
            
            logger.info(f"Calling subprocess: {python_path} {script_path} {netuid}")
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    python_path, script_path, str(netuid),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                ),
                timeout=5.0  # 5 seconds to start the process
            )
            
            # Wait for process to complete with 90 second timeout
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=90.0
            )
            
            if proc.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='replace')
                logger.error(f"Subprocess failed with code {proc.returncode}: {error_msg}")
                raise HTTPException(status_code=500, detail=f"Failed to load metagraph: {error_msg}")
            
            data = json.loads(stdout.decode('utf-8'))
            logger.info(f"Successfully loaded metagraph with {data.get('totalNeurons', 0)} neurons")
            
            if data.get("error"):
                logger.error(f"Error from subprocess: {data['error']}")
                raise HTTPException(status_code=500, detail=data["error"])
            
            _metagraph_cache[netuid] = {"data": data, "timestamp": current_time}
            return data
        except asyncio.TimeoutError:
            logger.error("Timeout error: Metagraph loading exceeded 90 seconds")
            raise HTTPException(status_code=504, detail="Metagraph loading timeout")
    except HTTPException:
        raise
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Error loading metagraph from Bittensor: {e}")
        logger.error(f"Full traceback:\n{error_details}")
        raise HTTPException(status_code=500, detail=str(e))


UID_STATUS_CACHE_TTL = 60
_uid_status_cache: dict = {}  # (uid, include_books) -> {"data": ..., "timestamp": ...}


def _store_uid_status_cache(cache_key: tuple, data: dict, timestamp: float) -> None:
    if len(_uid_status_cache) >= UID_STATUS_CACHE_MAX:
        oldest = min(_uid_status_cache, key=lambda k: _uid_status_cache[k]["timestamp"])
        del _uid_status_cache[oldest]
    _uid_status_cache[cache_key] = {"data": data, "timestamp": timestamp}


@app.get("/api/uid-status/{uid}")
async def get_uid_status(uid: int, request: Request):
    import asyncio
    import time

    if uid < 0 or uid > 65535:
        raise HTTPException(status_code=400, detail="Invalid UID")

    include_books = request.query_params.get("books", "").lower() in ("1", "true", "yes")
    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    cache_key = (uid, include_books)
    current_time = time.time()

    if not refresh:
        cache_entry = _uid_status_cache.get(cache_key)
        if cache_entry and (current_time - cache_entry["timestamp"]) < UID_STATUS_CACHE_TTL:
            return cache_entry["data"]

    try:
        from uid_status_checker import build_uid_status_response

        data = await asyncio.to_thread(build_uid_status_response, uid, include_books)
        _store_uid_status_cache(cache_key, data, current_time)
        return data
    except ImportError as e:
        logger.error("UID status module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"UID status module unavailable: {e}")
    except Exception as e:
        logger.error("UID status fetch failed for uid=%s: %s", uid, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/uid-metrics")
async def get_uid_metrics(request: Request):
    import asyncio
    from datetime import datetime, timezone
    from miner_status_cache import get_cached_metrics_payload, load_daily_status, filter_metrics

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    uids_param = request.query_params.get("uids", "").strip()
    uids: Optional[list[int]] = None
    if uids_param:
        try:
            uids = [int(x.strip()) for x in uids_param.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid uids parameter")

    if not refresh:
        cached = get_cached_metrics_payload(uids)
        if cached:
            return cached

    try:
        cached = None
        if uids:
            from uid_status_checker import fetch_agent_display_metrics
            metrics = await asyncio.to_thread(fetch_agent_display_metrics, None, uids)
        else:
            await _run_status_refresh_subprocess()
            cached = load_daily_status()
            if not cached:
                raise HTTPException(status_code=502, detail="Refresh completed but cache file missing")
            metrics = filter_metrics(cached.get("metrics", {}), uids)
        from miner_status_cache import load_prev_epoch_metrics
        prev = load_prev_epoch_metrics()
        payload = {
            "metrics": metrics,
            "prev_metrics": prev.get("metrics", {}) if prev else {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": "live" if uids else "file",
        }
        if cached and cached.get("current_block") is not None:
            payload["current_block"] = cached["current_block"]
        return payload
    except ImportError as e:
        logger.error("UID metrics module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"UID status module unavailable: {e}")
    except Exception as e:
        logger.error("UID metrics fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/miner-history")
async def list_miner_history_meta():
    from miner_history import list_history_days, list_my_miner_uids

    return {
        "tracked_uids": list_my_miner_uids(),
        "days": list_history_days(),
    }


@app.get("/api/miner-history/{uid}")
async def get_miner_history(uid: int, request: Request):
    from miner_history import build_uid_history, list_my_miner_uids

    if uid not in list_my_miner_uids():
        raise HTTPException(status_code=404, detail=f"No history tracking for UID {uid}")
    try:
        days = max(1, min(int(request.query_params.get("days", "7")), 90))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid days parameter")
    day = request.query_params.get("day", "").strip() or None
    limit_param = request.query_params.get("limit", "").strip()
    limit: Optional[int] = None
    if limit_param:
        try:
            limit = max(1, int(limit_param))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid limit parameter")
    return build_uid_history(uid, days=days, day=day, limit=limit)


@app.get("/api/agent-history/{uid}")
async def get_agent_history(uid: int, request: Request):
    from agent_history import build_agent_history_response, uid_history_path

    if not os.path.isfile(uid_history_path(uid)):
        return {"uid": uid, "count": 0, "entries": []}
    limit_param = request.query_params.get("limit", "").strip()
    limit: Optional[int] = 50
    if limit_param:
        try:
            limit = max(1, min(int(limit_param), 500))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid limit parameter")
    return build_agent_history_response(uid, limit=limit)


@app.get("/api/fee-regime")
async def get_fee_regime(request: Request):
    import asyncio
    import time
    from datetime import datetime, timezone
    from fee_rate import build_fee_regime_summary, fetch_fee_rates

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    include_books = request.query_params.get("books", "").lower() in ("1", "true", "yes")
    now = time.time()
    if (
        not refresh
        and not include_books
        and _fee_regime_cache["payload"] is not None
        and now < _fee_regime_cache["expires_at"]
    ):
        return _fee_regime_cache["payload"]

    def _cached_fallback(reason: str):
        """Serve the last-good regime summary during a transient upstream miss
        instead of failing the whole panel."""
        cached = _fee_regime_cache["payload"]
        if cached is not None:
            logger.warning("Fee regime unavailable (%s); serving cached summary", reason)
            stale = dict(cached)
            stale["stale"] = True
            return stale
        return None

    try:
        maker, taker = await asyncio.to_thread(fetch_fee_rates)
        summary = build_fee_regime_summary(maker, taker)
        if summary.get("error"):
            fallback = _cached_fallback(summary["error"])
            if fallback is not None:
                return fallback
            raise HTTPException(status_code=502, detail=summary["error"])
        if not include_books:
            summary.pop("books", None)
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Cache the regime summary (without per-book payload) both for reuse and
        # as a fallback during transient upstream misses. Populate on the books
        # path too so a fallback exists even if the first request asked for books.
        _fee_regime_cache["payload"] = {k: v for k, v in summary.items() if k != "books"}
        _fee_regime_cache["expires_at"] = now + FEE_REGIME_CACHE_SECONDS
        return summary
    except HTTPException:
        raise
    except ImportError as e:
        logger.error("Fee regime module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Fee regime module unavailable: {e}")
    except Exception as e:
        fallback = _cached_fallback(str(e))
        if fallback is not None:
            return fallback
        logger.error("Fee regime fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/fee-regime/history")
async def get_fee_regime_history(request: Request):
    from fee_rate import build_fee_history, list_fee_history_days

    try:
        days = max(1, min(int(request.query_params.get("days", "7")), 90))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid days parameter")
    day = request.query_params.get("day", "").strip() or None
    uid_param = request.query_params.get("uid", "").strip()
    uid: Optional[int] = None
    if uid_param:
        try:
            uid = int(uid_param)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid uid parameter")
    limit_param = request.query_params.get("limit", "").strip()
    limit: Optional[int] = None
    if limit_param:
        try:
            limit = max(1, int(limit_param))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid limit parameter")
    payload = build_fee_history(days=days, day=day, uid=uid, limit=limit)
    payload["days_available"] = list_fee_history_days()
    return payload


@app.get("/api/reg-cost")
async def get_reg_cost(request: Request):
    import asyncio
    import time
    from datetime import datetime, timezone
    from regbot import run_chain_cli

    try:
        netuid = int(request.query_params.get("netuid", "79"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid netuid parameter")
    if netuid < 0 or netuid > 255:
        raise HTTPException(status_code=400, detail="Invalid netuid parameter")

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    now = time.time()
    cached = _reg_cost_cache.get(netuid)
    if not refresh and cached and now < cached.get("expires_at", 0):
        return cached["payload"]

    try:
        # Out of process, not in a thread: this query takes ~5s of pure-Python substrate
        # decoding, and run in-process it holds the GIL for that whole time — measured, it
        # took the event loop's response latency from ~1ms to 6-400ms for the duration, so
        # every other caller (an agent Confirm, a poll) waited on it. See regbot.chain_cli.
        payload_cli = await run_chain_cli(["reg-cost", str(netuid)], timeout=120.0)
        result = payload_cli.get("result") if payload_cli.get("ok") else None
        if result is None:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch reg cost for subnet {netuid}: {payload_cli.get('error') or 'no result'}",
            )
        payload = {
            **result,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _reg_cost_cache[netuid] = {"payload": payload, "expires_at": now + REG_COST_CACHE_SECONDS}
        return payload
    except HTTPException:
        raise
    except ImportError as e:
        logger.error("Reg cost module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Reg cost module unavailable: {e}")
    except Exception as e:
        logger.error("Reg cost fetch failed for netuid=%s: %s", netuid, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/sim-id")
async def get_sim_id_endpoint():
    """Currently-live TAOS simulation epoch for subnet 79 (get_sim_id has its
    own 5-min cache, so this is cheap to poll on every dashboard refresh)."""
    import asyncio
    from sim_uid import get_sim_id

    try:
        sim_id = await asyncio.to_thread(get_sim_id)
        return {"sim_id": sim_id}
    except Exception as e:
        logger.error("Sim id fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


def _load_alpha_series(netuid: int) -> Optional[dict[str, Any]]:
    """Parsed contents of ALPHA_PRICE_DIR/<netuid>.json, or None if there is no
    such file. Returns rows as (timestamp, high, low, close); open and volume are
    dropped because nothing here plots them."""
    path = os.path.join(ALPHA_PRICE_DIR, f"{netuid}.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    cached = _alpha_price_cache.get(netuid)
    if cached and cached.get("mtime") == mtime:
        return cached

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    rows = []
    for row in raw.get("series") or []:
        # [date, open, high, low, close, volume]
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            rows.append((str(row[0]), float(row[2]), float(row[3]), float(row[4])))
        except (TypeError, ValueError):
            continue

    entry = {
        "mtime": mtime,
        "name": raw.get("name"),
        "rows": rows,
    }
    _alpha_price_cache[netuid] = entry
    return entry


def _downsample_last(rows: list, max_points: int) -> list:
    """Thin `rows` to at most max_points by keeping the last row of each bucket.

    Last-of-bucket rather than an average because these are closes: averaging
    invents prices that never traded, and the final point must stay the real
    latest price. Intra-bucket extremes are lost, which is why the endpoint
    reports high/low from the full window instead of from this output.
    """
    n = len(rows)
    if n <= max_points:
        return list(rows)
    step = n / max_points
    out = [rows[int((i + 1) * step) - 1] for i in range(max_points)]
    out[-1] = rows[-1]
    return out


@app.get("/api/alpha-price")
async def get_alpha_price(request: Request):
    """Alpha price history for a subnet, windowed and thinned for the header
    sparkline."""
    from datetime import timezone

    try:
        netuid = int(request.query_params.get("netuid", "79"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid netuid parameter")
    if netuid < 0 or netuid > 255:
        raise HTTPException(status_code=400, detail="Invalid netuid parameter")

    range_key = (request.query_params.get("range") or ALPHA_PRICE_DEFAULT_RANGE).lower()
    if range_key not in ALPHA_PRICE_RANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid range; expected one of {', '.join(ALPHA_PRICE_RANGES)}",
        )

    try:
        entry = await asyncio.to_thread(_load_alpha_series, netuid)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error("Alpha price read failed for netuid=%s: %s", netuid, e)
        raise HTTPException(status_code=502, detail=f"Could not read alpha price data: {e}")

    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"No alpha price history for subnet {netuid}"
        )
    rows = entry["rows"]
    if not rows:
        raise HTTPException(
            status_code=502, detail=f"Alpha price history for subnet {netuid} is empty"
        )

    hours = ALPHA_PRICE_RANGES[range_key]
    window = rows
    if hours is not None:
        # Sliced on the timestamps rather than by row count: the series is hourly
        # but not gap-free, so "the last N rows" would quietly overstate the window.
        try:
            end = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M")
            cutoff = (end - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
            # Lexicographic compare is exact for zero-padded "%Y-%m-%d %H:%M".
            start_i = next(
                (i for i, r in enumerate(rows) if r[0] >= cutoff), len(rows) - 1
            )
            window = rows[start_i:]
        except ValueError:
            window = rows[-hours:] if hours < len(rows) else rows

    first_close = window[0][3]
    last_close = window[-1][3]
    change = last_close - first_close
    change_pct = (change / first_close * 100.0) if first_close else None

    points = _downsample_last(window, ALPHA_PRICE_MAX_POINTS)

    return {
        "netuid": netuid,
        "name": entry.get("name"),
        "range": range_key,
        # [[timestamp, close], ...] — the timestamp is passed through verbatim from
        # the source file, which carries no timezone marker, so it is not restated
        # as UTC here or in the UI.
        "points": [[p[0], p[3]] for p in points],
        "current": last_close,
        "first": first_close,
        "change": change,
        "change_pct": change_pct,
        # From the full window, not from `points` — see _downsample_last.
        "high": max(r[1] for r in window),
        "low": min(r[2] for r in window),
        "period_start": window[0][0],
        "period_end": window[-1][0],
        "source_points": len(window),
        "downsampled": len(points) < len(window),
        "updated_at": datetime.fromtimestamp(entry["mtime"], timezone.utc).isoformat(),
    }


@app.get("/api/investing88/miner/{uid}")
async def get_investing88_miner(uid: int, request: Request):
    import asyncio
    import time

    if uid < 0 or uid > 255:
        raise HTTPException(status_code=400, detail="Invalid uid")

    hotkey = request.query_params.get("hotkey", "").strip() or None
    rank_param = request.query_params.get("rank", "").strip()
    rank: Optional[int] = None
    if rank_param:
        try:
            rank = int(rank_param)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid rank parameter")

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    force_live = request.query_params.get("live", "").lower() in ("1", "true", "yes")
    now = time.time()
    cached = _investing88_cache.get(uid)
    if not refresh and not force_live and cached and now < cached.get("expires_at", 0):
        return cached["payload"]

    try:
        from investing88_miner import fetch_miner_details

        payload = await asyncio.to_thread(
            fetch_miner_details,
            uid,
            hotkey,
            rank,
            use_cache=not force_live,
            force_live=force_live,
        )
        _investing88_cache[uid] = {"payload": payload, "expires_at": now + INVESTING88_CACHE_SECONDS}
        return payload
    except ImportError as e:
        logger.error("Investing88 module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Investing88 module unavailable: {e}")
    except Exception as e:
        logger.error("Investing88 fetch failed for uid=%s: %s", uid, e)
        raise HTTPException(status_code=502, detail=str(e))


_I88_SCORE_FIELDS = (
    "asset", "days", "last", "score", "value", "lsr", "mar",
    "risk_percent", "odds_percent", "daily_percent", "yield_percent",
)


def _extract_i88_scores(payload: dict) -> dict:
    """Pull per-uid score fields out of an Investing88 cache payload."""
    scores: dict = {}
    for uid_str, miner in (payload.get("miners") or {}).items():
        score = miner.get("score") if isinstance(miner, dict) else None
        if not isinstance(score, dict):
            continue
        scores[uid_str] = {k: score.get(k) for k in _I88_SCORE_FIELDS}
    return scores


@app.get("/api/investing88/days")
async def get_investing88_days(request: Request):
    import asyncio
    import os
    import re
    import time

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    now = time.time()
    if not refresh:
        cached = _investing88_cache.get(-1)
        if cached and now < cached.get("expires_at", 0):
            return cached["payload"]

    try:
        from investing88_cache import load_cache_payload, CACHE_DIR

        payload = await asyncio.to_thread(load_cache_payload)
        if not payload:
            raise HTTPException(status_code=502, detail="Investing88 cache is unavailable")
        scores = _extract_i88_scores(payload)

        # Previous-day scores for day-over-day deltas: second-newest dated file.
        prev_scores: dict = {}
        try:
            dated = sorted(
                n[:-5]
                for n in os.listdir(CACHE_DIR)
                if re.match(r"\d{4}-\d{2}-\d{2}\.json$", n)
            )
            if len(dated) >= 2:
                prev_payload = await asyncio.to_thread(load_cache_payload, dated[-2])
                if prev_payload:
                    prev_scores = _extract_i88_scores(prev_payload)
        except Exception as prev_exc:
            logger.warning("Investing88 prev-day scores unavailable: %s", prev_exc)

        result = {
            "days": payload.get("days", {}),
            "scores": scores,
            "prev_scores": prev_scores,
        }
        _investing88_cache[-1] = {"payload": result, "expires_at": now + INVESTING88_CACHE_SECONDS}
        return result
    except ImportError as e:
        logger.error("Investing88 cache module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Investing88 cache module unavailable: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Investing88 days fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/investing88/cache-status")
async def get_investing88_cache_status():
    try:
        from investing88_cache import get_cache_status

        return get_cache_status()
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Investing88 cache module unavailable: {e}")


@app.post("/api/investing88/sync")
async def sync_investing88_cache():
    try:
        await _run_investing88_sync_subprocess()
        from investing88_cache import get_cache_status

        return get_cache_status()
    except Exception as e:
        logger.error("Investing88 manual sync failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ==================== RegBot (SN79 registration + miner launch) ====================
# Triggers Paul-SuperRegBot/run79.sh on a remote server via SSH. This spends
# real TAO (up to reglimit per hotkey, max_count hotkeys) and launches a live
# pm2 miner on success — every trigger is written to a local audit log.

class RegBotRunRequest(BaseModel):
    server: str
    dispatch_agent: str
    reglimit: float
    max_count: int
    dispatch_agent_id: Optional[int] = None
    port_prefix: Optional[str] = None  # None = use run79.sh's own default band

    model_config = ConfigDict(extra="forbid")


class RegBotKillRequest(BaseModel):
    server: str
    run_id: str

    model_config = ConfigDict(extra="forbid")


class UnstakeTransferRequest(BaseModel):
    server: str
    mode: str = "unstake"  # "unstake" | "unstake_transfer" | "transfer"
    destination: Optional[str] = None  # required for the transfer modes
    amount: Optional[float] = None  # "transfer" only; None sweeps the balance
    netuid: int = 79

    model_config = ConfigDict(extra="forbid")


class UnstakeBatchItem(BaseModel):
    server: str
    mode: str = "unstake"
    destination: Optional[str] = None
    amount: Optional[float] = None

    model_config = ConfigDict(extra="forbid")


class UnstakeBatchStartRequest(BaseModel):
    items: list[UnstakeBatchItem]

    model_config = ConfigDict(extra="forbid")


class BatchServerConfig(BaseModel):
    server: str
    count: int
    reglimit: float
    port_prefix: Optional[str] = None  # None = use run79.sh's own default band

    model_config = ConfigDict(extra="forbid")


class BatchStartRequest(BaseModel):
    servers: list[BatchServerConfig]
    dispatch_agent: str
    dispatch_agent_id: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


@app.get("/api/regbot/presets")
async def regbot_presets():
    from regbot import get_server_presets, get_dispatch_agent_options, load_balance_cache

    servers = get_server_presets()
    balances = (load_balance_cache() or {}).get("balances", {})
    for s in servers:
        bal = balances.get(s["name"])
        s["balance_tao"] = bal["tao"] if bal else None

    return {
        "servers": servers,
        "dispatch_agents": get_dispatch_agent_options(),
    }


@app.post("/api/regbot/run")
async def regbot_run(payload: RegBotRunRequest):
    from regbot import trigger_run, RegBotError

    try:
        return await trigger_run(
            payload.server,
            payload.dispatch_agent,
            payload.reglimit,
            payload.max_count,
            dispatch_agent_id=payload.dispatch_agent_id,
            port_prefix=payload.port_prefix,
        )
    except RegBotError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("RegBot run failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/regbot/log")
async def regbot_log(server: str, run_id: str, lines: int = 200):
    from regbot import tail_log, RegBotError

    try:
        return await tail_log(server, run_id, lines)
    except RegBotError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("RegBot log fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/regbot/kill")
async def regbot_kill(payload: RegBotKillRequest):
    from regbot import kill_run, RegBotError

    try:
        result = await kill_run(payload.server, payload.run_id)
    except RegBotError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("RegBot kill failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    if result.get("result") == "KILL_FAILED":
        raise HTTPException(
            status_code=502,
            detail="Process did not die even after SIGKILL — it may be stuck in "
            "uninterruptible I/O, or the pidfile is stale from an older run. "
            "Check the server manually.",
        )
    return result


@app.get("/api/regbot/balance")
async def regbot_balance(server: str, refresh: bool = False):
    from regbot import get_server_balance, load_balance_cache, RegBotError

    if not refresh:
        cache = load_balance_cache()
        cached = (cache or {}).get("balances", {}).get(server)
        if cached:
            return cached
        # No file cache yet (e.g. right after a fresh deploy, before the
        # background worker's first pass completes) — fall through to a
        # live fetch so the UI isn't stuck empty.

    try:
        return await get_server_balance(server)
    except RegBotError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("RegBot balance fetch failed for server=%s: %s", server, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/regbot/balance/refresh")
async def regbot_balance_refresh():
    """Force-refresh every server's balance/stake cache immediately, instead
    of waiting for the next REGBOT_BALANCE_CACHE_SECONDS background cycle —
    used on page load/refresh so the Add Miner / Unstake / Multi-Server
    Register modals always show current numbers."""
    from regbot import refresh_balance_cache

    try:
        return await refresh_balance_cache()
    except Exception as e:
        logger.error("RegBot balance refresh failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/regbot/history")
async def regbot_history(limit: int = 50):
    from regbot import list_recent_runs

    return {"runs": list_recent_runs(limit)}


@app.get("/api/regbot/active")
async def regbot_active():
    from regbot import get_active_runs

    try:
        return await get_active_runs()
    except Exception as e:
        logger.error("RegBot active-run scan failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/regbot/batch/start")
async def regbot_batch_start(payload: BatchStartRequest):
    from regbot import start_batch, RegBotError

    try:
        server_configs = [
            {
                "server": s.server,
                "count": s.count,
                "reglimit": s.reglimit,
                "port_prefix": s.port_prefix,
            }
            for s in payload.servers
        ]
        return await start_batch(
            server_configs,
            payload.dispatch_agent,
            dispatch_agent_id=payload.dispatch_agent_id,
        )
    except RegBotError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Batch start failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/regbot/batch/active")
async def regbot_batch_active():
    """`batches` is the full set (several can run at once); `batch` is the most
    recent one, kept so existing callers keep working."""
    from regbot import get_active_batch, get_active_batches

    return {"batches": get_active_batches(), "batch": get_active_batch()}


@app.get("/api/regbot/batch/{batch_id}")
async def regbot_batch_status(batch_id: str):
    from regbot import get_batch_status, RegBotError

    try:
        return get_batch_status(batch_id)
    except RegBotError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/regbot/batch/{batch_id}/cancel")
async def regbot_batch_cancel(batch_id: str):
    from regbot import cancel_batch, RegBotError

    try:
        return await cancel_batch(batch_id)
    except RegBotError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/unstake/presets")
async def unstake_presets():
    from regbot import load_balance_cache
    from unstake import get_unstake_server_presets

    servers = get_unstake_server_presets()
    balances = (load_balance_cache() or {}).get("balances", {})
    for s in servers:
        bal = balances.get(s["name"])
        s["balance_tao"] = bal["tao"] if bal else None
        s["staked_tao"] = bal.get("staked_tao") if bal else None
    return {"servers": servers}


@app.post("/api/unstake/preview")
async def unstake_preview(payload: UnstakeTransferRequest):
    from unstake import preview_unstake, UnstakeError

    try:
        return await preview_unstake(
            payload.server, payload.mode, payload.destination, payload.netuid,
            amount=payload.amount,
        )
    except UnstakeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Unstake preview failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/unstake/run")
async def unstake_run(payload: UnstakeTransferRequest):
    from unstake import start_unstake_run, UnstakeError

    try:
        return await start_unstake_run(
            payload.server, payload.mode, payload.destination, payload.netuid,
            amount=payload.amount,
        )
    except UnstakeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Unstake run failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/unstake/log")
async def unstake_log(run_id: str):
    from unstake import get_unstake_run_status, UnstakeError

    try:
        return get_unstake_run_status(run_id)
    except UnstakeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Unstake log fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/unstake/active")
async def unstake_active():
    from unstake import get_active_unstake_runs_view

    return {"active": get_active_unstake_runs_view()}


@app.post("/api/unstake/batch/start")
async def unstake_batch_start(payload: UnstakeBatchStartRequest):
    from unstake import start_unstake_batch, UnstakeError

    try:
        items = [item.model_dump() for item in payload.items]
        return await start_unstake_batch(items)
    except UnstakeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Unstake batch start failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/unstake/batch/active")
async def unstake_batch_active():
    from unstake import get_active_unstake_batch

    return {"batch": get_active_unstake_batch()}


@app.get("/api/unstake/batch/{batch_id}")
async def unstake_batch_status(batch_id: str):
    from unstake import get_unstake_batch_status, UnstakeError

    try:
        return get_unstake_batch_status(batch_id)
    except UnstakeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Unstake batch status fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/unstake/history")
async def unstake_history(limit: int = 50):
    from unstake import list_recent_runs

    return {"runs": list_recent_runs(limit)}


@app.get("/api/coldkeys/summary")
async def coldkeys_summary():
    from coldkeys_watch import get_latest_summary

    return get_latest_summary() or {
        "run_at": None,
        "is_first_run": True,
        "new_coldkeys": [],
        "new_ips": [],
        "new_coldkey_to_ips": {},
        "total_coldkeys": 0,
        "total_ips": 0,
        "total_miners": 0,
    }


@app.get("/api/coldkeys/list")
async def coldkeys_list():
    from coldkeys_watch import get_latest_collection

    data = get_latest_collection()
    if not data:
        raise HTTPException(status_code=404, detail="No coldkey/IP collection has run yet")
    return data


@app.post("/api/coldkeys/refresh")
async def coldkeys_refresh():
    from coldkeys_watch import run_collect_and_diff

    try:
        return await run_collect_and_diff()
    except Exception as e:
        logger.error("Manual coldkeys refresh failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=12800)
