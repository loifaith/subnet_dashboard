# Volume auto-switcher

> ⚠️ **This module currently fails to import.** See
> [../tricks/known_issues.md](../tricks/known_issues.md). Everything below describes
> the intended behaviour, which has not run since ~2026-07-21.

[79Miners/monitor/volume_agent_switcher.py](../../../79Miners/monitor/volume_agent_switcher.py)

## The problem it solves

Each book has a daily volume cap (`BOOK_VOLUME_LIMIT = 500_000`). A book at ≥98% of
that cap is "hot" — further trading on it earns nothing. A miner with many hot books
is burning compute for no incentive. The fix is to switch it to `FreeAllAgent`,
which stops consuming volume.

## The rule

Called at the end of every daily-status refresh via
`maybe_switch_hot_miners_to_free_all(metrics)`:

```
for each owned uid:
    if hot_book_count <= HOT_BOOK_SWITCH_THRESHOLD (20):        skip
    if blocks_since_registration < IMMUNITY_PERIOD_BLOCKS (7200): skip  (immune)
    if current agent is already FreeAllAgent:                    skip
    if current agent is in AUTO_SWITCH_EXEMPT_AGENTS:            skip
    else: set agent_index = FreeAllAgent's agent_id, save, rsync, log
```

### The immunity guard

An immune miner is still establishing itself; switching it to a passive agent during
immunity wastes the immunity window. `blocks_since_registration` comes from the
registration info merged into `metrics` during the refresh, and a missing/unparseable
value **defaults to `IMMUNITY_PERIOD_BLOCKS`** — i.e. "assume not immune", so a data
gap doesn't freeze the switcher permanently.

### Exempt agents

`AUTO_SWITCH_EXEMPT_AGENTS` (env `VOLUME_SWITCH_EXEMPT_AGENTS`) is a set of agent
names that must never be auto-switched away — strategies where hot books are
expected or the switch would be counterproductive. Intended members:
`RandomMakerAgentHarvester`, `WarriorAgent`, `SpearAgent`, `OFTAgent`, `CBSAgent`,
`COCAgent`.

**This is the line that is broken** — see the known-issues doc.

## Kill switch

`HOT_BOOK_AUTO_SWITCH=0` (or `false`/`no`/`off`) disables the whole thing.
`maybe_switch_hot_miners_to_free_all` returns `[]` immediately.

## Every skip is explicit

`switch_uid_to_free_all()` returns a structured result with a `reason` for every
non-action: `no_params_file`, `read_failed`, `missing_agents`,
`invalid_agent_index`, `free_all_agent_not_found`, `already_free_all`,
`exempt_agent`. No silent no-ops — the result set tells you exactly why each miner
was or wasn't touched.

## Writes

On a real switch it uses `save_and_sync_params()` from
[miner_params_sync.py](../../../79Miners/monitor/miner_params_sync.py) (which *does*
use rsync, unlike the dashboard's SSH-stream push), then writes **two** records:

1. `agent_history/<uid>.jsonl` with `source="auto_volume"` and
   `extra = {hot_book_count, max_book_volume}` — so an automatic switch is
   indistinguishable in shape from a manual one, and shows up in the same UI.
2. `volume_agent_switches/<YYYY-MM-DD>.jsonl` — a dedicated daily log of every
   switch decision including the skips.

A failed rsync is logged at **error** level (`UID %s rsync failed after switch`)
because the local file now disagrees with the running miner.

## Note on `agent_index`

`_find_agent_id()` returns the `agent_id` of the first agent with the target name,
falling back to list position when `agent_id` is absent — the same
id-not-position rule as everywhere else
([../miners/miner_params.md](../miners/miner_params.md)).
