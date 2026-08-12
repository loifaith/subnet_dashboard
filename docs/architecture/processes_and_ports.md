# Processes, ports and configuration

## PM2 apps

Defined in [ecosystem.config.json](../../ecosystem.config.json). All four use the
repo's own venv interpreter `/root/79/subnet_dashboard/.venv/bin/python`.

| App | Script | cwd | Port | Purpose |
|---|---|---|---|---|
| `dashboard` | `app.py` | repo root | **12800** | the live dashboard |
| `chaind` | `chain_daemon.py` | `79Miners/monitor` | *(unix socket)* | one held chain connection, shared |
| `backend_api` | `backend_api.py` | repo root | **9900** | legacy SN71 Postgres CRUD |
| `status_server` | `status_server.py` | `79Miners/monitor` | **9800** | SN79 miner status feed |

`chaind` listens on `/tmp/.sn79-chaind.sock` (0600), not a TCP port — it is
strictly local and never reachable from off-box. It is a **latency optimisation
with a full fallback**: stopping it makes chain reads slow again and nothing else.
See [../sn79/performance/chain_daemon.md](../sn79/performance/chain_daemon.md).

Logs land in [logs/](../../logs/) as `<app>-out.log` / `<app>-error.log`.
Note `logging.basicConfig(level=INFO)` writes to **stderr**, so ordinary INFO lines
appear in `*-error.log`, not `*-out.log`.

**`dashboard` is `instances: 1`, `exec_mode: fork`** — one process, one event loop.
That is not incidental: every in-process cache and all eight background workers
assume a single instance. Switching to cluster mode would give each worker its own
copy of every cache, multiply the chain and SSH fan-outs by the instance count, and
make `X-Cache` meaningless. See
[../sn79/performance/page_load_audit.md](../sn79/performance/page_load_audit.md).

