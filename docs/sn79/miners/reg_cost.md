# Registration cost

Two related things: the **current** price to register on a subnet (shown in the
header, and used to sanity-check a RegBot run), and the **historical** price at each
miner's own registration block (shown per-row in the table).

## Current price

`GET /api/reg-cost?netuid=79[&refresh=1]` — [app.py:2782-2783](../../../app.py#L2782-L2783)

- Served from `chain_daemon.py`'s **block-paced poller**: a ~1ms socket read with
  no chain query behind it, and never more than one block (~12s) old.
- `_reg_cost_refresh_worker` therefore refreshes every 10s
  (`REG_COST_REFRESH_FAST_SECONDS`) instead of 240s — it costs nothing, and the
  daemon polls once regardless of how often we ask.
- **Fallback**: if the daemon is unreachable it runs `chain_cli.py reg-cost
  <netuid>` as a child process (120s timeout) and the worker drops back to 240s.
  The interval is derived from the recorded `source`, never assumed — running the
  2.1s subprocess every 10s would be far worse than the 240s it replaced.

The polling loop is modelled on `BT_79_Dashboard/scripts/reg_price_watcher.py` and
the reg bot's `ChainPollSession`: registration cost only changes when a block
lands, so fast-poll at 1s to catch the turn and settle-sleep 10s once a new block
is seen. Verified tracking blocks 8828405 → 8828408 with the recycle price decaying
each block, where it used to be up to 240s stale.
See [../performance/chain_daemon.md](../performance/chain_daemon.md).

This was also the canonical example of why chain work is out-of-process: the query
is ~5 seconds of pure-Python substrate metadata decoding, and run in-process it
held the GIL for that whole time, pushing unrelated request latency from ~1ms to
6–400ms. That rule is unchanged — the daemon is its own process.
See [../performance/chain_subprocess.md](../performance/chain_subprocess.md).

## Historical price per registration block

`fetch_reg_costs_by_block()`
([get_reg_cost.py:256](../../../79Miners/monitor/get_reg_cost.py#L256)) resolves a set
of registration blocks to their burn cost, and is called during the daily status
refresh.

Three layers of thrift, because these are expensive archive queries:

1. **Persistent cache**: `miner_status/reg_cost_by_block_79.json`, keyed by block.
   Registration cost at a past block never changes, so this cache never expires.
2. **`max_new=48`**: at most 48 uncached blocks are fetched per refresh. New miners
   fill in over successive cycles rather than stalling one refresh for hours.
3. **Endpoint selection** (`_pick_endpoint`): blocks within 900 of the current block
   go to `finney`; anything older goes to the archive node
   `wss://archive.chain.opentensor.ai:443`. Finney doesn't retain old state, and the
   archive node is slower and rate-limits, so this picks the cheapest one that can
   actually answer.

Blocks are grouped by endpoint and each endpoint opens **one** connection for its
whole group, closed in a `finally`.

## Connection retries

`connect_subtensor()`
([get_reg_cost.py:66](../../../79Miners/monitor/get_reg_cost.py#L66)) retries with
exponential backoff capped at 60s — but detects HTTP 429 specially and backs off
much harder (min 15s base, capped at 120s). Archive nodes rate-limit, and the
normal backoff was not enough to be forgiven.

## In the UI

- `loadSubnetRegCost()` / `renderSubnetRegCost()` — header chip.
- `renderRegCostWarning()` in the Add Miner modal — compares the live price against
  the `reglimit` you're about to set, so you don't launch a run that can never fire
  (or one that will overpay).
- Per-row `reg_cost_tao` comes from the daily status file, merged by
  `merge_reg_costs_from_cache()`.
