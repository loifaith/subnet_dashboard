# Bulk agent edit

`PUT /api/agent-bulk` — [app.py:1792](../../../app.py#L1792)

Apply **one partial params change** to many miners at once, optionally switching
them all to a different sub-agent, and optionally updating that agent's canonical
defaults.

## Two modes

The body determines which:

### 1. Agent-scoped (no `uids`)

```json
{ "target_agent": "GammaPrimeAgent", "agent_index": 43,
  "set": {"max_quantity": 2.0}, "remove": ["eat_quantity"], "sync": true }
```

- **Selects** every miner whose *active* agent name equals `target_agent`.
- **Also rewrites** the canonical defaults in `AGENT_DEFAULTS_FILE`
  (`dispatch_example.json`) for that agent, so newly-registered miners inherit
  the change.

### 2. UID-scoped (`uids` present)

```json
{ "uids": [12, 34, 56], "set": {"taker_ratio": 0.4}, "sync": true }
```

- Applies to exactly those miners regardless of which agent each runs.
- **Never touches the defaults** — a subset edit must not move the fleet baseline.
  This is the key asymmetry between the two modes.

## Merge, not replace

`_merge_params()` ([app.py:1174](../../../app.py#L1174)) drops `remove` keys, sets
`set` keys (type-coerced when the key already exists), and leaves every other param
on the miner untouched. That's the opposite of the single-miner PUT
([miner_params.md](miner_params.md)), and it's why this endpoint is safe to use
against miners with divergent tuning.

## Switching agents while editing

`agent_index` is optional. Omitted, each miner keeps its current agent and only its
params change. Set to a different `agent_id`, every selected miner switches — and the
merged params are written onto the **newly active** agent's entry, not the old one.

## Execution

All miners are processed with `asyncio.gather(..., return_exceptions=True)`, so one
box being unreachable degrades to a per-UID `{"ok": false, "error": ...}` row instead
of failing the batch. Each miner gets its own SSH push over the shared warm master.

Response summarises `count`, `updated`, `synced`, `defaults_updated`, plus a
per-miner list.

## Defaults resolution

When agent-scoped and no explicit `agent_index` is given, the endpoint looks up the
target agent's own `agent_id` by scanning `dispatch_example.json` by name
([app.py:1467](../../../app.py#L1467)). If that lookup fails,
`defaults_updated` comes back `false` and only the miners are changed — the miners
are never left un-updated because the defaults file was unreadable.

## History

Each affected miner gets an `agent_history` entry with
`extra = {"params": set, "removed": remove}` and `source` of `manual` (switched) or
`params_update` (same agent).
