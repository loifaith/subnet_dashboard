# Miner params editor

Edit one miner's active sub-agent and its tuned params, push to the box, record the
change. This is the most-used write path in the dashboard.

## Endpoints

| Method | Path | Source |
|---|---|---|
| GET | `/api/miner-params` | [app.py:1127](../../../app.py#L1127) — owned UIDs + a per-UID agent summary |
| GET | `/api/miner-params/{uid}` | [app.py:1144](../../../app.py#L1144) — the raw file |
| PUT | `/api/miner-params/{uid}` | [app.py:1153](../../../app.py#L1153) — save + push + history |
| POST | `/api/miner-restart/{uid}` | [app.py:1274](../../../app.py#L1274) |
| POST | `/api/miner-restart-bulk` | [app.py:1298](../../../app.py#L1298) |

## The roster read is cached as bytes — and the write must invalidate it

`GET /api/miner-params` scans ~200 `run_miner_*.json` files. Measured at **99ms**,
which on a single event loop is 99ms of stall for every other in-flight request, on
a call the page makes on every load. So the scan runs in a thread and the serialised
result is held in `_miner_params_cache` (`MINER_PARAMS_CACHE_SECONDS` = 30s):
**99ms → 1.2ms**.

**`_save_miner_params_file()` calls `_miner_params_cache.invalidate()`.** Any new
writer of `79Miners/params/` must do the same, or the agent column shows the
pre-edit value for up to 30 seconds after a save the user just watched succeed. This
is the failure mode a TTL alone does not cover — see
[../performance/cache.md](../performance/cache.md) §7.

## `agent_index` is an `agent_id`

The single most important rule in this file format. `_active_agent_pos()`
([app.py:1053](../../../app.py#L1053)):

1. find the entry whose `agent_id == agent_index` → return its list position;
2. otherwise, if `agent_index` is in range, treat it as a list position (legacy files);
3. otherwise `None` → HTTP 400.

Positional indexing alone silently picks the wrong agent whenever two entries share
a name, which is common in `dispatch_example.json`.

## PUT semantics: replace, not merge

The editor sends the agent's **complete** param set, so the endpoint replaces it:

- keys present → set (type-coerced against the existing value)
- keys omitted → **removed**
- new keys → added as-is

That's what makes the UI's "remove a param" button work. Contrast with
[agent_bulk_edit.md](agent_bulk_edit.md), which merges a *partial* change.

### Type coercion

`_coerce_param_value()` ([app.py:748](../../../app.py#L748)) preserves the existing
value's type — bool stays bool, int stays int — with one deliberate exception: an
int-typed field given `1.5` returns a float, so fractional input isn't silently
truncated. Unparseable strings fall back to the original value rather than raising.

## The push

`_rsync_miner_params()` ([app.py:847](../../../app.py#L847)) — **despite the name it
does not use rsync**:

```bash
ssh <opts> root@<ip> 'cat > final.<pid>.<uid>.tmp && mv -f ... final'   # file on stdin
```

Why: for a ~4KB file the cost is round-trips, not bytes, and rsync's delta protocol
adds ~5 extra round-trips over a plain SSH channel — measured **~1.2s vs ~0.5s** on
the 160ms-RTT boxes. The temp file sits in the target's own directory so the closing
`mv` is an atomic same-filesystem rename; the miner never reads a half-written file.
The unique temp name (`pid` + `uid`) means concurrent or aborted pushes can't clobber
each other.

The SSH options carry ControlMaster multiplexing — see
[../performance/ssh_multiplexing.md](../performance/ssh_multiplexing.md).

## Restart

`pm2 restart 79-miner-<uid>` over the same warm connection. Bulk restart
**groups UIDs by box** and issues one `pm2 restart a b c …` per box, all boxes in
parallel ([app.py:1321](../../../app.py#L1321)) — one ssh per box rather than per UID,
and it avoids racing the pm2 daemon with concurrent CLI calls. Capped at 500 UIDs.

Restarting is rarely needed: the dispatcher re-reads the params file on every poll.

## History

Every PUT writes an [agent_history](miner_history.md) entry via
`record_agent_switch()`. The `source` field distinguishes:

- `manual` — the active agent changed
- `params_update` — same agent, params edited
- `auto_volume` — written by the auto-switcher, not this endpoint

The recorded `extra.params` is a **diff** (keys added or changed vs the previous
saved params) plus `extra.removed`, not the whole param set — so the history reads
as a changelog.

## Failure behaviour

The local save happens **before** the push, and a failed push still returns HTTP 200
with `rsynced: false` and `rsync_message`. That's intentional: the local file is the
record of intent, and the operator sees the failure in the response. It does mean
local and remote can diverge if a box is down — the history entry records that too.
