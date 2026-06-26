#!/usr/bin/env python3
"""
SN-79 DIS fee-regime checker.

Pulls the live per-book maker vs taker fee rates from the TAOS metrics
datasource and reports which side is currently cheaper (and whether either side
is being rebated). Fee rates float with order flow under the Dynamic Incentive
Structure, so this is a point-in-time snapshot.

Sign convention: POSITIVE = that side PAYS the fee; NEGATIVE = that side is
REBATED (paid to trade).

Usage:
    python fee_rate.py            # current regime, summary
    python fee_rate.py --books    # + per-book detail (worst/best books)
    python fee_rate.py 118 77     # sample specific UIDs (default: a few live ones)

Exit codes: 0 ok, 1 datasource error.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time
from typing import Dict, List, Optional, Tuple

import uid_status_checker as usc

# Fees are per-book and market-wide (same for every participant on a book), so
# any handful of UIDs that are active across the books gives the full picture.
DEFAULT_SAMPLE_UIDS = [118, 77, 161, 228, 16]


def fetch_book_rates_sampled(
    scraper, gauge: str, uids: List[int]
) -> Dict[str, float]:
    """Return {book_id: rate} for one fee gauge across sampled UIDs."""
    pat = "|".join(str(u) for u in uids)
    q = (
        f'agent_gauges{{wallet=~"{usc.WALLET}", agent_id=~"({pat})", '
        f'agent_gauge_name="{gauge}", netuid="{usc.NETUID}"}}'
    )
    now = int(time.time())
    for attempt in range(3):
        r = scraper.post(
            f"{usc.API_BASE}/query_range",
            data={"query": q, "start": str(now - 600), "end": str(now), "step": "60"},
            timeout=45,
        )
        if r.status_code == 200:
            out: Dict[str, float] = {}
            for series in r.json().get("data", {}).get("result", []):
                bid = series.get("metric", {}).get("book_id")
                values = series.get("values", [])
                if bid is None or not values:
                    continue
                try:
                    out[str(bid)] = float(values[-1][1])
                except (TypeError, ValueError):
                    continue
            if out:
                return out
        time.sleep(2)
    return {}


def fetch_book_rates_all(scraper, gauge: str) -> Dict[str, float]:
    """Return deduped {book_id: rate} from all UIDs reporting this gauge."""
    books_by_uid = usc.query_agent_gauge_all_uids_books(scraper, gauge)
    out: Dict[str, float] = {}
    for uid_books in books_by_uid.values():
        for book_id, rate in uid_books.items():
            out[str(book_id)] = rate
    return out


def fetch_fee_rates(
    scraper=None, uids: Optional[List[int]] = None
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Return (maker_rates, taker_rates) keyed by book_id."""
    own_scraper = scraper is None
    if own_scraper:
        scraper = usc.create_scraper()
    if uids:
        maker = fetch_book_rates_sampled(scraper, "fees_maker_rate", uids)
        taker = fetch_book_rates_sampled(scraper, "fees_taker_rate", uids)
    else:
        maker = fetch_book_rates_all(scraper, "fees_maker_rate")
        taker = fetch_book_rates_all(scraper, "fees_taker_rate")
    return maker, taker


def fetch_uid_weighted_fees(
    scraper=None,
    maker_by_book: Optional[Dict[str, float]] = None,
    taker_by_book: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict[str, float]]:
    """Volume-weighted maker/taker fee rates per UID across active books."""
    own_scraper = scraper is None
    if own_scraper:
        scraper = usc.create_scraper()
    if maker_by_book is None or taker_by_book is None:
        maker_by_book, taker_by_book = fetch_fee_rates(scraper)
    volumes = usc.query_agent_gauge_all_uids_books(scraper, "daily_volume")
    out: Dict[str, Dict[str, float]] = {}
    for uid, books in volumes.items():
        m_num = m_den = t_num = t_den = 0.0
        for book_id, vol in books.items():
            if vol <= 0:
                continue
            book_key = str(book_id)
            m_rate = maker_by_book.get(book_key)
            t_rate = taker_by_book.get(book_key)
            if m_rate is not None:
                m_num += vol * m_rate
                m_den += vol
            if t_rate is not None:
                t_num += vol * t_rate
                t_den += vol
        row: Dict[str, float] = {}
        if m_den > 0:
            row["fee_mkr"] = m_num / m_den
            row["vol_mkr"] = m_den
        if t_den > 0:
            row["fee_tkr"] = t_num / t_den
            row["vol_tkr"] = t_den
        if row:
            out[str(uid)] = row
    return out


