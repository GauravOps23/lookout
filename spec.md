# SPEC: Lookout

Technical specification derived from `PRD.md`. The PRD says *what* and *why*; this says *how*. Items marked **[ASSUMPTION]** answer the PRD's open questions (section 8) and can be changed without breaking the rest.

## 0. Assumptions & decisions

| # | Decision | Rationale |
|---|----------|-----------|
| A1 | Single organisation, groups for separation (no multi-tenancy) | PRD Q1 |
| A2 | Local accounts for v1; auth behind a `AuthProvider` interface so LDAP can be added | PRD Q2 |
| A3 | Linux host, Docker Compose, Python 3.11+ | PRD Q3 |
| A4 | SSH views are whitelist-only; free-form commands need `view.ssh.custom` permission | PRD Q4 |
| A5 | External-panel credentials stored server-side, encrypted; revealed to user via copy button, audited | PRD Q5 |
| A6 | LLM reached through an OpenAI-compatible HTTP endpoint (Ollama/vLLM/llama.cpp server) | PRD Q6 |
| A7 | Scale: <=100 resources, <=50 users, <=10k rows/result, refresh >=1 min | PRD Q7 |
| A8 | Backend FastAPI + SQLAlchemy 2 + Alembic; PostgreSQL 15; React + Vite + TypeScript | PRD section 5 |

## 1. Architecture

```
Browser (React SPA)
   |  HTTPS, cookie session, CSRF token, SSE for chat
Reverse proxy (nginx/caddy, TLS)
   |
FastAPI app  --- routers: auth, admin, resources, views, dashboards, links, llm, backup
   |   |-- services: authz, secrets, view_engine, scheduler, llm_agent, backup, audit
   |   |-- view plugins: views/{ssh_health,db_table,api_response,trend}
   |   `-- connectors: oracle (python-oracledb), ssh (paramiko), http (httpx + zeep)
   |
PostgreSQL (app data)      Worker pool (thread pool, per-resource semaphores)
Master key file / env      APScheduler (in-process, DB job store)
LLM endpoint (external)
```

Rules:
- Every route goes through `authz.require(user, permission, resource?)`. No route trusts client-supplied ownership.
- Connectors are the only code that sees decrypted secrets; they receive them via `secrets.open(resource)` and never log them.
- The LLM agent calls the same service layer as the UI, with the caller's identity (no service account).

### 1.1 Repository layout
```
backend/app/
  main.py  config.py  db.py
  auth/        (providers/local.py, sessions.py, passwords.py)
  authz/       (permissions.py, acl.py)
  secrets/     (vault.py)            # AES-GCM envelope encryption
  resources/   (models.py, router.py, connectors/{oracle,ssh,http}.py)
  views/       (registry.py, base.py, ssh_health/, db_table/, api_response/, trend/)
  scheduler/   (jobs.py, cache.py)
  dashboards/  links/  llm/  backup/  audit/
  migrations/  (alembic)
backend/tests/
frontend/src/
  pages/ components/ renderers/ api/ chat/
