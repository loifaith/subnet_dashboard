# The `/miners` page

One HTML file, [templates/miners.html](../../../templates/miners.html), served at both
`/` and `/miners`. Vanilla JS, Bootstrap 5, Chart.js, no build step.

## Feature docs

| Doc | Covers |
|---|---|
| [metagraph_view.md](metagraph_view.md) | the base data load, stat cards, incentive chart |
| [miner_table.md](miner_table.md) | the grid: columns, sorting, colouring, actions |
| [miner_params.md](miner_params.md) | editing one miner's agent + params, push, restart |
| [agent_bulk_edit.md](agent_bulk_edit.md) | changing many miners (and the defaults) at once |
| [uid_status.md](uid_status.md) | live per-UID status vs. the cached daily metrics |
| [miner_history.md](miner_history.md) | performance history and agent-switch history |
| [fee_regime.md](fee_regime.md) | maker/taker fee regime and its history |
| [reg_cost.md](reg_cost.md) | current and historical registration price |
| [alpha_price.md](alpha_price.md) | the header price sparkline |
| [sim_id.md](sim_id.md) | simulation epoch display + rotation alerting |
| [coldkeys.md](coldkeys.md) | newly-seen coldkeys and IPs |

Write actions that spend or move TAO live in their own sections:
[../regbot/](../regbot/) and [../unstake/](../unstake/).

## Page layout

```
┌ top bar ─────────────────────────────────────────────────────────┐
│ subnet input · reg cost · sim id · alpha price + range buttons    │
│ theme toggle · logout · Refresh · Add Miner · Unstake & Transfer  │
│ Multi-Server Register · Multi-Server Unstake · Coldkeys [badge]   │
├ active bars (hidden unless something is running) ─────────────────┤
│ regbotActiveBar · batchActiveBar · unstakeActiveBar               │
├ stats row ───────────────────────────────────────────────────────┤
│ Total · Avg Inc · Top Inc · Immune · My Miners · TAO/Day · /Epoch │
├ register-my-coldkeys card ───────────────────────────────────────┤
├ incentive distribution chart ────────────────────────────────────┤
├ fee regime panel (SN79 only) ────────────────────────────────────┤
└ miner table ─────────────────────────────────────────────────────┘
```

Modals: miner params, UID status, miner history, add miner (+confirm, +kill
confirm), unstake (+confirm), multi-register (+confirm, +cancel confirm),
multi-unstake (+confirm), coldkeys.

## Client-side conventions

- **Theme** is stored in `localStorage["sn88-dashboard-theme"]` with a separate
  `-user-set` flag, so an explicit choice survives while an unset user still follows
  the system preference. An inline IIFE in `<head>` applies it before first paint to
  avoid a flash.
- **Registered coldkeys** live in `localStorage["registeredColdkeys"]`, seeded from
  `DEFAULT_COLDKEYS`. They are browser state, not server state — a different browser
  sees a different "My Miners".
- **All `fetch` is wrapped once** to redirect to `/login` on a 401
  ([../../architecture/auth.md](../../architecture/auth.md)).
- Confirm dialogs for anything destructive restate server, wallet, destination and
  amount — the server validates again regardless.
