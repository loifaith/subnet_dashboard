#!/usr/bin/env python3
"""
SN-79 real-time miner status server.

Polls the TAOS datasource every 30s for *my* registered miners (the UIDs that
have a run_miner_<uid>.json params file) and keeps a rolling 3-hour history in
memory + a CSV file. Other servers (the miners themselves) poll this service
over HTTP to get the latest status without each one hammering TAOS.

Per-book gauges (book_count / hot books / pnl books) come from the same flaky
range query that intermittently returns empty; when a cycle's per-book fetch
comes back empty we carry forward the last good values per UID instead of
serving zeros.

Endpoints:
    GET /health              -> {"status": "ok", ...}
    GET /uids                -> tracked UID list
    GET /status              -> latest snapshot for all tracked miners
    GET /status/{uid}        -> latest metrics for one miner
    GET /history?uid=&minutes=  -> rolling history (<= 3h)

Run:
    python status_server.py            # serves on 0.0.0.0:9800
Config via env: STATUS_SERVER_PORT, STATUS_POLL_INTERVAL, STATUS_RETENTION_SECONDS,
STATUS_CSV_PATH.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import uid_status_checker as usc
from miner_history import list_my_miner_uids

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("status_server")

# ---- Config ----
PORT = int(os.getenv("STATUS_SERVER_PORT", "9800"))
POLL_INTERVAL = int(os.getenv("STATUS_POLL_INTERVAL", "30"))
RETENTION_SECONDS = int(os.getenv("STATUS_RETENTION_SECONDS", str(3 * 60 * 60)))  # 3h
_HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.getenv("STATUS_CSV_PATH", os.path.join(_HERE, "status_history.csv"))
# Per-miner per-book history lives in its own directory: books_<uid>.csv
BOOKS_DIR = os.getenv("STATUS_BOOKS_DIR", os.path.join(_HERE, "books_history"))
# Per-book CSVs are append-only; rewrite to drop >3h rows every N cycles.
BOOK_PRUNE_EVERY = int(os.getenv("STATUS_BOOK_PRUNE_EVERY", "20"))  # ~10 min at 30s

AGG_FIELDS = ["kappa", "real_pnl", "unrl_pnl", "volume", "vol_mkr", "vol_tkr"]
BOOK_FIELDS = ["hot_book_count", "book_count", "max_book_volume"]
PNL_FIELDS = ["pnl_books_pos", "pnl_books_neg"]
ALL_FIELDS = AGG_FIELDS + BOOK_FIELDS + PNL_FIELDS
CSV_COLUMNS = ["snapshot_at", "epoch", "uid"] + ALL_FIELDS + ["books_stale"]

# Per-book gauges (full breakdown saved per miner). Reuses the checker's list.
BOOK_GAUGES = list(usc.BOOK_GAUGES)
BOOK_CSV_COLUMNS = ["snapshot_at", "epoch", "book_id"] + BOOK_GAUGES

# ---- State (single-process, guarded by an asyncio lock for writes) ----
_snapshots: List[Dict[str, Any]] = []          # rolling list of {epoch, snapshot_at, metrics}
_last_good_counts: Dict[str, Dict[str, Any]] = {}  # uid -> derived per-book counts (carry-forward)
_latest_books: Dict[str, Dict[str, Any]] = {}      # uid -> {snapshot_at, epoch, books:{book_id:{gauge:val}}}
_tracked_uids: List[int] = []
_cycle_count = 0
_state_lock = asyncio.Lock()


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    cutoff = now - RETENTION_SECONDS
    while _snapshots and _snapshots[0]["epoch"] < cutoff:
        _snapshots.pop(0)


def _derive_counts(books: Dict[str, Dict[str, float]], warn_at: float) -> Dict[str, Any]:
    """Aggregate per-book counts from a {book_id: {gauge: val}} breakdown."""
    vols = [b.get("daily_volume") for b in books.values() if b.get("daily_volume") is not None]
    pnls = [b.get("realized_pnl") for b in books.values() if b.get("realized_pnl") is not None]
    return {
        "book_count": len(books),
        "hot_book_count": sum(1 for v in vols if v >= warn_at),
        "max_book_volume": max(vols) if vols else None,
        "pnl_books_pos": sum(1 for p in pnls if p > 0),
        "pnl_books_neg": sum(1 for p in pnls if p < 0),
    }


def _fetch_all_blocking(uids: List[int]) -> Dict[str, Any]:
    """Blocking TAOS fetch: aggregate metrics + full per-book breakdown per miner.

    Fetches all per-book gauges once (one all-UIDs query per gauge) and derives
    the aggregate counts from them. Carries forward counts when the per-book
    query comes back empty for a miner so we never serve spurious zeros.
    """
    scraper = usc.create_scraper()
    try:
        metrics = usc.fetch_agent_display_metrics(scraper, uids)   # aggregate gauges (reliable)
        my = {str(u) for u in uids}
        warn_at = usc.book_volume_warn_threshold()

        # One range query per per-book gauge, across all UIDs, then filter to mine.
        per_uid_books: Dict[str, Dict[str, Dict[str, float]]] = {}
        for gauge in BOOK_GAUGES:
            by_uid = usc.query_agent_gauge_all_uids_books(scraper, gauge)
            for uid, books in by_uid.items():
                uid_str = str(uid)
                if uid_str not in my:
                    continue
                dest = per_uid_books.setdefault(uid_str, {})
                for book_id, val in books.items():
                    dest.setdefault(str(book_id), {})[gauge] = val

        fresh_books: Dict[str, Dict[str, Dict[str, float]]] = {}
        out: Dict[str, Dict[str, Any]] = {}
        fresh_count = 0
        for uid_str in sorted(my):
            agg = metrics.get(uid_str, {})
            merged: Dict[str, Any] = {f: agg.get(f) for f in AGG_FIELDS}
            books = per_uid_books.get(uid_str)
            if books:
                counts = _derive_counts(books, warn_at)
                _last_good_counts[uid_str] = counts
                fresh_books[uid_str] = books
                fresh_count += 1
            else:
                counts = _last_good_counts.get(
                    uid_str,
                    {f: (None if f == "max_book_volume" else 0) for f in BOOK_FIELDS + PNL_FIELDS},
                )
            merged.update(counts)
            out[uid_str] = merged

        books_stale = fresh_count < len(my)
        return {"metrics": out, "books": fresh_books, "books_stale": books_stale}
    finally:
        # cloudscraper is a requests.Session; close it so its connection pool
        # (sockets/fds) is released every cycle instead of leaking.
        try:
            scraper.close()
        except Exception:  # noqa: BLE001
            pass


def _write_csv() -> None:
    """Rewrite the whole CSV from the in-memory buffer (<= ~3h, small)."""
    tmp = CSV_PATH + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for snap in _snapshots:
            stale = "1" if snap.get("books_stale") else "0"
            for uid_str, row in snap["metrics"].items():
                writer.writerow(
                    [snap["snapshot_at"], int(snap["epoch"]), uid_str]
                    + [row.get(f) for f in ALL_FIELDS]
                    + [stale]
                )
    os.replace(tmp, CSV_PATH)


def _load_csv() -> None:
    """Warm-start the in-memory buffer (and last-good caches) from the CSV."""
    if not os.path.isfile(CSV_PATH):
        return
    now = time.time()
    cutoff = now - RETENTION_SECONDS
    by_epoch: Dict[int, Dict[str, Any]] = {}
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    epoch = int(r["epoch"])
                except (KeyError, TypeError, ValueError):
                    continue
                if epoch < cutoff:
                    continue
                snap = by_epoch.setdefault(
                    epoch,
                    {
                        "epoch": epoch,
                        "snapshot_at": r.get("snapshot_at"),
                        "books_stale": r.get("books_stale") == "1",
                        "metrics": {},
                    },
                )
                uid_str = r.get("uid")
                if not uid_str:
                    continue
                row: Dict[str, Any] = {}
                for fld in ALL_FIELDS:
                    val = _num(r.get(fld))
                    if fld in ("hot_book_count", "book_count", "pnl_books_pos", "pnl_books_neg"):
                        row[fld] = int(val) if val is not None else None
                    else:
                        row[fld] = val
                snap["metrics"][uid_str] = row
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not warm-start from CSV: %s", exc)
        return

    for epoch in sorted(by_epoch):
        _snapshots.append(by_epoch[epoch])
    _prune(now)

    # Seed last-good per-book counts from the newest snapshot with real data.
    count_fields = BOOK_FIELDS + PNL_FIELDS
    for snap in reversed(_snapshots):
        for uid_str, row in snap["metrics"].items():
            if uid_str not in _last_good_counts and row.get("book_count"):
                _last_good_counts[uid_str] = {f: row.get(f) for f in count_fields}
    logger.info("Warm-started %d snapshots from %s", len(_snapshots), CSV_PATH)


# ---- Per-book (per-miner) storage ----
def _books_csv_path(uid: Any) -> str:
    return os.path.join(BOOKS_DIR, f"books_{uid}.csv")


def _append_books(uid_str: str, books: Dict[str, Dict[str, float]],
                  snapshot_at: str, epoch: float) -> None:
    """Append this cycle's per-book rows for one miner (append-only)."""
    path = _books_csv_path(uid_str)
    new_file = not os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(BOOK_CSV_COLUMNS)
        for book_id, gauges in books.items():
            writer.writerow(
                [snapshot_at, int(epoch), book_id]
                + [gauges.get(g) for g in BOOK_GAUGES]
            )


