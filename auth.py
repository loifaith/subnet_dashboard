"""Shared-login auth for the dashboard: a small allowlist of users (auth_users.json,
gitignored) and a signed session cookie. Stdlib-only (hmac/pbkdf2) so no new deps.

Manage users with manage_users.py (add/remove/list) rather than editing
auth_users.json by hand.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Optional

DASHBOARD_ROOT = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.getenv("DASHBOARD_USERS_FILE", os.path.join(DASHBOARD_ROOT, "auth_users.json"))
SECRET_KEY_FILE = os.getenv(
    "DASHBOARD_SECRET_KEY_FILE", os.path.join(DASHBOARD_ROOT, ".dashboard_secret_key")
)
SESSION_COOKIE_NAME = "dashboard_session"
SESSION_SECONDS = int(os.getenv("DASHBOARD_SESSION_SECONDS", str(30 * 24 * 3600)))
PBKDF2_ITERATIONS = 260_000


def _load_or_create_secret_key() -> bytes:
    env_key = os.getenv("DASHBOARD_SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().encode("utf-8")
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    os.chmod(SECRET_KEY_FILE, 0o600)
    return key.encode("utf-8")


_SECRET_KEY = _load_or_create_secret_key()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    )
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hex_digest = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(digest.hex(), hex_digest)


def load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users: dict) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, sort_keys=True)
    os.chmod(USERS_FILE, 0o600)


def authenticate(username: str, password: str) -> bool:
    stored = load_users().get(username)
    if not stored:
        return False
    return verify_password(password, stored)


def create_session_cookie(username: str) -> str:
    expires_at = int(time.time()) + SESSION_SECONDS
    payload = f"{username}:{expires_at}"
    payload_b64 = urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8").rstrip("=")
    signature = hmac.new(_SECRET_KEY, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_session_cookie(cookie_value: Optional[str]) -> Optional[str]:
    if not cookie_value or "." not in cookie_value:
        return None
    payload_b64, signature = cookie_value.rsplit(".", 1)
    expected = hmac.new(_SECRET_KEY, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        padding = "=" * (-len(payload_b64) % 4)
        payload = urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        username, expires_at = payload.rsplit(":", 1)
        if int(expires_at) < int(time.time()):
            return None
        return username
    except Exception:
        return None
