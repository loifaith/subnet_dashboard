#!/usr/bin/env python3
"""
SN-39 (Cathedral) miner status fetcher.

Pulls the public Cathedral leaderboard (https://cathedral.computer) and reports
the stats for *my* miners — matched by hotkey. The Cathedral API keys miners by
hotkey (no coldkey, uid usually null), so "mine" is resolved as:

  1. explicit hotkeys you list, and/or
  2. coldkeys you list, expanded to hotkeys via the SN39 metagraph.

Identify my miners via (first found wins):
  - env  CATHEDRAL_MY_HOTKEYS   (comma-separated ss58 hotkeys)
  - env  CATHEDRAL_MY_COLDKEYS  (comma-separated ss58 coldkeys)
  - file 39Miners/my_miners.json  ->  {"hotkeys": [...], "coldkeys": [...]}

Usage:
  python cathedral_status.py                 # my miners, summary table
  python cathedral_status.py --uid           # also resolve on-chain uid (loads SN39 metagraph, slow)
  python cathedral_status.py --all           # full leaderboard (top 200), not just mine
  python cathedral_status.py --hotkey 5Xxx   # one specific hotkey
  python cathedral_status.py --json          # raw JSON of the selected miners

Exit codes: 0 ok, 1 error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Set

import requests

API_BASE = "https://cathedral.computer/api/cathedral"
DASHBOARD_ENDPOINT = f"{API_BASE}/v1/dashboard/miners"
# The endpoint defaults to the top 200; request a high limit to get every
# tracked miner (currently ~331, more than the 256 on-chain neurons).
DASHBOARD_LIMIT = int(os.getenv("CATHEDRAL_LIMIT", "1000"))
NETUID = 39

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "my_miners.json")
# Parent dashboard's standalone metagraph loader (runs bittensor in a subprocess).
METAGRAPH_SCRIPT = os.path.join(os.path.dirname(HERE), "load_metagraph.py")

# (source_field_in_cathedral, short column header) — order defines the table.
DISPLAY_FIELDS = [
    ("uid",                    "uid"),
    ("current_weight_rank",    "wt_rank"),
    ("current_payment_rank",   "pay_rank"),
    ("current_weight",         "weight"),
    ("chain_incentive",        "incent"),
    ("chain_emission",         "emiss"),
    ("eligible_24h_solves",    "solves24"),
    ("tier1_24h",              "tier1"),
    ("tier2_24h",              "tier2"),
    ("weighted_units_24h",     "w_units"),
    ("pm_accepted",            "pm_acc"),
    ("pm_rejected",            "pm_rej"),
    ("last_accepted_at",       "last_accepted"),
]


def fetch_dashboard(timeout: float = 40.0) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (SN39 miner status)"}
    r = requests.get(
        DASHBOARD_ENDPOINT, params={"limit": DASHBOARD_LIMIT}, headers=headers, timeout=timeout
    )
    r.raise_for_status()
    return r.json()


def build_miner_index(data: dict) -> Dict[str, dict]:
    """Merge dashboard.miners + top.miners into {hotkey: combined record}."""
    index: Dict[str, dict] = {}
    for m in (data.get("dashboard", {}).get("miners") or []):
        hk = m.get("hotkey")
        if hk:
            index.setdefault(hk, {})["hotkey"] = hk
            index[hk].update({k: v for k, v in m.items() if v is not None or k not in index[hk]})
    for m in (data.get("top", {}).get("miners") or []):
        hk = m.get("miner_hotkey")
        if not hk:
            continue
        row = index.setdefault(hk, {"hotkey": hk})
        for k, v in m.items():
            if k == "miner_hotkey":
                continue
            # don't clobber an existing non-null value with a null
            if v is not None or k not in row:
                row[k] = v
    return index


def load_my_identity() -> tuple[Set[str], Set[str]]:
    """Return (my_hotkeys, my_coldkeys) from env or the config file."""
    hotkeys: Set[str] = set()
    coldkeys: Set[str] = set()

    env_hk = os.getenv("CATHEDRAL_MY_HOTKEYS", "").strip()
    env_ck = os.getenv("CATHEDRAL_MY_COLDKEYS", "").strip()
    if env_hk:
        hotkeys |= {x.strip() for x in env_hk.split(",") if x.strip()}
    if env_ck:
        coldkeys |= {x.strip() for x in env_ck.split(",") if x.strip()}

    if not hotkeys and not coldkeys and os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            hotkeys |= {x for x in (cfg.get("hotkeys") or []) if isinstance(x, str)}
            coldkeys |= {x for x in (cfg.get("coldkeys") or []) if isinstance(x, str)}
        except (OSError, json.JSONDecodeError, TypeError) as e:
            print(f"Warning: could not read {CONFIG_PATH}: {e}", file=sys.stderr)

    return hotkeys, coldkeys


def load_sn39_metagraph() -> Optional[dict]:
    """Run the parent load_metagraph.py for netuid 39 (subprocess; ~60-90s)."""
    if not os.path.isfile(METAGRAPH_SCRIPT):
        print(f"Warning: metagraph loader not found at {METAGRAPH_SCRIPT}", file=sys.stderr)
        return None
    try:
        out = subprocess.run(
            [sys.executable, METAGRAPH_SCRIPT, str(NETUID)],
            capture_output=True, text=True, timeout=180,
        )
        if out.returncode != 0:
            print(f"Warning: metagraph load failed: {out.stderr.strip()[:200]}", file=sys.stderr)
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"Warning: metagraph load error: {e}", file=sys.stderr)
        return None


def resolve_my_hotkeys(
    hotkeys: Set[str], coldkeys: Set[str], meta: Optional[dict]
) -> tuple[Set[str], Dict[str, int]]:
    """Expand coldkeys -> hotkeys via metagraph; return (my_hotkeys, hotkey->uid)."""
    resolved = set(hotkeys)
    hk_to_uid: Dict[str, int] = {}
    if meta:
        h2c = meta.get("hotkeyToColdkey", {})
        h2u = meta.get("hotkeyToUid", {})
        hk_to_uid = {hk: h2u[hk] for hk in h2u}
        if coldkeys:
            resolved |= {hk for hk, ck in h2c.items() if ck in coldkeys}
    return resolved, hk_to_uid


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if abs(v) >= 1_000_000:
            return f"{v/1e6:.2f}M"
        if abs(v) >= 10_000:
            return f"{v/1e3:.1f}k"
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if isinstance(v, str) and len(v) > 20:
        return v[:19]
    return str(v)


def print_table(rows: List[dict]) -> None:
    headers = ["hotkey"] + [h for _, h in DISPLAY_FIELDS]
    table = []
    for r in rows:
        hk = r.get("hotkey", "")
        line = [hk[:6] + ".." + hk[-4:] if len(hk) > 12 else hk]
        line += [fmt(r.get(src)) for src, _ in DISPLAY_FIELDS]
        table.append(line)
    widths = [len(h) for h in headers]
    for line in table:
        for i, c in enumerate(line):
            widths[i] = max(widths[i], len(c))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for line in table:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(line)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch SN39 Cathedral miner status for my miners.")
    ap.add_argument("--all", action="store_true", help="Show the full leaderboard, not just mine")
    ap.add_argument("--uid", action="store_true", help="Resolve on-chain uid via SN39 metagraph (slow)")
    ap.add_argument("--hotkey", help="Show only this hotkey")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON")
    args = ap.parse_args()

    try:
        data = fetch_dashboard()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Cathedral leaderboard: {e}", file=sys.stderr)
        return 1

    index = build_miner_index(data)

    # Decide which hotkeys to show.
    meta = None
    hk_to_uid: Dict[str, int] = {}
    if args.hotkey:
        selected = {args.hotkey}
    elif args.all:
        selected = set(index.keys())
    else:
        hotkeys, coldkeys = load_my_identity()
        if not hotkeys and not coldkeys:
            print(
                "No miners configured. Set CATHEDRAL_MY_HOTKEYS / CATHEDRAL_MY_COLDKEYS, or fill\n"
                f"{CONFIG_PATH}, or run with --all to see the full leaderboard.",
                file=sys.stderr,
            )
            return 1
        if coldkeys or args.uid:
            meta = load_sn39_metagraph()
        selected, hk_to_uid = resolve_my_hotkeys(hotkeys, coldkeys, meta)

    if args.uid and meta is None and not args.hotkey:
        meta = meta or load_sn39_metagraph()
    if meta:
        h2u = meta.get("hotkeyToUid", {})
        hk_to_uid = {hk: h2u[hk] for hk in h2u}

    # Build output rows; fill uid from metagraph when the API's is null.
    rows = []
    for hk in sorted(selected):
        row = dict(index.get(hk, {"hotkey": hk, "_not_on_leaderboard": True}))
        row["hotkey"] = hk
        if row.get("uid") is None and hk in hk_to_uid:
            row["uid"] = hk_to_uid[hk]
        rows.append(row)

    # Sort by weight rank when present.
    rows.sort(key=lambda r: (r.get("current_weight_rank") is None, r.get("current_weight_rank") or 1e9))

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    on_lb = [r for r in rows if not r.get("_not_on_leaderboard")]
    missing = [r for r in rows if r.get("_not_on_leaderboard")]
    print(f"Cathedral SN39 — {len(on_lb)} miner(s) on leaderboard "
          f"(generated_at {data.get('generated_at', '?')})")
    if on_lb:
        print_table(on_lb)
    if missing and not args.all:
        print(f"\n{len(missing)} configured hotkey(s) not in the top-200 leaderboard:")
        for r in missing:
            hk = r["hotkey"]
            uid = f" (uid {r['uid']})" if r.get("uid") is not None else ""
            print(f"  {hk}{uid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
