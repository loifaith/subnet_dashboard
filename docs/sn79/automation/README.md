# Automation

Things that happen without anyone clicking.

| Doc | Covers |
|---|---|
| [background_workers.md](background_workers.md) | the six loops, their cadence, and the pattern they share |
| [volume_agent_switcher.md](volume_agent_switcher.md) | auto-switching hot miners to `FreeAllAgent` ⚠️ currently broken |

## What runs on its own

| Every | What |
|---|---|
| 60 s | sim-epoch check → Telegram alert on rotation ([../miners/sim_id.md](../miners/sim_id.md)) |
| 300 s | SSH master keepalive ([../performance/ssh_multiplexing.md](../performance/ssh_multiplexing.md)) |
| 300 s | coldkey balance sweep ([../regbot/overview.md](../regbot/overview.md)) |
| 900 s | daily status refresh → history, fee history, auto-switch ([../miners/uid_status.md](../miners/uid_status.md)) |
| 3600 s | coldkey/IP watch ([../miners/coldkeys.md](../miners/coldkeys.md)) |
| 3600 s | Investing88 sync ([../../sn88/performance/cache.md](../../sn88/performance/cache.md)) |
| 30 s | status server poll — separate process ([../status_server/overview.md](../status_server/overview.md)) |
| startup | resume interrupted RegBot batches; bootstrap the SN88 cache |

## Alerting

The only outbound alert is Telegram, and only for **sim-epoch rotation**. Everything
else — a failed worker, a failed params push, a `LAUNCH_FAILED` batch item — is
logged and nothing more.

Two Telegram bots are involved, with different tokens:

| Bot | Sends | Configured in |
|---|---|---|
| `@sn79_sim_id_bot` | sim rotation, from the dashboard | [app.py:65](../../../app.py#L65) |
| the RegBot bot | registration progress, from `creg79.py` on the miner box | [run79.sh:24](../../../79Miners/RegBot/run79.sh#L24) |

Both tokens are hard-coded as defaults and one is committed —
[../tricks/known_issues.md](../tricks/known_issues.md).

## The visibility gap

No worker reports its health anywhere a human looks. A worker that has been failing
for three weeks is indistinguishable from a healthy one unless you grep
`logs/dashboard-error.log` — which is truncated by cron every 30 minutes. That is
exactly how the auto-switcher stayed broken. If you add one thing to this system,
make it a `/api/health` reporting last-success timestamps per worker.
