# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Two generations of the same product live side by side. Git repo on `main`.

- **Legacy "Servers Panel"** (repo root): `get_error.py` + `index.html`. A static dashboard of daily error counts per Oracle database. Still works until cutover.
- **New "Lookout"** (`backend/`): an authenticated, permissioned FastAPI service that replaces it. Milestones M1-M3 are done and tested against fakes; M4 is built: trend views (`views/trend/`, a "derived" view type that reads another view's run history) and Sensei (`llm/`: config, tools, agent loop, SSE chat API; UI on the Sensei page). Sensei is tested only against a fake model. The UI enhancement round from `enhancement.md` is also built (plan and decisions in `enhancement_plan.md`): responsive layout and pinnable drawer, Servers and Pods pages, OpenShift connections, paged views, a Postman-style API page, new Sensei tools, and Users/Groups/Policy pages.

Design docs at the root: `spec.md` (authoritative, section numbers are cited in code and README), `PRD.md`, `PREREQUISITES.md`, `README.md` (run instructions, API list, deviations from the spec, what is and is not verified), `DEPLOYMENT.md` (RHEL 8 step-by-step). `gauravinstruction.md` is a local Windows "how to run again" cheat sheet that duplicates the Commands below. Read the README "Deviations from the spec" before changing behaviour the spec describes.

## Commands (run from `backend/`)

```bash
python -m venv .venv && .venv/Scripts/activate      # PowerShell: .venv\Scripts\activate; Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                                     # all tests (~230, about three minutes)
python -m pytest tests/test_sqlguard.py              # one file
python -m pytest tests/test_views_api.py -k name     # one test
copy .env.example .env                               # SQLite dev config; fill MASTER_KEY from the next command
python -m app.cli gen-master-key
alembic upgrade head
python -m app.cli create-admin admin                 # prompts for a 12+ char password
python -m uvicorn app.main:app --port 8000           # API + UI at http://localhost:8000/
python -m app.scheduler                              # separate process; run exactly one
```

There is no linter or formatter configured. Tests need no database or network: `tests/conftest.py` sets env vars, uses a file-backed SQLite DB per test and fake Oracle/SSH/HTTP connectors. The `Api` helper there logs in and sends the CSRF header automatically.

`backend/.env` and `dev.db` exist locally (gitignored). `deploy/docker-compose.yml` has never been run. Production target is RHEL 8 (nginx, PostgreSQL 16): `deploy/lookout.sh` is an idempotent install/update script (run with sudo; env vars `RUN_USER`, `APP_DIR`, `DATABASE_URL`, `SERVER_NAME`, `PYTHON`), and `DEPLOYMENT.md` is the manual walkthrough.

## Backend architecture (`backend/app/`)

- `main.py` builds the app: security-headers middleware, feature routers under `/api`, then mounts `static/` last so API routes win. OpenAPI docs are disabled.
- Feature packages each hold a `router.py` (+ service/schemas): `auth`, `admin`, `authz`, `backup`, `audit`, `resources`, `views`, `dashboards`, `links`, `home`, `monitor`, `scheduler`, `secrets`. Models are all in `models.py`; migrations in `migrations/versions/0001-0005` (add a new Alembic revision for any model change; `dev.db` needs `alembic upgrade head` after pulling a new one).
- **Monitor** (`monitor/`): live read-only endpoints for the Servers page (`/api/servers/health`, one SSH round-trip per host via `views/ssh_health/snapshot.py`) and the Pods page (`/api/pods`). Both need `use` access on the connection and cache briefly (`monitor/cache.py`).
- **Authz** (`authz/`, `deps.py`): capability permissions (direct or via group) plus per-object ACL levels view < use < edit < manage; default deny. Access is re-checked on every view run and every dashboard widget fetch.
- **Secrets** (`secrets/vault.py`): AES-256-GCM envelope encryption keyed by `MASTER_KEY`/`MASTER_KEY_FILE`; blobs are bound to their row; secrets are write-only through the API and redacted from logs (`logging_filter.py`) and audit.
- **Resources** (`resources/`): types `ssh`, `oracle`, `api`, `ocp`, each with a connector in `resources/connectors/` (Oracle thin mode in a read-only transaction; SSH with TOFU host-key pinning; HTTP GET/POST with SSRF guard; `ocp` is read-only GETs to the Kubernetes API with a service-account token, built on the HTTP connector). A new type also needs `PER_RESOURCE` in `views/engine.py` and a permission in `authz/permissions.py`.
- **Views** (`views/`): plugin system. Types: `db_table`, `ssh_health`, `api_response`, `ocp_pods`, `trend`. Results over 10,000 rows keep a preview in `ViewRun.result` and the full rows (up to 100,000) gzipped in `ViewRun.rows_blob`, latest run only; `GET /api/views/{id}/rows` pages through them. An `api_response` view with `output: "raw"` is what the API page runs: user-typed headers beat the defaults (SOAP 1.1 defaults to `text/xml` + `SOAPAction`). A view type is a folder with a `plugin.py` registered by one line in `views/registry.py`; every plugin returns the single `ResultSet` contract from `views/base.py`. `engine.py` handles caching, coalescing, concurrency limits and fan-out, and must report per-source failures rather than swallow them. `db_table` SQL goes through `sqlguard.py` (SELECT-only allow-list); loosening it is a security change.
- **Scheduler** (`scheduler/`): separate process (APScheduler, in-memory jobs re-read every 60 s): status checks, cron views run as their owner, retention.
- **UI** (`static/`): plain HTML/CSS/JS single-page app (hash routes in `app.js`, in nav order: overview, dashboards, servers, pods, views and `views/<id>`, api and `api/<id>`, links, sensei, inventory, and for admins users, groups, policy; detail pages open in a new tab; the drawer can be pinned) with no build step and a strict CSP: no inline script/style, no external fonts, never `innerHTML` (`tests/test_ui.py` enforces this). Set styles via `el.style.*`, not the `style` attribute. `[hidden]` is forced to `display:none` in `style.css` because layout rules otherwise override it. The response viewer (`responseView`) tokenizes XML/JSON itself and builds DOM nodes, never HTML. The JS has been checked only in headless Edge against stubbed API data, not by a person using a real session; there is no Node here, so a syntax slip shows up as a page stuck on "Loading…" (load the page in headless Edge to see the console error).
- **Naming**: the product is "Lookout"; the LLM chatbot is "Sensei". The legacy dashboard name "Servers Panel" survives only in `legacy_import.py` (`DASHBOARD_NAME`) and the migration.
- Plugins and connectors are synchronous (threaded), not async.

## Legacy panel notes

- `get_error.py` runs on a Linux host, not here. It reads `/home/ilink/gaurav/csvfileupload/serverlist_update` (lines `username,password,ip,port,servicename`), runs one query per server with `cx_Oracle`, and writes `<servicename>.csv` (`SYSTEM,REQ_USER,ERROR,COUNT`). It is Python 2 style (`'wb'` for `csv.writer` fails on Python 3), has a bare `except: pass`, and prints the connection string including the password.
- `index.html` links each box to `index2.html?csvFile=<servicename>.csv`; `index2.html` and `CsvToTable2.js` are not in this repo. Servers appear under several categories; large commented-out blocks are dead code. Logo is hotlinked from S3.
- The legacy query cross-joins `system_name_tmp` with no join condition. The migration deliberately preserves this so results match old CSVs (open item in PREREQUISITES.md).
- `tools/migrate_serverlist.py` (uses `app/legacy_import.py`) imports the server list and `index.html` categories into the new service; supports `--dry-run` and `--compare-csv-dir`. Never copy `serverlist_update` into the repo or print its passwords.
