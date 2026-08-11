# Background workers

Six long-running loops plus two one-shot startup tasks, all created in `lifespan()`
([app.py:216](../../../app.py#L216)) and cancelled on shutdown.

| Worker | Interval | Does |
|---|---|---|
| `_miner_status_refresh_worker` | 900s | runs `miner_status_cache.py` as a **subprocess** → daily status, history, fee history, auto-switch |
| `_investing88_daily_sync_worker` | 3600s | runs `investing88_cache.py` as a subprocess |
| `_sim_id_watch_worker` | 60s | polls the sim epoch, Telegram-alerts on change |
| `_miner_master_keepalive_worker` | 300s | re-warms SSH masters to every miner box |
| `_regbot_balance_refresh_worker` | 300s | one chain child process, all coldkeys |
| `_coldkeys_watch_worker` | 3600s | runs the coldkey/IP collector, diffs it |

One-shot at startup:

- `_resume_regbot_batches()` — [../regbot/batches.md](../regbot/batches.md)
- `_bootstrap_investing88_cache()` — syncs immediately if today's cache is missing

## The shape every worker follows

```python
async def _worker():
    while True:
        try:
            await do_the_thing()
        except Exception as exc:
            logger.error("... failed: %s", exc)
        await asyncio.sleep(INTERVAL)
```

`try` **inside** the loop, never around it. One failure logs and the loop continues;
it never kills the worker for the life of the process. Every worker in this file
follows it — keep new ones consistent.

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
[app.py:37-40](../../../app.py#L37-L40) but the worker just loops hourly and does not
consult them. The 19:00 Tokyo boundary lives inside
`investing88_cache.py`'s day-keying instead.

## Cancellation

Shutdown cancels all six and awaits them under `suppress(asyncio.CancelledError)`.
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
