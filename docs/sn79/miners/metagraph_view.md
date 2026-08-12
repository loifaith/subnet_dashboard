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
- The loader still runs [load_metagraph.py](../../../load_metagraph.py) **as a child
  process** (5s to spawn, 90s to finish), single-flight per netuid. Never in-process —
  see [../performance/chain_subprocess.md](../performance/chain_subprocess.md).
- `?refresh=1` forces a fresh load. If it fails and a cached copy exists, the cached
  copy is returned (`X-Cache: stale-error`) rather than an error.
- `fetched_at` (unix seconds) is stamped into the payload so a client can tell how
  old a snapshot is. `X-Cache` says the same thing to curl.
- Responses carry a weak `ETag`; an unchanged re-poll is a `304`.

For just the header emission figure, use `GET /api/subnet-emission?netuid=<n>` —
same warm cache, ~100 bytes instead of ~150KB.

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
10000 blocks if that call fails ([app.py:613-618](../../../app.py#L613-L618)).

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
