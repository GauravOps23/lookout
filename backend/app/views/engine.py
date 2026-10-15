"""View execution (spec 7.5): authorization, validation, caching, coalescing, concurrency limits, fan-out.

DB access happens only on the calling thread; worker threads get pre-built Handles and run connectors.
"""
import copy
import hashlib
import json
import threading
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.access import require_level
from app.authz.acl import Level
from app.db import as_utc, now
from app.errors import AppError
from app.models import Resource, ResourceAcl, User, View, ViewAcl, ViewRun
from app.resources import service
from app.resources.connectors.base import ConnectorError, Handle, sanitize
from app.views.base import RunContext, bind_params, merge_results
from app.views.registry import REGISTRY

GLOBAL_SLOTS = threading.BoundedSemaphore(20)
PER_RESOURCE = {"oracle": 2, "ssh": 4, "api": 4}
FANOUT_WORKERS = 8

_guard = threading.Lock()
_sems: dict[str, threading.BoundedSemaphore] = {}
_locks: dict[tuple, threading.Lock] = {}


def _sem(h: Handle) -> threading.BoundedSemaphore:
    with _guard:
        return _sems.setdefault(h.id, threading.BoundedSemaphore(PER_RESOURCE[h.type]))


def _lock(key: tuple) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def params_hash(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def load_plugin(view: View):
    plugin = REGISTRY.get(view.type)
    if plugin is None:
        raise AppError("VALIDATION", f"Unknown view type: {view.type}")
    return plugin, plugin.config_schema(**view.config)


def sources_of(db: Session, view: View) -> list[Resource]:
    ids = [uuid.UUID(str(i)) for i in view.source_resource_ids]
    rows = list(db.scalars(select(Resource).where(Resource.id.in_(ids))))
    order = {str(i): n for n, i in enumerate(view.source_resource_ids)}
    return sorted(rows, key=lambda r: order[str(r.id)])


def authorize(db: Session, user: User, view: View) -> list[Resource]:
    """Re-evaluated on EVERY run: losing access to a source stops the view working (spec 4.2)."""
    require_level(db, user, view, ViewAcl, Level.use)
    resources = sources_of(db, view)
    if len(resources) != len(view.source_resource_ids):
        raise AppError("CONFLICT", "A source resource no longer exists")
    for r in resources:
        require_level(db, user, r, ResourceAcl, Level.use)
    return resources


def cached(db: Session, view: View, phash: str, ttl_s: int | None) -> tuple[dict, ViewRun] | None:
    """Latest successful stored run for these parameters; ttl_s=None accepts any age."""
    q = (select(ViewRun).where(ViewRun.view_id == view.id, ViewRun.params_hash == phash,
                               ViewRun.status.in_(("ok", "partial")), ViewRun.result.is_not(None))
         .order_by(ViewRun.started_at.desc()).limit(1))
    run = db.scalar(q)
    if run is None or (ttl_s is not None and as_utc(run.started_at) < now() - timedelta(seconds=ttl_s)):
        return None
    res = copy.deepcopy(run.result)
    age = (now() - as_utc(run.started_at)).total_seconds()
    res["meta"].update(cached=True, generated_at=as_utc(run.started_at).isoformat(), stale=age > view.cache_ttl_s)
    return res, run


def _run_one(plugin, cfg, ctx: RunContext, h: Handle):
    with _sem(h), GLOBAL_SLOTS:
        return plugin.run(ctx, cfg, h)


def execute(db: Session, view: View, resources: list[Resource], typed: dict, triggered_by: str) -> dict:
    plugin, cfg = load_plugin(view)
    for r in resources:
        if r.type != plugin.source_kind:
            raise AppError("VALIDATION", f"{view.type} views need {plugin.source_kind} resources")
    handles = [service.handle(r) for r in resources]
    ctx = RunContext(params=typed, meta={"connectors": {"api": service.connector_for(db, "api")}})
    t0, started = time.monotonic(), now()

    def work(h: Handle):
        try:
            return h, _run_one(plugin, cfg, ctx, h), ""
        except AppError:
            raise
        except Exception as e:  # noqa: BLE001 - recorded per source with a sanitised message
            return h, None, sanitize(e)

    if len(handles) == 1:
        outcomes = [work(handles[0])]
    else:
        with ThreadPoolExecutor(min(FANOUT_WORKERS, len(handles))) as pool:
            outcomes = list(pool.map(work, handles))

    by_id = {str(r.id): r for r in resources}
    sources = []
    for h, rs, err in outcomes:
        service.apply_observed(db, by_id[h.id], h, err)
        sources.append({"resource_id": h.id, "name": h.name, "status": "ok" if rs else "error",
                        "row_count": rs["meta"]["row_count"] if rs else 0, "error": err})
    good = [(h.name, rs) for h, rs, _ in outcomes if rs]
    errors = sorted({s["error"] for s in sources if s["error"]})

    if not good:
        status, result, msg = "error", None, "; ".join(errors)
    else:
        status = "partial" if errors else "ok"
        result = good[0][1] if len(handles) == 1 else merge_results(good, cfg.row_limit if hasattr(cfg, "row_limit") else 10000)
        result["meta"].update(sources=sources, cached=False, stale=False, generated_at=started.isoformat(),
                              duration_ms=int((time.monotonic() - t0) * 1000))
        msg = "; ".join(errors)
    db.add(ViewRun(view_id=view.id, params_hash=params_hash(typed), started_at=started,
                   duration_ms=int((time.monotonic() - t0) * 1000), status=status, error=msg,
                   row_count=result["meta"]["row_count"] if result else 0, result=result,
                   triggered_by=triggered_by))
    db.commit()
    if result is None:
        raise AppError("UPSTREAM_ERROR", msg or "Run failed", {"sources": sources})
    return result


def run_view(db: Session, user: User, view: View, params: dict | None, *, fresh: bool = False,
             triggered_by: str = "user") -> dict:
    resources = authorize(db, user, view)
    plugin, cfg = load_plugin(view)
    typed = bind_params(plugin.variables(cfg), params)
    phash = params_hash(typed)
    if not fresh and (hit := cached(db, view, phash, view.cache_ttl_s)):
        return hit[0]
    lk = _lock((str(view.id), phash))
    waited = lk.locked()
    with lk:  # overlapping runs of the same view+params coalesce: a waiter reuses the run that just finished
        if waited:
            db.expire_all()
            if (hit := cached(db, view, phash, 5)):
                return hit[0]
        return execute(db, view, resources, typed, triggered_by)


def cached_unchecked(db: Session, view: View, params: dict | None = None) -> dict | None:
    """Latest stored result, no access check and no connector call. Callers must have authorised the read
    (viewer with view access, or a dashboard share link)."""
    plugin, cfg = load_plugin(view)
    hit = cached(db, view, params_hash(bind_params(plugin.variables(cfg), params)), None)
    return hit[0] if hit else None


def latest_result(db: Session, user: User, view: View, params: dict | None = None) -> dict | None:
    """Cached data only. Needs view-level access (spec 4.2: view = see cached data)."""
    require_level(db, user, view, ViewAcl, Level.view)
    return cached_unchecked(db, view, params)
