# How to run Lookout (errorpanel) again

Everything is already set up (venv, `.env`, database). Just start the server.

## Quick start (every time)

Open PowerShell:

```powershell
cd D:\errorpanel\backend
.venv\Scripts\activate
python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000/ in the browser. Stop the server with `Ctrl+C`.

Login: the `admin` user created earlier (`python -m app.cli create-admin admin`).
If you forgot the password, run that command again, or create a different user name.

## Optional: scheduler

Runs status checks, cron views and retention. Run in a second terminal, only one copy:

```powershell
cd D:\errorpanel\backend
.venv\Scripts\activate
python -m app.scheduler
```

## Run tests

```powershell
cd D:\errorpanel\backend
.venv\Scripts\activate
python -m pytest
```

## If something is missing (first-time setup)

```powershell
cd D:\errorpanel\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env
python -m app.cli gen-master-key      # put the output in .env as MASTER_KEY
alembic upgrade head
python -m app.cli create-admin admin
```

## Troubleshooting

- Port 8000 busy: use `--port 8001` and open that port instead.
- After pulling model changes: run `alembic upgrade head` again.
- Legacy dashboard (old Servers Panel) is `get_error.py` + `index.html` at the repo root; it runs on a Linux host, not here.
