# API reference

Every HTTP endpoint, in one table. All of these live in [app.py](../app.py) on port
**12800** and require a session cookie (or a loopback client) — see
[architecture/auth.md](architecture/auth.md).

Read endpoints are `GET` and safe. **Write endpoints are marked ⚠ where they spend
or move TAO.**

Responses are gzipped above 1KB, and the cached read paths carry a weak `ETag` with
`Cache-Control: no-cache` — send `If-None-Match` and an unchanged payload answers
`304` with no body. Endpoints backed by a background worker also set `X-Cache:
hit|stale|miss|stale-error|hit-coalesced`. `hit-coalesced` answers a `?refresh=1`
that arrived within one block (12s) of the last real read — the chain cannot have
changed, so the 2-3s upstream call is skipped. See
[sn79/performance/page_load_audit.md](sn79/performance/page_load_audit.md).

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
| GET | `/api/subnet-emission` | `netuid` | total non-validator emission + alpha price, off the warm metagraph |
| GET | `/api/metagraph-identity` | `netuid` | hotkey/uid/coldkey maps + `order`; changes only on registration churn |
| GET | `/api/metagraph-state` | `netuid` | per-block values, columnar against `order`, with the `identity_etag` they match |
| GET | `/api/uid-status/{uid}` | `books`, `refresh` | [uid_status](sn79/miners/uid_status.md) |
| GET | `/api/uid-metrics` | `uids`, `refresh`, `shape` | [uid_status](sn79/miners/uid_status.md) |
| GET | `/api/miner-history` | — | [miner_history](sn79/miners/miner_history.md) |
| GET | `/api/miner-history/{uid}` | `days` (1–90), `day`, `limit` | ″ |
| GET | `/api/agent-history/{uid}` | `limit` (1–500) | ″ |
| GET | `/api/fee-regime` | `books`, `refresh` | [fee_regime](sn79/miners/fee_regime.md) |
| GET | `/api/fee-regime/history` | `days`, `day`, `uid`, `limit`, `book_points` | ″ |
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
| GET | `/api/regbot/active` | `refresh` — omit it; see below |
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

## Two parameters worth knowing

**`/api/fee-regime/history?book_points=`** — `all` (default, one per-book snapshot
per point), `latest` (only the newest, which is all the books chart plots), `none`.
`latest` is the difference between a 754KB and a 30KB response; the UI uses it.

**`/api/uid-metrics?shape=`** — `slim` (default) returns columns plus ready-made
deltas; `full` returns the original per-UID objects and `prev_metrics`, for readers
outside the dashboard. Slim is 23 KB gzipped against 58 KB.

**`/api/fee-regime?books=1`** — the per-book maker/taker rates behind the Books
toggle. Served from cache: the same worker tick that produces the summary now keeps
the books rather than discarding them (**2.8s → ~1ms**).

**`/api/reg-cost?netuid=<n>`** — served from `chaind`'s block-paced poller, so it is
never more than one block (~12s) old and costs ~1ms. `?refresh=1` is the same read.

**`POST /api/regbot/balance/refresh`** — reads the worker-maintained
`balance_cache.json` and returns immediately (`cache: hit`), kicking a sweep behind
the response only if the file is older than `REGBOT_BALANCE_CACHE_SECONDS`
(`cache: stale`). Add `?wait=1` to block on a live 12-coldkey sweep (~3s) when you
need the new numbers before continuing.

**`/api/regbot/active?refresh=1`** — forces the SSH fan-out across all 12 presets
instead of reading `_regbot_active_scan_worker`'s last result. ~690ms vs ~1ms. Use it
only right after an action that changed what is running. **Never from a poll.**

## Pages and assets

| Method | Path | Notes |
|---|---|---|
| GET | `/js/miners.js` | the page's client code; ETag'd, revalidated, not under `/asset` |
| GET | `/api/events` | Server-Sent Events change notifications (`event: change`, `{topic, etag}`) |

## Error conventions

| Status | Means |
|---|---|
| 400 | validation — bad param, unknown server, bad mode/destination, unknown `agent_id` |
| 401 | no session (API paths only; page paths get a 303 to `/login`) |
| 404 | no such UID / no params file / no history / no alpha data |
| 500 | a domain module failed to import |
| 502 | an upstream failed — chain, TAOS, SSH, Investing88 |
| 304 | you sent `If-None-Match` and nothing changed |
| 503 | status server has no data yet (first poll pending) |
| 504 | metagraph load exceeded 90 s |

`RegBotError` and `UnstakeError` are the user-facing validation types and always map
to **400**; anything else from those modules maps to **502**.
