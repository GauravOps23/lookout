# Lookout: deploy and update on RHEL 8 (step by step)

Run every command **as your normal user**. If a step says `Permission denied` or `must be root`, run the **"If it fails"** line for that step (it adds `sudo`). Run the blocks in order, in one SSH session, so the variables carry over.

Not yet run on a real host (see README "what is and is not verified"). Try it on staging first.

Everything lives under your home folder, so no root is needed except where marked.

## Block 0: Variables (every new SSH session)

```bash
export APP=$HOME/lookout          # code + venv
export CONF=$HOME/lookout-conf    # settings, master key, certificate
export DATA=$HOME/lookout-data    # backups
export PYTHON=python3.11
```

## Block 1: Folders

```bash
mkdir -p $APP $CONF/certs $DATA/backups
chmod 700 $CONF
```

If it fails (e.g. you chose `/opt/lookout`): `sudo mkdir -p /opt/lookout && sudo chown -R $USER /opt/lookout`, then set `APP=/opt/lookout` in Block 0.

## Block 2: Copy the code

Run from the folder that holds the project on the server (after `git clone` or a WinSCP upload):

```bash
cd ~/errorpanel        # your upload/clone folder
tar --exclude=.venv --exclude=.env --exclude='*.db' --exclude=__pycache__ \
    --exclude=.git --exclude=serverlist_update -cf - . | tar -C $APP -xf -
ls $APP/backend/app/main.py
```

If it fails: `sudo tar ... | sudo tar -C $APP -xf -` then `sudo chown -R $USER $APP`.

## Block 3: Packages

Check what you have:

```bash
$PYTHON --version      # need 3.11
nginx -v               # optional, only for Block 9 (port 443)
```

If `python3.11` is missing (needs root):

```bash
sudo dnf install -y python3.11 python3.11-pip
```

Python packages (no root, inside a venv):

```bash
cd $APP/backend
$PYTHON -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

If pip fails building a package (`gcc` / `Python.h` / `libffi` errors):

```bash
sudo dnf install -y gcc python3.11-devel libffi-devel openssl-devel
```

then repeat the `pip install` line. If the server has no internet, use your internal mirror: `.venv/bin/pip install -r requirements.txt -i https://YOUR_MIRROR/simple`.

## Block 4: Database (PostgreSQL 16)

You need a PostgreSQL database the app can reach. Creating a role and database needs a DB admin. Try as yourself first (replace HOST, ADMINUSER):

```bash
PW=$(openssl rand -hex 16); echo "DB password: $PW"      # save this
psql -h HOST -U ADMINUSER -d postgres -c "CREATE ROLE lookout LOGIN PASSWORD '$PW';"
psql -h HOST -U ADMINUSER -d postgres -c "CREATE DATABASE lookout OWNER lookout ENCODING 'UTF8';"
```

If it fails (not allowed, or Postgres is on this same server): use the postgres OS user, via root:

```bash
sudo -u postgres psql -c "CREATE ROLE lookout LOGIN PASSWORD '$PW';"
sudo -u postgres psql -c "CREATE DATABASE lookout OWNER lookout ENCODING 'UTF8';"
```

If you can't get admin access at all, ask your DBA for: a role `lookout` and an empty database `lookout` owned by it, and its password.

For a separate DB server, the DBA must also allow the app server: `host lookout lookout <APP_IP>/32 scram-sha-256` in `pg_hba.conf`, then `sudo systemctl reload postgresql-16`.

Test the connection (must print `1`):

```bash
PGPASSWORD="$PW" psql -h HOST -U lookout -d lookout -c 'select 1'
```

## Block 5: Settings and master key

```bash
cd $APP/backend
.venv/bin/python -m app.cli gen-master-key > $CONF/master_key
chmod 600 $CONF/master_key

cat > $CONF/lookout.env <<EOF
DATABASE_URL=postgresql+psycopg://lookout:$PW@HOST:5432/lookout
MASTER_KEY_FILE=$CONF/master_key
BACKUP_DIR=$DATA/backups
EOF
chmod 600 $CONF/lookout.env
```

Replace `HOST` (use `127.0.0.1` if Postgres is on this server). If the password has special characters (`@ : / %`), URL-encode them.

