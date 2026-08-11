# Subnet 88 (Investing88)

A secondary view. Setting the page's subnet selector to **88** swaps the SN79
panels (fee regime, sim id, agent params) for Investing88 score columns.

There is **no write path for SN88** — no registration, no unstake, no params
editing. It is read-only.

## Layout

```
88Miners/
├── const.py                     API bases, netuid, cache dir, sync hour/tz
├── INVESTING88_API.md           notes on the upstream API
└── monitor/investing88_cache.py the daily sync job

79Miners/monitor/investing88_miner.py   the API client (lives under 79Miners/)
```

The client sitting under `79Miners/monitor/` is a historical accident, not a
dependency direction — `investing88_cache.py` explicitly adds both directories to
`sys.path` to import it.

## Upstream

| Base | Used for |
|---|---|
| `https://api.investing88.ai` | the bulk `days` table |
| `https://db.investing88.ai/api` | per-UID detail: `close/{uid}`, `strat/{uid}`, `pnl/{uid}` |

Plain `requests` with a 120s timeout — no `cloudscraper` needed, unlike TAOS.
Responses are sometimes a JSON **string** containing JSON, so `_api_get` re-parses
when it gets a `str`.

The bulk rows are positional arrays, normalised by `_normalize_days_rows()` into
`{uid, hotkey, date, rank, asset, asset_label, days, last, cash}`. `asset` is an
int mapped through `ASSET_LABELS` (`0 = Tao / Alpha`, `1 = US Equities`).

## Feature docs

- [miners/investing88.md](miners/investing88.md) — the dashboard columns and modal
- [performance/cache.md](performance/cache.md) — the daily cache and why it exists
