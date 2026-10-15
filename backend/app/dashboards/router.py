import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.authz.access import levels_for, require_level
from app.authz.acl import Level
from app.authz.permissions import has_capability
from app.db import as_utc, get_db, now
from app.deps import Ctx, RateLimiter, client_ip, current_ctx
from app.errors import AppError
from app.models import Dashboard, DashboardAcl, DashboardWidget, Group, User, View, ViewAcl
from app.resources.router import AclIn
from app.views import engine
from app.views.base import bind_params
from app.views.router import RENDERERS

router = APIRouter(prefix="/api", tags=["dashboards"])
_shared_limiter = RateLimiter(60)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Grid(Strict):
    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=10000)
    w: int = Field(ge=1, le=12)
    h: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _fits(self):
        if self.x + self.w > 12:
            raise ValueError("widget exceeds the 12-column grid")
        return self


class WidgetIn(Strict):
    view_id: uuid.UUID
    title: str = Field("", max_length=128)
    renderer: str = "table"
    grid: Grid
    options: dict[str, Any] = {}
    params: dict[str, Any] = {}
    refresh_s: int = Field(0, ge=0, le=86400)


class DashboardCreate(Strict):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    widgets: list[WidgetIn] = Field([], max_length=50)


class DashboardPatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None
    widgets: list[WidgetIn] | None = Field(None, max_length=50)  # replaces all widgets


class ShareIn(Strict):
    expires_in_hours: int = Field(168, ge=1, le=720)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _widgets(db: Session, d: Dashboard) -> list[DashboardWidget]:
    return list(db.scalars(select(DashboardWidget).where(DashboardWidget.dashboard_id == d.id)
                           .order_by(DashboardWidget.created_at)))


def _out(db: Session, ctx: Ctx, d: Dashboard, level: Level) -> dict:
    ws = _widgets(db, d)
    views = [v for v in (db.get(View, w.view_id) for w in ws) if v]
    seen = levels_for(db, ctx.user, views, ViewAcl)
    out = []
    for w in ws:
        base = {"id": str(w.id), "title": w.title, "renderer": w.renderer, "grid": w.grid,
                "options": w.options, "refresh_s": w.refresh_s}
        if w.view_id in seen:
            out.append({**base, "view_id": str(w.view_id), "params": w.params, "no_access": False})
        else:  # placeholder: nothing about the hidden view leaves the server
            out.append({**base, "title": "", "view_id": None, "params": {}, "no_access": True})
    return {"id": str(d.id), "name": d.name, "description": d.description, "owner_id": str(d.owner_id),
            "level": level.name, "shared": d.share_token_hash is not None, "widgets": out}


def _unique(db: Session, name: str, exclude: uuid.UUID | None = None) -> None:
    dup = db.scalar(select(Dashboard).where(Dashboard.name == name))
    if dup is not None and dup.id != exclude:
        raise AppError("CONFLICT", "A dashboard with that name already exists")


def _check_widgets(db: Session, ctx: Ctx, items: list[WidgetIn]) -> list[DashboardWidget]:
    out = []
    for w in items:
        v = db.get(View, w.view_id)
        if v is None:
            raise AppError("VALIDATION", "Unknown view in widget")
        require_level(db, ctx.user, v, ViewAcl, Level.view)
        if w.renderer not in RENDERERS:
            raise AppError("VALIDATION", "Unknown renderer")
        plugin, cfg = engine.load_plugin(v)
        bind_params(plugin.variables(cfg), w.params)  # a widget that could never run is rejected now
        out.append(DashboardWidget(view_id=w.view_id, title=w.title, renderer=w.renderer, grid=w.grid.model_dump(),
                                   options=w.options, params=w.params, refresh_s=w.refresh_s))
    return out


def _get(db: Session, ctx: Ctx, did: uuid.UUID, required: Level) -> tuple[Dashboard, Level]:
    d = db.get(Dashboard, did)
    if d is None:
        raise AppError("NOT_FOUND", "Not found")
    return d, require_level(db, ctx.user, d, DashboardAcl, required)


@router.get("/dashboards")
def list_dashboards(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Dashboard).order_by(Dashboard.name)))
    lv = levels_for(db, ctx.user, rows, DashboardAcl)
    return [{"id": str(d.id), "name": d.name, "description": d.description, "level": lv[d.id].name}
            for d in rows if d.id in lv]