deploy/ docker-compose.yml  nginx.conf  .env.example
tools/migrate_serverlist.py    # one-off import of serverlist_update
```

## 2. Data model (PostgreSQL)

All tables have `id uuid pk`, `created_at`, `updated_at`. `owner_id` columns reference `users.id`.

| Table | Key columns |
|-------|-------------|
| users | login (unique, citext), display_name, password_hash, must_change_pw bool, is_admin bool, is_active bool, failed_attempts int, locked_until ts, last_login ts, totp_secret_enc (nullable) |
| groups | name (unique), description |
| user_groups | user_id, group_id (pk pair) |
| group_permissions | group_id, permission text, scope_id uuid null (for `panel.access`) |
| user_permissions | same as group_permissions but per user |
| sessions | id (random 256-bit, stored hashed), user_id, csrf_token, expires_at, last_seen, ip, user_agent |
| resources | type enum(`ssh`,`oracle`,`api`), name, tags text[], owner_id, config jsonb (non-secret), secret_blob bytea (encrypted), key_version int, llm_allowed bool default false, status enum(up,down,unknown), last_checked_at, last_error |
| resource_acl | resource_id, principal_type (user/group), principal_id, level enum(view,use,edit,manage) |
| views | type text, name, owner_id, config jsonb, source_resource_id null, source_view_ids uuid[], renderer text, schedule_cron null, cache_ttl_s int, llm_allowed bool |
| view_acl | same shape as resource_acl |
| view_runs | view_id, started_at, duration_ms, status, error, row_count, result jsonb (capped), triggered_by (schedule/user/llm) |
| dashboards | name, owner_id, layout jsonb, share_token_hash null, share_expires_at null |
| dashboard_widgets | dashboard_id, view_id, renderer, grid jsonb (x,y,w,h), options jsonb |
| dashboard_acl | same shape as resource_acl |
| link_tiles | name, url, tags, credential_mode enum(none,shared,per_user), shared_secret_blob, owner_id |
| link_user_credentials | tile_id, user_id, secret_blob |
| llm_config | singleton row: base_url, model, api_key_enc, temperature, max_tokens, timeout_s, system_prompt, tool_mode(native/json), enabled_groups uuid[] |
| llm_conversations / llm_messages | user_id, title / conversation_id, role, content, tool_calls jsonb |
| llm_audit | user_id, conversation_id, tool, args jsonb, resource_ids, row_count, at |
| audit_log | at, actor_id, action, target_type, target_id, ip, detail jsonb (no secrets) |
| settings | key, value jsonb |
| backups | filename, size, created_at, kind(manual/scheduled), status |

Indexes: `resource_acl(principal_id)`, `view_runs(view_id, started_at desc)`, `audit_log(at desc)`, `resources(tags gin)`.

## 3. Authentication & sessions

- `POST /api/auth/login {login,password}` -> sets `sid` cookie (HttpOnly, Secure, SameSite=Strict, 8h absolute, 30m idle) and returns `{user, csrf_token}`.
- Passwords: argon2id; min 12 chars; reject top common passwords. Admin-created accounts get a one-time temp password and `must_change_pw=true`; all routes except `change-password`/`logout` return 403 `PASSWORD_CHANGE_REQUIRED` until changed.
- Lockout: 5 consecutive failures -> `locked_until = now+15m`; same generic error for unknown user/bad password; constant-time compare; login rate limit 10/min/IP.
- State-changing requests require header `X-CSRF-Token`.
- `AuthProvider` interface: `authenticate(login, password) -> ExternalIdentity | None`. v1 ships `LocalProvider`; `LdapProvider` later maps LDAP groups to app groups.
- Optional TOTP (later): `totp_secret_enc`, verify step after password.

## 4. Authorization

### 4.1 Permission catalog
`admin.users`, `admin.groups`, `admin.settings`, `admin.backup`, `admin.llm`, `admin.audit`,
`inventory.ssh.create`, `inventory.oracle.create`, `inventory.api.create`,
`view.ssh_health.create`, `view.ssh.custom`, `view.db_table.create`, `view.api_response.create`, `view.trend.create`,
`dashboard.create`, `link.create`, `panel.access` (scope = link tile or dashboard id), `llm.use`.
`is_admin` implies all.

### 4.2 Resolution
`can(user, perm, obj=None, level=None)`:
1. admin -> allow.
2. Capability perms (`*.create`, `llm.use`): allowed if in `user_permissions` or any of the user's `group_permissions`.
3. Object access: owner has `manage`; else max level over `resource_acl`/`view_acl`/`dashboard_acl` rows matching the user or any of their groups. `level >= required` (view < use < edit < manage).
4. Default deny. Result cached per request only.

Level meaning: **view** = see metadata and cached data; **use** = run it / use as a source for views; **edit** = change config; **manage** = edit + share + delete.

Derived rules:
- Creating a view requires `use` on its source resource(s)/view(s). A trend view requires `use` on its source view; a user who later loses access to it can no longer run the trend view (the check is re-evaluated on every run).
- Viewing a dashboard shows widgets only for views the viewer has `view` on; others render a "no access" placeholder (data never leaves the server).
- Dashboard share links (`share_token`) grant read-only access to the dashboard's *cached* results only; they never trigger live connector calls.

## 5. Secrets

- Envelope encryption: random 256-bit DEK per secret, AES-256-GCM; DEK wrapped by the master key (`MASTER_KEY` from file path or env; KMS/Vault adapter later). Blob = `key_version | wrapped_dek | nonce | ciphertext | tag`. AAD = resource id.
- Rotation: `POST /api/admin/rotate-key` re-wraps DEKs in a background job; `key_version` tracks progress.
- Secret fields are write-only in the API (`"password": {"set": true}` on read). Update by sending a new value; omit to keep.
- Reveal (link credentials only): `POST /api/links/{id}/reveal` returns secret once, writes audit entry, rate limited.
- Logging filter redacts keys named `password|secret|token|key|authorization`.

## 6. Resources (inventory)

### 6.1 Common API
```
GET    /api/resources?type=&tag=&q=          # only those user has view+ on
POST   /api/resources                         # needs inventory.<type>.create
GET    /api/resources/{id}
PATCH  /api/resources/{id}                    # edit
DELETE /api/resources/{id}                    # manage; blocked if views depend (409 with list)
POST   /api/resources/{id}/test               # use; runs connector self-test, updates status
GET/PUT /api/resources/{id}/acl               # manage
POST   /api/resources/{id}/transfer {user_id} # manage or admin
```

### 6.2 Per-type config (`config` jsonb) and secrets

**ssh**
```json
{ "host":"10.0.0.5","port":22,"username":"app","auth":"password|key|local",
  "host_key_fingerprint":"SHA256:...","connect_timeout_s":10 }
