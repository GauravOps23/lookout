from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import now
from app.main import create_app
from app.models import AuditLog, Dashboard, LinkTile
from tests.helpers import grant, new_oracle, new_view

W = lambda vid, **k: {"view_id": vid, "grid": {"x": 0, "y": 0, "w": 6, "h": 4}, **k}  # noqa: E731


def setup_view(admin, fakes, name="Daily", rows=None):
    rid = new_oracle(admin, name + "-src")["id"]
    if rows:
        fakes.oracle.rows[name + "-src"] = rows
    return new_view(admin, [rid], name=name, cache_ttl_s=300).json()["id"]


# ---- dashboards -----------------------------------------------------------------------------
def test_create_needs_capability_and_valid_widgets(admin, make_user, fakes):
    u, vid = make_user("dora"), setup_view(admin, fakes)
    assert u.post("/api/dashboards", json={"name": "D"}).status_code == 403
    grant(admin, u, "dashboard.create")
    assert u.post("/api/dashboards", json={"name": "D", "widgets": [W(vid)]}).status_code == 404  # no view access
    assert admin.post("/api/dashboards", json={"name": "D", "widgets": [
        {"view_id": vid, "grid": {"x": 8, "y": 0, "w": 6, "h": 1}}]}).status_code == 422  # off the 12-col grid
    assert admin.post("/api/dashboards", json={"name": "D", "widgets": [W(vid, renderer="nope")]}).status_code == 422
    ok = admin.post("/api/dashboards", json={"name": "D", "widgets": [W(vid, title="errors")]})
    assert ok.status_code == 201 and ok.json()["widgets"][0]["title"] == "errors"
    assert admin.post("/api/dashboards", json={"name": "D"}).status_code == 409


def test_widget_params_validated_at_save(admin, fakes):
    rid = new_oracle(admin)["id"]
    cfg = {"mode": "sql", "sql": "SELECT * FROM t WHERE c = {{c}}", "params": [{"name": "c", "pattern": "^[0-9]+$"}]}
    vid = new_view(admin, [rid], config=cfg).json()["id"]
    assert admin.post("/api/dashboards", json={"name": "A", "widgets": [W(vid)]}).status_code == 422
    assert admin.post("/api/dashboards", json={"name": "A", "widgets": [W(vid, params={"c": "x"})]}).status_code == 422
    assert admin.post("/api/dashboards", json={"name": "A", "widgets": [W(vid, params={"c": "12"})]}).status_code == 201


def test_viewer_sees_placeholder_for_views_without_access(admin, make_user, fakes):
    viewer = make_user("vee")
    v1, v2 = setup_view(admin, fakes, "V1"), setup_view(admin, fakes, "V2")
    did = admin.post("/api/dashboards", json={"name": "D", "widgets": [W(v1, title="a"), W(v2, title="secret")]}).json()["id"]
    admin.put(f"/api/dashboards/{did}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": viewer.id, "level": "view"}]})
    admin.put(f"/api/views/{v1}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": viewer.id, "level": "view"}]})
    admin.post(f"/api/views/{v1}/run")
    admin.post(f"/api/views/{v2}/run")
    ws = viewer.get(f"/api/dashboards/{did}").json()["widgets"]
    assert ws[0]["no_access"] is False and ws[0]["view_id"] == v1
    assert ws[1] == {**ws[1], "no_access": True, "view_id": None, "title": "", "params": {}}
    assert "secret" not in str(ws[1])
    assert viewer.get(f"/api/dashboards/{did}/widgets/{ws[0]['id']}/data").status_code == 200
    hidden = admin.get(f"/api/dashboards/{did}").json()["widgets"][1]["id"]
    assert viewer.get(f"/api/dashboards/{did}/widgets/{hidden}/data").status_code == 404  # data never leaves