**Back up `$CONF/master_key` somewhere separate from DB backups** (password manager). Without it, stored credentials can't be read.

Optional settings you can add to `lookout.env`: `SESSION_IDLE_MINUTES`, `SESSION_ABSOLUTE_HOURS`, `LOGIN_RATE_PER_MINUTE`, `MAX_FAILED_ATTEMPTS`, `LOCKOUT_MINUTES`. Don't set `COOKIE_SECURE=false` in production.

## Block 6: Database tables and admin user

```bash
cd $APP/backend
set -a; . $CONF/lookout.env; set +a
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli create-admin admin      # asks for a 12+ character password
```

If `alembic` can't connect, go back to the Block 4 connection test and fix `DATABASE_URL`. Nothing in this block needs root.

## Block 7: HTTPS certificate

The login cookie is `Secure`, so the app must be served over HTTPS. Self-signed for now (browsers will warn):

```bash
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout $CONF/certs/lookout.key -out $CONF/certs/lookout.crt \
  -subj "/CN=$(hostname -f)"
chmod 600 $CONF/certs/lookout.key
```

For a company certificate, copy your `.crt` and `.key` over those two files.

## Block 8: Start the app (no root, port 8443)

Ports below 1024 need root, so as a user the app listens on **8443** with TLS directly. Create two start scripts:

```bash
cat > $CONF/run-api.sh <<EOF
#!/usr/bin/env bash
cd $APP/backend
set -a; . $CONF/lookout.env; set +a
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8443 \\
  --ssl-keyfile $CONF/certs/lookout.key --ssl-certfile $CONF/certs/lookout.crt --workers 2
EOF
cat > $CONF/run-scheduler.sh <<EOF
#!/usr/bin/env bash
cd $APP/backend
set -a; . $CONF/lookout.env; set +a
exec .venv/bin/python -m app.scheduler
EOF
chmod 700 $CONF/run-api.sh $CONF/run-scheduler.sh
```

Run them as **systemd user services** (stay up after you log out, restart on failure):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/lookout-api.service <<EOF
[Unit]
Description=Lookout API
[Service]
ExecStart=$CONF/run-api.sh
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
EOF
cat > ~/.config/systemd/user/lookout-scheduler.service <<EOF
[Unit]
Description=Lookout scheduler (run exactly one)
[Service]
ExecStart=$CONF/run-scheduler.sh
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now lookout-api lookout-scheduler
```

Keep them running after logout and across reboots (needs root once):

```bash
sudo loginctl enable-linger $USER
```

If `systemctl --user` fails (`Failed to connect to bus`): log out and back in with a full SSH login, or `export XDG_RUNTIME_DIR=/run/user/$(id -u)` and retry. Still failing? Use plain background processes instead (they stop on reboot):

```bash
nohup $CONF/run-api.sh       > $DATA/api.log 2>&1 &
nohup $CONF/run-scheduler.sh > $DATA/scheduler.log 2>&1 &
```

If you are allowed root and want proper system services, use the one-command script instead of this block: `sudo ./deploy/lookout.sh` (installs to `/opt/lookout`, sets up systemd and nginx).

Run **one** scheduler on one server only.

## Block 9: Firewall and port 443 (root, optional)

Open port 8443 so people can reach the app (skip if the network team already allows it):

```bash
sudo firewall-cmd --permanent --add-port=8443/tcp && sudo firewall-cmd --reload
```

To serve on the normal port 443 instead, put nginx in front (root needed). Switch the API to localhost only first: in `$CONF/run-api.sh` change `--host 0.0.0.0` to `--host 127.0.0.1`, drop the two `--ssl-*` options, add `--proxy-headers --forwarded-allow-ips=127.0.0.1`, then `systemctl --user restart lookout-api`. Then:

```bash
sudo tee /etc/nginx/conf.d/lookout.conf >/dev/null <<EOF
server {
    listen 80;
    server_name _;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl http2;
    server_name _;
    ssl_certificate     $CONF/certs/lookout.crt;
    ssl_certificate_key $CONF/certs/lookout.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 5m;
    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
    location /api/llm/ {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }
}
EOF
sudo setsebool -P httpd_can_network_connect 1
sudo chcon -R -t httpd_config_t $CONF/certs 2>/dev/null || true
sudo nginx -t && sudo systemctl enable --now nginx
sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload
```

If nginx can't read the certificate in your home folder (SELinux or folder permissions), copy the two cert files to `/etc/pki/tls/certs/lookout.crt` and `/etc/pki/tls/private/lookout.key` with `sudo cp` and point the config there.

## Block 10: Health check

```bash
curl -sk https://127.0.0.1:8443/readyz        # expect: ready
systemctl --user status lookout-api lookout-scheduler --no-pager
```

Open `https://SERVER:8443/` (or `https://SERVER/` if you did Block 9) and log in as `admin`.

