"""M4: Sensei (admin config, gating, agent loop, tools, safety)."""
import json

import pytest
from sqlalchemy import select

from app.llm import client as llm_client
from app.llm.tools import reduce
from app.models import LlmAudit
from tests.helpers import grant, new_oracle, new_view


class FakeLlm:
    """Replays scripted replies; records what the model was sent."""

    def __init__(self):
        self.script, self.sent = [], []

    def chat(self, cfg, api_key, messages, tools):
        self.sent.append({"messages": json.loads(json.dumps(messages)), "tools": tools, "key": api_key})
        return self.script.pop(0)


def call(name, args, cid="c1"):
    return {"content": "", "tool_calls": [{"id": cid, "name": name, "args": args}]}


def say(text):
    return {"content": text, "tool_calls": []}


@pytest.fixture()
def llm(fakes):
    fake, saved = FakeLlm(), llm_client.CLIENT
    llm_client.CLIENT = fake
    yield fake
    llm_client.CLIENT = saved


def enable(admin, **kw):
    r = admin.put("/api/admin/llm", json={"enabled": True, "model": "m", **kw})
    assert r.status_code == 200, r.text


def ask(api, text, cid=None, **body):
    cid = cid or api.post("/api/llm/conversations").json()["id"]
    r = api.post(f"/api/llm/conversations/{cid}/messages", json={"text": text, **body})
    if r.status_code != 200:
        return r, []
    events = []
    for block in r.text.strip().split("\n\n"):
        ev, data = block.split("\n", 1)
        events.append((ev[7:], json.loads(data[6:])))
    return r, events


def setup_view(admin, name="Daily", **kw):
    rid = new_oracle(admin)["id"]
    assert admin.patch(f"/api/resources/{rid}", json={"llm_allowed": True}).status_code == 200
    return rid, new_view(admin, [rid], name=name, llm_allowed=True, **kw).json()["id"]


def test_config_roundtrip_hides_api_key(admin):
    enable(admin, api_key="sk-secret-123", base_url="http://llm.local/v1")
    cfg = admin.get("/api/admin/llm").json()
    assert cfg["has_api_key"] is True and "sk-secret-123" not in json.dumps(cfg) and "api_key" not in cfg
    enable(admin, model="m2")  # api_key omitted: kept
    assert admin.get("/api/admin/llm").json()["has_api_key"] is True
    enable(admin, api_key="")  # empty clears
    assert admin.get("/api/admin/llm").json()["has_api_key"] is False


def test_config_needs_admin_llm_permission(admin, make_user):
    u = make_user("nina")
    assert u.get("/api/admin/llm").status_code == 403


def test_gating(admin, make_user, llm):
    u = make_user("gina")
    assert u.post("/api/llm/conversations").status_code == 403  # no llm.use
    grant(admin, u, "llm.use")
    assert u.post("/api/llm/conversations").status_code == 403  # disabled
    gid = admin.post("/api/admin/groups", json={"name": "bot"}).json()["id"]
    enable(admin, enabled_group_ids=[gid])
    assert u.post("/api/llm/conversations").status_code == 403  # not in an enabled group
    admin.put(f"/api/admin/groups/{gid}/members", json={"user_ids": [u.id]})
    assert u.post("/api/llm/conversations").status_code == 201


