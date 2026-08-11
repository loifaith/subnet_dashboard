# Multi-server unstake batches

Unstake (and optionally transfer) several coldkeys at once — one server per item.

`start_unstake_batch()` — [unstake.py:432](../../../79Miners/monitor/unstake.py#L432)

## Parallel, unlike RegBot's batch

This is the deliberate opposite of [../regbot/batches.md](../regbot/batches.md):

```python
await asyncio.gather(*[_run_one_batch_item(item, batch_id) for item in queue])
```

The docstring states the reason:

> Unlike RegBot's batch, there's no shared price limit to protect by serializing:
> each coldkey's unstake is fully independent, so running them at the same time is
> both what was asked for and the technically correct call here.

RegBot serialises because every item competes for the same chain registration price.
Unstakes have no such contention.

## Validation up front

Before anything launches:

- **no duplicate servers** in one batch — the same coldkey twice is a double-submit;
- each item passes the same `_validate()` as a solo run;
- each server passes `_server_busy()` — a solo run already in flight blocks the batch;
- **only one batch at a time** globally (`get_active_unstake_batch()`).

Rejecting the whole batch on any of these is better than discovering it halfway
through moving funds.

## A batch item is just a normal run

`_run_one_batch_item()` calls `start_unstake_run(..., triggered_by=f"batch:{id}")`
and then polls `get_unstake_run_status()` every 5s. So batch items get the same
audit entries, the same log files, the same process-group isolation and the same
post-run balance refresh as a solo run.

It also means `get_active_unstake_runs_view()` — and therefore the `#unstakeActiveBar`
at the top of the page — shows **both** solo runs and in-flight batch items in one
bar, with no special-casing.

## Endpoints

| Path | Purpose |
|---|---|
| `POST /api/unstake/batch/start` | `{items: [{server, mode, destination, amount}]}` |
| `GET /api/unstake/batch/active` | the single active batch, or `null` |
| `GET /api/unstake/batch/{id}` | full queue state |
| `GET /api/unstake/active` | flat list of in-flight runs (solo + batch) |
| `GET /api/unstake/history?limit=` | recent audit entries |

There is **no cancel endpoint** for unstake batches, unlike RegBot. Once an extrinsic
is submitted there is nothing useful to cancel, and killing the wrapper mid-run is
the failure mode the timeout handling already covers.

## Not crash-safe

Unlike RegBot batches, unstake batch state lives **only in memory** — there is no
`active_batch.json` equivalent. A `pm2 restart dashboard` loses the batch's
bookkeeping.

What survives: the individual `unstake_env.py` children, because of
`start_new_session=True`. They keep running to completion and their logs keep
filling. What is lost: the batch view, the per-item status, and the post-run
balance refresh (that runs in the parent task).

To reconstruct after a restart, read `79Miners/Unstake/logs/<run_id>.log` and
`unstake_runs.jsonl`. Making this crash-safe would mean copying RegBot's persisted
state pattern ([../tricks/crash_safe_state.md](../tricks/crash_safe_state.md)).

## UI

`openMultiUnstakeModal` → per-server rows with mode/destination/amount →
`multiUnstakeSelectedItems()` → estimate → confirm dialog restating every
destination → `showMultiUnstakeProgress` polling every 3s.
