# Background workers

Ten long-running loops plus two one-shot startup tasks, all created in `lifespan()`
and cancelled on shutdown.

| Worker | Interval | Does |
|---|---|---|
| `_miner_status_refresh_worker` | 900s | runs `miner_status_cache.py` as a **subprocess** → daily status, history, fee history, auto-switch |
| `_investing88_daily_sync_worker` | 3600s | runs `investing88_cache.py` as a subprocess |
| `_sim_id_watch_worker` | 60s | polls the sim epoch, Telegram-alerts on change |
| `_miner_master_keepalive_worker` | 300s | re-warms SSH masters to every miner box |
| `_regbot_balance_refresh_worker` | 300s | one chain child process, all coldkeys |
| `_coldkeys_watch_worker` | 3600s | runs the coldkey/IP collector, diffs it |
| `_metagraph_refresh_worker` | 90s | keeps netuid 79/71/88 metagraphs warm — a **request must never trigger the chain subprocess** |
| `_regbot_active_scan_worker` | 20s | the SSH active-run scan across all 12 presets |
| `_fee_regime_refresh_worker` | 240s | the TAOS fee-regime summary (2.2s cold) |
| `_reg_cost_refresh_worker` | 240s | registration cost per warm netuid (2.1s cold) |

Workers also **publish change notifications**. When a worker's payload gets a new
ETag it calls `_publish(topic, etag)`, which fans out to every `/api/events`
subscriber; the browser then re-fetches that one endpoint conditionally. Publishing
belongs to whatever *changes the data* — never to a request handler that merely
noticed a cold cache, which races the fetch that caused it. See
[../performance/wire_protocol.md](../performance/wire_protocol.md).

The last two exist for one reason: they are the only two things a page load used to
do that took seconds. Both are covered in
[../performance/page_load_audit.md](../performance/page_load_audit.md) and
[../performance/cache.md](../performance/cache.md) §6.

One-shot at startup:

- `_resume_regbot_batches()` — [../regbot/batches.md](../regbot/batches.md)
- `_bootstrap_investing88_cache()` — syncs immediately if today's cache is missing

## The shape every worker follows

```python
async def _worker():
    while True:
        started = time.time()
        try:
            await do_the_thing()
        except Exception as exc:
            logger.error("... failed: %s", exc)
        await _sleep_until_next_tick(started, INTERVAL)
```

`try` **inside** the loop, never around it. One failure logs and the loop continues;
it never kills the worker for the life of the process. Every worker in this file
follows it — keep new ones consistent.

**`_sleep_until_next_tick`, not a flat `sleep(INTERVAL)`.** A flat sleep placed after
the work makes the real cadence `INTERVAL + work`, which is not what any of the
reasoning below assumes. The three cache-warming workers schedule on a deadline;
the rest are unchanged, since their intervals dwarf their work.

## Cadence has to sit under the consumer's staleness window

`_metagraph_refresh_worker` runs at 90s against a 120s `METAGRAPH_CACHE_TTL`;
`_fee_regime_refresh_worker` and `_reg_cost_refresh_worker` at 240s against 300s.
Those numbers are not arbitrary — they are chosen so a warm copy never lapses
between ticks. Raise `METAGRAPH_REFRESH_SECONDS` past 120 and every copy starts
reading as stale, with nothing failing to tell you.

**The cadence you configure has to be the cadence you get.** With a flat sleep after
the work, the metagraph worker's real cycle was **105s**, not 90 — three netuids at
~3s each plus their 2s gaps, added on top of the sleep. Measured directly: a payload
sampled 100s apart came back with `fetched_at` ages of 2s and then 103s. That still
fit under the 120s TTL, but the margin was 15s rather than the 30s the constants
imply, and a slow chain would erase it. On a deadline the same worker holds 90s
until the work itself exceeds it.

