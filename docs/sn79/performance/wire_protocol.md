# Wire protocol: what actually goes over the network

Measured and built 2026-08-12, on top of the work in
[page_load_audit.md](page_load_audit.md). That pass fixed *how fast* a page loads.
This one is about *how many bytes a running dashboard costs*, which is the number
that matters when the link is slow or metered.

| | before this pass | after |
|---|---|---|
| First load | 297 KB / 18 req | **263 KB / 16 req** |
| Reload (warm tab) | 59.0 KB | **6.3 KB** — 13 of 17 responses are 304 |
| Steady state, one visible tab | 4.17 KB/min, 9.2 req/min | **0.38 KB/min, 1.3 req/min** |

Cumulatively against where this started: a page load went 1 797 KB → 263 KB, and an
idle open tab went from re-downloading everything every few seconds to under half a
kilobyte a minute.

---

## The four mechanisms, in the order they pay off

### 1. Conditional requests — a 304 is the perfect diff

Zero body, ~150 bytes of headers, and nothing to implement on either side beyond a
validator. Every cached read path already emitted a weak `ETag`; the problem was
that nobody sent `If-None-Match` back.

```
REQ /js/miners.js                inm=YES  -> 304     parser subresource, uses the HTTP cache
REQ /api/regbot/batch/active     inm=no   -> 200     fetch(), never revalidated
```

`fetch()` was not revalidating these at all, so a byte-identical 13 KB payload was
re-sent on every poll. Rather than depend on browser cache heuristics, the client
owns the validator explicitly — `conditionalFetch()` in
[asset/miners.js](../../../asset/miners.js):

- keeps `{etag, text}` per URL and sends `If-None-Match`,
- sets `cache: "no-store"` so the browser doesn't layer a second, disagreeing
  validation on top,
- caches the response **text**, not the parsed object, and re-parses on each 304.
  Handing back one shared object would let a single caller's mutation corrupt what
  every later caller sees; re-parsing 60 KB costs well under a millisecond.
- persists the map to `sessionStorage`, which is what takes a reload from 59 KB to
  6.3 KB. Bounds are on the *decoded* text (`ETAG_STORE_MAX_ENTRY` 512 KB,
  `ETAG_STORE_MAX_TOTAL` 1 MB) because that is several times the wire size — the
  metagraph identity half is ~103 KB of JSON that travels as ~20 KB.

**The trap:** a "when did we last look" field in the body defeats all of this. Two
were found and fixed:

| Field | Why it broke revalidation |
|---|---|
| `updated_at` in `/api/regbot/active` | the scan reruns every 20s and the answer is nearly always identical, but the clock always moved |
| `fetched_at` in the metagraph | same, every 90s |

`_serialize_json(payload, etag_over=...)` takes the tag over the payload *minus*
that field. A **weak** validator is exactly the right tool: `W/` means
"semantically equivalent", not "byte-identical", so a 304 that leaves the client
holding a slightly older timestamp is correct by the spec rather than a fudge.
`/api/alpha-price` uses the same escape hatch — its `updated_at` is the source
file's mtime, which the collector bumps on a timer whether or not prices moved.

### 2. Stop sending what nobody reads

Bigger than any protocol change, and free at runtime.

**`/api/uid-metrics`: 281 835 raw / 58 556 gz → 69 505 / 23 183.**

- `prev_metrics` was a byte-for-byte duplicate of `metrics` — half the payload — to
  draw delta arrows on exactly three fields. The server now computes those deltas
  (`UID_METRICS_DELTA_FIELDS`: `real_pnl`, `unrl_pnl`, `volume`).
- `UID_METRICS_DROP_FIELDS` removes four fields the client never reads
  (`reg_cost_rao`, `reg_cost_tao`, `reg_block`, `max_book_volume`) and one it can
  derive (`blocks_since_registration` = `currentBlock − reg_block`). A **denylist**
  rather than an allowlist, so a genuinely new metric reaches the UI without a
  server change. Verify with `grep -c '\bfield\b' asset/miners.js` before adding to it.
