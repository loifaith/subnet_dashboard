# Fee regime panel

SN79 runs a Dynamic Incentive Structure: per-book maker and taker fee rates float
with order flow. The panel answers "is it currently cheaper to make or to take, and
is either side being paid to trade?"

## Sign convention

**Positive = that side PAYS the fee. Negative = that side is REBATED.**
Get this backwards and every conclusion inverts.

## Endpoints

| Path | Source |
|---|---|
| `GET /api/fee-regime?books=1&refresh=1` | [app.py:1493](../../../app.py#L1493) |
| `GET /api/fee-regime/history?days=7&day=&uid=&limit=` | [app.py:1552](../../../app.py#L1552) |

Backed by [fee_rate.py](../../../79Miners/monitor/fee_rate.py).

## Caching and the stale fallback

`FEE_REGIME_CACHE_SECONDS = 300`. Two details worth knowing:

1. **The `books` payload is never cached** — only the summary is. A `?books=1`
   request always goes upstream, and the resulting summary still populates the
   cache so a later summary-only request has something to serve.
2. **`_cached_fallback()`** ([app.py:1511](../../../app.py#L1511)): if the upstream
   query fails or returns an error, the endpoint serves the last good summary with
   `stale: true` added rather than failing. A transient TAOS miss shows a slightly
   old regime instead of an empty panel. Only when there is *no* cached payload at
   all does it 502.

## Fetching the rates

Fees are per-book and **market-wide** — the same for every participant on a book —
so any handful of active UIDs reveals the whole picture. Two strategies:

- `fetch_book_rates_sampled()` — one query filtered to
  `DEFAULT_SAMPLE_UIDS = [118, 77, 161, 228, 16]`. Cheap.
- `fetch_book_rates_all()` — all UIDs reporting the gauge, deduped by `book_id`.
  Used when no explicit UID list is passed.

Both retry (3× for sampled, with a 2s sleep) and treat an empty result as a miss
rather than as "no fees".

## History

`append_fee_regime_history()` writes one line per daily-status refresh:

```
fee_history/<YYYY-MM-DD>.jsonl
```

with the regime summary plus `uid_fees` — per-UID **volume-weighted** maker/taker
fees from `fetch_uid_weighted_fees()`, so you can see what a specific miner
actually paid rather than the market median.

`build_fee_history(days, day, uid, limit)` reassembles that into chart series;
`list_fee_history_days()` (max 30) drives the day picker.

## In the UI

- `renderFeeRegimeSummary()` — the regime verdict and hint
  (`regime_label()` / `regime_hint()` in fee_rate.py).
- `renderFeeRegimeHistoryCharts()` — median maker/taker over time, plus per-book
  series when expanded.
- `renderFeeRegimeDetails()` — the worst/best books table, sorted by taker rate.

The panel is hidden entirely when the subnet selector is not 79
(`hideFeeRegimePanel()`).
