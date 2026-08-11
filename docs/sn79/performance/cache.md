# Caching

Five distinct caching strategies, each chosen for the shape of its data. Knowing
which one applies tells you how stale a number on screen can be.

## The layers

| # | Strategy | Used for |
|---|---|---|
| 1 | In-process TTL dict | metagraph, fee regime, reg cost, UID status, Investing88 |
| 2 | mtime-keyed parse cache | alpha price series |
| 3 | On-disk daily JSON | miner status, Investing88 |
| 4 | On-disk state snapshot | RegBot balances, batch state |
| 5 | Permanent keyed cache | registration cost by block |

## 1. In-process TTL dicts

```python
_fee_regime_cache = {"payload": None, "expires_at": 0.0}
_reg_cost_cache: dict[int, dict]        # netuid -> {payload, expires_at}
_metagraph_cache: dict                  # netuid -> {data, timestamp}
_uid_status_cache: dict                 # (uid, books) -> {data, timestamp}
_investing88_cache: dict[int, dict]     # uid -> {payload, expires_at}; key -1 = the days payload
```

| Cache | TTL | Bypass |
|---|---|---|
| metagraph | 120s | `?refresh=1` |
| fee regime | 300s | `?refresh=1`, and `?books=1` always goes upstream |
| reg cost | 300s | `?refresh=1` |
| UID status | 60s | `?refresh=1` |
| Investing88 | 300s | `?refresh=1` or `?live=1` |

Two details:

- **Only `_uid_status_cache` is bounded** (`UID_STATUS_CACHE_MAX = 256`, oldest
  evicted on insert). The others are keyed by netuid or uid over a small domain, so
  they can't grow without bound in practice.
- Caches are process-local. `pm2 restart dashboard` clears all of them; the on-disk
  layers are what survive.

## 2. mtime-keyed parse cache

`_load_alpha_series()` caches parsed rows keyed on the source file's **mtime**. The
file is rewritten hourly by an external job, so mtime is an exact change signal —
no TTL guessing, no staleness. Re-parsing ~9k rows of JSON on every poll of an
always-visible widget is the cost being avoided. See
[../miners/alpha_price.md](../miners/alpha_price.md).

Use this pattern for any file written by something outside this process.

## 3. On-disk daily JSON

`miner_status/<date>.json` and `investing88_cache/<date>.json` are the *primary*
read path, not a fallback. `/api/uid-metrics` serves from the file by default and
only goes live when explicitly asked.

This inverts the usual shape: the expensive work runs on a **schedule** (a background
worker), and requests are always cheap. A user-triggered refresh is the exception,
not the norm.

`save_daily_status()` snapshots the previous contents to
`prev_epoch_metrics.json` before overwriting, which is what powers the delta arrows
in the UI.

## 4. On-disk state snapshots

`balance_cache.json` and `active_batch.json`. Both written `tmp` + `os.replace()`.

`/api/regbot/balance` reads the file — but falls through to a **live fetch** when
the file has no entry for that server yet (right after a fresh deploy, before the
worker's first pass), so the UI is never stuck empty
([app.py:2056-2064](../../../app.py#L2056-L2064)).

`POST /api/regbot/balance/refresh` forces the sweep immediately; the UI calls it on
a manual page refresh so the spend modals always show current numbers.

## 5. Permanent keyed cache

`miner_status/reg_cost_by_block_79.json`. The registration cost at a past block
**never changes**, so entries never expire. Combined with `max_new=48` per refresh
and archive-vs-finney endpoint selection, this is what keeps historical reg costs
affordable. See [../miners/reg_cost.md](../miners/reg_cost.md).

## Serving stale on purpose

`/api/fee-regime` returns the last good summary with `stale: true` when upstream
fails, rather than erroring. `status_server` carries forward per-book counts and
flags `books_stale`. `_carry_forward_book_fields` fills gaps from the previous
snapshot. All three are the same judgement: a slightly old real number beats a fresh
zero. See [../tricks/stale_data_handling.md](../tricks/stale_data_handling.md).

## Adding a cache

1. Is the data derived from a file someone else writes? → **mtime key**, not a TTL.
2. Is it expensive and needed by every page load? → **scheduled worker + on-disk
   file**, not a request-time TTL.
3. Is it cheap and per-request? → **TTL dict**, and bound it if the key space is
   user-controlled.
4. Does a miss render an empty panel? → add a **stale fallback**.
