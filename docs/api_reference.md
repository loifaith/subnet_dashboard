# API reference

Every HTTP endpoint, in one table. All of these live in [app.py](../app.py) on port
**12800** and require a session cookie (or a loopback client) — see
[architecture/auth.md](architecture/auth.md).

Read endpoints are `GET` and safe. **Write endpoints are marked ⚠ where they spend
or move TAO.**

## Pages

| Method | Path | Notes |
|---|---|---|
| GET | `/` | renders `miners.html` |
| GET | `/miners` | renders `miners.html` |
| GET | `/login` | public |
| POST | `/login` | public; form `username`, `password`, `next` |
| GET | `/logout` | public |
| — | `/asset/*` | public static mount |

## Metagraph and metrics

| Method | Path | Query | Doc |
|---|---|---|---|
| GET | `/api/metagraph-data` | `netuid`, `refresh` | [metagraph_view](sn79/miners/metagraph_view.md) |
| GET | `/api/uid-status/{uid}` | `books`, `refresh` | [uid_status](sn79/miners/uid_status.md) |
| GET | `/api/uid-metrics` | `uids`, `refresh` | [uid_status](sn79/miners/uid_status.md) |
| GET | `/api/miner-history` | — | [miner_history](sn79/miners/miner_history.md) |
| GET | `/api/miner-history/{uid}` | `days` (1–90), `day`, `limit` | ″ |
| GET | `/api/agent-history/{uid}` | `limit` (1–500) | ″ |
| GET | `/api/fee-regime` | `books`, `refresh` | [fee_regime](sn79/miners/fee_regime.md) |
| GET | `/api/fee-regime/history` | `days`, `day`, `uid`, `limit` | ″ |
| GET | `/api/reg-cost` | `netuid`, `refresh` | [reg_cost](sn79/miners/reg_cost.md) |
| GET | `/api/sim-id` | — | [sim_id](sn79/miners/sim_id.md) |
| GET | `/api/alpha-price` | `netuid`, `range` (24h/7d/30d/90d/1y/all) | [alpha_price](sn79/miners/alpha_price.md) |

## Miner params

| Method | Path | Body | Doc |
|---|---|---|---|
| GET | `/api/miner-params` | — | [miner_params](sn79/miners/miner_params.md) |
| GET | `/api/miner-params/{uid}` | — | ″ |
| PUT | `/api/miner-params/{uid}` | `{agent_index, params, sync_ip}` — **replaces** params | ″ |
| PUT | `/api/agent-bulk` | `{target_agent\|uids, agent_index, set, remove, sync}` — **merges** | [agent_bulk_edit](sn79/miners/agent_bulk_edit.md) |
| POST | `/api/miner-restart/{uid}` | `{ip?}` | [miner_params](sn79/miners/miner_params.md) |
| POST | `/api/miner-restart-bulk` | `{uids: [...]}` (max 500) | ″ |

## RegBot — [docs](sn79/regbot/)

| Method | Path | Body / query |
|---|---|---|
| GET | `/api/regbot/presets` | — |
| ⚠ POST | `/api/regbot/run` | `{server, dispatch_agent, reglimit, max_count, dispatch_agent_id?, port_prefix?}` |
| GET | `/api/regbot/log` | `server`, `run_id`, `lines` (1–2000) |
| POST | `/api/regbot/kill` | `{server, run_id}` |
| GET | `/api/regbot/balance` | `server`, `refresh` |
| POST | `/api/regbot/balance/refresh` | — |
| GET | `/api/regbot/history` | `limit` |
| GET | `/api/regbot/active` | — |
| ⚠ POST | `/api/regbot/batch/start` | `{servers: [{server, count, reglimit, port_prefix?}], dispatch_agent, dispatch_agent_id?}` |
| GET | `/api/regbot/batch/active` | — |
| GET | `/api/regbot/batch/{batch_id}` | — |
| POST | `/api/regbot/batch/{batch_id}/cancel` | — |

## Unstake — [docs](sn79/unstake/)

| Method | Path | Body / query |
|---|---|---|
| GET | `/api/unstake/presets` | — |
| POST | `/api/unstake/preview` | `{server, mode, destination?, amount?, netuid}` — dry run, safe |
| ⚠ POST | `/api/unstake/run` | same body — **irreversible** |
| GET | `/api/unstake/log` | `run_id` |
| GET | `/api/unstake/active` | — |
| ⚠ POST | `/api/unstake/batch/start` | `{items: [{server, mode, destination?, amount?}]}` |
| GET | `/api/unstake/batch/active` | — |
| GET | `/api/unstake/batch/{batch_id}` | — |
| GET | `/api/unstake/history` | `limit` |

Modes: `unstake`, `unstake_transfer`, `transfer`. `amount` applies to `transfer` only.

## Coldkeys — [docs](sn79/miners/coldkeys.md)

| Method | Path |
|---|---|
| GET | `/api/coldkeys/summary` |
| GET | `/api/coldkeys/list` |
| POST | `/api/coldkeys/refresh` |

## Investing88 (SN88) — [docs](sn88/miners/investing88.md)

| Method | Path | Query |
|---|---|---|
| GET | `/api/investing88/miner/{uid}` | `hotkey`, `rank`, `refresh`, `live` |
| GET | `/api/investing88/days` | `refresh` |
| GET | `/api/investing88/cache-status` | — |
| POST | `/api/investing88/sync` | — |

## Status server (separate process, port 9800)

No authentication; CORS open. Consumed by the miner boxes.
[docs](sn79/status_server/overview.md)

| Method | Path | Query |
|---|---|---|
| GET | `/health` | — |
| GET | `/uids` | — |
| GET | `/status` | — |
| GET | `/status/{uid}` | — |
| GET | `/books/{uid}` | — |
| GET | `/books/{uid}/history` | `minutes` (1–180) |
| GET | `/history` | `uid`, `minutes` (1–180) |

## Error conventions

| Status | Means |
|---|---|
| 400 | validation — bad param, unknown server, bad mode/destination, unknown `agent_id` |
| 401 | no session (API paths only; page paths get a 303 to `/login`) |
| 404 | no such UID / no params file / no history / no alpha data |
| 500 | a domain module failed to import |
| 502 | an upstream failed — chain, TAOS, SSH, Investing88 |
| 503 | status server has no data yet (first poll pending) |
| 504 | metagraph load exceeded 90 s |

`RegBotError` and `UnstakeError` are the user-facing validation types and always map
to **400**; anything else from those modules maps to **502**.