def _prune_books_csv(now: Optional[float] = None) -> None:
    """Rewrite each per-miner CSV dropping rows older than the retention window."""
    now = now if now is not None else time.time()
    cutoff = now - RETENTION_SECONDS
    if not os.path.isdir(BOOKS_DIR):
        return
    for name in os.listdir(BOOKS_DIR):
        if not (name.startswith("books_") and name.endswith(".csv")):
            continue
        path = os.path.join(BOOKS_DIR, name)
        try:
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        except OSError:
            continue
        if not rows:
            continue
        header, body = rows[0], rows[1:]
        try:
            ei = header.index("epoch")
        except ValueError:
            continue
        kept = [r for r in body if len(r) > ei and r[ei].isdigit() and int(r[ei]) >= cutoff]
        if len(kept) == len(body):
            continue
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(kept)
        os.replace(tmp, path)


def _load_latest_books() -> None:
    """Seed in-memory latest per-book breakdown from the newest epoch in each CSV."""
    if not os.path.isdir(BOOKS_DIR):
        return
    for name in os.listdir(BOOKS_DIR):
        if not (name.startswith("books_") and name.endswith(".csv")):
            continue
        uid_str = name[len("books_") : -len(".csv")]
        path = os.path.join(BOOKS_DIR, name)
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = list(csv.DictReader(f))
        except OSError:
            continue
        if not reader:
            continue
        max_epoch = max((int(r["epoch"]) for r in reader if r.get("epoch", "").isdigit()), default=None)
        if max_epoch is None:
            continue
        books: Dict[str, Dict[str, float]] = {}
        snap_at = None
        for r in reader:
            if not r.get("epoch", "").isdigit() or int(r["epoch"]) != max_epoch:
                continue
            snap_at = r.get("snapshot_at")
            books[r["book_id"]] = {g: _num(r.get(g)) for g in BOOK_GAUGES}
        if books:
            _latest_books[uid_str] = {"snapshot_at": snap_at, "epoch": max_epoch, "books": books}


