# On-disk data layout

Everything is flat files. There is no database in the live path.

## The one file that defines ownership

```
79Miners/params/run_miner_<uid>.json
```

Its **existence** is how the system knows a UID is yours — `list_my_miner_uids()`
([miner_history.py:39](../../79Miners/monitor/miner_history.py#L39)) is a directory
scan, and every "my miners" feature keys off it. Its **contents** are the miner's
live runtime config, re-read by the remote dispatcher on every poll, so editing it
hot-swaps behaviour with no restart.

Shape (abridged):

```json
{
  "miner_ip": "65.108.232.96",
  "agent_index": 42,
  "agents": [
    { "agent_id": 42, "name": "GammaPrimeAgent", "params": { "max_quantity": 1.0, ... } },
    ...
  ]
}
```

`agent_index` is an **`agent_id`, not a list position**. See
[../sn79/miners/miner_params.md](../sn79/miners/miner_params.md).

`79Miners/params/dispatch_example.json` is the canonical roster of every available
sub-agent. `AGENT_DEFAULTS_FILE` points at the *authoritative* copy on the RegBot
box (`/root/79/Paul-SuperRegBot/myAgents/dispatch_example.json`); the in-repo copy
is what the dashboard reads for the agent dropdown.

## Directory map

| Path | Written by | Format | Retention |
|---|---|---|---|
| `79Miners/params/` | RegBot pull, params editor, auto-switcher | JSON per UID | forever |
| `79Miners/RegBot/regbot_runs.jsonl` | `_append_audit_log` | JSONL, one per trigger | forever |
| `79Miners/RegBot/active_batch.json` | `_save_batch_state` | JSON, whole batch list | rewritten |
| `79Miners/RegBot/balance_cache.json` | balance worker | JSON snapshot | rewritten every 300s |
| `79Miners/Unstake/unstake_runs.jsonl` | unstake audit | JSONL | forever |
| `79Miners/Unstake/logs/<run_id>.log` | unstake child stdout | text | forever |
| `79Miners/Coldkeys/out/` | `collect_coldkeys_ips.py` + watcher | JSON | rewritten |
| `miner_status/<YYYY-MM-DD>.json` | `save_daily_status` | JSON, all UIDs | one file/day |
| `miner_status/prev_epoch_metrics.json` | `_save_prev_epoch_metrics` | JSON | rewritten |
| `miner_status/reg_cost_by_block_79.json` | `save_reg_cost_cache` | `{block: {rao,tao}}` | grows |
| `miner_history/<YYYY-MM-DD>.jsonl` | `append_my_miners_history` | JSONL snapshots | one file/day |
| `agent_history/<uid>.jsonl` | `record_agent_switch` | JSONL per UID | forever |
| `fee_history/<YYYY-MM-DD>.jsonl` | `append_fee_regime_history` | JSONL | one file/day |
| `volume_agent_switches/<YYYY-MM-DD>.jsonl` | auto-switcher | JSONL | one file/day |
| `investing88_cache/<YYYY-MM-DD>.json` + `latest.json` | SN88 sync | JSON | one file/day |
| `79Miners/monitor/status_history.csv` | status_server | CSV | rolling 3h |
| `79Miners/monitor/books_history/books_<uid>.csv` | status_server | CSV | rolling 3h |
| `params_backups/` | manual | copies of `params/` | protection against `rsync --delete` |

All of these are gitignored ([.gitignore](../../.gitignore)) — the repo tracks code,
not fleet state.

## Why append-only JSONL for some things and rewrite-JSON for others

- **JSONL, append-only**: anything that is a record of something that happened —
  a registration trigger, an unstake, an agent switch, a metrics snapshot. Appends
  are atomic enough at these sizes and a crash can never corrupt earlier lines.
- **Whole-file JSON, atomically replaced**: anything that is *current state* —
  balance cache, batch state, daily status. Written to `<path>.tmp` then
  `os.replace()`, which is an atomic same-filesystem rename, so a reader never
  sees a half-written file. See [../sn79/tricks/crash_safe_state.md](../sn79/tricks/crash_safe_state.md).

## Day-boundary behaviour

Daily files use `date.today()` (**local** time on the dashboard host), except the
SN88 cache which uses `Asia/Tokyo` explicitly
([investing88_cache.py:49](../../88Miners/monitor/investing88_cache.py#L49)). The
SN79 registration-date display in the UI also renders in Tokyo time
(`formatInstantInTokyo`). If you compare a `miner_status/` filename against an SN88
cache filename, they are not the same clock.
