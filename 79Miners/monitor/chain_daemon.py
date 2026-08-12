#!/usr/bin/env python3
"""One long-lived AsyncSubtensor, shared by the whole dashboard over a Unix socket.

WHY THIS EXISTS
---------------
Every chain read in this service used to spawn a fresh child process, and the
measurements say the query was never the expensive part:

    import bittensor                     0.77 s
    connect + runtime-metadata decode    1.04 s
    ------------------------------------------
    fixed setup paid on EVERY call      ~1.81 s

    metagraph sync (one netuid)          0.40 s
    Burn / reg-cost storage query        0.02 s
    subnet price                         0.06 s

So ~1.8 s of setup bought ~0.5 s of work, three times per metagraph cycle and
again for every reg-cost and balance sweep. Holding ONE connection open and
reusing it removes all of it: netuid 79's metagraph goes 2.97 s -> ~0.4 s, and
reg cost goes 2.2 s -> a memory read.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
Registration. Real miner registration (creg79 / run79.sh) keeps its own
connection on the miner box, exactly as before. A registration race is timing
critical and must not queue behind a dashboard read, or share a socket whose
reconnect it cannot control. This daemon is READ-ONLY: it never signs, never
submits an extrinsic, and holds no wallet.

NOT A SINGLE POINT OF FAILURE
-----------------------------
Every caller falls back to the old child-process path when the socket is absent
(see chain_client.py). Stopping this daemon degrades latency and nothing else.

BLOCK-PACED REG-COST POLLING
----------------------------
Modelled on BT_79_Dashboard/scripts/reg_price_watcher.py and the reg bot's
ChainPollSession: reg cost only changes when a block lands (~12 s), so polling it
on a timer is either stale or wasteful. Instead we fast-poll at 1 s to catch the
block turn, and once a NEW block is seen we settle-sleep 10 s because the next
one cannot arrive sooner. That is ~2-3 queries per block instead of a connection
per request, and the cached answer is never more than one block old.

PROTOCOL
--------
Newline-delimited JSON over AF_UNIX. Request mirrors chain_cli.py's argv so
run_chain_cli() can route to either without translating:

    {"cmd": "reg-cost",  "args": ["79"]}
    {"cmd": "balances",  "args": ["79", "<ss58>", ...]}
    {"cmd": "metagraph", "args": ["79"]}
    {"cmd": "ping",      "args": []}

Response is always one JSON line, always with "ok", so callers read one shape.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
DASHBOARD_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, DASHBOARD_ROOT)

SOCKET_PATH = os.getenv("CHAIN_DAEMON_SOCKET", "/tmp/.sn79-chaind.sock")
ENDPOINT = os.getenv("CHAIN_DAEMON_ENDPOINT", "finney")
# Netuids whose reg cost is kept block-fresh in memory.
REG_COST_NETUIDS = [
    int(x) for x in os.getenv("CHAIN_DAEMON_REG_COST_NETUIDS", "79,88").split(",") if x.strip()
]
# Cadence, straight from the reference watcher.
SETTLE_SECONDS = float(os.getenv("CHAIN_DAEMON_SETTLE_SECONDS", "10"))
POLL_SECONDS = float(os.getenv("CHAIN_DAEMON_POLL_SECONDS", "1"))
FAIL_SLEEP_SECONDS = float(os.getenv("CHAIN_DAEMON_FAIL_SLEEP_SECONDS", "30"))
# A single query that hangs must not wedge the shared connection forever.
QUERY_TIMEOUT = float(os.getenv("CHAIN_DAEMON_QUERY_TIMEOUT", "90"))
# asyncio streams cap a single line at 64KiB by default; a netuid-79 metagraph
# payload is ~150KB. Must match chain_client.STREAM_LIMIT.
STREAM_LIMIT = int(os.getenv("CHAIN_DAEMON_STREAM_LIMIT", str(32 * 1024 * 1024)))
REG_COST_STATE_DIR = os.path.join(DASHBOARD_ROOT, "miner_status")


def _log(msg: str) -> None:
    print("[chaind] %s" % msg, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: str, payload: dict) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ChainSession:
    """One AsyncSubtensor, reconnected on demand.

    Mirrors Paul-SuperRegBot's ChainPollSession: a failed query invalidates the
    connection so the next caller reconnects, rather than every caller inheriting
    a dead websocket. Connecting is serialised by a lock — a chain hiccup would
    otherwise have every in-flight request open its own replacement, which is the
    connection burst this whole daemon exists to avoid.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._sub = None
        self._lock = asyncio.Lock()
        self.connected_at: Optional[float] = None
        self.reconnects = 0

    async def get(self):
        if self._sub is not None:
            return self._sub
        async with self._lock:
            if self._sub is not None:  # another caller won the race
                return self._sub
            from bittensor.core.async_subtensor import AsyncSubtensor

            sub = AsyncSubtensor(network=self.endpoint)
            await sub.initialize()
            # initialize() is lazy; force the metadata decode here so the first
            # real query isn't the one that pays for it.
            await sub.get_current_block()
            self._sub = sub
            self.connected_at = time.time()
            self.reconnects += 1
            _log("connected to %s (connection #%d)" % (self.endpoint, self.reconnects))
            return self._sub

    async def invalidate(self) -> None:
        async with self._lock:
            sub, self._sub = self._sub, None
        if sub is None:
            return
        try:
            await sub.close()
        except Exception:
            pass
        _log("connection dropped; next call reconnects")

    async def run(self, coro_factory, what: str):
        """Await a chain call, invalidating the connection if it fails."""
        try:
            sub = await self.get()
            return await asyncio.wait_for(coro_factory(sub), timeout=QUERY_TIMEOUT)
        except Exception as exc:
            _log("%s failed: %s: %s" % (what, type(exc).__name__, exc))
            await self.invalidate()
            raise


