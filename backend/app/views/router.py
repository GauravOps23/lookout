import json
import uuid
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.authz.access import levels_for, require_level
from app.authz.acl import Level
from app.authz.permissions import has_capability
from app.db import get_db
from app.deps import Ctx, current_ctx
from app.errors import AppError
from app.models import Dashboard, DashboardWidget, Group, Resource, ResourceAcl, User, View, ViewAcl
from app.resources.router import AclIn
from app.views import engine
from app.views.base import has_unsatisfied_required
from app.views.db_table.plugin import DAILY_ERRORS_SQL
from app.views.registry import REGISTRY

router = APIRouter(prefix="/api", tags=["views"])
RENDERERS = {"table", "kv_cards", "chart_line", "chart_bar", "status_badge", "raw_json"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ViewCreate(Strict):
    type: str
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    config: dict[str, Any]
    source_resource_ids: list[uuid.UUID] = Field([], max_length=200)  # empty for trend views
    renderer: str | None = None
    schedule_cron: str | None = None
    cache_ttl_s: int = Field(60, ge=0, le=86400)
    llm_allowed: bool = False


class ViewPatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None
    config: dict[str, Any] | None = None
    source_resource_ids: list[uuid.UUID] | None = Field(None, max_length=200)
    renderer: str | None = None
    schedule_cron: str | None = None
    cache_ttl_s: int | None = Field(None, ge=0, le=86400)
    llm_allowed: bool | None = None


class RunIn(Strict):
    params: dict[str, Any] = {}


def _plugin(vtype: str):
    p = REGISTRY.get(vtype)
    if p is None:
        raise AppError("VALIDATION", f"Unknown view type: {vtype}")
    return p


def _validate_config(plugin, config: dict):
    try:
        return plugin.config_schema(**config)
    except ValidationError as e:
        raise AppError("VALIDATION", "Invalid view configuration",
                       {"errors": [{"loc": [str(p) for p in x["loc"]], "msg": x["msg"]} for x in e.errors()]}) from None


def _check_cron(cron: str | None, plugin, cfg) -> None:
    if not cron:
        return
    try:
        CronTrigger.from_crontab(cron)
    except ValueError:
        raise AppError("VALIDATION", "Invalid cron expression (5 fields)") from None
    if (missing := has_unsatisfied_required(plugin.variables(cfg))):
        raise AppError("VALIDATION", "Scheduled views need a default for required variables",
                       {"missing_defaults": missing})


def _check_sources(db: Session, ctx: Ctx, plugin, ids: list[uuid.UUID]) -> None:
    ids = list(dict.fromkeys(ids))
    if engine.is_derived(plugin):
        if ids:
            raise AppError("VALIDATION", f"{plugin.key} views read other views, not resources")
        return
    if not ids:
        raise AppError("VALIDATION", "At least one source resource is required")
    for rid in ids:
        r = db.get(Resource, rid)
        if r is None:
            raise AppError("VALIDATION", "Unknown source resource")
        require_level(db, ctx.user, r, ResourceAcl, Level.use)  # creating a view needs 'use' on its sources
        if r.type != plugin.source_kind:
            raise AppError("VALIDATION", f"{plugin.key} views need {plugin.source_kind} resources")


def _check_derived(db: Session, ctx: Ctx, plugin, cfg) -> None:
    """Trend views: creating one needs 'use' on every source view; sources must be of an allowed
    type and a trend source must be scheduled (spec 7.3)."""
    if not engine.is_derived(plugin):
        return
    for src in engine.source_views(db, plugin, cfg):
        require_level(db, ctx.user, src, ViewAcl, Level.use)
        if src.type not in plugin.allowed_source_types:
            raise AppError("VALIDATION", f"{plugin.key} views cannot read {src.type} views")
        if getattr(plugin, "needs_scheduled_source", False) and not src.schedule_cron:
            raise AppError("VALIDATION", "The source view must be scheduled so history is recorded")


def _check_custom(db: Session, ctx: Ctx, cfg) -> None:
    if getattr(cfg, "custom", None) and not has_capability(db, ctx.user, "view.ssh.custom"):
        raise AppError("FORBIDDEN", "Custom SSH commands need the view.ssh.custom permission")


def out(v: View, level: Level, with_config: bool = True) -> dict:
    return {"id": str(v.id), "type": v.type, "name": v.name, "description": v.description,
            "owner_id": str(v.owner_id), "source_resource_ids": [str(i) for i in v.source_resource_ids],
            "renderer": v.renderer, "schedule_cron": v.schedule_cron, "cache_ttl_s": v.cache_ttl_s,
            "llm_allowed": v.llm_allowed, "level": level.name, "config": v.config if with_config else None}


def _unique_name(db: Session, vtype: str, name: str, exclude: uuid.UUID | None = None) -> None:
    dup = db.scalar(select(View).where(View.type == vtype, View.name == name))
    if dup is not None and dup.id != exclude:
        raise AppError("CONFLICT", "A view of this type with that name already exists")


def _get(db: Session, ctx: Ctx, vid: uuid.UUID, required: Level) -> View:
    v = db.get(View, vid)
    if v is None:
        raise AppError("NOT_FOUND", "Not found")
    require_level(db, ctx.user, v, ViewAcl, required)
    return v


@router.get("/view-types")
def view_types(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    return [{"key": p.key, "source_kind": p.source_kind, "default_renderer": p.default_renderer,
             "can_create": has_capability(db, ctx.user, p.permission),
             "config_schema": p.config_schema.model_json_schema()} for p in REGISTRY.values()]


@router.get("/view-templates")
def view_templates(ctx: Ctx = Depends(current_ctx)):
    return [{"name": "Daily errors", "type": "db_table", "cache_ttl_s": 300,
             "description": "Today's failed requests (req_status 8/9) grouped by system, user and message",
             "config": {"mode": "sql", "sql": DAILY_ERRORS_SQL}}]


@router.get("/views")
def list_views(type: str | None = None, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    stmt = select(View).order_by(View.name)
    if type:
        stmt = stmt.where(View.type == type)
    rows = list(db.scalars(stmt))
    lv = levels_for(db, ctx.user, rows, ViewAcl)
    return [out(v, lv[v.id], lv[v.id] >= Level.use) for v in rows if v.id in lv]


@router.post("/views", status_code=201)
def create_view(body: ViewCreate, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    plugin = _plugin(body.type)
    if not has_capability(db, ctx.user, plugin.permission):
        raise AppError("FORBIDDEN", "Permission denied")
    cfg = _validate_config(plugin, body.config)
    _unique_name(db, body.type, body.name)
    _check_custom(db, ctx, cfg)
    _check_sources(db, ctx, plugin, body.source_resource_ids)
    _check_derived(db, ctx, plugin, cfg)
    _check_cron(body.schedule_cron, plugin, cfg)
    renderer = body.renderer or plugin.default_renderer
    if renderer not in RENDERERS:
        raise AppError("VALIDATION", "Unknown renderer")
    v = View(type=body.type, name=body.name, description=body.description, owner_id=ctx.user.id,
             config=cfg.model_dump(mode="json"), source_resource_ids=[str(i) for i in dict.fromkeys(body.source_resource_ids)],
             renderer=renderer, schedule_cron=body.schedule_cron, cache_ttl_s=body.cache_ttl_s,
             llm_allowed=body.llm_allowed)
    db.add(v)
    db.commit()
    record(db, "view.create", actor_id=ctx.user.id, target_type="view", target_id=v.id, ip=ctx.ip,
           detail={"type": v.type, "name": v.name, "sources": len(v.source_resource_ids)})
    return out(v, Level.manage)


@router.get("/views/{vid}")
def get_view(vid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    v = db.get(View, vid)
    if v is None:
        raise AppError("NOT_FOUND", "Not found")
    lv = require_level(db, ctx.user, v, ViewAcl, Level.view)
    return out(v, lv, lv >= Level.use)


@router.patch("/views/{vid}")
def patch_view(vid: uuid.UUID, body: ViewPatch, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    v = _get(db, ctx, vid, Level.edit)
    plugin, f = _plugin(v.type), body.model_dump(exclude_unset=True)
    if f.get("name"):
        _unique_name(db, v.type, f["name"], exclude=v.id)
    cfg = _validate_config(plugin, f["config"]) if "config" in f else plugin.config_schema(**v.config)
    if "config" in f:
        _check_custom(db, ctx, cfg)
        _check_derived(db, ctx, plugin, cfg)
        v.config = cfg.model_dump(mode="json")
    if "source_resource_ids" in f:
        _check_sources(db, ctx, plugin, f["source_resource_ids"])
        v.source_resource_ids = [str(i) for i in dict.fromkeys(f["source_resource_ids"])]
    cron = f["schedule_cron"] if "schedule_cron" in f else v.schedule_cron
    _check_cron(cron, plugin, cfg)
    v.schedule_cron = cron or None
    if f.get("renderer") is not None:
        if f["renderer"] not in RENDERERS:
            raise AppError("VALIDATION", "Unknown renderer")
        v.renderer = f["renderer"]
    for k in ("name", "description", "cache_ttl_s", "llm_allowed"):
        if f.get(k) is not None:
            setattr(v, k, f[k])
    db.commit()
    record(db, "view.update", actor_id=ctx.user.id, target_type="view", target_id=v.id, ip=ctx.ip,
           detail={"fields": sorted(f)})
    return out(v, Level.edit)


@router.delete("/views/{vid}", status_code=204)
def delete_view(vid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    v = _get(db, ctx, vid, Level.manage)
    used = list(db.scalars(select(Dashboard.name).join(DashboardWidget, DashboardWidget.dashboard_id == Dashboard.id)
                           .where(DashboardWidget.view_id == v.id).distinct()))
    if used:
        raise AppError("CONFLICT", "View is used by dashboards", {"dashboards": used})
    users = [d.name for d in db.scalars(select(View)) if d.id != v.id and engine.is_derived(REGISTRY[d.type])
             and v.id in REGISTRY[d.type].source_view_ids(REGISTRY[d.type].config_schema(**d.config))]
    if users:
        raise AppError("CONFLICT", "View is a source of other views", {"views": users})
    db.execute(delete(ViewAcl).where(ViewAcl.view_id == v.id))
    db.delete(v)
    db.commit()
    record(db, "view.delete", actor_id=ctx.user.id, target_type="view", target_id=vid, ip=ctx.ip,
           detail={"name": v.name})


@router.post("/views/{vid}/run")
def run(vid: uuid.UUID, body: RunIn = RunIn(), fresh: bool = Query(False), ctx: Ctx = Depends(current_ctx),
        db: Session = Depends(get_db)):
    v = db.get(View, vid)
    if v is None:
        raise AppError("NOT_FOUND", "Not found")
    try:
        result = engine.run_view(db, ctx.user, v, body.params, fresh=fresh, triggered_by="user")
    finally:
        record(db, "view.run", actor_id=ctx.user.id, target_type="view", target_id=vid, ip=ctx.ip,
               detail={"param_names": sorted(body.params)})  # names only: values may be sensitive
    return result


@router.get("/views/{vid}/result")
def result(vid: uuid.UUID, params: str | None = None, ctx: Ctx = Depends(current_ctx),
           db: Session = Depends(get_db)):
    v = db.get(View, vid)
    if v is None:
        raise AppError("NOT_FOUND", "Not found")
    try:
        p = json.loads(params) if params else {}
    except ValueError:
        raise AppError("VALIDATION", "params must be JSON") from None
    res = engine.latest_result(db, ctx.user, v, p)
    if res is None:
        raise AppError("NOT_FOUND", "No result yet")
    return res


@router.get("/views/{vid}/acl")
def get_acl(vid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    v = _get(db, ctx, vid, Level.manage)
    rows = db.scalars(select(ViewAcl).where(ViewAcl.view_id == v.id))
    return {"entries": [{"principal_type": a.principal_type, "principal_id": str(a.principal_id),
                         "level": a.level} for a in rows]}


@router.put("/views/{vid}/acl")
def put_acl(vid: uuid.UUID, body: AclIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    v = _get(db, ctx, vid, Level.manage)
    for e in body.entries:
        if db.get(User if e.principal_type == "user" else Group, e.principal_id) is None:
            raise AppError("VALIDATION", "Unknown principal in ACL")
    db.execute(delete(ViewAcl).where(ViewAcl.view_id == v.id))
    db.add_all(ViewAcl(view_id=v.id, principal_type=e.principal_type, principal_id=e.principal_id,
                       level=e.level) for e in body.entries)
    db.commit()
    record(db, "view.acl", actor_id=ctx.user.id, target_type="view", target_id=v.id, ip=ctx.ip,
           detail={"entries": len(body.entries)})
    return {"entries": [e.model_dump(mode="json") for e in body.entries]}
