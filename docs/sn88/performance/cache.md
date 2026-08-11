# Investing88 daily cache

[88Miners/monitor/investing88_cache.py](../../../88Miners/monitor/investing88_cache.py)

## Why a daily cache

A full sync fetches the bulk `days` table plus **three per-UID endpoints for every
UID** (`close/`, `strat/`, `pnl/`). That is hundreds of HTTP requests against a
third-party API. Doing it per page load is not viable; doing it once a day is.

## Day keying is Tokyo, not UTC

```python
def _today_tokyo() -> str:
    return datetime.now(ZoneInfo(SYNC_TZ)).date().isoformat()   # SYNC_TZ = Asia/Tokyo
```

Files land as `investing88_cache/<YYYY-MM-DD>.json` plus a `latest.json` pointer.

**This is the only day boundary in the repo that isn't the host's local date** —
`miner_status/`, `miner_history/` and `fee_history/` all use `date.today()`. Don't
assume two same-named daily files cover the same window.

## Parallel fetch

```python
SYNC_WORKERS = int(os.getenv("INVESTING88_SYNC_WORKERS", "8"))
```

`ThreadPoolExecutor` + `as_completed` over UIDs. Threads are correct here: it is
pure network I/O against a remote HTTP API, nothing holding the GIL. Contrast with
the chain work in [../../sn79/performance/chain_subprocess.md](../../sn79/performance/chain_subprocess.md),
where a thread would not have helped.

A per-UID failure is captured into that miner's `errors` list rather than aborting
the sync — one bad UID must not cost you the other two hundred.

## How it's driven

| Trigger | Path |
|---|---|
| Hourly worker | `_investing88_daily_sync_worker` → `investing88_cache.py` as a **subprocess** (1800s timeout) |
| Startup bootstrap | `_bootstrap_investing88_cache()` — syncs immediately if `cache_exists_for_today()` is false |
| Manual | `POST /api/investing88/sync` |

The bootstrap exists because the worker **sleeps before its first run**, so without
it a fresh deploy would have no SN88 data for an hour. See
[../../sn79/automation/background_workers.md](../../sn79/automation/background_workers.md).

After any sync, `_investing88_cache.clear()` drops the in-process TTL entries so the
next request sees the new file rather than a stale 300s cache.

## Dead config

`INVESTING88_SYNC_HOUR` (19) and `INVESTING88_SYNC_TZ` are read into module
constants in [app.py:37-40](../../../app.py#L37-L40) but the worker never consults
them — it just loops hourly. The Tokyo boundary that matters is the one in
`_today_tokyo()` above. Noted in
[../../sn79/tricks/known_issues.md](../../sn79/tricks/known_issues.md).

## Reading the cache

| Function | Returns |
|---|---|
| `load_cache_payload(day=None)` | today's (or a given day's) payload |
| `load_cached_miner(uid, day=None)` | one miner's slice |
| `cache_exists_for_today()` | bootstrap check |
| `get_cache_status()` | what `/api/investing88/cache-status` serves |
