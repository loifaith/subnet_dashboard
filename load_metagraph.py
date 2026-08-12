#!/usr/bin/env python3
"""Load metagraph data for one netuid and print it as JSON.

Run as a child process so the pure-Python substrate decode never holds the API
process's GIL — see 79Miners/monitor/chain_cli.py for the measurements.

This is now the FALLBACK path. chain_daemon.py normally answers the same query
over a long-lived AsyncSubtensor in ~0.4s instead of ~2.9s, because it does not
pay `import bittensor` (0.8s) plus a connect and metadata decode (~1.1s) on every
call. This script stays because that daemon must never be a single point of
failure: if it is down, stopped, or being restarted, app.py falls straight back
to spawning this, and behaviour is exactly what it always was.

build_payload() is shared by both paths so the two can never drift apart.
"""
import json
import socket
import sys


def _format_axon_ip(ip) -> str:
    if ip is None:
        return ""
    if isinstance(ip, int):
        try:
            return socket.inet_ntoa(ip.to_bytes(4, byteorder="big"))
        except (OSError, OverflowError, ValueError):
            return str(ip)
    value = str(ip).strip()
    if value.lower() in ("0", "0.0.0.0", "n/a", "none"):
        return ""
    return value


def empty_payload(netuid: int, error=None) -> dict:
    """The shape every caller expects when there is nothing to report."""
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
        "uidToIp": {},
        "totalNeurons": 0,
        "netuid": netuid,
        "currentBlock": 0,
        "immunityPeriod": 10000,
        "alphaPriceTao": None,
        "error": error,
    }


def build_payload(metagraph, netuid: int, immunity_period: int, alpha_price_tao) -> dict:
    """Turn a synced metagraph into the API-shaped bundle the dashboard reads.

    Pure: no chain access, no globals, no I/O. Split out of load_metagraph() so
    chain_daemon.py — which syncs the same metagraph over its own long-lived
    AsyncSubtensor — produces identical payloads without re-deriving any of this.
    The two paths disagreeing would mis-render the whole table, and only on the
    fallback path, so they share one builder rather than two copies.
    """
    uids = metagraph.uids
    hotkeys = metagraph.hotkeys
    coldkeys = metagraph.coldkeys
    incentives = metagraph.I
    emissions = metagraph.E
    validator_permit = getattr(metagraph, "validator_permit", None)

    def _num(x):
        return x.item() if hasattr(x, "item") else x

    # Immunity is measured from the registration block, not last_update.
    #
    # Coerced to int here rather than left as whatever numpy scalar the metagraph
    # hands back. `current_block - reg_block` on a numpy int yields a numpy int,
    # and comparing that to immunity_period yields numpy.bool_ — which json.dumps
    # refuses. The old script hid this behind a convert_to_native() pass over the
    # finished payload; the daemon serialises straight from here, so the values
    # have to be native at the point they are computed.
    current_block = int(_num(metagraph.block))
    immunity_period = int(_num(immunity_period))
    block_at_registration = metagraph.block_at_registration

    n = len(uids)
    if validator_permit is None:
        validator_permit = [False] * n

    hotkeyToUid = {}
    uidToHotkey = {}
    hotkeyToColdkey = {}
    coldkeyToHotkeys = {}
    emissions_dict = {}
    incentives_dict = {}
    isValidator = {}
    isImmune = {}
    minerAge = {}
    uidToIp = {}

    for i in range(n):
        hk = hotkeys[i]
        uid = int(_num(uids[i]))
        ck = coldkeys[i]

        reg_block = int(_num(block_at_registration[i]))
        blocks_since_registration = current_block - reg_block

        hotkeyToUid[hk] = uid
        uidToHotkey[str(uid)] = hk
        hotkeyToColdkey[hk] = ck
        coldkeyToHotkeys.setdefault(ck, []).append(hk)
        emissions_dict[hk] = float(_num(emissions[i]))
        incentives_dict[hk] = float(_num(incentives[i]))
        isValidator[hk] = bool(_num(validator_permit[i]))
        isImmune[hk] = blocks_since_registration < immunity_period
        minerAge[hk] = blocks_since_registration
        try:
            axon = metagraph.axons[i]
            uidToIp[str(uid)] = _format_axon_ip(getattr(axon, "ip", None))
        except Exception:
            uidToIp[str(uid)] = ""

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
        "uidToIp": uidToIp,
        "totalNeurons": n,
        "netuid": netuid,
        "currentBlock": current_block,
        "immunityPeriod": immunity_period,
        "alphaPriceTao": alpha_price_tao,
        "error": None,
    }


def load_metagraph(netuid: int = 71):
    """Load metagraph over a one-shot connection and return the API payload."""
    import gc
    import traceback

    metagraph = None
    try:
        from bittensor.core.metagraph import Metagraph

        metagraph = Metagraph(netuid=netuid, network="finney", lite=True, sync=True)

        immunity_period = 10000
        alpha_price_tao = None
        try:
            immunity_period = metagraph.subtensor.get_subnet_hyperparameters(netuid).immunity_period
        except Exception:
            pass
        try:
            alpha_price_tao = float(metagraph.subtensor.subnet(netuid).price)
        except Exception:
            pass

        return build_payload(metagraph, netuid, immunity_period, alpha_price_tao)
    except Exception as e:
        return empty_payload(netuid, error=str(e) + "\n" + traceback.format_exc())
    finally:
        try:
            if metagraph is not None:
                sub = getattr(metagraph, "subtensor", None)
                if sub is not None:
                    substrate = getattr(sub, "substrate", None) or getattr(sub, "_substrate", None)
                    if substrate is not None and hasattr(substrate, "close"):
                        substrate.close()
                del metagraph
        except Exception:
            pass
        gc.collect()


if __name__ == "__main__":
    netuid = int(sys.argv[1]) if len(sys.argv) > 1 else 71

    def convert_to_native(obj):
        if hasattr(obj, "item"):
            return obj.item()
        if isinstance(obj, dict):
            return {k: convert_to_native(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [convert_to_native(i) for i in obj]
        return obj

    print(json.dumps(convert_to_native(load_metagraph(netuid))))
