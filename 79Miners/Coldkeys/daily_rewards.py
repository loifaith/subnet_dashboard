#!/usr/bin/env python3
"""
Daily emission reward per coldkey for a subnet.

Sums each coldkey's miner emissions across the metagraph and converts them to
TAO, giving what that coldkey earns per epoch and per day.

Runs with no arguments; reads defaults from ./settings.json (same file as
collect_coldkeys_ips.py). Output goes to ./out/ by default.

Units, matching what the dashboard already shows:
  metagraph.E[uid]  -> ALPHA earned per epoch by that UID
  subnet(n).price   -> TAO per ALPHA
  epochs per day    -> 86400s / (tempo blocks * 12s per block); 360 -> 20
so  tao_per_day = sum(E) * price * epochs_per_day.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Reuse the retry-wrapped connect/sync rather than reimplementing it — the
# public finney endpoint drops handshakes often enough that a single attempt
# is not reliable.
from collect_coldkeys_ips import (
    fetch_metagraph,
    load_settings,
    make_subtensor,
)

SECONDS_PER_DAY = 86_400
SECONDS_PER_BLOCK = 12
DEFAULT_EPOCHS_PER_DAY = 20.0  # tempo 360 * 12s = 1.2h -> 20 epochs/day


def _as_float(value) -> float:
    return float(value.item()) if hasattr(value, "item") else float(value)


def epochs_per_day(subtensor, netuid: int) -> float:
    """Derived from the subnet's own tempo rather than assumed: every subnet
    happens to use 360 blocks today, but that is a chain parameter, not a
    constant, and a wrong value would scale every number here."""
    try:
        tempo = int(subtensor.get_subnet_hyperparameters(netuid).tempo)
        if tempo > 0:
            return SECONDS_PER_DAY / (tempo * SECONDS_PER_BLOCK)
    except Exception:
        pass
    return DEFAULT_EPOCHS_PER_DAY


def alpha_price_tao(subtensor, netuid: int) -> float | None:
    try:
        return float(subtensor.subnet(netuid).price)
    except Exception:
        return None


def rewards_by_coldkey(
    metagraph,
    *,
    price: float | None,
    per_day: float,
    validator_uids_to_exclude: set[int],
) -> Dict[str, Any]:
    alpha_epoch: Dict[str, float] = defaultdict(float)
    uids_by_coldkey: Dict[str, list] = defaultdict(list)

    n = len(metagraph.axons) if hasattr(metagraph, "axons") else 0
    for uid in range(n):
        if uid in validator_uids_to_exclude:
            continue
        coldkey = str(metagraph.axons[uid].coldkey)
        if not coldkey or coldkey == "N/A":
            continue
        alpha_epoch[coldkey] += _as_float(metagraph.E[uid])
        uids_by_coldkey[coldkey].append(uid)

    total_alpha_day = sum(alpha_epoch.values()) * per_day

    rows = []
    for coldkey, per_epoch in alpha_epoch.items():
        alpha_day = per_epoch * per_day
        rows.append({
            "coldkey": coldkey,
            "miners": len(uids_by_coldkey[coldkey]),
            "uids": sorted(uids_by_coldkey[coldkey]),
            "alpha_per_epoch": round(per_epoch, 9),
            "alpha_per_day": round(alpha_day, 9),
            # None rather than 0 when the price is unavailable: a missing rate
            # is not the same as earning nothing.
            "tao_per_epoch": round(per_epoch * price, 9) if price is not None else None,
            "tao_per_day": round(alpha_day * price, 9) if price is not None else None,
            "share_pct": round(alpha_day / total_alpha_day * 100, 4) if total_alpha_day else 0.0,
        })

    rows.sort(key=lambda r: (-r["alpha_per_day"], r["coldkey"]))
    return {
        "coldkeys": rows,
        "totals": {
            "coldkeys": len(rows),
            "miners": sum(r["miners"] for r in rows),
            "alpha_per_epoch": round(sum(alpha_epoch.values()), 9),
            "alpha_per_day": round(total_alpha_day, 9),
            "tao_per_day": round(total_alpha_day * price, 9) if price is not None else None,
        },
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
    # fetch_metagraph hands back a synced metagraph but not its subtensor, and
    # price/tempo need one; this connection is only used for two cheap reads.
    subtensor = make_subtensor(chain_endpoint)
    price = alpha_price_tao(subtensor, netuid)
    per_day = epochs_per_day(subtensor, netuid)

    result = rewards_by_coldkey(
        metagraph,
        price=price,
        per_day=per_day,
        validator_uids_to_exclude=validator_uids,
    )

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subnet": netuid,
        "chain_endpoint": chain_endpoint,
        "current_block": int(getattr(metagraph, "block", 0)) if hasattr(metagraph, "block") else None,
        "alpha_price_tao": price,
        "epochs_per_day": per_day,
        "validator_uids_excluded": sorted(validator_uids),
        **result,
    }

    out_path = out_dir / f"subnet{netuid}_daily_rewards.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    totals = payload["totals"]
    print(f"✅ wrote {out_path}")
    if price is None:
        print("   ⚠️  alpha price unavailable — TAO columns are null, ALPHA still valid")
    print(
        f"   {totals['miners']} miners across {totals['coldkeys']} coldkeys · "
        f"{totals['alpha_per_day']:.4f} α/day"
        + (f" · τ{totals['tao_per_day']:.4f}/day" if price is not None else "")
    )
    top = payload["coldkeys"][:5]
    if top:
        print("   top coldkeys by daily reward:")
        for row in top:
            tao = f" τ{row['tao_per_day']:>10.4f}" if row["tao_per_day"] is not None else ""
            print(
                f"     {row['alpha_per_day']:>12.4f} α{tao}  "
                f"{row['share_pct']:>6.2f}%  {row['miners']:>3} miners  {row['coldkey']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
