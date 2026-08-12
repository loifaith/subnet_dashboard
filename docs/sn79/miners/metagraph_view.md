# Metagraph view

The base layer of the page: every neuron on the selected subnet, its incentive,
emission, coldkey, immunity and age.

## Endpoint

`GET /api/metagraph-data?netuid=<n>[&refresh=1]`

- Cache keyed by netuid, holding the dict **and its serialised bytes**, kept warm by
  `_metagraph_refresh_worker` at 90s against a 120s `METAGRAPH_CACHE_TTL`.
- **A request never triggers the chain.** An expired-but-present entry is returned
  immediately (`X-Cache: stale`) while the refresh runs behind the request. Cold load
  is ~2.9s, warm ~13ms, which is why this is worth the machinery — see
  [../performance/page_load_audit.md](../performance/page_load_audit.md) §3.2.
- The loader asks `chain_daemon.py` first, which syncs over a connection it already
  holds: **~0.46s against ~2.97s** for a fresh child process. If the daemon is
  unreachable it falls back to running
  [load_metagraph.py](../../../load_metagraph.py) as a child process (5s to spawn,
  90s to finish). Single-flight per netuid either way, and never in-process — see
  [../performance/chain_daemon.md](../performance/chain_daemon.md) and
  [../performance/chain_subprocess.md](../performance/chain_subprocess.md).
- Both paths build their payload with `load_metagraph.build_payload()`, so the fast
  path and the fallback cannot return different shapes.
- `?refresh=1` forces a fresh load. If it fails and a cached copy exists, the cached
  copy is returned (`X-Cache: stale-error`) rather than an error. A forced read
  landing within `CHAIN_BLOCK_SECONDS` (12s) of the last successful one is answered
  from cache (`X-Cache: hit-coalesced`): inside a block there is no new state to
  read, so the 2.9s subprocess would only reproduce the same numbers.
- `fetched_at` (unix seconds) is stamped into the payload so a client can tell how
  old a snapshot is. `X-Cache` says the same thing to curl.
- Responses carry a weak `ETag`; an unchanged re-poll is a `304`.

For just the header emission figure, use `GET /api/subnet-emission?netuid=<n>` —
same warm cache, ~100 bytes instead of ~150KB.

## The table follows the worker, not a timer

The server publishes `metagraph:<netuid>` whenever a refresh lands a snapshot whose
ETag changed, and **the client subscribes to it**. Measured in a real browser, the
two halves are refetched at 32s, 122s and 212s after load — every 90s, tracking
`METAGRAPH_REFRESH_SECONDS` — where `loadData`'s flat 5-minute poll used to be the
only trigger. Identity comes back `304`; only state carries a body.

This is fresher rather than cheaper: ~5KB per push, because the state half really
does change every block. Details and the three guards in
[../performance/wire_protocol.md](../performance/wire_protocol.md).

## The page does not use this endpoint

It fetches **two halves** instead, because they change on completely different
clocks:

| Endpoint | Contains | Changes | gzip |
|---|---|---|---|
| `/api/metagraph-identity` | hotkey ↔ uid ↔ coldkey, validator flags, IPs, `order` | on registration churn | 20 KB |
| `/api/metagraph-state` | emission, incentive, age, immunity, current block | every block | **5 KB** |
| `/api/metagraph-data` | both, as one object | every block | 28 KB |

State is columnar — arrays positional against identity's `order` — and the client
merges the two back into exactly the shape `processMetagraph()` has always read, so
nothing downstream knows about the split.

**If you touch this, read the hazard first.** A client holding a stale identity
would map every state value onto the wrong hotkey, and a length check does not catch
it: one deregistration plus one registration in the same block keeps the count
identical. State therefore carries `identity_etag`, and the client refetches
identity unconditionally on a mismatch. Details in
[../performance/wire_protocol.md](../performance/wire_protocol.md).

`/api/metagraph-data` is kept for anything outside the dashboard.

Response is a bundle of hotkey-keyed maps, shaped to match the old subnet71 API:

```
hotkeyToUid, uidToHotkey, hotkeyToColdkey, coldkeyToHotkeys,
emissions, incentives, isValidator, isImmune, minerAge,
totalNeurons, netuid, currentBlock, immunityPeriod, error
```

## Immunity

Computed client-of-chain rather than read directly:

```
age_blocks = current_block - last_update[i]
is_immune  = age_blocks < immunity_period
```

`immunity_period` comes from `subtensor.immunity_period(netuid)`, defaulting to
10000 blocks if that call fails ([app.py:998](../../../app.py#L998)).

Note this uses `last_update`, not the registration block. The *registration*-based
view (reg date, reg cost, blocks since registration) comes from a different source —
see [reg_cost.md](reg_cost.md) and `fetch_uid_registration_info()`.

## "My miners"

A miner counts as yours if its coldkey is in the browser's registered-coldkey list
(`localStorage["registeredColdkeys"]`, seeded from `DEFAULT_COLDKEYS` in
[miners.js](../../../asset/miners.js)) **or** it has a params file. There are
two view modes; `newColdkeysOnly()` switches the stat cards into a "Testing" label
for coldkeys the watcher has flagged as new
([coldkeys.md](coldkeys.md)).

## Stat cards

| Card | Computation |
|---|---|
| Total Miners | `minersArray.length` |
| Average / Top Incentive | mean / max of `incentive` |
| Immune Miners | count where `immune` |
| My Miners | count + `total X% · avg Y%` |
| My TAO/Day | `Σ(emission × alpha_price × 20)` |
| My TAO/Epoch #N | `Σ(emission × alpha_price)`, `N = floor(currentBlock / 360)` |

The ×20 is 20 epochs/day at 360 blocks per epoch. `alpha_price` comes from the
subnet emission background fetch, so both cards render `…` until it lands and `-`
if it never does — deliberately distinguishable states.

## Chart

A Chart.js bar/scatter of the incentive distribution with per-coldkey colouring
(`getColdkeyColor`, a stable hash → HSL) and highlight support. `exportCSV()` dumps
the current filtered table.

## Subnet switching

`#subnetInput` (default **79**) drives everything. `applySubnet()` reloads coldkeys
then `loadData(true)`. Netuid 79 enables the fee panel, sim id, agent info and UID
metrics; 88 swaps in the Investing88 columns; anything else shows the bare
metagraph. See [`loadData()`](../../../asset/miners.js).

## What `loadData(true)` does — and does not — wait for

The `true` is `forceRefresh`, and it reaches three endpoints as `?refresh=1`:
`/api/metagraph-data`, `/api/fee-regime`, `/api/reg-cost`. Each is a 2-3s upstream
call.

**None of them gate the paint.** The table renders from the warm metagraph halves
first and the forced chain re-read runs behind it via `forceMetagraphReread()`,
repainting when it lands. The Refresh button's icon spins until then; the page is
interactive throughout. Measured after the chain daemon landed: table visible
**431ms** from navigation, and the whole Refresh cycle — click to button
re-enabled, including the forced chain read — **682ms**, down from 3314ms. A re-read arriving within one block
of the last one is answered from cache (`X-Cache: hit-coalesced`) rather than
repeating a query that cannot produce different numbers.

Two rules this encodes, both learned from outages:

- a forced re-read is *newer data*, never *the only data* — the warm copy is at most
  `METAGRAPH_REFRESH_SECONDS` old and is always good enough to render;
- if the user switches subnet while a re-read is in flight, its result is dropped.
  Merging netuid 79's metagraph into an 88 table is worse than showing nothing new.

See [../performance/wire_protocol.md](../performance/wire_protocol.md#the-refresh-button-was-the-last-blocking-path).
