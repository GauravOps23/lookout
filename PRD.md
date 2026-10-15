# PRD: Lookout (evolution of "Servers Panel")

## 1. Context
Today: `get_error.py` (cron-style Python 2-ish script) pulls daily error counts from ~60 Oracle DBs listed in a plaintext file (`user,password,ip,port,service`) into one CSV per service; `index.html` is a hand-edited grid of links to `index2.html?csvFile=X.csv`. Problems: plaintext credentials, silent `except: pass`, no auth, hardcoded paths, adding a server = editing 2 places, one fixed query.

Goal: a multi-user web platform where users register their own inventory (SSH hosts, Oracle DBs, REST/SOAP APIs), build data views and dashboards over it, share them with controlled permissions, link out to other panels, and let a local LLM summarise/query the same data. Existing Servers Panel becomes the first dashboard, and `serverlist_update` is migrated into DB inventory.

## 2. Users & roles
- **Admin**: creates users/groups, grants permissions, configures LLM, backups, global settings.
- **Power user (creator)**: has `inventory.create` / `dashboard.create`; manages own resources.
- **Viewer**: sees only panels/dashboards shared with them.

## 3. Functional requirements (mapped to your 7 points)

### 3.1 Auth (point 1)
- Login with user id + password; accounts created only by admin (no self-signup). Force password change on first login, lockout after N failures, session timeout, optional TOTP 2FA (later).
- Passwords hashed with argon2id/bcrypt. Optional LDAP/AD later (Airtel likely has it) — design auth as pluggable provider.

### 3.2 Permissions (point 2)
- RBAC with **groups**: user -> groups -> permissions. Per-resource ACL on top (owner, shared-with user/group, level: view / use / edit / manage).
- Permission catalog: `inventory.create`, `inventory.ssh|db|api.create`, `view.create`, `dashboard.create`, `panel.access:<id>`, `llm.use`, `admin.*`.
- Rule: effective access = union of group grants + direct ACL; deny by default. Every check done server-side.

### 3.3 Inventory (point 3)
Resource types, each owned by a user/group, visible only to those with ACL:
- **SSH host**: auth by password, private key (+passphrase), or "known_host / same-server local" entry. Store host-key fingerprint (TOFU, warn on change).
- **Oracle DB**: host, port, service name, user/password; test-connection button; read-only user recommended.
- **API**: REST and SOAP, GET/POST only; base URL, headers, auth (basic/bearer/api-key), WSDL/envelope template for SOAP; timeout.
- All secrets encrypted at rest (see 5), never returned to the browser after save (write-only fields), "Test connection" and last-status/health shown.
- Tags/folders (replaces the hardcoded categories 5G, PACS, POSTPAID...).

### 3.4 Data views (point 4) — each view type is a separate module
Every view type is its own self-contained plugin with its own config form, permission (`view.<type>.create`), executor, and renderer, registered through a common interface `ViewType { configSchema, run(ctx) -> ResultSet, defaultRenderer, llmToolSchema }`. Types never share config; they only share the normalized `ResultSet` (columns + rows + meta) so any renderer/LLM tool can consume them. A view = saved, parameterised instance of one type.

| # | View type | Source | Config | Default format |
|---|-----------|--------|--------|----------------|
| V1 | **Server Health** | SSH resource | whitelisted checks (CPU, mem, disk, load, uptime, process); custom command only for permitted users | status cards + gauges |
| V2 | **DB Table** | Oracle resource | saved read-only SELECT or table+columns+filters; params (e.g. `{{today}}`); row limit + timeout. Existing error-count query = first template | paged table |
| V3 | **API Response** | REST/SOAP resource | GET/POST, headers, body, JSONPath/XPath field mapping | key-value / table |
| V4 | **Combined** | 2 existing V2 (or V2+V3) views | join key + join type, done in-app on row-capped results (not cross-DB SQL); needs access to both sources | joined table |
| V5 | **Metric/Trend** (suggested) | any view with a numeric column | stored history of scheduled runs | line/bar chart |

