# History: performance and agent switches

Two independent append-only histories, two endpoints, two modals.

## Performance history

**Written** by `append_my_miners_history()`
([miner_history.py](../../../79Miners/monitor/miner_history.py)) at the end of every
daily-status refresh (~every 15 min).

```
miner_history/<YYYY-MM-DD>.jsonl
  {"snapshot_at": iso, "current_block": n, "miner_count": k,
   "miners": {"<uid>": {kappa, real_pnl, unrl_pnl, volume, vol_mkr, vol_tkr,
                        hot_book_count, book_count, max_book_volume, incentive}}}
```

One line per snapshot, all owned miners in that line. Only keys with non-null
values are stored, so a row is sparse rather than full of nulls.

**Read** via:

- `GET /api/miner-history` → `{tracked_uids, days}` — [app.py:1446](../../../app.py#L1446)
- `GET /api/miner-history/{uid}?days=7&day=&limit=` — [app.py:1456](../../../app.py#L1456)

`days` is clamped to 1–90. A UID not in `list_my_miner_uids()` returns 404 — the
history only exists for miners you own.

The modal renders Chart.js line charts (`renderHistoryCharts`, ~line 6649 of
miners.html) for kappa, PnL, volume and book counts.

## Agent switch history

**Written** by `record_agent_switch()`
([agent_history.py](../../../79Miners/monitor/agent_history.py)) from three callers:

| `source` | Caller |
|---|---|
| `manual` | params PUT / bulk edit where the active agent changed |
| `params_update` | params PUT / bulk edit, same agent, params changed |
| `auto_volume` | [../automation/volume_agent_switcher.md](../automation/volume_agent_switcher.md) |

```
agent_history/<uid>.jsonl
  {timestamp, from_index, from_agent, to_index, to_agent, source,
   miner_ip, rsynced, saved_locally, rsync_message, extra: {...}}
```

`extra` carries the param diff (`params`, `removed`) for edits, or
`hot_book_count` / `max_book_volume` for auto-switches.

`rsynced: false` plus an `rsync_message` is how a failed push is preserved — the
history is the record of *what was attempted*, not only what succeeded.

**Read** via `GET /api/agent-history/{uid}?limit=50` (clamped 1–500) —
[app.py:1477](../../../app.py#L1477). A UID with no file returns
`{"count": 0, "entries": []}` rather than 404, since "never switched" is normal.

Timestamps are rendered in `Asia/Tokyo` (`TOKYO = ZoneInfo("Asia/Tokyo")`).

## Retention

Neither history is pruned. `miner_history/` grows by one file per day; a single
day's file is roughly `96 snapshots × miners × 10 fields`. `agent_history/` grows
only when something changes. Both are gitignored.

The **rolling 3h** per-book history is a different thing entirely and lives in the
status server — see [../status_server/overview.md](../status_server/overview.md).
