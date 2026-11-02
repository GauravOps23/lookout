# Lookout enhancement plan (from enhancement.md)

## Context
Lookout M1-M4 works, but the UI has no edit screens, wastes space on wide monitors (content capped at 1280px, left-aligned: see `screenshot_v2/ui_aligment.png`), and lacks Pods, OCP, a Linux-health page, view paging and API-runner features. Goal: deliver everything in `enhancement.md` in phases that each ship and test on their own.

Decisions made: split **Servers** (health) from **Inventory** (connections); **server-side paging** at 50 rows/page, cap ~100k; dashboards keep the **existing 50 total widget limit** (no 20/10 per-type limit); OCP uses a **read-only service-account token**.

Constraints (CLAUDE.md / `tests/test_ui.py`): no `innerHTML`/`outerHTML`/`insertAdjacentHTML` (even in comments), no inline script/style, no `style=` attribute (use `el.style.*`), no build step. New Alembic revision (`0005`) for any model change. Reuse `h()`, `modal()`, `field()`, `pageHead()`, `renderResult()`, `confirmBox()` in `static/app.js`.

## Phase 1: UI foundation (no backend)
Files: `static/style.css`, `static/app.js`, `static/index.html`.
- **Responsive**: remove `main{max-width:1280px}` (`style.css:39`) and fluid-fill the width; use `auto-fit/minmax` grids; add tablet breakpoints (820-1280px); cap the activity feed height with scroll; replace `.chat` `calc(100vh-190px)` and the `.login-wrap` negative margins with robust layout.
- **Drawer**: hamburger toggles open/closed, pin button keeps it open (state in `localStorage`, wrapped in try/catch). Unpinned = overlay on small screens, collapsed rail or hidden on desktop.
- **Nav order**: Overview, Dashboards, Servers, Pods, Views, API, Links, Sensei, Inventory, then Users/Groups/Policy (admin-only). Rewrite `drawNav` (`app.js:557`) and `PAGES` (`app.js:555`).
- **Shared helpers**: add a `table()` helper (every page hand-builds tables today) and a `pager()` helper; page-heading banner style.
- **Overview**: page heading with a shadowed header band; stat blocks per category (Servers, DBs, APIs, OCP: total/up/down); remove "Top errors today"; keep Recent activity. Extend `/api/home/summary` (`home/`) to return per-type counts.

## Phase 2: Edit everywhere (the "can't edit, only delete" complaint)
Backend already has `PATCH` for resources, views, dashboards, users. Gaps: no group PATCH.
- Backend: add `PATCH /api/admin/groups/{id}` (name/description); edit for link tiles if missing.
- UI: reuse the create dialogs in edit mode (prefill, send `PATCH`, blank secret = keep existing) for Inventory, Views, Dashboards (full widget editor), Users (name, admin flag), Groups. Edit buttons shown by `level` (`edit`/`manage`) as delete is today.
- Links stay untouched, as you asked.

## Phase 3: Inventory + OCP + API config
- **Inventory page** = current Servers list moved, grouped by Linux / Oracle DB / API (REST, SOAP) / OCP, with add + edit.
- **OCP type**: `OcpConfig`/`OcpSecret` in `resources/schemas.py:67` (api_url, token, verify_tls, default namespace); `inventory.ocp.create` in `authz/permissions.py:319`; `resources/connectors/ocp.py` (`ping` via HTTPS to the k8s API, reuse `HttpConnector` + `check_ssrf`); register in `resources/service.py:86` and `PER_RESOURCE["ocp"]` in `views/engine.py:196` (else `KeyError`); add `"ocp"` to the Sensei enum (`llm/tools.py:34`). No migration needed (type is a free string).
- **API form**: UI for `ApiConfig` (REST/SOAP, GET/POST, auth none/basic/bearer/api-key, default headers); SOAP envelope + SOAPAction already supported in the `api_response` plugin.

