# Lookout

Backend for the panel described in [`spec.md`](spec.md) (derived from `PRD.md`). It replaces the static
"Servers Panel" (`index.html` + `get_error.py`, still in this folder) with an authenticated, permissioned service.

**Status: Milestones M1 (Foundation), M2 (Inventory & views) and M3 (Dashboards & link panel, with a small
read-oriented web UI) are implemented and tested against fakes. M4 is partly built: trend views and Sensei (tested against a fake model, not a real LLM).** See [Status](#status) for what is and is not verified.

See [PREREQUISITES.md](PREREQUISITES.md) for what you need installed and which access to prepare.

## Quick start (development)

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows;  Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                  # 161 tests, a couple of minutes; no database or external system needed
```

## Run it

```bash
cd backend
export DATABASE_URL=postgresql+psycopg://panel:panel@localhost:5432/panel
export MASTER_KEY=$(python -m app.cli gen-master-key)   # or MASTER_KEY_FILE=/path/to/file; keep it safe
export COOKIE_SECURE=false                              # local http only; keep true behind TLS

alembic upgrade head                                    # create the schema
python -m app.cli create-admin alice --name "Alice"     # prompts for a password (12+ chars)
uvicorn app.main:app --port 8000                        # the API
python -m app.scheduler                                 # in a second process: status checks, cron views, retention
```

Run **one** scheduler process. Docker: copy `deploy/.env.example` to `deploy/.env`, write a key to
`deploy/master_key.txt`, then `docker compose -f deploy/docker-compose.yml up --build` (db, one-shot migrate, app,
scheduler). The compose file has not been run yet. Put a TLS proxy in front; login will not work over plain http while
`COOKIE_SECURE=true`.

Open `http://localhost:8000/` for the UI (sign in, home, dashboards, links, shared dashboards).
Health: `GET /healthz` (process), `GET /readyz` (database reachable). OpenAPI docs are disabled.

### Windows / PowerShell, no database (quickest way to see the UI)

```powershell
cd backend
.venv\Scripts\activate                       # after: python -m venv .venv ; pip install -r requirements-dev.txt
copy .env.example .env                        # then edit .env: paste the output of the next command into MASTER_KEY=
python -m app.cli gen-master-key
alembic upgrade head
python -m app.cli create-admin admin          # choose a 12+ character password
python -m uvicorn app.main:app --port 8000    # leave running, then open http://localhost:8000/
```

If the page does not open: the `uvicorn` window must still be running; the default `DATABASE_URL` is PostgreSQL, so
without `.env` (or the variable) `alembic`/the API fail to connect; and if you see the login page but cannot sign in,
check `COOKIE_SECURE=false` when using plain http. For the scheduler, run `python -m app.scheduler` in a second window.

### Migrating from the old panel

```bash
cd backend
python ../tools/migrate_serverlist.py --serverlist /path/to/serverlist_update --index-html ../index.html \
       --owner alice --dry-run                           # report only, writes nothing
python ../tools/migrate_serverlist.py ... --owner alice  # import (safe to re-run)
python ../tools/migrate_serverlist.py ... --owner alice --compare-csv-dir /path/to/abc   # equivalence check
```

It creates the "Servers Panel" dashboard (one Daily-errors widget) and one `oracle` resource per server (password goes into the encrypted vault, never printed), tags each with
its categories from the live part of `index.html` (commented-out categories are ignored), and creates the scheduled
"Daily errors" view fanned out over all of them. Then rotate the old plaintext passwords and delete `serverlist_update`.
`--compare-csv-dir` runs the view and compares each service with its legacy CSV; it needs real Oracle access.

### Configuration (environment variables)

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` | local Postgres | SQLAlchemy URL |
| `MASTER_KEY` / `MASTER_KEY_FILE` | - | base64 32-byte key for secret encryption |
| `BACKUP_DIR` | `./backups` | where backup archives are written |
| `ALLOWED_ORIGINS` | `[]` | JSON list of CORS origins |
| `COOKIE_SECURE` | `true` | `Secure` flag on the session cookie |
| `SESSION_ABSOLUTE_HOURS` / `SESSION_IDLE_MINUTES` | 8 / 30 | session lifetime |
| `LOGIN_RATE_PER_MINUTE` | 10 | per-IP login limit |
| `MAX_FAILED_ATTEMPTS` / `LOCKOUT_MINUTES` | 5 / 15 | account lockout |

## What is implemented

### M1 Foundation
| Spec | Where | Notes |
|------|-------|-------|
| 3 Authentication | `app/auth/` | argon2id, hashed session tokens, HttpOnly/Strict cookie, CSRF header, lockout, login rate limit, forced first-login password change, `AuthProvider` interface |
| 4 Authorization | `app/authz/`, `app/deps.py` | capability permissions (direct or via group, scoped `panel.access`), object ACL levels view < use < edit < manage, default deny |
| 5 Secrets | `app/secrets/vault.py` | AES-256-GCM envelope encryption, blobs bound to their row, DEK re-wrap for rotation, log redaction |
| 10 Backup | `app/backup/` | encrypted archive, checksums, dry-run restore, idempotent, schema-version check |
| 11 Audit | `app/audit/` | append-only, secrets scrubbed, filter + CSV export |

### M2 Inventory & views
| Spec | Where | Notes |
|------|-------|-------|
| 6 Resources | `app/resources/` | `ssh`, `oracle`, `api` types; secrets write-only; ACLs; transfer; delete blocked while views depend on it; self-test with sanitised errors |
| 6.2 Connectors | `app/resources/connectors/` | Oracle thin mode (read-only transaction), SSH with trust-on-first-use host-key pinning, HTTP with GET/POST only, SSRF guard, 5 MB cap, no redirects |
| 7.1-7.2 Plugin + ResultSet | `app/views/base.py`, `registry.py` | new view type = new folder + one registry line; one ResultSet contract |
| 7.3 V1 ssh_health | `app/views/ssh_health/` | cpu_load, mem, disk, uptime, process, port with warn/crit thresholds; custom commands need `view.ssh.custom` |
| 7.3 V2 db_table | `app/views/db_table/`, `sqlguard.py` | SELECT-only allow-list (sqlglot + text checks), named binds, row cap in SQL, fan-out across many Oracle resources with a `SOURCE` column, "Daily errors" template |
| 7.3 V3 api_response | `app/views/api_response/` | variables validated then encoded per slot (URL / JSON leaf / XML text), header variables need a pattern, JSONPath mapping, REST and SOAP |
| 7.5 Execution | `app/views/engine.py` | access re-checked on every run, cache per parameter values, coalescing, per-resource and global concurrency limits, per-source failures reported (never swallowed) |
| 6.3, 7.5 Scheduler | `app/scheduler/` | status check every 5 min, cron views run as their owner, 30-day result / 1-year history retention |
| 15 Migration | `app/legacy_import.py`, `tools/migrate_serverlist.py` | see above |

### M3 Dashboards, link panel, home
| Spec | Where | Notes |
|------|-------|-------|
| 8 Dashboards | `app/dashboards/` | 12-column widget grid, ACLs (view/edit/manage), widgets validated at save (view access, variables), a viewer without access to a widget's view gets a placeholder and no data, `?run=1` needs `use` |
| 8 Share links | same | random token stored hashed, expiry (default 7 days), revocable; unauthenticated, cached results only, never a live connector call, rate limited |
| 8 Home | `app/home/` | summary, tag heat-map, top errors (aggregated from the "Daily errors" views you can see), activity; all scoped to what the caller can see |
| 8 Link tiles | `app/links/` | http(s) URLs only (no embedded credentials), access by owner/admin/scoped `panel.access`, shared or per-user credentials encrypted, `reveal` audited and rate limited |
| UI | `app/static/` | plain HTML/CSS/JS, no build step, served by the API; strict CSP (no inline script/style), no HTML injection by construction |

### API (all under `/api`; state-changing calls need the `X-CSRF-Token` returned by login or `/auth/me`)

- Auth: `POST /auth/login|logout|change-password`, `GET /auth/me`
- Admin (`admin.users|groups|audit|backup|settings`): users, groups, permissions, `GET /admin/audit[/export.csv]`,
  `POST /admin/backup`, `GET /admin/backups[/{id}/download]`, `POST /admin/restore`,
  `GET|PUT /admin/settings/ssrf-allowlist`
- Resources: `GET|POST /resources`, `GET|PATCH|DELETE /resources/{id}`, `POST /resources/{id}/test|transfer`,
  `GET|PUT /resources/{id}/acl`
- Dashboards: `GET|POST /dashboards`, `GET|PATCH|DELETE /dashboards/{id}`, `GET|PUT /dashboards/{id}/acl`,
  `GET /dashboards/{id}/widgets/{wid}/data[?run=1]`, `POST|DELETE /dashboards/{id}/share-link`, `GET /shared/{token}` (no login)
- Links: `GET|POST /links`, `PATCH|DELETE /links/{id}`, `POST /links/{id}/launch|reveal`, `PUT /links/{id}/my-credential`
- Home: `GET /home/summary|heatmap|top-errors|activity`
- Views: `GET /view-types`, `GET /view-templates`, `GET|POST /views`, `GET|PATCH|DELETE /views/{id}`,
  `POST /views/{id}/run[?fresh=1]`, `GET /views/{id}/result` (cached data only), `GET|PUT /views/{id}/acl`

## Deviations from the spec

- **Backup encryption** uses scrypt + AES-256-GCM from `cryptography`, not `age` (archives are `.tar.enc`).
  Backups include users (no password hashes), groups, permissions, settings, resources, views and ACLs, plus resource
  credentials in `secrets.json` unless `include_secrets=false`. Restored users get a random password and need an
  admin reset. The scheduled backup (daily 02:00, keep 14) and `POST /admin/rotate-key` are not wired.
- **Plugins are synchronous** (threads) rather than `async def run`; the connector libraries are blocking.
- **Lists are JSON columns** (`tags`, `source_resource_ids`), not Postgres `text[]` / `uuid[]`, so there is no GIN
  index on tags. Fine at the spec's scale (<=100 resources).
- **SOAP** is plain HTTP + `defusedxml` + ElementTree paths, not `zeep` + XPath. No WSDL support.
- **`{{today}}`** expands to the fixed expression `TRUNC(SYSDATE)` instead of a bind; other `{{name}}` become binds.
- **Scheduler** re-reads the views table every 60 s and keeps jobs in memory, instead of an APScheduler DB job store.
- **Logins** are lower-cased in the app instead of using Postgres `citext`; the login rate limiter is in-process.
- **UI is not the spec's React SPA.** It is a small vanilla-JS page: home, dashboard view, links, shared view, login and
  forced password change. There are no screens yet for admin (users, groups, audit, backup), inventory, view building or
  dashboard editing: use the API for those. `chart_line`/`chart_bar`/`status_badge`/`kv_cards` renderers fall back to the table.
- **Home page** is served by dedicated endpoints; there is no per-user editable "home" dashboard.
- **Backup** also covers dashboards (widgets, ACLs) and link tiles (shared credentials only). Share tokens and per-user
  link credentials are not exported: re-share and re-enter after a restore.
- **SSRF guard** allows private RFC1918 ranges (internal APIs are the point). DNS can change between the check and the
  connection, so it is not a complete defence against DNS rebinding.
- `(type, name)` must be unique for resources and for views: backup/restore and the import match on it.

## Status

| Milestone | State |
|-----------|-------|
| M1 Foundation | Done, tested |
| M2 Inventory & views | Done, tested against fake connectors |
| M3 Dashboards & link panel, home, minimal UI | Done, tested at the API level; UI not exercised in a browser |
| M4 Trend view & "Sensei" LLM chatbot | Built, tested against fakes. Sensei replies are not token-streamed: the model is called non-streaming and the answer is sent as one `token` event. Sensei config is not in backups. Needs a real endpoint test (Ollama/vLLM) |

**Verified:** 161 automated tests; migrations 0001-0003 upgrade, downgrade and upgrade
again on SQLite; the migration CLI against a scratch database with the real `index.html`.

**Not verified:** the web UI in a real browser (no Node or browser was available: the JS has never been run, only
served and scanned for inline script/HTML-injection patterns by tests); anything against real Oracle, SSH servers or APIs (the `oracledb`/`paramiko` code paths and the SQL
`SET TRANSACTION READ ONLY` + `ROWNUM` wrapping are only exercised through fakes); PostgreSQL (all tests use SQLite);
the Docker setup; the old-vs-new CSV equivalence check (needs your Oracle servers). Run `--compare-csv-dir` on one
real day before retiring `get_error.py`.

## Legacy files

`index.html` and `get_error.py` keep working until you cut over. `get_error.py` prints the connection string with the
password and silently skips failing servers; the new panel records every per-server failure instead.
