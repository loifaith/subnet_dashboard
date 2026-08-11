# Alpha price sparkline

The header price widget: subnet alpha price over a selectable window
(24H / 7D / 30D / 90D / ALL), with change %, high and low.

`GET /api/alpha-price?netuid=79&range=7d` — [app.py:1696](../../../app.py#L1696)

## Source

**Not** produced by this repo. It reads

```
$ALPHA_PRICE_DIR/<netuid>.json      # default /root/work/bittensor/88/88dashboard/data/alpha
{"netuid": n, "name": "...", "series": [[date, o, h, l, c, v], ...]}
```

written hourly by a sibling project's `collect_prices.py`. A missing file is a
clean 404, not a crash.

## Three optimisations, each for a stated reason

### 1. mtime-keyed parse cache

`_load_alpha_series()` ([app.py:1643](../../../app.py#L1643)) caches parsed rows
keyed on the file's **mtime**. The source is rewritten hourly, so mtime is an exact
change signal — and re-parsing ~9k rows of JSON on every poll of an always-visible
header widget is not worth it. Open and volume are dropped at parse time; nothing
plots them.

### 2. Timestamp-based windowing, not row counts

```python
cutoff = end - timedelta(hours=hours)
start_i = next(i for i, r in enumerate(rows) if r[0] >= cutoff)
```

The series is hourly but **not gap-free**, so "the last N rows" would quietly
overstate the window. Lexicographic string comparison is exact here because the
timestamps are zero-padded `%Y-%m-%d %H:%M`.

### 3. Downsample by last-of-bucket

`_downsample_last()` ([app.py:1679](../../../app.py#L1679)) thins to
`ALPHA_PRICE_MAX_POINTS` (240) by keeping the **last** row of each bucket, and
forces the final point to be the true latest.

Averaging was rejected deliberately: these are closes, and an average invents a
price that never traded. The cost is that intra-bucket extremes are lost — which is
why `high` and `low` in the response are computed from the **full window**, not from
the returned points.

Without this, `range=all` would ship ~9k points on every poll to a ~130px-wide
sparkline.

## Response

```json
{"netuid", "name", "range", "points": [[ts, close], ...],
 "current", "first", "change", "change_pct",
 "high", "low", "period_start", "period_end",
 "source_points", "downsampled", "updated_at"}
```

`updated_at` is the source file's mtime as UTC. The **point timestamps are passed
through verbatim** — the source file carries no timezone marker, so neither the
endpoint nor the UI restates them as UTC. Don't add a `Z` to them.

## In the UI

`loadAlphaPrice(range)` → `renderAlphaPrice()` + `renderAlphaPriceChart()`. The line
colour follows the direction of `change_pct`, resolved against the current theme
(`alphaPriceLineColor(direction, isDark)`). Range buttons are a delegated click
handler on `#alphaPriceRanges`.
