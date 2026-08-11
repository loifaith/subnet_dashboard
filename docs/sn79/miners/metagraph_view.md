# Metagraph view

The base layer of the page: every neuron on the selected subnet, its incentive,
emission, coldkey, immunity and age.

## Endpoint

`GET /api/metagraph-data?netuid=<n>[&refresh=1]` — [app.py:1284](../../../app.py#L1284)

- 2-minute in-process cache keyed by netuid (`METAGRAPH_CACHE_TTL = 120`).
- On a miss it runs [load_metagraph.py](../../../load_metagraph.py) **as a child
  process**: 5s to spawn, 90s to finish. Never in-process — see
  [../performance/chain_subprocess.md](../performance/chain_subprocess.md).
- `?refresh=1` skips the cache. The UI's Refresh button also appends `&t=<now>`
  to defeat any HTTP caching.

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
10000 blocks if that call fails ([app.py:381-386](../../../app.py#L381-L386)).

Note this uses `last_update`, not the registration block. The *registration*-based
view (reg date, reg cost, blocks since registration) comes from a different source —
see [reg_cost.md](reg_cost.md) and `fetch_uid_registration_info()`.

## "My miners"

A miner counts as yours if its coldkey is in the browser's registered-coldkey list
(`localStorage["registeredColdkeys"]`, seeded from `DEFAULT_COLDKEYS` in
[miners.html](../../../templates/miners.html)) **or** it has a params file. There are
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
metagraph. See [`loadData()`](../../../templates/miners.html) ~line 4413.
