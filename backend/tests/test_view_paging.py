import uuid

from sqlalchemy import select

from app.models import ViewRun
from tests.helpers import fakes, grant, new_oracle, new_view  # noqa: F401

BIG = 25000  # more than the 10,000-row preview kept in the run record


def make_big(admin, fakes, n=BIG, name="BIG"):
    rid = new_oracle(admin, name)["id"]
    fakes.oracle.rows[name] = [["SYS", "u", f"e{i}", i] for i in range(n)]
    vid = new_view(admin, [rid], name="Big", cache_ttl_s=300).json()["id"]
    return rid, vid


def test_pages_of_50_cover_every_row_without_requerying(admin, fakes, db):
    _, vid = make_big(admin, fakes)
    first = admin.get(f"/api/views/{vid}/rows").json()
    assert (first["page"], first["size"], first["total_rows"], first["pages"]) == (1, 50, BIG, 500)
    assert len(first["rows"]) == 50 and first["rows"][0][3] == 0 and first["rows"][49][3] == 49
    assert first["meta"]["preview_only"] is True and first["meta"]["row_count"] == BIG
    rid = first["meta"]["run_id"]

    last = admin.get(f"/api/views/{vid}/rows", params={"page": 500, "run_id": rid}).json()
    assert len(last["rows"]) == 50 and last["rows"][-1][3] == BIG - 1
    assert admin.get(f"/api/views/{vid}/rows", params={"page": 501, "run_id": rid}).json()["rows"] == []
    assert len(fakes.oracle.calls) == 1  # paging reads the stored run; it never goes back to Oracle

    run = db.scalar(select(ViewRun).where(ViewRun.view_id == uuid.UUID(vid)))
    assert len(run.result["rows"]) == 10000 and run.rows_blob  # preview in the record, everything in the blob
    assert len(run.rows_blob) < 1_000_000                       # compressed


def test_run_endpoint_returns_a_preview_with_the_true_total(admin, fakes):
    _, vid = make_big(admin, fakes)
    res = admin.post(f"/api/views/{vid}/run?fresh=true", json={"params": {}}).json()
    assert len(res["rows"]) == 10000 and res["meta"]["row_count"] == BIG and res["meta"]["preview_only"]
    again = admin.post(f"/api/views/{vid}/run", json={"params": {}}).json()  # served from the stored run
    assert again["meta"]["cached"] and len(again["rows"]) == 10000 and again["meta"]["run_id"] == res["meta"]["run_id"]


def test_only_the_latest_run_keeps_the_full_rows(admin, fakes, db):
    _, vid = make_big(admin, fakes)
    for _ in range(3):
        admin.get(f"/api/views/{vid}/rows", params={"fresh": "true"})
    db.expire_all()
    runs = list(db.scalars(select(ViewRun).where(ViewRun.view_id == uuid.UUID(vid))))
    assert len(runs) == 3 and sum(1 for r in runs if r.rows_blob) == 1


def test_small_results_have_no_blob_and_still_page(admin, fakes, db):
    _, vid = make_big(admin, fakes, n=120, name="SMALL")
    p = admin.get(f"/api/views/{vid}/rows", params={"size": 50}).json()
    assert (p["total_rows"], p["pages"], len(p["rows"])) == (120, 3, 50) and "preview_only" not in p["meta"]
    assert len(admin.get(f"/api/views/{vid}/rows", params={"size": 50, "page": 3, "run_id": p["meta"]["run_id"]}).json()["rows"]) == 20
    assert db.scalar(select(ViewRun).where(ViewRun.view_id == uuid.UUID(vid))).rows_blob is None
    assert admin.get(f"/api/views/{vid}/rows", params={"size": 500}).status_code == 422


def test_empty_result_is_one_empty_page(admin, fakes):
    _, vid = make_big(admin, fakes, n=0, name="NONE")
    p = admin.get(f"/api/views/{vid}/rows").json()
    assert (p["total_rows"], p["pages"], p["rows"]) == (0, 1, [])


def test_access_rules_for_paging(admin, make_user, fakes):
    rid, vid = make_big(admin, fakes, n=200, name="ACL1")
    u = make_user("pia")
    assert u.get(f"/api/views/{vid}/rows").status_code == 404                       # cannot even see the view
    run_id = admin.get(f"/api/views/{vid}/rows").json()["meta"]["run_id"]
    assert u.get(f"/api/views/{vid}/rows", params={"run_id": run_id}).status_code == 404
    admin.put(f"/api/views/{vid}/acl", json={"entries": [{"principal_type": "user", "principal_id": u.id, "level": "view"}]})
    assert u.get(f"/api/views/{vid}/rows", params={"run_id": run_id}).status_code == 200   # view = read stored data
    assert u.get(f"/api/views/{vid}/rows", params={"fresh": "true"}).status_code == 403    # running needs use
    other = new_view(admin, [rid], name="Other").json()["id"]
    assert u.get(f"/api/views/{other}/rows", params={"run_id": run_id}).status_code == 404  # run belongs to another view


def test_expired_result_asks_to_run_again(admin, fakes, db):
    _, vid = make_big(admin, fakes, n=60, name="OLD")
    run_id = admin.get(f"/api/views/{vid}/rows").json()["meta"]["run_id"]
    run = db.scalar(select(ViewRun).where(ViewRun.view_id == uuid.UUID(vid)))
    run.result, run.rows_blob = None, None  # what the retention job does
    db.commit()
    r = admin.get(f"/api/views/{vid}/rows", params={"run_id": run_id})
    assert r.status_code == 404 and "Run the view again" in r.json()["error"]["message"]
