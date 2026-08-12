# Homepage load audit — measured and fixed 2026-08-11

Measured against the live process on `95.216.27.87:12800` with headless Chromium
(Resource Timing API), from the dashboard host itself, so the numbers are server +
client cost with the network removed, then re-run with emulated RTT and bandwidth
to model a real remote operator.

**All eleven findings below have been fixed.** This doc keeps the before/after so
the next person can tell which properties are load-bearing and which are incidental.

Reference design: [`/root/79/BT_79_Dashboard/docs/performance/`](../../../../BT_79_Dashboard/docs/performance/PERFORMANCE.md).

---

## 1. Result

"Usable" = the app's own `loadingOverlay` is gone **and** the miner table has painted.

| Client | before | after this pass | after [wire_protocol.md](wire_protocol.md) |
|---|---|---|---|
| loopback, 0 RTT | 1 130 ms | 411 ms | **333 ms** |
| 100 ms RTT / 10 Mbps (EU broadband) | 1 914 ms | 883 ms | — |
| 250 ms RTT / 8 Mbps (Asia → Finland) | 2 866 ms | 1 654 ms | **1 385 ms** |
| any of the above, cold metagraph | +2 900 ms | **no cliff** | no cliff |

| | before | after |
|---|---|---|
| requests | 25 | 18 |
| HTML document | 373 KB | **15 KB** (gzip; 110 KB decoded) |
| same-origin `/api/*` | 1 286 KB | **145 KB** |
| CDN (Bootstrap, Icons, Chart.js) | 138 KB | 138 KB |
| **total** | **≈ 1 797 KB** | **≈ 297 KB** (−83 %) |

A second pass took this further — 264 KB on a cold load, 6.3 KB on a reload and
0.38 KB/min for an idle tab. See [wire_protocol.md](wire_protocol.md).

Repeat loads are cheaper again: the document, `/js/miners.js` and every cached JSON
endpoint answer `304` with a zero-byte body when nothing has changed.

---

## 2. The measured waterfall

### Before

```
 start   dur  stall  serv    dl      KB  gz?  url
   116    76      4    71     2   151.2  NO   /api/metagraph-data?netuid=79
   117   690      6   683     1     0.3  NO   /api/regbot/active        ← SSH fan-out
   117   292      6   274    12    19.2  NO   /api/regbot/batch/active
   118   281      5   274     2   144.4  NO   /api/metagraph-data?netuid=71   ← not viewed
   118   295     75   217     3   155.6  NO   /api/metagraph-data?netuid=88   ← not viewed
   196  1148    610   537     1     0.3  NO   /api/regbot/active        ← 2nd time
   198   480    213   260     6   476.0  NO   /api/fee-regime/history?days=1
   199   478    214   261     3    18.6  NO   /api/miner-params
   739    66      0    64     2   275.3  NO   /api/uid-metrics          ← after miner-params
   808   208      1   206     1     0.1  NO   /api/reg-cost?netuid=79   ← after uid-metrics
  1074  1013    270   742     1     0.3  NO   /api/regbot/active        ← 3rd time
```

### After

```
 start   dur  stall  serv    dl      KB  gz?  url
    15    32      6    23     4    52.4  yes  /js/miners.js
   115    21      4    11     6    26.7  yes  /api/metagraph-data?netuid=79
   115    19      3    11     5     0.3  NO   /api/regbot/active
   115    22      3    15     4     0.0  NO   /api/regbot/batch/active
   116    25      3    20     2     0.1  NO   /api/subnet-emission?netuid=71
   145    39      4    30     5     5.9  yes  /api/fee-regime/history?days=1&book_points=latest
   145    38      4    30     4     1.5  yes  /api/miner-params
   145    39      4    28     7    55.8  yes  /api/uid-metrics
   145    43     39     4     1     0.1  NO   /api/reg-cost?netuid=79
   146    69     39    29     1     1.3  yes  /api/alpha-price?netuid=79&range=7d
```

`stall` = queued behind the browser's 6-connections-per-origin limit; `serv` = server
think time. The staircase (739 → 808 → 1074) and the uniform ~270 ms `serv` are both
gone.

