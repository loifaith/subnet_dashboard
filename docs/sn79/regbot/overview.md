# RegBot — registration

> **This spends real TAO.** Up to `reglimit` per hotkey × `max_count` hotkeys, and
> on success it launches a live pm2 miner. Every trigger is written to an
> append-only audit log before anything else happens.

[79Miners/monitor/regbot.py](../../../79Miners/monitor/regbot.py) (1266 lines).

## What one run does

```
dashboard                          miner server (Paul-SuperRegBot)
    │
    │ 1. read 79Miners/RegBot/run79.sh  (template)
    │ 2. regex-substitute agent/reglimit/max_count/wallet/port_prefix
    │ 3. ssh mkdir -p dashboard_runs/
    │ 4. scp   → dashboard_runs/run79_<run_id>.sh
    │ 5. ssh   → setsid detached launch, stdout → run79_<run_id>.log
    │                                    pid    → run79_<run_id>.pid
    │ 6. append regbot_runs.jsonl
    ▼ returns immediately
                                   creg79.py polls the chain price, registers
                                   when it clears reglimit, launches the miner
                                   via pm2, writes run_miner_<uid>.json
    │
    │ 7. poll tail_log() every 4s (UI) / 10s (batch)
    │ 8. on FINISHED: scp run_miner_<uid>.json back
```

The HTTP request returns after step 6. Registration itself can legitimately run for
**hours** waiting for the price to clear.

## Endpoints

| Path | Purpose |
|---|---|
| `GET /api/regbot/presets` | servers (+cached balances) and dispatch-agent options |
| `POST /api/regbot/run` | trigger one run |
| `GET /api/regbot/log?server=&run_id=&lines=` | tail + status + params sync |
| `POST /api/regbot/kill` | terminate a run's process group |
| `GET /api/regbot/balance?server=&refresh=` | one coldkey's balance |
| `POST /api/regbot/balance/refresh` | force a full sweep |
| `GET /api/regbot/history?limit=` | recent audit entries |
| `GET /api/regbot/active` | live runs across all servers |
| `POST /api/regbot/batch/*` | see [batches.md](batches.md) |

Request bodies are Pydantic models with `extra="forbid"`
([app.py:3140](../../../app.py#L3140)) — an unexpected field is a 400, not
a silently ignored typo.

## Validation

`_validate_inputs()` ([regbot.py:325](../../../79Miners/monitor/regbot.py#L325)):

| Field | Rule | Why |
|---|---|---|
| `server` | must be a known preset | the wallet comes from the preset, never from user input |
| `dispatch_agent` | `^[A-Za-z0-9_]+$` | interpolated into a shell script running as root |
| `dispatch_agent_id` | 0–999 | ditto |
| `reglimit` | 0.0001–5.0 | the spend cap |
| `max_count` | 1–10 | how many hotkeys |
| `port_prefix` | `^[0-9X]{1,4}(,[0-9X]{1,4})*$` | ditto — digits, `X`, commas only |

**The wallet is never user-supplied.** It is looked up from the server preset. That
is the main thing standing between this endpoint and an arbitrary-wallet drain.

## `dispatch_agent_id` vs `dispatch_agent`

`dispatch_example.json` contains multiple entries sharing a display name with
different tuned params (two `GammaPrimeAgent` variants). `creg79.py --dispatch-agent`
resolves **by name and matches the first**, making later duplicates unreachable.

So `get_dispatch_agent_options()`
([regbot.py:277](../../../79Miners/monitor/regbot.py#L277)) returns one entry per
`agent_id`, with a disambiguated `label` ("GammaPrimeAgent 1", "GammaPrimeAgent 2"),
and the generated script prefers `--dispatch-index <agent_id>` when an id is given.
See [run79_template.md](run79_template.md).

## Balances

`refresh_balance_cache()` ([regbot.py:235](../../../79Miners/monitor/regbot.py#L235))
sweeps every preset's coldkey every 300s into
`79Miners/RegBot/balance_cache.json`, so `/api/regbot/balance` is a file read.

Coldkey addresses are read from local `coldkeypub.txt` files under
`/root/.bittensor/wallets/` — no chain call needed to resolve an address, and
`creg79.py` keeps those files current.

One address failing (missing `coldkeypub.txt`) is logged and skipped; it does not
block the other eleven. The 12 presets share fewer distinct coldkeys than there are
presets, so addresses are de-duplicated before querying.

This function has an important performance history — see
[../performance/chain_subprocess.md](../performance/chain_subprocess.md).

## Audit log

```
79Miners/RegBot/regbot_runs.jsonl
{run_id, triggered_at, triggered_by, server, ip, wallet,
 dispatch_agent, dispatch_agent_id, reglimit, max_count, port_prefix,
 remote_script, remote_log, remote_pid}
```

Written **after a confirmed launch** (`LAUNCHED` echoed back), so it records runs
that actually started. `triggered_by` is `"dashboard"` or `"batch:<batch_id>"`.

## Kill

`kill_run()` sends `kill -TERM -<pid>` — **negative pid, i.e. the whole process
group**. `setsid` made the tracked pid a group leader, so this reaches both the
`run79.sh` shell and its `creg79.py` child. A plain `kill <pid>` would only hit the
shell and orphan `creg79.py`, which would keep spending.

Escalation: TERM → 2s → KILL → 1s → report `KILL_FAILED`. That last case surfaces as
a 502 with an explicit "check the server manually" message.

**Kill only stops the registration loop.** Any miner already launched via pm2 is an
independent process and keeps running, and no hotkey already registered can be
un-registered or refunded.
