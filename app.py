from fastapi import FastAPI, Request, HTTPException, Body
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager, suppress
from typing import Optional, Any
import uvicorn
import logging
import os
import sys
import json
import asyncio
import shlex
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
INVESTING88_CACHE_SECONDS = int(os.getenv("INVESTING88_CACHE_SECONDS", "300"))
INVESTING88_SYNC_HOUR = int(os.getenv("INVESTING88_SYNC_HOUR", "19"))
INVESTING88_SYNC_INTERVAL_SECONDS = int(os.getenv("INVESTING88_SYNC_INTERVAL_SECONDS", "3600"))
INVESTING88_SYNC_TZ = os.getenv("INVESTING88_SYNC_TZ", "Asia/Tokyo")
UID_STATUS_CACHE_MAX = int(os.getenv("UID_STATUS_CACHE_MAX", "256"))
REFRESH_STATUS_SCRIPT = os.path.join(MONITOR_DIR, "miner_status_cache.py")
INVESTING88_SYNC_SCRIPT = os.path.join(SN88_MONITOR_DIR, "investing88_cache.py")
DASHBOARD_ROOT = os.path.dirname(__file__)
_fee_regime_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}
_reg_cost_cache: dict[int, dict[str, Any]] = {}
_investing88_cache: dict[int, dict[str, Any]] = {}


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_task = asyncio.create_task(_miner_status_refresh_worker())
    investing88_task = asyncio.create_task(_investing88_daily_sync_worker())

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
    with suppress(asyncio.CancelledError):
        await refresh_task
        await investing88_task


app = FastAPI(title="SN71 Session Manager", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# Mount static files (for favicon and other assets)
app.mount("/asset", StaticFiles(directory="asset"), name="asset")

# Homepage: Miners page (default first load)
@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    return templates.TemplateResponse("miners.html", {"request": request})

# Miners page (also at / for homepage)
@app.get("/miners", response_class=HTMLResponse)
async def miners_page(request: Request):
    return templates.TemplateResponse("miners.html", {"request": request})

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
MINER_PARAMS_REMOTE_DIR = os.getenv("MINER_PARAMS_REMOTE_DIR", "/root/79/sn-79")
MINER_PARAMS_RSYNC_USER = os.getenv("MINER_PARAMS_RSYNC_USER", "root")
MINER_PARAMS_RSYNC_SSH_OPTS = os.getenv(
    "MINER_PARAMS_RSYNC_SSH_OPTS",
    "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15",
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
    "95.216.27.87": "Server4",
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


def _save_miner_params_file(uid: int, data: dict) -> str:
    os.makedirs(MINER_PARAMS_DIR, exist_ok=True)
    path = _miner_params_path(uid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


async def _rsync_miner_params(uid: int, local_path: str, miner_ip: str) -> tuple[bool, str]:
    if not miner_ip:
        return False, "miner_ip is missing from params file"
    remote = f"{MINER_PARAMS_RSYNC_USER}@{miner_ip}:{MINER_PARAMS_REMOTE_DIR}/run_miner_{uid}.json"
    ssh_cmd = f"ssh {MINER_PARAMS_RSYNC_SSH_OPTS}"
    cmd = ["rsync", "-avz", "-e", ssh_cmd, local_path, remote]
    logger.info("Rsync miner params: %s", " ".join(shlex.quote(part) for part in cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        return False, "rsync timed out after 60 seconds"
    except FileNotFoundError:
        return False, "rsync command not found"

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        return False, err or f"rsync failed with exit code {proc.returncode}"

    msg = stdout.decode("utf-8", errors="replace").strip()
    return True, msg or f"Synced to {remote}"


def _display_param_keys_for_agent(agent_name: str) -> list[str]:
    keys = list(DISPLAY_PARAM_KEYS)
    if agent_name and "Eater" in agent_name and "eat_quantity" not in keys:
        keys.append("eat_quantity")
    return keys


def _read_miner_agent_summary(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        miner_ip = data.get("miner_ip") if isinstance(data.get("miner_ip"), str) else ""
        server = SERVER_NICKNAMES.get(miner_ip, miner_ip or None)
        agents = data.get("agents") or []
        agent_index = int(data.get("agent_index", 0))
        if 0 <= agent_index < len(agents):
            agent = agents[agent_index]
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
    if agent_index < 0 or agent_index >= len(agents):
        raise HTTPException(status_code=400, detail=f"agent_index out of range (0-{len(agents) - 1})")

    data["agent_index"] = agent_index

    new_params = payload.get("params")
    if not isinstance(new_params, dict):
        raise HTTPException(status_code=400, detail="params must be an object")

    current_params = agents[agent_index].get("params", {})
    if not isinstance(current_params, dict):
        raise HTTPException(status_code=400, detail="Active agent params must be an object")

    updated_params = dict(current_params)
    for key, value in new_params.items():
        if key not in current_params:
            raise HTTPException(status_code=400, detail=f"Unknown param key: {key}")
        updated_params[key] = _coerce_param_value(current_params[key], value)

    agents[agent_index]["params"] = updated_params
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
    prev_agent = (
        agents[previous_agent_index].get("name")
        if 0 <= previous_agent_index < len(agents)
        else None
    )
    new_agent = agents[agent_index].get("name")
    source = "manual" if agent_switched else "params_update"
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
        extra={"params": updated_params} if not agent_switched else None,
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

    try:
        maker, taker = await asyncio.to_thread(fetch_fee_rates)
        summary = build_fee_regime_summary(maker, taker)
        if summary.get("error"):
            raise HTTPException(status_code=502, detail=summary["error"])
        if not include_books:
            summary.pop("books", None)
        summary["updated_at"] = datetime.now(timezone.utc).isoformat()
        if not include_books:
            _fee_regime_cache["payload"] = summary
            _fee_regime_cache["expires_at"] = now + FEE_REGIME_CACHE_SECONDS
        return summary
    except HTTPException:
        raise
    except ImportError as e:
        logger.error("Fee regime module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Fee regime module unavailable: {e}")
    except Exception as e:
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
    from get_reg_cost import get_current_subnet_reg_cost

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
        result = await asyncio.to_thread(get_current_subnet_reg_cost, netuid)
        if result is None:
            raise HTTPException(status_code=502, detail=f"Failed to fetch reg cost for subnet {netuid}")
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


@app.get("/api/investing88/days")
async def get_investing88_days(request: Request):
    import asyncio
    import time

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    now = time.time()
    if not refresh:
        cached = _investing88_cache.get(-1)
        if cached and now < cached.get("expires_at", 0):
            return cached["payload"]

    try:
        from investing88_cache import load_cache_payload

        payload = await asyncio.to_thread(load_cache_payload)
        if not payload:
            raise HTTPException(status_code=502, detail="Investing88 cache is unavailable")
        scores: dict = {}
        for uid_str, miner in (payload.get("miners") or {}).items():
            score = miner.get("score") if isinstance(miner, dict) else None
            if not isinstance(score, dict):
                continue
            scores[uid_str] = {
                "asset": score.get("asset"),
                "days": score.get("days"),
                "last": score.get("last"),
                "score": score.get("score"),
                "value": score.get("value"),
                "lsr": score.get("lsr"),
                "mar": score.get("mar"),
                "risk_percent": score.get("risk_percent"),
                "odds_percent": score.get("odds_percent"),
                "daily_percent": score.get("daily_percent"),
                "yield_percent": score.get("yield_percent"),
            }
        result = {"days": payload.get("days", {}), "scores": scores}
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=12800)