class RegCostPoller:
    """Keeps one netuid's registration cost within a block of the chain."""

    def __init__(self, session: ChainSession, netuid: int) -> None:
        self.session = session
        self.netuid = netuid
        self.current: Optional[dict] = None
        self.last_block: Optional[int] = None
        self.last_price: Optional[float] = None
        # What the most recent registrant effectively paid: the price sampled
        # just BEFORE the burn jumped. Same reading as creg79's "‼" separator.
        self.last_registered_tao: Optional[float] = None
        self.last_registered_block: Optional[int] = None
        self.last_registered_at: Optional[str] = None
        self._seed_from_disk()

    @property
    def state_path(self) -> str:
        return os.path.join(REG_COST_STATE_DIR, "reg_cost_%d.json" % self.netuid)

    def _seed_from_disk(self) -> None:
        """A restart must not forget the last-registered reading."""
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.last_registered_tao = d.get("last_registered_tao")
        self.last_registered_block = d.get("last_registered_block")
        self.last_registered_at = d.get("last_registered_at")
        result = d.get("result")
        if isinstance(result, dict):
            # Serve something immediately rather than making the first caller wait
            # for the first poll. Marked stale so nobody mistakes it for live.
            self.current = {**result, "stale": True}

    async def _sample(self) -> Optional[dict]:
        async def _q(sub):
            block = await sub.get_current_block()
            call = await sub.substrate.query(
                module="SubtensorModule",
                storage_function="Burn",
                params=[self.netuid],
                block_hash=await sub.determine_block_hash(block),
            )
            if call is None:
                return None
            rao = int(getattr(call, "value", call))
            return {
                "rao": rao,
                "tao": float(rao) / pow(10, 9),
                "block": block,
                "netuid": self.netuid,
            }

        return await self.session.run(_q, "reg-cost netuid=%d" % self.netuid)

    def _persist(self) -> None:
        try:
            _atomic_write(
                self.state_path,
                {
                    "result": self.current,
                    "last_registered_tao": self.last_registered_tao,
                    "last_registered_block": self.last_registered_block,
                    "last_registered_at": self.last_registered_at,
                    "updated_at": _now_iso(),
                },
            )
        except Exception as exc:
            _log("reg-cost persist failed for netuid=%d: %s" % (self.netuid, exc))

    async def loop(self) -> None:
        while True:
            try:
                sample = await self._sample()
            except Exception:
                await asyncio.sleep(FAIL_SLEEP_SECONDS)
                continue
            if sample is None:
                await asyncio.sleep(FAIL_SLEEP_SECONDS)
                continue

            price, block = sample["tao"], sample["block"]

            # An up-tick means a registration just landed and the burn jumped; the
            # price immediately before it is what that registrant paid.
            if self.last_price is not None and price > self.last_price:
                self.last_registered_tao = self.last_price
                self.last_registered_block = block
                self.last_registered_at = _now_iso()
                _log(
                    "netuid=%d up-tick at block %s (%.4f -> %.4f); last_registered=%.4f"
                    % (self.netuid, block, self.last_price, price, self.last_price)
                )

            self.current = sample
            if price != self.last_price:
                self.last_price = price
                self._persist()

            # Block-paced: a new block means the next is ~12s out, so settle;
            # otherwise fast-poll to catch the turn.
            if block != self.last_block:
                self.last_block = block
                await asyncio.sleep(SETTLE_SECONDS)
            else:
                await asyncio.sleep(POLL_SECONDS)


