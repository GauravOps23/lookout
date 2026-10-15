import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.authz.access import ACL_FK, levels_for, require_level
from app.authz.acl import Level
from app.authz.permissions import has_capability
from app.db import get_db
from app.deps import Ctx, current_ctx
from app.errors import AppError
from app.models import Group, Resource, ResourceAcl, User, View
from app.resources import service
from app.resources.schemas import TYPES

router = APIRouter(prefix="/api/resources", tags=["resources"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceCreate(Strict):
    type: str
    name: str = Field(min_length=1, max_length=128)
    tags: list[str] = []
    config: dict[str, Any]
    secret: dict[str, Any] | None = None
    llm_allowed: bool = False


class ResourcePatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=128)
    tags: list[str] | None = None
    config: dict[str, Any] | None = None
    secret: dict[str, Any] | None = None  # omit to keep the stored secret
    llm_allowed: bool | None = None


class AclItem(Strict):
    principal_type: str = Field(pattern="^(user|group)$")
    principal_id: uuid.UUID
    level: str = Field(pattern="^(view|use|edit|manage)$")


class AclIn(Strict):
    entries: list[AclItem]


class TransferIn(Strict):
    user_id: uuid.UUID


def _validate(rtype: str, config: dict, secret: dict | None):
    if rtype not in TYPES:
        raise AppError("VALIDATION", f"Unknown resource type: {rtype}")
    cfg_model, secret_model, _ = TYPES[rtype]
    try:
        cfg = cfg_model(**config).model_dump()
        sec = secret_model(**secret).model_dump(exclude_none=True) if secret is not None else None
    except (ValidationError, ValueError) as e:
        errs = e.errors() if isinstance(e, ValidationError) else [{"loc": [], "msg": str(e)}]
        raise AppError("VALIDATION", "Invalid resource configuration",
                       {"errors": [{"loc": [str(p) for p in x["loc"]], "msg": x["msg"]} for x in errs]}) from None
    return cfg, sec


def out(r: Resource, level: Level | None, can_see_config: bool = True) -> dict:
    return {
        "id": str(r.id), "type": r.type, "name": r.name, "tags": r.tags, "owner_id": str(r.owner_id),
        "status": r.status, "last_checked_at": r.last_checked_at, "last_error": r.last_error,
        "llm_allowed": r.llm_allowed, "level": level.name if level else None,
        "config": r.config if can_see_config else None,
        "secret": {"set": bool(r.secret_blob)},  # write-only: never the value
    }


def _unique_name(db: Session, rtype: str, name: str, exclude: uuid.UUID | None = None) -> None:
    """(type, name) is the natural key used by backup/restore and the legacy import."""
    dup = db.scalar(select(Resource).where(Resource.type == rtype, Resource.name == name))
    if dup is not None and dup.id != exclude:
        raise AppError("CONFLICT", "A resource of this type with that name already exists")


def _get(db: Session, ctx: Ctx, rid: uuid.UUID, required: Level) -> Resource:
    r = db.get(Resource, rid)
    if r is None:
        raise AppError("NOT_FOUND", "Not found")
    require_level(db, ctx.user, r, ResourceAcl, required)
    return r


@router.get("")
def list_resources(type: str | None = None, tag: str | None = None, q: str | None = None,
                   ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    stmt = select(Resource).order_by(Resource.name)
    if type:
        stmt = stmt.where(Resource.type == type)
    rows = [r for r in db.scalars(stmt)
            if (not tag or tag in r.tags) and (not q or q.lower() in r.name.lower())]
    levels = levels_for(db, ctx.user, rows, ResourceAcl)  # list never 403s: just omits what is hidden
    return [out(r, levels[r.id], can_see_config=levels[r.id] >= Level.use) for r in rows if r.id in levels]


@router.post("", status_code=201)
def create_resource(body: ResourceCreate, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    if body.type not in TYPES:
        raise AppError("VALIDATION", f"Unknown resource type: {body.type}")
    if not has_capability(db, ctx.user, TYPES[body.type][2]):
        raise AppError("FORBIDDEN", "Permission denied")
    cfg, sec = _validate(body.type, body.config, body.secret)
    _unique_name(db, body.type, body.name)
    r = Resource(id=uuid.uuid4(), type=body.type, name=body.name, tags=body.tags, owner_id=ctx.user.id,
                 config=cfg, llm_allowed=body.llm_allowed)
    service.seal_secret(r, sec)
    db.add(r)
    db.commit()
    record(db, "resource.create", actor_id=ctx.user.id, target_type="resource", target_id=r.id, ip=ctx.ip,
           detail={"type": r.type, "name": r.name})
    return out(r, Level.manage)


@router.get("/{rid}")
def get_resource(rid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    r = db.get(Resource, rid)
    if r is None:
        raise AppError("NOT_FOUND", "Not found")
    lv = require_level(db, ctx.user, r, ResourceAcl, Level.view)
    return out(r, lv, can_see_config=lv >= Level.use)


@router.patch("/{rid}")
def patch_resource(rid: uuid.UUID, body: ResourcePatch, ctx: Ctx = Depends(current_ctx),
                   db: Session = Depends(get_db)):
    r = _get(db, ctx, rid, Level.edit)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("name"):
        _unique_name(db, r.type, fields["name"], exclude=r.id)
    for k in ("name", "tags", "llm_allowed"):
        if k in fields:
            setattr(r, k, fields[k])
    if "config" in fields or "secret" in fields:
        cfg, sec = _validate(r.type, fields.get("config", r.config), fields.get("secret"))
        old = r.config
        r.config = cfg
        # A changed target invalidates the pinned host key; keep it only if host/port are unchanged.
        if r.type == "ssh" and (old.get("host"), old.get("port")) != (cfg["host"], cfg["port"]):
            r.config = {**cfg, "host_key_fingerprint": None}
        if "secret" in fields:
            service.seal_secret(r, sec)
    db.commit()
    record(db, "resource.update", actor_id=ctx.user.id, target_type="resource", target_id=r.id, ip=ctx.ip,
           detail={"fields": sorted(fields)})  # field names only, never values
    return out(r, Level.edit)


@router.delete("/{rid}", status_code=204)
def delete_resource(rid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    r = _get(db, ctx, rid, Level.manage)
    deps = [v for v in db.scalars(select(View)) if str(r.id) in map(str, v.source_resource_ids)]
    if deps:
        raise AppError("CONFLICT", "Resource is used by views",
                       {"views": [{"id": str(v.id), "name": v.name} for v in deps]})
    db.execute(delete(ResourceAcl).where(ResourceAcl.resource_id == r.id))
    db.delete(r)
    db.commit()
    record(db, "resource.delete", actor_id=ctx.user.id, target_type="resource", target_id=rid, ip=ctx.ip,
           detail={"name": r.name})


@router.post("/{rid}/test")
def test_resource(rid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    r = _get(db, ctx, rid, Level.use)
    ok, err = service.test_resource(db, r)
    record(db, "resource.test", actor_id=ctx.user.id, target_type="resource", target_id=r.id, ip=ctx.ip,
           detail={"ok": ok})
    return {"ok": ok, "status": r.status, "error": err}


@router.get("/{rid}/acl")
def get_acl(rid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    r = _get(db, ctx, rid, Level.manage)
    rows = db.scalars(select(ResourceAcl).where(ResourceAcl.resource_id == r.id))
    return {"entries": [{"principal_type": a.principal_type, "principal_id": str(a.principal_id),
                         "level": a.level} for a in rows]}


@router.put("/{rid}/acl")
def put_acl(rid: uuid.UUID, body: AclIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    r = _get(db, ctx, rid, Level.manage)
    for e in body.entries:
        model = User if e.principal_type == "user" else Group
        if db.get(model, e.principal_id) is None:
            raise AppError("VALIDATION", "Unknown principal in ACL")
    db.execute(delete(ResourceAcl).where(ResourceAcl.resource_id == r.id))
    db.add_all(ResourceAcl(resource_id=r.id, principal_type=e.principal_type, principal_id=e.principal_id,
                           level=e.level) for e in body.entries)
    db.commit()
    record(db, "resource.acl", actor_id=ctx.user.id, target_type="resource", target_id=r.id, ip=ctx.ip,
           detail={"entries": len(body.entries)})
    return {"entries": [e.model_dump(mode="json") for e in body.entries]}


@router.post("/{rid}/transfer")
def transfer(rid: uuid.UUID, body: TransferIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    r = _get(db, ctx, rid, Level.manage)
    if db.get(User, body.user_id) is None:
        raise AppError("VALIDATION", "Unknown user")
    r.owner_id = body.user_id
    db.commit()
    record(db, "resource.transfer", actor_id=ctx.user.id, target_type="resource", target_id=r.id, ip=ctx.ip,
           detail={"to": str(body.user_id)})
    return {"owner_id": str(r.owner_id)}
