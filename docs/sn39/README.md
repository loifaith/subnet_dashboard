# Subnet 39 — Cathedral

A **standalone CLI script**, not part of the dashboard. No endpoint, no UI, no
background worker imports it.

[39Miners/cathedral_status.py](../../39Miners/cathedral_status.py)

## What it does

Pulls the public Cathedral leaderboard (`https://cathedral.computer`) and reports
the stats for your miners.

The interesting wrinkle: the Cathedral API keys miners by **hotkey** — no coldkey,
and `uid` is usually `null`. So "mine" has to be resolved indirectly:

1. hotkeys you list explicitly, and/or
2. coldkeys you list, **expanded to hotkeys via the SN39 metagraph**.

## Identifying your miners

First source found wins:

| Source | Format |
|---|---|
| `CATHEDRAL_MY_HOTKEYS` | comma-separated ss58 hotkeys |
| `CATHEDRAL_MY_COLDKEYS` | comma-separated ss58 coldkeys |
| `39Miners/my_miners.json` | `{"hotkeys": [...], "coldkeys": [...]}` |

## Usage

```bash
.venv/bin/python 39Miners/cathedral_status.py            # my miners, summary table
.venv/bin/python 39Miners/cathedral_status.py --uid      # also resolve on-chain uid (loads the SN39 metagraph — slow)
.venv/bin/python 39Miners/cathedral_status.py --all      # full leaderboard (top 200)
```

`--uid` is opt-in precisely because it costs a metagraph load. Run it from the repo
venv; it loads `bittensor` and so should never be imported into the web process
([../sn79/performance/chain_subprocess.md](../sn79/performance/chain_subprocess.md)).

## If you ever wire it into the dashboard

Follow the SN88 pattern rather than the SN79 one: a scheduled subprocess writing a
daily cache file, and a read-only endpoint serving that file. Don't call it
in-process — the `--uid` path loads a metagraph.
