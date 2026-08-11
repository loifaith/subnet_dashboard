# RegBot

Registering new SN79 hotkeys and launching miners for them.

> **Spends real TAO.** Read [overview.md](overview.md) before touching any of it.

| Doc | Covers |
|---|---|
| [overview.md](overview.md) | how one run works, endpoints, validation, balances, kill |
| [run79_template.md](run79_template.md) | the script template and its six load-bearing lines |
| [batches.md](batches.md) | multi-server round-robin batches and crash-safe resume |
| [active_runs.md](active_runs.md) | detecting live runs and, harder, detecting completion |

## The shape in one paragraph

The dashboard reads a shell-script template, rewrites six lines with the chosen
agent / price cap / count / wallet / port band, scp's it to
`Paul-SuperRegBot/dashboard_runs/` on the target box, and launches it fully detached
under `setsid` with its stdout to a log and its pid to a pidfile. The HTTP request
returns immediately; registration itself can run for hours waiting for the chain
price to clear. Progress is read by tailing the remote log over SSH; completion is
judged from the log, not the pid. On completion the newly written
`run_miner_<uid>.json` files are pulled back.

## Safety properties worth preserving

| Property | Mechanism |
|---|---|
| The wallet can't be chosen by the caller | it comes from the server preset |
| Nothing user-supplied can break out of the root shell | allowlist regexes, [../tricks/input_validation.md](../tricks/input_validation.md) |
| A kill reaches `creg79.py`, not just the wrapper | `setsid` + negative-pid signal |
| A restart never double-registers | `run_id` assigned before launch; resume asks whether *that* run landed |
| An unreachable server never causes a relaunch | `_remote_run_exists` raises rather than guessing |
| Every trigger is recorded | `regbot_runs.jsonl` |
| A price spike mid-retry can't overpay | `--feelimit` set equal to `--reglimit` |