```
Secrets: `password` or `private_key` + `passphrase`. `auth=local` means "use the known_hosts / agent of the app server's account" and has no secret. First successful connect stores the fingerprint (TOFU); a later mismatch marks the resource `down` with error `HOST_KEY_CHANGED` and requires manage-level re-trust.

**oracle**
```json
{ "host":"...","port":1521,"service_name":"CRMDB","username":"RO_USER","fetch_size":500,"call_timeout_ms":30000 }
```
Secret: `password`. Connect through `python-oracledb` thin mode with DSN `host:port/service_name`; never concatenate credentials into a string. Self-test: `SELECT 1 FROM dual`.

**api**
```json
{ "kind":"rest|soap","base_url":"https://...","allowed_methods":["GET","POST"],
  "default_headers":{}, "auth":"none|basic|bearer|api_key","api_key_header":"X-Key",
  "_note":"fixed per-resource; request data (path/query/body) are variables defined in the view (see 7.3 V3)",
  "wsdl_url":null,"timeout_s":15,"verify_tls":true }
```
Secrets: basic password / bearer token / api key. Only GET and POST accepted (enforced in connector, not just UI). SSRF guard: resolve host, reject loopback/link-local/metadata ranges unless admin sets an allow-list in settings. Response size cap 5 MB.

### 6.3 Status
Scheduler runs `test` for every resource every 5 min (configurable) with jitter; status and `last_error` (sanitised) are stored. Status feeds the home heat-map.

## 7. Views

### 7.1 Plugin interface
```python
class ViewType(Protocol):
    key: str                          # "db_table"
    permission: str                   # "view.db_table.create"
    source_kinds: list[str]           # ["oracle"]
    config_schema: type[BaseModel]    # pydantic; also exported as JSON Schema to the UI
    default_renderer: str
    async def run(ctx: RunContext, cfg) -> ResultSet
    def llm_tool_schema(view) -> dict # JSON schema for the tool arguments
```
Registered in `views/registry.py` by import; the UI fetches `GET /api/view-types` (keys, JSON Schemas, default renderer) to build forms generically. A new type = a new folder; no changes elsewhere.

### 7.2 ResultSet (the only cross-plugin contract)
```json
{ "columns":[{"name":"ERROR","type":"string"},{"name":"COUNT","type":"number"}],
  "rows":[["ORA-00001...",12]],
  "meta":{"row_count":1,"truncated":false,"duration_ms":412,"generated_at":"...","source":"resource-uuid"} }
