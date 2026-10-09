from fastapi import FastAPI, Request, HTTPException, Body, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from contextlib import asynccontextmanager, suppress
from typing import Optional, Any
from urllib.parse import quote
import uvicorn
import logging
import os
import sys
import json
import time
import hashlib
import asyncio
import shlex
import gzip
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
# How old the balance file must be before POST /api/regbot/balance/refresh kicks a
# background sweep. Deliberately NOT the same number as the worker interval above.
# When the two are equal the endpoint declares the file stale in the same instant
# the worker is about to rewrite it, so a page load landing just before a tick
# triggers a redundant sweep — the "a TTL equal to the producer's period is the
# worst possible value" trap, in its own codebase. One and a half periods means
# only a genuinely missed tick counts as stale.
REGBOT_BALANCE_STALE_SECONDS = int(
    os.getenv("REGBOT_BALANCE_STALE_SECONDS", str(REGBOT_BALANCE_CACHE_SECONDS * 3 // 2))
)
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
# When to call the HISTORY overdue. The candles are hourly, but they do not
# arrive hourly: upstream ships a whole day at once, so the newest candle is
# normally the previous day's 23:00 and the series runs 13-37h behind. Measured
# across three samples: 08-10 23:00 seen at both 08-11 04:39 (5.7h) and 08-11
# 23:29 (24.5h), then exactly 24 new candles by 08-12 12:15 (13.7h).
#
# 2h was the original value and was wrong — it read that normal daily cadence as
# a failure and would have kept the warning lit permanently, which is the same
# as having no warning. 30h is just past the top of the normal range, so this
# fires only when a daily drop is genuinely missed.
#
# Candle timestamps carry no timezone and are read as UTC for this one
# calculation; the daily boundary landing exactly on 23:00 is consistent with
# that. Note the header's headline price does NOT depend on any of this — it
# comes live from the chain (see renderAlphaPrice); this governs the sparkline's
# history only.
ALPHA_PRICE_STALE_AFTER_HOURS = float(os.getenv("ALPHA_PRICE_STALE_AFTER_HOURS", "30"))
# How often to refresh the warm SSH master connections to the miner boxes. Must be shorter
# than the SSH ControlPersist window (600s) so masters never expire between refreshes.
MINER_PARAMS_KEEPALIVE_SECONDS = int(os.getenv("MINER_PARAMS_KEEPALIVE_SECONDS", "300"))
SIM_ID_POLL_SECONDS = int(os.getenv("SIM_ID_POLL_SECONDS", "60"))
# Metagraph freshness is owned by a worker, not by whoever happens to request it.
# Cold load measured at 2.9s vs 13ms warm, so a request that triggers the chain
# subprocess is the single biggest cliff on a page load. Refresh comfortably
# inside METAGRAPH_CACHE_TTL so a warm copy never lapses in normal operation.
METAGRAPH_REFRESH_SECONDS = int(os.getenv("METAGRAPH_REFRESH_SECONDS", "90"))
# The page reads netuid 79 directly and shows a header emission figure for 71/88.
METAGRAPH_WARM_NETUIDS = [
    int(x) for x in os.getenv("METAGRAPH_WARM_NETUIDS", "79,71,88").split(",") if x.strip()
]
# Pause between warming one netuid and the next. Was 2s, to stop three concurrent
# child processes each opening a chain connection. chain_daemon.py reuses one
# connection for all three, so the burst it guarded against no longer exists on
# the normal path; the gap stays small rather than zero for the fallback path,
# where each netuid really is a fresh subprocess again.
METAGRAPH_WARM_GAP_SECONDS = float(os.getenv("METAGRAPH_WARM_GAP_SECONDS", "0.5"))
# Same idea for the RegBot active-run scan: ~690ms of SSH fan-out across all 12
# presets, polled by every open tab. Run it once here instead, so the cost stops
# scaling with the number of dashboards open.
REGBOT_ACTIVE_SCAN_SECONDS = int(os.getenv("REGBOT_ACTIVE_SCAN_SECONDS", "20"))
# ...and when there is nothing to watch (no active run, no browser subscribed to
# the event stream) the same scan drops to this. 12 SSH probes every 20s forever
# is ~2,160 connections an hour spent confirming that nothing is happening.
REGBOT_ACTIVE_SCAN_IDLE_SECONDS = int(os.getenv("REGBOT_ACTIVE_SCAN_IDLE_SECONDS", "120"))
# Two more upstreams that were still request-driven. Both have a 300s TTL and the
# page reloads every 300s, so *every* automatic refresh landed exactly on expiry
# and paid the full cost: measured 2.24s for the TAOS fee-regime query and 2.13s
# for the reg-cost chain query, which is what a browser reports as 3-4s of
# "waiting for server response". Refresh under the TTL so a warm copy never lapses.
FEE_REGIME_REFRESH_SECONDS = int(os.getenv("FEE_REGIME_REFRESH_SECONDS", "240"))
REG_COST_REFRESH_SECONDS = int(os.getenv("REG_COST_REFRESH_SECONDS", "240"))
# Used instead of the above when chain_daemon.py is serving reg cost from its
# block-paced poller: that is a ~1ms socket read with no chain query behind it, so
# staying 240s stale would be pointless. One block is 12s; refreshing a little
# faster keeps the displayed cost within a block of the chain for free.
REG_COST_REFRESH_FAST_SECONDS = int(os.getenv("REG_COST_REFRESH_FAST_SECONDS", "10"))
# A forced refresh (?refresh=1) landing within one block of the last successful
# read cannot return a different answer — the chain has not produced new state —
# so it is pure latency: ~2.9s for the metagraph, ~2.2s for reg cost. This window
# collapses a double-click, several tabs hitting Refresh together, and a reload
# followed immediately by the button. It never serves data from an older block.
CHAIN_BLOCK_SECONDS = int(os.getenv("CHAIN_BLOCK_SECONDS", "12"))
REG_COST_WARM_NETUIDS = [
    int(x) for x in os.getenv("REG_COST_WARM_NETUIDS", "79,88").split(",") if x.strip()
]
# Both of these are keyed on an exact change signal (a file mtime, a directory
# fingerprint), not on elapsed time, so the TTL is only a long backstop. A short
# TTL here was actively harmful: it threw away a valid result every 30s and made
# the next caller pay the full rebuild — measured 718ms for the uid-metrics read
# and ~100ms for the 200-file params scan, for data that changes every 900s.
MINER_PARAMS_CACHE_SECONDS = float(os.getenv("MINER_PARAMS_CACHE_SECONDS", "3600"))
UID_METRICS_CACHE_SECONDS = float(os.getenv("UID_METRICS_CACHE_SECONDS", "3600"))
FEE_HISTORY_CACHE_SECONDS = float(os.getenv("FEE_HISTORY_CACHE_SECONDS", "60"))
# The fee charts are a few hundred CSS px wide; a day of raw snapshots was 476KB,
# roughly an order of magnitude more points than the chart has pixels.
FEE_HISTORY_MAX_POINTS = int(os.getenv("FEE_HISTORY_MAX_POINTS", "600"))
# @sn79_sim_id_bot, posting to the "SN79 Sim ID Alert" channel it's admin in.
SIM_ID_TELEGRAM_BOT_TOKEN = os.getenv(
    "SIM_ID_TELEGRAM_BOT_TOKEN", "8453130208:AAF4awFThe9zakRRlIH5-Ov2fki-55BNJBk"
)
SIM_ID_TELEGRAM_CHAT_ID = os.getenv("SIM_ID_TELEGRAM_CHAT_ID", "-1004372773110")
REFRESH_STATUS_SCRIPT = os.path.join(MONITOR_DIR, "miner_status_cache.py")
INVESTING88_SYNC_SCRIPT = os.path.join(SN88_MONITOR_DIR, "investing88_cache.py")
DASHBOARD_ROOT = os.path.dirname(__file__)
_fee_regime_cache: dict[str, Any] = {
    "payload": None,     # summary without `books`, what the panel asks for
    "books": None,       # per-book rates, computed by the same fetch and kept
    "expires_at": 0.0,
}
_reg_cost_cache: dict[int, dict[str, Any]] = {}
_investing88_cache: dict[int, dict[str, Any]] = {}
_sim_id_state: dict[str, Any] = {"last": None}
# netuid -> {"mtime": float, "rows": [(timestamp, high, low, close), ...]}. Keyed on
# mtime because collect_prices.py rewrites the file hourly; re-parsing 9k rows of
# JSON on every poll of an always-visible header widget is not worth it.
_alpha_price_cache: dict[int, dict[str, Any]] = {}


# ---- Serialised-response cache, ETags, and off-loop work --------------------
#
# The dashboard is ONE fork-mode uvicorn process (ecosystem.config.json), so every
# handler shares a single event loop. A handler that spends 135ms in json.dumps
# does not slow itself down — it stalls every other in-flight request by 135ms.
#
# Measured on a page load (docs/sn79/performance/page_load_audit.md): six endpoints
# costing ~1ms each in isolation were all reporting ~270ms of server time, purely
# because they were queued behind fee-regime/history (135ms), miner-params (99ms)
# and uid-metrics (44ms) serialising.
#
# So the hot read paths cache the *serialised bytes*, not the dict, and return them
# with a weak ETag. An unchanged re-poll then costs a 304 — no body, no gzip pass.


def _dumps(payload: Any) -> bytes:
    """Same json.dumps settings FastAPI's JSONResponse uses, so a handler switched
    to the byte-cache path produces byte-identical output. `default=str` is a
    safety net for the datetimes jsonable_encoder used to absorb."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _serialize_json(payload: Any, etag_over: Any = None) -> tuple[bytes, str]:
    """Serialise once; derive a **weak** ETag.

    `etag_over` exists because a "when did we last look" timestamp in the body
    changes on every worker tick even when the answer is identical, which defeats
    revalidation entirely — measured: `/api/regbot/active` re-sent its 353-byte
    payload on every poll purely because of its own `updated_at`. Passing the
    payload minus that field makes the tag track *the data*, not the clock.

    A weak validator is exactly the right tool: `W/` means "semantically
    equivalent", not "byte-identical", so a 304 that leaves the client holding a
    slightly older `updated_at` is correct by the spec, not a fudge.
    """
    body = _dumps(payload)
    basis = body if etag_over is None else _dumps(etag_over)
    return body, 'W/"%s"' % hashlib.blake2b(basis, digest_size=12).hexdigest()


def _etag_matches(request: Request, etag: str) -> bool:
    """If-None-Match is a comma-separated list, even though fetch() sends one tag."""
    header = request.headers.get("if-none-match", "")
    return bool(header) and etag in (tag.strip() for tag in header.split(","))


# ---- Pre-compressed bodies --------------------------------------------------
#
# The byte caches above exist so a hot endpoint never re-serialises. But every 200
# then handed those same bytes to GZipMiddleware, which compressed them again from
# scratch, in the event loop, at level 9. Measured per response:
#
#     /js/miners.js        16.7 ms    241 KB -> 58 KB
#     /api/metagraph-data   3.9 ms    151 KB -> 27 KB
#     /miners               5.4 ms    112 KB -> 15 KB
#
# Compressing once per distinct payload and keeping the result is the natural
# completion of "cache the bytes, not the object". Keyed by ETag, which already
# identifies a payload exactly, so an entry can never go stale: new data means a
# new tag means a new entry.
#
# Level 6 rather than 9: measured +0.7% on miners.js and +1.9% on the metagraph
# for 2.5x less CPU on the miss that fills the cache. Nobody notices 400 extra
# bytes; a 17 ms loop stall on a cold load is the thing worth removing.
_GZIP_LEVEL = int(os.getenv("GZIP_LEVEL", "6"))
# Matches the GZipMiddleware minimum_size below: under this, framing overhead is
# most of the payload and the round trip is dominated by latency anyway.
_GZIP_MIN_SIZE = 1000
_GZIP_CACHE_CAP = 64
_gzip_cache: dict[str, bytes] = {}


def _gzipped(body: bytes, etag: str) -> bytes:
    hit = _gzip_cache.get(etag)
    if hit is None:
        hit = gzip.compress(body, _GZIP_LEVEL)
        if len(_gzip_cache) >= _GZIP_CACHE_CAP:
            _gzip_cache.clear()
        _gzip_cache[etag] = hit
    return hit


def _maybe_gzip(request: Request, body: bytes, etag: str, headers: dict) -> bytes:
    """Swap in the pre-compressed body when the client accepts gzip.

    Starlette's GZipMiddleware skips any response that already carries a
    Content-Encoding, so setting it here means the body passes through untouched
    rather than being compressed twice.

    It also skips adding `Vary` on that pass-through path, so we set it ourselves:
    without it a shared cache could hand these gzipped bytes to a client that
    never asked for them.
    """
    if len(body) < _GZIP_MIN_SIZE or "gzip" not in request.headers.get("accept-encoding", ""):
        return body
    headers["Content-Encoding"] = "gzip"
    headers["Vary"] = "Accept-Encoding"
    return _gzipped(body, etag)


def _json_bytes_response(
    request: Request, body: bytes, etag: str, cache_state: str = "hit"
) -> Response:
    """Hand back pre-serialised JSON, or a 304 if the client already has it.

    `Cache-Control: no-cache` means "store it, but revalidate" — which is exactly
    what turns a repeat poll into an If-None-Match round trip with no body.
    X-Cache is for operators reading curl -I; nothing in the UI depends on it.
    """
    headers = {"ETag": etag, "Cache-Control": "no-cache", "X-Cache": cache_state}
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    body = _maybe_gzip(request, body, etag, headers)
    return Response(content=body, media_type="application/json", headers=headers)


class _ByteCache:
    """(bytes, etag) keyed by request shape, with a TTL and explicit invalidation.

    Deliberately not an LRU: the key spaces here are small and bounded by what the
    UI actually asks for. The cap only exists so a caller passing arbitrary query
    combinations can't grow it without limit.
    """

    def __init__(self, ttl: float, cap: int = 32) -> None:
        self.ttl = ttl
        self.cap = cap
        self._entries: dict[Any, tuple[float, bytes, str]] = {}

    def get(self, key: Any = None) -> Optional[tuple[bytes, str]]:
        entry = self._entries.get(key)
        if not entry or (time.time() - entry[0]) >= self.ttl:
            return None
        return entry[1], entry[2]

    def put(self, key: Any, payload: Any) -> tuple[bytes, str]:
        body, etag = _serialize_json(payload)
        if len(self._entries) >= self.cap:
            self._entries.clear()
        self._entries[key] = (time.time(), body, etag)
        return body, etag

    def invalidate(self) -> None:
        self._entries.clear()


# ---- Change notifications (Server-Sent Events) -----------------------------
#
# The workers already know the instant a payload changes; the browser was finding
# out up to 20s later by asking. So they publish a one-line "topic X is now at
# etag Y" and the client re-fetches *that* endpoint conditionally.
#
# Push the notification, pull the data. The alternative — pushing payloads down
# the stream — would mean a second serialisation path per topic, per-connection
# state, and no ETag/gzip on the hot bytes. This way the existing cached-byte
# handlers stay the single source of truth and a notification costs ~60 bytes.
#
# SSE rather than WebSocket on purpose: this traffic is strictly one-directional
# (every write is already an ordinary POST), and SSE gets auto-reconnect, plain
# HTTP, and the existing session cookie for free with no framing library.
_sse_subscribers: set[asyncio.Queue] = set()
SSE_HEARTBEAT_SECONDS = float(os.getenv("SSE_HEARTBEAT_SECONDS", "20"))
# Per-subscriber bound. A client too slow to drain 32 notifications is not going
# to be rescued by buffering more; drop it and let EventSource reconnect, which
# re-syncs via the conditional fetch anyway.
SSE_QUEUE_MAX = 32
_sse_topic_etags: dict[str, str] = {}


def _publish(topic: str, etag: str) -> None:
    """Announce that `topic` changed. Safe to call from any worker; never raises."""
    _sse_topic_etags[topic] = etag
    if not _sse_subscribers:
        return
    message = {"topic": topic, "etag": etag}
    for queue in list(_sse_subscribers):
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop the slow subscriber rather than let it back up the workers.
            _sse_subscribers.discard(queue)


async def _swallow(coro) -> None:
    """Await a background refresh without letting its failure escape as a task
    exception — the caller has already been served a stale-but-real value."""
    try:
        await coro
    except Exception as exc:
        logger.warning("Background refresh failed: %s", exc)


def _downsample(points: list, max_points: int) -> list:
    """At most `max_points`, evenly spaced, always keeping the newest one.

    The last snapshot is what the "current" readouts key off, so it has to survive;
    a plain stride drops it whenever len(points) % stride != 1.
    """
    n = len(points)
    if max_points <= 0 or n <= max_points:
        return points
    step = n / max_points
    picked = [points[int(i * step)] for i in range(max_points)]
    picked[-1] = points[-1]
    return picked


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
    """Rebuild the daily-status files on a fixed cadence.

    Deadline-scheduled: _run_status_refresh_subprocess() is an SSH-driven sweep
    with no upper bound on its runtime, so sleeping a flat interval *after* it
    made the real period `interval + however long the sweep took` and let it drift
    indefinitely. As a deadline it holds MINER_STATUS_REFRESH_SECONDS until the
    work itself exceeds that, then simply runs back-to-back instead of falling
    further behind every cycle.
    """
    while True:
        started = time.time()
        try:
            await _run_status_refresh_subprocess()
            # The daily-status file has just been rewritten, so the byte cache is
            # stale by definition and every open tab wants to know.
            _uid_metrics_cache.invalidate()
            _publish("uid_metrics", "refreshed:%d" % int(time.time()))
        except Exception as exc:
            logger.error("Miner status cache refresh failed: %s", exc)
        await _sleep_until_next_tick(started, MINER_STATUS_REFRESH_SECONDS)


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
    # Sleeps first (nothing to sync at boot), then on a deadline: the sync itself
    # can run for many minutes, and a flat sleep after it would push each run
    # later than the last.
    await asyncio.sleep(INVESTING88_SYNC_INTERVAL_SECONDS)
    while True:
        started = time.time()
        logger.info("Investing88 hourly sync starting (interval=%ss)", INVESTING88_SYNC_INTERVAL_SECONDS)
        try:
            await _run_investing88_sync_subprocess()
        except Exception as exc:
            logger.error("Investing88 cache sync failed: %s", exc)
        await _sleep_until_next_tick(started, INVESTING88_SYNC_INTERVAL_SECONDS)


_regbot_balance_inflight: dict[str, "asyncio.Task"] = {}


async def _refresh_regbot_balances() -> dict:
    """Single-flight around regbot.refresh_balance_cache().

    Without it, the worker and every open tab that hits
    POST /api/regbot/balance/refresh each spawn their own chain_cli, and each of
    those opens its own substrate connection. That burst — several metadata
    decodes at once — is the documented worst source of latency on this service.
    Shielded so a browser hanging up mid-request cannot cancel a sweep the worker
    and other tabs are waiting on.
    """
    from regbot import refresh_balance_cache

    task = _regbot_balance_inflight.get("all")
    if task is None or task.done():
        task = asyncio.create_task(refresh_balance_cache())
        _regbot_balance_inflight["all"] = task
    return await asyncio.shield(task)


def _balance_cache_age() -> float:
    """Seconds since the balance cache file was last written, or inf if absent."""
    from regbot import BALANCE_CACHE_PATH

    try:
        return time.time() - os.path.getmtime(BALANCE_CACHE_PATH)
    except OSError:
        return float("inf")


async def _regbot_balance_refresh_worker() -> None:
    """Refresh every RegBot coldkey's balance to a file every
    REGBOT_BALANCE_CACHE_SECONDS, so /api/regbot/balance reads are served
    from disk instead of blocking on a live chain call per request."""
    while True:
        started = time.time()
        try:
            await _refresh_regbot_balances()
        except Exception as exc:
            logger.error("RegBot balance cache refresh failed: %s", exc)
        await _sleep_until_next_tick(started, REGBOT_BALANCE_CACHE_SECONDS)


async def _coldkeys_watch_worker() -> None:
    """Collects netuid-79 coldkeys/IPs every COLDKEYS_WATCH_INTERVAL_SECONDS
    and diffs against the previous run, so the dashboard can flag ones seen
    for the first time."""
    from coldkeys_watch import run_collect_and_diff

    while True:
        started = time.time()
        try:
            await run_collect_and_diff()
        except Exception as exc:
            logger.error("Coldkey/IP watch run failed: %s", exc)
        await _sleep_until_next_tick(started, COLDKEYS_WATCH_INTERVAL_SECONDS)


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
        started = time.time()
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
        await _sleep_until_next_tick(started, SIM_ID_POLL_SECONDS)


# Last result of the RegBot active-run scan: {"payload", "updated_at", "body", "etag"}.
_regbot_active_state: dict[str, Any] = {
    "payload": None, "updated_at": 0.0, "body": None, "etag": None,
}


async def _store_regbot_active(payload: dict) -> tuple[bytes, str]:
    # The ETag deliberately ignores `updated_at`: the scan runs every 20s and the
    # answer is usually identical, so tagging the clock would mean re-sending the
    # whole payload forever. Tag the scan result instead.
    scan = {k: v for k, v in payload.items() if k != "updated_at"}
    payload["updated_at"] = time.time()
    body, etag = _serialize_json(payload, etag_over=scan)
    changed = etag != _regbot_active_state.get("etag")
    _regbot_active_state.update(
        payload=payload, updated_at=payload["updated_at"], body=body, etag=etag
    )
    if changed:
        _publish("regbot_active", etag)
    return body, etag


async def _regbot_active_scan_worker() -> None:
    """Own the active-run scan so the request path never fans out over SSH.

    get_active_runs() probes all 12 SERVER_PRESETS for live .pid files and tails a
    log per running job — measured ~690ms, and the browser polled it every 20s per
    open tab. Doing it here once makes the cost independent of how many dashboards
    are open, and takes the longest request on a page load down to a memory read.

    Errors keep the previous result rather than blanking the bar: a momentarily
    unreachable box should not look like "no runs are active".

    IDLE BACKOFF. At 20s this is the highest-frequency remote work in the service:
    12 SSH probes every 20s is ~2,160 connections an hour, run forever whether or
    not anything is happening. It only needs to be fast when there is something to
    watch, so when no run is active AND no browser is subscribed to the event
    stream, it drops to REGBOT_ACTIVE_SCAN_IDLE_SECONDS. Either condition
    returning restores the fast cadence on the very next tick — and a run started
    through this dashboard publishes immediately anyway, so the bar never waits on
    a scan to appear.
    """
    from regbot import get_active_runs

    while True:
        started = time.time()
        try:
            await _store_regbot_active(await get_active_runs())
        except Exception as exc:
            logger.error("RegBot active-run scan failed: %s", exc)
        active = ((_regbot_active_state.get("payload") or {}).get("active")) or []
        idle = not active and not _sse_subscribers
        await _sleep_until_next_tick(
            started,
            REGBOT_ACTIVE_SCAN_IDLE_SECONDS if idle else REGBOT_ACTIVE_SCAN_SECONDS,
        )


_fee_regime_inflight: dict[str, "asyncio.Task"] = {}
_reg_cost_inflight: dict[int, "asyncio.Task"] = {}


async def _compute_fee_regime(include_books: bool = False) -> dict:
    """Fetch the live fee rates and build the regime summary, populating the cache.

    Single-flight via _refresh_fee_regime; never call this directly from a request.
    """
    from datetime import datetime, timezone
    from fee_rate import build_fee_regime_summary, fetch_fee_rates

    maker, taker = await asyncio.to_thread(fetch_fee_rates)
    summary = build_fee_regime_summary(maker, taker)
    if summary.get("error"):
        raise RuntimeError(summary["error"])
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Cache BOTH shapes. fetch_fee_rates() is the entire ~2.8s cost and it already
    # returns per-book maker/taker rates; build_fee_regime_summary() derives
    # `books` from them with local arithmetic and no further upstream call. The
    # cache used to drop that key, so the worker paid for the books every 240s,
    # deleted them, and the next click of the Books toggle spent another 2.8s
    # regenerating 8.4KB that had been in memory moments earlier. Keeping them
    # costs nothing and makes that click a cache hit.
    _fee_regime_cache["payload"] = {k: v for k, v in summary.items() if k != "books"}
    _fee_regime_cache["books"] = summary.get("books")
    _fee_regime_cache["loaded_at"] = time.time()
    _fee_regime_cache["expires_at"] = time.time() + FEE_REGIME_CACHE_SECONDS
    if not include_books:
        summary.pop("books", None)
    return summary


async def _refresh_fee_regime(include_books: bool = False) -> dict:
    key = "books" if include_books else "summary"
    task = _fee_regime_inflight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_compute_fee_regime(include_books))
        _fee_regime_inflight[key] = task
    return await asyncio.shield(task)


async def _sleep_until_next_tick(started: float, interval: float) -> None:
    """Sleep the remainder of `interval`, counting from when the tick started.

    Sleeping a flat `interval` *after* the work makes the real cadence
    interval + work, which is not what any of the "refresh faster than the TTL"
    reasoning assumes. Measured on the metagraph worker: three netuids at ~3s each
    plus 2s gaps turned a nominal 90s into 105s against a 120s TTL — a 15s margin
    that a slow chain would erase, at which point every copy reads as stale. As a
    deadline it stays 90s until the work itself exceeds that, and then it simply
    runs continuously instead of falling further behind every cycle.
    """
    await asyncio.sleep(max(0.0, interval - (time.time() - started)))


async def _fee_regime_refresh_worker() -> None:
    """Own fee-regime freshness so a page load never waits on TAOS."""
    while True:
        started = time.time()
        try:
            await _refresh_fee_regime()
        except Exception as exc:
            logger.warning("Fee regime warm refresh failed: %s", exc)
        await _sleep_until_next_tick(started, FEE_REGIME_REFRESH_SECONDS)


async def _compute_reg_cost(netuid: int) -> dict:
    """Registration cost for one subnet, out of process. See the note in the
    endpoint: this is ~2s of pure-Python substrate decode and must not run in a
    thread, where it would hold the GIL and stall every other caller."""
    from datetime import datetime, timezone
    from regbot import run_chain_cli

    payload_cli = await run_chain_cli(["reg-cost", str(netuid)], timeout=120.0)
    result = payload_cli.get("result") if payload_cli.get("ok") else None
    if result is None:
        raise RuntimeError(payload_cli.get("error") or "no result")
    payload = {**result, "updated_at": datetime.now(timezone.utc).isoformat()}
    _reg_cost_cache[netuid] = {
        "payload": payload, "loaded_at": time.time(),
        "expires_at": time.time() + REG_COST_CACHE_SECONDS,
        # "poller" means chain_daemon.py answered from its block-paced loop, i.e.
        # this refresh cost a socket round-trip and no chain query at all. The
        # worker below reads it to decide how often it can afford to run.
        "source": payload_cli.get("source"),
    }
    return payload


async def _refresh_reg_cost(netuid: int) -> dict:
    task = _reg_cost_inflight.get(netuid)
    if task is None or task.done():
        task = asyncio.create_task(_compute_reg_cost(netuid))
        _reg_cost_inflight[netuid] = task
    return await asyncio.shield(task)


async def _reg_cost_refresh_worker() -> None:
    """Keep the subnets the UI shows warm, as often as it is cheap to do so.

    Two cadences, chosen by where the answer came from:

    - chain_daemon.py answered from its block-paced poller. That is a Unix socket
      round-trip, ~1ms, and no chain query — the daemon polls once no matter how
      often we ask. So there is no reason to be 240s stale: refreshing every
      REG_COST_REFRESH_FAST_SECONDS makes the displayed cost effectively
      block-fresh at zero chain cost.
    - the daemon is down and this fell back to a child process. That is the old
      2.1s connect-and-query, and running it every 15s would be far worse than
      the 240s it used to be. So the slow cadence is kept for that case.

    Getting this backwards is the failure mode the fast path invites, which is why
    the interval is derived from the recorded source rather than assumed.
    """
    while True:
        started = time.time()
        via_daemon = True
        for netuid in REG_COST_WARM_NETUIDS:
            try:
                await _refresh_reg_cost(netuid)
            except Exception as exc:
                logger.warning("Reg cost warm refresh failed for netuid=%s: %s", netuid, exc)
                via_daemon = False
            if (_reg_cost_cache.get(netuid) or {}).get("source") != "poller":
                via_daemon = False
            if not via_daemon:
                await asyncio.sleep(2)
        await _sleep_until_next_tick(
            started,
            REG_COST_REFRESH_FAST_SECONDS if via_daemon else REG_COST_REFRESH_SECONDS,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Synchronously, before the first request is accepted: an async warm is not
    # enough, because a visitor landing during the fan-out just waits it out.
    _seed_metagraph_from_disk()
    refresh_task = asyncio.create_task(_miner_status_refresh_worker())
    investing88_task = asyncio.create_task(_investing88_daily_sync_worker())
    sim_id_task = asyncio.create_task(_sim_id_watch_worker())
    keepalive_task = asyncio.create_task(_miner_master_keepalive_worker())
    regbot_balance_task = asyncio.create_task(_regbot_balance_refresh_worker())
    coldkeys_watch_task = asyncio.create_task(_coldkeys_watch_worker())
    metagraph_task = asyncio.create_task(_metagraph_refresh_worker())
    regbot_active_task = asyncio.create_task(_regbot_active_scan_worker())
    fee_regime_task = asyncio.create_task(_fee_regime_refresh_worker())
    reg_cost_task = asyncio.create_task(_reg_cost_refresh_worker())

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
    metagraph_task.cancel()
    regbot_active_task.cancel()
    fee_regime_task.cancel()
    reg_cost_task.cancel()
    with suppress(asyncio.CancelledError):
        await refresh_task
        await investing88_task
        await sim_id_task
        await keepalive_task
        await regbot_balance_task
        await coldkeys_watch_task
        await metagraph_task
        await regbot_active_task
        await fee_regime_task
        await reg_cost_task


app = FastAPI(title="SN71 Session Manager", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# Nothing sits in front of port 12800 — uvicorn is the edge — so compression has to
# happen here or not at all. Measured on one page load: 1659KB of HTML+JSON becomes
# ~290KB (-82%), which is ~1.4s off the load over an 8 Mbit link. Added before the
# auth middleware below, and Starlette runs middleware outermost-last-added, so the
# order ends up auth_gate -> gzip -> routes.
app.add_middleware(GZipMiddleware, minimum_size=1000)

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


# miners.html contains no Jinja tags at all, so "rendering" it was reading a file
# and running a template engine over it for nothing, 373KB at a time, on every
# navigation. Read once per mtime and let the browser revalidate with an ETag: an
# unchanged page then costs a 304 instead of the whole document.
_static_page_cache: dict[str, tuple[float, bytes, str]] = {}


def _static_page(name: str) -> tuple[bytes, str]:
    path = os.path.join(DASHBOARD_ROOT, "templates", name)
    mtime = os.path.getmtime(path)
    hit = _static_page_cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    with open(path, "rb") as f:
        body = f.read()
    etag = 'W/"%s"' % hashlib.blake2b(body, digest_size=12).hexdigest()
    _static_page_cache[name] = (mtime, body, etag)
    return body, etag


def _html_page_response(request: Request, name: str) -> Response:
    body, etag = _static_page(name)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    body = _maybe_gzip(request, body, etag, headers)
    return Response(content=body, media_type="text/html; charset=utf-8", headers=headers)


@app.get("/js/miners.js")
async def miners_js(request: Request):
    """The page's ~7300 lines of JS, split out of miners.html so it can be cached.

    Not under the /asset StaticFiles mount on purpose: that mount sends no
    Cache-Control, which leaves browsers free to heuristically cache a stale copy
    of the app after a deploy. Serving it here gives an mtime-keyed ETag and
    explicit revalidation — correct across deploys, and still ~0 bytes on a repeat
    load.
    """
    path = os.path.join(DASHBOARD_ROOT, "asset", "miners.js")
    mtime = os.path.getmtime(path)
    hit = _static_page_cache.get("miners.js")
    if not hit or hit[0] != mtime:
        with open(path, "rb") as f:
            body = f.read()
        hit = (mtime, body, 'W/"%s"' % hashlib.blake2b(body, digest_size=12).hexdigest())
        _static_page_cache["miners.js"] = hit
    headers = {"ETag": hit[2], "Cache-Control": "no-cache"}
    if _etag_matches(request, hit[2]):
        return Response(status_code=304, headers=headers)
    body = _maybe_gzip(request, hit[1], hit[2], headers)
    return Response(
        content=body, media_type="application/javascript; charset=utf-8", headers=headers
    )


# Homepage: Miners page (default first load)
@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    return _html_page_response(request, "miners.html")

# Miners page (also at / for homepage)
@app.get("/miners", response_class=HTMLResponse)
async def miners_page(request: Request):
    return _html_page_response(request, "miners.html")

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
    "65.109.34.53": "Server_1",
    "65.109.87.169": "Server3",
    "84.32.34.89": "server_14",
    "65.109.69.29": "Server_2",
    "88.216.183.132": "Server_7",
    "84.32.131.131": "Server_9"
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
    # The /api/miner-params roster is cached as serialised bytes; every write to a
    # params file has to drop it, or the agent column shows the pre-edit value for
    # up to MINER_PARAMS_CACHE_SECONDS after a successful save.
    _miner_params_cache.invalidate()
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


def _ssh_hosts_to_keep_warm() -> set[str]:
    """Every box the dashboard talks to: one SSH master each, shared by everything.

    The union of two lists that are usually — but not necessarily — the same 12
    machines: the IPs in the params files, and regbot's SERVER_PRESETS. Deriving
    it from the params files alone meant a preset with no params yet (a freshly
    provisioned box) never got a master, so every RegBot probe to it paid a full
    cold handshake. Presets are the authoritative list of servers; params IPs
    cover anything registered outside them.
    """
    hosts = _miner_ips()
    try:
        from regbot import SERVER_PRESETS

        for preset in SERVER_PRESETS:
            ip = (preset.get("ip") or "").strip()
            if ip:
                hosts.add(ip)
    except Exception as exc:  # noqa: BLE001 - a missing preset list must not stop the warm
        logger.warning("Could not read SERVER_PRESETS for SSH keepalive: %s", exc)
    return hosts


async def _miner_master_keepalive_worker() -> None:
    """Keep one SSH master connection warm per server, shared by every caller.

    All the SSH in this service — params pushes, RegBot launches and probes, the
    active-run scan, unstake runs — goes through the same ControlPath
    (/tmp/.mp-cm-%C), so there is exactly ONE connection per box and whoever needs
    it next reuses it. That is the whole point: a cold handshake to the overseas
    boxes is ~10-13 round-trips at 125-160ms RTT, i.e. 2-3s, and `get_active_runs`
    alone fans out one ssh per server every REGBOT_ACTIVE_SCAN_SECONDS.

    ControlPersist only keeps a master alive for a while after its last use, so
    this worker touches every host a little more often than that window, and a
    master that has died is simply reopened. Best-effort: an unreachable box just
    fails its refresh, and its real commands would have failed anyway.

    Deadline-scheduled — the warm itself takes as long as the slowest box.
    """
    while True:
        started = time.time()
        try:
            hosts = await asyncio.to_thread(_ssh_hosts_to_keep_warm)
            if hosts:
                await asyncio.gather(*(_warm_one_master(ip) for ip in hosts))
                logger.info("Warmed SSH masters to %d box(es)", len(hosts))
        except Exception as exc:
            logger.warning("Miner SSH master keepalive failed: %s", exc)
        await _sleep_until_next_tick(started, MINER_PARAMS_KEEPALIVE_SECONDS)


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


_miner_params_cache = _ByteCache(MINER_PARAMS_CACHE_SECONDS)


def _miner_params_fingerprint() -> tuple:
    """(name, mtime, size) for every params file, from a stat-only pass.

    ~200 files: scandir with entry.stat() is about a millisecond, against ~100ms
    to open and parse them all. The directory is also written from outside this
    process (RegBot pulls, the auto-switcher), so an mtime on the directory alone
    would miss an in-place edit and _save_miner_params_file's invalidation would
    not cover it either.
    """
    if not os.path.isdir(MINER_PARAMS_DIR):
        return ()
    out = []
    with os.scandir(MINER_PARAMS_DIR) as entries:
        for e in entries:
            if not (e.name.startswith("run_miner_") and e.name.endswith(".json")):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            out.append((e.name, st.st_mtime, st.st_size))
    out.sort()
    return tuple(out)


def _scan_miner_params() -> dict:
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


@app.get("/api/miner-params")
async def list_miner_params(request: Request):
    """Roster of owned UIDs and their current agent, from the params directory.

    ~200 files of readdir+open+parse, measured at 99ms — which on a single event
    loop is 99ms of stall for every other in-flight request, and the page fetches
    this on every load. So the scan runs in a thread and the result is served from
    a byte cache that _save_miner_params_file() invalidates on every write.
    """
    fingerprint = await run_in_threadpool(_miner_params_fingerprint)
    hit = _miner_params_cache.get(fingerprint)
    if hit:
        return _json_bytes_response(request, hit[0], hit[1])
    payload = await run_in_threadpool(_scan_miner_params)
    body, etag = _miner_params_cache.put(fingerprint, payload)
    return _json_bytes_response(request, body, etag, "miss")


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
# netuid -> {"data", "body", "etag", "timestamp"}. The serialised bytes live here
# too: re-running json.dumps over a 155KB metagraph on every poll is ~10ms of
# event-loop stall for a payload that only changes every 90s.
_metagraph_cache: dict = {}
_metagraph_inflight: dict[int, "asyncio.Task"] = {}

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

async def _load_metagraph_subprocess(netuid: int) -> dict:
    """Run load_metagraph.py in a child process. The fallback path.

    Bittensor must never be imported into uvicorn (see
    docs/sn79/performance/chain_subprocess.md), so this stays a subprocess.
    """
    python_path = sys.executable or "python3"
    script_path = os.path.join(os.path.dirname(__file__), "load_metagraph.py")
    proc = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            python_path, script_path, str(netuid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ),
        timeout=5.0,  # 5 seconds to start the process
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90.0)
    if proc.returncode != 0:
        raise RuntimeError(
            stderr.decode("utf-8", errors="replace").strip()
            or f"load_metagraph.py exited {proc.returncode}"
        )
    return json.loads(stdout.decode("utf-8"))


async def _load_metagraph(netuid: int) -> dict:
    """Fetch one netuid's metagraph and cache dict + serialised bytes.

    Asks chain_daemon.py first: it holds one AsyncSubtensor open, so a sync is
    ~0.46s against ~2.97s for a fresh subprocess. Nearly all of that difference is
    setup the daemon has already paid — `import bittensor` 0.77s plus connect and
    metadata decode ~1.04s — not the sync itself, which is only ~0.4s either way.
    Three warm netuids per cycle therefore cost ~1.4s instead of 8.8s, over one
    chain connection instead of three.

    Falls back to the child process whenever the daemon cannot be reached, so
    stopping chaind costs latency and nothing else. Both paths build their payload
    with load_metagraph.build_payload(), so they cannot return different shapes.

    Do not call this directly from a request — go through _refresh_metagraph(),
    which makes concurrent callers share one run.
    """
    from chain_client import call as chain_daemon_call

    data = None
    payload = await chain_daemon_call("metagraph", [str(netuid)], timeout=120.0)
    if payload is not None and payload.get("ok"):
        data = payload.get("result")
    if data is None:
        if payload is not None:
            logger.warning(
                "chaind metagraph netuid=%s failed (%s); falling back to subprocess",
                netuid, payload.get("error"),
            )
        data = await _load_metagraph_subprocess(netuid)
    if data.get("error"):
        raise RuntimeError(data["error"])
    # Stamped into the payload rather than sent as a header so the client can tell
    # how old a snapshot is without needing to read response metadata.
    data["fetched_at"] = time.time()
    # The ETag ignores `fetched_at` for the same reason regbot/active's does: a
    # refresh that finds nothing new must not force a re-send. Between blocks the
    # identity half in particular is usually unchanged.
    body, etag = _serialize_json(data, etag_over={k: v for k, v in data.items() if k != "fetched_at"})
    previous = _metagraph_cache.get(netuid) or {}
    _metagraph_cache[netuid] = {
        "data": data, "body": body, "etag": etag, "timestamp": time.time(),
    }
    _save_metagraph_snapshot(netuid, data)
    if etag != previous.get("etag"):
        _publish("metagraph:%d" % netuid, etag)
    logger.info(
        "Metagraph netuid=%s loaded (%s neurons, %.0fKB)",
        netuid, data.get("totalNeurons", 0), len(body) / 1024,
    )
    return data


def _metagraph_snapshot_path(netuid: int) -> str:
    return os.path.join(DASHBOARD_ROOT, "miner_status", f"metagraph_{netuid}.json")


def _save_metagraph_snapshot(netuid: int, data: dict) -> None:
    """Persist the last good metagraph so a restart doesn't depend on the chain.

    Written after every successful load, atomically. Observed 2026-08-12: the
    finney entrypoint stopped accepting websocket connections entirely, and
    because the cache was in-process only, a `pm2 restart` during that window
    left the dashboard with nothing to render at all — every panel 502.
    """
    path = _metagraph_snapshot_path(netuid)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not persist metagraph snapshot for netuid=%s: %s", netuid, exc)


# How old a persisted snapshot may be and still be worth showing at startup. Well
# past this and it is misleading rather than useful; the payload carries
# `fetched_at` either way so the age is never hidden.
METAGRAPH_SNAPSHOT_MAX_AGE = float(os.getenv("METAGRAPH_SNAPSHOT_MAX_AGE", str(24 * 3600)))


def _seed_metagraph_from_disk() -> None:
    """Fill the cache from the last persisted snapshots, synchronously, before the
    first request. Entries are seeded with their *original* timestamp, so they
    read as stale immediately and the first request kicks a refresh behind itself
    rather than being served as fresh."""
    for netuid in METAGRAPH_WARM_NETUIDS:
        path = _metagraph_snapshot_path(netuid)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        fetched_at = data.get("fetched_at") or 0
        age = time.time() - fetched_at
        if age > METAGRAPH_SNAPSHOT_MAX_AGE:
            logger.info("Skipping metagraph snapshot for netuid=%s (%.1fh old)", netuid, age / 3600)
            continue
        body, etag = _serialize_json(data, etag_over={k: v for k, v in data.items() if k != "fetched_at"})
        _metagraph_cache[netuid] = {
            "data": data, "body": body, "etag": etag, "timestamp": fetched_at,
        }
        logger.info("Seeded metagraph netuid=%s from disk (%.0fs old)", netuid, age)


async def _refresh_metagraph(netuid: int) -> dict:
    """Single-flight around _load_metagraph.

    Without this, the warm worker and a cold request landing in the same window
    each spawn their own chain subprocess. Shielded so that a browser hanging up
    mid-request doesn't cancel a load other callers are waiting on.
    """
    task = _metagraph_inflight.get(netuid)
    if task is None or task.done():
        task = asyncio.create_task(_load_metagraph(netuid))
        _metagraph_inflight[netuid] = task
    return await asyncio.shield(task)


async def _metagraph_refresh_worker() -> None:
    """Own metagraph freshness so that no browser request ever has to.

    The previous design (TTL only, refreshed by whoever arrived first after it
    lapsed) meant one unlucky page load per 120s per netuid paid the full ~2.9s.

    Netuids are still refreshed one at a time rather than gathered, but the gap
    between them is now small. It existed because each netuid was its own child
    process opening its own chain connection, and three of those at once was the
    burst that made the balance sweep starve unrelated queries. Through
    chain_daemon.py all three share the one connection it already holds, so there
    is no burst to space out — the gap remains only to keep the fallback path
    (three real subprocesses) behaving as it did.

    Measured per cycle: 8.82s of chain work plus 4s of gaps, down to ~1.4s.
    """
    while True:
        started = time.time()
        for netuid in METAGRAPH_WARM_NETUIDS:
            try:
                await _refresh_metagraph(netuid)
            except Exception as exc:
                logger.warning("Metagraph warm refresh failed for netuid=%s: %s", netuid, exc)
            await asyncio.sleep(METAGRAPH_WARM_GAP_SECONDS)
        await _sleep_until_next_tick(started, METAGRAPH_REFRESH_SECONDS)


@app.get("/api/metagraph-data")
async def get_metagraph_data(request: Request):
    netuid = _parse_netuid(request.query_params.get("netuid"), 71)
    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    entry = _metagraph_cache.get(netuid)

    if entry and refresh and (time.time() - entry["timestamp"]) < CHAIN_BLOCK_SECONDS:
        # Same block, so a re-read is 2.9s to produce identical bytes.
        return _json_bytes_response(request, entry["body"], entry["etag"], "hit-coalesced")

    if entry and not refresh:
        if (time.time() - entry["timestamp"]) < METAGRAPH_CACHE_TTL:
            return _json_bytes_response(request, entry["body"], entry["etag"], "hit")
        # Stale-but-real beats making this one user wait 2.9s for the chain. Serve
        # what we have and refresh behind the request; `fetched_at` in the payload
        # says how old it is.
        asyncio.create_task(_swallow(_refresh_metagraph(netuid)))
        return _json_bytes_response(request, entry["body"], entry["etag"], "stale")

    try:
        await _refresh_metagraph(netuid)
    except asyncio.TimeoutError:
        logger.error("Metagraph load for netuid=%s exceeded 90s", netuid)
        raise HTTPException(status_code=504, detail="Metagraph loading timeout")
    except Exception as e:
        logger.error("Metagraph load failed for netuid=%s: %s", netuid, e)
        if entry:
            # A forced refresh that failed is not a reason to blank the page.
            return _json_bytes_response(request, entry["body"], entry["etag"], "stale-error")
        raise HTTPException(status_code=502, detail=str(e))

    entry = _metagraph_cache[netuid]
    return _json_bytes_response(request, entry["body"], entry["etag"], "miss")


@app.get("/api/subnet-emission")
async def get_subnet_emission(request: Request):
    """Total non-validator emission and alpha price for one subnet.

    The header's per-subnet emission figures used to be computed client-side from a
    *full* metagraph fetch per subnet — 144KB for netuid 71 plus 156KB for 88 on
    every page load, to render two numbers each. This reads the same warm cache and
    ships ~100 bytes.
    """
    netuid = _parse_netuid(request.query_params.get("netuid"), 79)
    entry = _metagraph_cache.get(netuid)
    if not entry:
        try:
            await _refresh_metagraph(netuid)
        except Exception as e:
            logger.error("Subnet emission load failed for netuid=%s: %s", netuid, e)
            raise HTTPException(status_code=502, detail=str(e))
        entry = _metagraph_cache[netuid]

    data = entry["data"]
    emissions = data.get("emissions") or {}
    is_validator = data.get("isValidator") or {}
    total = sum(
        em
        for hotkey, em in emissions.items()
        if not is_validator.get(hotkey) and isinstance(em, (int, float)) and em > 0
    )
    body, etag = _serialize_json({
        "netuid": netuid,
        "emission": total,
        "alphaPriceTao": data.get("alphaPriceTao"),
        "fetched_at": data.get("fetched_at"),
    })
    return _json_bytes_response(request, body, etag)


@app.get("/api/events")
async def sse_events(request: Request):
    """Change notifications. One line per changed topic; the client re-fetches.

    `Content-Encoding: identity` is load-bearing — Starlette's GZipMiddleware
    passes a response straight through when that header is already set, and
    without it the stream would sit in a gzip buffer waiting for enough bytes to
    be worth compressing, which for ~60-byte events is forever.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_MAX)
    _sse_subscribers.add(queue)

    async def stream():
        try:
            # Tell a reconnecting client what it may have missed while away, so it
            # can revalidate immediately instead of waiting for the next change.
            yield b"retry: 3000\n\n"
            for topic, etag in list(_sse_topic_etags.items()):
                yield b"event: change\ndata: %s\n\n" % _dumps({"topic": topic, "etag": etag})
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # A comment frame. Keeps the connection off any idle reaper and
                    # gives the client something to notice a dead link by.
                    yield b": ping\n\n"
                    continue
                yield b"event: change\ndata: %s\n\n" % _dumps(message)
        finally:
            _sse_subscribers.discard(queue)

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "Content-Encoding": "identity",
            "X-Accel-Buffering": "no",
        },
    )


