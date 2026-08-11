# Subnet 79 (TAOS)

The subnet this dashboard actually exists for. SN79 miners are simulated-trading
agents: a validator publishes per-miner and per-book *gauges* to a Grafana-backed
metrics API, and incentive follows those scores.

## Domain model

### A miner is a params file

```
79Miners/params/run_miner_<uid>.json
```

Ownership, history tracking, status polling, auto-switching and the "My Miners"
stat all derive from that directory listing. There is no registry anywhere else.

### A miner runs a *dispatcher* with a selected sub-agent

The params file holds an `agents` list and an `agent_index`. **`agent_index` is an
`agent_id` field, not a list position** — `_active_agent_pos()`
([app.py:815](../../app.py#L815)) looks up by `agent_id` first and only falls back to
positional indexing for old files. Several entries can share a display name with
different tuned params (two `GammaPrimeAgent` variants), which is exactly why
`agent_id` is authoritative.

The remote dispatcher re-reads this file every poll, so **changing params or
switching agents takes effect without restarting the miner**.

### Twelve servers, twelve coldkeys

`SERVER_PRESETS` ([regbot.py:67](../../79Miners/monitor/regbot.py#L67)) maps
`ServerN → {ip, wallet, port_prefix}`. Each server has its own axon port band so
miners on different boxes can never collide.

⚠️ **This list is duplicated in three places** and they must stay in sync:

| Copy | Purpose |
|---|---|
| `SERVER_PRESETS` in [regbot.py](../../79Miners/monitor/regbot.py) | source of truth for RegBot/unstake |
| `SERVER_NICKNAMES` in [app.py:494](../../app.py#L494) | IP → name for table display |
| `SERVER_NICKNAMES` in [miners.html](../../templates/miners.html) | client-side IP → name |

Preset names are validated at import: a name that can't appear in a `run_id`
(e.g. a trailing space) raises at startup rather than failing one queue item at a
time. That guard exists because `"Server3 "` once did exactly that.

## Data sources

| Source | Reached by | Gives |
|---|---|---|
| **TAOS metrics API** (`taos.simulate.trading`, Grafana proxy) | `cloudscraper` POST to `/query_range` | per-UID and per-book gauges, fee rates, live sim id |
| **Bittensor chain** (finney / archive) | `chain_cli.py` child process | reg cost, balances, stake, metagraph |
| **The miner boxes** | SSH (multiplexed) | params push, `pm2 restart`, RegBot launch + log tail |
| **Local disk** | direct | daily status, history, audit logs, caches |

TAOS queries always use a **10-minute window with `step=60` and one retry**
([uid_status_checker.py:79](../../79Miners/monitor/uid_status_checker.py#L79)) rather
than an instant query — an instant read between validator scrapes looks like
missing data.

The reporting wallet and API id are hard-coded at
[uid_status_checker.py:32-34](../../79Miners/monitor/uid_status_checker.py#L32-L34) and
mirrored in [sim_uid.py](../../79Miners/monitor/sim_uid.py).

## Key metrics vocabulary

| Gauge | Column | Meaning |
|---|---|---|
| `kappa_score` | `kappa` | the main scoring term |
| `total_realized_pnl` | `real_pnl` | closed PnL |
| `pnl` | `unrl_pnl` | mark-to-market of open inventory |
| `total_daily_volume` / `_maker_` / `_taker_` | `volume`, `vol_mkr`, `vol_tkr` | daily volume split |
| `daily_volume` (per book) | — | drives "hot book" detection |
| `fees_maker_rate` / `fees_taker_rate` | — | positive = you pay, negative = you're rebated |

`BOOK_VOLUME_LIMIT = 500_000`, `BOOK_VOLUME_WARN_RATIO = 0.98` — a book at ≥98% of
the daily volume cap is "hot". Too many hot books triggers the auto-switch
([automation/volume_agent_switcher.md](automation/volume_agent_switcher.md)).

## Map of this section

- [miners/](miners/) — everything on the dashboard page
- [regbot/](regbot/) — registering new hotkeys (spends TAO)
- [unstake/](unstake/) — unstaking and sweeping TAO (irreversible)
- [status_server/](status_server/) — the `:9800` feed the miners consume
- [automation/](automation/) — background workers, auto-switch, Telegram
- [performance/](performance/) — caching, out-of-process chain work, SSH reuse
- [tricks/](tricks/) — the non-obvious techniques, and what's currently broken