def aggregate_order_fees(
    uid_fees: Dict[str, Dict[str, float]],
) -> Optional[Dict[str, float]]:
    """Network-wide maker/taker fees weighted by order volume across UIDs."""
    m_num = m_den = t_num = t_den = 0.0
    m_sum = t_sum = 0.0
    m_count = t_count = 0
    for row in uid_fees.values():
        fee_mkr = row.get("fee_mkr")
        if fee_mkr is not None:
            vol_mkr = row.get("vol_mkr")
            if vol_mkr and vol_mkr > 0:
                m_num += fee_mkr * vol_mkr
                m_den += vol_mkr
            else:
                m_sum += fee_mkr
                m_count += 1
        fee_tkr = row.get("fee_tkr")
        if fee_tkr is not None:
            vol_tkr = row.get("vol_tkr")
            if vol_tkr and vol_tkr > 0:
                t_num += fee_tkr * vol_tkr
                t_den += vol_tkr
            else:
                t_sum += fee_tkr
                t_count += 1
    out: Dict[str, float] = {}
    if m_den > 0:
        out["maker"] = m_num / m_den
    elif m_count > 0:
        out["maker"] = m_sum / m_count
    if t_den > 0:
        out["taker"] = t_num / t_den
    elif t_count > 0:
        out["taker"] = t_sum / t_count
    return out or None


def pct(x: float) -> str:
    return f"{x * 100:+.4f}%"


def build_fee_regime_summary(
    maker: Dict[str, float], taker: Dict[str, float]
) -> dict:
    """Structured summary for CLI or API use."""
    mkv = list(maker.values())
    tkv = list(taker.values())
    if not mkv or not tkv:
        return {"error": "no fee data from datasource"}

    mk_med, tk_med = st.median(mkv), st.median(tkv)
    mk_reb = sum(1 for v in mkv if v < 0)
    tk_reb = sum(1 for v in tkv if v < 0)
    cheaper = "TAKER" if tk_med < mk_med else "MAKER"
    gap = abs(mk_med - tk_med)

    if mk_med < 0 and tk_med >= 0:
        regime = "MAKER-REBATE"
    elif tk_med < 0 and mk_med >= 0:
        regime = "TAKER-REBATE"
    else:
        regime = "BOTH_PAY"

    return {
        "book_count": len(maker),
        "maker": {
            "median": mk_med,
            "min": min(mkv),
            "max": max(mkv),
            "rebate_books": mk_reb,
        },
        "taker": {
            "median": tk_med,
            "min": min(tkv),
            "max": max(tkv),
            "rebate_books": tk_reb,
        },
        "cheaper_side": cheaper,
        "median_gap": gap,
        "regime": regime,
        "regime_label": regime_label(regime),
        "regime_hint": regime_hint(regime),
        "books": {
            book_id: {"maker": maker[book_id], "taker": taker.get(book_id)}
            for book_id in sorted(maker, key=lambda b: int(b) if b.isdigit() else b)
        },
    }


def regime_label(regime: str) -> str:
    labels = {
        "MAKER-REBATE": "Maker Rebate",
        "TAKER-REBATE": "Taker Rebate",
        "BOTH_PAY": "Both Pay",
    }
    return labels.get(regime, regime)


def regime_hint(regime: str) -> str:
    hints = {
        "MAKER-REBATE": "Favours maker-heavy strategies",
        "TAKER-REBATE": "Favours taking liquidity",
        "BOTH_PAY": "Neither side rebated on average",
    }
    return hints.get(regime, "")


FEE_HISTORY_DIR = os.getenv(
    "FEE_HISTORY_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fee_history"),
)


def _fee_history_path(day: Optional[str] = None) -> str:
    from datetime import date

    day = day or date.today().isoformat()
    return os.path.join(FEE_HISTORY_DIR, f"{day}.jsonl")


def append_fee_regime_history(
    summary: dict,
    uid_fees: Optional[Dict[str, Dict[str, float]]] = None,
) -> Optional[str]:
    """Append one global fee-regime snapshot (optional per-UID weighted fees)."""
    if summary.get("error"):
        return None
    from datetime import datetime, timezone

    books = summary.get("books") or {}
    payload = {k: v for k, v in summary.items() if k not in ("books",)}
    payload["snapshot_at"] = datetime.now(timezone.utc).isoformat()
    if books:
        payload["books"] = {
            book_id: {
                "m": info["maker"],
                "t": info.get("taker"),
            }
            for book_id, info in books.items()
            if info.get("maker") is not None
        }
    if uid_fees:
        payload["uid_fees"] = uid_fees
    os.makedirs(FEE_HISTORY_DIR, exist_ok=True)
    path = _fee_history_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return path


