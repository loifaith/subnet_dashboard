# Alpha price sparkline

The header price widget: subnet alpha price over a selectable window
(24H / 7D / 30D / 90D / ALL), with change %, high and low.

`GET /api/alpha-price?netuid=79&range=7d` — [app.py:2106](../../../app.py#L2106)

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

`_load_alpha_series()` ([app.py:2039](../../../app.py#L2039)) caches parsed rows
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

`_downsample_last()` ([app.py:2075](../../../app.py#L2075)) thins to
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
 "source_points", "downsampled",
 "latest_candle", "age_hours", "stale", "stale_after_hours", "updated_at"}
```

`updated_at` is the source file's mtime as UTC. The **point timestamps are passed
through verbatim** — the source file carries no timezone marker, so neither the
endpoint nor the UI restates them as UTC. Don't add a `Z` to them.

## Staleness: never key it off `updated_at`

The collector rewrites the file on a timer **whether or not the feed behind it
produced anything**. So mtime keeps moving while the series sits still, and
`updated_at` says when the file was touched, not how current the prices are.

This is not hypothetical. On 2026-08-11 the upstream `88taodata` collector stopped
after the `2026-08-10 23:00` candle; the zip kept being repackaged every ~15min and
`updated_at` stayed minutes-fresh while every one of the 129 subnets in it was
frozen at the same timestamp. The header showed a day-old close as the live price —
3.6% off the actual chain price by then.

So freshness is computed from `latest_candle` (the newest row in the **whole
series**, not the selected window) via [`_alpha_candle_age_hours()`](../../../app.py#L2092), and `stale` is
`age_hours > ALPHA_PRICE_STALE_AFTER_HOURS` (env, default 2 — the series is hourly,
so a multi-hour gap means stopped, not lagging).

Candle timestamps are read as **UTC** for this one calculation. The data supports
it: when the feed stalled, the gap grew hour-for-hour with wall clock (5.7h, then
24.5h) — a timezone offset would have held constant.

To diagnose a frozen chart, check the source rather than this repo:

```bash
python - <<'EOF'
import zipfile
z = zipfile.ZipFile('/root/work/bittensor/88/investing_1/Agent/subnet_3mo.zip')
for n in [x for x in z.namelist() if x.startswith('SN79_')]:
    print(z.read(n).decode().strip().splitlines()[-1])
EOF
```

If every subnet in the zip ends at the same timestamp, the collector upstream has
stopped and nothing here can fix it.

## In the UI

`loadAlphaPrice(range)` → `renderAlphaPrice()` + `renderAlphaPriceChart()`. The line
colour follows the direction of `change_pct`, resolved against the current theme
(`alphaPriceLineColor(direction, isDark)`). Range buttons are a delegated click
handler on `#alphaPriceRanges`.

`renderAlphaPriceAge()` marks a stale chip: `.is-stale` on the card dims the value
and sparkline, and `#alphaPriceAge` shows an amber "`⚠ 25h old`" pill. The chip is
otherwise unchanged, so a healthy feed shows no extra chrome.
