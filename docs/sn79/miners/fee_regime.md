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
| `GET /api/fee-regime?books=1&refresh=1` | [app.py:2600](../../../app.py#L2600) |
| `GET /api/fee-regime/history?days=7&day=&uid=&limit=&book_points=` | [app.py:2681](../../../app.py#L2681) |

Backed by [fee_rate.py](../../../79Miners/monitor/fee_rate.py).

## Caching and the stale fallback

`FEE_REGIME_CACHE_SECONDS = 300`. Two details worth knowing:

1. **Both shapes are cached — the summary and the books.** This used to cache only
   the summary and `pop()` the books, so `?books=1` was the one query shape that
   bypassed every cache branch and always went upstream: **measured 2.3-3.4s on
   every click of the Books toggle.**

   The waste was total. `fetch_fee_rates()` is the entire ~2.8s cost and already
   returns per-book maker/taker rates; `build_fee_regime_summary()` derives `books`
   from them with local arithmetic and no further upstream call. So the worker paid
   for the books every 240s, deleted them, and the next click spent another 2.8s
   regenerating 8.4KB that had been in memory moments earlier. **2.8s → 73ms**
   click-to-rendered, measured in a real browser.

   If the upstream is down, `_cached_fallback()` carries the books through when the
   caller asked for them — dropping that key turns a degraded answer into a broken
   panel.
2. **`_cached_fallback()`** ([app.py:2645](../../../app.py#L2645)): if the upstream
   query fails or returns an error, the endpoint serves the last good summary with
   `stale: true` added rather than failing. A transient TAOS miss shows a slightly
   old regime instead of an empty panel. Only when there is *no* cached payload at
   all does it 502.

## `book_points` — the parameter that makes history affordable

`book_points` takes `all` (default), `latest` or `none`.

The books chart reads exactly one thing out of `book_points`:

```js
var latestBookPoint = bookPoints[bookPoints.length - 1];
booksPayload = latestBookPoint.books;
```

**Only the newest snapshot is ever plotted.** With `all`, one day of history is 128
books x ~93 snapshots — 754KB, of which ~99% was downloaded, parsed and discarded.
The UI now passes `book_points=latest`: **754KB → 30KB** raw, 171KB → 6KB gzipped.

The default stays `all` so any script or ad-hoc query keeps the original shape.

Two more things happen to this payload before it goes out:

- **A byte cache** (`FEE_HISTORY_CACHE_SECONDS = 60`) keyed on
  `(days, day, uid, limit, book_points)`. Building it costs ~135ms of event-loop
  time; the underlying JSONL only grows once per fee-history append.
- **Downsampling** to `FEE_HISTORY_MAX_POINTS` (600) per series, evenly spaced and
  **always keeping the newest point** — a plain stride drops it whenever
  `len % stride != 1`, and the last snapshot is what the "current" readouts use. At
  `days=1` (~93 points) this does not engage; it matters from `days=30` up.
  `point_count` / `book_point_count` describe what was shipped; `*_total` carries
  the pre-downsample figure.

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
