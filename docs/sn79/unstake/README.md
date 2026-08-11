# Unstake & transfer

Unstaking SN79 alpha back to TAO, and sweeping it to a destination address.

> **Moves real, irreversible TAO.** Read [overview.md](overview.md) first.

| Doc | Covers |
|---|---|
| [overview.md](overview.md) | modes, validation, preview, the run lifecycle, timeouts |
| [batches.md](batches.md) | multi-server batches (parallel, unlike RegBot's) |

## Shape

A **local** subprocess — the dashboard and the wallets are on the same box — wrapping
the sibling tool `unstake_env.py`. A dry-run preview shows exactly what would happen;
the real run returns a `run_id` immediately and streams to a log file the UI polls.

## How it differs from RegBot

| | RegBot | Unstake |
|---|---|---|
| Where it runs | remote, over SSH | local subprocess |
| Detachment | `setsid` on the remote box | `start_new_session=True` |
| Batch mode | **serial** round-robin | **parallel** |
| Batch crash-safety | persisted + resumed | in-memory only |
| Cancel | yes (kills the process group) | no |
| Reversible | the spend isn't | nothing is |

Both append to a JSONL audit log, both refresh the balance cache afterwards, and both
return a `run_id` the UI polls rather than blocking a request.

## The one thing people get wrong

A timeout or a non-zero exit **does not mean nothing happened on-chain**. Hotkeys are
unstaked one at a time, each awaiting finalization. Partial output is captured, the
audit entry is written regardless, and the balance cache is refreshed regardless.
Re-running the same action is safe — it only acts on hotkeys still above the stake
threshold, so it resumes where it left off.
