"""Turn a Kubernetes PodList into rows and a health grade. Pure functions plus one fetch helper."""
import re
from datetime import datetime, timezone

NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
PAGE = 5000
OK, WARN, CRIT = "ok", "warn", "crit"
_RANK = {CRIT: 0, WARN: 1, OK: 2}  # sort order: worst first

BAD = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "CreateContainerError",
       "InvalidImageName", "RunContainerError", "OOMKilled", "Error", "Evicted", "ContainerCannotRun",
       "DeadlineExceeded"}
HIGH_RESTARTS = 5


def _age(ts: str | None, at: datetime) -> str:
    if not ts:
        return ""
    secs = int((at - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{max(secs, 0)}s"


def _stuck(statuses: list[dict], prefix: str = "") -> str:
    """First container reason that explains why the pod is not healthy."""
    for c in statuses:
        st = c.get("state") or {}
        w, t = st.get("waiting") or {}, st.get("terminated") or {}
        if w.get("reason") and w["reason"] != "PodInitializing":
            return prefix + w["reason"]
        if t.get("exitCode") and t.get("reason"):  # a failed init container or crashed container
            return prefix + t["reason"]
    return ""


def parse_pod(item: dict, at: datetime) -> dict:
    meta, spec, st = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
    phase = st.get("phase", "Unknown")
    cs = st.get("containerStatuses") or []
    total = len(spec.get("containers") or []) or len(cs)
    ready = sum(1 for c in cs if c.get("ready"))
    restarts = sum(c.get("restartCount", 0) for c in cs)

    reason = _stuck(st.get("initContainerStatuses") or [], "Init:") or _stuck(cs) or st.get("reason", "")
    if not reason and phase == "Pending":
        reason = next((c.get("reason", "") for c in st.get("conditions") or []
                       if c.get("type") == "PodScheduled" and c.get("status") == "False"), "")
    if meta.get("deletionTimestamp"):
        reason = "Terminating"
    state = "Completed" if phase == "Succeeded" else (reason or phase)

    if phase == "Succeeded":
        status = OK
    elif phase in ("Failed", "Unknown") or reason.removeprefix("Init:") in BAD:
        status = CRIT
    elif phase == "Running" and not reason and ready == total:
        status = WARN if restarts >= HIGH_RESTARTS else OK
    else:
        status = WARN  # Pending, ContainerCreating, Terminating, or running but not ready
    return {"namespace": meta.get("namespace", ""), "name": meta.get("name", ""), "state": state,
            "ready": f"{ready}/{total}", "restarts": restarts, "age": _age(meta.get("creationTimestamp"), at),
            "node": spec.get("nodeName", ""), "status": status}


def fetch_pods(conn, h, namespaces: list[str]) -> tuple[list[dict], bool]:
    """All pods (worst first) of the namespaces, or of the whole cluster when none are given."""
    at, pods, truncated = datetime.now(timezone.utc), [], False
    paths = [f"/api/v1/namespaces/{ns}/pods" for ns in namespaces] or ["/api/v1/pods"]
    for path in paths:
        body = conn.get_json(h, path, {"limit": PAGE})
        pods += [parse_pod(i, at) for i in body.get("items") or []]
        truncated |= bool((body.get("metadata") or {}).get("continue"))
    pods.sort(key=lambda p: (_RANK[p["status"]], p["namespace"], p["name"]))
    return pods, truncated