**The failure this prevents is worth naming.** Before these two workers existed,
`FEE_REGIME_CACHE_SECONDS` and `REG_COST_CACHE_SECONDS` were both 300s and the page
reloaded every 300s — so *every* automatic refresh landed exactly on expiry and paid
2.2s + 2.1s of upstream, which is what a browser reports as 3-4s of "waiting for
server response". A TTL equal to the consumer's poll interval is the worst possible
value it can have.

The metagraph worker also refreshes its netuids **one at a time with a 2s gap**
rather than gathering them. Each is a chain connection, and three at once is exactly
the burst that made the balance sweep starve unrelated queries — see
[../performance/chain_subprocess.md](../performance/chain_subprocess.md).

## Deferred imports

Workers import their domain module *inside* the function body
(`from regbot import refresh_balance_cache`). A module that fails to import breaks
one worker at its first tick, not application startup.

## The sync worker gotcha

`_investing88_daily_sync_worker` **sleeps first, then works**:

```python
while True:
    await asyncio.sleep(INTERVAL)     # <- before, not after
    await _run_investing88_sync_subprocess()
```

That is why `_bootstrap_investing88_cache()` exists as a separate one-shot task —
without it, a fresh start would have no SN88 cache for a full hour.

Note the names and the reality have drifted: `INVESTING88_SYNC_HOUR` (19) and
`INVESTING88_SYNC_TZ` (`Asia/Tokyo`) are defined at
[app.py:42-44](../../../app.py#L42-L44) but the worker just loops hourly and does not
consult them. The 19:00 Tokyo boundary lives inside
`investing88_cache.py`'s day-keying instead.

## Cancellation

Shutdown cancels all ten and awaits them under `suppress(asyncio.CancelledError)`.
RegBot's `_run_batch` deliberately intercepts `CancelledError` first to leave its
persisted state resumable — see [../regbot/batches.md](../regbot/batches.md).

## Subprocess workers vs in-process workers

| Worker | Runs as | Why |
|---|---|---|
| status refresh | child process | loads `bittensor` — must never enter uvicorn |
| investing88 sync | child process | long, CPU-ish, isolated failure |
| coldkeys watch | child process | loads `bittensor` |
| balance refresh | calls `chain_cli.py` child | substrate holds the GIL |
| sim id | `asyncio.to_thread` | plain `urllib`, network-bound |
| SSH keepalive | `asyncio.gather` of ssh subprocesses | pure I/O |
| metagraph refresh | calls `load_metagraph.py` child | loads `bittensor` |
| reg cost refresh | calls `chain_cli.py` child | substrate holds the GIL |
| fee regime refresh | `asyncio.to_thread` | `cloudscraper`, network-bound |
| regbot active scan | `asyncio.gather` of ssh subprocesses | pure I/O, 12 boxes in parallel |

The rule: **anything that imports `bittensor` runs out of process.** See
[../performance/chain_subprocess.md](../performance/chain_subprocess.md).

## Failure visibility

Worker failures are `logger.error` only — nothing surfaces in the UI. `logging`
writes to stderr, so look in `logs/dashboard-error.log`:

```bash
grep -E "refresh failed|watch failed|cache refresh failed" logs/dashboard-error.log | tail
```

A worker that has been failing for weeks looks exactly like one that is working,
from the dashboard. That is how the issue in
[../tricks/known_issues.md](../tricks/known_issues.md) stayed unnoticed.

Two of the new workers have a visible tell, at least. `X-Cache` on
`/api/metagraph-data` and `/api/regbot/active` reports `hit`, `stale`, `miss` or
`stale-error`:

```bash
curl -sI localhost:12800/api/metagraph-data?netuid=79 | grep -i x-cache
curl -sI localhost:12800/api/regbot/active | grep -i x-cache
```

A persistent `stale` on either means its worker is wedged. That is still a manual
check — a `/api/health` endpoint reporting last-success timestamps per worker remains
the right fix for the class.
