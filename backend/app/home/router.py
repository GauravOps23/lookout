"""Home-page data (spec 8). Every number is limited to what the caller is allowed to see."""
from collections import Counter, defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.access import levels_for
from app.db import get_db, now
from app.deps import Ctx, current_ctx
from app.errors import AppError
from app.models import AuditLog, Dashboard, DashboardAcl, Resource, ResourceAcl, View, ViewAcl, ViewRun
from app.views import engine
from app.legacy_import import VIEW_NAME

router = APIRouter(prefix="/api/home", tags=["home"])


def _resources(db: Session, ctx: Ctx) -> list[Resource]:
    rows = list(db.scalars(select(Resource).order_by(Resource.name)))
    seen = levels_for(db, ctx.user, rows, ResourceAcl)
    return [r for r in rows if r.id in seen]


def _views(db: Session, ctx: Ctx) -> list[View]:
    rows = list(db.scalars(select(View)))
    seen = levels_for(db, ctx.user, rows, ViewAcl)
    return [v for v in rows if v.id in seen]


@router.get("/summary")
def summary(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    res, views = _resources(db, ctx), _views(db, ctx)
    ids = [v.id for v in views]
    failed = 0
    if ids:
        failed = len(list(db.scalars(select(ViewRun.id).where(
            ViewRun.view_id.in_(ids), ViewRun.status == "error", ViewRun.started_at >= now() - timedelta(hours=24)))))
    dashboards = list(db.scalars(select(Dashboard)))
    return {"resources": dict(Counter(r.status for r in res)), "views": len(views), "failed_runs_24h": failed,
            "dashboards": len(levels_for(db, ctx.user, dashboards, DashboardAcl))}


@router.get("/heatmap")
def heatmap(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """Visible resources grouped by tag; a resource with several tags appears in each group."""
    groups: dict[str, list] = defaultdict(list)
    for r in _resources(db, ctx):
        item = {"id": str(r.id), "name": r.name, "type": r.type, "status": r.status, "last_error": r.last_error}
        for tag in r.tags or ["untagged"]:
            groups[tag].append(item)
    return dict(sorted(groups.items()))


@router.get("/top-errors")
def top_errors(limit: int = Query(10, ge=1, le=50), ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """Today's most frequent errors across every 'Daily errors' view the caller can see (cached results only)."""
    counts: Counter = Counter()
    servers: dict[str, set] = defaultdict(set)
    used = 0
    for v in _views(db, ctx):
        if v.name != VIEW_NAME or v.type != "db_table":
            continue
        try:
            res = engine.cached_unchecked(db, v, {})
        except AppError:
            res = None
        if not res:
            continue
        used += 1
        names = [c["name"] for c in res["columns"]]
        if not {"ERROR", "COUNT"} <= set(names):
            continue
        e, c, s = names.index("ERROR"), names.index("COUNT"), names.index("SOURCE") if "SOURCE" in names else None
        for row in res["rows"]:
            if isinstance(row[c], (int, float)):
                counts[row[e]] += row[c]
                if s is not None:
                    servers[row[e]].add(row[s])
    return {"views_used": used, "errors": [{"error": k, "count": n, "servers": len(servers[k])}
                                           for k, n in counts.most_common(limit)]}


@router.get("/activity")
def activity(limit: int = Query(20, ge=1, le=100), ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """Your own recent actions; admins see everyone's."""
    q = select(AuditLog).order_by(AuditLog.at.desc()).limit(limit)
    if not ctx.user.is_admin:
        q = q.where(AuditLog.actor_id == ctx.user.id)
    return [{"at": a.at.isoformat(), "action": a.action, "target_type": a.target_type, "target_id": a.target_id}
            for a in db.scalars(q)]