# ---- Metagraph, split by how often each half actually changes ---------------
#
# Identity (hotkey <-> uid <-> coldkey, validator flags, axon IPs) only changes
# when something registers or deregisters — minutes to hours apart. State
# (emission, incentive, age, immunity) changes every block. Shipping them
# together meant a 28KB gzipped response every poll to carry ~5KB of movement.
#
# Split, identity answers 304 almost always and state is sent columnar: the
# hotkey strings are the bulk of this payload, and the combined shape wrote each
# 48-character hotkey seven times over, once as a key in each parallel map.
_METAGRAPH_IDENTITY_KEYS = (
    "hotkeyToUid", "uidToHotkey", "hotkeyToColdkey", "coldkeyToHotkeys",
    "isValidator", "uidToIp", "netuid", "immunityPeriod", "totalNeurons", "error",
)


def _metagraph_identity_payload(data: dict) -> dict:
    payload = {k: data.get(k) for k in _METAGRAPH_IDENTITY_KEYS if k in data}
    # `order` is the contract between the two halves: state arrays are positional
    # against it. Taken from hotkeyToUid so both halves always agree.
    payload["order"] = list(data.get("hotkeyToUid") or {})
    return payload


def _metagraph_state_payload(data: dict, identity_etag: str) -> dict:
    order = list(data.get("hotkeyToUid") or {})
    cols = {}
    for name, source in (
        ("emissions", data.get("emissions") or {}),
        ("incentives", data.get("incentives") or {}),
        ("minerAge", data.get("minerAge") or {}),
    ):
        cols[name] = [source.get(h) for h in order]
    immune = data.get("isImmune") or {}
    cols["isImmune"] = [1 if immune.get(h) else 0 for h in order]
    return {
        # State arrays are positional against identity's `order`, so a client
        # holding a *stale* identity would map values onto the wrong hotkeys. It
        # can't detect that from lengths alone — one deregistration plus one
        # registration in the same block keeps the count identical. So state
        # carries the identity ETag it was built against and the client refetches
        # identity unconditionally when it doesn't match what it holds.
        "identity_etag": identity_etag,
        "order_len": len(order),
        "cols": cols,
        "currentBlock": data.get("currentBlock"),
        "alphaPriceTao": data.get("alphaPriceTao"),
        "netuid": data.get("netuid"),
        "fetched_at": data.get("fetched_at"),
    }