class Daemon:
    def __init__(self) -> None:
        self.session = ChainSession(ENDPOINT)
        self.pollers: Dict[int, RegCostPoller] = {
            n: RegCostPoller(self.session, n) for n in REG_COST_NETUIDS
        }
        self.started_at = time.time()
        self.served = 0

    # ---- operations ---------------------------------------------------------

    async def op_ping(self, args) -> dict:
        return {
            "ok": True,
            "uptime": round(time.time() - self.started_at, 1),
            "served": self.served,
            "reconnects": self.session.reconnects,
            "connected_at": self.session.connected_at,
            "reg_cost_netuids": sorted(self.pollers),
        }

    async def op_reg_cost(self, args) -> dict:
        """Served from the block-paced poller: no chain call on this path at all."""
        netuid = int(args[0])
        poller = self.pollers.get(netuid)
        if poller is not None and poller.current is not None:
            return {"ok": True, "result": poller.current, "source": "poller"}
        # A netuid nobody polls, or the first sample hasn't landed yet.
        adhoc = poller or RegCostPoller(self.session, netuid)
        try:
            sample = await adhoc._sample()
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        if sample is None:
            return {"ok": False, "error": "failed to fetch reg cost for subnet %d" % netuid}
        if poller is not None:
            poller.current = sample
        return {"ok": True, "result": sample, "source": "live"}

    async def op_balances(self, args) -> dict:
        """Same fields and same one-connection rule as chain_cli.py's `balances`."""
        netuid = int(args[0])
        addresses = list(dict.fromkeys(args[1:]))
        if not addresses:
            return {"ok": False, "error": "no addresses given"}

        async def _q(sub):
            price_rao = int(await sub.get_subnet_price(netuid))
            alpha_price_tao = float(price_rao) / pow(10, 9)
            balances = await sub.get_balances(*addresses)
            stake_lists = await asyncio.gather(
                *[sub.get_stake_info_for_coldkey(a) for a in addresses],
                return_exceptions=True,
            )
            out: Dict[str, Any] = {}
            errors: Dict[str, str] = {}
            for addr, stakes in zip(addresses, stake_lists):
                if isinstance(stakes, BaseException):
                    errors[addr] = str(stakes)
                    continue
                bal = balances.get(addr)
                if bal is None:
                    errors[addr] = "no balance returned"
                    continue
                rao = int(bal)
                staked_rao = sum(int(si.stake) for si in stakes if si.netuid == netuid)
                staked_alpha = float(staked_rao) / pow(10, 9)
                out[addr] = {
                    "address": addr,
                    "rao": rao,
                    "tao": float(rao) / pow(10, 9),
                    "staked_rao": staked_rao,
                    "staked_alpha": staked_alpha,
                    "alpha_price_tao": alpha_price_tao,
                    "staked_tao": staked_alpha * alpha_price_tao,
                }
            return {"ok": True, "balances": out, "errors": errors}

        try:
            return await self.session.run(_q, "balances netuid=%d" % netuid)
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    async def op_metagraph(self, args) -> dict:
        """The same payload load_metagraph.py prints, built by the same function."""
        from load_metagraph import build_payload, empty_payload

        netuid = int(args[0])

        async def _q(sub):
            mg = await sub.metagraph(netuid=netuid, lite=True)
            immunity_period = 10000
            alpha_price_tao = None
            try:
                hp = await sub.get_subnet_hyperparameters(netuid)
                immunity_period = hp.immunity_period
            except Exception:
                pass
            try:
                alpha_price_tao = float((await sub.subnet(netuid)).price)
            except Exception:
                pass
            return build_payload(mg, netuid, immunity_period, alpha_price_tao)

        try:
            return {"ok": True, "result": await self.session.run(_q, "metagraph netuid=%d" % netuid)}
        except Exception as exc:
            return {
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "result": empty_payload(netuid, error=str(exc)),
            }

    # ---- server -------------------------------------------------------------

    async def dispatch(self, request: dict) -> dict:
        ops = {
            "ping": self.op_ping,
            "reg-cost": self.op_reg_cost,
            "balances": self.op_balances,
            "metagraph": self.op_metagraph,
        }
        cmd = request.get("cmd")
        handler = ops.get(cmd)
        if handler is None:
            return {"ok": False, "error": "unknown cmd %r" % (cmd,)}
        args = request.get("args") or []
        if not isinstance(args, list):
            return {"ok": False, "error": "args must be a list"}
        try:
            self.served += 1
            return await handler([str(a) for a in args])
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # One request per connection: the client is asyncio too, and a
            # per-call socket keeps a slow caller from blocking anyone else's
            # line on a shared stream.
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not line:
                return
            try:
                request = json.loads(line)
            except ValueError as exc:
                response = {"ok": False, "error": "bad request: %s" % exc}
            else:
                response = await self.dispatch(request)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            _log("handler error: %s: %s" % (type(exc).__name__, exc))
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def main() -> None:
    daemon = Daemon()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = await asyncio.start_unix_server(daemon.handle, path=SOCKET_PATH, limit=STREAM_LIMIT)
    os.chmod(SOCKET_PATH, 0o600)
    _log("listening on %s (endpoint=%s)" % (SOCKET_PATH, ENDPOINT))

    pollers = [asyncio.create_task(p.loop()) for p in daemon.pollers.values()]
    _log("block-paced reg-cost pollers: %s" % sorted(daemon.pollers))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with server:
        await stop.wait()

    _log("shutting down")
    for t in pollers:
        t.cancel()
    await asyncio.gather(*pollers, return_exceptions=True)
    await daemon.session.invalidate()
    try:
        os.unlink(SOCKET_PATH)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
