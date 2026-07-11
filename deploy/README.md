# deploy/ — putting FDM Platform behind a reverse proxy

`install.sh` runs uvicorn directly on `FDM_PORT` (default 8000) with **no
proxy-header trust**: the login lockout and audit source IP key on the real
socket peer address, and any client-sent `X-Forwarded-For` is ignored. That is
the safe default for a LAN install.

For an internet-facing install, terminate TLS in nginx and tell the app to
trust exactly that one proxy:

1. **nginx** — copy `deploy/nginx.conf` to `/etc/nginx/conf.d/fdm.conf`, set
   `server_name`, run certbot for TLS, `nginx -t && systemctl reload nginx`.
   The config **sets** `X-Forwarded-For` from `$remote_addr` (never appends a
   client-supplied chain). When the platform manages TLS for a bench site it
   drives certbot through the fixed root-owned wrapper `deploy/fdm-certbot`
   (never a raw `certbot certonly *` sudoers grant — see DOO-220 and
   `docs/production-setup.md`).

2. **App** — in `backend/.env` (or `FDM_TRUSTED_PROXY_IPS` on a fresh
   `install.sh` run):

   ```
   TRUSTED_PROXY_IPS=127.0.0.1
   COOKIE_SECURE=true
   ```

   With `TRUSTED_PROXY_IPS` set, the app enables uvicorn's
   `ProxyHeadersMiddleware` restricted to those addresses — the equivalent of
   `--proxy-headers --forwarded-allow-ips=127.0.0.1`. Never set it to `*` on a
   reachable service. Empty (default) disables proxy-header handling entirely.

3. **Close the side door** — edit `/etc/systemd/system/fdm-api.service` and
   change `--host 0.0.0.0` to `--host 127.0.0.1` so nothing can reach uvicorn
   around the proxy (bypassing TLS and the header-scrubbing hop). Then
   `systemctl daemon-reload && systemctl restart fdm-api`.

## Why this matters (SEC-M1)

Login lockout is keyed `(email, client IP)` plus a per-email cross-IP
backstop (`LOGIN_EMAIL_FAILURE_LIMIT` failures per
`LOGIN_EMAIL_FAILURE_WINDOW_SECONDS` locks the account regardless of source
IP). Behind a proxy **without** this configuration, every client would share
the proxy's IP — one attacker could lock any account platform-wide. With a
**permissive** forwarded-IP trust, an attacker could rotate fake
`X-Forwarded-For` values to dodge the pair lock (the email backstop still
catches that). Trusting exactly the one proxy, which itself overwrites the
header, closes both holes.

## Manual verification

From an untrusted host (not the proxy), a spoofed header must not change the
throttle key — six failures lock you out even while rotating the header:

```bash
for i in 1 2 3 4 5 6 7; do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "X-Forwarded-For: 198.51.100.$i" \
    -H 'Content-Type: application/json' \
    -d '{"email":"admin@example.com","password":"wrong"}' \
    https://fdm.example.com/api/auth/login
done
# expected: 401 ×6, then 429 — the rotating header did not reset the lock
```

The automated equivalents live in `backend/tests/test_auth_proxy_lockout.py`.
