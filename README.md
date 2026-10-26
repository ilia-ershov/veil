<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
  <img src="docs/logo.svg" width="72" height="72" alt="">
</picture>

# Veil

**A separate random email address for every website, forwarded to your real inbox and switched off in one click.**

*Организатор почты* · the interface is in Russian

[![CI](https://github.com/ilia-ershov/veil/actions/workflows/ci.yml/badge.svg)](https://github.com/ilia-ershov/veil/actions/workflows/ci.yml)
![Python 3.13](https://img.shields.io/badge/Python-3.13-3776ab?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-000000?logo=flask&logoColor=white)
![React 19](https://img.shields.io/badge/React-19-149eca?logo=react&logoColor=white)
![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17-4169e1?logo=postgresql&logoColor=white)
![Postfix](https://img.shields.io/badge/mail-Postfix-555555)

[**Live: veil.ershov.me**](https://veil.ershov.me) · [Deploy your own](deploy/README.md) · [API](#api)

<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/home-dark.png">
  <img src="docs/screenshots/home.png" alt="Home page: a headline and an example list of forwarding addresses" width="900">
</picture>

</div>

## Why

You sign up somewhere with `x7k2m9q4ab@w3r4kl.ru` instead of your real address. Mail sent to it lands in your inbox as usual. When that site leaks its database or starts sending spam, you switch that one address off and nothing else changes.

- Your real address is never handed to third parties.
- A leaked database can't be cross-referenced to find out where else you are registered.
- Phishing aimed at an address that belongs to one shop is obviously out of place.
- Changing your real inbox means updating one setting, not every website.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/dashboard-dark.png">
  <img src="docs/screenshots/dashboard.png" alt="List of addresses with service names, links and actions" width="100%">
</picture>

| Create an address | Two-factor setup | Mobile |
| --- | --- | --- |
| ![Create](docs/screenshots/create.png) | ![2FA](docs/screenshots/two-factor.png) | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/mobile-dark.png"><img src="docs/screenshots/mobile.png" alt="Mobile"></picture> |

## Features

- **Random aliases**: one per service, labelled with the service name and link. You can edit, search and copy them.
- **Instant deactivation and reactivation.** A deactivated alias stops accepting mail at once and is never given to another user.
- **One main inbox**: change it once (after verifying the new address) and every alias follows.
- **Accounts with email-verified signup**, password reset, password change and "sign out everywhere".
- **TOTP two-factor authentication** (Google Authenticator, Aegis, Bitwarden…) with one-time recovery codes.
- **Security notifications** when the main address, password or 2FA changes, or the account is deleted.
- **Help centre** with practical guidance on phishing, passwords and what to do after a compromise.
- **Mail forwarding through Postfix**, which reads active aliases straight from the database with a read-only role.

## How it works

```mermaid
flowchart LR
    B["Browser<br/>React SPA"] -->|HTTPS| N["nginx<br/>static files, /api proxy"]
    N -->|/api| A["Flask API<br/>gunicorn"]
    A --> P[("PostgreSQL")]
    A --> R[("Redis<br/>rate limits")]
    A -->|SMTP| M["Mail server<br/>codes, notifications"]
    S["Any sender"] -->|SMTP| X["Postfix"]
    X -->|"alias lookup<br/>(read-only role)"| P
    X -->|forward| I["User's real inbox"]
```

Postfix resolves `alias@MAIL_DOMAIN` to the owner's main address with one SQL query, so deactivating an alias rejects its mail immediately, with no map rebuild or reload.

**Stack:** Python 3.13, Flask 3.1, SQLAlchemy 2, PostgreSQL 17, PyJWT, bcrypt, pyotp, Flask-Limiter + Redis, gunicorn · React 19, React Router 7, Vite 8 · Docker Compose, nginx, Postfix · pytest, Playwright, ruff, ESLint, GitHub Actions.

## Security design

| Threat | Controls |
| --- | --- |
| Stolen or guessed passwords | bcrypt (cost 12) • password policy enforced by the server • login and 2FA rate limits per IP and per account that count only failures • optional TOTP 2FA with replay protection • recovery codes stored as HMACs |
| Brute-forcing email codes | 6-digit codes from `secrets`, stored only as HMACs • 5 attempts per code • 10-minute lifetime • 60 s resend cooldown • per-IP limits • one code per address and purpose, so a stranger's request cannot void the owner's code • a reset token sets a password only once |
| Session theft | 4-hour JWTs • every token carries its type, so a verification or 2FA token can never act as a session • server-side revocation (`token_version`) on password change/reset and "sign out everywhere" • password re-entry for email change, 2FA removal and account deletion |
| Account enumeration | Identical answers for registered and unknown addresses • constant-work password check • every code request sends a message (a notice instead of a code where there is nothing to confirm), so response times match |
| Alias abuse | Addresses are generated by the server and handed out as signed, user-bound, expiring tokens • unique constraint • per-user limit • deleted addresses are retired and never handed out again |
| XSS and injection | React escaping • main addresses limited to letters, digits and RFC 5322 symbols, so Postfix and smtplib never split one into several recipients • strict Content-Security-Policy without inline scripts • SQLAlchemy parameters • service links limited to http(s) without credentials |
| Information leaks | JSON errors without internals • no secrets in logs • no third-party requests (fonts are bundled and self-hosted; the 2FA QR code is rendered in the browser) |
| Misconfiguration | Refuses to start without a strong `SECRET_KEY` • debug mode off by default • CORS off by default • non-root containers • Postfix role limited to 5 columns |

## Quick start

```bash
cp .env.example .env
# set SECRET_KEY and POSTGRES_PASSWORD, e.g. python3 -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose up -d --build
```

Open http://localhost:8080. While `MAIL_BACKEND=console`, verification codes appear in `docker compose logs -f backend`. For real delivery, set `MAIL_BACKEND=smtp` and the `SMTP_*` variables.

The stack listens on `127.0.0.1:8080`. In production, put a TLS-terminating reverse proxy in front of it (Caddy, nginx + certbot, …), set `TRUST_PROXY=2` so rate limits see real client IPs, and enable the HSTS line in `project/front/nginx/security-headers.conf`.

**A complete server** (HTTPS, mail server with forwarding, SRS, DKIM, spam filtering, daily backups, hardening): [`deploy/README.md`](deploy/README.md). `deploy/server-setup.sh` sets up a fresh Debian 13 server; it reproduces the setup behind [veil.ershov.me](https://veil.ershov.me).

## Development

<details>
<summary><b>Run the backend and frontend locally</b></summary>

**Backend** (needs PostgreSQL):

```bash
sudo -u postgres psql -c "CREATE ROLE mail_organizer LOGIN PASSWORD 'secret'" \
                      -c "CREATE DATABASE mail_organizer OWNER mail_organizer"
cd project
python3 -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # set SECRET_KEY and DATABASE_URL
python app.py                 # API on http://127.0.0.1:5000/api
```

**Frontend:**

```bash
cd project/front
npm ci
npm run dev                   # http://localhost:5173, /api is proxied to the backend
```

</details>

<details>
<summary><b>Configuration</b></summary>

Backend settings come from environment variables or `project/.env` (see [`project/.env.example`](project/.env.example)).

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | — (required) | Signs tokens and HMACs; at least 32 random characters |
| `DATABASE_URL` | — (required) | `postgresql://user:password@host/db` |
| `MAIL_DOMAIN` | `w3r4kl.ru` | Domain of the forwarding addresses |
| `MAIL_BACKEND` | `console` | `console` (log messages) or `smtp` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `SMTP_SSL`, `MAIL_FROM` | | Outgoing mail |
| `RATELIMIT_STORAGE_URI` | `memory://` | Use `redis://…` when running several workers |
| `TRUST_PROXY` | `0` | Number of reverse proxies in front of the API |
| `CORS_ORIGINS` | empty (off) | Only needed if the SPA is served from another origin |
| `ACCESS_TOKEN_TTL_MINUTES` | `240` | Session lifetime |
| `MAX_ALIASES_PER_USER` | `100` | Limit per account, including deactivated ones |
| `AUTO_INIT_DB` | `true` | Create/upgrade the schema on start (`flask --app app init-db` does it explicitly) |

The frontend reads `VITE_MAIL_DOMAIN` and, for split deployments only, `VITE_API_URL` at build time (see [`project/front/.env.example`](project/front/.env.example)).

</details>

<details>
<summary><b>Mail forwarding with Postfix (manual setup)</b></summary>

`deploy/server-setup.sh` does all of this; these are the steps for an existing mail server.

1. Point the MX record of the alias domain at the mail server and install `postfix` and `postfix-pgsql`.
2. Create the read-only lookup role: `psql -d mail_organizer -f deploy/postfix/lookup-role.sql` (change the password first).
3. Copy [`deploy/postfix/pgsql-aliases.cf`](deploy/postfix/pgsql-aliases.cf) to `/etc/postfix/` and add the lines from [`deploy/postfix/main.cf.example`](deploy/postfix/main.cf.example) to `main.cf`.
4. Check the lookup with `postmap -q abc123def4@w3r4kl.ru pgsql:/etc/postfix/pgsql-aliases.cf`, then `postfix reload`.

Forwarded mail keeps the original sender, so use SRS (for example `postsrsd`) to keep it from failing SPF at the destination.

</details>

<details>
<summary><b>Upgrading an installation of the first version</b></summary>

The first version stored aliases as JSON arrays in the `emails` and `deactivated` tables. The migration copies them into the new `aliases` table in a single transaction. It lower-cases account emails and reports duplicates and rows it cannot read.

```bash
pg_dump mail_organizer > backup.sql
cd project
flask --app app migrate-legacy                 # copy and report; safe to re-run
flask --app app migrate-legacy --drop-legacy   # copy, then drop the old tables
```

`SECRET_KEY` must now be at least 32 characters, and everyone has to sign in again once.

</details>

## API

All endpoints live under `/api`. They accept and return JSON, and errors look like `{"error": "...", "code": "machine_readable_code"}`. Authenticated endpoints expect `Authorization: Bearer <token>`.

<details>
<summary><b>Endpoints</b></summary>

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| POST | `/auth/request-code` | | Send a 6-digit code; `purpose` = `register`, `change_email` or `reset_password` |
| POST | `/auth/verify-code` | | Exchange the code (`email`, `code`, `purpose`) for a 15-minute verification token |
| POST | `/auth/register` | | Create an account (`name`, `email`, `password`, `verification_token`) |
| POST | `/auth/login` | | Password step; returns a session token or `mfa_required` + `mfa_token` |
| POST | `/auth/login/2fa` | | Second step with a TOTP or recovery code |
| POST | `/auth/reset-password` | | Set a new password with a verification token |
| GET | `/account` | ✓ | Profile |
| POST | `/account/email` | ✓ | Change the main address (verification token + password) |
| POST | `/account/password` | ✓ | Change the password; other sessions are revoked |
| POST | `/account/logout-all` | ✓ | Revoke every session |
| DELETE | `/account` | ✓ | Delete the account (password) |
| POST | `/account/2fa/setup` · `/enable` · `/disable` | ✓ | Manage TOTP |
| POST | `/aliases/generate` | ✓ | A new random address as a signed token |
| GET | `/aliases?status=all\|active\|inactive` | ✓ | List, newest first |
| POST | `/aliases` | ✓ | Create from the token with a service name and link |
| PATCH | `/aliases/{id}` | ✓ | Edit the service name or link |
| POST | `/aliases/{id}/deactivate` · `/activate` | ✓ | Stop or resume forwarding |
| DELETE | `/aliases/{id}` | ✓ | Delete a deactivated alias; its address is never reused |
| GET | `/health` | | Liveness and database check |

</details>

## Tests

Two pytest suites. GitHub Actions runs the lint, the code tests on SQLite and PostgreSQL, the dependency audits and the live tests against the Docker stack on every push.

**Code tests** (`project/tests`, 101 tests) exercise the API in-process: auth, codes, aliases, 2FA, account management, security headers, rate limits, mail sending and the legacy migration.

```bash
cd project
pytest                                                          # SQLite (3 migration tests need PostgreSQL)
TEST_DATABASE_URL=postgresql://user:pass@localhost/test pytest   # all 83 tests on PostgreSQL
ruff check . ../live_tests && ruff format --check . ../live_tests
cd front && npm run lint && npm run build
```

**Live tests** (`live_tests`) check a *running* instance, your local Docker stack or the real server, including DNS and the mail server.

<details>
<summary><b>Live test groups and how to run them</b></summary>

Each group runs when its settings are present and is skipped otherwise:

| Group | Checks | Needs |
| --- | --- | --- |
| `test_web.py` | Site and API respond, SPA routing, security headers, caching, JSON errors, CORS, rate limiting, HTTPS redirect + HSTS | `BASE_URL` |
| `test_account_flow.py` | Real account: aliases, password change, 2FA login, deletion | `BASE_URL` + a code source |
| `test_browser.py` | Chromium goes through every user scenario of the spec | `BASE_URL` + a code source + Playwright |
| `test_dns.py` | MX, SPF, DMARC, DKIM, reverse DNS (IPv4/IPv6) | `MAIL_DOMAIN` (+ `SERVER_IP`) |
| `test_smtp.py` | Port 25 answers, STARTTLS, not an open relay, unknown and deactivated aliases rejected, active alias accepted | `MAIL_DOMAIN` (+ a code source) |
| `test_smtp.py` (delivery) | A message to an alias arrives in the inbox, not in spam | `IMAP_*` |

A code source is how the tests read verification codes. Use `CODE_LOG_COMMAND` with `MAIL_BACKEND=console`, or a real mailbox via `IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD`. The mailbox needs plus-addressing: Gmail, Yandex and Outlook support it, and Gmail needs an app password. Every test account is deleted at the end.

```bash
pip install -r live_tests/requirements.txt && playwright install chromium

# local Docker stack
docker compose up -d --build --wait
BASE_URL=http://127.0.0.1:8080 CODE_LOG_COMMAND="docker compose logs --no-log-prefix backend" pytest live_tests

# the deployed server
BASE_URL=https://example.com MAIL_DOMAIN=example.com SERVER_IP=203.0.113.10 \
IMAP_HOST=imap.gmail.com IMAP_USER=you@gmail.com IMAP_PASSWORD=app-password pytest live_tests

# the mail server before the MX record points at it
MAIL_DOMAIN=example.com MAIL_SERVER=203.0.113.10:25 SMTP_TLS_VERIFY=0 pytest live_tests/test_smtp.py
```

The API allows 5 code requests per minute per IP, and one full run uses 4. Wait a minute between runs.

</details>

## Project structure

```
├── project/                  Flask API
│   ├── app.py                app factory and CLI (init-db, migrate-legacy)
│   ├── api/                  auth, account and aliases endpoints
│   ├── security.py           passwords, tokens, codes, TOTP, login_required
│   ├── models.py  database.py  validation.py  mailer.py  config.py  errors.py
│   ├── tests/                pytest suite
│   └── front/                React SPA (Vite) and its nginx config
├── live_tests/               pytest checks of a running instance: web, accounts, browser, DNS, mail server
├── deploy/                   server setup script, daily backup, Postfix lookup, personal site
├── docs/                     screenshots and the original coursework documents (RU)
└── docker-compose.yml        PostgreSQL + Redis + API + nginx
```

The project started as coursework at HSE University; the original proposal and user scenarios are in [`docs/coursework`](docs/coursework/README.md).