@router.post("/dashboards", status_code=201)
def create_dashboard(body: DashboardCreate, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    if not has_capability(db, ctx.user, "dashboard.create"):
        raise AppError("FORBIDDEN", "Permission denied")
    _unique(db, body.name)
    widgets = _check_widgets(db, ctx, body.widgets)
    d = Dashboard(name=body.name, description=body.description, owner_id=ctx.user.id)
    db.add(d)
    db.flush()
    for w in widgets:
        w.dashboard_id = d.id
    db.add_all(widgets)
    db.commit()
    record(db, "dashboard.create", actor_id=ctx.user.id, target_type="dashboard", target_id=d.id, ip=ctx.ip,
           detail={"name": d.name, "widgets": len(widgets)})
    return _out(db, ctx, d, Level.manage)


@router.get("/dashboards/{did}")
def get_dashboard(did: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    d, lv = _get(db, ctx, did, Level.view)
    return _out(db, ctx, d, lv)


@router.patch("/dashboards/{did}")
def patch_dashboard(did: uuid.UUID, body: DashboardPatch, ctx: Ctx = Depends(current_ctx),
                    db: Session = Depends(get_db)):
    d, lv = _get(db, ctx, did, Level.edit)
    f = body.model_dump(exclude_unset=True)
    if f.get("name"):
        _unique(db, f["name"], exclude=d.id)
        d.name = f["name"]
    if f.get("description") is not None:
        d.description = f["description"]
    if body.widgets is not None:
        new = _check_widgets(db, ctx, body.widgets)
        db.execute(delete(DashboardWidget).where(DashboardWidget.dashboard_id == d.id))
        for w in new:
            w.dashboard_id = d.id
        db.add_all(new)
    db.commit()
    record(db, "dashboard.update", actor_id=ctx.user.id, target_type="dashboard", target_id=d.id, ip=ctx.ip,
           detail={"fields": sorted(f)})
    return _out(db, ctx, d, lv)


@router.delete("/dashboards/{did}", status_code=204)
def delete_dashboard(did: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    d, _ = _get(db, ctx, did, Level.manage)
    db.execute(delete(DashboardWidget).where(DashboardWidget.dashboard_id == d.id))
    db.execute(delete(DashboardAcl).where(DashboardAcl.dashboard_id == d.id))
    db.delete(d)
    db.commit()
    record(db, "dashboard.delete", actor_id=ctx.user.id, target_type="dashboard", target_id=did, ip=ctx.ip,
           detail={"name": d.name})


@router.get("/dashboards/{did}/acl")
def get_acl(did: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    d, _ = _get(db, ctx, did, Level.manage)
    rows = db.scalars(select(DashboardAcl).where(DashboardAcl.dashboard_id == d.id))
    return {"entries": [{"principal_type": a.principal_type, "principal_id": str(a.principal_id),
                         "level": a.level} for a in rows]}


@router.put("/dashboards/{did}/acl")
def put_acl(did: uuid.UUID, body: AclIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    d, _ = _get(db, ctx, did, Level.manage)
    for e in body.entries:
        if db.get(User if e.principal_type == "user" else Group, e.principal_id) is None:
            raise AppError("VALIDATION", "Unknown principal in ACL")
    db.execute(delete(DashboardAcl).where(DashboardAcl.dashboard_id == d.id))
    db.add_all(DashboardAcl(dashboard_id=d.id, principal_type=e.principal_type, principal_id=e.principal_id,
                            level=e.level) for e in body.entries)
    db.commit()
    record(db, "dashboard.acl", actor_id=ctx.user.id, target_type="dashboard", target_id=d.id, ip=ctx.ip,
           detail={"entries": len(body.entries)})
    return {"entries": [e.model_dump(mode="json") for e in body.entries]}


@router.get("/dashboards/{did}/widgets/{wid}/data")
def widget_data(did: uuid.UUID, wid: uuid.UUID, run: bool = Query(False), ctx: Ctx = Depends(current_ctx),
                db: Session = Depends(get_db)):
    """Cached result of the widget's view; `run=1` runs it live if the caller has 'use' on the view."""
    d, _ = _get(db, ctx, did, Level.view)
    w = db.get(DashboardWidget, wid)
    if w is None or w.dashboard_id != d.id:
        raise AppError("NOT_FOUND", "Not found")
    v = db.get(View, w.view_id)
    res = (engine.run_view(db, ctx.user, v, w.params, triggered_by="user") if run
           else engine.latest_result(db, ctx.user, v, w.params))
    if res is None:
        raise AppError("NOT_FOUND", "No result yet")
    return res


# ---- share links: read-only, cached results only, never a live connector call -------------------------------
@router.post("/dashboards/{did}/share-link")
def share(did: uuid.UUID, body: ShareIn = ShareIn(), ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    d, _ = _get(db, ctx, did, Level.manage)
    token = secrets.token_urlsafe(32)
    d.share_token_hash, d.share_expires_at = _hash(token), now() + timedelta(hours=body.expires_in_hours)
    db.commit()
    record(db, "dashboard.share", actor_id=ctx.user.id, target_type="dashboard", target_id=d.id, ip=ctx.ip,
           detail={"expires_in_hours": body.expires_in_hours})
    return {"token": token, "expires_at": d.share_expires_at, "path": f"/#/shared/{token}"}  # shown once


@router.delete("/dashboards/{did}/share-link", status_code=204)
def unshare(did: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    d, _ = _get(db, ctx, did, Level.manage)
    d.share_token_hash = d.share_expires_at = None
    db.commit()
    record(db, "dashboard.unshare", actor_id=ctx.user.id, target_type="dashboard", target_id=d.id, ip=ctx.ip)


@router.get("/shared/{token}")
def shared(token: str, request: Request, db: Session = Depends(get_db)):
    """No login. The token is the credential; unknown, expired and revoked tokens all look the same."""
    _shared_limiter.check(client_ip(request))
    d = db.scalar(select(Dashboard).where(Dashboard.share_token_hash == _hash(token)))
    if d is None or d.share_expires_at is None or as_utc(d.share_expires_at) <= now():
        raise AppError("NOT_FOUND", "Not found")
    widgets = []
    for w in _widgets(db, d):
        v = db.get(View, w.view_id)
        try:
            res = engine.cached_unchecked(db, v, w.params) if v else None
        except AppError:
            res = None
        widgets.append({"title": w.title, "renderer": w.renderer, "grid": w.grid, "options": w.options,
                        "result": res})
    return {"name": d.name, "description": d.description, "widgets": widgets}