- `reg_date`, `reg_date_iso` and `reg_date_tz` are three renderings of the instant
  `reg_date_utc_iso` already carries. They are dropped **only when every row has the
  ISO field**, because the client's fallback path is what reads them on older
  daily-status files.

**`/api/fee-regime/history`** was the same story one level up — see
[../miners/fee_regime.md](../miners/fee_regime.md) on `book_points=latest`.

### 3. Split a payload by how often each half changes

`/api/metagraph-data` mixed two very different things:

| | changes | raw | gzip |
|---|---|---|---|
| identity — hotkey ↔ uid ↔ coldkey, validator flags, IPs | on registration churn | 103 240 | **20 193** |
| state — emission, incentive, age, immunity | every block | 11 370 | **5 039** |
| *combined, as it was sent* | every block | 154 644 | 27 868 |

So a poll now moves **5 KB instead of 28 KB**, and the identity half answers 304
until something registers. State is columnar — arrays positional against identity's
`order` — because the combined shape wrote each 48-character hotkey **seven times**,
once as a key in each parallel map.

`/api/metagraph-data` still serves the original combined shape for anything outside
the dashboard.

**The correctness hazard, and how it is handled.** State's arrays are positional
against identity's `order`. A client holding a *stale* identity would map every
value onto the wrong hotkey, and it cannot detect that from lengths alone — one
deregistration plus one registration in the same block keeps the count identical.
So state carries `identity_etag`, the ETag of the identity it was built from, and
the client refetches identity unconditionally when that doesn't match what it holds.
Both halves are derived together and memoised on the same cache entry
(`_metagraph_halves`), so they can never disagree at the source.

### 4. Push notifications, pull data (SSE)

`GET /api/events` is a Server-Sent Events stream. Workers publish one line when a
topic's ETag changes; the client re-fetches *that endpoint* conditionally.

```
retry: 3000

event: change
data: {"topic":"regbot_active","etag":"W/\"2cbde9cc3a3b8c5b6c1f34b0\""}
```

**SSE rather than WebSocket** because this traffic is strictly one-directional —
every write is already an ordinary POST — and SSE brings auto-reconnect, plain HTTP
and the existing session cookie with no framing library.

**Notification, not payload.** Pushing the data down the stream would mean a second
serialisation path per topic, per-connection state, and no ETag or gzip on the hot
bytes. This way the cached-byte handlers stay the single source of truth and a
notification costs ~60 bytes.

Design points that are load-bearing:

- **`Content-Encoding: identity` on the response.** Starlette's `GZipMiddleware`
  passes a response straight through when that header is set; without it the stream
  sits in a gzip buffer waiting for enough bytes to be worth compressing, which for
  60-byte events is forever.
- **The stream is an optimisation, never the only path.** When it is up, the poll
  timers drop from 15–20s to 120s; when it drops, they resume and the page works
  exactly as it did before SSE existed. `eventStreamUp` is the only switch.
- **Bounded per-subscriber queue** (`SSE_QUEUE_MAX` 32). A client too slow to drain
  32 notifications will not be rescued by buffering more — drop it and let
  `EventSource` reconnect, which re-syncs via the conditional fetch anyway.
- **Replay on connect.** A reconnecting client is sent the current ETag of every
  known topic, so it revalidates immediately instead of waiting for the next change.
- **The client skips a notification whose ETag it already holds.** Two endpoints
  publish from inside their own GET handler, so the tab that just fetched is told
  about its own result; without that check it becomes a fetch loop.

**Publish on data change, not on cache miss.** `/api/uid-metrics` originally
published from its byte-cache miss, which raced the very fetch that caused it: the
notification arrived before that fetch had stored its ETag, so the client could not
tell it already had the data and re-downloaded all 22.5 KB. The publish belongs in
`_miner_status_refresh_worker`, which knows when the underlying file actually
changed.

---

## Two TTLs that guaranteed a slow request every cycle