def test_dashboard_acl_levels_and_hidden(admin, make_user, fakes):
    a, b = make_user("ann"), make_user("bob")
    did = admin.post("/api/dashboards", json={"name": "D"}).json()["id"]
    assert b.get("/api/dashboards").json() == [] and b.get(f"/api/dashboards/{did}").status_code == 404

    def put(level):
        admin.put(f"/api/dashboards/{did}/acl", json={"entries": [
            {"principal_type": "user", "principal_id": b.id, "level": level}]})
    put("view")
    assert b.patch(f"/api/dashboards/{did}", json={"name": "X"}).status_code == 403
    put("edit")
    assert b.patch(f"/api/dashboards/{did}", json={"name": "X"}).status_code == 200
    assert b.delete(f"/api/dashboards/{did}").status_code == 403
    assert b.post(f"/api/dashboards/{did}/share-link").status_code == 403  # sharing is 'manage'
    assert a.get(f"/api/dashboards/{did}").status_code == 404


def test_widget_data_run_needs_use(admin, make_user, fakes):
    viewer, vid = make_user("walt"), setup_view(admin, fakes)
    did = admin.post("/api/dashboards", json={"name": "D", "widgets": [W(vid)]}).json()
    wid = did["widgets"][0]["id"]
    did = did["id"]
    admin.put(f"/api/dashboards/{did}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": viewer.id, "level": "view"}]})
    admin.put(f"/api/views/{vid}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": viewer.id, "level": "view"}]})
    assert viewer.get(f"/api/dashboards/{did}/widgets/{wid}/data").status_code == 404  # nothing cached yet
    assert admin.get(f"/api/dashboards/{did}/widgets/{wid}/data", params={"run": 1}).status_code == 200
    assert viewer.get(f"/api/dashboards/{did}/widgets/{wid}/data").status_code == 200
    assert viewer.get(f"/api/dashboards/{did}/widgets/{wid}/data", params={"run": 1}).status_code == 403


def test_view_and_resource_deletion_blocked_by_dashboard(admin, fakes):
    vid = setup_view(admin, fakes)
    admin.post("/api/dashboards", json={"name": "D", "widgets": [W(vid)]})
    r = admin.delete(f"/api/views/{vid}")
    assert r.status_code == 409 and r.json()["error"]["details"]["dashboards"] == ["D"]


def test_share_link_is_cached_only_expires_and_revokes(admin, db, fakes):
    vid = setup_view(admin, fakes)
    did = admin.post("/api/dashboards", json={"name": "D", "widgets": [W(vid, title="t")]}).json()["id"]
    admin.post(f"/api/views/{vid}/run")
    calls = len(fakes.oracle.calls)
    link = admin.post(f"/api/dashboards/{did}/share-link", json={"expires_in_hours": 2}).json()
    anon = TestClient(create_app())  # no session at all
    r = anon.get(f"/api/shared/{link['token']}")
    assert r.status_code == 200 and r.json()["widgets"][0]["result"]["rows"] and "id" not in r.json()
    assert len(fakes.oracle.calls) == calls  # never a live connector call
    row = db.scalar(select(Dashboard))
    assert row.share_token_hash != link["token"]  # stored hashed
    assert anon.get("/api/shared/not-a-token").status_code == 404
    row.share_expires_at = now() - timedelta(seconds=1)
    db.commit()
    assert anon.get(f"/api/shared/{link['token']}").status_code == 404  # expired
    new = admin.post(f"/api/dashboards/{did}/share-link").json()["token"]
    assert anon.get(f"/api/shared/{new}").status_code == 200
    assert admin.delete(f"/api/dashboards/{did}/share-link").status_code == 204
    assert anon.get(f"/api/shared/{new}").status_code == 404  # revoked


def test_share_link_shows_null_when_nothing_cached(admin, fakes):
    vid = setup_view(admin, fakes)
    did = admin.post("/api/dashboards", json={"name": "D", "widgets": [W(vid)]}).json()["id"]
    tok = admin.post(f"/api/dashboards/{did}/share-link").json()["token"]
    assert TestClient(create_app()).get(f"/api/shared/{tok}").json()["widgets"][0]["result"] is None


