import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.authz.permissions import has_capability
from app.db import get_db
from app.deps import Ctx, RateLimiter, current_ctx
from app.errors import AppError
from app.models import LinkTile, LinkUserCredential, User
from app.resources import service

router = APIRouter(prefix="/api/links", tags=["links"])
_reveal_limiter = RateLimiter(10)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credential(Strict):
    username: str = Field(max_length=256)
    password: str = Field(max_length=1024)


def _http_url(v: str) -> str:
    u = TypeAdapter(HttpUrl).validate_python(v)  # raises on junk; only http(s) pass HttpUrl
    if u.username or u.password:
        raise ValueError("URL must not contain credentials")
    return v


class TileCreate(Strict):
    name: str = Field(min_length=1, max_length=128)
    url: str
    tags: list[str] = []
    credential_mode: Literal["none", "shared", "per_user"] = "none"
    credential: Credential | None = None  # only for shared mode

    _u = field_validator("url")(_http_url)


class TilePatch(Strict):
    name: str | None = Field(None, min_length=1, max_length=128)
    url: str | None = None
    tags: list[str] | None = None
    credential_mode: Literal["none", "shared", "per_user"] | None = None
    credential: Credential | None = None

    @field_validator("url")
    @classmethod
    def _u(cls, v):
        return _http_url(v) if v is not None else v


def _can_open(db: Session, user: User, t: LinkTile) -> bool:
    return user.is_admin or t.owner_id == user.id or has_capability(db, user, "panel.access", t.id)


def _can_manage(user: User, t: LinkTile) -> bool:
    return user.is_admin or t.owner_id == user.id


def _tile(db: Session, ctx: Ctx, tid: uuid.UUID, manage: bool = False) -> LinkTile:
    t = db.get(LinkTile, tid)
    if t is None or not _can_open(db, ctx.user, t):
        raise AppError("NOT_FOUND", "Not found")
    if manage and not _can_manage(ctx.user, t):
        raise AppError("FORBIDDEN", "Only the owner or an admin can change this tile")
    return t


def _my_cred(db: Session, t: LinkTile, user: User) -> LinkUserCredential | None:
    return db.scalar(select(LinkUserCredential).where(LinkUserCredential.tile_id == t.id,
                                                      LinkUserCredential.user_id == user.id))


def _secret(db: Session, t: LinkTile, user: User) -> dict | None:
    if t.credential_mode == "shared" and t.shared_secret_blob:
        return json.loads(service.vault().open(t.shared_secret_blob, t.id))
    if t.credential_mode == "per_user":
        c = _my_cred(db, t, user)
        return json.loads(service.vault().open(c.secret_blob, c.id)) if c else None
    return None


def _out(db: Session, ctx: Ctx, t: LinkTile) -> dict:
    has = (bool(t.shared_secret_blob) if t.credential_mode == "shared"
           else _my_cred(db, t, ctx.user) is not None if t.credential_mode == "per_user" else False)
    return {"id": str(t.id), "name": t.name, "url": t.url, "tags": t.tags, "credential_mode": t.credential_mode,
            "has_credential": has, "can_manage": _can_manage(ctx.user, t)}  # never the secret itself


def _unique(db: Session, name: str, exclude: uuid.UUID | None = None) -> None:
    dup = db.scalar(select(LinkTile).where(LinkTile.name == name))
    if dup is not None and dup.id != exclude:
        raise AppError("CONFLICT", "A link tile with that name already exists")


def _set_shared(t: LinkTile, cred: Credential | None) -> None:
    if t.credential_mode != "shared":
        t.shared_secret_blob = None
    elif cred is not None:
        t.shared_secret_blob = service.vault().seal(json.dumps(cred.model_dump()).encode(), t.id)
    if t.credential_mode == "shared" and t.shared_secret_blob is None:
        raise AppError("VALIDATION", "A shared tile needs a credential")


@router.get("")
def list_tiles(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    return [_out(db, ctx, t) for t in db.scalars(select(LinkTile).order_by(LinkTile.name))
            if _can_open(db, ctx.user, t)]


@router.post("", status_code=201)
def create_tile(body: TileCreate, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    if not has_capability(db, ctx.user, "link.create"):
        raise AppError("FORBIDDEN", "Permission denied")
    _unique(db, body.name)
    t = LinkTile(id=uuid.uuid4(), name=body.name, url=body.url, tags=body.tags,
                 credential_mode=body.credential_mode, owner_id=ctx.user.id)
    _set_shared(t, body.credential)
    db.add(t)
    db.commit()
    record(db, "link.create", actor_id=ctx.user.id, target_type="link", target_id=t.id, ip=ctx.ip,
           detail={"name": t.name, "mode": t.credential_mode})
    return _out(db, ctx, t)


@router.patch("/{tid}")
def patch_tile(tid: uuid.UUID, body: TilePatch, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    t = _tile(db, ctx, tid, manage=True)
    f = body.model_dump(exclude_unset=True)
    if f.get("name"):
        _unique(db, f["name"], exclude=t.id)
    for k in ("name", "url", "tags", "credential_mode"):
        if f.get(k) is not None:
            setattr(t, k, f[k])
    _set_shared(t, body.credential)
    db.commit()
    record(db, "link.update", actor_id=ctx.user.id, target_type="link", target_id=t.id, ip=ctx.ip,
           detail={"fields": sorted(f)})
    return _out(db, ctx, t)


@router.delete("/{tid}", status_code=204)
def delete_tile(tid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    t = _tile(db, ctx, tid, manage=True)
    db.query(LinkUserCredential).filter_by(tile_id=t.id).delete()
    db.delete(t)
    db.commit()
    record(db, "link.delete", actor_id=ctx.user.id, target_type="link", target_id=tid, ip=ctx.ip,
           detail={"name": t.name})


@router.post("/{tid}/launch")
def launch(tid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """The UI opens `url` in a new tab (noopener). The hint only says whether a credential exists."""
    t = _tile(db, ctx, tid)
    hint = {"mode": t.credential_mode, "has_credential": _out(db, ctx, t)["has_credential"]}
    if t.credential_mode == "per_user" and not hint["has_credential"]:
        hint["needs_credential"] = True
    return {"url": t.url, "credential_hint": hint}


@router.post("/{tid}/reveal")
def reveal(tid: uuid.UUID, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    """Return the credential for the copy buttons. Audited and rate limited."""
    _reveal_limiter.check(str(ctx.user.id))
    t = _tile(db, ctx, tid)
    s = _secret(db, t, ctx.user)
    if s is None:
        raise AppError("NOT_FOUND", "No credential")
    record(db, "link.reveal", actor_id=ctx.user.id, target_type="link", target_id=t.id, ip=ctx.ip)
    return s


@router.put("/{tid}/my-credential", status_code=204)
def my_credential(tid: uuid.UUID, body: Credential, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    t = _tile(db, ctx, tid)
    if t.credential_mode != "per_user":
        raise AppError("VALIDATION", "This tile does not use per-user credentials")
    c = _my_cred(db, t, ctx.user)
    if c is None:
        c = LinkUserCredential(id=uuid.uuid4(), tile_id=t.id, user_id=ctx.user.id, secret_blob=b"")
        db.add(c)
    c.secret_blob = service.vault().seal(json.dumps(body.model_dump()).encode(), c.id)
    db.commit()
    record(db, "link.credential_set", actor_id=ctx.user.id, target_type="link", target_id=t.id, ip=ctx.ip)