- Output formats are likewise separate renderers, selectable per view/widget: table, key-value cards, chart (line/bar), status badge, raw JSON; CSV export for table.
- Execution modes: live (on open) or scheduled with cached result (replaces `get_error.py` cron) — scheduler + result cache with TTL, stale indicator.
- Code layout: `views/<type>/{schema,executor,renderer}`; adding a new type needs no change to dashboards, ACL, or LLM code.

### 3.5 Home dashboard (point 5 — suggestion)
Home page, per user, widget-based:
1. Global status strip: # resources up/down/unknown, failed scheduled runs.
2. "My favourites / pinned dashboards".
3. Health heat-map of inventory by tag (evolution of the current category boxes; red/amber/green).
4. Top errors today (the current error-panel data, aggregated across DBs).
5. Recently shared with me / recent activity.
6. LLM "daily briefing" card (summary of overnight failures) if enabled.
7. Admin-only: users online, failed logins, backup status.
Users can rearrange/hide widgets.

### 3.6 Dashboards & link panel (point 6)
- **Dashboard**: grid of widgets (each bound to a view) with layout saved, shareable to users/groups (view or edit), optional read-only share link with expiry.
- **Link panel / launcher**: tiles pointing to external panel URLs. Per tile: name, URL, tag, and credential mode:
  - *none*, *shared stored credential* (encrypted, admin/permitted user sets; launch via server-side proxy/SSO helper where possible), or *per-user credential* (each user saves their own; prompt if missing).
  - Caveat: browsers can't safely auto-fill another site's login. Options: (a) show credential with copy button + reveal audit, (b) basic-auth URL where target supports it, (c) reverse proxy injecting auth. Recommend (a) as MVP, (c) for specific targets.
  - Open in new tab (like current `target=_blank`), or iframe only if target allows.

### 3.7 Local LLM chatbot "Sensei" (point 7)
- Admin configures provider: Ollama / llama.cpp / vLLM / any OpenAI-compatible endpoint; model name, temperature, max tokens, timeout, system prompt; enable/disable per group.
- **Chatbot UI**: floating chat widget/side panel available on every page (and a full-page chat). Conversation history per user, streaming replies, "sources used" chips showing which resources/views were called, and a "summarise this dashboard/view" quick action when opened from a page.
- **Resources as tools**: each resource and view the user can access is exposed to the bot as a tool, auto-generated from the view type's `llmToolSchema` (see 3.4), e.g. `list_resources`, `list_views`, `run_view(id, params)` (DB table / server health / API / combined), `get_view_summary(id)`, `query_view_table(id, filter, limit)`. The bot picks tools, receives the normalized `ResultSet` (truncated/aggregated to token cap), and writes a summary. Admin can toggle per resource "allow LLM access".
- Tools execute **as the asking user**, so ACL applies to the LLM too; a resource the user cannot open is not even listed to the bot.
- Guardrails: LLM never gets credentials; read-only tools only; row/token caps (truncate/aggregate before sending); log every prompt, tool call and result; per-user rate limit. Treat data returned by tools as untrusted (prompt-injection from DB/API content) — no write tools.
- Fallback: if the model has no native tool-calling, use a JSON-action prompt loop.

### 3.8 Settings & backup
- Settings store (DB) with export: users/groups/ACL, inventory (secrets re-encrypted or excluded by option), views, dashboards, LLM config -> single encrypted archive (zip + AES / age). Import/restore with dry-run diff. Scheduled automatic backups with retention, plus manual download. Also DB-level backup of the app database.

### 3.9 Cross-cutting
Audit log (login, permission change, secret reveal, view run, LLM tool call); health checks for inventory; i18n not required; light/dark; responsive.

## 4. Non-functional
- Handles ~100 inventory items, ~50 users, view result up to e.g. 10k rows (paged).
- Security first (in-bank/telecom internal): HTTPS only, CSRF protection, rate limiting, input validation, secrets never logged.
- Single-host deployable (Docker Compose or systemd) with no internet dependency (logo currently hotlinked from S3 — bundle it).