def _metagraph_halves(entry: dict) -> dict:
    """Build and memoise both halves together, so state always names the identity
    it was derived from. One reshape per refresh, not one per request."""
    if "halves" not in entry:
        data = entry["data"]
        identity = _serialize_json(_metagraph_identity_payload(data))
        state = _serialize_json(_metagraph_state_payload(data, identity[1]))
        entry["halves"] = {"identity": identity, "state": state}
    return entry["halves"]


async def _metagraph_half(request: Request, kind: str) -> Response:
    netuid = _parse_netuid(request.query_params.get("netuid"), 79)
    entry = _metagraph_cache.get(netuid)
    if not entry:
        try:
            await _refresh_metagraph(netuid)
        except Exception as e:
            logger.error("Metagraph %s load failed for netuid=%s: %s", kind, netuid, e)
            raise HTTPException(status_code=502, detail=str(e))
        entry = _metagraph_cache[netuid]
    elif (time.time() - entry["timestamp"]) >= METAGRAPH_CACHE_TTL:
        asyncio.create_task(_swallow(_refresh_metagraph(netuid)))
    body, etag = _metagraph_halves(entry)[kind]
    return _json_bytes_response(request, body, etag)


@app.get("/api/metagraph-identity")
async def get_metagraph_identity(request: Request):
    """Hotkey/uid/coldkey maps. Changes only on registration churn — expect 304s."""
    return await _metagraph_half(request, "identity")


