# Detecting active runs and completion

The hard part of RegBot isn't launching — it's knowing when a run is *actually*
done. Two independent signals, and the log wins.

## Who calls it, and when

`get_active_runs()` is **not** on the request path. It is ~690ms of SSH fan-out, and
it used to run on every poll of every open tab; `_regbot_active_scan_worker()` now
owns it at `REGBOT_ACTIVE_SCAN_SECONDS` (20s) and `GET /api/regbot/active` reads its
last result out of memory in ~1ms.

Two consequences worth internalising:

- **Cost is independent of how many dashboards are open.** Ten tabs polling at 20s
  produce one scan per 20s, not ten.
- **A failed scan keeps the previous result.** A briefly unreachable box must not
  look like "no runs are active".

`?refresh=1` forces a live scan. The UI passes it only right after an action that
changed what is running — a trigger, a kill, a batch step finishing — where waiting
up to 20s for a pill to appear or disappear would look broken. **A background poll
must never pass it.**

## Discovering live runs

`get_active_runs()` ([regbot.py:856](../../../79Miners/monitor/regbot.py#L856)) fans
out one SSH per server preset, concurrently:

```sh
for f in dashboard_runs/*.pid; do
  pid=$(cat "$f"); kill -0 "$pid" 2>/dev/null && basename "$f" .pid
done
```

`_check_server_active()` **never raises** — an unreachable or timing-out server is
reported as `reachable: false` and listed in `unreachable_servers`, not treated as
an error. One box being down must not blank the whole active bar.

Each discovered `run_id` is joined against the audit log (last 500 entries) to
recover wallet / agent / reglimit / max_count, and `_run_id_started_at()` parses the
timestamp back out of the `run_id` itself.

## Why `kill -0`, not `pgrep`

A `pgrep -f <script name>` would match **the very SSH command running the check** —
its own command line necessarily contains the script name (see `trigger_run`'s
`launch_cmd`). `kill -0 "$(cat …pid)"` tests one specific pid and has no such
reflection problem.

## Why the pid is not the completion signal

`creg79.py` does not always exit cleanly after finishing its work. Observed: a run
sat alive for hours post-completion with no further log output, likely a lingering
connection thread. A raw `kill -0` keeps reporting RUNNING long after there is no
useful work happening.

So completion comes from the **log**.

## The completion rule

`creg79.py` prints `Remaining count: N` exactly once per successful registration,
right after that hotkey's miner launch and Telegram notification — regardless of
chain price movement.

```
finished  ⟺  the last "Remaining count: N" has N < 0
```

**Strictly less than zero, not ≤ zero.** `creg79.py` computes
`N = max_count - count` post-increment, and its loop only exits once
`count > max_count`, at which point N is always exactly `-1`.

`N == 0` happens one registration **earlier** (`count == max_count`) while the loop
condition still holds — i.e. at least one more hotkey attempt is still pending for
any `max_count >= 2`. Treating `0` as finished was a real off-by-one bug that
prematurely marked multi-hotkey runs done after their second-to-last registration.

### Why not the "X/Y registered" line

That line only reprints when the **price changes**, so it goes stale and
under-reports. Observed in practice still showing N-1 after the Nth (final)
registration had already landed. `Remaining count:` is unconditional.

## `tail_log()` — the polling endpoint

`GET /api/regbot/log?server=&run_id=&lines=` (lines clamped 1–2000) returns:

```json
{"server", "run_id", "status": "RUNNING"|"FINISHED",
 "log": "...", "synced_uids": [...], "sync_error": null}
```

One SSH round trip does everything:

```sh
tail -n <lines> <log>; echo '---STATUS---'; kill -0 "$(cat <pid>)" && echo RUNNING || echo FINISHED
```

then the `Remaining count` check overrides `RUNNING` → `FINISHED` when the log says
so. In `get_active_runs`, a run whose log says every requested hotkey registered is
dropped from the active list even though its process is still alive — otherwise the
bar would show a pill forever.

## Pulling back the new params files

On the first transition to FINISHED, `sync_new_params()` runs.

The new UIDs are identified from `creg79.py`'s own log line:

```
Stamped miner_ip=... into run_miner_<uid>.json
```

**Not by file mtime.** `creg79.py`'s default `--propagate-agents` behaviour rewrites
*every* existing miner's control file on the server whenever any one dispatcher
miner is launched, so a time-based "what changed since this run started" check would
false-positive on unrelated, already-registered miners.

`creg79.py` already scp's new files back on its own; this is a belt-and-suspenders
pull for when that push fails silently.

`_synced_uids_cache` stops the 4-second UI poll from re-scp'ing forever once a run
finishes — but it is **only populated on success**, so a failed pull retries on the
next manual refresh. A pull failure surfaces as `sync_error` without failing the log
fetch.

## UI

`#regbotActiveBar` polls `/api/regbot/active` every 20s and renders one pill per
live run with its last log line and registered count. Clicking a pill opens the log
modal, which polls `/api/regbot/log` every 4s. `notifyRegBotFinished()` raises a
toast when a watched run completes.