```
Types: `string|number|boolean|datetime|json`. Hard cap `MAX_ROWS=10000` (view can set lower); `truncated=true` when cut.

### 7.3 Types

**V1 ssh_health** - config `{checks:[{id:"disk",args:{mount:"/"}}], custom:[{name,command}]}`. Check catalog (server-side templates, args validated by regex/enum): `cpu_load` (`cat /proc/loadavg`), `mem` (`free -m`), `disk` (`df -P {mount}`), `uptime`, `process` (`pgrep -fc {name}`), `port` (`ss -ltn`). Parsers turn output into rows `(check, metric, value, unit, status)`; thresholds (warn/crit) in config drive `status`. `custom` commands require `view.ssh.custom` and run via `exec_command` with timeout and 64 KB output cap; no PTY, no stdin.

**V2 db_table** - config `{mode:"sql"|"table", sql, params:[{name,type,default}], table, columns, filters, row_limit, timeout_s}`.
- SQL validation (`sqlglot`, Oracle dialect): exactly one statement, root must be `SELECT` (or `WITH ... SELECT`); reject `INTO`, DML/DDL, `DBMS_*`/`UTL_*` calls, `FOR UPDATE`, db links. Parameters only via named binds (`:today`); `{{today}}` style macros in config are converted to binds (`today` = `TRUNC(SYSDATE)`), never string-substituted.
- Execution wraps as `SELECT * FROM (<sql>) WHERE ROWNUM <= :max_rows`, sets `call_timeout`, uses a read-only transaction.
- Built-in template **"Daily errors"**: the query from `get_error.py` rewritten with a range predicate (`req_start_time >= TRUNC(SYSDATE) AND req_start_time < TRUNC(SYSDATE)+1`, index-friendly) instead of `to_char(...)` comparison, same grouping on `TRANSLATE(smessage,...)`, columns `SYSTEM, REQ_USER, ERROR, COUNT`.
- A view may be **fanned out** to many Oracle resources (`source_resource_ids`), producing one ResultSet per source plus a merged one with a `SOURCE` column; this is what replaces the ~60 per-service CSVs.

**V3 api_response** - config `{method, path, query_template, headers, body_template, soap:{operation, envelope_template}, variables:[...], mapping:{root:"$.items[*]", fields:[{name, path, type}]}}`. JSONPath via `jsonpath-ng`; SOAP via `zeep` with XPath mapping. Output rows follow `mapping`; if no mapping, a 2-column key/value flatten of the response.

*Fixed vs variable parts.* The **resource** holds the fixed part: base URL, auth, default headers (set once by the owner). The **view** holds the request shape with **variables** in `{{name}}` placeholders in `path`, `query_template`, `body_template` (JSON/XML/form) and `envelope_template`. The caller supplies values at run time; the request only runs once every required variable has a valid value.

Variable definition (shared by V2 SQL params, so UI and LLM treat both the same):
```json
{ "name":"customer_id", "label":"Customer ID", "type":"string|number|boolean|date|enum",
  "required":true, "default":null, "enum":["A","B"], "pattern":"^[0-9]{10}$",
  "min":null, "max":null, "max_length":64, "description":"10-digit MSISDN" }
