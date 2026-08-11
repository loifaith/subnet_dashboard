# Concurrency model

Everything is one asyncio event loop in one process. There is no thread pool of
workers, no task queue, no Celery. Concurrency comes from four primitives, each used
for a specific class of work.

## The four primitives

| Primitive | For | Example |
|---|---|---|
| `asyncio.gather` over subprocesses | SSH fan-out, parallel independent work | `get_active_runs`, bulk restart, keepalive |
| `asyncio.create_subprocess_exec` | anything CPU-bound or that loads `bittensor` | `chain_cli.py`, metagraph, status refresh |
| `asyncio.to_thread` | blocking **network** I/O in a C-releasing library | `cloudscraper` TAOS queries, file parsing |
| `asyncio.create_task` | work that outlives the request | RegBot batches, unstake runs, all six workers |

## Choosing between `to_thread` and a subprocess

This is the decision that matters most:

- **`to_thread`** is correct when the blocking call releases the GIL — sockets,
  file reads. `fetch_agent_display_metrics` (cloudscraper) and `_load_alpha_series`
  (JSON parse of a modest file) use it.
- **A subprocess** is required when the work is pure Python holding the GIL.
  `substrate`'s metadata decode is the canonical case, and a thread does *not* help.
  See [chain_subprocess.md](chain_subprocess.md).

Getting this backwards is the main way to reintroduce the 13.7s stalls.

## Fan-out patterns

### Independent work, collect all results

```python
outcomes = await asyncio.gather(*(_ssh_pm2_restart_many(by_ip[ip], ip) for ip in ips))
```

Used by bulk restart, keepalive, `get_active_runs`, and the unstake batch.

### Tolerate individual failures

```python
results = await asyncio.gather(*[apply_one(*t) for t in targets], return_exceptions=True)
for t, r in zip(targets, results):
    ... {"uid": t[0], "ok": False, "error": str(r)} if isinstance(r, Exception) else r
```

`/api/agent-bulk` does this — one unreachable box degrades to one failed row, not a
failed request.

### Never-raising probes

`_check_server_active()` returns `{"reachable": False, "run_ids": []}` on timeout or
non-zero exit instead of raising, so `gather` over 12 servers always returns 12
answers. The endpoint reports `unreachable_servers` separately.

### Two-phase gather

`get_active_runs` gathers server probes, builds a `pending` list, then gathers
`_tail_last_line` over exactly that list. Two round-trip generations rather than
`servers × runs` sequential calls.

## Background tasks

Long work returns a handle immediately and is polled:

| Subsystem | Handle | Poll endpoint | Interval |
|---|---|---|---|
| RegBot run | `run_id` | `/api/regbot/log` | 4s (UI) |
| RegBot batch | `batch_id` | `/api/regbot/batch/{id}` | 3s (UI), 10s (internal) |
| Unstake run | `run_id` | `/api/unstake/log` | 4s (UI) |
| Unstake batch | `batch_id` | `/api/unstake/batch/{id}` | 3s (UI), 5s (internal) |

Task registries are module-level dicts (`_batch_tasks`, `_run_tasks`,
`_active_batches`, `_active_runs`) popped in a `finally`. They are **in-process
state** — which is why RegBot batches also persist to disk
([../tricks/crash_safe_state.md](../tricks/crash_safe_state.md)) and unstake batches
don't survive a restart.

## Serial where serial is correct

Not everything should be parallel, and the codebase is explicit about which:

| Subsystem | Mode | Reason |
|---|---|---|
| RegBot batch | **serial** round-robin | every item competes for the same chain registration price |
| Unstake batch | **parallel** | each coldkey is independent; no shared resource |
| Unstake, same server | **blocked** | double-submit risk on one coldkey |
| Bulk params edit | parallel | independent files, independent boxes |

Don't "optimise" the RegBot batch into parallelism; the serialisation is the feature.

## Timeouts everywhere

Every subprocess and SSH call has an explicit `asyncio.wait_for`, and spawning has
its **own** shorter timeout separate from the work:

```python
proc = await asyncio.wait_for(create_subprocess_exec(...), timeout=10.0)   # spawn
stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600.0)  # work
```

On timeout the process is killed (`proc.kill()`, often under `suppress(ProcessLookupError)`)
and drained. An unkilled child would hold the pipe open and leak.

Representative values: ssh probe 10–12s, ssh mkdir/launch 20s, scp 20–30s,
params push 60s, bulk restart 120s, chain balances 300s, status refresh 600s,
unstake run 1800s.