There is **no reverse proxy** in front of 12800 — uvicorn is the edge. Compression
and cache validation (weak ETags) therefore happen in `app.py` or not at all.
Byte-cached responses over 1KB are served **pre-compressed** from `_maybe_gzip()`,
which sets `Content-Encoding` and `Vary` itself; `GZipMiddleware` (>1KB) still
covers everything else. See
[../sn79/performance/cache.md](../sn79/performance/cache.md#10-pre-compressed-bodies).

## Start / restart

```bash
./start_servers.sh          # dashboard + backend_api (pm2 startOrReload + pm2 save)
./start_status_server.sh    # status_server only
pm2 logs dashboard
pm2 restart dashboard
```

[run_app.sh](../../run_app.sh) is stale — it still `cd`s to the old LeadPoet path.
Don't use it.

**Restarting `dashboard` is safe for in-flight work**, by design:

- RegBot runs live on the *remote* box under `setsid` — unaffected.
- RegBot *batches* persist to `active_batch.json` and resume on startup.
- Unstake children use `start_new_session=True`, so the restart signal cannot
  reach a mid-extrinsic unstake.

See [../sn79/tricks/process_detachment.md](../sn79/tricks/process_detachment.md).

## Environment variables

`.env` exists but is only consumed by `backend_api.py` (`load_dotenv()`).
`app.py` reads plain `os.getenv` with in-code defaults, so **the defaults below are
what actually runs** unless PM2 injects otherwise.

### Timing / worker cadence ([app.py:37-102](../../app.py#L37-L102))

| Variable | Default | Effect |
|---|---|---|
| `MINER_STATUS_REFRESH_SECONDS` | 900 | daily-status refresh subprocess cadence |
| `FEE_REGIME_CACHE_SECONDS` | 300 | `/api/fee-regime` TTL |
| `REG_COST_CACHE_SECONDS` | 300 | `/api/reg-cost` TTL |
| `REGBOT_BALANCE_CACHE_SECONDS` | 300 | coldkey balance sweep cadence |
| `COLDKEYS_WATCH_INTERVAL_SECONDS` | 3600 | coldkey/IP collection cadence |
| `INVESTING88_CACHE_SECONDS` | 300 | SN88 endpoint TTL |
| `INVESTING88_SYNC_INTERVAL_SECONDS` | 3600 | SN88 sync worker cadence |
| `MINER_PARAMS_KEEPALIVE_SECONDS` | 300 | SSH master warm-up (must stay < `ControlPersist=600`) |
| `SIM_ID_POLL_SECONDS` | 60 | sim-epoch watcher |
| `UID_STATUS_CACHE_MAX` | 256 | LRU-ish bound on the per-UID status cache |
| `METAGRAPH_REFRESH_SECONDS` | 90 | metagraph warm-refresh worker — **must stay < `METAGRAPH_CACHE_TTL` (120)** |
| `METAGRAPH_WARM_NETUIDS` | `79,71,88` | which metagraphs that worker keeps warm |
| `REGBOT_ACTIVE_SCAN_SECONDS` | 20 | RegBot active-run SSH scan cadence |
| `MINER_PARAMS_CACHE_SECONDS` | 3600 | `/api/miner-params` byte-cache backstop (keyed on a directory fingerprint) |
| `UID_METRICS_CACHE_SECONDS` | 3600 | `/api/uid-metrics` byte-cache backstop (keyed on file mtimes) |
| `FEE_REGIME_REFRESH_SECONDS` | 240 | fee-regime warm refresh; **must stay under `FEE_REGIME_CACHE_SECONDS` (300)** |
| `REG_COST_REFRESH_SECONDS` | 240 | reg-cost warm refresh; **must stay under `REG_COST_CACHE_SECONDS` (300)** |
| `REG_COST_WARM_NETUIDS` | `79,88` | which subnets the reg-cost worker keeps warm |
| `FEE_HISTORY_CACHE_SECONDS` | 60 | `/api/fee-regime/history` byte cache |
| `FEE_HISTORY_MAX_POINTS` | 600 | per-series cap before downsampling |
| `SSE_HEARTBEAT_SECONDS` | 20 | `/api/events` keepalive comment frame |
| `METAGRAPH_SNAPSHOT_MAX_AGE` | 86400 | oldest on-disk metagraph snapshot still worth seeding at startup |
| `UID_METRICS_DROP_FIELDS` | see app.py | per-UID fields not sent to the client |

### Paths

| Variable | Default |
|---|---|
| `ALPHA_PRICE_DIR` | `/root/work/bittensor/88/88dashboard/data/alpha` |
| `AGENT_DEFAULTS_FILE` | `/root/79/Paul-SuperRegBot/myAgents/dispatch_example.json` |
| `MINER_PARAMS_REMOTE_DIR` | `/root/79/sn-79` |
| `MINER_PARAMS_RSYNC_USER` | `root` |
| `DASHBOARD_USERS_FILE` | `auth_users.json` |
| `DASHBOARD_SECRET_KEY_FILE` | `.dashboard_secret_key` |

### status_server ([status_server.py:50-58](../../79Miners/monitor/status_server.py#L50-L58))

`STATUS_SERVER_PORT` (9800), `STATUS_POLL_INTERVAL` (30),
`STATUS_RETENTION_SECONDS` (10800 via PM2 env), `STATUS_CSV_PATH`,
`STATUS_BOOKS_DIR`, `STATUS_BOOK_PRUNE_EVERY` (20).

## Hard-coded external dependencies

These are absolute paths on the dashboard host (`95.216.27.87`). They are *not*
in this repo, and their absence degrades specific features rather than the app:

| Path | Used by | Missing ⇒ |
|---|---|---|
| `/root/79/Paul-SuperRegBot/` (remote boxes) | RegBot | registration fails |
| `/root/79/sn-79/` (remote boxes) | params push, miner repo | params sync fails |
| `/root/.bittensor/wallets/<wallet>/coldkeypub.txt` | balance lookup | that server's balance is skipped |
| `/root/work/bittensor/bittensor-reg-script/unstake-transfer/` | unstake | unstake fails |
| `/root/work/bittensor/88/88dashboard/data/alpha/<netuid>.json` | header sparkline | `/api/alpha-price` 404s |
| `79Miners/Coldkeys/collect_coldkeys_ips.py` + `settings.json` | coldkey watch | watcher errors |

## Python dependencies

[requirements.txt](../../requirements.txt): `fastapi`, `uvicorn[standard]`,
`psycopg[binary]`, `psycopg_pool`, `jinja2`, `python-multipart`, `requests`,
`cloudscraper`, `python-dotenv`, `bittensor`.

`psycopg*` is only for the legacy backend. `cloudscraper` is how TAOS is queried
(it fronts Grafana behind Cloudflare).
