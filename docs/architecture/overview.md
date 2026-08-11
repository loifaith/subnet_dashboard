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
~11k lines of vanilla JS + Bootstrap 5 + Chart.js, no build step) that talks to
~35 JSON endpoints on the same origin.

- `GET /` and `GET /miners` both render `miners.html`.
- `GET /login`, `POST /login`, `GET /logout` are the only unauthenticated paths,
  plus the `/asset/` static mount.
- Everything else is `/api/...` JSON, gated by the auth middleware.

The page's subnet selector (`#subnetInput`, default **79**) decides which feature
blocks load: SN79 lights up fee regime / sim-id / miner agent info / UID metrics;
SN88 swaps in the Investing88 panel; any other netuid shows the bare metagraph.
See [`loadData()`](../../templates/miners.html) around line 4413.

## Where the work happens

| Layer | Files | Role |
|---|---|---|
| HTTP + orchestration | [app.py](../../app.py) | routes, caches, background workers, SSH fan-out |
| SN79 domain | [79Miners/monitor/](../../79Miners/monitor/) | chain, TAOS, RegBot, unstake, history |
| SN88 domain | [88Miners/](../../88Miners/) | Investing88 API client + daily cache |
| UI | [templates/miners.html](../../templates/miners.html) | everything the operator sees |
| Auth | [auth.py](../../auth.py), [manage_users.py](../../manage_users.py) | allowlist + signed cookie |

`app.py` puts `79Miners/monitor` and `88Miners/monitor` on `sys.path` at import time
([app.py:26-30](../../app.py#L26-L30)), which is why endpoints can
`from regbot import ...` with no package prefix. Domain modules are imported
**inside the endpoint function**, not at module top level — a broken or heavy module
degrades one endpoint instead of failing startup.

## Data flow for one dashboard refresh (SN79)

1. `GET /api/metagraph-data?netuid=79` — 2-minute in-process cache; on a miss it
   runs [load_metagraph.py](../../load_metagraph.py) as a child process (90s timeout).
2. `GET /api/miner-params` — lists owned UIDs by scanning `79Miners/params/`.
3. `GET /api/uid-metrics` — served from today's `miner_status/<date>.json` file.
4. `GET /api/fee-regime`, `/api/sim-id`, `/api/reg-cost`, `/api/alpha-price`.
5. Background bars poll `/api/regbot/active` (20s), `/api/regbot/batch/active` (15s),
   `/api/unstake/active` (20s). Whole page auto-refreshes every 5 minutes.

Nothing in that path opens a chain connection inside the web process.
