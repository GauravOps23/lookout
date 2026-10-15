"""Import the legacy panel into the new model (spec section 15).

serverlist_update lines (`user,password,ip,port,service`) become oracle resources, the category tags come
from index.html, and one scheduled "Daily errors" view fans out to all of them.
"""
import csv
import re
import uuid
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.models import Dashboard, DashboardWidget, Resource, User, View
from app.resources import service
from app.views.db_table.plugin import DAILY_ERRORS_SQL

VIEW_NAME = "Daily errors"
DASHBOARD_NAME = "Servers Panel"
_COMMENT = re.compile(r"<!--.*?-->", re.S)  # index.html has large dead commented-out blocks
_CATEGORY = re.compile(r'<div class="category">(.*?)</div>', re.S)
_CSV_LINK = re.compile(r"csvFile=([^\"'&]+?)\.csv", re.I)


def parse_index_tags(html: str) -> dict[str, list[str]]:
    """{service (lower-case): [category, ...]}; a server listed under several categories gets several tags."""
    tags: dict[str, list[str]] = {}
    for block in _CATEGORY.findall(_COMMENT.sub("", html)):
        head = re.sub(r"<[^>]+>", " ", block.split("<a", 1)[0]).strip()
        name = re.sub(r"\s+", " ", head)
        for svc in _CSV_LINK.findall(block):
            lst = tags.setdefault(svc.lower(), [])
            if name and name not in lst:
                lst.append(name)
    return tags


def parse_serverlist(text: str) -> tuple[list[dict], list[str]]:
    """Return (servers, problems). Passwords stay in memory only; they are never printed."""
    servers, problems = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.strip().split(",")]
        if len(parts) != 5 or not all(parts) or not parts[3].isdigit():
            problems.append(f"line {n}: expected user,password,ip,port,service")
            continue
        servers.append(dict(username=parts[0], password=parts[1], host=parts[2], port=int(parts[3]),
                            service=parts[4]))
    return servers, problems


def migrate(db: Session, owner: User, servers: list[dict], tags: dict[str, list[str]], *,
            schedule: str = "*/10 * * * *", dry_run: bool = False) -> dict:
    report = {"created": [], "skipped": [], "failed": [], "untagged": [], "unlisted_categories": [],
              "view": None}
    seen = Counter(s["service"].lower() for s in servers)
    existing = {r.name.lower(): r for r in db.scalars(select(Resource).where(Resource.type == "oracle"))}
    resources: list[Resource] = []

    for s in servers:
        name, key = s["service"], s["service"].lower()
        if seen[key] > 1 and any(r["name"] == name for r in report["created"]):
            report["failed"].append({"name": name, "reason": "duplicate service in server list"})
            continue
        if key in existing:
            report["skipped"].append(name)
            resources.append(existing[key])
            continue
        if not tags.get(key):
            report["untagged"].append(name)
        try:
            r = Resource(id=uuid.uuid4(), type="oracle", name=name, tags=tags.get(key, []), owner_id=owner.id,
                         config=dict(host=s["host"], port=s["port"], service_name=name,
                                     username=s["username"], fetch_size=500, call_timeout_ms=30000,
                                     connect_timeout_s=10))
            if not dry_run:
                service.seal_secret(r, {"password": s["password"]})
                db.add(r)
            existing[key] = r
            resources.append(r)
            report["created"].append({"name": name, "tags": r.tags})
        except Exception as e:  # noqa: BLE001
            report["failed"].append({"name": name, "reason": type(e).__name__})

    listed = {s["service"].lower() for s in servers}
    report["unlisted_categories"] = sorted(k for k in tags if k not in listed)

    if resources:
        v = db.scalar(select(View).where(View.name == VIEW_NAME, View.type == "db_table"))
        ids = [str(r.id) for r in resources]
        report["view"] = "exists (sources refreshed)" if v else "created"
        if not dry_run:
            if v is None:
                v = View(type="db_table", name=VIEW_NAME, owner_id=owner.id, renderer="table", cache_ttl_s=300,
                         description="Today's failed requests grouped by system, user and message",
                         config={"mode": "sql", "sql": DAILY_ERRORS_SQL, "params": [], "filters": [],
                                 "columns": [], "row_limit": 10000, "timeout_s": 30})
                db.add(v)
            v.source_resource_ids, v.schedule_cron = ids, schedule
            db.flush()
            if db.scalar(select(Dashboard).where(Dashboard.name == DASHBOARD_NAME)) is None:
                d = Dashboard(name=DASHBOARD_NAME, owner_id=owner.id, description="Daily errors per server")
                db.add(d)
                db.flush()
                db.add(DashboardWidget(dashboard_id=d.id, view_id=v.id, title=VIEW_NAME,
                                       grid={"x": 0, "y": 0, "w": 12, "h": 8}, refresh_s=300))
    if not dry_run:
        db.commit()
        record(db, "migration.serverlist", actor_id=owner.id,
               detail={"created": len(report["created"]), "skipped": len(report["skipped"]),
                       "failed": len(report["failed"])})
    return report


def compare_with_csv(result: dict, csv_dir: Path) -> dict[str, str]:
    """Equivalence check (spec 15): per service, merged rows must equal the legacy CSV rows (as multisets)."""
    names = [c["name"] for c in result["columns"]]
    si = names.index("SOURCE")
    per: dict[str, Counter] = {}
    for row in result["rows"]:
        per.setdefault(row[si], Counter())[tuple(str(x) for i, x in enumerate(row) if i != si)] += 1
    verdict = {}
    for svc, got in per.items():
        path = next((p for p in csv_dir.glob("*.csv") if p.stem.lower() == svc.lower()), None)
        if path is None:
            verdict[svc] = "no legacy CSV"
            continue
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))[1:]
        want = Counter(tuple(r) for r in rows)
        verdict[svc] = "match" if want == got else f"DIFFERENT ({sum(got.values())} rows vs {sum(want.values())} in CSV)"
    return verdict
