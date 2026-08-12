# Input validation on the money paths

The dashboard interpolates user input into shell scripts that run as **root** on
remote boxes, and into commands that spend TAO. Validation is deliberately narrow
and happens twice.

## Allowlist, never escape

Nothing here tries to escape shell metacharacters. Every free-text field that
reaches a shell is matched against a restrictive regex first, and rejected outright
if it doesn't fit.

| Field | Regex / rule | Reaches |
|---|---|---|
| `server` | must be a key in `_PRESETS_BY_NAME` | selects ip **and wallet** |
| `dispatch_agent` | `^[A-Za-z0-9_]+$` | `DISPATCH_AGENT=` in a root shell script |
| `dispatch_agent_id` | int 0–999 | `DISPATCH_INDEX=` |
| `port_prefix` | `^[0-9X]{1,4}(,[0-9X]{1,4})*$` | `PORT_PREFIX=` |
| `run_id` | `^[a-zA-Z0-9_.-]+$` | remote log/pid/script **paths** |
| `reglimit` | float 0.0001–5.0 | the spend cap |
| `max_count` | int 1–10 | number of hotkeys |
| `destination` | `^[1-9A-HJ-NP-Za-km-z]{46,48}$` | where TAO is sent |
| `netuid` | int 0–255 | queries |
| `uid` | int 0–65535 | queries |

`port_prefix`'s character class is described in the code as "deliberately
restrictive — digits, X and commas only, nothing that could break out of the
assignment". `creg79.py` validates the *semantics* (band ordering, width)
separately; this layer only guarantees it can't be a shell injection.

## The wallet is never user input

The single most important rule in RegBot. `_validate_inputs()` returns the **preset**,
and `trigger_run` takes `ip, wallet = preset["ip"], preset["wallet"]`. A caller can
choose *which* server, never *which* wallet. Same in unstake:
`preset["wallet"]` is what reaches `--coldkey`.

## Where escaping is used

Where a value is a path or a process name rather than a script variable, it goes
through `shlex.quote`:

```python
remote_cmd = f"cat > {shlex.quote(tmp)} && mv -f {shlex.quote(tmp)} {shlex.quote(final)}"
remote_cmd = "pm2 restart " + " ".join(shlex.quote(n) for n in names)
```

Those values are derived from an int `uid`, so they're already safe; the quoting is
belt-and-braces.

## Pydantic with `extra="forbid"`

Every RegBot/unstake request body is a Pydantic model with
`model_config = ConfigDict(extra="forbid")`
([app.py:2898](../../../app.py#L2898)). A misspelled field is a **400**, not
a silently-ignored parameter that leaves you registering with a default `reglimit`.

## Validate the whole batch before starting it

`start_batch` runs the *same* `_validate_inputs()` that `trigger_run` will apply,
against every server config, before the first launch. `start_unstake_batch` does the
same plus duplicate-server and busy checks.

Rejecting up front beats discovering a bad config partway through a queue that has
already spent TAO.

## Two-sided validation

The frontend also validates (`isValidPortPrefix`, `modeNeedsDestination`,
`updateUnstakeRunEnabled`, the reg-cost and balance warnings) — but purely for UX.
The server never trusts it. Confirm dialogs restate server, wallet, destination and
amount so the operator sees exactly what will happen, and then the server checks it
all again.

## Bounds on read paths too

Not just the money paths: `days` clamped 1–90, `limit` clamped 1–500, `lines`
clamped 1–2000, `minutes` constrained `ge=1, le=180`, bulk restart capped at 500
UIDs. These bound work per request rather than guard against injection.
