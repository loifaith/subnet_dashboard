# Multi-server registration batches

Register N hotkeys spread across several servers, **one at a time**, in strict
round-robin order.

`start_batch()` — [regbot.py:1001](../../../79Miners/monitor/regbot.py#L1001)

## The queue

`{Server1: 3, Server2: 2, Server3: 4}` becomes:

```
Server1, Server2, Server3,   Server1, Server2, Server3,   Server1, Server3,   Server3
```

One server per round, dropping a server once its count is exhausted
(`_build_batch_queue`). Each queue item is **one single-hotkey attempt** carrying
that server's own `reglimit` and `port_prefix`.

## Serial by design, not by accident

Only one item is ever actively attempting. A step can legitimately sit for minutes
or hours waiting for that one server's reglimit to clear, while the other servers
in the batch idle.

That trade-off — strict controlled turn-taking over maximum throughput — was an
explicit choice, recorded at
[regbot.py:895-912](../../../79Miners/monitor/regbot.py#L895-L912). If you want full
concurrency, trigger each server separately; don't "fix" the batch.

## Several batches *can* run at once

Including on the same server. That is deliberate, and it has a real consequence
spelled out in the `start_batch` docstring:

> the same coldkey can have two registrations in flight at once, so the wallet
> drains at twice the rate its per-batch reg limits would suggest — check the
> balance against the combined spend of everything running, not just this batch.

`batch_id` carries a random suffix (`batch_<ts>_<hex4>`) for the same reason
`run_id` does: two batches created in the same second would otherwise collide in
`_active_batches`, the second silently replacing the first and orphaning its queue.

## Crash-safe resume

The orchestrator is just an `asyncio.Task` in the dashboard process. A deploy, a
`pm2 restart` or a crash used to kill it mid-queue and **silently drop every
PENDING item** — already-launched remote runs kept going and registered fine, so
from the outside it looked like "the batch just stopped". That happened for real on
2026-08-03 (`batch_20260803T013020Z`: 4 items registered, restart at 03:13:24Z,
remaining items never launched).

The fix has four parts:

### 1. State is persisted on every transition

`_save_batch_state()` writes `79Miners/RegBot/active_batch.json` via
`tmp` + `os.replace()`. It never raises — losing persistence is bad, but not a
reason to take down a running batch.

### 2. `run_id` is assigned *before* the launch

```python
item["run_id"] = item.get("run_id") or f"{item['server']}_{_make_run_id()}"
_save_batch_state()
await trigger_run(..., run_id=item["run_id"])
```

So if the dashboard dies mid-launch, the resumed batch knows exactly **which** run
to look for instead of inferring from whatever happens to be live. That is what
makes concurrent runs on one server safe to resume.

### 3. Resume asks a precise question

`_remote_run_exists(server, run_id)` checks for that run's own `.log` or `.pid` on
the box.

- **YES** → re-attach, skip the launch.
- **NO** → launch under the same pre-assigned `run_id`.
- **Unreachable / timeout** → **raise**, mark the item `LAUNCH_FAILED`.

That last case is the important one: "no answer" must never be read as "never
launched", because relaunching a run that did start double-registers and
double-spends.

### 4. Shutdown leaves the batch marked RUNNING

`_run_batch` catches `asyncio.CancelledError` and deliberately does **not** write a
terminal status:

> writing a "DONE" here would record a completion that never happened and stop the
> next startup from resuming the items still waiting to launch.

`resume_interrupted_batches()` runs once at startup ([app.py:225](../../../app.py#L225))
and restarts the orchestrator for anything still `RUNNING`/`CANCELLING`. Items
already in `("DONE", "CANCELLED", "LAUNCH_FAILED")` are skipped — re-running them
would double-register.

## Cancel

`cancel_batch()` sets `status = "CANCELLING"` and persists **immediately** — so a
restart landing between the flag and the loop noticing it doesn't come back up and
resume a cancelled batch. The loop checks the flag between poll ticks (≤10s), kills
the in-flight run, marks remaining items `CANCELLED`, and stops.

## Terminal status

```python
succeeded = count(status == "DONE")
batch["status"] = "DONE" if succeeded else "FAILED"
```

A batch where **nothing** launched is `FAILED`, not `DONE`. Reporting DONE there
reads as "everything registered" while in fact nothing did — which is exactly how a
broken `run79.sh` template looked on 2026-08-03: HTTP 200, every item
`LAUNCH_FAILED`, batch "DONE", zero registrations.

## Endpoints

| Path | Purpose |
|---|---|
| `POST /api/regbot/batch/start` | `{servers: [{server, count, reglimit, port_prefix}], dispatch_agent, dispatch_agent_id}` |
| `GET /api/regbot/batch/active` | `{batches: [...], batch: <most recent>}` |
| `GET /api/regbot/batch/{id}` | full queue state |
| `POST /api/regbot/batch/{id}/cancel` | request cancellation |

Every server config is validated up front with the **same** `_validate_inputs()`
that `trigger_run` applies, so a bad batch is rejected before the first launch
rather than partway through the queue.

## UI

`openMultiRegisterModal` → per-server count/reglimit/port rows → estimate → confirm
dialog → `showMultiRegisterProgress` polling `pollMultiRegisterBatch` every 3s. The
`#batchActiveBar` at the top of the page polls `/api/regbot/batch/active` every 15s
so an in-progress batch is visible from anywhere on the page.
