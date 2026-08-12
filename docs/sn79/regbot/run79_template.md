# The run79.sh template

[79Miners/RegBot/run79.sh](../../../79Miners/RegBot/run79.sh) is a **template**, not a
script that is run as-is. `_build_script()`
([regbot.py:377](../../../79Miners/monitor/regbot.py#L377)) reads it, rewrites six
specific lines, and scp's the result to the target box.

## The substitutions

Each is a `re.subn(..., count=1, flags=re.M)` that **raises `RegBotError` if it does
not match exactly once**. A drifted template fails loudly at trigger time with
"Template substitution failed for pattern: …" rather than launching something
half-configured.

| Line rewritten | To |
|---|---|
| `cd "$(dirname "$0")" \|\| exit 1` | `cd "/root/79/Paul-SuperRegBot" \|\| exit 1` |
| `DISPATCH_AGENT="${DISPATCH_AGENT:-…}"` | the chosen agent name |
| `DISPATCH_INDEX="${DISPATCH_INDEX:-…}"` | the chosen `agent_id`, or empty |
| `REGLIMIT="${REGLIMIT:-…}"` | the price cap |
| `MAX_COUNT="${MAX_COUNT:-…}"` | how many hotkeys |
| `WALLET="${WALLET:-…}"` | **the server preset's wallet** |
| `PORT_PREFIX="${PORT_PREFIX-…}"` | only when a band was requested |

### Why the `cd` is rewritten

The template's `cd "$(dirname "$0")"` assumes it lives directly in
`Paul-SuperRegBot/` next to `.venv` and `creg79.py`. Generated copies go into
`Paul-SuperRegBot/dashboard_runs/`, one level down, so the original `cd` would land
in the wrong directory and neither the venv nor `creg79.py` would resolve.

### Why `PORT_PREFIX` uses `-` not `:-`

`${PORT_PREFIX-79XX}` (single dash) honours an explicitly empty value as "no band",
falling back to `creg79.py`'s legacy uncapped port walk. `${PORT_PREFIX:-79XX}`
would treat empty as unset. The substitution regex has to match the single dash;
this is easy to break by "tidying" the template.

## Editing the template

> The comment block above `DISPATCH_INDEX` in the file says it explicitly:
> **removing that line breaks every dashboard-triggered registration** with
> "Template substitution failed". The same applies to all six lines above.

Safe edits: anything not on those lines. Unsafe: renaming a variable, changing
`:-` to `-` or back, wrapping an assignment in a conditional, reformatting the
assignment across lines.

There is no test covering this. The fastest check after editing is to call
`_build_script()` directly:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'79Miners/monitor')
import regbot; print(regbot._build_script('WarriorAgent', 0.1, 1, 'bittensor001', 42, '79XX')[:0] or 'OK')"
```

## What the generated script runs

```
creg79.py --netuid 79 --name $WALLET --path /root/.bittensor/wallets
          --launch-miner --miner-repo /root/79/sn-79
          --agent-preset dispatcher  ($DISPATCH_ARG)
          --no-check-whitelist --connect-retries 10 --connect-retry-base 3
          --reglimit $REGLIMIT --feelimit $REGLIMIT
          --max-count $MAX_COUNT  ($PORT_ARGS)
          --telegram-token ... --telegram-chat-id ...
```

### `--reglimit` and `--feelimit` must both be set, and must match

`--reglimit` is only a **one-time outer gate** before the retry loop. `--feelimit` is
what makes `creg79.py` re-check the live recycle fee **before every retry inside the
loop**. Without `--feelimit`, a price spike mid-retry registers anyway — that is how
one UID cost 0.1302934τ against a 0.1016τ limit.

`creg79.py` parses with `parse_known_args()`, so a dropped or misspelled flag fails
**silently**. There is no error to notice; you just overpay.

### Port bands

`X` is a wildcard digit: `79XX` = 7900–7999 (100 ports), `809X` = 8090–8099 (10).
Comma-separate, no spaces, to spill into a later band once the first fills:
`"809X,808X"`. Allocation never leaves the listed bands. Each server gets its own
band so miners on different boxes cannot collide.

### Root

`creg79.py` requires root. The template runs it directly when `id -u` is 0, else via
`sudo`, always with the venv python (never bare `python3`).
