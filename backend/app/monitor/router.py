"""Live status pages: Linux host health (spec: Servers) and OpenShift pods (Pods).

Read-only. A caller sees only the connections they hold at least `use` on, the same bar as running a view.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.access import levels_for
from app.authz.acl import Level
from app.db import get_db
from app.deps import Ctx, current_ctx
from app.models import Resource, ResourceAcl
from app.monitor.cache import TtlCache
from app.resources import service
from app.resources.connectors.base import sanitize
from app.views import engine
from app.views.ocp_pods import parse as pods_parse
from app.views.ssh_health import snapshot

router = APIRouter(prefix="/api", tags=["monitor"])

SERVER_TTL_S, POD_TTL_S, WORKERS = 60, 30, 8
_servers, _pods = TtlCache(), TtlCache()


def _usable(db: Session, ctx: Ctx, rtype: str) -> list[Resource]:
    rows = list(db.scalars(select(Resource).where(Resource.type == rtype).order_by(Resource.name)))
    seen = levels_for(db, ctx.user, rows, ResourceAcl)
    return [r for r in rows if seen.get(r.id, 0) >= Level.use]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _gather(db: Session, rows: list[Resource], cache: TtlCache, ttl: int, fresh: bool, probe) -> list[dict]:
    """Run `probe(handle)` for each resource in a pool; handles are built here because the DB session stays on this thread."""
    handles = {r.id: service.handle(r) for r in rows}

    def one(r: Resource):
        h = handles[r.id]

        def compute():
            try:
                with engine._sem(h), engine.GLOBAL_SLOTS:
                    return {"status": "up", "error": "", **probe(h)}
            except Exception as e:  # noqa: BLE001 - shown per connection with a sanitised message
                return {"status": "down", "error": sanitize(e)}

        value, hit, at = cache.get_or_compute(("probe", str(r.id)), ttl, compute, fresh)
        return r, h, {"id": str(r.id), "name": r.name, "tags": r.tags, **value, "cached": hit, "checked_at": _iso(at)}

    with ThreadPoolExecutor(min(WORKERS, max(1, len(rows)))) as pool:
        done = list(pool.map(one, rows))
    for r, h, out in done:  # persist a first-seen SSH host key (TOFU) like a manual test would
        service.apply_observed(db, r, h, out["error"])
    return [out for _, _, out in done]


@router.get("/servers/health")
def servers_health(fresh: bool = False, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    ssh = service.CONNECTORS["ssh"]

    def probe(h):
        return snapshot.parse(ssh.exec(h, snapshot.COMMAND, 30))

    return {"servers": _gather(db, _usable(db, ctx, "ssh"), _servers, SERVER_TTL_S, fresh, probe)}


def _summary(pods: list[dict], namespace: str | None, show_all: bool) -> list[dict]:
    by_ns: dict[str, list[dict]] = {}
    for p in pods:
        by_ns.setdefault(p["namespace"], []).append(p)
    out = []
    for ns, items in sorted(by_ns.items()):
        counts = {k: sum(1 for p in items if p["status"] == k) for k in ("ok", "warn", "crit")}
        entry = {"name": ns, "total": len(items), **counts, "problems": [p for p in items if p["status"] != "ok"]}
        if show_all and ns == namespace:
            entry["pods"] = items
        out.append(entry)
    return out


@router.get("/pods")
def pods(fresh: bool = False, cluster: str | None = None, namespace: str | None = Query(None, max_length=63),
         all: bool = False, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """Per cluster: pods grouped by namespace with counts and every pod that is not healthy.
    Pass `cluster`, `namespace` and `all=true` to also get one namespace's complete pod list."""
    rows = [r for r in _usable(db, ctx, "ocp") if cluster is None or str(r.id) == cluster]
    conn = service.connector_for(db, "ocp")

    def probe(h):
        found, truncated = pods_parse.fetch_pods(conn, h, h.config.get("namespaces", []))
        return {"pods": found, "truncated": truncated}

    out = []
    for c in _gather(db, rows, _pods, POD_TTL_S, fresh, probe):
        found = c.pop("pods", [])
        out.append({**c, "namespaces": _summary(found, namespace, all), "truncated": c.pop("truncated", False),
                    "totals": {k: sum(1 for p in found if p["status"] == k) for k in ("ok", "warn", "crit")}})
    return {"clusters": out}
