# subnet_dashboard — documentation

Operator dashboard for a Bittensor mining fleet. It is not a viewer: it registers
hotkeys, edits live miner params, restarts miners over SSH, and unstakes/transfers
real TAO. Primary subnet is **79** (TAOS simulated trading); **88**, **71** and **39**
are secondary or legacy.

## How this tree is organised

```
docs/
├── architecture/          cross-cutting: processes, auth, disk layout, legacy code
├── sn79/                  the live subnet — most of the codebase
│   ├── miners/            features of the /miners page
│   ├── regbot/            registration (spends TAO)
│   ├── unstake/           unstake + transfer (moves TAO)
│   ├── status_server/     the standalone :9800 polling service
│   ├── automation/        background workers and auto-switching
│   ├── performance/       why things are cached / out-of-process / multiplexed
│   └── tricks/            non-obvious techniques and the reasoning behind them
├── sn88/                  Investing88 view
├── sn71/                  legacy LeadPoet session manager (dormant)
└── sn39/                  Cathedral status script (standalone CLI)
```

## Start here

| If you want to… | Read |
|---|---|
| Understand the whole system in 5 minutes | [architecture/overview.md](architecture/overview.md) |
| Deploy / restart / find a log | [architecture/processes_and_ports.md](architecture/processes_and_ports.md) |
| Know what every directory on disk holds | [architecture/data_layout.md](architecture/data_layout.md) |
| Look up an endpoint | [api_reference.md](api_reference.md) |
| Understand the SN79 domain model | [sn79/README.md](sn79/README.md) |
| Add a feature to the dashboard page | [sn79/miners/](sn79/miners/) |
| Touch anything that spends TAO | [sn79/regbot/overview.md](sn79/regbot/overview.md), [sn79/unstake/overview.md](sn79/unstake/overview.md) |
| Avoid re-introducing a solved latency bug | [sn79/performance/](sn79/performance/) |
| See what a page load costs and why | [sn79/performance/page_load_audit.md](sn79/performance/page_load_audit.md) |
| Know what's currently broken | [sn79/tricks/known_issues.md](sn79/tricks/known_issues.md) |

## Ground rules this codebase follows

1. **Bittensor never loads inside the web process.** Every chain query runs as a
   child process. See [sn79/performance/chain_subprocess.md](sn79/performance/chain_subprocess.md).
2. **A browser request never waits on an upstream.** Chain queries and SSH fan-outs
   are owned by background workers; the request path does a memory lookup. See
   [sn79/performance/cache.md](sn79/performance/cache.md) §6.
3. **Anything that spends or moves TAO writes an append-only audit log** before
   or after the fact, and is validated twice (client and server).
4. **Every file write that a reader might race is atomic** (`tmp` + `os.replace`).
5. **State that outlives a request is persisted**, so a `pm2 restart` never
   silently drops in-flight work.
6. **Stale-but-real beats fresh-but-empty**: upstream misses carry forward the last
   good value rather than rendering zeros.
