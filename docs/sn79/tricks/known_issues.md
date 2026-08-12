# Known issues and gotchas

Verified against the working tree on **2026-08-12**. Fix or re-verify before relying
on any of this.

The page-load performance problems that were on this list are **fixed** — see
[../performance/page_load_audit.md](../performance/page_load_audit.md). What follows
is what is still outstanding.

---

## 1. ~~`volume_agent_switcher` cannot be imported~~ — fixed 2026-08-12, switcher held off

**Fixed**, but read the last subsection before re-enabling: the module works again
and is deliberately **disabled by configuration**.

The bug, for the record —
[volume_agent_switcher.py:38](../../../79Miners/monitor/volume_agent_switcher.py#L38)

```python
AUTO_SWITCH_EXEMPT_AGENTS: set = {
    a.strip()
    for a in os.getenv("VOLUME_SWITCH_EXEMPT_AGENTS",
                       "RandomMakerAgentHarvester", "WarriorAgent", "SpearAgent",
                       "OFTAgent", "CBSAgent", "COCAgent").split(",")
    ...
}
```

`os.getenv` takes **two** arguments. This raises at module import:

```
$ .venv/bin/python -c "import sys; sys.path.insert(0,'79Miners/monitor'); import volume_agent_switcher"
TypeError: getenv() takes from 1 to 2 positional arguments but 7 were given
```

### Blast radius

`refresh_daily_status()` imports it as its **last** step
([miner_status_cache.py:243](../../../79Miners/monitor/miner_status_cache.py#L243)), so:

| Effect | |
|---|---|
| Daily status, miner history, fee history | ✅ still written — they happen before the import |
| Hot-book auto-switch to `FreeAllAgent` | ❌ has not run since the bug was introduced |
| `miner_status_cache.py` exit code | ❌ non-zero → the worker logs `Miner status cache refresh failed` |
| `POST /api/uid-metrics?refresh=1` (no `uids`) | ❌ 502 |

The six-argument call landed **2026-07-20** (`f0c3d8f`); the last switch actually
logged is `volume_agent_switches/2026-07-17.jsonl`, so it was dead for 23 days.
The last clean daily-status refresh before the fix was 2026-07-11. (Don't read
counts off `logs/dashboard-error.log` — see gotcha 5.)

### What was applied

One comma-separated default rather than six arguments — plus a `try` around the
call site, so this class of failure can never again take down a refresh whose files
are already written:

```python
# miner_status_cache.py
try:
    from volume_agent_switcher import maybe_switch_hot_miners_to_free_all
    agent_switches = maybe_switch_hot_miners_to_free_all(metrics)
except Exception as exc:
    logger.error("Volume agent switcher skipped: %s", exc, exc_info=True)
    agent_switches = {"error": str(exc)}
```

Confirmed: `Saved 257 UIDs to miner_status/2026-08-12.json`, first clean refresh
since 2026-07-11. `POST /api/uid-metrics?refresh=1` no longer 502s after 11.4s, and
the worker now reaches its `_uid_metrics_cache.invalidate()` and SSE publish — which
it had been skipping, because the exception fired first.

### The switcher itself is still off, on purpose

`HOT_BOOK_AUTO_SWITCH=0` is set on the dashboard app in
[ecosystem.config.json](../../../ecosystem.config.json). Fixing an import is not the
same decision as resuming automation that rewrites live miner configs and rsyncs
them out — 23 days of hot books have accumulated behind that threshold, and the
first successful run could switch a large part of the fleet to `FreeAllAgent` at
once.

To resume, re-check the threshold and immunity guard in
[../automation/volume_agent_switcher.md](../automation/volume_agent_switcher.md),
then drop the env var and restart **from the ecosystem file** — `pm2 restart
dashboard --update-env` does not re-read it:

```bash
pm2 restart ecosystem.config.json --only dashboard --update-env
```

---

## 2. Live Telegram bot tokens are hard-coded, and one is committed

**Severity: medium.**

| Location | Tracked in git? |
|---|---|
| `SIM_ID_TELEGRAM_BOT_TOKEN` default, [app.py:116](../../../app.py#L116) | yes |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, [run79.sh:24](../../../79Miners/RegBot/run79.sh#L24) | **yes** |

Two different tokens for two different bots. `.gitignore` is otherwise careful
(params, audit logs, caches, `auth_users.json`, `.dashboard_secret_key` are all
excluded), so this looks like an oversight rather than a decision.

Both already read from env with the hard-coded value as a fallback, so the fix is to
drop the defaults and set the env vars — plus rotate the tokens, since they are in
git history.

---

## 3. `SERVER_PRESETS` is duplicated in three places

**Severity: medium (silent wrong behaviour).**

| Copy | Contains |
|---|---|
| `SERVER_PRESETS`, [regbot.py:67](../../../79Miners/monitor/regbot.py#L67) | name, ip, wallet, port_prefix — the source of truth |
| `SERVER_NICKNAMES`, [app.py:961](../../../app.py#L961) | ip → name |
| `SERVER_NICKNAMES`, [miners.html](../../../templates/miners.html) | ip → name |

Add a server to only the first and the table shows a raw IP. Add it to only the
latter two and RegBot rejects it as `Unknown server`.

There *is* a guard against one failure mode: preset names are validated against
`RUN_ID_RE` at import and a bad name (e.g. the trailing space in `"Server3 "`) is a
**startup crash** rather than a per-run failure — because a bad name fails late, at
launch time, one queue item at a time, and a batch would happily accept the server
then burn every slot on "Invalid run_id".

Serving `/api/regbot/presets` to the frontend and dropping the two client/server
copies would remove the class entirely.

---

## 4. Unstake batches do not survive a restart

**Severity: low-medium.** In-memory only, unlike RegBot batches. The
`unstake_env.py` children survive (own process group) but the batch view, per-item
status and post-run balance refresh are lost. Reconstruct from
`79Miners/Unstake/logs/` and `unstake_runs.jsonl`. See
[../unstake/batches.md](../unstake/batches.md).

---

## 5. `pm2 flush` runs every 30 minutes — logs are not a reliable history

**Severity: operational.** Root's crontab has:

```
*/30 * * * * pm2 flush
```

It truncates every PM2 log in place. Because the writers hold open file descriptors,
the files end up **sparse** — old timestamps survive at the head, most of the middle
is NUL, and only the last ≤30 minutes is genuinely complete.

Consequences:

- `grep -c` over `logs/dashboard-error.log` is **not** an occurrence count.
- A failure that happens every 15 minutes may show zero hits if you look in the wrong
  half-hour.
- Debugging anything intermittent needs `pm2 logs dashboard` live, or disabling the
  cron entry first.

Also note `logging.basicConfig` writes to **stderr**, so ordinary INFO lines land in
`*-error.log`, not `*-out.log`.

---

## 6. New invariants that are easy to break

These came in with the 2026-08-11/12 performance work. Nothing fails loudly if you
violate them — that is why they are written down.

| Invariant | Break it by | Symptom |
|---|---|---|
| `_miner_params_cache.invalidate()` on every params write | adding a second writer of `79Miners/params/` | agent column shows the pre-edit value for up to 30s after a successful save |
| `METAGRAPH_REFRESH_SECONDS` < `METAGRAPH_CACHE_TTL` | raising it past 120 | every metagraph copy reads as stale |
| Worker cadence is a deadline from the tick's start | `await asyncio.sleep(INTERVAL)` after the work | real cadence is `interval + work` — the metagraph worker's nominal 90s was really 105s |
| Every warm-refresh cadence stays under its TTL | setting a TTL equal to the client's poll interval | *every* refresh lands on expiry — this is what produced 3-4s of "waiting for server response" on fee-regime and reg-cost |
| `_uid_metrics_cache` / `_miner_params_cache` are keyed on a change signal | reverting them to a plain TTL | a valid result is discarded on a timer and the next caller pays a 718ms rebuild |
| `/api/regbot/active?refresh=1` only after a state change | passing it from a poll | the 690ms SSH fan-out across 12 boxes is back, per tab |
| background polls created only in `startBackgroundPolling()` | a bare `setInterval` elsewhere | hidden tabs resume polling; SSH fan-out runs for nobody |
| `_json_bytes_response` bodies come from `_serialize_json` | hand-rolling `json.dumps` | ETag stops matching the body, 304s become wrong |
| A generation timestamp in a body is excluded from its ETag | adding `updated_at`/`fetched_at` without `etag_over=` | revalidation silently stops working; the payload is re-sent on every poll forever |
| `/api/metagraph-state` is only valid against the identity it names | ignoring `identity_etag` on the client | every metric maps onto the wrong hotkey after a registration; a length check will not catch it |
| Change notifications come from whatever changes the data | publishing from a request handler that saw a cold cache | the notification races the fetch that caused it, and clients re-download the payload they were already getting |
| `UID_METRICS_DROP_FIELDS` only holds fields the client provably never reads | adding one without checking | the column silently renders empty |
| No user action waits on a chain read before painting | awaiting a `?refresh=1` in a loader | the Refresh button freezes the page for ~3s per click |
| A `/refresh` endpoint reads its worker's cache, it does not redo the work | calling the sweep synchronously in the handler | `POST /api/regbot/balance/refresh` blocked 4.5-15s for a body its only caller discards |
| Panels never gate the Refresh button | awaiting them in `loadData`'s returned chain | the button reads busy for ~3s after the page is already current |
| An optional side effect at the end of a job is wrapped in `try` | calling it bare | a failure discards work that already succeeded — exactly issue 1 |

## 7. Smaller things

- **`refresh_daily_status()` does six unrelated jobs.** A failure in any one used to
  take the rest with it. The auto-switch — the last and most failure-prone — now has
  its own `try`, which is what contains issue 1. The other five are still one
  fate-sharing sequence.
- **`_uid_status_cache` eviction is O(n)** — `min()` over up to 256 entries on every
  insert once full. Fine at this size; not a pattern to copy.
- **`INVESTING88_SYNC_HOUR` / `INVESTING88_SYNC_TZ` are dead config** in `app.py` —
  the worker loops hourly and never reads them. The 19:00 Tokyo boundary actually
  lives in `investing88_cache.py`'s day-keying.
- **`run79.sh`'s header comment is self-referential** — it calls itself "the
  example/template counterpart to run79.sh". Harmless, but it reads as if a second
  file exists.
- **FastAPI title still says "SN71 Session Manager"** ([app.py:673](../../../app.py#L673)),
  and `miners.html`'s `<title>` says SN88. Cosmetic.
- **Worker failures are still mostly invisible in the UI.** Every background worker
  logs and continues; nothing surfaces. That is how issue 1 went unnoticed for 23
  days, and there are now **ten** workers rather than six. The two new ones at least have a
  tell — `X-Cache` on `/api/metagraph-data` and `/api/regbot/active` reports
  `hit|stale|miss|stale-error`, and a persistent `stale` means the worker is wedged:

  ```bash
  curl -sI localhost:12800/api/metagraph-data?netuid=79 | grep -i x-cache
  ```

  A `/api/health` endpoint reporting last-success timestamps per worker is still the
  right fix for the class, and is now the single highest-value thing left undone.
- **`/js/miners.js` is now the largest single same-origin payload** at ~56KB
  gzipped — more than half of a cold load, though it answers 304 on every load
  after the first.
- **Bootstrap, Bootstrap Icons and Chart.js still come from jsdelivr** — 138KB over
  4 requests to a second origin, and the render-blocking stylesheet costs a
  DNS + TCP + TLS handshake before first paint. Self-hosting them under `/asset/`
  would put them on the connection that is already open.
