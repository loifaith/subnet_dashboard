# Crash-safe state

The dashboard restarts often — deploys, `pm2 restart`, the occasional crash. Any
work that outlives a request has to survive that.

## Atomic writes, everywhere

```python
tmp = f"{path}.tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.replace(tmp, path)        # atomic same-filesystem rename
```

Used by `_save_batch_state`, `refresh_balance_cache`, `save_daily_status`,
`_save_prev_epoch_metrics`, `_apply_defaults_bulk`, `_write_csv`, `_prune_books_csv`.

A reader either sees the whole old file or the whole new one — never a truncated
JSON document. The same idea applies over SSH in the params push
(`cat > tmp && mv -f tmp final`, temp file in the **target's own directory** so the
rename stays on one filesystem).

`_save_batch_state()` additionally **never raises**: losing persistence is bad, but
not a reason to take down a batch that is otherwise running.

## Persist before you act

The pattern that makes RegBot batch resume correct:

```python
item["status"]  = "RUNNING"
item["run_id"]  = item.get("run_id") or f"{server}_{_make_run_id()}"
_save_batch_state()                    # <- persisted BEFORE the launch
await trigger_run(..., run_id=item["run_id"])
```

Assigning the identity **before** the side effect means a crash mid-launch still
leaves a record of *which* run to go look for. Without that, resume would have to
infer from whatever happens to be running on the box — which is unsafe as soon as
two runs can be live on one server.

Same principle in `cancel_batch()`: the `CANCELLING` flag is persisted immediately,
so a restart landing between the flag and the loop noticing it doesn't come back up
and resume a cancelled batch.

## Ask a definite question on resume

`_remote_run_exists(server, run_id)` has three outcomes, and the third is the point:

| Answer | Action |
|---|---|
| log/pid file exists | re-attach, don't launch |
| neither exists | launch under the same pre-assigned `run_id` |
| **server unreachable / timeout** | **raise** → mark `LAUNCH_FAILED` |

> "no answer" must never be read as "never launched" — relaunching a run that did
> start double-registers and double-spends.

Defaulting to the safe-looking action ("just launch it") is exactly what would burn
TAO here.

## Don't write a terminal status you didn't reach

```python
except asyncio.CancelledError:
    interrupted = True
    logger.info("Batch %s: interrupted by shutdown, state left for resume", batch_id)
    raise
finally:
    if not interrupted:
        ... set DONE / FAILED / CANCELLED
```

Writing `DONE` in the shutdown path would record a completion that never happened
**and** stop the next startup from resuming the items still waiting to launch. The
batch is deliberately left marked `RUNNING` on disk.

## Distinguish "finished with nothing" from "finished"

```python
succeeded = sum(1 for i in queue if i["status"] == "DONE")
batch["status"] = "DONE" if succeeded else "FAILED"
```

A batch where nothing launched is `FAILED`. On 2026-08-03 a broken `run79.sh`
template produced: HTTP 200, every item `LAUNCH_FAILED`, batch status "DONE", zero
registrations. The status was the only thing anyone looked at.

## Idempotence on replay

`_FINALISED_ITEM_STATUSES = ("DONE", "CANCELLED", "LAUNCH_FAILED")` items are skipped
on resume. `_run_batch` is safe to re-enter from index 0 because every item carries
its own terminal state.

## Unique ids are not decorative

`run_id` is `{server}_{YYYYMMDDTHHMMSSZ}_{hex6}` and `batch_id` is
`batch_{ts}_{hex4}`. The timestamps only have second resolution, and `run_id`
determines the remote log/pid/script paths:

> two runs launched on the same server within one second would otherwise share all
> three files — the second run's pidfile would clobber the first's, making
> `kill_run` target the wrong process and `tail_log` show the wrong log.

Concurrent batches on one server make that collision likely, not theoretical. Same
argument for `batch_id` colliding in `_active_batches`.

## What is *not* crash-safe

- **Unstake batches** — in-memory only. The children survive (own process group) but
  the batch view doesn't. See [../unstake/batches.md](../unstake/batches.md).
- **All in-process caches** — cleared on restart by design.
- **`_synced_uids_cache`** — cleared, so a finished run may re-pull its params once.
  Harmless.
