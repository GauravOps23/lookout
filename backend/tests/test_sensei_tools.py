"""Sensei: counts, dashboards, views by name, raw API bodies."""
import uuid

import pytest
from sqlalchemy import select

from app.llm import tools
from app.llm.tools import ToolError, reduce
from app.models import User
from app.resources.connectors.base import ConnectorError
from tests.helpers import grant, new_oracle, new_view
from tests.test_sensei import ask, call, enable, llm, say, setup_view  # noqa: F401  (llm is a fixture)

CFG = {"max_rows": 20, "max_chars": 6000}


def admin_user(db):
    return db.scalar(select(User).where(User.login == "admin"))


def user_of(db, api):
    return db.get(User, uuid.UUID(api.id))


def new_ssh(admin, name, allowed=True):
    r = admin.post("/api/resources", json={"type": "ssh", "name": name, "tags": ["prod"], "llm_allowed": allowed,
                                           "config": {"host": f"{name}.x", "username": "u", "auth": "password"}, "secret": {"password": "p"}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_dashboard(admin, name, view_ids):
    widgets = [{"view_id": v, "title": f"W{i}", "grid": {"x": 0, "y": i * 4, "w": 6, "h": 4}} for i, v in enumerate(view_ids)]
    r = admin.post("/api/dashboards", json={"name": name, "description": "wall", "widgets": widgets})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---- counts --------------------------------------------------------------------------------------
def test_counts_up_down_and_what_is_left_out(admin, make_user, fakes, db):
    a, b = new_ssh(admin, "A"), new_ssh(admin, "B")
    new_ssh(admin, "HIDDEN", allowed=False)                       # the administrator did not allow Sensei to see it
    c = new_oracle(admin, "C")["id"]
    admin.patch(f"/api/resources/{c}", json={"llm_allowed": True})
    admin.post(f"/api/resources/{a}/test")
    fakes.ssh.fail = ConnectorError("Connection refused")
    admin.post(f"/api/resources/{b}/test")                          # B goes down
    out, sources = tools.execute(db, admin_user(db), CFG, "get_resource_counts", {})
    assert out["overall"] == {"total": 3, "up": 1, "down": 1, "not_checked": 1}
    assert out["by_type"]["ssh"] == {"total": 2, "up": 1, "down": 1, "not_checked": 0}
    assert out["by_type"]["oracle"] == {"total": 1, "up": 0, "down": 0, "not_checked": 1}
    assert out["down"] == [{"name": "B", "type": "ssh", "error": "Connection refused"}]
    assert sources == [{"type": "resource", "id": b, "name": "B"}]
    only = tools.execute(db, admin_user(db), CFG, "get_resource_counts", {"type": "oracle"})[0]
    assert only["overall"]["total"] == 1 and only["down"] == []
    with pytest.raises(ToolError, match="VALIDATION"):
        tools.execute(db, admin_user(db), CFG, "get_resource_counts", {"type": "mainframe"})


def test_counts_only_cover_what_the_user_can_see(admin, make_user, fakes, db):
    a = new_ssh(admin, "A")
    u = make_user("cora")
    empty = tools.execute(db, user_of(db, u), CFG, "get_resource_counts", {})[0]
    assert empty["overall"]["total"] == 0 and "allows each one" in empty["note"]
    admin.put(f"/api/resources/{a}/acl", json={"entries": [{"principal_type": "user", "principal_id": u.id, "level": "view"}]})
    assert tools.execute(db, user_of(db, u), CFG, "get_resource_counts", {})[0]["overall"]["total"] == 1


# ---- views by name ---------------------------------------------------------------------------------
def test_a_view_can_be_named_instead_of_identified(admin, llm, fakes, db):
    rid, vid = setup_view(admin, name="Daily errors")
    for spelled in ("Daily errors", "  daily ERRORS "):
        out, sources = tools.execute(db, admin_user(db), CFG, "run_view", {"view_id": spelled})
        assert out["row_count"] == 1 and sources == [{"type": "view", "id": vid, "name": "Daily errors"}]
    with pytest.raises(ToolError, match="NOT_FOUND"):
        tools.execute(db, admin_user(db), CFG, "run_view", {"view_id": "no such view"})
    # a second view with the same name but another type makes the name ambiguous
    ssh = new_ssh(admin, "S1")
    assert admin.post("/api/views", json={"type": "ssh_health", "name": "Daily errors", "llm_allowed": True, "source_resource_ids": [ssh],
                                          "config": {"checks": [{"id": "uptime"}]}}).status_code == 201
    with pytest.raises(ToolError, match="AMBIGUOUS"):
        tools.execute(db, admin_user(db), CFG, "run_view", {"view_id": "Daily errors"})
    assert tools.execute(db, admin_user(db), CFG, "run_view", {"view_id": vid})[0]["row_count"] == 1  # the id still works


def test_a_named_view_the_assistant_may_not_use_stays_hidden(admin, fakes, db):
    rid = new_oracle(admin)["id"]
    admin.patch(f"/api/resources/{rid}", json={"llm_allowed": True})
    new_view(admin, [rid], name="Private")                       # view.llm_allowed is false
    with pytest.raises(ToolError, match="NOT_FOUND"):
        tools.execute(db, admin_user(db), CFG, "run_view", {"view_id": "Private"})
    assert fakes.oracle.calls == []


# ---- dashboards ------------------------------------------------------------------------------------
def test_list_and_summarize_a_dashboard_by_name(admin, fakes, db):
    _, v1 = setup_view(admin, name="Errors")
    rid2 = new_oracle(admin, "B")["id"]
    admin.patch(f"/api/resources/{rid2}", json={"llm_allowed": True})
    v2 = new_view(admin, [rid2], name="Not for the bot").json()["id"]            # view.llm_allowed false
    did = make_dashboard(admin, "NOC wall", [v1, v2])
    admin.post(f"/api/views/{v1}/run?fresh=true", json={"params": {}})
    fakes.oracle.calls.clear()

    listed, srcs = tools.execute(db, admin_user(db), CFG, "list_dashboards", {})
    assert listed == [{"id": did, "name": "NOC wall", "description": "wall", "widgets": 2, "available_to_assistant": 1}]
    assert srcs == [{"type": "dashboard", "id": did, "name": "NOC wall"}]

    out, sources = tools.execute(db, admin_user(db), CFG, "summarize_dashboard", {"dashboard": "noc WALL"})
    assert out["dashboard"] == "NOC wall" and out["widget_count"] == 2 and out["not_available_to_assistant"] == 1
    assert len(out["widgets"]) == 1 and out["widgets"][0]["view"] == "Errors" and out["widgets"][0]["row_count"] == 1
    assert "Not for the bot" not in str(out)                                     # nothing about the hidden widget leaks
    assert [s["type"] for s in sources] == ["dashboard", "view"]
    assert fakes.oracle.calls == []                                              # stored results only, no queries
    assert tools.execute(db, admin_user(db), CFG, "summarize_dashboard", {"dashboard": did})[0]["dashboard"] == "NOC wall"


def test_summary_runs_only_widgets_without_a_result_when_asked(admin, fakes, db):
    _, v1 = setup_view(admin, name="Fresh")
    did = make_dashboard(admin, "Board", [v1])
    quiet = tools.execute(db, admin_user(db), CFG, "summarize_dashboard", {"dashboard": "Board"})[0]
    assert "no stored result" in quiet["widgets"][0]["note"] and fakes.oracle.calls == []
    loud = tools.execute(db, admin_user(db), CFG, "summarize_dashboard", {"dashboard": "Board", "refresh": True})[0]
    assert loud["widgets"][0]["row_count"] == 1 and len(fakes.oracle.calls) == 1
    with pytest.raises(ToolError, match="VALIDATION"):
        tools.execute(db, admin_user(db), CFG, "summarize_dashboard", {"dashboard": "Board", "refresh": "yes"})
    assert did


def test_dashboard_names_must_match_one_and_be_visible(admin, make_user, fakes, db):
    _, v1 = setup_view(admin, name="V")
    one = make_dashboard(admin, "Ops east", [v1])
    make_dashboard(admin, "Ops west", [v1])
    admin_u = admin_user(db)
    assert tools.execute(db, admin_u, CFG, "summarize_dashboard", {"dashboard": "east"})[0]["dashboard"] == "Ops east"   # a part is enough
    with pytest.raises(ToolError, match="AMBIGUOUS.*Ops east.*Ops west"):
        tools.execute(db, admin_u, CFG, "summarize_dashboard", {"dashboard": "ops"})
    with pytest.raises(ToolError, match="NOT_FOUND"):
        tools.execute(db, admin_u, CFG, "summarize_dashboard", {"dashboard": "nothing like it"})
    with pytest.raises(ToolError, match="VALIDATION"):
        tools.execute(db, admin_u, CFG, "summarize_dashboard", {"dashboard": "  "})
    u = make_user("dave")                                                           # no access to either dashboard
    assert tools.execute(db, user_of(db, u), CFG, "list_dashboards", {})[0] == []
    with pytest.raises(ToolError, match="NOT_FOUND"):
        tools.execute(db, user_of(db, u), CFG, "summarize_dashboard", {"dashboard": "Ops east"})
    admin.put(f"/api/dashboards/{one}/acl", json={"entries": [{"principal_type": "user", "principal_id": u.id, "level": "view"}]})
    shown = tools.execute(db, user_of(db, u), CFG, "summarize_dashboard", {"dashboard": "ops"})[0]
    assert shown["dashboard"] == "Ops east" and shown["not_available_to_assistant"] == 1   # sees the dashboard, not the view behind it


def test_a_big_dashboard_is_summarised_in_part(admin, fakes, db):
    rid = new_oracle(admin)["id"]
    admin.patch(f"/api/resources/{rid}", json={"llm_allowed": True})
    views = [new_view(admin, [rid], name=f"V{i}", llm_allowed=True).json()["id"] for i in range(14)]
    make_dashboard(admin, "Large", views)
    out = tools.execute(db, admin_user(db), CFG, "summarize_dashboard", {"dashboard": "Large"})[0]
    assert out["widget_count"] == 14 and len(out["widgets"]) == tools.MAX_DASHBOARD_WIDGETS and "first 12 of 14" in out["note"]


# ---- raw API bodies --------------------------------------------------------------------------------
def test_a_raw_api_body_is_kept_far_longer_than_a_table_cell():
    cols = [{"name": "status", "type": "number"}, {"name": "body", "type": "string"}]
    res = {"columns": cols, "rows": [[200, "x" * 3000]], "meta": {}}
    assert len(reduce(res, 5, 20000)["rows"][0][1]) == 3000
    assert len(reduce({**res, "rows": [[200, "x" * 9000]]}, 5, 20000)["rows"][0][1]) == tools.RAW_BODY_CELL + 1
    other = {"columns": [{"name": "note", "type": "string"}], "rows": [["y" * 3000]], "meta": {}}
    assert len(reduce(other, 5, 20000)["rows"][0][0]) == tools.MAX_CELL + 1


# ---- switching Sensei on for a view or an API request ----------------------------------------------
def test_the_sensei_switch_on_a_view_works_both_ways(admin, fakes, db):
    rid = new_oracle(admin)["id"]
    admin.patch(f"/api/resources/{rid}", json={"llm_allowed": True})
    vid = new_view(admin, [rid], name="Switch").json()["id"]
    names = lambda: [v["name"] for v in tools.execute(db, admin_user(db), CFG, "list_views", {})[0]]
    assert names() == []
    assert admin.patch(f"/api/views/{vid}", json={"name": "Switch", "llm_allowed": True}).json()["llm_allowed"] is True
    assert names() == ["Switch"]
    assert admin.patch(f"/api/views/{vid}", json={"llm_allowed": False}).json()["llm_allowed"] is False
    assert names() == []


def test_sensei_sends_a_saved_api_request_and_sees_the_whole_body(admin, fakes, db):
    api = admin.post("/api/resources", json={"type": "api", "name": "crm", "llm_allowed": True, "config": {
        "kind": "rest", "base_url": "https://crm.example/api", "allowed_methods": ["GET"]}}).json()["id"]
    v = admin.post("/api/views", json={"type": "api_response", "name": "Customer", "llm_allowed": True, "source_resource_ids": [api],
                                       "cache_ttl_s": 0, "config": {"output": "raw", "path": "/c/{{id}}", "variables": [
                                           {"name": "id", "pattern": "^[0-9]+$"}]}}).json()["id"]
    fakes.http.body = ('{"name": "' + "A" * 1500 + '"}').encode()
    out, sources = tools.execute(db, admin_user(db), CFG, "run_view", {"view_id": "customer", "params": {"id": "7"}})
    assert out["columns"] == ["status", "body"] and out["rows"][0][0] == 200 and "A" * 1500 in out["rows"][0][1]
    assert fakes.http.calls[0]["url"] == "https://crm.example/api/c/7" and sources[0]["id"] == v
    with pytest.raises(ToolError, match="VALIDATION|MISSING"):
        tools.execute(db, admin_user(db), CFG, "run_view", {"view_id": "customer", "params": {"id": "7/../9"}})


# ---- through the agent loop ------------------------------------------------------------------------
def test_asking_how_many_servers_are_down_uses_the_counts_tool(admin, llm, fakes):
    a = new_ssh(admin, "A")
    admin.post(f"/api/resources/{a}/test")
    fakes.ssh.fail = ConnectorError("Connection refused")
    b = new_ssh(admin, "B")
    admin.post(f"/api/resources/{b}/test")
    enable(admin)
    llm.script = [call("get_resource_counts", {}), say("2 servers: 1 up, 1 down (B).")]
    _, ev = ask(admin, "how many servers are up and down?")
    assert [e for e, _ in ev][:2] == ["tool_call", "tool_result"] and ev[1][1] == {"name": "get_resource_counts", "ok": True}
    sent = llm.sent[1]["messages"][-1]["content"]
    assert '"total": 2' in sent and '"down": 1' in sent and '"name": "B"' in sent and sent.startswith('<tool_result untrusted="true">')
    assert [s for e, s in ev if e == "sources"][0] == [{"type": "resource", "id": b, "name": "B"}]
    assert "get_resource_counts" in llm.sent[0]["messages"][0]["content"]       # the instructions point the model at it