def _iter_fee_history_days(days: int, end_day: Optional[str] = None) -> List[str]:
    from datetime import date, timedelta

    end = date.fromisoformat(end_day) if end_day else date.today()
    return [(end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def load_fee_history_snapshots(
    days: int = 7,
    day: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    day_list = [day] if day else _iter_fee_history_days(max(1, days), day)
    snapshots: List[dict] = []
    for d in day_list:
        path = _fee_history_path(d)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("snapshot_at"):
                        snapshots.append(row)
        except OSError:
            continue
    snapshots.sort(key=lambda row: row.get("snapshot_at") or "")
    if limit is not None and limit > 0:
        snapshots = snapshots[-limit:]
    return snapshots


def build_fee_history(
    days: int = 7,
    day: Optional[str] = None,
    uid: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict:
    snapshots = load_fee_history_snapshots(days=days, day=day, limit=limit)
    global_points: List[dict] = []
    book_points: List[dict] = []
    uid_points: List[dict] = []
    uid_str = str(uid) if uid is not None else None
    for snap in snapshots:
        global_points.append(
            {
                "snapshot_at": snap.get("snapshot_at"),
                "regime": snap.get("regime"),
                "regime_label": snap.get("regime_label"),
                "maker_median": (snap.get("maker") or {}).get("median"),
                "taker_median": (snap.get("taker") or {}).get("median"),
                "cheaper_side": snap.get("cheaper_side"),
                "median_gap": snap.get("median_gap"),
            }
        )
        books_raw = snap.get("books")
        if isinstance(books_raw, dict) and books_raw:
            books: Dict[str, dict] = {}
            for book_id, info in books_raw.items():
                if not isinstance(info, dict):
                    continue
                maker = info.get("maker", info.get("m"))
                taker = info.get("taker", info.get("t"))
                if maker is not None or taker is not None:
                    books[str(book_id)] = {"maker": maker, "taker": taker}
            if books:
                book_points.append(
                    {
                        "snapshot_at": snap.get("snapshot_at"),
                        "books": books,
                    }
                )
        if uid_str:
            fees = (snap.get("uid_fees") or {}).get(uid_str)
            if fees:
                uid_points.append(
                    {
                        "snapshot_at": snap.get("snapshot_at"),
                        **fees,
                    }
                )
    result = {
        "days": days if not day else 1,
        "day": day,
        "point_count": len(global_points),
        "global_points": global_points,
        "book_point_count": len(book_points),
        "book_points": book_points,
    }
    if uid is not None:
        result["uid"] = uid
        result["uid_point_count"] = len(uid_points)
        result["uid_points"] = uid_points
    return result


def list_fee_history_days(max_days: int = 30) -> List[str]:
    if not os.path.isdir(FEE_HISTORY_DIR):
        return []
    days: List[str] = []
    for name in os.listdir(FEE_HISTORY_DIR):
        if name.endswith(".jsonl"):
            days.append(name[: -len(".jsonl")])
    days.sort(reverse=True)
    return days[:max_days] if max_days > 0 else days


def print_summary(summary: dict, show_books: bool = False) -> None:
    if summary.get("error"):
        print(f"ERROR: {summary['error']}", file=sys.stderr)
        return

    mk = summary["maker"]
    tk = summary["taker"]
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    print(f"SN-79 DIS fee regime  |  {ts}  |  {summary['book_count']} books sampled")
    print("-" * 60)
    print(
        f"  MAKER fee: median {pct(mk['median']):>11}   "
        f"range [{pct(mk['min'])}, {pct(mk['max'])}]   "
        f"{mk['rebate_books']} books rebate"
    )
    print(
        f"  TAKER fee: median {pct(tk['median']):>11}   "
        f"range [{pct(tk['min'])}, {pct(tk['max'])}]   "
        f"{tk['rebate_books']} books rebate"
    )
    print("-" * 60)

    def side(v: float) -> str:
        return "REBATED" if v < 0 else "pays"

    gap = summary["median_gap"]
    cheaper = summary["cheaper_side"]
    print(
        f"  maker {side(mk['median'])} {pct(abs(mk['median'])).lstrip('+')}, "
        f"taker {side(tk['median'])} {pct(abs(tk['median'])).lstrip('+')}"
    )
    print(f"  => {cheaper} is the cheaper side by {pct(gap).lstrip('+')}")
    if summary["regime"] == "MAKER-REBATE":
        print("  => MAKER-REBATE regime (favours maker-heavy strategies)")
    elif summary["regime"] == "TAKER-REBATE":
        print("  => TAKER-REBATE / inverted regime (favours taking liquidity)")
    else:
        print("  => both pay; neither side rebated on average")

    if show_books:
        taker_books = [
            (book_id, info.get("taker"))
            for book_id, info in summary["books"].items()
            if info.get("taker") is not None
        ]
        print("\nper-book taker fee, worst (most expensive) -> best:")
        for book_id, rate in sorted(taker_books, key=lambda x: -x[1])[:10]:
            print(f"    book {book_id:>4}: taker {pct(rate)}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SN-79 DIS fee-regime checker")
    parser.add_argument(
        "uids",
        nargs="*",
        type=int,
        help="UIDs to sample (default: a few live UIDs)",
    )
    parser.add_argument(
        "--books",
        action="store_true",
        help="show per-book taker fee detail (worst -> best)",
    )
    parser.add_argument(
        "--all-uids",
        action="store_true",
        help="query all UIDs instead of the default sample set",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    uids = None if args.all_uids else (args.uids or DEFAULT_SAMPLE_UIDS)
    maker, taker = fetch_fee_rates(uids=uids)
    summary = build_fee_regime_summary(maker, taker)
    if summary.get("error"):
        print(f"ERROR: {summary['error']}", file=sys.stderr)
        return 1
    print_summary(summary, show_books=args.books)
    return 0


if __name__ == "__main__":
    sys.exit(main())