```
Rules:
- Placeholders are substituted by the engine after validation, **never by raw string concat**: values are URL-encoded in path/query, JSON-encoded inside JSON bodies (template is parsed as JSON first, values set as typed leaves), XML-escaped in SOAP envelopes. A variable cannot change the host, scheme, method, auth or headers (header variables only allowed if explicitly declared and restricted by `pattern`).
- Missing required value -> `422 VALIDATION` listing the missing variables; unknown variables are rejected.
- The UI auto-builds a **run form** from `variables`; "Test with sample values" is available while editing the view. Last-used values may be remembered per user (never for fields marked `sensitive:true`).
- Dashboard widgets bind variable values (fixed, or linked to a dashboard-level filter) so a saved dashboard can still run parameterised views.
- Scheduled runs use `default` values (or saved "schedule parameters"); a scheduled view with a required variable lacking a default is rejected at save time.
- Cache key = view id + hash of variable values.


**V5 trend** - config `{source_view_id, value_column, group_by, aggregation:"sum|avg|max|count", window_days}`. Reads `view_runs` history of the source view; requires the source to be scheduled. Output: `(timestamp, series, value)`.

### 7.4 Renderers (frontend, independent of view type)
`table` (TanStack, sort/filter/page, CSV export), `kv_cards`, `chart_line`, `chart_bar`, `status_badge` (maps a column value to green/amber/red via config), `raw_json`. A renderer declares the ResultSet shapes it accepts; the UI only offers compatible ones.

### 7.5 Execution & caching
- `POST /api/views/{id}/run` (use) -> returns ResultSet; if a cached run younger than `cache_ttl_s` exists and `?fresh=1` is absent, returns it with `meta.cached=true`.
- Scheduled views (`schedule_cron`): APScheduler job runs the view as its owner, stores a `view_runs` row; results older than TTL show a "stale" flag. Retention: keep results for 30 days, metadata 1 year (settings).
- Concurrency: per-resource semaphore (default 2 for Oracle, 4 for SSH/API); global worker pool 20; each run has a hard timeout; overlapping runs of the same view are coalesced.
- Failures are recorded (`status=error`, sanitised message). Never swallow silently: this replaces the bare `except: pass` in `get_error.py`.

## 8. Dashboards & link panel

- `GET/POST /api/dashboards`, `GET/PATCH/DELETE /api/dashboards/{id}`, `PUT /api/dashboards/{id}/acl`, `POST /api/dashboards/{id}/share-link {expires_in}`.
- Layout: 12-column grid; widget = `{view_id, renderer, grid:{x,y,w,h}, options, refresh_s}`. Widgets fetch `GET /api/views/{id}/result` (cached) and optionally trigger a run if the user has `use`.
- Home page is a system dashboard per user (`dashboards.kind='home'`) seeded with the widgets listed in PRD 3.5; backed by dedicated endpoints: `GET /api/home/summary` (resource status counts, failed runs), `/api/home/heatmap` (resources grouped by tag), `/api/home/top-errors` (uses the "Daily errors" view results the user can access), `/api/home/activity`.
- Link tiles: `GET/POST/PATCH/DELETE /api/links`. Click -> `POST /api/links/{id}/launch` returns `{url, credential_hint}`; UI opens URL in a new tab (`rel="noopener noreferrer"`) and shows copy buttons for username/password when a credential exists (reveal is audited). `per_user` mode: `PUT /api/links/{id}/my-credential`; missing credential prompts the user to add one. URLs must be `http(s)` only. Proxy-injection mode is out of scope for v1.

## 9. LLM chatbot ("Sensei")

### 9.1 Admin config
`GET/PUT /api/admin/llm` (admin.llm): base_url, model, api key, temperature, max_tokens, timeout_s, system prompt, `tool_mode` (`native` | `json`), enabled groups, per-user rate limit (default 20 msg/10 min), max tool calls per turn (default 6), max result rows sent to model (default 50), max chars per tool result (default 8000). `POST /api/admin/llm/test` sends a ping and a tool-call probe.

### 9.2 Chat API
```
POST /api/llm/conversations                       -> {id}
GET  /api/llm/conversations                       # own only
POST /api/llm/conversations/{id}/messages {text, context?:{view_id|dashboard_id}}
     -> SSE stream: event types  token | tool_call | tool_result | sources | done | error