---

## 3. What was wrong, and what was done

### 3.1 No HTTP compression — the single biggest win

Nothing sat in front of :12800 and no `GZipMiddleware` was installed, so every byte
went out raw.

**Fixed:** [app.py](../../../app.py) — `app.add_middleware(GZipMiddleware, minimum_size=1000)`.
Added before the auth middleware, so Starlette's outermost-last-added ordering ends
up `auth_gate → gzip → routes`.

Measured on the real payloads: HTML 382→69 KB, `fee-regime/history` 487→113 KB,
`uid-metrics` 282→58 KB, `metagraph-data` 155→28 KB. **−82 % across the board.**

### 3.2 The metagraph cache was request-driven — a 2.9 s cliff every 120 s

`METAGRAPH_CACHE_TTL = 120`, cold load **2 896 ms** vs warm 13 ms, and none of the six
`lifespan()` workers owned it. Whoever arrived first after each lapse paid the chain
subprocess in full — across three keys (79 viewed, 71 and 88 for the header), each
expiring independently.

**Fixed:** three things, together.

1. `_metagraph_refresh_worker()` refreshes `METAGRAPH_WARM_NETUIDS` (79, 71, 88)
   every `METAGRAPH_REFRESH_SECONDS` (90 s, comfortably inside the 120 s TTL), one
   netuid at a time with a 2 s gap — each is a chain connection, and firing three at
   once is the burst pattern that made the balance sweep starve unrelated queries.
2. `_refresh_metagraph()` is single-flight and `asyncio.shield`ed, so the worker and
   a cold request never spawn two subprocesses, and a browser hanging up mid-request
   can't cancel a load other callers are waiting on.
3. The endpoint is now **stale-while-revalidate**: an expired-but-present entry is
   returned immediately while the refresh runs behind the request. `fetched_at` is
   stamped into the payload so the client can see the age; `X-Cache: hit|stale|miss|stale-error`
   says the same thing to whoever is holding curl.

A forced `?refresh=1` that fails now returns the last good snapshot rather than an
error — blanking the page is worse than showing data a minute old. Errors on a cold
cache changed from **500 to 502**, matching the documented convention that 502 means
an upstream failed.

### 3.3 `/api/regbot/active` did an SSH fan-out on the request path, three times per load

`get_active_runs()` probes all 12 `SERVER_PRESETS` and tails a log per live run —
correctly concurrent, correctly non-blocking, and **690 ms** every single call, with
no cache. It held one of the browser's six connection slots long enough to cause
610 ms of `stall` on everything behind it, and it ran three times per load because
`updateTableColumns()` — a render function — re-triggered all three active-run
loaders on top of the `DOMContentLoaded` call and the 20 s timer.

**Fixed:**
- `_regbot_active_scan_worker()` owns the scan at `REGBOT_ACTIVE_SCAN_SECONDS` (20 s).
  The endpoint reads its last result out of memory: **690 ms → 1.3 ms**, and the cost
  no longer scales with the number of open tabs.
- A failed scan keeps the previous result rather than blanking the bar — a briefly
  unreachable box should not look like "no runs are active".
- `?refresh=1` forces a live scan. The UI passes it only after an action that just
  changed what is running (trigger, kill, a batch step finishing), where waiting up
  to 20 s for a pill to appear or disappear would look broken. Background polls must
  never pass it.
- The `updateTableColumns()` calls are gone. Hiding the SN79-only bars on a subnet
  switch moved to `applySubnet()`, which is the user action that actually changes it.

### 3.4 ~310 ms of synchronous CPU inside `async def` stalled the one event loop

One fork-mode uvicorn process, all 50 handlers `async def`. Measured in isolation
with the loop idle: `fee-regime/history` **135 ms**, `miner-params` **99 ms**,
`uid-metrics` **44 ms**, plus ~10 ms × 3 re-serialising cached metagraphs. Arriving
together, that is ~300 ms of strictly serial work — which is exactly the uniform
~270 ms `serv` the waterfall showed on `/api/sim-id` (1.2 ms alone),
`/api/unstake/active` (1.0 ms) and `/api/coldkeys/summary` (1.7 ms).

