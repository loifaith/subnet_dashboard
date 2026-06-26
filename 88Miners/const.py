"""Investing88 (SN88) shared constants."""

import os

API_BASE = "http://api.investing88.ai"
API_BASE_HTTPS = "https://api.investing88.ai"

# Dashboard (miner detail UI + per-uid endpoints)
DASHBOARD_BASE = "https://db.investing88.ai"
DASHBOARD_API_BASE = f"{DASHBOARD_BASE}/api"

NETUID = 88

# Local daily cache (synced once per day at 19:00 Asia/Tokyo)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(PROJECT_ROOT, "investing88_cache")
SYNC_HOUR = int(os.getenv("INVESTING88_SYNC_HOUR", "19"))
SYNC_TZ = os.getenv("INVESTING88_SYNC_TZ", "Asia/Tokyo")
