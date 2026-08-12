# Concurrency model

Everything is one asyncio event loop in one process. There is no thread pool of
workers, no task queue, no Celery. Concurrency comes from four primitives, each used
for a specific class of work.

## The four primitives

| Primitive | For | Example |
|---|---|---|
| `asyncio.gather` over subprocesses | SSH fan-out, parallel independent work | `get_active_runs`, bulk restart, keepalive |
| `asyncio.create_subprocess_exec` | anything CPU-bound or that loads `bittensor` | `chain_cli.py`, metagraph, status refresh |
| `asyncio.to_thread` / `run_in_threadpool` | blocking **file** or **network** I/O | `cloudscraper` TAOS queries, every params/JSONL read on a request path |
| `asyncio.create_task` | work that outlives the request | RegBot batches, unstake runs, all eight workers |

## Choosing between `to_thread` and a subprocess

This is the decision that matters most:

- **`to_thread`** is correct when the blocking call releases the GIL — sockets,
  file reads. `fetch_agent_display_metrics` (cloudscraper) and `_load_alpha_series`
  (JSON parse of a modest file) use it.
- **A subprocess** is required when the work is pure Python holding the GIL.
  `substrate`'s metadata decode is the canonical case, and a thread does *not* help.
  See [chain_subprocess.md](chain_subprocess.md).

Getting this backwards is the main way to reintroduce the 13.7s stalls.

## The third case: pure-Python work that is neither

`json.dumps` of a 275KB payload is not I/O and not a chain call. It is ~44ms of
GIL-held CPU inside an `async def`, which means ~44ms of stall for *every* other
in-flight request — and a page load fires six of these at once.

Measured before it was fixed: `/api/sim-id`, `/api/unstake/active` and
`/api/coldkeys/summary` each cost ~1ms in isolation but reported ~270ms of server
time during a page load, purely from queueing behind three handlers serialising
large payloads.

The answer is not a thread. It is **not doing the work per request**: cache the
serialised bytes and return them, so a warm request neither reads the disk nor
serialises. See [cache.md](cache.md) §7 and
[page_load_audit.md](page_load_audit.md) §3.4.

Anything left on a request path that touches the filesystem goes through
`starlette.concurrency.run_in_threadpool` — the params directory scan,
`get_cached_metrics_payload`, `build_fee_history`, `build_uid_history`,
`build_agent_history_response`.

## Rule of thumb for a new endpoint

1. Does it spawn a process or open an SSH connection? → **background worker**, and
   the handler reads its result out of memory. No exceptions.
2. Does it block on a socket or the filesystem? → `run_in_threadpool`.
3. Does it serialise more than ~20KB, or get polled? → cache the **bytes** and give
   it a weak ETag.
4. Is it GIL-bound pure Python? → subprocess.

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