@app.get("/api/metagraph-state")
async def get_metagraph_state(request: Request):
    """Per-block values as arrays positional against the identity's `order`."""
    return await _metagraph_half(request, "state")


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


_uid_metrics_cache = _ByteCache(UID_METRICS_CACHE_SECONDS)

# Fields the client never reads. Verified against asset/miners.js before removing
# each one; re-check with `grep -c '\bfield\b' asset/miners.js` before adding to
# this list. A denylist rather than an allowlist so a genuinely new metric reaches
# the UI without a server change.
UID_METRICS_DROP_FIELDS = frozenset(
    f.strip() for f in os.getenv(
        "UID_METRICS_DROP_FIELDS",
        # unused outright:            derivable client-side from reg_block:
        "reg_cost_rao,reg_cost_tao,reg_block,max_book_volume,blocks_since_registration",
    ).split(",") if f.strip()
)
# The only three metrics rendered with a delta arrow (renderPnlMetricCell for the
# first two, an explicit call for the third). `prev_metrics` was a byte-for-byte
# duplicate of `metrics` — half the payload — to serve exactly these.
UID_METRICS_DELTA_FIELDS = ("real_pnl", "unrl_pnl", "volume")


def _slim_uid_metrics(payload: dict) -> dict:
    """Replace prev_metrics with per-UID deltas, drop unread fields, go columnar.

    Measured on the live fleet: 281,835 raw / 58,556 gzipped -> 59,465 / 25,339.
    The client rehydrates `cols` back into the per-UID object shape on arrival, so
    nothing downstream of loadUidMetrics() changes.
    """
    metrics = payload.get("metrics") or {}
    prev = payload.get("prev_metrics") or {}
    out = {k: v for k, v in payload.items() if k not in ("metrics", "prev_metrics")}

    uids = sorted(metrics, key=lambda u: int(u) if str(u).lstrip("-").isdigit() else 0)
    fields = sorted({f for row in metrics.values() for f in row} - UID_METRICS_DROP_FIELDS)
    # reg_date/_iso/_tz are three renderings of the instant reg_date_utc_iso already
    # carries; the client only falls back to them when the ISO field is missing on
    # an older daily-status file, so keep them exactly then and drop them otherwise.
    if all((metrics[u].get("reg_date_utc_iso") for u in uids)):
        fields = [f for f in fields if f not in ("reg_date", "reg_date_iso", "reg_date_tz")]

    out["uids"] = [int(u) if str(u).lstrip("-").isdigit() else u for u in uids]
    out["fields"] = fields
    out["cols"] = {f: [metrics[u].get(f) for u in uids] for f in fields}
    out["deltas"] = {
        f: [
            (metrics[u][f] - prev[u][f])
            if (isinstance(metrics.get(u, {}).get(f), (int, float))
                and isinstance(prev.get(u, {}).get(f), (int, float)))
            else None
            for u in uids
        ]
        for f in UID_METRICS_DELTA_FIELDS
    }
    return out


