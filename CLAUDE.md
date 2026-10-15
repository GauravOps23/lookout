# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Two generations of the same product live side by side. Not a git repository.

- **Legacy "Servers Panel"** (repo root): `get_error.py` + `index.html`. A static dashboard of daily error counts per Oracle database. Still works until cutover.
- **New "Lookout"** (`backend/`): an authenticated, permissioned FastAPI service that replaces it. Milestones M1-M3 are done and tested against fakes; M4 (combined/trend views, the "Sensei" LLM chatbot) is not started.

Design docs at the root: `spec.md` (authoritative, section numbers are cited in code and README), `PRD.md`, `PREREQUISITES.md`, `README.md` (run instructions, API list, deviations from the spec, what is and is not verified). Read the README "Deviations from the spec" before changing behaviour the spec describes.

## Commands (run from `backend/`)

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest                                     # all tests (~161, a couple of minutes)
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

`backend/.env` and `dev.db` exist locally (gitignored in intent; there is a `.gitignore`, though no git repo). `deploy/docker-compose.yml` has never been run.

## Backend architecture (`backend/app/`)

- `main.py` builds the app: security-headers middleware, feature routers under `/api`, then mounts `static/` last so API routes win. OpenAPI docs are disabled.
- Feature packages each hold a `router.py` (+ service/schemas): `auth`, `admin`, `authz`, `backup`, `audit`, `resources`, `views`, `dashboards`, `links`, `home`, `scheduler`, `secrets`. Models are all in `models.py`; migrations in `migrations/versions/0001-0003` (add a new Alembic revision for any model change).
- **Authz** (`authz/`, `deps.py`): capability permissions (direct or via group) plus per-object ACL levels view < use < edit < manage; default deny. Access is re-checked on every view run and every dashboard widget fetch.
- **Secrets** (`secrets/vault.py`): AES-256-GCM envelope encryption keyed by `MASTER_KEY`/`MASTER_KEY_FILE`; blobs are bound to their row; secrets are write-only through the API and redacted from logs (`logging_filter.py`) and audit.
- **Resources** (`resources/`): types `ssh`, `oracle`, `api`, each with a connector in `resources/connectors/` (Oracle thin mode in a read-only transaction; SSH with TOFU host-key pinning; HTTP GET/POST with SSRF guard).
- **Views** (`views/`): plugin system. A view type is a folder with a `plugin.py` registered by one line in `views/registry.py`; every plugin returns the single `ResultSet` contract from `views/base.py`. `engine.py` handles caching, coalescing, concurrency limits and fan-out, and must report per-source failures rather than swallow them. `db_table` SQL goes through `sqlguard.py` (SELECT-only allow-list); loosening it is a security change.
- **Scheduler** (`scheduler/`): separate process (APScheduler, in-memory jobs re-read every 60 s): status checks, cron views run as their owner, retention.
- **UI** (`static/`): plain HTML/CSS/JS single-page app (hash routes in `app.js`: overview, servers, views, dashboards, links, users, sensei placeholder) with no build step and a strict CSP: no inline script/style, no external fonts, never `innerHTML` (`tests/test_ui.py` enforces this). Set styles via `el.style.*`, not the `style` attribute. `[hidden]` is forced to `display:none` in `style.css` because layout rules otherwise override it. The JS has never been run in a real browser.
- **Naming**: the product is "Lookout"; the planned LLM chatbot (M4) is "Sensei". The legacy dashboard name "Servers Panel" survives only in `legacy_import.py` (`DASHBOARD_NAME`) and the migration.
- Plugins and connectors are synchronous (threaded), not async.

## Legacy panel notes

- `get_error.py` runs on a Linux host, not here. It reads `/home/ilink/gaurav/csvfileupload/serverlist_update` (lines `username,password,ip,port,servicename`), runs one query per server with `cx_Oracle`, and writes `<servicename>.csv` (`SYSTEM,REQ_USER,ERROR,COUNT`). It is Python 2 style (`'wb'` for `csv.writer` fails on Python 3), has a bare `except: pass`, and prints the connection string including the password.
- `index.html` links each box to `index2.html?csvFile=<servicename>.csv`; `index2.html` and `CsvToTable2.js` are not in this repo. Servers appear under several categories; large commented-out blocks are dead code. Logo is hotlinked from S3.
- The legacy query cross-joins `system_name_tmp` with no join condition. The migration deliberately preserves this so results match old CSVs (open item in PREREQUISITES.md).
- `tools/migrate_serverlist.py` (uses `app/legacy_import.py`) imports the server list and `index.html` categories into the new service; supports `--dry-run` and `--compare-csv-dir`. Never copy `serverlist_update` into the repo or print its passwords.
