# Stale data beats missing data

The TAOS metrics API is flaky in one specific way: the high-cardinality per-book
(`agent_gauges`) range query **intermittently returns an empty result** while the
aggregate query succeeds. Naively, that renders every book count as 0.

Zero is not "unknown". It is a real, wrong number that looks authoritative — a
miner with 40 hot books would display as having none, and the auto-switcher would
stop firing.

Four places handle this, all the same way: carry forward the last known good value
and flag it.

## 1. status_server — per-cycle carry-forward

[status_server.py:133-153](../../../79Miners/monitor/status_server.py#L133-L153)

```python
if books:
    counts = _derive_counts(books, warn_at)
    _last_good_counts[uid_str] = counts     # remember
else:
    counts = _last_good_counts.get(uid_str, {...zeros...})   # reuse
books_stale = fresh_count < len(my)
```

`_last_good_counts` is seeded on startup from the newest CSV snapshot that has real
data (`_load_csv`), so a restart during a flaky period doesn't lose the baseline.

**Every response carries `books_stale`** — `/status`, `/status/{uid}`, `/history`.
Consumers can tell fresh from carried-forward. The CSV stores it per row too.

## 2. Daily refresh — cross-snapshot carry-forward

`_carry_forward_book_fields()`
([miner_status_cache.py:184](../../../79Miners/monitor/miner_status_cache.py#L184))

`fetch_agent_display_metrics_with_warnings` deliberately leaves book fields
**absent** rather than zeroing them when the query fails. The carry-forward then
copies missing fields from the previous daily snapshot and logs a warning with the
count of fields carried.

The distinction matters: *absent* is a signal the carry-forward can act on; *zero*
would be indistinguishable from a real zero.

Carried fields: `pnl_books_pos`, `pnl_books_neg`, `hot_book_count`, `book_count`,
`max_book_volume`.

It early-exits if nothing is missing, so the common path costs one comprehension.

## 3. Fee regime — serve the last good summary

`_cached_fallback()` ([app.py:2408](../../../app.py#L2408)) returns the cached
summary with `stale: true` added when the upstream query fails or reports an error.
Only when there is no cached payload at all does it 502.

The cache is populated on the `?books=1` path too, specifically so a fallback exists
even if the very first request asked for books.

## 4. TAOS queries — window, don't sample an instant

Every query uses a 10-minute range with `step=60` and at least one retry
(`query_latest`, `query_miner_gauge_all_uids`, `fetch_book_rates_sampled` with 3
attempts):

```python
data={"query": q, "start": now-600, "end": now, "step": "60"}
```

An instant query between validator scrapes reads as missing data. The window makes a
scrape gap invisible. An empty result is treated as a **miss to retry**, never as a
valid zero.

## The pattern to copy

```
1. Distinguish "absent" from "zero" at the fetch layer.
2. Keep a last-good value keyed by entity.
3. On a miss, reuse it — don't emit zeros.
4. Flag the response so a consumer can tell (`stale`, `books_stale`).
5. Log it, so a permanent upstream failure is visible in logs even though the UI
   looks fine.
```

Point 5 is the trade-off being accepted: the UI deliberately hides transient
upstream failure, so a *permanent* one is invisible there. Logs are the only place
it shows up.
