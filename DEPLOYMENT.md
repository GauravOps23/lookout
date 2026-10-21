# Lookout: production deployment on RHEL 8 with PostgreSQL 16

Layout assumed (change names if you like):

| Item | Value |
|---|---|
| App host | RHEL 8, runs API + scheduler + nginx |
| DB host | PostgreSQL 16 server (same box or separate; both cases covered) |
| App dir | `/opt/lookout` (code in `/opt/lookout/backend`) |
| Service user | `lookout` (no login shell) |
| DB / DB user | `lookout` / `lookout` |
| Secrets dir | `/etc/lookout` (root-owned, group `lookout`, mode 750) |
| Backups | `/var/lib/lookout/backups` |

Not yet run in a real environment (see README "what is and is not verified"). Do a first pass on a staging host.

---

## 1. PostgreSQL 16: create the user and database (first-timer steps)

Background, in plain words:
- PostgreSQL has its own users, called **roles**. They are separate from Linux users.
- Lookout needs one role (we call it `lookout`) and one database (also `lookout`). The role **owns** the database, so it can create the tables itself. It does **not** need superuser (admin) rights.
- You create both once, using `psql`, the PostgreSQL command-line client, while logged in as the Linux user `postgres`.

Do all of 1.1 to 1.5 **on the DB server**, in your PuTTY session, as a user who can run `sudo`.

### 1.1 Check PostgreSQL 16 is installed and running

```bash
sudo systemctl status postgresql-16
```

You should see `active (running)` in green. Press `q` to leave the screen.

- If it says `could not be found`, install it first:
  ```bash
  sudo dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-8-x86_64/pgdg-redhat-repo-latest.noarch.rpm
  sudo dnf -qy module disable postgresql
  sudo dnf install -y postgresql16-server
  sudo /usr/pgsql-16/bin/postgresql-16-setup initdb
  sudo systemctl enable --now postgresql-16
  ```
- If it says `inactive` or `failed`, start it: `sudo systemctl start postgresql-16`, then check again.

### 1.2 Choose a password for the `lookout` role

Generate a safe random one (letters and digits only, so it needs no special handling later):

```bash
openssl rand -hex 16
```

It prints something like `9f3c1a7e5b2d4c60a81f3e7d92b4c5a0`. **Copy it into a notepad now.** You need it again in section 3 (`lookout.env`) and for the test in 1.5. Below it is written as `YOUR_PASSWORD`; replace that word with your real one (keep the single quotes).

### 1.3 Open the PostgreSQL prompt

```bash
sudo -u postgres psql
```

The prompt changes to `postgres=#`. You are now typing SQL. **Every SQL line must end with a semicolon `;`**, and nothing runs until you press Enter after it.

### 1.4 Create the role and the database

Type these two lines, one at a time, pressing Enter after each:

```sql
CREATE ROLE lookout LOGIN PASSWORD 'YOUR_PASSWORD';
CREATE DATABASE lookout OWNER lookout ENCODING 'UTF8';
```

Each should answer with `CREATE ROLE` and `CREATE DATABASE`. Check the result:

```sql
\du
\l
```

- `\du` lists roles. You should see `lookout` (it should **not** say `Superuser`).
- `\l` lists databases. You should see `lookout` with owner `lookout`.

Leave the prompt:

```sql
\q
```

Mistakes, and fixes:
- `role "lookout" already exists`: it is already there. To just change the password: `ALTER ROLE lookout PASSWORD 'YOUR_PASSWORD';`
- `database "lookout" already exists`: fine if it is empty or yours. To start over, `DROP DATABASE lookout;` (deletes all its data) and run `CREATE DATABASE` again.
- You typed something wrong and the prompt looks like `postgres-#` (a dash, not `#`): the statement is unfinished. Type `;` and Enter, or press Ctrl+C.

### 1.5 Confirm the new user can log in

```bash
PGPASSWORD='YOUR_PASSWORD' psql -h 127.0.0.1 -U lookout -d lookout -c 'select current_user, version();'
```

Success prints a one-row table that starts with `lookout` and mentions `PostgreSQL 16`.