## Update (every later release)

Get the new code onto the server, then:

```bash
export APP=$HOME/lookout CONF=$HOME/lookout-conf DATA=$HOME/lookout-data PYTHON=python3.11
# take a DB backup first when the release has migrations (needs DB access):
PGPASSWORD="$PW" pg_dump -h HOST -U lookout -Fc -d lookout -f $DATA/backups/lookout-$(date +%F).dump

cd ~/errorpanel        # folder with the new code
tar --exclude=.venv --exclude=.env --exclude='*.db' --exclude=__pycache__ \
    --exclude=.git --exclude=serverlist_update -cf - . | tar -C $APP -xf -

cd $APP/backend
.venv/bin/pip install -r requirements.txt
set -a; . $CONF/lookout.env; set +a
.venv/bin/alembic upgrade head
systemctl --user restart lookout-api lookout-scheduler
curl -sk https://127.0.0.1:8443/readyz
```

Settings, master key, certificate and the database are untouched. If a command fails with permission denied, prefix that one with `sudo` (and `sudo chown -R $USER $APP` if root-owned files got in).

## When something is wrong

| What | Command (user first; add `sudo` if denied) |
|---|---|
| API log | `journalctl --user -u lookout-api -n 100 --no-pager` |
| Scheduler log | `journalctl --user -u lookout-scheduler -n 100 --no-pager` |
| Without systemd | `tail -50 $DATA/api.log` |
| nginx errors | `sudo tail -50 /var/log/nginx/error.log` |

- `/readyz` fails: DB URL wrong or DB unreachable. Re-run the Block 4 test, fix `$CONF/lookout.env`, `systemctl --user restart lookout-api lookout-scheduler`.
- `Address already in use`: something else uses 8443. Change `--port` in `$CONF/run-api.sh`.
- Can't reach it from your PC: firewall (Block 9) or network ACL.
- `502 Bad Gateway` (nginx): API not running, or SELinux (`sudo setsebool -P httpd_can_network_connect 1`).
- `Ident authentication failed` / `no pg_hba.conf entry`: DBA must fix `pg_hba.conf` (Block 4), then `sudo systemctl reload postgresql-16`.

## Network paths Lookout needs (outbound from the app server)

Oracle listeners (1521), SSH resources (22; the first connection pins the host key), API resources (443/80, private addresses blocked by the SSRF guard, see README), and the LLM endpoint if you use Sensei. Check one with:

```bash
timeout 5 bash -c '</dev/tcp/ORACLE_HOST/1521' && echo OPEN || echo BLOCKED
```

## Optional

- Import the old server list: `cd $APP/backend && set -a; . $CONF/lookout.env; set +a; .venv/bin/python ../tools/migrate_serverlist.py --dry-run --serverlist /path/to/serverlist_update --index-html $APP/index.html --owner admin`. Keep `serverlist_update` outside `$APP`. Drop `--dry-run` once the output looks right.
- Nightly DB backup (your crontab, `crontab -e`): `0 2 * * * PGPASSWORD=PW pg_dump -h HOST -U lookout -Fc -d lookout -f $HOME/lookout-data/backups/lookout-$(date +\%F).dump`. Restore: `pg_restore -h HOST -U lookout -d lookout --clean --if-exists FILE`.
- All-root alternative: `sudo ./deploy/lookout.sh` does Blocks 1-10 in one go under `/opt/lookout` with system services and nginx.
- Containers: `deploy/docker-compose.yml` exists but has never been run (and pins `postgres:15`).