**Fixed:** the hot read paths now cache **serialised bytes**, not dicts, so a warm
request neither reads the disk nor runs `json.dumps`:

| endpoint | before | after |
|---|---|---|
| `/api/miner-params` | 99 ms | **1.2 ms** |
| `/api/uid-metrics` | 44 ms | **1.1 ms** |
| `/api/fee-regime/history?days=1` | 135 ms | **1.3 ms** |

The primitives live at the top of [app.py](../../../app.py): `_serialize_json()`
(same `json.dumps` settings FastAPI's `JSONResponse` uses, so the wire format is
unchanged), `_ByteCache` (TTL + explicit invalidation), and `_json_bytes_response()`.

Everything left on the request path that touches the filesystem goes through
`run_in_threadpool` — the params scan, `get_cached_metrics_payload`,
`build_fee_history`, `build_uid_history`, `build_agent_history_response`.

**Invariant:** `_save_miner_params_file()` calls `_miner_params_cache.invalidate()`.
Any new writer of `79Miners/params/` must do the same, or the agent column shows the
pre-edit value for up to `MINER_PARAMS_CACHE_SECONDS` after a successful save.

### 3.5 The `loadData()` tail was four sequential round trips with no data dependency

```js
tail = loadMinerAgentInfo().then(loadUidMetrics);
return tail.then(() => Promise.all([loadSubnetRegCost(...), loadAlphaPrice()])).then(afterLoad);
```

`loadUidMetrics()` requests `/api/uid-metrics` **with no `uids` parameter**, so it
consumed nothing `loadMinerAgentInfo()` produced; the last two take only a netuid.
(`.then(loadUidMetrics)` also silently passed the previous result as `refresh` — it
happened to be `undefined`, so it worked by accident.)

**Fixed:** one `Promise.all` per subnet branch. Each element is wrapped in
`settled()`, because the panel loaders now share a promise with the table render and
one of them throwing inside a `.then()` must not be able to stop the table painting —
which it couldn't when they were fire-and-forget.

### 3.6 Two full metagraphs fetched for one number each

`fetchSubnetEmissionBackground(71)` and `(88)` pulled the **entire** metagraph —
144 KB and 156 KB — to sum non-validator emission client-side, for subnets the user
is not looking at.

**Fixed:** `GET /api/subnet-emission?netuid=` does the sum against the same warm
cache and returns `{netuid, emission, alphaPriceTao, fetched_at}`. **300 KB → 188 bytes.**

### 3.7 The HTML was 373 KB, unversioned, uncached

One 10 968-line file with all CSS and ~7 300 lines of JS inline, no `ETag`, no
`Cache-Control` — re-downloaded in full on every navigation, and uncacheable because
it was rendered as a template.

**Fixed:** `miners.html` contains **zero Jinja tags** (verified), so "rendering" it
was running a template engine for nothing.

- The JS moved to [`asset/miners.js`](../../../asset/miners.js), served by app.py's
  `/js/miners.js` route with an mtime-keyed ETag. Deliberately **not** under the
  `/asset` StaticFiles mount: that mount sends no `Cache-Control`, which leaves
  browsers free to heuristically serve a stale copy of the app after a deploy.
- The document is read once per mtime and served as bytes with its own ETag.

Document: **373 KB → 110 KB raw / 15 KB gzipped**, and a repeat load gets `304` for
both it and the JS.

### 3.8 The client polled in hidden tabs

Five loops ran regardless of `document.visibilityState` — including the 690 ms SSH
fan-out at 20 s. Three idle background tabs meant a sweep of 12 servers every ~7 s.

**Fixed:** `startBackgroundPolling()` is now the only place background intervals are
created, and `pollWhenVisible()` skips a tick while the tab is hidden. A
`visibilitychange` listener refreshes the bars once on return, and re-runs `loadData`
if its data is over a minute old. Adding a background poll anywhere else
re-introduces the problem.

### 3.9 `/api/fee-regime/history` shipped ~740 KB the chart never read

