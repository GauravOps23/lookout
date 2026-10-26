# Lookout: deploy and update on RHEL 8

One script does everything, for both the first install and every later update:

```bash
sudo ./deploy/lookout.sh
```

Not yet run on a real host (see README "what is and is not verified"). Try it on staging first.

## Before you start

You already have Python 3.11, pip and nginx. You also need a **PostgreSQL 16 database** that the app can reach. If you don't have one yet, on the DB server:

```bash
sudo -u postgres psql -c "CREATE ROLE lookout LOGIN PASSWORD 'YOUR_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE lookout OWNER lookout ENCODING 'UTF8';"
```

(Use `openssl rand -hex 16` for the password. Local TCP logins must use `scram-sha-256` in `pg_hba.conf`; for a separate DB server also set `listen_addresses`, add a `host lookout lookout <APP_IP>/32 scram-sha-256` line and open port 5432 for the app server only.)

## First install

1. Put the project on the server (git clone, or upload the folder with WinSCP). Don't include `backend/.env`, `backend/dev.db`, or `serverlist_update`; the script skips them anyway.
2. Run it from inside the project:

```bash
cd lookout
DATABASE_URL='postgresql+psycopg://lookout:YOUR_PASSWORD@127.0.0.1:5432/lookout' \
SERVER_NAME=lookout.yourcompany.com \
sudo -E ./deploy/lookout.sh
```

Both variables are optional: without `DATABASE_URL` it asks; without `SERVER_NAME` nginx answers on any name.

It will: copy the code to `/opt/lookout`, create the venv and install packages, generate the master key, write the settings, run the migrations, ask you for the `admin` password, install and start the API and scheduler services, set up nginx with HTTPS, and check `/readyz`.

**No new Linux user is created.** The app runs as the user who ran `sudo`. If you want a dedicated unprivileged user instead, add `RUN_USER=lookout` (it is created for you).

Afterwards:
- **Back up `/etc/lookout/master_key`** somewhere separate from DB backups (password manager). Without it, stored credentials can't be read.
- With no certificate present the script makes a self-signed one (browser warning). For a real one, put the files at `/etc/pki/tls/certs/lookout.crt` and `/etc/pki/tls/private/lookout.key` **before** the first run, or edit `/etc/nginx/conf.d/lookout.conf` later and `sudo systemctl reload nginx`. HTTPS is required: the login cookie is `Secure`.
- Run **one** scheduler on one server only.

## Update

Get the new code (`git pull`, or upload over the folder), then:

```bash
sudo ./deploy/lookout.sh
```

Same command. It copies the new code, installs any new packages, runs new migrations, restarts the services, and checks health. Settings, master key, nginx config and the database are left alone. Take a DB backup first when the release has migrations:

```bash
sudo -u postgres pg_dump -Fc -d lookout -f lookout-$(date +%F).dump
```

## Useful options

| Variable | Default | Meaning |
|---|---|---|
| `RUN_USER` | user who ran sudo | Linux user the services run as |
| `APP_DIR` | `/opt/lookout` | where the code is installed |
| `SERVER_NAME` | any | nginx host name (first install only) |
| `PYTHON` | `python3.11` | Python binary |

Settings live in `/etc/lookout/lookout.env` (`DATABASE_URL`, `MASTER_KEY_FILE`, `BACKUP_DIR`; optional `SESSION_IDLE_MINUTES`, `SESSION_ABSOLUTE_HOURS`, `LOGIN_RATE_PER_MINUTE`, `MAX_FAILED_ATTEMPTS`, `LOCKOUT_MINUTES`). After editing: `sudo systemctl restart lookout-api lookout-scheduler`. Don't set `COOKIE_SECURE=false` in production.

## When something is wrong

| What | Command |
|---|---|
| API log | `sudo journalctl -u lookout-api -n 100 --no-pager` |
| Scheduler log | `sudo journalctl -u lookout-scheduler -n 100 --no-pager` |
| nginx errors | `sudo tail -50 /var/log/nginx/error.log` |
| Status | `sudo systemctl status lookout-api lookout-scheduler nginx` |

- `/readyz` fails: the DB URL is wrong or the DB is unreachable. Test with `PGPASSWORD=... psql -h HOST -U lookout -d lookout -c 'select 1'`, fix `/etc/lookout/lookout.env`, restart the services.
- `502 Bad Gateway`: the API isn't running, or SELinux blocks nginx (`sudo setsebool -P httpd_can_network_connect 1`).
- `Ident authentication failed` / `no pg_hba.conf entry`: fix `pg_hba.conf` as described above, then `sudo systemctl reload postgresql-16`.

## Network paths Lookout needs (outbound from the app server)

Oracle listeners (1521), SSH resources (22; the first connection pins the host key), API resources (443/80, private addresses blocked by the SSRF guard, see README), and the LLM endpoint if you use Sensei. Check one with:

```bash
timeout 5 bash -c '</dev/tcp/ORACLE_HOST/1521' && echo OPEN || echo BLOCKED
```

## Optional

- Import the old server list: `cd /opt/lookout/backend && sudo -u <RUN_USER> bash -c 'set -a; . /etc/lookout/lookout.env; set +a; .venv/bin/python ../tools/migrate_serverlist.py --dry-run --serverlist /path/to/serverlist_update --index-html /opt/lookout/index.html --owner admin'`. Keep `serverlist_update` outside `/opt/lookout`. Drop `--dry-run` once the output looks right.
- Nightly DB backup (DB server, postgres crontab): `0 2 * * * /usr/pgsql-16/bin/pg_dump -Fc -d lookout -f /var/backups/lookout/lookout-$(date +\%F).dump`. Restore: `pg_restore -d lookout --clean --if-exists FILE`.
- Containers: `deploy/docker-compose.yml` exists but has never been run (and pins `postgres:15`).
