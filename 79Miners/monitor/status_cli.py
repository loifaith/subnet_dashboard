#!/usr/bin/env python3
"""
Terminal client for the SN79 miner status server (status_server.py).

Fetches the latest miner status / per-book data over HTTP and prints readable
tables. Point it at any host running the status server.

Examples:
    python status_cli.py health
    python status_cli.py uids
    python status_cli.py status                 # all my miners (summary table)
    python status_cli.py status 71              # one miner
    python status_cli.py books 71               # latest per-book breakdown
    python status_cli.py books 71 --sort volume # sort books by daily_volume
    python status_cli.py books 71 --top 10      # worst 10 books by weighted_kappa
    python status_cli.py books-history 71 --minutes 30
    python status_cli.py status --json          # raw JSON (pipe to jq)

Server URL resolves from --url, else $STATUS_SERVER_URL, else
http://localhost:9800. Override host/port with --host/--port.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import os
import sys
from typing import Any, Dict, List, Optional

import requests

DEFAULT_URL = "http://localhost:9800"

STATUS_COLUMNS = [
    ("kappa", "kappa"),
    ("real_pnl", "real_pnl"),
    ("unrl_pnl", "unrl_pnl"),
    ("volume", "volume"),
    ("vol_mkr", "vol_mkr"),
    ("vol_tkr", "vol_tkr"),
    ("book_count", "books"),
    ("hot_book_count", "hot"),
    ("pnl_books_pos", "pnl+"),
    ("pnl_books_neg", "pnl-"),
]

BOOK_GAUGES_DEFAULT = [
    "weighted_kappa", "kappa", "daily_volume", "realized_pnl",
    "fees_maker_rate", "fees_taker_rate", "activity_factor",
]


def base_url(args) -> str:
    if args.url:
        return args.url.rstrip("/")
    if args.host or args.port:
        host = args.host or "localhost"
        port = args.port or 9800
        return f"http://{host}:{port}"
    return os.getenv("STATUS_SERVER_URL", DEFAULT_URL).rstrip("/")


def get(url: str, path: str, params: Optional[dict] = None, timeout: float = 15.0) -> Any:
    try:
        r = requests.get(url + path, params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        sys.exit(f"error: could not reach status server at {url}{path}: {e}")
    if r.status_code >= 400:
        detail = ""
        try:
            detail = r.json().get("detail", "")
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        sys.exit(f"error: HTTP {r.status_code} for {path}: {detail}")
    return r.json()


def fmt(v: Any, width: int = 10) -> str:
    """Compact numeric formatting (M / k), right-justified."""
    if v is None or v == "":
        return "-".rjust(width)
    if isinstance(v, bool):
        return str(v).rjust(width)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v).rjust(width)
    if f == int(f) and abs(f) < 100000:
        s = str(int(f))
    elif abs(f) >= 1_000_000:
        s = f"{f/1e6:.2f}M"
    elif abs(f) >= 10_000:
        s = f"{f/1e3:.1f}k"
    elif abs(f) < 0.001 and f != 0:
        s = f"{f:.2e}"
    else:
        s = f"{f:.4f}"
    return s.rjust(width)


def print_table(headers: List[str], rows: List[List[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.rjust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(str(cell).rjust(widths[i]) for i, cell in enumerate(row)))


# ---- Commands ----
def cmd_health(url, args):
    data = get(url, "/health")
    if args.json:
        print(jsonlib.dumps(data, indent=2))
        return
    for k, v in data.items():
        print(f"{k:>22}: {v}")


def cmd_uids(url, args):
    data = get(url, "/uids")
    if args.json:
        print(jsonlib.dumps(data, indent=2))
        return
    print(f"{data['count']} tracked UIDs:")
    print(", ".join(str(u) for u in data["uids"]))


def cmd_status(url, args):
    if args.uid is not None:
        data = get(url, f"/status/{args.uid}")
        if args.json:
            print(jsonlib.dumps(data, indent=2))
            return
        print(f"UID {data['uid']}  ·  snapshot {data['snapshot_at']}"
              + ("  (books_stale)" if data.get("books_stale") else ""))
        m = data["metrics"]
        for field, label in STATUS_COLUMNS:
            print(f"{label:>10}: {fmt(m.get(field)).strip()}")
        return

    data = get(url, "/status")
    if args.json:
        print(jsonlib.dumps(data, indent=2))
        return
    print(f"{data['count']} miners  ·  snapshot {data['snapshot_at']}"
          + ("  (books_stale)" if data.get("books_stale") else ""))
    headers = ["uid"] + [label for _, label in STATUS_COLUMNS]
    rows = []
    metrics = data["metrics"]
    for uid in sorted(metrics, key=lambda x: int(x)):
        m = metrics[uid]
        rows.append([uid] + [fmt(m.get(f)).strip() for f, _ in STATUS_COLUMNS])
    print_table(headers, rows)


def _book_rows(books: Dict[str, dict], gauges: List[str], sort_key: Optional[str],
               top: Optional[int]) -> List[List[str]]:
    items = list(books.items())
    if sort_key == "volume":
        items.sort(key=lambda kv: (kv[1].get("daily_volume") is None, -(kv[1].get("daily_volume") or 0)))
    elif sort_key == "pnl":
        items.sort(key=lambda kv: (kv[1].get("realized_pnl") is None, kv[1].get("realized_pnl") or 0))
    else:  # weighted_kappa, worst first (matches dashboard ordering)
        items.sort(key=lambda kv: (kv[1].get("weighted_kappa") is None, kv[1].get("weighted_kappa")
                                    if kv[1].get("weighted_kappa") is not None else float("inf")))
    if top:
        items = items[:top]
    rows = []
    for book_id, g in items:
        rows.append([book_id] + [fmt(g.get(name)).strip() for name in gauges])
    return rows


def cmd_books(url, args):
    data = get(url, f"/books/{args.uid}")
    if args.json:
        print(jsonlib.dumps(data, indent=2))
        return
    gauges = data.get("gauges") or BOOK_GAUGES_DEFAULT
    books = data["books"]
    print(f"UID {data['uid']}  ·  {data['book_count']} books  ·  snapshot {data['snapshot_at']}")
    headers = ["book"] + gauges
    rows = _book_rows(books, gauges, args.sort, args.top)
    print_table(headers, rows)


def cmd_books_history(url, args):
    data = get(url, f"/books/{args.uid}/history", params={"minutes": args.minutes} if args.minutes else None)
    if args.json:
        print(jsonlib.dumps(data, indent=2))
        return
    snaps = data["snapshots"]
    print(f"UID {data['uid']}  ·  {data['count']} snapshots"
          + (f"  (last {args.minutes} min)" if args.minutes else "  (up to 3h)"))
    if not snaps:
        print("(no history yet)")
        return
    gauges = data.get("gauges") or BOOK_GAUGES_DEFAULT
    if args.book is not None:
        # time-series of one book across snapshots
        print(f"book {args.book}:")
        headers = ["snapshot_at"] + gauges
        rows = []
        for s in snaps:
            g = s["books"].get(str(args.book))
            if g is None:
                continue
            rows.append([s["snapshot_at"]] + [fmt(g.get(name)).strip() for name in gauges])
        print_table(headers, rows)
    else:
        # one line per snapshot: count + aggregate volume/pnl
        headers = ["snapshot_at", "books", "sum_volume", "sum_pnl"]
        rows = []
        for s in snaps:
            bs = s["books"].values()
            vol = sum((b.get("daily_volume") or 0) for b in bs)
            pnl = sum((b.get("realized_pnl") or 0) for b in bs)
            rows.append([s["snapshot_at"], str(len(s["books"])), fmt(vol).strip(), fmt(pnl).strip()])
        print_table(headers, rows)
        print("\nTip: add --book <id> to see one book's time-series across snapshots.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Terminal client for the SN79 miner status server.")
    p.add_argument("--url", help="Full base URL (default $STATUS_SERVER_URL or http://localhost:9800)")
    p.add_argument("--host", help="Server host (with --port)")
    p.add_argument("--port", type=int, help="Server port (default 9800)")
    p.add_argument("--json", action="store_true", help="Print raw JSON")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Server health / config")
    sub.add_parser("uids", help="List tracked UIDs")

    ps = sub.add_parser("status", help="Latest status (all miners, or one with UID)")
    ps.add_argument("uid", nargs="?", type=int, help="Optional single UID")

    pb = sub.add_parser("books", help="Latest per-book breakdown for one miner")
    pb.add_argument("uid", type=int)
    pb.add_argument("--sort", choices=["weighted_kappa", "volume", "pnl"], default="weighted_kappa")
    pb.add_argument("--top", type=int, help="Show only first N books after sorting")

    ph = sub.add_parser("books-history", help="Per-book history (<=3h) for one miner")
    ph.add_argument("uid", type=int)
    ph.add_argument("--minutes", type=int, help="Last N minutes (1-180)")
    ph.add_argument("--book", type=int, help="Show one book's time-series instead of per-snapshot summary")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    url = base_url(args)
    dispatch = {
        "health": cmd_health,
        "uids": cmd_uids,
        "status": cmd_status,
        "books": cmd_books,
        "books-history": cmd_books_history,
    }
    dispatch[args.command](url, args)


if __name__ == "__main__":
    main()
