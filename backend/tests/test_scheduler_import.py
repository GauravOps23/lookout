from datetime import timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app import db as dbmod
from app import legacy_import as li
from app.db import now
from app.models import AuditLog, Resource, User, View, ViewRun
from app.scheduler import jobs
from tests.helpers import new_oracle, new_view

SERVERLIST = """\
ro_user,pw1,10.0.0.1,1521,JAGUAR
ro_user,pw2,10.0.0.2,1521,tulip
ro_user,pw3,10.0.0.3,1521,MAGPIE

broken line
ro_user,pw4,10.0.0.4,notaport,BAD
"""
INDEX = """\
<div class="category"><br> 5G <br><br>
  <a href="index2.html?csvFile=JAGUAR.csv" class="box">JAGUAR</a></div>
<div class="category"><br> CEIR<br><br>
  <a href="index2.html?csvFile=TULIP.csv" class="box">tulip</a></div>
<div class="category"><br> DARTS<br><br>
  <a href="index2.html?csvFile=tulip.csv" class="box">tulip</a></div>
<!--
<div class="category"><br>IVR_USSD<br><br>
  <a href="index2.html?csvFile=MAGPIE.csv" class="box">MAGPIE</a></div>
-->
"""


def factory():
    return dbmod.session_factory()


# ---- scheduler ------------------------------------------------------------------------------
def test_check_all_resources_updates_status(admin, fakes):
    new_oracle(admin, "UP1")
    new_oracle(admin, "DOWN1")
    fakes.oracle.fail["DOWN1"] = RuntimeError("ORA-12541 at secret-host")
    summary = jobs.check_all_resources(factory())
    assert summary == {"checked": 2, "up": 1, "down": 1}
    with factory()() as db:
        st = {r.name: (r.status, r.last_error) for r in db.scalars(select(Resource))}
    assert st["UP1"] == ("up", "") and st["DOWN1"] == ("down", "Oracle error ORA-12541")


def test_scheduled_view_runs_as_owner_and_records(admin, fakes):
    rid = new_oracle(admin)["id"]
    vid = new_view(admin, [rid], schedule_cron="*/10 * * * *").json()["id"]
    import uuid
    assert jobs.run_scheduled_view(factory(), uuid.UUID(vid)) == "ok"
    with factory()() as db:
        run = db.scalar(select(ViewRun))
        assert run.triggered_by == "schedule" and run.status == "ok" and run.row_count == 1


def test_scheduled_failure_is_audited_not_swallowed(admin, make_user, fakes):
    from tests.helpers import grant
    u = make_user("sched")
    grant(admin, u, "view.db_table.create")
    rid = new_oracle(admin)["id"]
    admin.put(f"/api/resources/{rid}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "use"}]})
    vid = new_view(u, [rid], schedule_cron="*/10 * * * *").json()["id"]
    admin.put(f"/api/resources/{rid}/acl", json={"entries": []})  # owner loses access to the source
    import uuid
    assert jobs.run_scheduled_view(factory(), uuid.UUID(vid)) in ("FORBIDDEN", "NOT_FOUND")
    with factory()() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "view.run_failed")) is not None


def test_sync_view_jobs_adds_changes_and_removes(admin, fakes):
    rid = new_oracle(admin)["id"]
    vid = new_view(admin, [rid], schedule_cron="*/10 * * * *").json()["id"]
    new_view(admin, [rid], name="unscheduled")
    sch = BackgroundScheduler()
    jobs.sync_view_jobs(sch, factory())
    assert [j.id for j in sch.get_jobs()] == [f"view:{vid}"]
    admin.patch(f"/api/views/{vid}", json={"schedule_cron": "0 * * * *"})
    jobs.sync_view_jobs(sch, factory())
    assert [j.name for j in sch.get_jobs()] == ["0 * * * *"]
    admin.patch(f"/api/views/{vid}", json={"schedule_cron": None})
    jobs.sync_view_jobs(sch, factory())
    assert sch.get_jobs() == []


def test_purge_retention(admin, fakes):
    rid = new_oracle(admin)["id"]
    vid = new_view(admin, [rid]).json()["id"]
    import uuid
    with factory()() as db:
        for days in (1, 40, 400):
            db.add(ViewRun(view_id=uuid.UUID(vid), started_at=now() - timedelta(days=days), status="ok",
                           result={"rows": []}))
        db.commit()
    assert jobs.purge(factory()) == {"results_dropped": 2, "runs_deleted": 1}
    with factory()() as db:
        runs = sorted(db.scalars(select(ViewRun)), key=lambda r: r.started_at)
        assert len(runs) == 2 and runs[0].result is None and runs[1].result is not None


