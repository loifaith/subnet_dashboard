# UID status and metrics

Two different endpoints with different jobs. Confusing them is the usual mistake.

| | `/api/uid-status/{uid}` | `/api/uid-metrics` |
|---|---|---|
| Scope | **one** UID, any UID on the subnet (yours or not) | **all owned** UIDs |
| Source | live TAOS query, on demand | today's `miner_status/<date>.json` file |
| Cost | one round of scraper queries | a file read |
| Cache | 60s, keyed `(uid, include_books)` | the daily file itself |
| Used by | the UID Status modal | the main table's metric columns |

## `/api/uid-status/{uid}` — [app.py:1364](../../../app.py#L1364)

Query params: `?books=1` for the per-book breakdown, `?refresh=1` to bypass cache.
Backed by `build_uid_status_response()`
([uid_status_checker.py:416](../../../79Miners/monitor/uid_status_checker.py#L416)),
run via `asyncio.to_thread` (it's network-bound `cloudscraper`, not CPU-bound —
unlike chain work it does *not* need a subprocess).

Works for **foreign miners** because TAOS keys metrics by `agent_id`, not coldkey.

The cache is bounded at `UID_STATUS_CACHE_MAX` (256) entries; on overflow
`_store_uid_status_cache()` evicts the oldest by timestamp
([app.py:1357](../../../app.py#L1357)) — a bounded dict, not a real LRU, but enough
to stop unbounded growth from browsing many UIDs.

Summary gauges come from `miner_gauges` (per-UID aggregates); `--books` /
`?books=1` pulls `agent_gauges` (per-book) and orders books worst→best by
`weighted_kappa`.

## `/api/uid-metrics` — [app.py:1396](../../../app.py#L1396)

Default path: `get_cached_metrics_payload()` reads today's daily file and returns
`metrics` + `prev_metrics` (the previous snapshot, for delta arrows).

`?refresh=1`:
- with `?uids=1,2,3` → live `fetch_agent_display_metrics` for just those UIDs;
- without → runs the **full daily refresh subprocess** and re-reads the file.

⚠️ That second path currently fails — see
[../tricks/known_issues.md](../tricks/known_issues.md).

### The daily refresh

`refresh_daily_status()`
([miner_status_cache.py:214](../../../79Miners/monitor/miner_status_cache.py#L214))
is one job that does six things, in order:

1. `fetch_agent_display_metrics_with_warnings()` — six bulk TAOS queries covering
   all UIDs at once (not one query per UID).
2. `_carry_forward_book_fields()` — fill book-level fields the query missed from
   the previous snapshot ([../tricks/stale_data_handling.md](../tricks/stale_data_handling.md)).
3. `fetch_uid_registration_info()` — reg block, reg date, blocks since registration.
4. `_apply_reg_costs()` — attach per-block registration cost ([reg_cost.md](reg_cost.md)).
5. Fee regime snapshot → `fee_history/` ([fee_regime.md](fee_regime.md)).
6. Save daily status → `miner_status/<date>.json`, append `miner_history/`, then
   run the volume auto-switcher.

`save_daily_status()` snapshots the *previous* contents to
`miner_status/prev_epoch_metrics.json` before overwriting, which is where the
delta arrows in the UI come from.

## The `prev_metrics` contract

`prev_metrics` is "the state at the last refresh", not "yesterday". With a 900s
refresh cadence the deltas are roughly per-15-minutes. Nothing in the payload
states the interval, so don't label them as per-epoch in new UI without checking
`updated_at`.