def _read_books_history(uid: Any, minutes: Optional[int]) -> List[Dict[str, Any]]:
    """Read a miner's per-book CSV and group rows by snapshot (epoch)."""
    path = _books_csv_path(uid)
    if not os.path.isfile(path):
        return []
    cutoff = time.time() - (minutes * 60) if minutes else 0
    by_epoch: Dict[int, Dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("epoch", "").isdigit():
                continue
            epoch = int(r["epoch"])
            if epoch < cutoff:
                continue
            snap = by_epoch.setdefault(
                epoch, {"snapshot_at": r.get("snapshot_at"), "epoch": epoch, "books": {}}
            )
            snap["books"][r["book_id"]] = {g: _num(r.get(g)) for g in BOOK_GAUGES}
    return [by_epoch[e] for e in sorted(by_epoch)]


def _write_books_blocking(books_by_uid: Dict[str, Any], snapshot_at: str, epoch: float,
                          do_prune: bool) -> None:
    os.makedirs(BOOKS_DIR, exist_ok=True)
    for uid_str, books in books_by_uid.items():
        _append_books(uid_str, books, snapshot_at, epoch)
        _latest_books[uid_str] = {"snapshot_at": snapshot_at, "epoch": int(epoch), "books": books}
    if do_prune:
        _prune_books_csv(epoch)


async def _poll_cycle() -> None:
    global _tracked_uids, _cycle_count
    uids = list_my_miner_uids()
    if not uids:
        logger.warning("No registered miners (run_miner_*.json) found; nothing to poll")
        _tracked_uids = []
        return
    _tracked_uids = uids
    result = await asyncio.to_thread(_fetch_all_blocking, uids)
    now = time.time()
    snapshot_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "epoch": now,
        "snapshot_at": snapshot_at,
        "books_stale": result["books_stale"],
        "metrics": result["metrics"],
    }
    _cycle_count += 1
    do_prune = (_cycle_count % BOOK_PRUNE_EVERY) == 0
    total_books = sum(len(b) for b in result["books"].values())
    async with _state_lock:
        _snapshots.append(snapshot)
        _prune(now)
        await asyncio.to_thread(_write_csv)
        if result["books"]:
            await asyncio.to_thread(
                _write_books_blocking, result["books"], snapshot_at, now, do_prune
            )
    logger.info(
        "Polled %d miners, %d books across %d miners (books_stale=%s, history=%d snapshots)",
        len(result["metrics"]),
        total_books,
        len(result["books"]),
        result["books_stale"],
        len(_snapshots),
    )


