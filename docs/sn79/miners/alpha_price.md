# Alpha price sparkline

The header price widget: subnet alpha price over a selectable window
(24H / 7D / 30D / 90D / ALL), with change %, high and low.

`GET /api/alpha-price?netuid=79&range=7d` — [app.py:2891](../../../app.py#L2891)

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

`_load_alpha_series()` ([app.py:2824](../../../app.py#L2824)) caches parsed rows
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

`_downsample_last()` ([app.py:2860](../../../app.py#L2860)) thins to
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

## The history is a DAILY feed, and the headline price is not from it

The candles are hourly but they do not *arrive* hourly. Upstream ships a whole day
at once, so the newest candle is normally the **previous day's 23:00** and the
series runs **13–37h behind** — permanently, by design, not as a fault. Observed:

| sampled (UTC) | newest candle | age |
|---|---|---|
| 08-11 04:39 | 08-10 23:00 | 5.7h |
| 08-11 23:29 | 08-10 23:00 | 24.5h |
| 08-12 12:15 | 08-11 23:00 | 13.7h — exactly 24 new candles |

Confirmation: the `08-11 23:00` close is `0.007792887`; the live chain price
measured on 08-11 was `0.007792486`. The series is one day behind the chain.

> **Caution, learned the hard way.** Sampling this twice inside one daily window
> looks exactly like a dead feed, and "all 129 subnets in the zip end at the same
> timestamp" reads as proof of a stall when it is simply what a daily batch looks
> like mid-cycle. It was misdiagnosed that way once. Judge this across a **day**
> boundary, never across hours.

Because of that lag the header does **not** take its headline price from this
endpoint. `renderAlphaPrice()` shows `subnetAlphaPrices[netuid]` — the live chain
price already fetched by `processMetagraph()`, which runs earlier in the same
`loadData()` cycle — and uses the series only for the sparkline shape, the
high/low, and the window start of the change %. Using `current` here left the
header a permanent ~1 day behind (3.2% out when measured).

### `stale` means the history is overdue, not that the price is wrong

Freshness comes from `latest_candle` (newest row of the **whole series**, not the
selected window) via [`_alpha_candle_age_hours()`](../../../app.py#L2587); `stale`
is `age_hours > ALPHA_PRICE_STALE_AFTER_HOURS` (env, **default 30**).

Never key it off `updated_at`: the collector rewrites the file on a timer whether
or not new candles arrived, so mtime says when the file was *touched*, not how
current the prices are.

30h sits just past the top of the normal 13–37h band. The original 2h was wrong —
against a daily feed it lights permanently, which is the same as having no warning
at all. If you tighten it, do not go below 24h.

Candle timestamps carry no timezone and are read as UTC for this one calculation;
the daily boundary landing exactly on 23:00 is consistent with that.

### Diagnosing a chart that really has stopped

```bash
python - <<'EOF'
import zipfile
z = zipfile.ZipFile('/root/work/bittensor/88/investing_1/Agent/subnet_3mo.zip')
for n in [x for x in z.namelist() if x.startswith('SN79_')]:
    print(z.read(n).decode().strip().splitlines()[-1])
EOF
```

Run it **a day apart**. If the last row has not advanced by ~24 candles between
runs, the upstream collector has genuinely stopped and nothing here can fix it. A
single run showing every subnet at one timestamp proves nothing.

## In the UI

`loadAlphaPrice(range)` → `renderAlphaPrice()` + `renderAlphaPriceChart()`. Range
buttons are a delegated click handler on `#alphaPriceRanges`.

The chip mixes two sources, so it labels both:

- **headline** — live chain price. Never dimmed: it stays correct however far
  behind the history runs.
- **delta** — recomputed as `(live − window first close) / first`, so it ends at
  *now*, matching the number above it. Stashed on the data object as
  `_effective_change_pct` so the theme-repaint path (which re-renders from
  `alphaPriceData`) colours the line by the same figure the chip shows.
- **`#alphaPriceHistEnd`** — `→ MM-DD HH:MM`, the sparkline's own end date, shown
  whenever the headline is live. This is what stops the normal daily gap between
  the two from reading as a bug.
- **`#alphaPriceAge`** — amber "`⚠ history 14h behind`", only past the 30h
  threshold. Worded as *history*, not *price*.

A feed inside its normal band shows just the headline, delta and end-date label.
