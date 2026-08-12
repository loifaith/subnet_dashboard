# Caching

Seven distinct caching strategies, each chosen for the shape of its data. Knowing
which one applies tells you how stale a number on screen can be.

## The layers

| # | Strategy | Used for |
|---|---|---|
| 1 | In-process TTL dict | fee regime, reg cost, UID status, Investing88 |
| 2 | mtime-keyed parse cache | alpha price series, the HTML document, `/js/miners.js` |
| 3 | On-disk daily JSON | miner status, Investing88 |
| 4 | On-disk state snapshot | RegBot balances, batch state |
| 5 | Permanent keyed cache | registration cost by block |
| 6 | **Worker-owned, stale-while-revalidate** | metagraph, RegBot active-run scan |
| 7 | **Serialised-byte cache + weak ETag** | miner params, uid metrics, fee history, the poll endpoints |

Layers 6 and 7 were added on 2026-08-11 —
[page_load_audit.md](page_load_audit.md) has the before/after measurements and the
reasoning. The short version: **layer 6 decides who pays for freshness, layer 7
decides who pays for serialisation.** Both used to be the browser.

## 1. In-process TTL dicts

```python
_fee_regime_cache = {"payload": None, "expires_at": 0.0}
_reg_cost_cache: dict[int, dict]        # netuid -> {payload, expires_at}
_uid_status_cache: dict                 # (uid, books) -> {data, timestamp}
_investing88_cache: dict[int, dict]     # uid -> {payload, expires_at}; key -1 = the days payload
```

| Cache | TTL | Bypass |
|---|---|---|
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
([app.py:2445-2452](../../../app.py#L2445-L2452)).

`POST /api/regbot/balance/refresh` forces the sweep immediately; the UI calls it on
a manual page refresh so the spend modals always show current numbers.

## 5. Permanent keyed cache

`miner_status/reg_cost_by_block_79.json`. The registration cost at a past block
**never changes**, so entries never expire. Combined with `max_new=48` per refresh
and archive-vs-finney endpoint selection, this is what keeps historical reg costs
affordable. See [../miners/reg_cost.md](../miners/reg_cost.md).

## 6. Worker-owned caches, served stale-while-revalidate

The rule these encode: **a browser request must never wait on an upstream.** A
worker owns the key's freshness, and the request path does a lookup.

| Cache | Owner | Cadence | Cost avoided per request |
|---|---|---|---|
| `_metagraph_cache` | `_metagraph_refresh_worker` | `METAGRAPH_REFRESH_SECONDS` = 90s | a 2.9s chain subprocess |
| `_regbot_active_state` | `_regbot_active_scan_worker` | `REGBOT_ACTIVE_SCAN_SECONDS` = 20s | a 690ms SSH fan-out across 12 boxes |

Three properties make this work, and all three matter:

- **The cadence sits under the staleness window.** 90s against a 120s
  `METAGRAPH_CACHE_TTL`, so a warm copy never lapses in normal operation. Raising
  `METAGRAPH_REFRESH_SECONDS` past the TTL silently puts the 2.9s subprocess back on
  the request path.
- **Single-flight.** `_refresh_metagraph()` shares one `asyncio.Task` per netuid and
  `shield`s it, so the worker and a cold request never spawn two subprocesses, and a
  browser hanging up mid-request can't cancel a load others are awaiting.
- **Expiry is not an error.** An expired-but-present metagraph is returned
  immediately with `X-Cache: stale` while the refresh runs behind the request. A
  failed forced refresh returns the last good copy (`stale-error`) rather than an
  error page.

The escape hatch is `?refresh=1`, and it is only for user actions that just changed
the underlying reality — killing a run, triggering a registration. **A background
poll must never pass it**, or the fan-out it exists to avoid is back.

## 7. Serialised-byte caches and weak ETags

One fork-mode uvicorn process means `json.dumps` of a 275KB payload is 44ms of stall
for *every* concurrent request, not just the one paying for it. So these caches hold
`(bytes, etag)`, not dicts — a warm request neither reads disk nor serialises.

| Cache | TTL | Invalidated by |
|---|---|---|
| `_miner_params_cache` | `MINER_PARAMS_CACHE_SECONDS` = 30s | **`_save_miner_params_file()`, explicitly** |
| `_uid_metrics_cache` | `UID_METRICS_CACHE_SECONDS` = 30s | `?refresh=1` |
| `_fee_history_cache` | `FEE_HISTORY_CACHE_SECONDS` = 60s | TTL only |

Primitives, at the top of [app.py](../../../app.py):

- `_serialize_json(payload) -> (bytes, etag)` — the same `json.dumps` settings
  FastAPI's `JSONResponse` uses, so switching a handler to it changes the wire format
  not at all. The ETag is a blake2b digest of the bytes.
- `_ByteCache(ttl, cap)` — deliberately not an LRU; the key spaces are bounded by
  what the UI asks for, and `cap` only stops an arbitrary-query caller growing it.
- `_json_bytes_response(request, body, etag, cache_state)` — returns `304` on a
  matching `If-None-Match`, else the bytes, with `Cache-Control: no-cache` (meaning
  "store it, but revalidate", which is what turns a repeat poll into a zero-byte
  round trip).

**The invariant that will bite you:** a byte cache over data someone else writes has
to be invalidated by the writer. `_save_miner_params_file()` calls
`_miner_params_cache.invalidate()`. Any new writer of `79Miners/params/` must too, or
the agent column keeps showing the pre-edit value for up to 30s after a successful
save.

## Serving stale on purpose

`/api/fee-regime` returns the last good summary with `stale: true` when upstream
fails, rather than erroring. `status_server` carries forward per-book counts and
flags `books_stale`. `_carry_forward_book_fields` fills gaps from the previous
snapshot. All three are the same judgement: a slightly old real number beats a fresh
zero. See [../tricks/stale_data_handling.md](../tricks/stale_data_handling.md).

## Adding a cache

1. Does it spawn a subprocess or open an SSH connection? → **background worker,
   full stop** (layer 6). Never a bare TTL: with a TTL, one unlucky request per
   window pays the whole cost, and that window is exactly when someone is looking.
2. Is the data derived from a file someone else writes? → **mtime key**, not a TTL.
3. Is it expensive and needed by every page load? → **scheduled worker + on-disk
   file**, not a request-time TTL.
4. Is it cheap and per-request? → **TTL dict**, and bound it if the key space is
   user-controlled.
5. Is the payload over ~20KB, or polled? → cache the **bytes** and give it an ETag
   (layer 7), not just the dict.
6. Does anything in this process write the underlying data? → **invalidate at the
   write**, don't rely on the TTL.
7. Does a miss render an empty panel? → add a **stale fallback**.