If you get:
- `password authentication failed`: wrong password. Reset it as shown in 1.4.
- `FATAL: no pg_hba.conf entry` or `Ident authentication failed`: open `/var/lib/pgsql/16/data/pg_hba.conf` (`sudo nano` or `sudo vi`) and make sure the lines for `127.0.0.1/32` and `::1/128` end in `scram-sha-256`, not `ident`. Save, then `sudo systemctl reload postgresql-16` and retry.
- `Connection refused`: PostgreSQL is not running; go back to 1.1.

The database side is now ready. Continue with 1a (same host) or 1b (separate host), then go on to section 2.

### 1a. If the database is on the SAME server as the app

Nothing more to do for networking. Just confirm that local TCP logins use a password (`scram-sha-256`):

```bash
sudo grep -vE '^\s*(#|$)' /var/lib/pgsql/16/data/pg_hba.conf
```

This prints the active rules only. The lines that start with `host` and mention `127.0.0.1/32` and `::1/128` must end in `scram-sha-256`. If they say `ident`, change that word to `scram-sha-256` (edit with `sudo nano /var/lib/pgsql/16/data/pg_hba.conf`, see "How to edit a file" below), then `sudo systemctl reload postgresql-16`.

In section 3 you will use `127.0.0.1` as the DB address. Skip 1b and go to **section 2**.

### 1b. If the database is on a SEPARATE server

You need the **app server's IP address**. On the app server run `hostname -I` and note the first address (example below: `10.0.0.20`). Replace it everywhere below. Do these steps **on the DB server**.

**How to edit a file (nano):** `sudo nano /path/to/file` opens it. Use the arrow keys to move. **Ctrl+O** then **Enter** saves. **Ctrl+X** exits. (If `nano` is missing: `sudo dnf install -y nano`, or use `sudo vi`: press `i` to type, `Esc` then `:wq` and Enter to save.)

