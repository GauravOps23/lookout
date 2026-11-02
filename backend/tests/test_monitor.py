import pytest

from app.resources.connectors.base import ConnectorError
from app.views.ssh_health import parsers, snapshot
from tests.helpers import fakes, grant, new_view  # noqa: F401

SNAP = """##load
0.50 0.40 0.30 1/200 1234
##cores
4
##mem
              total        used        free      shared  buff/cache   available
Mem:           7900        3950        1000         100        2950        3500
Swap:          2047         512        1535
##uptime
864000.55 3000.00
##disk
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sdb1         10000000   1000000   9000000      10% /data
/dev/sda1         51474044  41179235  10294809      81% /
##cpu
cpu  100 0 100 800 0 0 0 0
cpu  110 0 110 880 20 0 0 0
"""
SSH_CFG = {"host": "h1.example", "username": "u", "auth": "password"}


def new_ssh(api, name="LX1"):
    r = api.post("/api/resources", json={"type": "ssh", "name": name, "tags": ["prod"], "config": SSH_CFG,
                                         "secret": {"password": "p"}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def new_ocp(api, name="OCP1", **cfg):
    r = api.post("/api/resources", json={"type": "ocp", "name": name, "config": {"api_url": "https://api.c1:6443", **cfg},
                                         "secret": {"token": "t"}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---- parsers -----------------------------------------------------------------------------------
def test_swap_and_iowait_checks():
    free = "Mem: 100 50 50\nSwap: 2000 1500 500\n"
    rows = parsers.parse("swap", {}, free, 25, 60)
    assert rows[0] == ("swap", "used_pct", 75.0, "%", "crit")
    assert parsers.parse("swap", {}, "Mem: 100 50 50\nSwap: 0 0 0\n")[0][2] == 0.0  # no swap is not an error
    stat = "cpu  100 0 100 800 0 0 0 0\ncpu  110 0 110 880 20 0 0 0"
    assert parsers.parse("iowait", {}, stat, 10, 30)[0] == ("iowait", "iowait_pct", 16.7, "%", "warn")
    assert parsers.command("iowait", {}).count("/proc/stat") == 2 and "swap" in parsers.CHECKS


def test_snapshot_parse_values_and_health():
    s = snapshot.parse(SNAP)
    assert s["cpu"] == {"load1": 0.5, "load5": 0.4, "load15": 0.3, "cores": 4, "per_core": 0.12, "status": "ok"}
    assert (s["mem"]["used_pct"], s["mem"]["status"]) == (50.0, "ok")
    assert (s["swap"]["used_pct"], s["swap"]["status"]) == (25.0, "warn")
    assert (s["iowait"]["pct"], s["iowait"]["status"]) == (16.7, "warn")
    assert s["uptime_days"] == 10.0
    assert [d["mount"] for d in s["disks"]] == ["/", "/data"] and s["disks"][0]["status"] == "warn"
    assert s["health"] == "warn"


def test_snapshot_survives_one_unreadable_section():
    s = snapshot.parse(SNAP.replace("864000.55 3000.00", "garbage"))
    assert s["uptime_days"] is None and s["mem"]["used_pct"] == 50.0
    assert snapshot.parse("")["health"] == "ok" and snapshot.parse("")["cpu"] is None


# ---- /api/servers/health -----------------------------------------------------------------------
def test_servers_health_runs_once_caches_and_reports_down(admin, fakes):
    rid = new_ssh(admin)
    fakes.ssh.out["echo"] = SNAP
    first = admin.get("/api/servers/health").json()["servers"][0]
    assert first["id"] == rid and first["status"] == "up" and first["health"] == "warn" and first["cached"] is False
    assert len(fakes.ssh.calls) == 1 and "/proc/stat" in fakes.ssh.calls[0]  # one connection for every metric
    assert admin.get("/api/servers/health").json()["servers"][0]["cached"] is True and len(fakes.ssh.calls) == 1
    assert admin.get("/api/servers/health", params={"fresh": "true"}).json()["servers"][0]["cached"] is False
    assert len(fakes.ssh.calls) == 2

    fakes.ssh.fail = ConnectorError("Connection refused")
    down = admin.get("/api/servers/health", params={"fresh": "true"}).json()["servers"][0]
    assert down["status"] == "down" and down["error"] == "Connection refused" and "cpu" not in down


def test_servers_health_needs_use_access(admin, make_user, fakes):
    rid = new_ssh(admin)
    fakes.ssh.out["echo"] = SNAP
    u = make_user("vera")
    assert u.get("/api/servers/health").json() == {"servers": []}  # cannot even see it
    for level, expect in (("view", 0), ("use", 1)):
        admin.put(f"/api/resources/{rid}/acl", json={"entries": [
            {"principal_type": "user", "principal_id": u.id, "level": level}]})
        assert len(u.get("/api/servers/health").json()["servers"]) == expect, level


# ---- pods --------------------------------------------------------------------------------------
def pod(name, ns="shop", phase="Running", ready=True, restarts=0, waiting=None, node="n1", created="2026-10-01T00:00:00Z",
        reason=None, conditions=None, deleting=False):
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    meta = {"name": name, "namespace": ns, "creationTimestamp": created}
    if deleting:
        meta["deletionTimestamp"] = created
    status = {"phase": phase, "containerStatuses": [{"ready": ready, "restartCount": restarts, "state": state}],
              "conditions": conditions or []}
    if reason:
        status["reason"] = reason
    return {"metadata": meta, "spec": {"nodeName": node, "containers": [{}]}, "status": status}


PODS = {"items": [
    pod("web-1"),
    pod("web-2", restarts=7),
    pod("api-1", ns="core", phase="Pending", ready=False, waiting="ContainerCreating"),
    pod("api-2", ns="core", ready=False, waiting="CrashLoopBackOff", restarts=12),
    pod("job-1", ns="core", phase="Succeeded", ready=False),
    pod("lost-1", ns="core", phase="Pending", ready=False, node="",
        conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]),
    pod("gone-1", phase="Failed", reason="Evicted", ready=False),
    pod("stop-1", deleting=True),
]}


def states(items):
    return {p["name"]: (p["state"], p["status"]) for p in items}


def test_pod_states_and_grades():
    from datetime import datetime, timezone

    from app.views.ocp_pods.parse import parse_pod

    at = datetime(2026, 10, 4, 3, tzinfo=timezone.utc)
    got = {i["metadata"]["name"]: parse_pod(i, at) for i in PODS["items"]}
    assert (got["web-1"]["state"], got["web-1"]["status"], got["web-1"]["ready"], got["web-1"]["age"]) == ("Running", "ok", "1/1", "3d")
    assert got["web-2"]["status"] == "warn"                                           # running, but restarting a lot
    assert (got["api-1"]["state"], got["api-1"]["status"]) == ("ContainerCreating", "warn")  # created, not running
    assert (got["api-2"]["state"], got["api-2"]["status"]) == ("CrashLoopBackOff", "crit")
    assert (got["job-1"]["state"], got["job-1"]["status"]) == ("Completed", "ok")
    assert (got["lost-1"]["state"], got["lost-1"]["status"]) == ("Unschedulable", "warn")
    assert (got["gone-1"]["state"], got["gone-1"]["status"]) == ("Evicted", "crit")
    assert (got["stop-1"]["state"], got["stop-1"]["status"]) == ("Terminating", "warn")


def test_pods_endpoint_groups_by_namespace(admin, fakes):
    rid = new_ocp(admin)
    fakes.ocp.json["/api/v1/pods"] = PODS
    c = admin.get("/api/pods").json()["clusters"][0]
    assert c["id"] == rid and c["status"] == "up" and c["totals"] == {"ok": 2, "warn": 4, "crit": 2}
    ns = {n["name"]: n for n in c["namespaces"]}
    assert set(ns) == {"core", "shop"} and ns["core"]["total"] == 4
    assert {p["name"] for p in ns["core"]["problems"]} == {"api-1", "api-2", "lost-1"}
    assert ns["core"]["problems"][0]["status"] == "crit"                                # worst first
    assert "pods" not in ns["core"]
    full = admin.get("/api/pods", params={"cluster": rid, "namespace": "core", "all": "true"}).json()["clusters"][0]
    assert len(next(n for n in full["namespaces"] if n["name"] == "core")["pods"]) == 4
    assert [x[1] for x in fakes.ocp.calls] == ["/api/v1/pods"]                          # two requests, one cluster fetch (cached)


def test_pods_use_the_cluster_namespace_list_and_report_down(admin, fakes):
    new_ocp(admin, namespaces=["shop"])
    fakes.ocp.json["/api/v1/namespaces/shop/pods"] = {"items": [pod("web-1")]}
    c = admin.get("/api/pods").json()["clusters"][0]
    assert [n["name"] for n in c["namespaces"]] == ["shop"] and fakes.ocp.calls[0][1] == "/api/v1/namespaces/shop/pods"
    fakes.ocp.fail = ConnectorError("The cluster rejected the token")
    d = admin.get("/api/pods", params={"fresh": "true"}).json()["clusters"][0]
    assert d["status"] == "down" and d["error"] == "The cluster rejected the token" and d["namespaces"] == []
    assert admin.post("/api/resources", json={"type": "ocp", "name": "bad", "config": {
        "api_url": "https://x", "namespaces": ["Not Valid"]}, "secret": {"token": "t"}}).status_code == 422


def test_ocp_pods_view_runs_through_the_engine(admin, fakes):
    rid = new_ocp(admin)
    fakes.ocp.json["/api/v1/pods"] = PODS
    r = admin.post("/api/views", json={"type": "ocp_pods", "name": "Pods", "source_resource_ids": [rid],
                                       "config": {"only_problems": True}})
    assert r.status_code == 201, r.text
    res = admin.post(f"/api/views/{r.json()['id']}/run?fresh=true", json={"params": {}}).json()
    names = [row[1] for row in res["rows"]]
    assert "web-1" not in names and names[0] in ("api-2", "gone-1") and len(names) == 6
    assert [c["name"] for c in res["columns"]][:3] == ["namespace", "pod", "state"]
    assert admin.post("/api/views", json={"type": "ocp_pods", "name": "x", "source_resource_ids": [rid],
                                          "config": {"namespaces": ["BAD NS"]}}).status_code == 422
