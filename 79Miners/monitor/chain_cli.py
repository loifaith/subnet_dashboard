#!/usr/bin/env python3
"""One-shot Bittensor chain queries, run as a CHILD PROCESS and printing JSON.

WHY A CHILD PROCESS AND NOT A THREAD.
substrate's runtime-metadata decode is pure Python, so a chain query holds the GIL in long
bursts. Run inside the API process — even via `asyncio.to_thread` — it starves the asyncio
event loop, and every unrelated request queues behind it. Measured on this box:

    idle                          probe latency ~1 ms
    during ONE reg-cost query     probe latency 6-406 ms, for the whole 5.4 s
    during the 300 s balance sweep (18 concurrent queries)   seconds, up to 14 s

That is what made an agent Confirm — which touches no chain at all — take tens of seconds:
it was waiting for the loop, not for its own work. A child process has its own GIL, so the
API process keeps serving while the chain work runs. It is the same reason the metagraph and
miner-status refreshes already run as children.

ONE CONNECTION PER RUN. `balances` takes every address at once and reuses a single
`Subtensor`, instead of the previous one-connection-per-address. Connecting is the expensive
part (that is where the metadata decode happens), so 18 addresses went from 18 connects to 1.

Usage:
    chain_cli.py balances <netuid> <ss58> [<ss58> ...]   -> {"ok":true,"balances":{addr:{...}}}
    chain_cli.py reg-cost <netuid>                       -> {"ok":true,"result":{...}}

Always exits 0 with `{"ok": false, "error": ...}` on failure, so the caller reads one shape.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _balance_and_stake(sub, address: str, netuid: int) -> dict:
    """Same fields as get_reg_cost.get_balance_and_stake_for_address, on a shared connection."""
    balance = sub.get_balance(address)
    rao = int(balance)
    staked_rao = sum(
        int(si.stake) for si in sub.get_stake_info_for_coldkey(address) if si.netuid == netuid
    )
    staked_alpha = float(staked_rao) / pow(10, 9)
    alpha_price_tao = float(int(sub.get_subnet_price(netuid))) / pow(10, 9)
    return {
        "address": address,
        "rao": rao,
        "tao": float(rao) / pow(10, 9),
        "staked_rao": staked_rao,
        "staked_alpha": staked_alpha,
        "alpha_price_tao": alpha_price_tao,
        "staked_tao": staked_alpha * alpha_price_tao,
    }


def cmd_balances(argv: list[str]) -> dict:
    from get_reg_cost import DEFAULT_ENDPOINT, connect_subtensor

    netuid = int(argv[0])
    addresses = list(dict.fromkeys(argv[1:]))  # de-duped, order kept
    if not addresses:
        return {"ok": False, "error": "no addresses given"}
    # Same endpoint and retry policy the in-process path used, so behaviour is unchanged.
    sub = connect_subtensor(DEFAULT_ENDPOINT, try_limit=3, verbose=False)
    if sub is None:
        return {"ok": False, "error": "could not connect to subtensor"}
    out: dict = {}
    errors: dict = {}
    try:
        # The subnet price is one query for the whole run, not one per address.
        for addr in addresses:
            try:
                out[addr] = _balance_and_stake(sub, addr, netuid)
            except Exception as exc:  # one bad address must not lose the other seventeen
                errors[addr] = str(exc)
    finally:
        try:
            sub.close()
        except Exception:
            pass
    return {"ok": True, "balances": out, "errors": errors}


def cmd_reg_cost(argv: list[str]) -> dict:
    from get_reg_cost import get_current_subnet_reg_cost

    netuid = int(argv[0])
    result = get_current_subnet_reg_cost(netuid)
    if result is None:
        return {"ok": False, "error": f"failed to fetch reg cost for subnet {netuid}"}
    return {"ok": True, "result": result}


COMMANDS = {"balances": cmd_balances, "reg-cost": cmd_reg_cost}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({"ok": False, "error": f"usage: {os.path.basename(__file__)} {{{'|'.join(COMMANDS)}}} ..."}))
        return 0
    try:
        payload = COMMANDS[sys.argv[1]](sys.argv[2:])
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # stdout is the channel; bittensor logs to stderr, which the caller reads separately.
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
