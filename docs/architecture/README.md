# Architecture

Cross-cutting concerns — everything that isn't specific to one subnet.

| Doc | Covers |
|---|---|
| [overview.md](overview.md) | the three processes, request flow, where the work happens |
| [processes_and_ports.md](processes_and_ports.md) | PM2 apps, ports, env vars, external path dependencies |
| [auth.md](auth.md) | login, signed cookie, the middleware, the loopback bypass |
| [data_layout.md](data_layout.md) | every directory on disk and its format |
| [legacy_sn71_layer.md](legacy_sn71_layer.md) | which files are dead and safe to ignore |

Also useful: [../api_reference.md](../api_reference.md) — every HTTP endpoint in one
table.

## Three things to know before changing anything

1. **`app.py` is the whole web app.** `backend_api.py` is legacy and unused.
2. **`bittensor` must never be imported into the uvicorn process.** Chain work runs
   as a child process. [../sn79/performance/chain_subprocess.md](../sn79/performance/chain_subprocess.md)
3. **Domain modules are imported inside endpoint functions**, not at module top
   level, so a broken module degrades one route instead of failing startup. Keep
   that pattern.