Reported from Chrome DevTools after the work above: "waiting for server response"
of 3–4s. The server was idle — load average 0.59, the dashboard process at 1.5% CPU,
every warm endpoint answering in 1–100ms. It was not CPU.

It was the same request-driven-TTL bug the metagraph had, still present on the two
endpoints that pass had not touched:

| Endpoint | cold | TTL | client polls |
|---|---|---|---|
| `/api/fee-regime` | **2.24 s** (TAOS via cloudscraper) | 300 s | 300 s |
| `/api/reg-cost` | **2.13 s** (chain subprocess) | 300 s | 300 s |

**The TTL was exactly the poll interval.** Every automatic 5-minute refresh landed
on expiry and paid both in full, concurrently — which is what the browser reported
as 3–4s. Fixed the same way as the metagraph: `_fee_regime_refresh_worker` and
`_reg_cost_refresh_worker` at 240s (under the 300s TTL), both single-flight and
`shield`ed, and both endpoints now serve stale-while-revalidate with an
`X-Cache: stale` marker rather than blocking.

**A TTL equal to the consumer's poll interval is the worst possible value.** It
converts "usually cached" into "never cached, for one unlucky request every cycle".
Refresh cadence must sit *under* the staleness window — that is why
`METAGRAPH_REFRESH_SECONDS` is 90 against a 120s TTL.

## Cache keys: prefer a change signal over a clock

Same class, one order of magnitude smaller. `/api/uid-metrics` and
`/api/miner-params` held their serialised bytes behind a 30s TTL, so roughly every
poll threw away a valid result and paid the rebuild — 718ms for the uid-metrics read
(`get_cached_metrics_payload` parses two JSON files) and ~100ms for the 200-file
params scan. For data that changes every 900s.

Both have an exact signal available, so neither is keyed on time any more:

| Cache | Key | Cost of checking |
|---|---|---|
| `_uid_metrics_cache` | mtimes of the daily-status and prev-epoch files | 2 × `stat()` |
| `_miner_params_cache` | `(name, mtime, size)` for every params file, stat-only `scandir` | ~1 ms for 200 files |

The TTL survives only as a long backstop (3600s). The params fingerprint is a
stat pass rather than a directory mtime because the directory is written from
outside this process too — RegBot pulls and the auto-switcher — and a directory
mtime does not move when a file is edited in place.

Result: every endpoint on the page now answers in ≤23 ms of server time, warm or
cold-ish, with no periodic spike.

## The Refresh button was the last blocking path

Everything above is about the *automatic* path — page load and background polls.
Clicking **Refresh** went somewhere else entirely, and still cost 3s per click after
all of it. `loadData(true)` passed `forceRefresh` down to `loadMetagraph`, which
**awaited** `/api/metagraph-data?refresh=1` before fetching the halves it actually
renders. Measured on the server:

| | |
|---|---|
| `import bittensor` into a fresh subprocess | **0.81 s** |
| syncing the metagraph from finney | **~2.1 s** |
| **forced re-read, total** | **2.9-3.1 s** |

Nothing in that has to happen before the user sees a table.
`_metagraph_refresh_worker` keeps the server's copy under 90s old, so the warm
halves are already a good answer. The button now:

1. paints from the warm halves — measured **89 ms** from click to interactive page,
2. fires the chain re-read behind the paint,
3. repaints when it lands, and spins the button's icon until it does.

`forceMetagraphReread()` owns that, and it is deliberately not on the promise chain
`loadData` returns — `.finally` leaves the button alone while a re-read is
outstanding. It drops its result if the user changed subnet meanwhile: rendering
netuid 79's metagraph into an 88 table is worse than showing nothing new.

### Coalescing a re-read that cannot say anything new

Server-side, `?refresh=1` within `CHAIN_BLOCK_SECONDS` (12s) of the last successful
read returns the cached bytes as `X-Cache: hit-coalesced`. Inside one block the chain
has produced no new state, so the re-read is 3s spent to reproduce identical numbers.
This is not a staleness trade — it collapses double-clicks, several tabs refreshing
together, and a reload followed straight away by the button. `/api/fee-regime` and
`/api/reg-cost` use the same window.

