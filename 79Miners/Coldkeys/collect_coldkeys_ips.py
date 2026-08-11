#!/usr/bin/env python3
"""
Collect coldkeys, IP addresses, and miner counts per coldkey for a subnet metagraph.

Runs with no arguments; reads defaults from ./settings.json.
Output goes to ./out/ by default.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import bittensor as bt

# The public finney entrypoint refuses/So times out a websocket handshake often
# enough that a single attempt is not reliable — one blip used to abort the whole
# collection with "timed out while waiting for handshake response". Retried with
# exponential backoff, same as unstake_env.py's connect_subtensor.
CONNECT_ATTEMPTS = 5
CONNECT_BASE_DELAY_SEC = 2.0


def load_settings(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_subtensor(chain_endpoint: str):
    # Bittensor API compatibility:
    # - older versions exposed bt.subtensor(...)
    # - newer versions (e.g. 10.x) use bt.Subtensor(...)
    if hasattr(bt, "subtensor"):
        return bt.subtensor(network=chain_endpoint)
    return bt.Subtensor(network=chain_endpoint)


def fetch_metagraph(chain_endpoint: str, netuid: int):
    """Connect and sync, retrying the whole sequence on transient chain errors.

    Connect, metagraph fetch and sync are retried as one unit deliberately: a
    half-dead websocket won't recover in place, so each attempt starts from a
    fresh connection rather than reusing the one that just failed.
    """
    last_err = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            subtensor = make_subtensor(chain_endpoint)
            metagraph = subtensor.metagraph(netuid)
            metagraph.sync(subtensor=subtensor)
            return metagraph
        except Exception as exc:  # noqa: BLE001 — any chain/network error is retryable
            last_err = exc
            print(
                f"⚠️  Chain fetch failed (attempt {attempt + 1}/{CONNECT_ATTEMPTS}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if attempt + 1 < CONNECT_ATTEMPTS:
                delay = min(CONNECT_BASE_DELAY_SEC * (2**attempt), 60.0)
                print(f"⏳ Retrying in {delay:.1f}s…", file=sys.stderr)
                time.sleep(delay)
    raise RuntimeError(
        f"Could not fetch netuid {netuid} metagraph from {chain_endpoint} "
        f"after {CONNECT_ATTEMPTS} attempts: {last_err}"
    )


def extract_coldkeys_and_ips_from_metagraph(
    metagraph,
    *,
    validator_uids_to_exclude: set[int],
) -> Dict[str, Any]:
    coldkeys = set()
    ip_addresses = set()
    coldkey_to_ips = defaultdict(set)
    coldkey_to_miner_count: Dict[str, int] = defaultdict(int)

    n = len(metagraph.axons) if hasattr(metagraph, "axons") else 0
    for uid in range(n):
        if uid in validator_uids_to_exclude:
            continue

        axon = metagraph.axons[uid]
        coldkey = str(axon.coldkey) if hasattr(axon, "coldkey") else "N/A"
        # "0.0.0.0" is bittensor's own sentinel for "not currently serving" (see
        # AxonInfo.is_serving) - real for a not-yet-launched or deregistered
        # hotkey, but not a real IP, so it must not pollute the IP outputs.
        ip = str(axon.ip) if hasattr(axon, "ip") else "N/A"
        is_serving = ip not in ("N/A", "0.0.0.0")

        if coldkey and coldkey != "N/A":
            coldkeys.add(coldkey)
            coldkey_to_miner_count[coldkey] += 1
        if is_serving:
            ip_addresses.add(ip)
        if coldkey and coldkey != "N/A" and is_serving:
            coldkey_to_ips[coldkey].add(ip)

    return {
        "coldkeys": sorted(coldkeys),
        "ip_addresses": sorted(ip_addresses),
        "coldkey_to_ip_addresses": {ck: sorted(ips) for ck, ips in sorted(coldkey_to_ips.items())},
        "coldkey_to_miner_count": {ck: coldkey_to_miner_count[ck] for ck in sorted(coldkeys)},
        "total_miners": sum(coldkey_to_miner_count.values()),
    }


def main() -> int:
    settings_path = Path(__file__).resolve().parent / "settings.json"
    settings = load_settings(settings_path)

    netuid = int(settings.get("netuid", 88))
    chain_endpoint = str(settings.get("chain_endpoint"))
    out_dir = Path(__file__).resolve().parent / str(settings.get("out_dir", "out"))
    out_dir.mkdir(parents=True, exist_ok=True)

    validator_uids = set(int(x) for x in (settings.get("validator_uids_to_exclude") or []))

    metagraph = fetch_metagraph(chain_endpoint, netuid)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subnet": netuid,
        "chain_endpoint": chain_endpoint,
        "current_block": int(getattr(metagraph, "block", 0)) if hasattr(metagraph, "block") else None,
        "validator_uids_excluded": sorted(validator_uids),
        **extract_coldkeys_and_ips_from_metagraph(metagraph, validator_uids_to_exclude=validator_uids),
    }

    out_path = out_dir / f"subnet{netuid}_coldkeys_ips.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"✅ wrote {out_path}")
    print(
        f"   miners: {payload['total_miners']} across "
        f"{len(payload['coldkeys'])} coldkeys"
    )
    top = sorted(
        payload["coldkey_to_miner_count"].items(),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    if top:
        print("   top coldkeys by miner count:")
        for coldkey, count in top:
            print(f"     {count:3d}  {coldkey}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


