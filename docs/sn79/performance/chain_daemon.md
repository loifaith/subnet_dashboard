# One shared chain connection

`chain_daemon.py` holds a single long-lived `AsyncSubtensor` and answers chain
queries for the whole dashboard over a Unix socket. It is the second half of the
lesson in [chain_subprocess.md](chain_subprocess.md): chain work must stay out of
the API process, but it does **not** have to start a new process every time.

## Why: the query was never the expensive part

Measured on this box with the venv interpreter, all on a warm connection:

| | |
|---|---|
| `import bittensor` | **0.77 s** |
| connect + runtime-metadata decode | **1.04 s** |
| **fixed setup, paid on every call** | **~1.81 s** |
| metagraph sync, one netuid | 0.40 s |
| `Burn` storage query (reg cost) | 0.02 s |
| subnet price | 0.06 s |

Confirmed independently through `chain_cli.py`: one address 2.04 s versus twelve
addresses 3.10 s, i.e. ~0.096 s marginal per query and ~1.94 s fixed.

So **~1.8 s of setup bought ~0.5 s of work**, and it was paid three times per
metagraph cycle plus once per reg-cost and balance sweep. Holding the connection
open removes all of it.

## Results

| Path | Subprocess | Via daemon |
|---|---|---|
| `metagraph` one netuid | 2.97 s | **0.46 s** |
| metagraph worker cycle (3 netuids + gaps) | 12.8 s | **~1.4 s** |
| `balances` for 12 coldkeys | 3.10 s | **0.31 s** |
| `reg-cost` | 2.20 s | **0.001 s** (see below) |
| `GET /api/metagraph-data?refresh=1` | 2.9–3.2 s | **0.55 s** |
| `GET /api/reg-cost?refresh=1` | 2.20 s | **0.002 s** |

## What it deliberately does not do

**Registration.** Real miner registration (`creg79` / `run79.sh`) keeps its own
connection on the miner box, exactly as before. A registration race is timing
critical and must not queue behind a dashboard read or share a socket whose
reconnect it does not control.

The daemon is **read-only**: it never signs, never submits an extrinsic, and holds
no wallet.

## Block-paced reg-cost polling

Modelled on `BT_79_Dashboard/scripts/reg_price_watcher.py` and the reg bot's
`ChainPollSession`. Registration cost only changes when a block lands (~12 s), so
polling it on an arbitrary timer is either stale or wasteful:

```
sample -> new block?  yes -> sleep 10s (settle; the next block cannot be sooner)
                      no  -> sleep 1s  (fast-poll to catch the turn)
```

That is ~2–3 queries per block on a connection that is already open, instead of a
fresh connection per request. `/api/reg-cost` is then served from memory and is
never more than one block old — verified tracking blocks 8828405 → 8828408 with
the recycle price decaying each block, where it used to be up to 240 s stale.

Because a refresh now costs a ~1 ms socket read and no chain query,
`_reg_cost_refresh_worker` runs at `REG_COST_REFRESH_FAST_SECONDS` (10 s) while
the daemon is answering, and drops back to `REG_COST_REFRESH_SECONDS` (240 s) if
it ever falls back to the 2.1 s subprocess. **Getting that backwards is the trap
the fast path invites**, so the interval is derived from the recorded `source`
field, never assumed.

The poller also records the price sampled immediately *before* each up-tick — what
the last registrant actually paid — and persists it to
`miner_status/reg_cost_<netuid>.json` so a restart does not forget it.

## Protocol

Newline-delimited JSON over `AF_UNIX` at `/tmp/.sn79-chaind.sock` (0600). The
request mirrors `chain_cli.py`'s argv so `run_chain_cli()` routes to either
without translating:

```json
{"cmd": "reg-cost",  "args": ["79"]}
{"cmd": "balances",  "args": ["79", "<ss58>", "..."]}
{"cmd": "metagraph", "args": ["79"]}
{"cmd": "ping",      "args": []}
```

Every response is one JSON line carrying `ok`, so callers read one shape.

> **The stream limit is load-bearing.** `asyncio`'s `StreamReader` refuses any
> line longer than 64 KiB by default, and a netuid-79 metagraph payload is ~150 KB.
> Both `start_unix_server()` and `open_unix_connection()` are given
> `limit=STREAM_LIMIT` (32 MB). Without it `readline()` raises
> `LimitOverrunError`, the client treats that as "unreachable", and the daemon
> looks broken while working perfectly.

## It is never a single point of failure

`chain_client.call()` returns **`None`** — meaning "fall back" — when the socket
is missing, times out, or the response is unparseable. A daemon that *answers*
with `{"ok": false}` is a real chain error and is returned as-is, so a genuine
failure is not silently retried down a second path.

Every caller then runs the child-process path it always ran:

- `run_chain_cli()` → `chain_cli.py` ([regbot.py](../../../79Miners/monitor/regbot.py))
- `_load_metagraph()` → `_load_metagraph_subprocess()` → `load_metagraph.py`

Verified by stopping `chaind` outright: warm endpoints kept serving from cache at
1–7 ms, and `?refresh=1` fell back to the subprocess at 3.0 s — the old behaviour
exactly, with no errors. A missing socket costs one failed `os.path.exists`, and
`chain_client` remembers the absence for `CHAIN_DAEMON_RECHECK_SECONDS` so a
dashboard running without the daemon does not attempt a connect per call.

## One payload builder, two paths

`load_metagraph.build_payload()` is a pure function shared by the daemon and the
subprocess. Two copies would be free to drift, and the drift would only show on
the fallback path — the one that runs when something is already wrong.

Splitting it surfaced a latent bug immediately: `metagraph.block` is a numpy
scalar, so `current_block - reg_block` is a numpy int and comparing it to
`immunity_period` yields `numpy.bool_`, which `json.dumps` refuses. The old script
hid this behind a `convert_to_native()` pass over the finished payload. The values
are now coerced where they are computed.

## Operating it

Runs under pm2 as **`chaind`** ([ecosystem.config.json](../../../ecosystem.config.json)).

```bash
pm2 restart ecosystem.config.json --only chaind --update-env   # re-reads env
pm2 logs chaind
```

Health check — `served` and `reconnects` tell you whether the connection is being
reused or churning:

```bash
python - <<'PY'
import asyncio, sys; sys.path.insert(0, "79Miners/monitor")
import chain_client; print(asyncio.run(chain_client.call("ping", [])))
PY
```

| Env var | Default | Meaning |
|---|---|---|
| `CHAIN_DAEMON_SOCKET` | `/tmp/.sn79-chaind.sock` | socket path (client and daemon must agree) |
| `CHAIN_DAEMON_ENDPOINT` | `finney` | chain endpoint |
| `CHAIN_DAEMON_REG_COST_NETUIDS` | `79,88` | netuids kept block-fresh |
| `CHAIN_DAEMON_SETTLE_SECONDS` | `10` | sleep after a new block |
| `CHAIN_DAEMON_POLL_SECONDS` | `1` | fast-poll while waiting for the turn |
| `CHAIN_DAEMON_QUERY_TIMEOUT` | `90` | one query may not wedge the connection |
| `CHAIN_DAEMON_STREAM_LIMIT` | `33554432` | must match on both sides |

A failed query calls `invalidate()`, so the next caller reconnects rather than
every in-flight request inheriting a dead websocket. Connecting is serialised by a
lock — otherwise a chain hiccup has every waiter open its own replacement, which
is the connection burst this whole daemon exists to avoid.
