# SSH connection multiplexing

The miner boxes are geographically spread: RTT is ~0.4 ms for the local Hetzner
machines but **125–160 ms** for the overseas ones. Every SSH operation in this
codebase is designed around that.

## The insight

A params push is a ~4 KB file. **The transfer is not the cost.** The cost is the
~10–13 network round-trips of a fresh SSH handshake (TCP + KEX + auth) plus the
protocol chatter — at 160 ms RTT that is 2–3 seconds.

## The configuration

```
-o ControlMaster=auto
-o ControlPath=/tmp/.mp-cm-%C
-o ControlPersist=600
-o GSSAPIAuthentication=no
-o ConnectTimeout=10..15
-o BatchMode=yes            (RegBot)
-o StrictHostKeyChecking=accept-new  (params push)
```

Defined twice, and **deliberately on the same `ControlPath`**:

- `MINER_PARAMS_RSYNC_SSH_OPTS` — [app.py:718](../../../app.py#L718)
- `SSH_OPTS` — [regbot.py:52](../../../79Miners/monitor/regbot.py#L52)

Sharing the path means the params push and the RegBot probes use **one master per
box**, and the params keepalive worker keeps them warm for free.

`GSSAPIAuthentication=no` drops one pointless auth round-trip.

## Measured effect

| Operation | Cold | Warm |
|---|---|---|
| params push to a far box | ~3.0 s | ~1.3 s |
| params push (after switching rsync → SSH stream) | ~1.2 s | ~0.5 s |
| `pm2 restart` over a warm master | — | ~0.5 s |

`get_active_runs()` fans out one ssh per server preset on every poll. Without
multiplexing that was ~10 round-trips × 12–18 servers, every 20 seconds.

## The keepalive worker

`ControlPersist` keeps a master alive only for a while **after its last use**. With
no traffic it expires, and then the next Confirm pays the full cold handshake again.

`_miner_master_keepalive_worker()` ([app.py:1025](../../../app.py#L1025)) runs
`ssh <opts> host true` against every distinct miner IP every
`MINER_PARAMS_KEEPALIVE_SECONDS`, resetting the persist timer.

> **The invariant: `MINER_PARAMS_KEEPALIVE_SECONDS` (300) must stay shorter than
> `ControlPersist` (600).** Raise one without the other and every operation goes
> back to paying cold-handshake latency.

It is best-effort — an unreachable box just fails its refresh (and its confirms
would fail anyway) — and it runs all boxes concurrently via `asyncio.gather`.

`ControlMaster=auto` still falls back to a fresh connection if no master is up, so a
cold start behaves exactly as before, just slower.

## Not rsync

Despite the name, `_rsync_miner_params()` streams the file over a plain SSH channel:

```bash
ssh <opts> root@<ip> 'cat > <final>.<pid>.<uid>.tmp && mv -f ... <final>'   # stdin = file
```

rsync's delta protocol adds ~5 extra round-trips for zero benefit on a 4 KB file.
Content is byte-identical to what rsync wrote (verified with sha256 on text and
binary payloads); only mtime differs, which the miner doesn't use.

The temp file is in the target's own directory, so the closing `mv` is an atomic
same-filesystem rename — the miner never reads a half-written params file.

`miner_params_sync.py` (used by the auto-switcher) still uses real rsync; that's a
separate, older path.

## Batching over fan-out

`_ssh_pm2_restart_many()` restarts every UID on **one box in a single**
`pm2 restart a b c …` call rather than one ssh per UID. Beyond reusing the warm
master, this avoids racing the pm2 daemon with concurrent CLI invocations.

The bulk endpoint groups by IP first, then runs the boxes in parallel.
