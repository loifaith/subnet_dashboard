# Status server (port 9800)

A standalone FastAPI service,
[79Miners/monitor/status_server.py](../../../79Miners/monitor/status_server.py),
running as its own PM2 app.

## Why it exists

Every miner box wants live status. If each of ~100 miners polled TAOS directly,
that's ~100× the query load against a Cloudflare-fronted Grafana proxy that already
returns empty results under stress.

So **one** service polls TAOS every 30s and the miner boxes poll *it* over HTTP.
The dashboard does not consume this service — it has its own daily-cache path
([../miners/uid_status.md](../miners/uid_status.md)).

## What it tracks

Tracked UIDs come from `list_my_miner_uids()` — the same `run_miner_*.json` scan as
everywhere else, re-evaluated on **every cycle**, so a newly registered miner starts
being polled within 30s with no restart.

| Field group | Fields |
|---|---|
| `AGG_FIELDS` | `kappa, real_pnl, unrl_pnl, volume, vol_mkr, vol_tkr` |
| `BOOK_FIELDS` | `hot_book_count, book_count, max_book_volume` |
| `PNL_FIELDS` | `pnl_books_pos, pnl_books_neg` |

Per-book gauges kept in full: `weighted_kappa, kappa, daily_volume, realized_pnl,
fees_maker_rate, fees_taker_rate, activity_factor`.

## One poll cycle

`_fetch_all_blocking()` ([status_server.py:108](../../../79Miners/monitor/status_server.py#L108)):

1. `fetch_agent_display_metrics(scraper, uids)` — the reliable aggregate query.
2. For each per-book gauge, **one all-UIDs range query**, then filter to mine. Seven
   queries total, not seven-per-miner.
3. `_derive_counts()` computes `book_count`, `hot_book_count`, `max_book_volume`,
   `pnl_books_pos/neg` from the per-book breakdown.
4. Carry forward the last good counts for any miner whose per-book fetch came back
   empty, and set `books_stale` if any did.

The `cloudscraper` session is **closed in a `finally`** each cycle — it's a
`requests.Session`, and not closing it leaks its connection pool (sockets/fds) every
30 seconds.

`_poll_worker` sleeps `max(1.0, POLL_INTERVAL - elapsed)`, so a slow cycle doesn't
compound into drift.

## Two storage strategies

| | Aggregate history | Per-book history |
|---|---|---|
| File | `status_history.csv` (one) | `books_history/books_<uid>.csv` (one per miner) |
| Write | **full rewrite** from the in-memory buffer each cycle | **append-only** |
| Prune | on every cycle, in memory | rewrite every `BOOK_PRUNE_EVERY` (20) cycles ≈ 10 min |

The aggregate buffer is ≤3h and small, so rewriting it wholesale each cycle (via
`tmp` + `os.replace`) is cheap and always consistent. Per-book data is far larger —
appending is the only sane write, with periodic compaction.

Both are bounded by `RETENTION_SECONDS` (10800 = 3h via the PM2 env).

## Warm start

On startup `_load_csv()` and `_load_latest_books()` rebuild the in-memory buffer and
the last-good caches from disk, so a restart doesn't serve an empty `/status` or
regress the carry-forward values. Snapshots older than the retention window are
dropped during the load.

## Endpoints

| Path | Returns |
|---|---|
| `GET /health` | tracked count, snapshot count, intervals, latest snapshot time |
| `GET /uids` | tracked UID list |
| `GET /status` | latest snapshot, all miners |
| `GET /status/{uid}` | latest metrics for one miner |
| `GET /books/{uid}` | latest full per-book breakdown |
| `GET /books/{uid}/history?minutes=` | per-book history, grouped by snapshot (≤180 min) |
| `GET /history?uid=&minutes=` | aggregate history (≤180 min) |

`503` means "no data yet, first poll pending" — distinct from `404` "not tracked".
CORS is wide open (`allow_origins=["*"]`) because the consumers are the miner boxes.

Every response carries `books_stale`, so a consumer can tell whether the book counts
are fresh or carried forward. See
[../tricks/stale_data_handling.md](../tricks/stale_data_handling.md).

## Related CLI

[status_cli.py](../../../79Miners/monitor/status_cli.py) is a terminal client for the
same service — useful for checking it without a browser.
