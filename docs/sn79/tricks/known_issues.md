# Known issues and gotchas

Verified against the working tree on **2026-08-11**. Fix or re-verify before relying
on any of this.

The page-load performance problems that were on this list are **fixed** — see
[../performance/page_load_audit.md](../performance/page_load_audit.md). What follows
is what is still outstanding.

---

## 1. `volume_agent_switcher` cannot be imported — the auto-switcher never runs

**Severity: high.** [volume_agent_switcher.py:36](../../../79Miners/monitor/volume_agent_switcher.py#L36)

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

`logs/dashboard-error.log` shows the resulting failure recurring through
2026-08-11 04:36; the last logged *success* is 2026-07-11. (Don't read counts off
that file — see gotcha 5.)

### Fix

```python
_DEFAULT_EXEMPT = "RandomMakerAgentHarvester,WarriorAgent,SpearAgent,OFTAgent,CBSAgent,COCAgent"
AUTO_SWITCH_EXEMPT_AGENTS = {
    a.strip() for a in os.getenv("VOLUME_SWITCH_EXEMPT_AGENTS", _DEFAULT_EXEMPT).split(",") if a.strip()
}
```

Then re-check the threshold and immunity guard in
[../automation/volume_agent_switcher.md](../automation/volume_agent_switcher.md)
before letting it loose on a live fleet that hasn't been auto-switched in weeks.

---

## 2. Live Telegram bot tokens are hard-coded, and one is committed

**Severity: medium.**

| Location | Tracked in git? |
|---|---|
| `SIM_ID_TELEGRAM_BOT_TOKEN` default, [app.py:88](../../../app.py#L88) | yes |
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
| `SERVER_NICKNAMES`, [app.py:728](../../../app.py#L728) | ip → name |
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

These came in with the 2026-08-11 performance work. Nothing fails loudly if you
violate them — that is why they are written down.

| Invariant | Break it by | Symptom |
|---|---|---|
| `_miner_params_cache.invalidate()` on every params write | adding a second writer of `79Miners/params/` | agent column shows the pre-edit value for up to 30s after a successful save |
| `METAGRAPH_REFRESH_SECONDS` < `METAGRAPH_CACHE_TTL` | raising it past 120 | the 2.9s chain subprocess is silently back on the request path |
| `/api/regbot/active?refresh=1` only after a state change | passing it from a poll | the 690ms SSH fan-out across 12 boxes is back, per tab |
| background polls created only in `startBackgroundPolling()` | a bare `setInterval` elsewhere | hidden tabs resume polling; SSH fan-out runs for nobody |
| `_json_bytes_response` bodies come from `_serialize_json` | hand-rolling `json.dumps` | ETag stops matching the body, 304s become wrong |

## 7. Smaller things

- **`refresh_daily_status()` does six unrelated jobs.** A failure in any one (as in
  issue 1) takes the rest with it. Splitting the auto-switch out behind its own
  `try` would have contained that bug to a warning.
- **`_uid_status_cache` eviction is O(n)** — `min()` over up to 256 entries on every
  insert once full. Fine at this size; not a pattern to copy.
- **`INVESTING88_SYNC_HOUR` / `INVESTING88_SYNC_TZ` are dead config** in `app.py` —
  the worker loops hourly and never reads them. The 19:00 Tokyo boundary actually
  lives in `investing88_cache.py`'s day-keying.
- **`run79.sh`'s header comment is self-referential** — it calls itself "the
  example/template counterpart to run79.sh". Harmless, but it reads as if a second
  file exists.
- **FastAPI title still says "SN71 Session Manager"** ([app.py:440](../../../app.py#L440)),
  and `miners.html`'s `<title>` says SN88. Cosmetic.
- **Worker failures are still mostly invisible in the UI.** Every background worker
  logs and continues; nothing surfaces. That is how issue 1 stayed unnoticed, and
  there are now **eight** workers rather than six. The two new ones at least have a
  tell — `X-Cache` on `/api/metagraph-data` and `/api/regbot/active` reports
  `hit|stale|miss|stale-error`, and a persistent `stale` means the worker is wedged:

  ```bash
  curl -sI localhost:12800/api/metagraph-data?netuid=79 | grep -i x-cache
  ```

  A `/api/health` endpoint reporting last-success timestamps per worker is still the
  right fix for the class, and is now the single highest-value thing left undone.
- **`/api/uid-metrics` is the largest remaining same-origin payload** at ~56KB
  gzipped. It ships every metric for every UID; the table renders a subset.
- **Bootstrap, Bootstrap Icons and Chart.js still come from jsdelivr** — 138KB over
  4 requests to a second origin, and the render-blocking stylesheet costs a
  DNS + TCP + TLS handshake before first paint. Self-hosting them under `/asset/`
  would put them on the connection that is already open.