DELETE /api/llm/conversations/{id}
```
Requires `llm.use` and membership in an enabled group.

### 9.3 Agent loop
1. Build messages: system prompt + safety preamble (below) + history (token-trimmed) + user text (+ optional "summarise this" context).
2. Tool list is built per request from what the user can access: always `list_resources`, `list_views`; plus `run_view`, `get_view_summary`, `query_view_table`. Views/resources with `llm_allowed=false` are omitted. Each view's `llm_tool_schema` is used to describe its parameters.
3. Loop (max N tool calls): model returns tool call -> server validates args against the schema -> `authz.can(user, 'use', view)` -> execute via the view engine (cache preferred) -> ResultSet is reduced (`reduce()`: top rows, column stats, truncation note, char cap) -> appended as tool message -> model continues.
4. Stream tokens; emit `sources` listing every view/resource touched; persist messages and an `llm_audit` row per tool call.

**Variables and the LLM.** Each view's `variables` list is turned into the JSON Schema of that view's tool arguments (`llm_tool_schema`): name, type, enum, pattern, required, description. So the model sees e.g. `run_view(view_id=..., params={"customer_id": string, required})`.
- If the user's message doesn't give a required value, the model must **ask the user** for it instead of guessing; the server enforces this by rejecting the call with `MISSING_PARAMS: [customer_id]`, which is returned to the model as the tool result.
- Values chosen by the model go through the exact same validation and safe substitution as values typed in the UI (type, enum, pattern, length), so the LLM cannot inject extra headers, change the URL/method, or alter SQL. Out-of-schema values -> `VALIDATION` error returned to the model.
- Variables marked `sensitive:true` or `llm_fillable:false` cannot be set by the model; the chat UI shows an inline form for the user to enter them, and the value never enters the prompt.
- The `tool_call` SSE event shows the user the exact params used (shown in "sources" chips), and `llm_audit` stores them (sensitive ones masked).

Tools:
| Tool | Args | Returns |
|------|------|---------|
| `list_resources` | `tag?`, `type?` | id, name, type, status (no config/secrets) |
| `list_views` | `tag?`, `type?` | id, name, type, description, params |
| `run_view` | `view_id`, `params` (validated against that view's `variables`) | reduced ResultSet, or `MISSING_PARAMS` / `VALIDATION` error |
| `get_view_summary` | `view_id` | row_count, column stats, last run time, top N rows |
| `query_view_table` | `view_id`, `filters[]`, `order_by?`, `limit<=50` | filtered rows from the cached/just-run ResultSet (in-memory filter, never SQL) |

`tool_mode=json` fallback: system prompt instructs the model to reply with `{"tool":..., "args":...}` blocks; the server parses, executes and loops identically.

### 9.4 Safety
- Identity: all calls run as the requesting user; cross-user leakage impossible because tool results are not shared between conversations.
- No write tools, no raw SQL/shell/HTTP tool; the model can only run saved views.
- Tool output is wrapped (`<tool_result untrusted="true">...`) and the preamble tells the model to treat it as data and ignore instructions inside it. Tool args are re-validated server-side regardless of model output.
- Credentials never enter any prompt or tool result; `reduce()` drops columns named like secrets.
- Rate limits and token caps as above; every message/tool call audited.

## 10. Backup & settings

- `POST /api/admin/backup` (manual) and cron (default daily 02:00, retain 14) produce `backup-<ts>.tar.age` containing: `export.json` (users minus password hashes optional, groups, permissions, ACLs, resources, views, dashboards, link tiles, llm_config, settings), `secrets.enc` (blobs re-wrapped with a **backup passphrase** so the archive restores on a host with a different master key; option to exclude), and `manifest.json` (schema version, counts, sha256 per file).
- Encrypted with `age` using admin-supplied passphrase/recipient. Files stored under `BACKUP_DIR`, downloadable by admin only.
- Restore: `POST /api/admin/restore` upload -> **dry-run** returns diff (to create/update/skip counts) -> confirm applies in one transaction. Refuses if manifest schema version is newer than the app. Restore itself is audited.
- Also documented: `pg_dump` of the app DB in the compose setup for full-fidelity DR.

## 11. Audit

Append-only `audit_log`. Events: login success/fail/lockout, password change, user/group/permission change, resource create/update/delete/test, ACL change, secret reveal, view run (user-triggered), dashboard share-link created, backup/restore, LLM config change, every LLM tool call. Admin UI: filter by actor/action/date, export CSV. Retention default 1 year.

## 12. API conventions

- JSON, REST, prefix `/api`; OpenAPI served at `/api/docs` (admin only in prod).
- Errors: `{ "error": { "code": "FORBIDDEN", "message": "...", "details": {} } }`; codes: `UNAUTHENTICATED, FORBIDDEN, NOT_FOUND, VALIDATION, CONFLICT, RATE_LIMITED, UPSTREAM_ERROR, TIMEOUT, PASSWORD_CHANGE_REQUIRED`. Upstream error text is sanitised (no hosts/credentials).
- Pagination: `?limit=&cursor=`; list endpoints return only items the caller can see (never 403 on list).
- Validation by pydantic; unknown fields rejected.

## 13. Security checklist

TLS only (HSTS); CSP without inline scripts; CSRF token; cookie flags as in section 3; per-IP and per-user rate limits; SSRF guard for API resources; SQL parser allow-list; SSH command templates with validated args (no shell interpolation of raw input); output size and time caps everywhere; dependency pinning + `pip-audit`/`npm audit` in CI; no secrets in logs or error messages; admin bootstrap via `python -m app.cli create-admin`; default deny.

## 14. Deployment

`docker-compose.yml`: `app` (uvicorn, 2 workers - scheduler runs in a single dedicated `scheduler` container to avoid duplicate jobs), `db` (postgres), `web` (nginx serving built SPA + proxy). Config via env: `DATABASE_URL`, `MASTER_KEY_FILE`, `SESSION_SECRET`, `BACKUP_DIR`, `ALLOWED_ORIGINS`. Health: `/healthz` (process), `/readyz` (DB reachable). Logo and fonts bundled (no external requests). Oracle thin mode needs no client install.

## 15. Migration from current files

`tools/migrate_serverlist.py`:
1. Read `serverlist_update` (`user,password,ip,port,service`).
2. Create one `oracle` resource per line (name = service, tag from a mapping file derived from the categories in `index.html`: 5G, CEIR, DARTS, GM, M2M, PACS WEST/NORTH/SOUTH, POSTPAID, UD, GM_ACT_1/2, GM_deact, VOLTE, New_Postpaid, Vas, DARTs_FULFILLMENT; a server in several categories gets several tags).
3. Create the "Daily errors" `db_table` view fanned out to all imported resources, scheduled (e.g. every 10 min), and a "Servers Panel" dashboard grouping by tag.
4. Print a report (created/failed) and remind to rotate the old plaintext passwords and delete `serverlist_update`.
Equivalence check: for one day, the merged ResultSet per service must match the CSV that `get_error.py` produced (same rows/counts).

## 16. Testing

| Level | What |
|-------|------|
| Unit | permission resolution matrix (user/group/ACL/level, deny default); SQL validator accept/reject corpus; secret round-trip + tamper detection (AAD); ResultSet join; reduce(); SSH check parsers |
| Integration | Oracle XE container (view run, timeout, row cap); OpenSSH container (health view, host-key change); mock REST + SOAP server; Postgres migrations up/down; backup -> restore on empty DB |
| Security | secrets absent from API responses and logs; LLM cannot invoke a view user lacks `use` on; prompt-injection fixture in a DB row does not trigger extra tool calls; CSRF/lockout/rate-limit tests |
| E2E (Playwright) | admin creates user+group -> user creates oracle resource -> db_table view -> dashboard -> shares to viewer -> viewer sees only shared; chat summarises a view with stubbed LLM |
| Performance | 10k-row result renders paged < 1 s; 60 scheduled Oracle views complete within refresh interval with semaphores |

## 17. Delivery milestones (maps to PRD section 6)

| Milestone | Scope | Done when |
|-----------|-------|-----------|
| M1 Foundation | sections 3, 4, 5, 10, 11, 12 + skeleton UI (login, admin users/groups, audit) | admin can create user+group, grant permissions, backup/restore works on empty DB |
| M2 Inventory & views | sections 6, 7 (V1-V3), scheduler, section 15 migration | all ~60 Oracle servers imported; daily-errors view matches old CSVs |
| M3 Dashboards | section 8 | home page + shared dashboards + link panel working |
| M4 Trend & LLM | V5, section 9 | chatbot answers "summarise today's errors for POSTPAID" using tools, with sources |
| Later | LDAP/SSO, TOTP, alerting, proxy-injected panel login, web terminal | - |

## 18. Open items still needing owner input
1. Confirm assumptions A1-A7.
2. Which Oracle objects/columns may views read (to pre-approve templates)?
3. Retention periods and backup storage location.
4. Alert channels (mail/Teams/SMS) if alerting is pulled into v1.
