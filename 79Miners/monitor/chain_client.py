#!/usr/bin/env python3
"""Talk to chain_daemon.py, or say clearly that it isn't there.

The daemon is a latency optimisation, never a dependency. Everything here is
written so that a missing, wedged, or restarting daemon costs one connect() to a
Unix socket — microseconds — and then the caller runs the child-process path it
always ran. That is why `call()` returns None for "no daemon" rather than
raising: None means "fall back", while a dict means the daemon answered and the
answer (ok or not) is authoritative.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

SOCKET_PATH = os.getenv("CHAIN_DAEMON_SOCKET", "/tmp/.sn79-chaind.sock")
# Long enough for a cold metagraph sync on a warm connection (~0.4s) plus a
# reconnect (~1.2s) with room to spare, short enough that a wedged daemon does
# not out-wait the subprocess it replaced.
DEFAULT_TIMEOUT = float(os.getenv("CHAIN_DAEMON_TIMEOUT", "45"))
# asyncio's StreamReader refuses any line longer than 64KiB by default, and a
# netuid-79 metagraph payload is ~150KB — readline() raised LimitOverrunError and
# the daemon looked "unreachable" while working perfectly. Must match the value
# start_unix_server() is given in chain_daemon.py.
STREAM_LIMIT = int(os.getenv("CHAIN_DAEMON_STREAM_LIMIT", str(32 * 1024 * 1024)))

# Set once per process when the socket is found missing, so a dashboard running
# without the daemon doesn't attempt a connect on every single chain call. Any
# successful call clears it; see _note_absent/_note_present.
_absent_since: float = 0.0
_ABSENT_RECHECK_SECONDS = float(os.getenv("CHAIN_DAEMON_RECHECK_SECONDS", "30"))


def _note_absent() -> None:
    global _absent_since
    _absent_since = time.monotonic()


def _note_present() -> None:
    global _absent_since
    _absent_since = 0.0


def _recently_absent() -> bool:
    if not _absent_since:
        return False
    return (time.monotonic() - _absent_since) < _ABSENT_RECHECK_SECONDS


async def call(
    cmd: str, args: List[Any], timeout: float = DEFAULT_TIMEOUT
) -> Optional[Dict[str, Any]]:
    """Send one request. Returns the daemon's payload, or None if it is unreachable.

    None is reserved strictly for "could not talk to the daemon". A daemon that
    answered with {"ok": false, ...} is a real answer and is returned as-is, so a
    genuine chain error is not silently retried through a second code path.
    """
    if not os.path.exists(SOCKET_PATH) or _recently_absent():
        _note_absent()
        return None

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(SOCKET_PATH, limit=STREAM_LIMIT), timeout=5.0
        )
        writer.write(json.dumps({"cmd": cmd, "args": args}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            _note_absent()
            return None
        payload = json.loads(line)
        _note_present()
        return payload
    except (OSError, asyncio.TimeoutError, ValueError):
        _note_absent()
        return None
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def is_up() -> bool:
    payload = await call("ping", [], timeout=5.0)
    return bool(payload and payload.get("ok"))
