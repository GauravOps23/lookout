# Prerequisites

## To run the tests / develop locally

| Need | Version | Notes |
|------|---------|-------|
| Python | 3.11+ (tested on 3.14) | `python --version` |
| pip | recent | bundled with Python |
| Git Bash or any shell | - | only for the shell snippets in the README |

No database or external system is needed for the tests: they use SQLite files in a temp directory and fake
Oracle/SSH/HTTP connectors.

## To run the service for real

| Need | Version | Notes |
|------|---------|-------|
| Linux host | any recent | spec assumption A3 |
| PostgreSQL | 15+ | app data; a dedicated database and user |
| Docker + Docker Compose | 24+ / v2 | optional, for `deploy/docker-compose.yml` |
| TLS reverse proxy | nginx or caddy | **required**: the session cookie is `Secure`, so login only works over HTTPS |
| Master key | 32 random bytes, base64 | `python -m app.cli gen-master-key`; keep it outside the DB and back it up separately. Losing it makes every stored resource credential unrecoverable |
| Backup passphrase | 12+ characters | chosen by the admin per backup; losing it makes that archive unreadable |
| One scheduler process | - | `python -m app.scheduler` (or the `scheduler` compose service). Run **exactly one** |

## Network access the app host needs (for the resources you add)

| To | Port (typical) | Used by |
|----|----------------|---------|
| Each Oracle database | 1521 | `oracle` resources. `python-oracledb` thin mode: no Oracle client install |
| Each monitored server | 22 | `ssh` resources |
| Each external API | 443 | `api` resources. Loopback, link-local (cloud metadata) and multicast targets are blocked unless an admin allow-lists them; private RFC1918 ranges are allowed |

## Accounts to prepare

- **Oracle:** a **read-only** database user per target. Views are SELECT-only and run in a read-only transaction, but a
  least-privilege account is the real safeguard. It needs SELECT on `sas_re_Request_message` and `system_name_tmp`
  for the "Daily errors" view.
- **SSH:** a non-root user per server (password, key, or the app account's agent). Health checks only read `/proc`,
  `free`, `df`, `pgrep`, `ss`.
- **Migration from the old panel:** read access to the old `serverlist_update` file and `index.html`. Afterwards rotate
  the passwords that were stored in plaintext and delete the file.

## Not needed yet (later milestones)

- An OpenAI-compatible LLM endpoint such as Ollama or vLLM (M4)
- Node.js / a build toolchain: the UI is plain files served by the API. Only needed if you later replace it with the
  spec's React SPA
- A browser: any current one; the UI needs no outbound internet access

## Open items from the spec (section 18) that need an owner decision

1. Confirm assumptions A1-A7.
2. Which Oracle objects/columns views may read (the "Daily errors" template reads the two tables above).
3. Retention periods and backup storage location (defaults: results 30 days, run history 1 year).
4. Alert channels, if alerting moves into v1.
5. **Legacy query:** `get_error.py` joins `system_name_tmp` with no join condition (a cross join). The migration keeps it
   unchanged so results stay comparable with the old CSVs. Confirm the intended join, then fix the template.