1. Make PostgreSQL listen on the network. Open `sudo nano /var/lib/pgsql/16/data/postgresql.conf`. Press Ctrl+W to search for `listen_addresses`. Change that line to the following (remove the leading `#` if present; use the DB server's own IP, from `hostname -I`):
   ```
   listen_addresses = 'localhost,10.0.0.30'
   ```
2. Allow only the app server to log in. Open `sudo nano /var/lib/pgsql/16/data/pg_hba.conf` and add this line at the **end** of the file:
   ```
   host    lookout    lookout    10.0.0.20/32    scram-sha-256
   ```
   Meaning: from the machine 10.0.0.20, the user `lookout` may connect to database `lookout` with a password.
3. Open the firewall for that machine only:
   ```bash
   sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.20/32" port port="5432" protocol="tcp" accept'
   sudo firewall-cmd --reload
   ```
   Each command should print `success`.
4. Restart PostgreSQL so the changes apply, and make sure it still runs:
   ```bash
   sudo systemctl restart postgresql-16
   sudo systemctl status postgresql-16
   ```
   If it fails to start, you have a typo in a config file: `sudo journalctl -u postgresql-16 -n 30` shows which line.

SELinux needs no change for PostgreSQL on its standard port.

### 1c. Test the connection from the APP server (separate-server setup only)

Do this on the **app server**. It installs only the small client program, not a database:

```bash
sudo dnf install -y postgresql
PGPASSWORD='YOUR_PASSWORD' psql -h 10.0.0.30 -U lookout -d lookout -c 'select current_user;'
```

(`10.0.0.30` = the DB server's IP.) Success prints `lookout`. If it fails:

| Message | Meaning and fix |
|---|---|
| `Connection refused` or hangs / `timeout` | `listen_addresses` not set or PostgreSQL not restarted (1b step 1, 4), or the firewall rule is missing or has the wrong IP (step 3) |
| `no pg_hba.conf entry for host ...` | the `host` line is missing, or the IP in it is not the app server's IP (step 2) |
| `password authentication failed` | wrong password; reset it as in 1.4 |

Logs on the DB server: `sudo tail -50 /var/lib/pgsql/16/data/log/*.log`.

**Optional, recommended later:** encrypt the DB connection (`ssl = on` in `postgresql.conf` with a certificate, a `hostssl` line in `pg_hba.conf`, and `?sslmode=require` at the end of `DATABASE_URL`). Ask your DBA/security team if there is a standard certificate.

---

## 2. App server: install software and copy the code

Do all of section 2 on the **app server**.

### 2.1 Install what is needed

```bash
sudo dnf install -y python3.11 python3.11-pip nginx
python3.11 --version
```

The last command should print `Python 3.11.x`. (Lookout needs 3.11 or newer; RHEL 8's default `python3` is older, which is why we ask for 3.11 specifically.)

### 2.2 Create a dedicated user and folders

The app will run as a normal, unprivileged Linux user called `lookout` instead of root, which limits damage if something goes wrong.

```bash
sudo useradd --system --home-dir /opt/lookout --no-create-home --shell /sbin/nologin lookout
sudo mkdir -p /opt/lookout /etc/lookout /var/lib/lookout/backups
sudo chown lookout:lookout /opt/lookout /var/lib/lookout/backups
sudo chown root:lookout /etc/lookout
sudo chmod 750 /etc/lookout
```

If `useradd` says the user already exists, that is fine.

### 2.3 Copy the code to the server

You are on Windows with PuTTY, so the easiest way is **WinSCP** (free) or the `scp` command that comes with Windows.

1. Upload the project folder `D:\errorpanel` to the server's `/tmp/lookout-upload` (WinSCP: drag and drop; or in PowerShell on your PC: `scp -r D:\errorpanel youruser@APP_SERVER_IP:/tmp/lookout-upload`).
   **Do not upload** these (delete them from the upload, or skip them): `backend\.env`, `backend\dev.db`, `backend\.venv`, and any file with real server passwords such as `serverlist_update`.
2. On the server, move it into place:
   ```bash
   sudo cp -a /tmp/lookout-upload/. /opt/lookout/
   sudo chown -R lookout:lookout /opt/lookout
   ls /opt/lookout/backend
   ```
   You should see `app`, `migrations`, `alembic.ini`, `requirements.txt` among the files.
3. Remove the leftover copy: `rm -rf /tmp/lookout-upload`

(If the server can reach your Git host, `sudo -u lookout git clone <repo-url> /opt/lookout` also works. The folder must be empty first.)

### 2.4 Create the Python environment and install packages

A "virtual environment" (venv) is a private folder of Python packages, so Lookout doesn't affect the rest of the server.

```bash
sudo -u lookout python3.11 -m venv /opt/lookout/backend/.venv
sudo -u lookout /opt/lookout/backend/.venv/bin/pip install --upgrade pip
sudo -u lookout /opt/lookout/backend/.venv/bin/pip install -r /opt/lookout/backend/requirements.txt
```

The last command takes a minute or two and ends with `Successfully installed ...`. Use `requirements.txt` here (the `-dev` file is for developers). No compiler is needed.

If the server has no internet access, pip will fail with connection errors; ask your network team for the internal pip mirror and add `--index-url <mirror>` to the command.

---

## 3. Configuration and secrets

### 3a. Create the master key

Lookout stores logins for your Oracle/SSH/API resources encrypted. The **master key** is what encrypts them.

```bash
sudo -u lookout bash -c 'cd /opt/lookout/backend && .venv/bin/python -m app.cli gen-master-key' | sudo tee /etc/lookout/master_key > /dev/null
sudo chown root:lookout /etc/lookout/master_key
sudo chmod 640 /etc/lookout/master_key
sudo wc -c /etc/lookout/master_key
```

The last command shows the file size. It should be about `44` bytes. If it says `0`, the key was not created: re-run the first command and read any error it prints.

> **Back this key up now**, somewhere separate from the database backups (for example in your company password manager). Run `sudo cat /etc/lookout/master_key` once, copy the text, and store it. If the key is lost, all stored resource credentials become permanently unreadable and must be re-entered. If it is stolen together with a DB backup, the credentials are exposed. Never paste it in chat or commit it.

### 3b. Create the settings file

```bash
sudo nano /etc/lookout/lookout.env
```

Paste this, then edit the values (see the table below):

```ini
DATABASE_URL=postgresql+psycopg://lookout:YOUR_PASSWORD@127.0.0.1:5432/lookout
MASTER_KEY_FILE=/etc/lookout/master_key
BACKUP_DIR=/var/lib/lookout/backups
```

Save with Ctrl+O, Enter, Ctrl+X. Then lock down the file, because it contains the DB password:

```bash
sudo chown root:lookout /etc/lookout/lookout.env
sudo chmod 640 /etc/lookout/lookout.env
```

What to change in `DATABASE_URL`:

| Part | Meaning | What to put |
|---|---|---|
| `lookout:` (first) | DB user | keep `lookout` |
| `YOUR_PASSWORD` | the password from step 1.2 | your real password, no quotes |
| `127.0.0.1` | DB server address | keep it if the DB is on this server; otherwise the DB server IP (e.g. `10.0.0.30`) |
| `5432` | PostgreSQL port | keep |
| `/lookout` (last) | database name | keep |

Rules that matter:
- It must start with `postgresql+psycopg://`, not just `postgresql://`.
- A hex password from `openssl rand -hex 16` is safe. If you picked your own password with characters like `@ : / # ? %`, replace them with `%40 %3A %2F %23 %3F %25`.
- Do **not** add `COOKIE_SECURE=false`. The default (secure) is right for production behind HTTPS.
- Optional: `SESSION_IDLE_MINUTES=30`, `SESSION_ABSOLUTE_HOURS=8`, `LOGIN_RATE_PER_MINUTE=10`, `MAX_FAILED_ATTEMPTS=5`, `LOCKOUT_MINUTES=15` (defaults shown; only add a line to change one).
- Make sure there is no `/opt/lookout/backend/.env` file left over from development.

---

## 4. Create the tables and the first admin login

### 4.1 Create the tables (migration)

```bash
cd /opt/lookout/backend
sudo -u lookout bash -c 'set -a; . /etc/lookout/lookout.env; set +a; .venv/bin/alembic upgrade head'
```

(The `set -a; . file; set +a` part just loads your settings file for this one command.) Success prints lines containing `Running upgrade ...` and ends without a red error or `Traceback`.

If it fails:
- `password authentication failed` or `connection refused`: the `DATABASE_URL` is wrong. Re-check section 3b and re-run the test in 1.5 / 1c with the same values.
- `ModuleNotFoundError`: the packages are not installed; repeat 2.4.
- `Permission denied` reading the env file: repeat the `chown`/`chmod` in 3b.

Check that tables exist (use `127.0.0.1` or the DB server IP as in your `DATABASE_URL`):

```bash
PGPASSWORD='YOUR_PASSWORD' psql -h 127.0.0.1 -U lookout -d lookout -c '\dt'
```

You should get a list of tables (users, sessions, resources, views, ...). (`psql` is on the server already if the DB is local; otherwise `sudo dnf install -y postgresql`.)

### 4.2 Create the first admin user

```bash
sudo -u lookout bash -c 'cd /opt/lookout/backend; set -a; . /etc/lookout/lookout.env; set +a; .venv/bin/python -m app.cli create-admin admin'
```

It asks `Password:` and `Repeat:`. **Typed characters will not appear on screen**; that is normal. Use at least 12 characters. It ends with `Admin 'admin' created.` This is the login for the website (user `admin`). If it says the user already exists, the admin was already created earlier.

### 4.3 (Optional) Import your existing server list

Only if you want to bring over the old "Servers Panel" servers. Do a **dry run** first; it changes nothing:

```bash
sudo -u lookout bash -c 'cd /opt/lookout/backend; set -a; . /etc/lookout/lookout.env; set +a; .venv/bin/python ../tools/migrate_serverlist.py --dry-run --serverlist /path/to/serverlist_update --index-html /opt/lookout/index.html --owner admin'
```

Read the output; if it looks right, run it again without `--dry-run`. Keep `serverlist_update` outside `/opt/lookout` (it holds passwords), readable by `lookout`, and never copy it into the repo.

---

## 5. Run Lookout as a service

A systemd "service" starts the app at boot and restarts it if it crashes. You need two: the **API** (the website and its backend) and the **scheduler** (runs the periodic checks).

### 5.1 Create the API service

```bash
sudo nano /etc/systemd/system/lookout-api.service
```

Paste, save (Ctrl+O, Enter, Ctrl+X):

```ini
[Unit]
Description=Lookout API
After=network-online.target
Wants=network-online.target

[Service]
User=lookout
Group=lookout
WorkingDirectory=/opt/lookout/backend
EnvironmentFile=/etc/lookout/lookout.env
ExecStart=/opt/lookout/backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1 --workers 2
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/lookout

[Install]
WantedBy=multi-user.target
```

`--host 127.0.0.1` means only this server itself can reach it; nginx (section 6) is the public door.

### 5.2 Create the scheduler service

```bash
sudo nano /etc/systemd/system/lookout-scheduler.service
```

```ini
[Unit]
Description=Lookout scheduler (single instance)
After=network-online.target

[Service]
User=lookout
Group=lookout
WorkingDirectory=/opt/lookout/backend
EnvironmentFile=/etc/lookout/lookout.env
ExecStart=/opt/lookout/backend/.venv/bin/python -m app.scheduler
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/lookout

[Install]
WantedBy=multi-user.target
```

> **Run exactly one scheduler, on one server only.** Two copies would run every check twice.

### 5.3 Start both and check

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lookout-api lookout-scheduler
sudo systemctl status lookout-api lookout-scheduler
```

Both should show `active (running)` in green (press `q` to leave). Then:

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/readyz
```

`/healthz` should print `{"status":"ok"}` (the app is up). `/readyz` should print `{"status":"ready"}` (the app can also reach the database). If `/readyz` shows an error, the DB connection is the problem: go back to section 3b.

If a service shows `failed`:

```bash
sudo journalctl -u lookout-api -n 50 --no-pager
```

Read the last lines. Common causes: a typo in the `.service` file or a wrong path (`status=203/EXEC`), a bad `DATABASE_URL`, or SELinux blocking it (check with `getenforce` and `sudo ausearch -m avc -ts recent`; if you see denials for `lookout`, tell your admin the exact lines). After fixing, run `sudo systemctl daemon-reload && sudo systemctl restart lookout-api`.

Live logs: `sudo journalctl -u lookout-api -f` (Ctrl+C to stop).

---

## 6. nginx and HTTPS (required)

nginx is the web server people connect to. It accepts HTTPS and forwards to the app on port 8000. HTTPS is **mandatory**: the login cookie is marked `Secure`, so logging in over plain `http://` will silently not work.

### 6.1 Get a certificate

You need two files: a certificate and its private key.

- **Production:** ask your security/infra team for a certificate for the site's name (for example `lookout.yourcompany.com`). Ask for the full chain certificate. Put the files on the server as `/etc/pki/tls/certs/lookout.crt` and `/etc/pki/tls/private/lookout.key`.
- **Testing only:** create a self-signed one. Browsers will show a warning that you must accept:
  ```bash
  sudo openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout /etc/pki/tls/private/lookout.key -out /etc/pki/tls/certs/lookout.crt \
    -subj "/CN=lookout.yourcompany.com"
  sudo chmod 600 /etc/pki/tls/private/lookout.key
  ```

### 6.2 Create the nginx site

```bash
sudo nano /etc/nginx/conf.d/lookout.conf
```

Replace `lookout.yourcompany.com` (twice) with your site's DNS name. If you have no DNS name yet and will browse by IP address, use `server_name _;` instead.

```nginx
server {
    listen 80;
    server_name lookout.yourcompany.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name lookout.yourcompany.com;

    ssl_certificate     /etc/pki/tls/certs/lookout.crt;
    ssl_certificate_key /etc/pki/tls/private/lookout.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 5m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }

    # Sensei chat streams its answer piece by piece (SSE), so buffering must be off
    location /api/llm/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }
}
```

### 6.3 Allow it through SELinux and the firewall, then start it

```bash
sudo setsebool -P httpd_can_network_connect 1
sudo firewall-cmd --permanent --add-service=http --add-service=https
sudo firewall-cmd --reload
sudo nginx -t
sudo systemctl enable --now nginx
```

- The first line lets nginx forward to the app (without it you get `502 Bad Gateway` on RHEL).
- `nginx -t` must print `syntax is ok` and `test is successful`. If not, it names the file and line to fix. After fixing, `sudo systemctl reload nginx`.

### 6.4 Open the site

Browse to `https://lookout.yourcompany.com/` (or `https://<server-ip>/`). With a self-signed certificate, click through the browser warning. You should see the login page; log in as `admin` with the password from 4.2.

If you get `502 Bad Gateway`: the API service is not running (check 5.3) or the SELinux line in 6.3 was skipped. `sudo tail -20 /var/log/nginx/error.log` gives the reason.

---

## 7. Let Lookout reach the systems it monitors

Lookout connects **out** from the app server to your Oracle databases and other systems. These paths must be open in your network firewall:

| To | Port | Needed for |
|---|---|---|
| each Oracle database server | 1521 (or your listener port) | database resources and views. No Oracle client needs installing |
| each SSH resource | 22 | SSH resources. The first connection to a host remembers its host key; do that first connection yourself and confirm the host is the right one |
| API resources | 443/80 | HTTP resources. Addresses on private networks are blocked by default (SSRF guard); see the README for how to allow them |
| LLM endpoint | usually 443 | Sensei, only if you use it |

Quick check that a port is reachable from the app server (replace host and port):

```bash
timeout 5 bash -c '</dev/tcp/ORACLE_HOST/1521' && echo OPEN || echo BLOCKED
```

`BLOCKED` means ask the network team to open that path from the app server's IP.

---

## 8. Backups, upgrades and logs

### 8.1 Database backup

On the **DB server**:

```bash
sudo mkdir -p /var/backups/lookout && sudo chown postgres:postgres /var/backups/lookout
sudo -u postgres pg_dump -Fc -d lookout -f /var/backups/lookout/lookout-$(date +%F).dump
ls -lh /var/backups/lookout
```

The file should appear with a non-zero size. To run it automatically every night at 02:00, edit the postgres user's schedule with `sudo -u postgres crontab -e` and add this line:

```
0 2 * * * /usr/pgsql-16/bin/pg_dump -Fc -d lookout -f /var/backups/lookout/lookout-$(date +\%F).dump
```

(Also remove old dumps now and then, and copy them off the server.) To restore into an empty database:

```bash
sudo -u postgres pg_restore -d lookout --clean --if-exists /var/backups/lookout/lookout-YYYY-MM-DD.dump
```

Also keep a copy of `/etc/lookout/master_key` as explained in 3a. A database backup without the master key cannot give back the stored credentials. The app also writes its own config backups to `/var/lib/lookout/backups`.

### 8.2 Upgrading to a new version

```bash
sudo systemctl stop lookout-scheduler
# 1. take a DB backup first (8.1)
# 2. upload the new code like in 2.3 into /tmp/lookout-upload, then:
sudo cp -a /tmp/lookout-upload/. /opt/lookout/ && sudo chown -R lookout:lookout /opt/lookout
sudo -u lookout /opt/lookout/backend/.venv/bin/pip install -r /opt/lookout/backend/requirements.txt
cd /opt/lookout/backend
sudo -u lookout bash -c 'set -a; . /etc/lookout/lookout.env; set +a; .venv/bin/alembic upgrade head'
sudo systemctl restart lookout-api lookout-scheduler
curl -s http://127.0.0.1:8000/readyz
```

Do not upload `.env`, `dev.db` or `.venv` again.

### 8.3 Where to look when something is wrong

| What | Command |
|---|---|
| API log | `sudo journalctl -u lookout-api -n 100 --no-pager` |
| Scheduler log | `sudo journalctl -u lookout-scheduler -n 100 --no-pager` |
| nginx errors | `sudo tail -50 /var/log/nginx/error.log` |
| PostgreSQL log | `sudo tail -50 /var/lib/pgsql/16/data/log/*.log` (on the DB server) |
| Service status | `sudo systemctl status lookout-api lookout-scheduler nginx` |
| Restart | `sudo systemctl restart lookout-api` |

The app hides secrets in its own logs, but still limit who can read them.

---

## 9. Go-live checklist

- [ ] The DB test in 1.5 (and 1c if the DB is on another server) printed `lookout`
- [ ] `pg_hba.conf` lets only the app server connect remotely (1b)
- [ ] `/etc/lookout/lookout.env` and `/etc/lookout/master_key` are `chmod 640`, owner `root:lookout`
- [ ] The master key is backed up somewhere safe, separate from DB backups
- [ ] `alembic upgrade head` finished without errors; `\dt` shows tables; admin created
- [ ] `lookout-api` and `lookout-scheduler` are `active (running)`; **only one** scheduler exists anywhere
- [ ] `curl http://127.0.0.1:8000/readyz` returns `{"status":"ready"}`
- [ ] `https://your-site/` opens and `admin` can log in; `COOKIE_SECURE` was not set to false
- [ ] Added one Oracle resource and ran one view successfully (proves section 7 network paths)
- [ ] Nightly DB backup is scheduled and one restore has been tested on a spare database

---

## Alternative: containers

`deploy/docker-compose.yml` runs Postgres, the migration, the API and the scheduler together. It has never been run, and it pins `postgres:15`; change that to `postgres:16` if you use it. This document is the plain-RHEL route and does not depend on it.