async def _poll_worker() -> None:
    while True:
        start = time.monotonic()
        try:
            await _poll_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.error("Poll cycle failed: %s", exc)
        elapsed = time.monotonic() - start
        await asyncio.sleep(max(1.0, POLL_INTERVAL - elapsed))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_csv()
    _load_latest_books()
    task = asyncio.create_task(_poll_worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="SN79 Miner Status Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    latest = _snapshots[-1] if _snapshots else None
    return {
        "status": "ok",
        "tracked_uids": len(_tracked_uids),
        "history_snapshots": len(_snapshots),
        "poll_interval_seconds": POLL_INTERVAL,
        "retention_seconds": RETENTION_SECONDS,
        "latest_snapshot_at": latest["snapshot_at"] if latest else None,
    }


@app.get("/uids")
async def uids():
    return {"uids": _tracked_uids, "count": len(_tracked_uids)}


@app.get("/status")
async def status():
    if not _snapshots:
        raise HTTPException(status_code=503, detail="No status data yet; first poll pending")
    latest = _snapshots[-1]
    return {
        "snapshot_at": latest["snapshot_at"],
        "books_stale": latest["books_stale"],
        "count": len(latest["metrics"]),
        "uids": sorted(int(u) for u in latest["metrics"].keys()),
        "metrics": latest["metrics"],
    }


@app.get("/status/{uid}")
async def status_uid(uid: int):
    if not _snapshots:
        raise HTTPException(status_code=503, detail="No status data yet; first poll pending")
    latest = _snapshots[-1]
    row = latest["metrics"].get(str(uid))
    if row is None:
        raise HTTPException(status_code=404, detail=f"UID {uid} is not tracked")
    return {
        "snapshot_at": latest["snapshot_at"],
        "books_stale": latest["books_stale"],
        "uid": uid,
        "metrics": row,
    }


@app.get("/books/{uid}")
async def books_latest(uid: int):
    """Latest full per-book breakdown for one miner."""
    entry = _latest_books.get(str(uid))
    if not entry:
        if str(uid) not in {str(u) for u in _tracked_uids}:
            raise HTTPException(status_code=404, detail=f"UID {uid} is not tracked")
        raise HTTPException(status_code=503, detail="No per-book data yet for this miner")
    return {
        "uid": uid,
        "snapshot_at": entry["snapshot_at"],
        "book_count": len(entry["books"]),
        "gauges": BOOK_GAUGES,
        "books": entry["books"],
    }


@app.get("/books/{uid}/history")
async def books_history(
    uid: int,
    minutes: Optional[int] = Query(default=None, ge=1, le=180),
):
    """Rolling per-book history (<= 3h) for one miner, grouped by snapshot."""
    if str(uid) not in {str(u) for u in _tracked_uids}:
        raise HTTPException(status_code=404, detail=f"UID {uid} is not tracked")
    snapshots = await asyncio.to_thread(_read_books_history, uid, minutes)
    return {"uid": uid, "gauges": BOOK_GAUGES, "count": len(snapshots), "snapshots": snapshots}


@app.get("/history")
async def history(
    uid: Optional[int] = Query(default=None),
    minutes: Optional[int] = Query(default=None, ge=1, le=180),
):
    cutoff = time.time() - (minutes * 60) if minutes else 0
    out = []
    for snap in _snapshots:
        if snap["epoch"] < cutoff:
            continue
        if uid is not None:
            row = snap["metrics"].get(str(uid))
            if row is None:
                continue
            out.append({"snapshot_at": snap["snapshot_at"], "books_stale": snap["books_stale"], "metrics": row})
        else:
            out.append(
                {
                    "snapshot_at": snap["snapshot_at"],
                    "books_stale": snap["books_stale"],
                    "metrics": snap["metrics"],
                }
            )
    return {"count": len(out), "uid": uid, "snapshots": out}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
