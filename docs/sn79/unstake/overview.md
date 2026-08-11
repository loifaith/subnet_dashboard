# Unstake and transfer

> **This moves real, irreversible TAO.** Every non-dry-run execution is appended to
> an audit log. There is no undo.

[79Miners/monitor/unstake.py](../../../79Miners/monitor/unstake.py).

## What it wraps

A sibling tool, `unstake_env.py`, at
`/root/work/bittensor/bittensor-reg-script/unstake-transfer/`, run with **its own
venv python** and `cwd=UNSTAKE_ENV_DIR` (so its `load_dotenv()` finds the right
`.env`).

Local subprocess — **no SSH**. The dashboard and the wallets live on the same box
(95.216.27.87). Contrast with RegBot, which is entirely remote.

## Three modes

| Mode | Unstakes? | Sends? | Needs destination |
|---|---|---|---|
| `unstake` | yes | no — TAO stays on the coldkey | no |
| `unstake_transfer` | yes | yes, in the same step | yes |
| `transfer` | no | sweeps the free balance already there | yes |

`amount` applies to `transfer` only; omitted entirely from the command line when
unset so `unstake_env.py` falls back to **its own** sweep-everything default rather
than one restated here.

## Destinations

`DEFAULT_DESTINATIONS` ([unstake.py:68](../../../79Miners/monitor/unstake.py#L68)) maps
wallet → sweep address. Note `wolf003` and `wolf005` sweep somewhere different from
the other `wolf*` coldkeys — that is intentional, not a typo.

These are only **form pre-fills**. Every destination is restated in the confirm
dialog and can be overridden per run.

## Validation

`_validate()` checks the server preset, the mode, and the destination against
`_SS58_RE = ^[1-9A-HJ-NP-Za-km-z]{46,48}$`.

That regex is a **loose sanity check, not checksum validation** — base58 alphabet
and plausible length. Enough to catch a garbage paste before spending an extrinsic
fee (or worse, sending real TAO) toward a doomed address. The chain is the final
arbiter. If you want real safety here, add SS58 checksum verification.

## Preview first

`POST /api/unstake/preview` runs the same command with `--dry-run`, 60s timeout.
It shows which hotkeys would be unstaked, the free balance, and (for
`unstake_transfer`) the sendable amount — without submitting anything. A dry run
only reads chain state, so it stays fast regardless of hotkey count.

## The real run

`POST /api/unstake/run` returns a `run_id` **immediately** and does the work in a
background task, because `unstake_multiple` submits **one extrinsic per hotkey,
sequentially**, each with `wait_for_inclusion=True` and
`wait_for_finalization=True`. For a wallet with many hotkeys that is genuinely many
minutes — a 120s timeout once killed a real run partway through, after it had
already unstaked some hotkeys but not all.

`RUN_TIMEOUT_SECONDS = 1800` (30 min). The UI polls `GET /api/unstake/log?run_id=`
every 4s; output streams to `79Miners/Unstake/logs/<run_id>.log`.

`PYTHONUNBUFFERED=1` is set on the child — without it Python block-buffers stdout
when it isn't a TTY, so prints would sit in a buffer until process exit and there
would be nothing to tail.

## Two protections worth knowing

### `start_new_session=True`

The child gets its **own process group**. Without it, `pm2 restart dashboard` — say,
to deploy an unrelated change — sends its termination signal to the whole process
group, which reaches this child and kills an in-flight unstake mid-extrinsic.
Observed in practice: a restart SIGINT'd a run partway through its 5th hotkey.

Same reasoning as RegBot's `setsid`. See
[../tricks/process_detachment.md](../tricks/process_detachment.md).

### Per-server busy lock

`_server_busy()` blocks a second run on the **same** server while one is in flight —
that's a double-submit risk on one coldkey. It is deliberately **per-server, not
global**: separate coldkeys are independent, so concurrent unstakes on different
servers are fine and are exactly what the batch does.

## On timeout, nothing is assumed

If the wrapper is killed at 1800s, the partial output is **captured, not discarded**
— it is the only record of what actually happened. The response carries an explicit
note:

> Killing the client-side wrapper does NOT undo what it already submitted. Hotkeys
> are unstaked one at a time, so some may have completed. It's safe to re-run the
> same action — it only acts on hotkeys still above the stake threshold, so it picks
> up exactly where this left off.

That idempotence is what makes the retry advice safe.

## After every run

The audit entry is appended **and** the balance cache is refreshed — on success,
failure *and* timeout. A non-success result does not mean nothing happened on-chain,
so the balances must be re-read regardless.

```
79Miners/Unstake/unstake_runs.jsonl
{triggered_at, triggered_by, server, wallet, netuid, mode, destination,
 amount, success, output, returncode, timed_out}
```