**Force still means force.** Past 12s the read is real and takes its full 3s; it just
no longer holds the page.

## A "refresh" endpoint that ignored its own worker

`POST /api/regbot/balance/refresh` measured **4.5s, and as much as 15.2s**. It ran
the full 12-coldkey sweep synchronously on every call — while
`_regbot_balance_refresh_worker` was already writing the same
`balance_cache.json` every 300s. The caller makes it worse than pointless:

```js
// loadRegBotBalances(), fired on every Refresh click
fetch("/api/regbot/balance/refresh", { method: "POST" }).catch(...)
```

Fire-and-forget — **the body is never read**. So the endpoint blocked for up to 15s
to produce a value nobody looked at, and with several tabs open each one spawned its
own `chain_cli`, each opening its own substrate connection. That burst of concurrent
metadata decodes is the documented worst source of latency on this service.

Now it follows the same rule as everything else: the worker owns freshness, the
request is a lookup, and a sweep runs *behind* the response only when the file has
actually gone stale (`cache: hit|stale`). `_refresh_regbot_balances()` single-flights
it, so the worker and any number of tabs share one sweep. **4.5-15s → 1.7-4.4ms.**

`?wait=1` keeps the old blocking behaviour for a caller that genuinely needs the new
numbers before it can continue.

## Where the time in a chain call actually goes

Worth knowing before optimising any of these further. Two runs of the same script,
differing only in how many balances they ask for:

| | |
|---|---|
| `chain_cli.py balances 79 <1 address>` | **2.04 s** |
| `chain_cli.py balances 79 <12 addresses>` | **3.10 s** |
| marginal cost per extra query | **0.096 s** |
| **fixed setup, paid every call** | **~1.94 s** |

That fixed cost is `import bittensor` (0.81s, measured separately) plus the websocket
connect and metadata decode. **The query itself is ~5% of a small chain call.** Every
`run_chain_cli()` pays it fresh because every call is a new subprocess.

So the ranking for anything chain-backed is: don't call it on the request path
(workers, above) → don't call it when the answer cannot have changed
(`hit-coalesced`, above) → and only then worry about the query. A long-lived helper
process holding one warm connection would cut ~1.9s off every call, and is the
largest single lever left; it has not been built.

## Restart resilience

Observed while building this: the finney entrypoint stopped accepting websocket
connections entirely. Because the metagraph cache was in-process only, a
`pm2 restart` during that window left the dashboard with **nothing to render** —
every panel 502.

`_save_metagraph_snapshot()` now persists each successful load to
`miner_status/metagraph_<netuid>.json`, and `_seed_metagraph_from_disk()` fills the
cache from those files **synchronously, before the first request is accepted** — an
async warm is not enough, because a visitor landing during the fan-out just waits it
out. Entries are seeded with their *original* timestamp, so they read as stale
immediately and the first request kicks a refresh off behind itself.

A snapshot older than `METAGRAPH_SNAPSHOT_MAX_AGE` (24h) is skipped rather than
shown. `fetched_at` is in the payload either way, so the age is never hidden.

The same outage exposed a second problem: `loadData()` awaited every panel before
painting anything, so `/api/reg-cost` sitting out its full 120s timeout meant a
blank page even though the metagraph had already come back from disk. The table now
paints as soon as the metagraph is in, and again when the two feeds that add columns
to it arrive. Panels own their own repaint. **One slow upstream must not be able to
hold the table hostage.**

---

## Adding an endpoint without regressing this

1. Return through `_json_bytes_response` so it has a validator, and fetch it with
   `conditionalFetch` so the validator is used.
2. If the payload contains a generation timestamp, pass `etag_over=` without it.
3. Check what the client actually reads before adding a field. Two of the three
   biggest payloads here were majority-unread.
4. If half of it changes on a different clock from the other half, split it — and
   make the fast half name the slow half it was built against.
5. Publish a change notification from whatever **changes the data**, never from a
   request handler that merely noticed a cold cache.