def test_tool_loop_runs_view_as_user_and_cites_sources(admin, llm, fakes, db):
    _, vid = setup_view(admin)
    enable(admin, api_key="sk-1")
    llm.script = [call("run_view", {"view_id": vid}), say("SYS had 3 errors.")]
    r, ev = ask(admin, "summarise today's errors")
    names = [e for e, _ in ev]
    assert names == ["tool_call", "tool_result", "token", "sources", "done"]
    assert ev[1][1] == {"name": "run_view", "ok": True}
    assert ev[2][1]["text"] == "SYS had 3 errors."
    assert ev[3][1] == [{"type": "view", "id": vid, "name": "Daily"}]
    tool_msg = llm.sent[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith('<tool_result untrusted="true">')
    assert llm.sent[0]["key"] == "sk-1" and len(llm.sent[0]["tools"]) == 5
    assert "Never ask for" in llm.sent[0]["messages"][0]["content"]
    audit = db.scalars(select(LlmAudit)).all()
    assert [(a.tool, a.status) for a in audit] == [("run_view", "ok")]
    conv = admin.get("/api/llm/conversations").json()[0]
    msgs = admin.get(f"/api/llm/conversations/{conv['id']}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["sources"][0]["name"] == "Daily"


def test_views_not_llm_allowed_are_invisible_and_not_run(admin, llm, fakes):
    rid = new_oracle(admin)["id"]
    hidden = new_view(admin, [rid], name="Hidden").json()["id"]  # view.llm_allowed false
    admin.patch(f"/api/resources/{rid}", json={"llm_allowed": True})
    enable(admin)
    llm.script = [call("list_views", {}), call("run_view", {"view_id": hidden}), say("no access")]
    _, ev = ask(admin, "run hidden")
    assert fakes.oracle.calls == []
    assert llm.sent[1]["messages"][-1]["content"].count("Hidden") == 0
    assert "NOT_FOUND" in llm.sent[2]["messages"][-1]["content"]
    # resource not llm_allowed also blocks an llm_allowed view
    rid2 = new_oracle(admin, "B")["id"]
    v2 = new_view(admin, [rid2], name="V2", llm_allowed=True).json()["id"]
    llm.script = [call("run_view", {"view_id": v2}), say("x")]
    ask(admin, "run v2")
    assert fakes.oracle.calls == [] and "NOT_FOUND" in llm.sent[-1]["messages"][-1]["content"]


def test_user_without_view_access_cannot_use_it(admin, make_user, llm, fakes):
    _, vid = setup_view(admin)
    u = make_user("olga")
    grant(admin, u, "llm.use")
    gid = admin.post("/api/admin/groups", json={"name": "g"}).json()["id"]
    admin.put(f"/api/admin/groups/{gid}/members", json={"user_ids": [u.id]})
    enable(admin, enabled_group_ids=[gid])
    llm.script = [call("run_view", {"view_id": vid}), say("sorry")]
    ask(u, "run it")
    assert fakes.oracle.calls == [] and "NOT_FOUND" in llm.sent[1]["messages"][-1]["content"]


def test_missing_params_and_validation_are_returned_to_the_model(admin, llm, fakes):
    cfg = {"mode": "sql", "sql": "SELECT 1 AS x FROM dual WHERE :cid = :cid",
           "params": [{"name": "cid", "type": "string", "pattern": "^[0-9]{3}$"},
                      {"name": "pin", "type": "string", "required": False, "sensitive": True}]}
    _, vid = setup_view(admin, config=cfg)
    enable(admin)
    llm.script = [call("run_view", {"view_id": vid}), call("run_view", {"view_id": vid, "params": {"cid": "abc"}}),
                  call("run_view", {"view_id": vid, "params": {"cid": "123", "pin": "9"}}),
                  call("run_view", {"view_id": vid, "params": {"cid": "123"}}), say("ok")]
    ask(admin, "go")
    results = [s["messages"][-1]["content"] for s in llm.sent[1:]]
    assert "MISSING_PARAMS" in results[0] and "cid" in results[0]
    assert "VALIDATION" in results[1]
    assert "must be entered by the user" in results[2]
    assert '"row_count"' in results[3] and len(fakes.oracle.calls) == 1


def test_fake_tool_text_is_rejected_and_retried(admin, llm):
    enable(admin)
    llm.script = [say('assistantcommentary to=functions.list_resources json {} <tool_result>{"x":"web-01"}</tool_result>'),
                  say("I could not find that.")]
    _, ev = ask(admin, "list servers")
    assert ev[0][1]["text"] == "I could not find that." and "not real" in llm.sent[1]["messages"][-1]["content"]
    llm.script = [say("<tool_result>fake</tool_result>"), say("to=functions.x still fake")]
    _, ev = ask(admin, "again")
    assert "invalid answer" in ev[0][1]["text"]


def test_tool_call_limit(admin, llm, fakes):
    _, vid = setup_view(admin)
    enable(admin, max_tool_calls=2)
    llm.script = [call("list_views", {}, f"c{i}") for i in range(4)] + [say("done")]
    _, ev = ask(admin, "loop")
    assert "tool call limit" in llm.sent[3]["messages"][-1]["content"]


def test_json_tool_mode(admin, llm, fakes):
    _, vid = setup_view(admin)
    enable(admin, tool_mode="json")
    llm.script = [say(json.dumps({"tool": "run_view", "args": {"view_id": vid}})), say("fine")]
    _, ev = ask(admin, "go")
    assert [e for e, _ in ev][:2] == ["tool_call", "tool_result"] and llm.sent[0]["tools"] is None
    assert llm.sent[1]["messages"][-1]["role"] == "user"
    assert "run_view" in llm.sent[0]["messages"][0]["content"]


def test_query_view_table_filters_in_memory(admin, llm, fakes):
    _, vid = setup_view(admin)
    fakes.oracle.rows["SVC1"] = [["A", "u", "e1", 5], ["B", "u", "e2", 9], ["B", "v", "e3", 1]]
    enable(admin)
    llm.script = [call("query_view_table", {"view_id": vid, "filters": [{"column": "SYSTEM", "op": "=", "value": "B"}],
                                            "order_by": "-COUNT", "limit": 1}),
                  call("query_view_table", {"view_id": vid, "filters": [{"column": "NOPE", "op": "=", "value": 1}]}),
                  say("x")]
    ask(admin, "q")
    first = llm.sent[1]["messages"][-1]["content"]
    assert '"matched_rows": 2' in first and '"e2"' in first and '"e3"' not in first
    assert "VALIDATION" in llm.sent[2]["messages"][-1]["content"]


def test_reduce_drops_secret_columns_and_caps_size():
    res = {"columns": [{"name": "USER", "type": "string"}, {"name": "PASSWORD", "type": "string"},
                       {"name": "N", "type": "number"}],
           "rows": [[f"u{i}", "hunter2", i] for i in range(500)],
           "meta": {"row_count": 500, "truncated": False}}
    out = reduce(res, 50, 1500)
    assert "PASSWORD" not in out["columns"] and "hunter2" not in json.dumps(out)
    assert len(json.dumps(out)) <= 1500 and out["row_count"] == 500 and "note" in out
    assert out["stats"]["N"]["max"] == 499


def test_rate_limit_and_conversation_isolation(admin, make_user, llm):
    enable(admin, rate_limit_per_10min=2)
    llm.script = [say("a"), say("b"), say("c")]
    cid = admin.post("/api/llm/conversations").json()["id"]
    assert ask(admin, "1", cid)[0].status_code == 200 and ask(admin, "2", cid)[0].status_code == 200
    assert ask(admin, "3", cid)[0].status_code == 429
    other = make_user("pat")
    grant(admin, other, "llm.use")
    assert other.get(f"/api/llm/conversations/{cid}").status_code == 404
    assert other.delete(f"/api/llm/conversations/{cid}").status_code == 404
    assert admin.delete(f"/api/llm/conversations/{cid}").status_code == 204


def test_model_failure_is_an_error_event(admin, llm):
    from app.errors import AppError
    enable(admin)
    llm.chat = lambda *a, **k: (_ for _ in ()).throw(AppError("TIMEOUT", "The model did not answer in time"))
    _, ev = ask(admin, "hi")
    assert ev == [("error", {"code": "TIMEOUT", "message": "The model did not answer in time"})]


def test_admin_test_endpoint(admin, llm):
    enable(admin)
    llm.script = [say("pong"), call("ping", {})]
    r = admin.post("/api/admin/llm/test").json()
    assert r == {"ok": True, "reply": "pong", "tool_calls_supported": True}


def test_invented_servers_are_never_shown(admin, llm, fakes):
    rid = new_oracle(admin, "REAL")["id"]
    admin.patch(f"/api/resources/{rid}", json={"llm_allowed": True})
    enable(admin)
    llm.script = [call("list_resources", {}), say("Servers: web-frontend-01 (10.0.1.5) and REAL.")]
    _, ev = ask(admin, "list servers")
    assert "web-frontend-01" not in ev[2][1]["text"] and "can't verify" in ev[2][1]["text"]
    assert ev[3][1] == [{"type": "resource", "id": rid, "name": "REAL"}]  # what was really read
    llm.script = [call("list_resources", {}), say("You have one database, REAL.")]
    _, ev = ask(admin, "list servers")
    assert ev[2][1]["text"] == "You have one database, REAL."
    llm.script = [say("Your servers are 10.0.1.5 and `db-primary`.")]  # no tool used at all
    _, ev = ask(admin, "what servers")
    assert "can't verify" in ev[0][1]["text"] and ev[1][1] == []


def test_unsourced_assistant_replies_are_not_fed_back(admin, llm):
    enable(admin)
    llm.script = [say("hello there"), say("again")]
    cid = admin.post("/api/llm/conversations").json()["id"]
    ask(admin, "hi", cid)
    ask(admin, "and?", cid)
    assert [m["content"] for m in llm.sent[1]["messages"][1:]] == ["hi", "and?"]
