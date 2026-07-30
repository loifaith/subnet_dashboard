#!/usr/bin/env python3
"""
SN-79 UID status reporter.

Fetch the full scoring/volume/responsiveness status of ANY UID(s) on subnet
79 from the TAOS metrics datasource (same source as monitoring.py) — works
for foreign miners too, since metrics are keyed by agent_id, not coldkey.

Usage:
    python uid_status_checker.py 245                 # one uid, summary
    python uid_status_checker.py 245 150 219         # several uids, summary table
    python uid_status_checker.py 245 --books         # add per-book breakdown
    python uid_status_checker.py 245 --chain         # add on-chain owner/immunity info

Summary gauges come from miner_gauges (per-UID aggregates published by the
validator); --books pulls agent_gauges (per-book) and prints every book,
sorted worst -> best by weighted_kappa, with a totals row at the bottom.

Exit codes: 0 ok, 1 error.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

import cloudscraper

# Same datasource + reporting validator as monitoring.py
API_BASE = "https://taos.simulate.trading/api/datasources/proxy/uid/c7665619-8b04-4227-9882-a44e8dad57a6/api/v1"
WALLET = "5EWwdZB7qCCMaAso5Mzcks4UUcPxKYvpAj32t5Mg1v6HSxoF"
NETUID = 79

# (gauge, short column header) — order defines the table
SUMMARY_GAUGES = [
    ("score",                    "score"),
    ("kappa_score",              "kappa"),
    ("kappa_penalty",            "penalty"),
    ("pnl_score",                "pnl_sc"),
    ("gentrx_score",             "gentrx"),
    ("incentive",                "incent"),
    ("emission",                 "emiss"),
    ("total_realized_pnl",       "real_pnl"),
    ("pnl",                      "unrl_pnl"),   # Unrealized PnL (QUOTE) = total_inventory_value (mark-to-market of open inventory)
    ("total_daily_volume",       "vol_day"),
    ("total_daily_maker_volume", "vol_mkr"),
    ("total_daily_taker_volume", "vol_tkr"),
    ("total_daily_self_volume",  "vol_self"),
    ("requests",                 "reqs"),
    ("timeouts",                 "tmout"),
    ("failures",                 "fails"),
]

BOOK_GAUGES = ["weighted_kappa", "kappa", "daily_volume", "realized_pnl",
               "fees_maker_rate", "fees_taker_rate", "activity_factor"]

BOOK_VOLUME_LIMIT = 500_000
BOOK_VOLUME_WARN_RATIO = 0.98

# Subset shown in dashboard agent column (6 bulk queries for all UIDs)
AGENT_DISPLAY_GAUGES = [
    ("kappa_score", "kappa"),
    ("total_realized_pnl", "real_pnl"),
    ("pnl", "unrl_pnl"),
    ("total_daily_volume", "volume"),
    ("total_daily_maker_volume", "vol_mkr"),
    ("total_daily_taker_volume", "vol_tkr"),
]


def create_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )


def query_latest(scraper, metric: str, gauge_label: str, gauge: str,
                 uid: int) -> Dict[str, float]:
    """Return {series_key: latest_value} for one gauge of one uid.

    series_key is book_id for agent_gauges, "" for miner_gauges. Queries a
    10-minute window with one retry so a between-scrapes instant can't read
    as missing (same hardening as monitoring.py).
    """
    q = (f'{metric}{{wallet=~"{WALLET}", agent_id=~"{uid}", '
         f'{gauge_label}="{gauge}", netuid="{NETUID}"}}')
    for attempt in range(2):
        now = int(time.time())
        r = scraper.post(
            f"{API_BASE}/query_range",
            data={"query": q, "start": str(now - 600), "end": str(now), "step": "60"},
            timeout=30,
        )
        if r.status_code == 200:
            out: Dict[str, float] = {}
            for series in r.json().get("data", {}).get("result", []):
                values = series.get("values", [])
                if not values:
                    continue
                key = series.get("metric", {}).get("book_id", "")
                try:
                    out[key] = float(values[-1][1])
                except (TypeError, ValueError):
                    continue
            if out:
                return out
        if attempt == 0:
            time.sleep(2)
    return {}


def query_miner_gauge_all_uids(scraper, gauge: str) -> Dict[int, float]:
    """Return {uid: latest_value} for one miner gauge across all UIDs."""
    q = (f'miner_gauges{{wallet=~"{WALLET}", miner_gauge_name="{gauge}", '
         f'netuid="{NETUID}"}}')
    for attempt in range(2):
        now = int(time.time())
        r = scraper.post(
            f"{API_BASE}/query_range",
            data={"query": q, "start": str(now - 600), "end": str(now), "step": "60"},
            timeout=30,
        )
        if r.status_code == 200:
            out: Dict[int, float] = {}
            for series in r.json().get("data", {}).get("result", []):
                values = series.get("values", [])
                if not values:
                    continue
                agent_id = series.get("metric", {}).get("agent_id")
                if agent_id is None:
                    continue
                try:
                    uid = int(agent_id)
                    out[uid] = float(values[-1][1])
                except (TypeError, ValueError):
                    continue
            if out:
                return out
        if attempt == 0:
            time.sleep(2)
    return {}


def book_volume_warn_threshold() -> float:
    return BOOK_VOLUME_LIMIT * BOOK_VOLUME_WARN_RATIO


def query_agent_gauge_all_uids_books(scraper, gauge: str) -> Dict[int, Dict[str, float]]:
    """Return {uid: {book_id: latest_value}} for one agent gauge across all UIDs.

    This is a high-cardinality query (~33K series for one gauge). The TAOS
    datasource intermittently returns an empty 200 for it — most often on the
    first call from a fresh scraper (cold Cloudflare clearance). Retry several
    times with backoff so an empty result is a true "no data", not a transient
    miss. A coarse step keeps the response small (we only use the latest sample).
    """
    q = (f'agent_gauges{{wallet=~"{WALLET}", agent_gauge_name="{gauge}", '
         f'netuid="{NETUID}"}}')
    attempts = 4
    for attempt in range(attempts):
        now = int(time.time())
        r = scraper.post(
            f"{API_BASE}/query_range",
            data={"query": q, "start": str(now - 600), "end": str(now), "step": "300"},
            timeout=30,
        )
        if r.status_code == 200:
            out: Dict[int, Dict[str, float]] = {}
            for series in r.json().get("data", {}).get("result", []):
                values = series.get("values", [])
                if not values:
                    continue
                metric = series.get("metric", {})
                agent_id = metric.get("agent_id")
                if agent_id is None:
                    continue
                try:
                    uid = int(agent_id)
                    book_id = str(metric.get("book_id", ""))
                    out.setdefault(uid, {})[book_id] = float(values[-1][1])
                except (TypeError, ValueError):
                    continue
            if out:
                return out
        if attempt < attempts - 1:
            time.sleep(2 * (attempt + 1))
    return {}


def fetch_book_volume_warnings(scraper=None) -> Optional[Dict[int, Dict[str, Optional[float]]]]:
    """Per-UID book volume warning stats for the 500K / book daily limit.

    Returns None (not {}) when the upstream book query yields nothing, so the
    caller can tell "no books fetched" apart from "miner genuinely has 0 books"
    and avoid overwriting good data with zeros.
    """
    own_scraper = scraper is None
    if own_scraper:
        scraper = create_scraper()
    warn_at = book_volume_warn_threshold()
    books_by_uid = query_agent_gauge_all_uids_books(scraper, "daily_volume")
    if not books_by_uid:
        return None
    out: Dict[int, Dict[str, Optional[float]]] = {}
    for uid, books in books_by_uid.items():
        if not books:
            continue
        hot_count = sum(1 for value in books.values() if value >= warn_at)
        out[uid] = {
            "hot_book_count": hot_count,
            "book_count": len(books),
            "max_book_volume": max(books.values()),
        }
    return out


def fetch_pnl_books_counts(scraper=None) -> Optional[Dict[int, Dict[str, int]]]:
    """Per-UID count of books with positive vs negative realized_pnl.

    Returns None (not {}) when the upstream book query yields nothing, so the
    caller can avoid caching all-zero pnl-book counts on a transient miss.
    """
    own_scraper = scraper is None
    if own_scraper:
        scraper = create_scraper()
    books_by_uid = query_agent_gauge_all_uids_books(scraper, "realized_pnl")
    if not books_by_uid:
        return None
    out: Dict[int, Dict[str, int]] = {}
    for uid, books in books_by_uid.items():
        pnls = list(books.values())
        out[uid] = {
            "pnl_books_pos": sum(1 for p in pnls if p > 0),
            "pnl_books_neg": sum(1 for p in pnls if p < 0),
        }
    return out


def fetch_agent_display_metrics(
    scraper=None, uids: Optional[List[int]] = None
) -> Dict[str, Dict[str, Optional[float]]]:
    """Return {uid: {kappa, real_pnl, ...}} for dashboard agent column."""
    own_scraper = scraper is None
    if own_scraper:
        scraper = create_scraper()
    uid_filter = set(uids) if uids else None
    rows: Dict[int, Dict[str, Optional[float]]] = {}
    for gauge, field in AGENT_DISPLAY_GAUGES:
        for uid, value in query_miner_gauge_all_uids(scraper, gauge).items():
            if uid_filter is not None and uid not in uid_filter:
                continue
            rows.setdefault(uid, {})[field] = value
    if uid_filter is not None:
        target_uids = uid_filter
    else:
        target_uids = rows.keys()
    fields = [field for _, field in AGENT_DISPLAY_GAUGES]
    return {
        str(uid): {field: rows.get(uid, {}).get(field) for field in fields}
        for uid in target_uids
    }


def fetch_agent_display_metrics_with_warnings(
    scraper=None, uids: Optional[List[int]] = None
) -> Dict[str, Dict[str, Optional[float]]]:
    """Display metrics plus per-UID hot book counts (>= 98% of 500K)."""
    own_scraper = scraper is None
    if own_scraper:
        scraper = create_scraper()
    metrics = fetch_agent_display_metrics(scraper, uids)
    warnings = fetch_book_volume_warnings(scraper)
    pnl_books = fetch_pnl_books_counts(scraper)
    for uid_str in list(metrics.keys()):
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        # Leave book fields absent when the upstream query failed (None) so a
        # transient miss doesn't get written as real zeros; the cache layer
        # carries forward the last known values instead.
        if warnings is not None:
            warn = warnings.get(uid)
            if warn:
                metrics[uid_str].update(warn)
            else:
                metrics[uid_str]["hot_book_count"] = 0
                metrics[uid_str]["book_count"] = 0
                metrics[uid_str]["max_book_volume"] = None
        if pnl_books is not None:
            pb = pnl_books.get(uid, {})
            metrics[uid_str]["pnl_books_pos"] = pb.get("pnl_books_pos", 0)
            metrics[uid_str]["pnl_books_neg"] = pb.get("pnl_books_neg", 0)
    return metrics


def fetch_uid_registration_info(netuid: int = NETUID) -> Tuple[Dict[str, dict], Optional[int]]:
    """Return ({uid: registration fields}, current_block) from on-chain metagraph."""
    import gc
    import bittensor as bt

    mg = None
    try:
        metagraph_cls = getattr(bt, "metagraph", None) or bt.Metagraph
        mg = metagraph_cls(netuid=netuid, network="finney", lite=True)
        mg.sync()
        try:
            current_block = int(mg.block.item())
        except Exception:
            try:
                current_block = int(mg.block)
            except Exception:
                current_block = None

        bar = getattr(mg, "block_at_registration", None)
        incentives = getattr(mg, "incentive", None)
        if incentives is None:
            incentives = getattr(mg, "I", None)
        if bar is None or current_block is None:
            return {}, current_block

        block_time_sec = 12
        now = datetime.now(timezone.utc)
        tokyo = ZoneInfo("Asia/Tokyo")
        out: Dict[str, dict] = {}
        for uid in range(mg.n):
            try:
                reg_block = int(bar[uid].item()) if hasattr(bar[uid], "item") else int(bar[uid])
            except Exception:
                continue
            blocks_since = max(0, current_block - reg_block)
            reg_date_utc = now - timedelta(seconds=blocks_since * block_time_sec)
            reg_date_tokyo = reg_date_utc.astimezone(tokyo)
            row = {
                "reg_block": reg_block,
                "reg_date_utc_iso": reg_date_utc.isoformat(),
                "reg_date_iso": reg_date_utc.isoformat(),
                "reg_date": reg_date_tokyo.strftime("%Y-%m-%d %H:%M:%S"),
                "reg_date_tz": "Asia/Tokyo",
                "blocks_since_registration": blocks_since,
            }
            if incentives is not None:
                try:
                    inc_val = incentives[uid]
                    row["incentive"] = float(inc_val.item()) if hasattr(inc_val, "item") else float(inc_val)
                except Exception:
                    pass
            out[str(uid)] = row
        return out, current_block
    finally:
        try:
            if mg is not None:
                sub = getattr(mg, "subtensor", None)
                if sub is not None:
                    substrate = getattr(sub, "substrate", None) or getattr(sub, "_substrate", None)
                    if substrate is not None and hasattr(substrate, "close"):
                        substrate.close()
                del mg
        except Exception:
            pass
        gc.collect()


def fetch_summary(scraper, uid: int) -> Dict[str, Optional[float]]:
    row: Dict[str, Optional[float]] = {}
    for gauge, _ in SUMMARY_GAUGES:
        vals = query_latest(scraper, "miner_gauges", "miner_gauge_name", gauge, uid)
        row[gauge] = next(iter(vals.values())) if vals else None
    return row


def fetch_books(scraper, uid: int) -> Dict[str, Dict[str, float]]:
    """{book_id: {gauge: value}} for the per-book breakdown."""
    books: Dict[str, Dict[str, float]] = {}
    for gauge in BOOK_GAUGES:
        for book_id, v in query_latest(
                scraper, "agent_gauges", "agent_gauge_name", gauge, uid).items():
            books.setdefault(book_id, {})[gauge] = v
    return books


def fmt(v: Optional[float], width: int = 10) -> str:
    if v is None:
        return "n/a".rjust(width)
    if abs(v) >= 1_000_000:
        return f"{v/1e6:.2f}M".rjust(width)
    if abs(v) >= 10_000:
        return f"{v/1e3:.1f}k".rjust(width)
    return f"{v:.4f}".rjust(width)


def print_summary(rows: Dict[int, Dict[str, Optional[float]]]) -> None:
    headers = [h for _, h in SUMMARY_GAUGES]
    print(f"\n{'uid':>5} " + " ".join(h.rjust(10) for h in headers))
    for uid, row in rows.items():
        if all(v is None for v in row.values()):
            print(f"{uid:>5} " + "-- no metrics reported (uid inactive or never scored) --")
            continue
        print(f"{uid:>5} " + " ".join(fmt(row[g]) for g, _ in SUMMARY_GAUGES))


def order_books(books: Dict[str, Dict[str, float]]):
    def wk(item):
        v = item[1].get("weighted_kappa")
        try:
            num = int(item[0])
        except ValueError:
            num = 0
        return (v if v is not None else float("inf"), num)

    return sorted(books.items(), key=wk)


def build_uid_status_response(uid: int, include_books: bool = False, scraper=None) -> dict:
    """Return JSON-serializable UID status for dashboard/API use."""
    own_scraper = scraper is None
    if own_scraper:
        scraper = create_scraper()
    summary = fetch_summary(scraper, uid)
    active = not all(v is None for v in summary.values())
    try:
        from sim_uid import get_sim_id
        sim_id = get_sim_id()
    except Exception:
        sim_id = None
    result = {
        "uid": uid,
        "active": active,
        "sim_id": sim_id,
        "summary": [
            {"gauge": gauge, "label": label, "value": summary.get(gauge)}
            for gauge, label in SUMMARY_GAUGES
        ],
    }
    if include_books:
        books = fetch_books(scraper, uid)
        ordered = order_books(books)
        book_rows = []
        for book_id, gauges in ordered:
            book_rows.append({
                "book_id": book_id,
                "weighted_kappa": gauges.get("weighted_kappa"),
                "kappa": gauges.get("kappa"),
                "daily_volume": gauges.get("daily_volume"),
                "realized_pnl": gauges.get("realized_pnl"),
                "fees_maker_rate": gauges.get("fees_maker_rate"),
                "fees_taker_rate": gauges.get("fees_taker_rate"),
                "activity_factor": gauges.get("activity_factor"),
            })
        pnls = [g.get("realized_pnl", 0.0) for _, g in ordered]
        sum_daily_volume = sum(g.get("daily_volume", 0.0) for _, g in ordered)
        sum_realized_pnl = sum(pnls)
        result["books"] = book_rows
        result["book_stats"] = {
            "count": len(ordered),
            "without_weighted_kappa": sum(1 for _, g in ordered if "weighted_kappa" not in g),
            "positive_pnl_books": sum(1 for p in pnls if p > 0),
            "negative_pnl_books": sum(1 for p in pnls if p < 0),
            "total_daily_volume": sum_daily_volume,
            "total_realized_pnl": sum_realized_pnl,
            "pnl_volume_ratio_pct": (
                100 * sum_realized_pnl / sum_daily_volume if sum_daily_volume else 0.0
            ),
        }
    return result


def print_books(uid: int, books: Dict[str, Dict[str, float]]) -> None:
    if not books:
        print(f"\nuid {uid}: no per-book metrics found")
        return
    ordered = order_books(books)

    n = len(ordered)
    no_kappa = sum(1 for _, g in ordered if "weighted_kappa" not in g)
    print(f"\nuid {uid}: {n} books reporting, {no_kappa} without weighted_kappa "
          f"(inactive-book tolerance is 37.5% of books)")
    print(f"{'book':>6} {'w_kappa':>10} {'kappa':>10} {'vol_day':>10} "
          f"{'real_pnl':>10} {'mkr_fee%':>9} {'tkr_fee%':>9} {'act_f':>8}")
    for book_id, g in ordered:
        print(f"{book_id:>6} "
              f"{fmt(g.get('weighted_kappa'))} "
              f"{fmt(g.get('kappa'))} "
              f"{fmt(g.get('daily_volume'))} "
              f"{fmt(g.get('realized_pnl'))} "
              f"{fmt((g.get('fees_maker_rate') or 0) * 100, 9)} "
              f"{fmt((g.get('fees_taker_rate') or 0) * 100, 9)} "
              f"{fmt(g.get('activity_factor'), 8)}")
    pnls = [g.get("realized_pnl", 0.0) for _, g in ordered]
    pos = sum(1 for p in pnls if p > 0)
    neg = sum(1 for p in pnls if p < 0)
    sum_daily_volume = sum(g.get('daily_volume', 0.0) for _, g in ordered)
    sum_realized_pnl = sum(pnls)
    print(f"{'TOTAL':>6} {'':>10} {'':>10} "
          f"{fmt(sum_daily_volume)} "
          f"{fmt(sum_realized_pnl)} "
          f"= {100 * sum_realized_pnl / sum_daily_volume if sum_daily_volume else 0:.4f} %  "
          f"pnl books: {pos}+ / {neg}- "
          f"(weighted_kappa ASC)")


def print_chain_info(uids: List[int]) -> None:
    import bittensor as bt
    print("\nLoading metagraph (slow)...")
    metagraph_cls = getattr(bt, "metagraph", None) or bt.Metagraph
    mg = metagraph_cls(netuid=NETUID, network="finney")
    mg.sync()
    try:
        current_block = mg.block.item()
    except Exception:
        current_block = None
    for uid in uids:
        if uid >= mg.n:
            print(f"uid {uid}: not on metagraph (n={mg.n})")
            continue
        line = (f"uid {uid}: hotkey={mg.hotkeys[uid]} coldkey={mg.coldkeys[uid]} "
                f"stake={float(mg.stake[uid]):.4f}")
        reg_block = None
        bar = getattr(mg, "block_at_registration", None)
        if bar is not None:
            try:
                reg_block = int(bar[uid])
            except Exception:
                reg_block = None
        if reg_block and current_block:
            immune_left = reg_block + 7200 - current_block
            if immune_left > 0:
                line += f" | IMMUNE for ~{immune_left*12/3600:.1f}h more"
            else:
                line += " | immunity expired"
        print(line)


def main():
    parser = argparse.ArgumentParser(description="SN-79 UID status from TAOS metrics")
    parser.add_argument("uids", nargs="+", type=int, help="UID(s) to inspect")
    parser.add_argument("--books", action="store_true", help="per-book breakdown")
    parser.add_argument("--chain", action="store_true", help="on-chain owner/immunity info (slow)")
    args = parser.parse_args()

    scraper = create_scraper()

    try:
        rows = {uid: fetch_summary(scraper, uid) for uid in args.uids}
        print_summary(rows)
        if args.books:
            for uid in args.uids:
                print_books(uid, fetch_books(scraper, uid))
        if args.chain:
            print_chain_info(args.uids)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
