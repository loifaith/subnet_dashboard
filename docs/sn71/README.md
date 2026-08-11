# Subnet 71 — legacy LeadPoet session manager

**Dormant.** This is what the repository was before it became a mining dashboard.
It still runs as a PM2 process; nothing in the live path talks to it.

Full inventory of what is dead and what is live:
[../architecture/legacy_sn71_layer.md](../architecture/legacy_sn71_layer.md).

## What it is

A FastAPI + PostgreSQL CRUD service on port **9900** for "sessions" and "processes",
plus lead-pipeline counts, plus an API-credit checker for OpenRouter and ScrapingDog.

```
backend_api.py    :9900   ~30 endpoints over psycopg + psycopg_pool (2–10 connections)
db_utils.py               HTTP client for the above
process.py                helpers
templates/*.html          dashboard, index, add, edit, process*, api_credit
README_API.md             its original documentation
```

The connection string is **hard-coded** at
[backend_api.py:52](../../backend_api.py#L52):

```python
conninfo="dbname=mydb user=myuser password=strongpassword host=localhost port=5432"
```

CORS is `allow_origins=["*"]` and there is **no authentication** — the dashboard's
auth middleware only wraps `app.py`, not this. Port 9900 should not be exposed
beyond localhost.

## The one thing that isn't dead

Viewing subnet 71 in the dashboard works, because `/api/metagraph-data` is
netuid-agnostic: type `71` into the subnet input and you get the metagraph view with
no SN79 or SN88 panels. That path doesn't touch `backend_api.py` at all.

## Retiring it

1. Remove the `backend_api` app from [ecosystem.config.json](../../ecosystem.config.json).
2. Delete `backend_api.py`, `db_utils.py`, `process.py`, `README_API.md`, and the
   seven unused templates.
3. Drop `psycopg[binary]` and `psycopg_pool` from [requirements.txt](../../requirements.txt).
4. `.env` / `.env.example` can go too — only `backend_api.py` calls `load_dotenv()`.

Nothing in `app.py`, `79Miners/` or `88Miners/` imports any of it. Back up the
Postgres database first if the data still matters to anyone.