def test_replace_widgets_and_delete(admin, fakes):
    v1, v2 = setup_view(admin, fakes, "V1"), setup_view(admin, fakes, "V2")
    d = admin.post("/api/dashboards", json={"name": "D", "widgets": [W(v1)]}).json()
    out = admin.patch(f"/api/dashboards/{d['id']}", json={"widgets": [W(v2), W(v2)]}).json()
    assert [w["view_id"] for w in out["widgets"]] == [v2, v2]
    assert admin.delete(f"/api/dashboards/{d['id']}").status_code == 204
    assert admin.delete(f"/api/views/{v1}").status_code == 204  # no longer referenced


# ---- link tiles -----------------------------------------------------------------------------
def tile(api, **kw):
    r = api.post("/api/links", json={"name": "Grafana", "url": "https://grafana.example/d/1", **kw})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x.example", "https://user:pw@x.example",
                                 "data:text/html,x", "//x.example", "not a url"])
def test_only_plain_http_urls(admin, url):
    assert admin.post("/api/links", json={"name": "T", "url": url}).status_code == 422


def test_tile_access_via_scoped_panel_access(admin, make_user):
    u = make_user("lou")
    a, b = tile(admin, name="A"), tile(admin, name="B")
    assert u.get("/api/links").json() == []
    assert u.post(f"/api/links/{a['id']}/launch").status_code == 404
    admin.put(f"/api/admin/users/{u.id}/permissions", json={"permissions": [
        {"permission": "panel.access", "scope_id": a["id"]}]})
    assert [t["name"] for t in u.get("/api/links").json()] == ["A"]  # only the scoped tile
    r = u.post(f"/api/links/{a['id']}/launch").json()
    assert r["url"] == "https://grafana.example/d/1" and r["credential_hint"] == {"mode": "none", "has_credential": False}
    assert u.post(f"/api/links/{b['id']}/launch").status_code == 404
    assert u.patch(f"/api/links/{a['id']}", json={"name": "Z"}).status_code == 403  # open != manage


def test_create_needs_link_create(admin, make_user):
    u = make_user("cal")
    assert u.post("/api/links", json={"name": "T", "url": "https://x.example"}).status_code == 403
    grant(admin, u, "link.create")
    t = tile(u, name="Mine")
    assert u.delete(f"/api/links/{t['id']}").status_code == 204  # owner manages


def test_shared_credential_hidden_until_audited_reveal(admin, make_user, db, fakes):
    u = make_user("sid")
    t = tile(admin, credential_mode="shared", credential={"username": "ops", "password": "pw-secret-9"})
    assert "pw-secret-9" not in str(admin.get("/api/links").json()) and t["has_credential"]
    admin.put(f"/api/admin/users/{u.id}/permissions", json={"permissions": [{"permission": "panel.access"}]})
    assert "pw-secret-9" not in str(u.post(f"/api/links/{t['id']}/launch").json())
    assert u.post(f"/api/links/{t['id']}/reveal").json() == {"username": "ops", "password": "pw-secret-9"}
    ev = db.scalar(select(AuditLog).where(AuditLog.action == "link.reveal"))
    assert ev is not None and str(ev.actor_id) == u.id and "pw-secret" not in str(ev.detail)
    row = db.scalar(select(LinkTile))
    assert b"pw-secret-9" not in row.shared_secret_blob
    assert admin.post("/api/links", json={"name": "N", "url": "https://x.example", "credential_mode": "shared"}).status_code == 422