@app.get("/api/uid-metrics")
async def get_uid_metrics(request: Request):
    import asyncio
    from datetime import datetime, timezone
    from miner_status_cache import get_cached_metrics_payload, load_daily_status, filter_metrics

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    uids_param = request.query_params.get("uids", "").strip()
    # `shape=full` returns the original per-UID objects plus prev_metrics, for
    # anything reading this endpoint outside the dashboard. The UI uses the
    # default slim shape.
    shape = request.query_params.get("shape", "slim").strip().lower()
    if shape not in ("slim", "full"):
        raise HTTPException(status_code=400, detail="Invalid shape parameter")
    uids: Optional[list[int]] = None
    if uids_param:
        try:
            uids = [int(x.strip()) for x in uids_param.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid uids parameter")

    def _shape(payload: dict) -> dict:
        return payload if shape == "full" else _slim_uid_metrics(payload)

    def _source_mtimes() -> tuple:
        """Exact change signal for this payload: it is derived entirely from two
        files, both rewritten by the 900s refresh worker. Two stat() calls decide
        whether the cached bytes are still correct."""
        from miner_status_cache import daily_status_path, prev_epoch_path

        out = []
        for path in (daily_status_path(), prev_epoch_path()):
            try:
                out.append(os.path.getmtime(path))
            except OSError:
                out.append(None)
        return tuple(out)

    if not refresh:
        # ~275KB for the full fleet. get_cached_metrics_payload() already avoids the
        # chain, but re-serialising that dict is 44ms of event-loop stall on every
        # poll for data the 900s refresh worker only changes four times an hour.
        cache_key = (uids_param, shape, _source_mtimes())
        hit = _uid_metrics_cache.get(cache_key)
        if hit:
            return _json_bytes_response(request, hit[0], hit[1])
        cached = await run_in_threadpool(get_cached_metrics_payload, uids)
        if cached:
            body, etag = _uid_metrics_cache.put(cache_key, _shape(cached))
            # Deliberately no _publish() here. A byte-cache miss means our copy
            # expired, not that the data changed — and announcing it from inside
            # the GET races the very fetch that caused it: the notification can
            # arrive before that fetch has stored its ETag, so the client cannot
            # tell it already has this and re-downloads the whole payload.
            # _miner_status_refresh_worker owns this topic; it knows when the
            # underlying file actually changed.
            return _json_bytes_response(request, body, etag, "miss")
    else:
        _uid_metrics_cache.invalidate()

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
        return _shape(payload)
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
    # Reads and parses up to 90 days of JSONL — measured 122ms for days=2. That is
    # opened from a modal, but a modal opening must not stall every other client.
    return await run_in_threadpool(build_uid_history, uid, days, day, limit)


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
    return await run_in_threadpool(build_agent_history_response, uid, limit)


@app.get("/api/fee-regime")
async def get_fee_regime(request: Request):
    # The upstream fetch and summary build live in _compute_fee_regime, so the
    # worker and this handler cannot drift apart.
    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    include_books = request.query_params.get("books", "").lower() in ("1", "true", "yes")
    now = time.time()

    def _cached_payload() -> Optional[dict]:
        """The cached answer in the shape this request asked for, or None.

        `books=1` used to bypass every branch below and always run the live 2.8s
        fetch — the one query shape the page actually uses for that panel. The
        books are now cached by the same worker tick that produced the summary,
        so both shapes are served from memory.
        """
        payload = _fee_regime_cache["payload"]
        if payload is None:
            return None
        if not include_books:
            return payload
        books = _fee_regime_cache.get("books")
        if books is None:
            return None
        return {**payload, "books": books}

    cached = _cached_payload()
    if (
        refresh
        and cached is not None
        and (now - _fee_regime_cache.get("loaded_at", 0)) < CHAIN_BLOCK_SECONDS
    ):
        body, etag = _serialize_json(cached)
        return _json_bytes_response(request, body, etag, "hit-coalesced")

    if not refresh and cached is not None:
        fresh = now < _fee_regime_cache["expires_at"]
        if not fresh:
            # Same rule as everywhere else here: stale-but-real beats a 2.2s wait
            # on TAOS. _fee_regime_refresh_worker owns freshness; this is the
            # backstop for a tick that failed.
            asyncio.create_task(_swallow(_refresh_fee_regime()))
        body, etag = _serialize_json(cached)
        return _json_bytes_response(request, body, etag, "hit" if fresh else "stale")

    def _cached_fallback(reason: str):
        """Serve the last-good regime summary during a transient upstream miss
        instead of failing the whole panel."""
        payload = _fee_regime_cache["payload"]
        if payload is None:
            return None
        logger.warning("Fee regime unavailable (%s); serving cached summary", reason)
        stale = dict(payload)
        # Carry the books through when the caller wanted them: the Books panel
        # renders from that key, so dropping it turns a degraded answer into a
        # broken one.
        if include_books and _fee_regime_cache.get("books") is not None:
            stale["books"] = _fee_regime_cache["books"]
        stale["stale"] = True
        return stale

    try:
        summary = await _refresh_fee_regime(include_books)
        body, etag = _serialize_json(summary)
        return _json_bytes_response(request, body, etag, "miss")
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


_fee_history_cache = _ByteCache(FEE_HISTORY_CACHE_SECONDS)


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

    # `book_points=latest` ships only the most recent per-book snapshot instead of
    # one per point. That is what dominates this payload — 128 books x 93 snapshots
    # is ~740KB of the ~754KB a full day costs — and the only thing the books chart
    # ever reads is bookPoints[bookPoints.length - 1]. Everything before it was
    # downloaded, parsed and discarded. Default stays "all" so any other caller
    # keeps the old shape.
    book_mode = request.query_params.get("book_points", "all").strip().lower()
    if book_mode not in ("all", "latest", "none"):
        raise HTTPException(status_code=400, detail="Invalid book_points parameter")

    # The single largest payload on a page load, and 135ms to build. The underlying
    # JSONL only grows once per fee-history append, so it is trimmed to chart
    # resolution and served from a byte cache.
    cache_key = (days, day, uid, limit, book_mode)
    hit = _fee_history_cache.get(cache_key)
    if hit:
        return _json_bytes_response(request, hit[0], hit[1])

    payload = await run_in_threadpool(build_fee_history, days, day, uid, limit)
    if book_mode != "all":
        points = payload.get("book_points") or []
        payload["book_points"] = points[-1:] if (book_mode == "latest" and points) else []
    for key in ("global_points", "book_points", "uid_points"):
        points = payload.get(key)
        if isinstance(points, list):
            payload[key] = _downsample(points, FEE_HISTORY_MAX_POINTS)
    # The *_count fields describe what was shipped, so charts and readouts stay
    # consistent with the arrays; *_total keeps the pre-downsample figure.
    for count_key, points_key in (
        ("point_count", "global_points"),
        ("book_point_count", "book_points"),
        ("uid_point_count", "uid_points"),
    ):
        if count_key in payload and isinstance(payload.get(points_key), list):
            payload[count_key + "_total"] = payload[count_key]
            payload[count_key] = len(payload[points_key])
    payload["max_points"] = FEE_HISTORY_MAX_POINTS
    payload["days_available"] = await run_in_threadpool(list_fee_history_days)

    body, etag = _fee_history_cache.put(cache_key, payload)
    return _json_bytes_response(request, body, etag, "miss")


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

    if cached and refresh and (now - cached.get("loaded_at", 0)) < CHAIN_BLOCK_SECONDS:
        body, etag = _serialize_json(cached["payload"])
        return _json_bytes_response(request, body, etag, "hit-coalesced")

    if cached and not refresh:
        fresh = now < cached.get("expires_at", 0)
        if not fresh:
            # Expired is not a reason to make this caller wait ~2s on the chain.
            # _reg_cost_refresh_worker normally gets here first; this covers a
            # netuid it doesn't warm, or a tick that failed.
            asyncio.create_task(_swallow(_refresh_reg_cost(netuid)))
        body, etag = _serialize_json(cached["payload"])
        return _json_bytes_response(request, body, etag, "hit" if fresh else "stale")

    try:
        # Out of process, not in a thread: this query is ~2s of pure-Python substrate
        # decoding, and run in-process it holds the GIL for that whole time — measured, it
        # took the event loop's response latency from ~1ms to 6-400ms for the duration, so
        # every other caller (an agent Confirm, a poll) waited on it. See regbot.chain_cli.
        try:
            payload = await _refresh_reg_cost(netuid)
        except Exception as exc:
            if cached:
                # A forced refresh that failed still has a last good answer.
                body, etag = _serialize_json(cached["payload"])
                return _json_bytes_response(request, body, etag, "stale-error")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch reg cost for subnet {netuid}: {exc}",
            )
        body, etag = _serialize_json(payload)
        return _json_bytes_response(request, body, etag, "miss")
    except HTTPException:
        raise
    except ImportError as e:
        logger.error("Reg cost module import failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Reg cost module unavailable: {e}")
    except Exception as e:
        logger.error("Reg cost fetch failed for netuid=%s: %s", netuid, e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/sim-id")
async def get_sim_id_endpoint(request: Request):
    """Currently-live TAOS simulation epoch for subnet 79 (get_sim_id has its
    own 5-min cache, so this is cheap to poll on every dashboard refresh)."""
    import asyncio
    from sim_uid import get_sim_id

    try:
        sim_id = await asyncio.to_thread(get_sim_id)
        body, etag = _serialize_json({"sim_id": sim_id})
        return _json_bytes_response(request, body, etag)
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


def _alpha_candle_age_hours(timestamp: str) -> Optional[float]:
    """Hours between `timestamp` (a source candle label) and now, or None if it
    doesn't parse. Never negative: a clock skew that puts the newest candle
    slightly in the future should read as "current", not as a negative age."""
    from datetime import timezone as _tz

    try:
        stamp = datetime.strptime(timestamp, "%Y-%m-%d %H:%M").replace(tzinfo=_tz.utc)
    except (TypeError, ValueError):
        return None
    delta = (datetime.now(_tz.utc) - stamp).total_seconds() / 3600.0
    return round(max(0.0, delta), 3)


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
    latest_candle = rows[-1][0]
    age_hours = _alpha_candle_age_hours(latest_candle)

    payload = {
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
        # How old the data actually is. Distinct from updated_at on purpose:
        # the collector rewrites the file on a timer whether or not the feed
        # behind it produced anything, so mtime stays fresh while the series
        # sits still — it says when the file was touched, not how current the
        # prices are. Everything user-facing keys off latest_candle instead.
        "latest_candle": latest_candle,
        "age_hours": age_hours,
        "stale": (age_hours is not None and age_hours > ALPHA_PRICE_STALE_AFTER_HOURS),
        "stale_after_hours": ALPHA_PRICE_STALE_AFTER_HOURS,
        "updated_at": datetime.fromtimestamp(entry["mtime"], timezone.utc).isoformat(),
    }
    # `updated_at` is the source file's mtime, which the collector bumps on a
    # timer whether or not the prices moved — so the ETag is taken over the
    # series instead, and an unchanged range answers 304.
    body, etag = _serialize_json(payload, etag_over={k: v for k, v in payload.items() if k != "updated_at"})
    return _json_bytes_response(request, body, etag)


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
async def regbot_balance_refresh(request: Request):
    """Make sure the balance/stake cache is current, for the Add Miner / Unstake /
    Multi-Server Register modals.

    This used to run the sweep synchronously on every call — 12 coldkeys over one
    chain connection, measured at 4.5s and as much as 15s — while
    `_regbot_balance_refresh_worker` was already keeping the same file warm every
    REGBOT_BALANCE_CACHE_SECONDS. The caller (`loadRegBotBalances`) is
    fire-and-forget and never reads the body, so blocking bought nothing at all.

    Now it follows the rule the rest of this service follows: the worker owns
    freshness, the request does a lookup, and a sweep only runs behind the
    response when the file has actually gone stale. `?wait=1` restores the old
    blocking behaviour for a caller that genuinely needs the new numbers before it
    can continue.
    """
    from regbot import load_balance_cache

    wait = request.query_params.get("wait", "").lower() in ("1", "true", "yes")
    age = _balance_cache_age()
    cached = load_balance_cache()

    if wait or cached is None:
        # Nothing to serve, or the caller asked to block.
        try:
            return await _refresh_regbot_balances()
        except Exception as e:
            logger.error("RegBot balance refresh failed: %s", e)
            if cached is not None:
                return {**cached, "stale": True}
            raise HTTPException(status_code=502, detail=str(e))

    if age >= REGBOT_BALANCE_STALE_SECONDS:
        # Stale: hand back what we have and let the sweep run behind the response.
        asyncio.create_task(_swallow(_refresh_regbot_balances()))
        return {**cached, "cache": "stale"}

    return {**cached, "cache": "hit"}


@app.get("/api/regbot/history")
async def regbot_history(limit: int = 50):
    from regbot import list_recent_runs

    return {"runs": list_recent_runs(limit)}


@app.get("/api/regbot/active")
async def regbot_active(request: Request):
    """Live RegBot runs, read out of _regbot_active_scan_worker's last result.

    `?refresh=1` forces a live scan and is what the UI uses right after triggering
    or killing a run, where waiting up to REGBOT_ACTIVE_SCAN_SECONDS for the pill
    to appear or disappear would look broken. Background polling must not use it —
    that is the SSH fan-out this worker exists to keep off the request path.
    """
    from regbot import get_active_runs

    refresh = request.query_params.get("refresh", "").lower() in ("1", "true", "yes")
    cold = _regbot_active_state["body"] is None

    if refresh or cold:
        try:
            body, etag = await _store_regbot_active(await get_active_runs())
        except Exception as e:
            logger.error("RegBot active-run scan failed: %s", e)
            if cold:
                raise HTTPException(status_code=502, detail=str(e))
            # A failed forced rescan still has the worker's last good result.
            return _json_bytes_response(
                request, _regbot_active_state["body"], _regbot_active_state["etag"], "stale-error"
            )
        return _json_bytes_response(request, body, etag, "miss")

    age = time.time() - _regbot_active_state["updated_at"]
    state = "hit" if age < REGBOT_ACTIVE_SCAN_SECONDS * 2 else "stale"
    return _json_bytes_response(
        request, _regbot_active_state["body"], _regbot_active_state["etag"], state
    )


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


_batch_active_etag = {"value": None}


@app.get("/api/regbot/batch/active")
async def regbot_batch_active(request: Request):
    """`batches` is the full set (several can run at once); `batch` is the most
    recent one, kept so existing callers keep working.

    Already an in-memory read, but ~19KB of it every 15s per tab — and byte-identical
    between polls whenever nothing is registering, which is most of the time. The
    ETag turns those into 304s.
    """
    from regbot import get_active_batch, get_active_batches

    body, etag = _serialize_json(
        {"batches": get_active_batches(), "batch": get_active_batch()}
    )
    if etag != _batch_active_etag["value"]:
        _batch_active_etag["value"] = etag
        _publish("batch_active", etag)
    return _json_bytes_response(request, body, etag)


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


_unstake_active_etag = {"value": None}


@app.get("/api/unstake/active")
async def unstake_active(request: Request):
    from unstake import get_active_unstake_runs_view

    body, etag = _serialize_json({"active": get_active_unstake_runs_view()})
    if etag != _unstake_active_etag["value"]:
        _unstake_active_etag["value"] = etag
        _publish("unstake_active", etag)
    return _json_bytes_response(request, body, etag)


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
async def coldkeys_summary(request: Request):
    from coldkeys_watch import get_latest_summary

    payload = get_latest_summary() or {
        "run_at": None,
        "is_first_run": True,
        "new_coldkeys": [],
        "new_ips": [],
        "new_coldkey_to_ips": {},
        "total_coldkeys": 0,
        "total_ips": 0,
        "total_miners": 0,
    }
    body, etag = _serialize_json(payload)
    return _json_bytes_response(request, body, etag)


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
