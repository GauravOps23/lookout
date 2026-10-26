#!/usr/bin/env bash
# Install or update Lookout on RHEL 8. Safe to re-run: the first run installs, later runs update.
#   sudo ./deploy/lookout.sh
# Optional settings (environment variables):
#   RUN_USER      Linux user the app runs as. Default: the user who ran sudo (nothing new is created).
#                 Set RUN_USER=lookout to have a dedicated system user created.
#   APP_DIR       install folder (default /opt/lookout)
#   DATABASE_URL  first install only, e.g. postgresql+psycopg://lookout:PASS@127.0.0.1:5432/lookout
#   SERVER_NAME   nginx host name (default: any)
#   PYTHON        python binary (default python3.11)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0"; exit 1; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-/opt/lookout}"
RUN_USER="${RUN_USER:-${SUDO_USER:-root}}"
SERVER_NAME="${SERVER_NAME:-_}"
PYTHON="${PYTHON:-python3.11}"
CONF=/etc/lookout
ENVF=$CONF/lookout.env
DATA=/var/lib/lookout
VENV=$APP_DIR/backend/.venv

command -v "$PYTHON" >/dev/null || { echo "$PYTHON not found. Install it: dnf install python3.11 python3.11-pip"; exit 1; }
command -v nginx >/dev/null || { echo "nginx not found. Install it: dnf install nginx"; exit 1; }

if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "== Creating system user $RUN_USER"
  useradd --system --home-dir "$APP_DIR" --no-create-home --shell /sbin/nologin "$RUN_USER"
fi

FIRST=0; [ -f "$ENVF" ] || FIRST=1

echo "== Copying code to $APP_DIR"
mkdir -p "$APP_DIR" "$CONF" "$DATA/backups"
if [ "$SRC" != "$APP_DIR" ]; then
  tar -C "$SRC" --exclude=.venv --exclude=.env --exclude='*.db' --exclude=__pycache__ \
      --exclude=.git --exclude=serverlist_update -cf - . | tar -C "$APP_DIR" -xf -
fi
chown -R "$RUN_USER" "$APP_DIR" "$DATA"

echo "== Python packages"
[ -x "$VENV/bin/python" ] || sudo -u "$RUN_USER" "$PYTHON" -m venv "$VENV"
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"

if [ "$FIRST" = 1 ]; then
  echo "== First install: creating settings"
  if [ -z "${DATABASE_URL:-}" ]; then
    echo "Enter the database URL (postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME)"
    read -r -p "DATABASE_URL: " DATABASE_URL
  fi
  [ -n "$DATABASE_URL" ] || { echo "DATABASE_URL is required"; exit 1; }
  if [ ! -f "$CONF/master_key" ]; then
    sudo -u "$RUN_USER" bash -c "cd '$APP_DIR/backend' && .venv/bin/python -m app.cli gen-master-key" > "$CONF/master_key"
  fi
  cat > "$ENVF" <<EOF
DATABASE_URL=$DATABASE_URL
MASTER_KEY_FILE=$CONF/master_key
BACKUP_DIR=$DATA/backups
EOF
  echo ">> BACK UP $CONF/master_key somewhere safe. Losing it makes stored credentials unreadable."
fi
chown root:"$RUN_USER" "$CONF" "$ENVF" "$CONF/master_key"
chmod 750 "$CONF"; chmod 640 "$ENVF" "$CONF/master_key"

run_app() { sudo -u "$RUN_USER" bash -c "cd '$APP_DIR/backend'; set -a; . '$ENVF'; set +a; $*"; }

echo "== Database migration"
run_app ".venv/bin/alembic upgrade head"

if [ "$FIRST" = 1 ]; then
  echo "== Create the admin login (password: 12+ characters)"
  run_app ".venv/bin/python -m app.cli create-admin admin" || true
fi

echo "== Services"
cat > /etc/systemd/system/lookout-api.service <<EOF
[Unit]
Description=Lookout API
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$ENVF
ExecStart=$VENV/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1 --workers 2
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/lookout-scheduler.service <<EOF
[Unit]
Description=Lookout scheduler (run exactly one)
After=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$ENVF
ExecStart=$VENV/bin/python -m app.scheduler
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable lookout-api lookout-scheduler >/dev/null 2>&1
systemctl restart lookout-api lookout-scheduler

echo "== nginx"
if [ ! -f /etc/nginx/conf.d/lookout.conf ]; then
  CRT=/etc/pki/tls/certs/lookout.crt; KEY=/etc/pki/tls/private/lookout.key
  if [ ! -f "$CRT" ]; then
    echo ">> No certificate at $CRT: making a self-signed one (browsers will warn). Replace it later."
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 -keyout "$KEY" -out "$CRT" -subj "/CN=${SERVER_NAME}" 2>/dev/null
    chmod 600 "$KEY"
  fi
  cat > /etc/nginx/conf.d/lookout.conf <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl http2;
    server_name $SERVER_NAME;
    ssl_certificate     $CRT;
    ssl_certificate_key $KEY;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 5m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
    # Sensei streams its answer (SSE): no buffering
    location /api/llm/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }
}
EOF
  command -v setsebool >/dev/null && setsebool -P httpd_can_network_connect 1 || true
  if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-service=http --add-service=https >/dev/null && firewall-cmd --reload >/dev/null
  fi
fi
nginx -t
systemctl enable nginx >/dev/null 2>&1
systemctl restart nginx

echo "== Health check"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  out="$(curl -s http://127.0.0.1:8000/readyz || true)"
  case "$out" in *ready*) echo "OK: $out"; echo "Open https://${SERVER_NAME/#_/<this-server>}/"; exit 0;; esac
  sleep 2
done
echo "Not ready yet. Look at: journalctl -u lookout-api -n 50 --no-pager"
exit 1