def test_reveal_is_rate_limited(admin, fakes):
    t = tile(admin, credential_mode="shared", credential={"username": "a", "password": "b"})
    codes = [admin.post(f"/api/links/{t['id']}/reveal").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


def test_per_user_credentials_are_private(admin, make_user):
    u1, u2 = make_user("pu1"), make_user("pu2")
    t = tile(admin, credential_mode="per_user")
    for u in (u1, u2):
        admin.put(f"/api/admin/users/{u.id}/permissions", json={"permissions": [{"permission": "panel.access"}]})
    assert u1.post(f"/api/links/{t['id']}/launch").json()["credential_hint"]["needs_credential"] is True
    assert u1.post(f"/api/links/{t['id']}/reveal").status_code == 404
    assert u1.put(f"/api/links/{t['id']}/my-credential", json={"username": "u1", "password": "p1"}).status_code == 204
    assert u2.put(f"/api/links/{t['id']}/my-credential", json={"username": "u2", "password": "p2"}).status_code == 204
    assert u1.post(f"/api/links/{t['id']}/reveal").json() == {"username": "u1", "password": "p1"}
    assert u2.post(f"/api/links/{t['id']}/reveal").json() == {"username": "u2", "password": "p2"}
    assert admin.post(f"/api/links/{t['id']}/reveal").status_code == 404  # even the owner has none of their own
    none = tile(admin, name="Plain")
    assert admin.put(f"/api/links/{none['id']}/my-credential", json={"username": "a", "password": "b"}).status_code == 422


def test_credential_secret_bound_to_its_row(admin, db):
    from app.errors import AppError
    from app.resources import service
    a = tile(admin, name="A", credential_mode="shared", credential={"username": "a", "password": "a"})
    b = tile(admin, name="B", credential_mode="shared", credential={"username": "b", "password": "b"})
    ra, rb = (db.scalar(select(LinkTile).where(LinkTile.name == n)) for n in "AB")
    with pytest.raises(AppError):
        service.vault().open(ra.shared_secret_blob, rb.id)
    assert a and b


# ---- home -----------------------------------------------------------------------------------
def test_home_is_scoped_to_what_the_caller_can_see(admin, make_user, fakes):
    u = make_user("hal")
    a = new_oracle(admin, "A", tags=["5G", "CEIR"])["id"]
    new_oracle(admin, "B", tags=[])
    admin.post(f"/api/resources/{a}/test")
    heat = admin.get("/api/home/heatmap").json()
    assert set(heat) == {"5G", "CEIR", "untagged"} and heat["5G"][0]["status"] == "up"
    assert admin.get("/api/home/summary").json()["resources"] == {"up": 1, "unknown": 1}
    assert u.get("/api/home/heatmap").json() == {} and u.get("/api/home/summary").json()["resources"] == {}
    admin.put(f"/api/resources/{a}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "view"}]})
    assert list(u.get("/api/home/heatmap").json()) == ["5G", "CEIR"]


def test_summary_counts_failed_runs_for_visible_views(admin, make_user, fakes):
    u = make_user("sue")
    rid = new_oracle(admin)["id"]
    fakes.oracle.fail["SVC1"] = TimeoutError()
    vid = new_view(admin, [rid]).json()["id"]
    admin.post(f"/api/views/{vid}/run")
    assert admin.get("/api/home/summary").json()["failed_runs_24h"] == 1
    assert u.get("/api/home/summary").json()["failed_runs_24h"] == 0  # cannot see that view


def test_top_errors_aggregates_across_sources_and_respects_access(admin, make_user, fakes):
    u = make_user("tom")
    a, b = new_oracle(admin, "A")["id"], new_oracle(admin, "B")["id"]
    fakes.oracle.rows = {"A": [["S", "u", "timeout", 5], ["S", "u", "refused", 1]], "B": [["S", "u", "timeout", 7]]}
    vid = new_view(admin, [a, b], name="Daily errors").json()["id"]
    admin.post(f"/api/views/{vid}/run")
    top = admin.get("/api/home/top-errors").json()
    assert top["errors"][0] == {"error": "timeout", "count": 12, "servers": 2}
    assert top["errors"][1]["error"] == "refused" and top["views_used"] == 1
    assert u.get("/api/home/top-errors").json() == {"views_used": 0, "errors": []}
    admin.put(f"/api/views/{vid}/acl", json={"entries": [
        {"principal_type": "user", "principal_id": u.id, "level": "view"}]})
    assert u.get("/api/home/top-errors", params={"limit": 1}).json()["errors"][0]["count"] == 12


def test_activity_own_vs_admin(admin, make_user):
    u = make_user("ace")
    grant(admin, u, "link.create")
    u.post("/api/links", json={"name": "T", "url": "https://x.example"})
    mine = [a["action"] for a in u.get("/api/home/activity").json()]
    assert "link.create" in mine and "user.create" not in mine
    assert "user.create" in [a["action"] for a in admin.get("/api/home/activity").json()]
