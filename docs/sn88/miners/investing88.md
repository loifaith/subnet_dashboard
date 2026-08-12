# Investing88 view

What the page shows when the subnet selector is set to 88.

## Endpoints

| Path | Source |
|---|---|
| `GET /api/investing88/days` | [app.py:2229](../../../app.py#L2229) — all UIDs: days table + scores + previous-day scores |
| `GET /api/investing88/miner/{uid}` | [app.py:2167](../../../app.py#L2167) — one miner's full detail |
| `GET /api/investing88/cache-status` | [app.py:2283](../../../app.py#L2283) |
| `POST /api/investing88/sync` | [app.py:2293](../../../app.py#L2293) — force a sync now |

## `/days` — the table columns

Returns three maps:

```json
{ "days":        { "<uid>": {uid, hotkey, date, rank, asset, asset_label, days, last, cash} },
  "scores":      { "<uid>": {asset, days, last, score, value, lsr, mar,
                             risk_percent, odds_percent, daily_percent, yield_percent} },
  "prev_scores": { "<uid>": { ...same shape... } } }
```

`prev_scores` comes from the **second-newest dated file** in `investing88_cache/`
— the day-over-day delta arrows in the table. If only one day of cache exists, it is
`{}` and the deltas simply don't render; a failure to load it is logged as a warning
and never fails the request.

`_extract_i88_scores()` ([app.py:2218](../../../app.py#L2218)) projects only the
eleven fields in `_I88_SCORE_FIELDS`, so the payload stays small regardless of what
the upstream returns.

Cached in-process for 300s under the sentinel key `-1` in the shared
`_investing88_cache` dict — the same dict that holds per-UID payloads under their
real uid. Slightly cute, but it means one clear-on-sync (`_investing88_cache.clear()`)
covers both.

## `/miner/{uid}` — the detail modal

`fetch_miner_details(uid, hotkey, rank, use_cache, force_live)`
([investing88_miner.py](../../../79Miners/monitor/investing88_miner.py)) assembles:

- **score** — the normalised score row
- **holdings** — `close/{uid}`
- **strategy_history** — `strat/{uid}`
- **pnl_history** — `pnl/{uid}`
- **dashboard_url** — a deep link to `db.investing88.ai`

Each sub-fetch is individually wrapped: a failure appends to an `errors` list rather
than failing the whole payload, so a partial detail view still renders.

Query params: `?hotkey=` and `?rank=` (hints passed through from the table row so the
client doesn't have to re-derive them), `?refresh=1` to bypass the 300s TTL,
`?live=1` to bypass the **daily file cache** and hit the API directly.

`uid` is validated `0..255`.

## Client side

`loadInvesting88Days(forceRefresh)` runs in place of the SN79 tail of `loadData()`.
It populates `investing88DaysMap`, `investing88ScoresMap` and
`investing88PrevScoresMap`; `getSn88Score(uid, field)` and `formatSn88Money()` read
from those when rendering table cells.

Switching away from subnet 88 clears all three maps, so stale SN88 numbers can never
leak into an SN79 render.