The largest single payload. Reading the renderer settled it:

```js
var latestBookPoint = bookPoints[bookPoints.length - 1];
booksPayload = latestBookPoint.books;
```

The books chart plots **only the newest snapshot**. Every earlier one — 128 books ×
92 snapshots, ~99 % of the response — was downloaded, parsed and discarded.

**Fixed:** `?book_points=latest|all|none`, defaulting to `all` so any other caller
keeps the old shape; the UI passes `latest`. **754 KB → 30 KB raw, 171 KB → 6 KB gzipped.**

`FEE_HISTORY_MAX_POINTS` (600) additionally caps the time series via `_downsample()`,
which keeps points evenly spaced and always retains the newest one — a plain stride
drops it whenever `len % stride != 1`, and the last snapshot is what the "current"
readouts key off. At the default `days=1` (~93 points) it does not engage; it matters
at `days=30` and beyond.

### 3.10 Weak ETags on the poll endpoints

`batch/active` (19 KB every 15 s) and `unstake/active` are byte-identical between
most polls. Both now return `_json_bytes_response`, so an unchanged re-poll is a
`304` with no body and no compression pass. Verified: `/`, `/js/miners.js`,
`/api/metagraph-data`, `/api/miner-params`, `/api/uid-metrics`, `/api/regbot/batch/active`
all answer `304 (0 bytes)` to a matching `If-None-Match`.

---

## 4. New configuration

All read from the environment, all with working defaults:

| Variable | Default | Controls |
|---|---|---|
| `METAGRAPH_REFRESH_SECONDS` | `90` | warm-refresh cadence; must stay under `METAGRAPH_CACHE_TTL` (120) |
| `METAGRAPH_WARM_NETUIDS` | `79,71,88` | which metagraphs the worker keeps warm |
| `REGBOT_ACTIVE_SCAN_SECONDS` | `20` | SSH active-run scan cadence |
| `MINER_PARAMS_CACHE_SECONDS` | `30` | params-roster byte cache (also invalidated on write) |
| `UID_METRICS_CACHE_SECONDS` | `30` | uid-metrics byte cache |
| `FEE_HISTORY_CACHE_SECONDS` | `60` | fee-history byte cache |
| `FEE_HISTORY_MAX_POINTS` | `600` | per-series cap before downsampling |

---

## 5. What is next, if this needs to go further

Not done, in payoff order:

0. ~~Trim `/api/uid-metrics`~~ and the rest of the payload work — **done**, see
   [wire_protocol.md](wire_protocol.md).
1. **Self-host the CDN assets.** Bootstrap, Bootstrap Icons and Chart.js are 138 KB
   across 4 requests to `cdn.jsdelivr.net`, and the render-blocking stylesheet costs
   a DNS + TCP + TLS handshake to a second origin before first paint — roughly 4 RTTs,
   which at 250 ms RTT is most of the remaining 738 ms FCP. Serving them from
   `/asset/` puts them on the connection that is already open.
2. **A `/api/health` endpoint** reporting last-success timestamps per worker. Three
   of the seven workers are new, and worker failure is still invisible in the UI —
   which is how [known_issues.md](../tricks/known_issues.md) issue 1 stayed unnoticed
   for weeks.

---

## 6. What was already right

None of this was changed, and none of it should be:

- Chain work runs in a subprocess, never a thread — [chain_subprocess.md](chain_subprocess.md).
  The metagraph worker calls the same `load_metagraph.py`; it changed *when* the
  subprocess runs, not *that* it does.
- SSH is multiplexed with a keepalive worker — [ssh_multiplexing.md](ssh_multiplexing.md).
- `get_active_runs()` fans out with `asyncio.gather` over `asyncio.create_subprocess_exec`,
  so its 690 ms is 12 servers in parallel, not serial. The problem was only that it
  ran on the request path.
- Caches carry forward stale-but-real values instead of rendering zeros —
  [stale_data_handling.md](../tricks/stale_data_handling.md). The metagraph SWR path
  and the regbot scan's error handling both extend that rule rather than break it.
