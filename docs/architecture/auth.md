# Authentication

Stdlib-only shared login. No sessions table, no external dependency —
[auth.py](../../auth.py) is 105 lines of `hmac` + `hashlib`.

## Pieces

| Piece | Where | Notes |
|---|---|---|
| User allowlist | `auth_users.json` | `{username: "salt$pbkdf2_hex"}`, chmod 600, gitignored |
| Signing key | `.dashboard_secret_key` | 32 random bytes hex, auto-created on first import, chmod 600, gitignored |
| Cookie | `dashboard_session` | `base64url(username:expiry).hmac_sha256` |
| Gate | `auth_gate` middleware, [app.py:702](../../app.py#L702) | wraps every request |

Passwords are PBKDF2-HMAC-SHA256, **260,000 iterations**, per-user 16-byte salt.
Verification uses `hmac.compare_digest`. Sessions default to 30 days
(`DASHBOARD_SESSION_SECONDS`).

## The middleware

```
/login, /logout, /asset/*        → pass through
client IP is 127.0.0.1 or ::1    → pass through as user "localhost"
valid cookie                     → pass through
/api/* without a cookie          → 401 JSON
anything else                    → 303 to /login?next=<path>
```

The **loopback bypass** ([app.py:698](../../app.py#L698)) lets `curl` and scripts on
the dashboard host hit the API without a cookie. It trusts `request.client.host`,
which is the real TCP peer only because port 12800 is exposed directly with no
reverse proxy. **If you ever put nginx in front of this, that check becomes a
spoofable auth bypass** — every request would arrive from 127.0.0.1. The in-code
comment says the same thing; treat it as a deployment invariant.

The API/HTML split matters: a page navigation gets a redirect, but a background
`fetch()` gets a 401 it can detect. The frontend wraps `window.fetch` once
([miners.js:8](../../asset/miners.js#L8)) and bounces to `/login` on any
401, so an expired session mid-session doesn't leave silently-dead panels.

`_safe_next_path()` ([app.py:691](../../app.py#L691)) only accepts same-site relative
paths for the post-login redirect — this is the open-redirect guard.

## Managing users

```bash
.venv/bin/python manage_users.py list
.venv/bin/python manage_users.py add <username>      # prompts for password
.venv/bin/python manage_users.py remove <username>
```

Editing `auth_users.json` by hand means hashing the password yourself; use the
script.

## Threat model, stated plainly

This is a **shared operator login on a single trusted box**, not a multi-tenant
system:

- No per-user authorisation — any logged-in user can spend TAO.
- The cookie is `httponly=True, samesite="lax"`
  ([app.py:751](../../app.py#L751)). There is no CSRF token, so `lax` is what stops
  cross-site POSTs; keep it if you add state-changing form posts.
- The session payload carries no server-side revocation list. Rotating
  `.dashboard_secret_key` is the only way to invalidate outstanding sessions, and
  it invalidates all of them at once.
- The loopback bypass above means anything that can run code on the host is
  already fully authorised.
- The audit logs ([sn79/regbot](../sn79/regbot/), [sn79/unstake](../sn79/unstake/))
  record `triggered_by`, but that field is `"dashboard"` or `"batch:<id>"` — it
  does **not** carry the logged-in username. If per-user attribution ever matters,
  that is the change to make.
