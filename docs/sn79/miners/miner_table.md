# The miner table

The main grid. Everything else on the page feeds it.

`renderTable()` — [miners.html](../../../templates/miners.html) ~line 8013.

## Where each column's data comes from

| Column group | Source |
|---|---|
| UID, hotkey, coldkey, incentive, emission, immune, age | `/api/metagraph-data` |
| Agent name + params summary, Server | `/api/miner-params` (owned miners only) |
| kappa, PnL, volume, book counts | `/api/uid-metrics` (daily status file) |
| Reg date, reg cost | `/api/uid-metrics` (registration fields merged in during the refresh) |
| SN88 score columns | `/api/investing88/days` (only when subnet = 88) |

`updateTableColumns()` / `getTableColspan()` add and remove column sets as the
subnet changes, so the same table markup serves 79, 88 and "any other netuid".

## Owned-miner decoration

- **Agent label** (`renderAgentLabel`) is colour-coded by *side* — `getAgentSide()`
  classifies an agent name as maker/taker/other, and `getAgentColor()` hashes the
  name into a stable hue within that side's palette. Same agent → same colour across
  reloads and across light/dark themes (`hslToRgba` handles the alpha fills).
- **Server cell** (`renderServerCell`) resolves `miner_ip` → `ServerN` through the
  client-side `SERVER_NICKNAMES` copy, falling back to the raw IP.
- **Coldkey cell** uses `getColdkeyNickname` / `shortColdkey` and a per-coldkey
  colour, so sibling miners are visually grouped.

## Registration date handling

Reg dates are rendered in **Asia/Tokyo** (`formatInstantInTokyo`,
`isTokyoRegDateIso`, `splitRegDateString`). `getRegDateSortValue()` exists
separately from the display formatter so sorting uses the underlying instant, not
the formatted string.

## Sorting, filtering, paging

- `sortTable(col)` → `getSortValue(m, column)`, with `getMetricSortValue(uid, key)`
  for metric columns so a missing metric sorts consistently instead of as `NaN`.
- `filterTable()` applies the search box **and** the coldkey view filters
  (`applyColdkeyViewFilters`, `applyNewColdkeyFilter`).
- `changePage(p)` paginates the filtered set.
- `exportCSV()` exports the current filtered view, not the whole metagraph.

## Row actions

Each owned row opens the modals documented elsewhere:

| Action | Doc |
|---|---|
| Params | [miner_params.md](miner_params.md) |
| Status | [uid_status.md](uid_status.md) |
| History | [miner_history.md](miner_history.md) |

## Refresh cadence

| What | Interval |
|---|---|
| Full `loadData()` | 5 min |
| RegBot active bar | 20 s |
| Batch active bar | 15 s |
| Unstake active bar | 20 s |
| An open run-log modal | 4 s |

Only the active-run bars poll frequently, and each is a single small JSON request.
The expensive full reload stays at 5 minutes.
