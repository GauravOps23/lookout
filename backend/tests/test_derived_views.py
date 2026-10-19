"""M4: trend (V5) views."""
from datetime import timedelta

from sqlalchemy import select

from app.db import now
from app.models import View, ViewRun
from tests.helpers import new_oracle, new_view


def _mk(api, body):
    return api.post("/api/views", json={"source_resource_ids": [], **body})


def _seed_history(db, vid, days_counts):
    for ago, count in days_counts:
        db.add(ViewRun(view_id=vid, params_hash=_default_hash(), started_at=now() - timedelta(days=ago),
                       status="ok", row_count=2, triggered_by="schedule", result={
                           "columns": [{"name": "SYSTEM", "type": "string"}, {"name": "COUNT", "type": "number"}],
                           "rows": [["S1", count], ["S2", 1]],
                           "meta": {"row_count": 2, "truncated": False}}))
    db.commit()


def _default_hash():
    from app.views.engine import params_hash
    return params_hash({})


def test_trend_aggregates_history(admin, fakes, db):
    rid = new_oracle(admin)["id"]
    unscheduled = new_view(admin, [rid], name="U").json()["id"]
    cfg = {"source_view_id": unscheduled, "value_column": "COUNT", "group_by": "SYSTEM", "window_days": 3}
    assert _mk(admin, {"type": "trend", "name": "T", "config": cfg}).status_code == 422  # not scheduled
    src = new_view(admin, [rid], name="S", schedule_cron="*/10 * * * *").json()["id"]
    t = _mk(admin, {"type": "trend", "name": "T", "config": {**cfg, "source_view_id": src}})
    assert t.status_code == 201, t.text
    _seed_history(db, db.scalar(select(View.id).where(View.name == "S")), [(2, 10), (1, 20), (30, 99)])  # the 30-day-old run is outside the window
    res = admin.post(f"/api/views/{t.json()['id']}/run").json()
    assert [c["name"] for c in res["columns"]] == ["timestamp", "series", "value"]
    s1 = [r for r in res["rows"] if r[1] == "S1"]
    assert [r[2] for r in s1] == [10, 20] and len(res["rows"]) == 4


def test_trend_count_and_validation(admin, fakes):
    rid = new_oracle(admin)["id"]
    src = new_view(admin, [rid], name="S", schedule_cron="*/10 * * * *").json()["id"]
    assert _mk(admin, {"type": "trend", "name": "T", "config": {"source_view_id": src, "aggregation": "count"}}
               ).status_code == 201
    r = _mk(admin, {"type": "trend", "name": "T2", "config": {"source_view_id": src}})
    assert r.status_code == 201  # sum without value_column is only detected when there is history to read
    assert _mk(admin, {"type": "trend", "name": "T3", "source_resource_ids": [rid],
                       "config": {"source_view_id": src}}).status_code == 422
