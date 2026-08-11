# Coldkey / IP watch

Surfaces coldkeys and axon IPs that are **newly seen** on the netuid-79 metagraph —
competitors spinning up, or your own registrations landing.

## Design: a diff layer over an untouched tool

`79Miners/Coldkeys/collect_coldkeys_ips.py` is a standalone, reusable script that
dumps the current metagraph's coldkeys and IPs. It is deliberately **not modified**.
[coldkeys_watch.py](../../../79Miners/monitor/coldkeys_watch.py) only adds the
"notice what's new" layer:

1. read the existing output file → `previous`
2. run the collector as a **subprocess** (keeps bittensor out of uvicorn), 120s timeout
3. read the newly written file → `current`
4. set-difference → summary

## The netuid trap

The collector names its output `subnet<netuid>_coldkeys_ips.json`, driven by
`Coldkeys/settings.json`. An earlier version hard-coded the netuid here, so changing
`settings.json` left the watcher reading a **stale file as both "before" and
"after"** — every run diffed identical data and reported "nothing new" forever, with
no error to show for it.

Fixed by `_configured_netuid()` reading `settings.json`, and by resolving the netuid
**once at the top of the run** so a mid-run settings change can't make "before" and
"after" refer to different subnets ([coldkeys_watch.py:102](../../../79Miners/monitor/coldkeys_watch.py#L102)).

Both output paths are per-netuid:

```
79Miners/Coldkeys/out/subnet79_coldkeys_ips.json
79Miners/Coldkeys/out/new_since_last_run_subnet79.json
```

## First run is not "everything is new"

`_diff()` returns empty `new_coldkeys` / `new_ips` when there is no baseline, and
sets `is_first_run: true`. The first run establishes the comparison point; flagging
all ~250 coldkeys as new would be noise, not signal.

## Endpoints

| Path | Source |
|---|---|
| `GET /api/coldkeys/summary` | [app.py:2269](../../../app.py#L2269) — the diff; returns an empty shape rather than 404 when nothing has run |
| `GET /api/coldkeys/list` | [app.py:2285](../../../app.py#L2285) — full current collection; 404 before the first run |
| `POST /api/coldkeys/refresh` | [app.py:2295](../../../app.py#L2295) — run it now |

The background worker `_coldkeys_watch_worker()` runs it every
`COLDKEYS_WATCH_INTERVAL_SECONDS` (3600).

## In the UI

The **Coldkeys** button carries a red badge with the new-coldkey count
(`#coldkeysBadge`). The modal (`openColdkeysModal` / `renderColdkeysSummary`) lists
new coldkeys with their IPs, new IPs, and totals, plus a "refresh now" button.

`newColdkeysOnly()` view mode filters the main table to miners on newly-seen
coldkeys and relabels the stat cards to "(Testing)" — that's the same flag used by
`applyNewColdkeyFilter()`.