def test_configure_registers_jobs(admin, fakes):
    sch = BackgroundScheduler()
    jobs.configure(sch, factory())
    assert {"resource-status", "sync-views", "purge"} <= {j.id for j in sch.get_jobs()}


# ---- legacy import --------------------------------------------------------------------------
def test_parse_index_ignores_commented_blocks_and_merges_categories():
    tags = li.parse_index_tags(INDEX)
    assert tags["jaguar"] == ["5G"]
    assert tags["tulip"] == ["CEIR", "DARTS"]  # same server under two categories, case-insensitive
    assert "magpie" not in tags  # inside an HTML comment: not on the live page


def test_parse_real_index_html():
    p = Path(__file__).resolve().parents[2] / "index.html"
    if not p.exists():
        return
    tags = li.parse_index_tags(p.read_text(encoding="utf-8"))
    assert len(tags) >= 50 and tags["ferrari"] == ["5G", "CEIR", "M2M"]


def test_parse_serverlist_reports_bad_lines_without_passwords():
    servers, problems = li.parse_serverlist(SERVERLIST)
    assert [s["service"] for s in servers] == ["JAGUAR", "tulip", "MAGPIE"]
    assert len(problems) == 2 and "pw" not in " ".join(problems)


def test_migrate_creates_resources_view_and_is_idempotent(db, fakes):
    owner = db.scalar(select(User))
    servers, _ = li.parse_serverlist(SERVERLIST)
    tags = li.parse_index_tags(INDEX)
    dry = li.migrate(db, owner, servers, tags, dry_run=True)
    assert len(dry["created"]) == 3 and db.scalar(select(Resource)) is None  # nothing written

    rep = li.migrate(db, owner, servers, tags)
    assert [c["name"] for c in rep["created"]] == ["JAGUAR", "tulip", "MAGPIE"]
    assert rep["untagged"] == ["MAGPIE"] and rep["view"] == "created"
    res = {r.name: r for r in db.scalars(select(Resource))}
    assert res["tulip"].tags == ["CEIR", "DARTS"] and res["tulip"].config["service_name"] == "tulip"
    assert "pw1" not in str(res["JAGUAR"].config) and res["JAGUAR"].secret_blob  # password only in the vault
    from app.resources import service
    assert service.open_secret(res["JAGUAR"]) == {"password": "pw1"}
    view = db.scalar(select(View))
    assert len(view.source_resource_ids) == 3 and view.schedule_cron == "*/10 * * * *"

    again = li.migrate(db, owner, servers, tags)
    assert again["created"] == [] and len(again["skipped"]) == 3 and again["view"].startswith("exists")
    assert len(list(db.scalars(select(Resource)))) == 3 and len(list(db.scalars(select(View)))) == 1
    from app.models import Dashboard, DashboardWidget
    assert len(list(db.scalars(select(Dashboard)))) == 1 and len(list(db.scalars(select(DashboardWidget)))) == 1


def test_migrated_view_runs_end_to_end(db, admin, fakes):
    owner = db.scalar(select(User))
    servers, _ = li.parse_serverlist(SERVERLIST)
    li.migrate(db, owner, servers, li.parse_index_tags(INDEX))
    vid = admin.get("/api/views").json()[0]["id"]
    out = admin.post(f"/api/views/{vid}/run").json()
    assert {r[0] for r in out["rows"]} == {"JAGUAR", "tulip", "MAGPIE"}


def test_compare_with_csv(tmp_path):
    (tmp_path / "jaguar.csv").write_text("SYSTEM,REQ_USER,ERROR,COUNT\nS,u,err one,3\nS,u,err two,1\n")
    (tmp_path / "tulip.csv").write_text("SYSTEM,REQ_USER,ERROR,COUNT\nS,u,err one,3\n")
    cols = [{"name": n} for n in ("SOURCE", "SYSTEM", "REQ_USER", "ERROR", "COUNT")]
    result = {"columns": cols, "rows": [["JAGUAR", "S", "u", "err two", 1], ["JAGUAR", "S", "u", "err one", 3],
                                        ["tulip", "S", "u", "err one", 4], ["OTHER", "S", "u", "x", 1]]}
    v = li.compare_with_csv(result, tmp_path)
    assert v["JAGUAR"] == "match" and v["tulip"].startswith("DIFFERENT") and v["OTHER"] == "no legacy CSV"