## 5. Technical suggestions
**Stack (recommended):** Python backend since existing code is Python and `cx_Oracle`/`oracledb`, `paramiko`/`asyncssh`, `zeep` (SOAP), `httpx` fit directly.
- Backend: **FastAPI** + SQLAlchemy + Alembic. (Alt: Django gives auth/admin/groups/permissions out of the box — fastest MVP for the RBAC part; pick Django if team prefers batteries-included.)
- App DB: **PostgreSQL** (or SQLite for single-user pilot; MySQL/Oracle if that's what ops supports).
- Frontend: **React + Vite + TypeScript** with TanStack Table, ECharts/Recharts, react-grid-layout for dashboards. (Lightweight alt: HTMX + Alpine if you want no JS build.)
- Oracle: use **python-oracledb** (thin mode, no Oracle client install) instead of deprecated `cx_Oracle`; use bind variables, never string formatting; read-only DB user.
- Scheduler: APScheduler or Celery+Redis (APScheduler enough for MVP); run DB/SSH calls in a worker pool with timeouts and per-resource concurrency limits.
- Secrets: encrypt with AES-GCM/Fernet using a master key from env/file/KMS/Vault (not stored in DB); key rotation support. Passwords: argon2id. Sessions: HttpOnly+Secure+SameSite cookies (or short JWT + refresh).
- SSH: paramiko; verify host keys; commands from whitelist templates; no interactive shell/web terminal in MVP.
- SQL safety: parse with `sqlglot`, allow single SELECT only, wrap with row limit, statement timeout, run as read-only account.
- LLM: OpenAI-compatible client against Ollama/vLLM; tool schema in JSON; streaming responses via SSE.
- Backup: `pg_dump` + app export archive, encrypted with `age`.
- Observability: structured logs, `/healthz`, Prometheus metrics.

**Data model (core tables):** users, groups, user_groups, permissions, group_permissions, resources(type, owner, config_json, secret_blob), resource_acl, views(source refs, query/template, renderer), view_runs(cache), dashboards, dashboard_widgets, dashboard_acl, link_tiles, link_credentials, llm_config, llm_audit, audit_log, settings, backups.

## 6. Phased delivery
1. **MVP-1 Foundation**: auth, admin user/group mgmt, RBAC, audit log, encrypted secret store, settings backup/restore.
2. **MVP-2 Inventory + views**: Oracle + SSH + REST/SOAP resources with test-connection; DB table view, server-health view; scheduler + cache; **migrate current error-count job and `serverlist_update`** as a first template, replacing `get_error.py` and the hand-edited `index.html`.
3. **MVP-3 Dashboards & sharing**: widgets, ACL sharing, home dashboard, link panel.
4. **MVP-4 Combined views & LLM**: join views, LLM config, summarise, tool-calling.
5. **Later**: LDAP/SSO, 2FA, alerting (mail/Teams/SMS on thresholds), web terminal, anomaly detection, mobile view.

## 7. Further suggestions
- **Alerting/thresholds** on views (error count > N -> notify) — highest-value addition for an ops panel.
- Resource **ownership transfer** when users leave; admin "orphaned resources" report.
- **Approval flow** for new custom SQL/commands; versioning + diff of views.
- **Import wizard** for existing `serverlist_update` (and rotate those plaintext passwords after import).
- Dry-run/"explain" mode and visible query cost before running heavy SQL.
- Data retention: keep history of view results to show trends (errors per day).
- Read-only **public/TV-wall mode** for NOC screens with auto-refresh.

## 8. Open questions (to decide before build)
1. Multi-tenant (many teams, isolated) or single-org with groups? (assumed single-org)
2. Is LDAP/SSO required or are local accounts final?
3. Preferred stack/hosting (Linux host, Docker allowed? Python version?)
4. Should SSH view commands be free-form or whitelist-only?
5. Can per-user credentials for external panels be stored server-side, or must they stay on the user's device?
6. Which local LLM runtime/model is already available (Ollama, etc.)?
7. Expected scale and refresh frequency for scheduled views.

## 9. Verification (for when implementation starts)
- Unit tests for permission resolution (deny-by-default matrix), SQL whitelist, secret encrypt/decrypt round-trip.
- Integration: Oracle test DB, SSH container, mock REST/SOAP; migrate `serverlist_update` and confirm output equals current CSVs from `get_error.py`.
- Security checks: no secret in API responses/logs, LLM tools denied for resources the user can't access, backup restore on clean instance.
- E2E (Playwright): login -> create inventory -> create view -> dashboard -> share -> viewer sees only shared items.
