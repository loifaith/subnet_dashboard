# The legacy SN71 layer

This repo began as **"SN71 Session Manager"** for an unrelated project (LeadPoet).
That code is still present, still deployed as a PM2 process, and is **not used by
the dashboard**. Knowing which files are dead saves a lot of reading.

## Dead files

| File | What it was | Live? |
|---|---|---|
| [backend_api.py](../../backend_api.py) (1034 lines) | FastAPI + PostgreSQL CRUD on `:9900` | runs under PM2, nothing calls it |
| [db_utils.py](../../db_utils.py) | HTTP helpers pointing at `localhost:9900` | not imported by `app.py` |
| [process.py](../../process.py) | process-table helpers | unused |
| [README_API.md](../../README_API.md) | docs for the above | describes a different app |
| `templates/dashboard.html` | SN71 counts dashboard | never rendered |
| `templates/index.html`, `add.html`, `edit.html` | SN71 session CRUD | never rendered |
| `templates/process*.html` | SN71 process CRUD | never rendered |
| `templates/api_credit.html` | OpenRouter / ScrapingDog credit view | never rendered |
| [run_app.sh](../../run_app.sh) | start script | `cd`s to `/work/jnh/leadpoet_manage` |
| [check_metagraph.py](../../check_metagraph.py), [test_immune.py](../../test_immune.py) | one-off scripts | standalone |
| `.env` / `.env.example` | `OPENROUTER_API_KEY`, `SCRAPINGDOG_API_KEY` | only `backend_api.py` loads it |

Verified: `app.py` renders exactly two templates —
`login.html` and `miners.html` ([app.py:673](../../app.py#L673)).

## Live files

`app.py`, `auth.py`, `manage_users.py`, `load_metagraph.py`, `templates/login.html`,
`templates/miners.html`, all of `79Miners/` and `88Miners/`.

`load_metagraph.py` is shared: it is the child process behind `/api/metagraph-data`
for *any* netuid, including 79 and 88.

## Leftover naming

- `app = FastAPI(title="SN71 Session Manager", ...)` — the FastAPI title at
  [app.py:673](../../app.py#L673) still says SN71.
- `_parse_netuid(raw, default=71)` — the server-side default netuid is 71, but the
  UI always sends an explicit `netuid` (default 79), so this default is never hit
  in practice.
- `templates/miners.html` `<title>` says "SN88"; the page is subnet-agnostic.
- The page's JS now lives in `asset/miners.js`; `miners.html` is markup and CSS only.

## If you want to remove it

Removing `backend_api` from `ecosystem.config.json` and deleting the dead templates
is safe — nothing in the live path imports them. `psycopg`/`psycopg_pool` could then
come out of `requirements.txt` too. Left in place so far because it costs nothing
running and the Postgres data may still matter to someone.