## Phase 4: Servers health (swap, uptime, iowait) and Pods
- **Servers page**: Linux-only health cards from `ssh_health` view runs. In `views/ssh_health/parsers.py` add checks: `swap` (parse the `Swap:` line of `free -m`, currently ignored at `:61`) and `iowait` (`vmstat 1 2` or `/proc/stat` delta); uptime exists. Auto-create or ship a default "Linux health" view per host so the page needs no setup.
- **Pods**: new `ocp_pods` view plugin (`views/ocp_pods/plugin.py`, register in `views/registry.py:7`) calling `GET /api/v1/pods` per namespace; returns the standard `ResultSet` with namespace, pod, phase, ready, restarts, age, node and reason (waiting reason / CrashLoopBackOff / Pending). Page shows, per OCP connection, namespace-grouped blocks with counts and a highlighted "not running" list.

## Phase 5: Views and Dashboards
- **Views**: remove the predefined "Daily errors" template (`views/router.py:106`, `GET /api/view-templates`); keep custom SQL with multiple DBs via `source_resource_ids`. List page groups by view name, with its DBs underneath; click opens `#/views/<id>` in a new tab (`target=_blank`).
- **Paging**: add `GET /api/views/{id}/rows?page=&size=50` backed by the stored `ViewRun.result`; raise the row cap (`base.py:12`, `db_table.row_limit`) to ~100k with an explicit "truncated" flag; for the DB column, store compressed rows or a side table to avoid bloating `view_runs`; UI pager with total count. Needs `0005` if a rows table is added. Risk: 100k rows x many sources memory; mitigation is the ROWNUM cap in `sqlguard.wrap` and streaming fetch.
- **Dashboards**: list page (cards), click opens new tab (`#/dashboards/<id>`); owner/admin editor (add/remove/reorder widgets); shared users already see dashboards via ACL (`dashboard_acl`), so this only needs the UI to display shared ones and to require view access on widgets' views.

## Phase 6: API runner (Postman-style)
- New "API" page and `api_runner` concept: admin defines a saved request (resource, method GET/POST, path, query, headers, body template, declared user parameters). User clicks it, fills parameters, sees the response. Implement as UI over the existing `api_response` view plugin (params/variables, REST + SOAP) rather than a new engine; add the page + a param-prompt dialog, and a "Run" using `/views/{id}/run?params`. Gap to fill: allow `view.api.create` admin-only authoring and a raw response viewer.

## Phase 7: Sensei
- Add tools in `llm/tools.py`: `list_dashboards`, `summarize_dashboard` (by name; respects ACL, uses cached widget data), `get_resource_counts` (servers up/down/total). Keep `llm_allowed` gating; admin-only config is already enforced (`admin.llm`). API hitting works via `run_view` on API views.

## Phase 8: Users, Groups, Policy page
- Single admin-only page with three tabs: Users (create/edit/activate, reset password), Groups (create/edit, members, permissions), Policy (per-resource ACL picker: which user/group can view/use/edit which Inventory item, View, Dashboard via existing `PUT .../acl`). Multiple admins already supported (`is_admin` boolean); add a guard against removing the last admin.

## Verification
- `cd backend && python -m pytest` (~161 tests, must stay green; `tests/test_ui.py` guards the CSP/innerHTML rules).
- Add tests per phase using the fakes in `tests/conftest.py`: group PATCH, OCP connector with fake HTTP, `ocp_pods` plugin, new `ssh_health` parsers (swap/iowait with canned output), view `rows` paging (1 and 100k fake rows), dashboard ACL visibility, new Sensei tools.
- UI: run `uvicorn app.main:app --port 8000` and check in a browser at 1366, 1920, 2560 widths and phone width (the JS has never been run in a real browser, so expect fixes). Check drawer open/close/pin, nav order for admin vs non-admin, edit round-trips, new-tab opening.
- Migration: `alembic upgrade head` on a fresh SQLite and a copy of `dev.db`.

## Order and size
Do phases 1-2 first (highest visible value, low risk), then 3-5, then 6-8. Each phase is its own commit. Largest risks: view paging at 100k rows (Phase 5) and OCP, which can only be tested against fakes here.
