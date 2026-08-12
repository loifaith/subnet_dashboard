# Simulation ID (sim epoch)

TAOS periodically rotates the live simulation. Every metrics series is scoped to a
`sim_id`, so a rotation resets scores and is operationally significant — you want to
know the moment it happens.

## Discovery

[sim_uid.py](../../../79Miners/monitor/sim_uid.py) — 33 lines, `urllib` only, no
`cloudscraper`.

The trick: query **one** metric with **no `sim_id` filter**:

```
last_over_time(miner_gauges{wallet=~"<WALLET>", netuid="79", miner_gauge_name=~"score"}[1h])
```

Every live sim returns its own series, so the set of distinct `sim_id` labels in the
response *is* the answer. Cheap — one metric, one round trip — versus enumerating
candidate sims.

Module-level cache with a 5-minute TTL (`SIM_TTL = 300`), and `get_sim_id(force=True)`
to bypass it.

## Endpoint

`GET /api/sim-id` — [app.py:2017](../../../app.py#L2017). Cheap to poll on every
dashboard refresh precisely because of the module cache. Rendered by `renderSimId()`
in the header; hidden for non-79 subnets.

## The watcher

`_sim_id_watch_worker()` ([app.py:309](../../../app.py#L309)) — every
`SIM_ID_POLL_SECONDS` (60):

1. `get_sim_id(force=True)` — **deliberately bypasses the 5-minute cache**, so a
   rotation is caught within ~60s instead of up to 5 minutes late.
2. Compare against `_sim_id_state["last"]`.
3. On change, send a Telegram message and log it.

**The first observation after startup only sets the baseline** — it does not fire an
alert. Without that guard, every `pm2 restart dashboard` would send a spurious
"simulation updated" message, and the alert would stop meaning anything.

## Telegram

Sent by `send_telegram_message()`
([telegram_notify.py](../../../79Miners/monitor/telegram_notify.py)) to
`@sn79_sim_id_bot` / the "SN79 Sim ID Alert" channel.

Bot token and chat id are **hard-coded as defaults** at
[app.py:88-93](../../../app.py#L88-L93), overridable via `SIM_ID_TELEGRAM_BOT_TOKEN`
and `SIM_ID_TELEGRAM_CHAT_ID`. See
[../tricks/known_issues.md](../tricks/known_issues.md) — there is a second,
different hard-coded token in the tracked `run79.sh`.

Failure to send is logged as a warning; the watcher still updates its baseline, so a
Telegram outage costs you one notification, not a permanently wrong state.
