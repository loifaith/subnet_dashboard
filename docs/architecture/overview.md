# Architecture overview

## What runs

Three PM2 processes, defined in [ecosystem.config.json](../../ecosystem.config.json):

```
                        browser
                           │  (session cookie)
                           ▼
        ┌──────────────────────────────────────────┐
        │  dashboard   app.py            :12800    │
        │  FastAPI + Jinja → templates/miners.html │
        └───┬───────────────┬──────────────┬───────┘
            │               │              │
   child processes     SSH (multiplexed)   HTTP
            │               │              │
   ┌────────▼──────┐  ┌─────▼────────┐  ┌──▼─────────────────┐
   │ chain_cli.py  │  │ 12 miner     │  │ TAOS metrics API   │
   │ load_metagraph│  │ servers      │  │ db.investing88.ai  │
   │ status refresh│  │ (params,     │  │                    │
   │ coldkey collect│ │  pm2, RegBot)│  └────────────────────┘
   └───────────────┘  └──────────────┘

        ┌──────────────────────────────────────────┐
        │  status_server  79Miners/monitor  :9800  │
        │  polls TAOS every 30s for owned UIDs     │
        │  the miner boxes poll THIS, not TAOS     │
        └──────────────────────────────────────────┘

        ┌──────────────────────────────────────────┐
        │  backend_api  backend_api.py     :9900   │
        │  legacy SN71 Postgres CRUD — dormant     │
        └──────────────────────────────────────────┘
```

`app.py` never calls `backend_api.py`. Only [db_utils.py](../../db_utils.py) points at
`:9900`, and nothing live imports it. See [legacy_sn71_layer.md](legacy_sn71_layer.md).

## Request shape

The dashboard is a **single HTML page** ([templates/miners.html](../../templates/miners.html),
~3.7k lines of markup and CSS) plus its client code
([asset/miners.js](../../asset/miners.js), ~7.3k lines of vanilla JS + Bootstrap 5 +
Chart.js, no build step), talking to ~36 JSON endpoints on the same origin.

- `GET /` and `GET /miners` both serve `miners.html`. It contains **no Jinja tags**,
  so it is read once per mtime and served as bytes with an `ETag` — not rendered.
- `GET /js/miners.js` serves the client code, ETag'd the same way. Deliberately not
  under the `/asset` mount, which sends no `Cache-Control`.
- `GET /login`, `POST /login`, `GET /logout` are the only unauthenticated paths,
  plus the `/asset/` static mount.
- Everything else is `/api/...` JSON, gated by the auth middleware.
- Responses over 1KB are gzipped by `GZipMiddleware`; there is no reverse proxy, so
  uvicorn is the edge.

The page's subnet selector (`#subnetInput`, default **79**) decides which feature
blocks load: SN79 lights up fee regime / sim-id / miner agent info / UID metrics;
SN88 swaps in the Investing88 panel; any other netuid shows the bare metagraph.
See [`loadData()`](../../asset/miners.js).

## Where the work happens

| Layer | Files | Role |
|---|---|---|
| HTTP + orchestration | [app.py](../../app.py) | routes, caches, background workers, SSH fan-out |
| SN79 domain | [79Miners/monitor/](../../79Miners/monitor/) | chain, TAOS, RegBot, unstake, history |
| SN88 domain | [88Miners/](../../88Miners/) | Investing88 API client + daily cache |
| UI | [templates/miners.html](../../templates/miners.html) + [asset/miners.js](../../asset/miners.js) | everything the operator sees |
| Auth | [auth.py](../../auth.py), [manage_users.py](../../manage_users.py) | allowlist + signed cookie |

`app.py` puts `79Miners/monitor` and `88Miners/monitor` on `sys.path` at import time
([app.py:30-34](../../app.py#L30-L34)), which is why endpoints can
`from regbot import ...` with no package prefix. Domain modules are imported
**inside the endpoint function**, not at module top level — a broken or heavy module
degrades one endpoint instead of failing startup.

## Data flow for one dashboard refresh (SN79)

1. `GET /api/metagraph-data?netuid=79` — served from a cache a background worker
   keeps warm at 90s. A request never runs [load_metagraph.py](../../load_metagraph.py);
   an expired entry is returned immediately while the refresh happens behind it.
2. Then, **all at once** — nothing below step 1 depends on anything else below it:
   `/api/miner-params` (owned UIDs), `/api/uid-metrics` (today's
   `miner_status/<date>.json`), `/api/fee-regime`, `/api/fee-regime/history`,
   `/api/sim-id`, `/api/reg-cost`, `/api/alpha-price`.
3. Background bars poll `/api/regbot/active` (20s), `/api/regbot/batch/active` (15s),
   `/api/unstake/active` (20s), and the page auto-refreshes every 5 minutes — all
   paused while the tab is hidden.

Nothing in that path opens a chain connection or an SSH connection inside the web
process. Both used to, and
[../sn79/performance/page_load_audit.md](../sn79/performance/page_load_audit.md) is
what they cost.
