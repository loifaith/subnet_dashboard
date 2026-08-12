# Chain queries run out of process

The single most consequential performance decision in this codebase.

> **This is still the rule, but it is no longer the fast path.** Chain work must
> stay out of the API process — that has not changed — but it does not have to
> start a *new* process every time. `chain_daemon.py` is a separate process that
> holds one connection open and answers over a Unix socket; everything below is
> what runs when it is unreachable. See [chain_daemon.md](chain_daemon.md).

## The problem, measured

`substrate`'s runtime-metadata decode is **pure Python**, so a chain query holds the
GIL in long bursts. Run inside the API process — *even via `asyncio.to_thread`* —
it starves the asyncio event loop and every unrelated request queues behind it.

Measured on this box, from the docstring at
[chain_cli.py:1-27](../../../79Miners/monitor/chain_cli.py#L1-L27):

| Condition | Event-loop probe latency |
|---|---|
| idle | ~1 ms |
| during **one** reg-cost query (5.4 s long) | 6–406 ms, for the whole 5.4 s |
| during the 300 s balance sweep (18 concurrent queries) | seconds, up to **13.7 s** |

That is what made an agent **Confirm** — which touches no chain at all — take tens
of seconds. It was waiting for the event loop, not for its own work. The stalls
ended exactly when `balance_cache.json` was written, which is how it was diagnosed.

**A thread does not help.** The GIL is the bottleneck, not blocking I/O. This is the
distinction that makes `to_thread` correct for `cloudscraper` calls and wrong for
substrate calls.

## The fix

A child process has its own GIL. [chain_cli.py](../../../79Miners/monitor/chain_cli.py)
does the same work out of process and prints one line of JSON:

```
chain_cli.py balances <netuid> <ss58> [<ss58> ...]  -> {"ok":true,"balances":{addr:{...}},"errors":{}}
chain_cli.py reg-cost <netuid>                      -> {"ok":true,"result":{...}}
```

It **always exits 0**, with `{"ok": false, "error": ...}` on failure, so callers have
exactly one shape to handle.

`run_chain_cli()` ([regbot.py:148](../../../79Miners/monitor/regbot.py#L148)) never
raises either — a spawn failure, timeout or non-zero exit all come back as
`{"ok": false, "error": ...}`.

### Parsing the output

```python
for line in reversed(stdout.splitlines()):
    if line.strip().startswith("{"):
        return json.loads(line)
```

**The payload is the last JSON line, not the whole stdout.** Some `bittensor`
versions print banners to stdout. `chain_cli.py` also notes that bittensor logs to
stderr, which the caller reads separately.

## One connection per run

`cmd_balances` takes **every** address at once and reuses a single `Subtensor`:

```python
sub = connect_subtensor(DEFAULT_ENDPOINT, try_limit=3, verbose=False)
for addr in addresses:
    out[addr] = _balance_and_stake(sub, addr, netuid)   # per-address failure isolated
sub.close()
```

Connecting is the expensive part — that is where the metadata decode happens — so
18 addresses went from **18 connects to 1**. The subnet price is fetched once for
the whole run rather than per address.

`refresh_balance_cache()` additionally de-duplicates addresses before calling
(12 presets share fewer distinct coldkeys), cutting the chain work by roughly a
third.

A single bad address is caught per-iteration and recorded in `errors` — it must not
lose the other seventeen.

## Everything else that loads bittensor

Same rule, same mechanism:

| Work | Child process |
|---|---|
| metagraph | [load_metagraph.py](../../../load_metagraph.py), 90s timeout — fallback only |
| daily miner status | `miner_status_cache.py` as `__main__`, 600s timeout |
| coldkey/IP collection | `collect_coldkeys_ips.py`, 120s timeout |
| balances, reg cost | `chain_cli.py`, 120–300s timeout |

All are spawned with `asyncio.create_subprocess_exec` + `asyncio.wait_for`, with a
**separate short timeout for the spawn itself** (5–10s) and a long one for the work.

## The rule for new code

> If it imports `bittensor` or `substrateinterface`, it runs in a child process.
> If it's network-bound (`requests`, `cloudscraper`, `urllib`), `asyncio.to_thread`
> is fine.

Adding a convenient `import bittensor` at the top of `app.py` would undo all of this.
`chaind` does not violate it: it is its own process with its own GIL, and uvicorn
talks to it over a socket.
