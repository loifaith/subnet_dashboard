# Investing88 API Reference

Base URLs (also defined in `const.py`):

- `http://api.investing88.ai`
- `https://api.investing88.ai`

Dashboard (separate from the API above):

- `https://db.investing88.ai` — miner dashboard UI
- `https://db.investing88.ai/api` — per-miner score / strategy / PnL / holdings

## Endpoints

| Method | Endpoint | Returns | Used by |
|--------|----------|---------|---------|
| GET | `/ratio` | Asset-class emission split, e.g. `[0.25, 0.25]` (Tao/Alpha, stocks; rest → owner UID) | `api.ratio()` |
| GET | `/pnl` | Full daily PnL series, all miners — the scoring input. Cols: `uid`, `hotkey`, `date`, `asset`, `block_*`, `value_*`, `swap_*` (open/high/low/close) | `api.pnl()` |
| GET | `/days` | Per-miner activity. Cols: `uid`, `hotkey`, `date`, `rank`, `a`, `days`, `last`, `cash` → feeds DEC (inactivity) & cash penalties | `api.days()` |
| GET | `/dist` | Dedupe data: allocation-distance + block-age matrices per asset, plus black/white-list & threshold | `api.dist()` |
| GET | `/assets` | HTML page: supported tickers, asset ratio, dereg list, cash ETFs list | README, `download_history.py` |
| GET | `/daily/<date>` | Tao/Alpha market data from `<date>` onward (OCHL by block; large ~32 MB) | `simst.fetchda()` |
| GET | `/daily1/<date>` | US-stock market data (`a=1`; very large, slow — timed out at 40 s in probe) | `simst.fetchda()` |
| GET | `/split` | Stock splits: `date`, `netuid`, `from`, `to` | `simst.fetchdb()` |
| GET | `/dividend` | Dividends: `ex_date`, `pay_date`, `netuid`, `amount`, `currency`, `type` | `simst.fetchdb()` |
| POST | `/rev` | Submit/revise a strategy. Body `{"ss58": <hotkey>, "strat": <file contents>}` | `api.rev()` |

## Local cache

The dashboard syncs miner data daily at **19:00 Asia/Tokyo** into `investing88_cache/`.

| File | Description |
|------|-------------|
| `investing88_cache/YYYY-MM-DD.json` | Full daily snapshot (all miners) |
| `investing88_cache/latest.json` | Copy of the most recent sync |

Manual sync: `python 88Miners/monitor/investing88_cache.py`

- `/pnl` is the main scoring input across all miners.
- `/days` drives inactivity (DEC) and cash penalties.
- `/dist` supports deduplication scoring (allocation distance, block age, lists, threshold).
- `/daily/<date>` and `/daily1/<date>` are large historical market-data dumps.
- `/rev` is write-only: submit or revise a miner strategy by hotkey.
