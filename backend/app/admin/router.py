import csv
import io
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.auth import passwords
from app.auth.sessions import destroy_user_sessions
from app.authz.permissions import CATALOG, SCOPED
from app.db import get_db
from app.deps import Ctx, require
from app.errors import AppError
from app.models import AuditLog, Group, GroupPermission, Setting, User, UserGroup, UserPermission

router = APIRouter(prefix="/api/admin", tags=["admin"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserCreate(Strict):
    login: str = Field(min_length=2, max_length=128, pattern=r"^[A-Za-z0-9._@-]+$")
    display_name: str = ""
    is_admin: bool = False


class UserPatch(Strict):
    display_name: str | None = None
    is_active: bool | None = None
    is_admin: bool | None = None


class GroupIn(Strict):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


class MembersIn(Strict):
    user_ids: list[uuid.UUID]


class PermItem(Strict):
    permission: str
    scope_id: uuid.UUID | None = None


class PermsIn(Strict):
    permissions: list[PermItem]


def _user(u: User) -> dict:
    return {"id": str(u.id), "login": u.login, "display_name": u.display_name, "is_admin": u.is_admin,
            "is_active": u.is_active, "must_change_pw": u.must_change_pw,
            "locked": bool(u.locked_until), "last_login": u.last_login,
            "groups": [str(g.id) for g in u.groups]}


def _get(db: Session, model, id_: uuid.UUID):
    obj = db.get(model, id_)
    if obj is None:
        raise AppError("NOT_FOUND", f"{model.__name__} not found")
    return obj


def _check_perms(items: list[PermItem]) -> None:
    for p in items:
        if p.permission not in CATALOG:
            raise AppError("VALIDATION", f"Unknown permission: {p.permission}")
        if p.scope_id is not None and p.permission not in SCOPED:
            raise AppError("VALIDATION", f"{p.permission} does not take a scope")


# ---- users ----------------------------------------------------------------
@router.get("/users")
def list_users(ctx: Ctx = Depends(require("admin.users")), db: Session = Depends(get_db)):
    return [_user(u) for u in db.scalars(select(User).order_by(User.login))]


@router.post("/users", status_code=201)
def create_user(body: UserCreate, ctx: Ctx = Depends(require("admin.users")), db: Session = Depends(get_db)):
    login = body.login.lower()
    if db.scalar(select(User).where(User.login == login)):
        raise AppError("CONFLICT", "Login already exists")
    temp = passwords.temp_password()
    u = User(login=login, display_name=body.display_name, is_admin=body.is_admin,
             password_hash=passwords.hash_password(temp), must_change_pw=True)
    db.add(u)
    db.commit()
    record(db, "user.create", actor_id=ctx.user.id, target_type="user", target_id=u.id, ip=ctx.ip,
           detail={"login": login, "is_admin": body.is_admin})
    return {**_user(u), "temp_password": temp}  # shown once


@router.patch("/users/{user_id}")
def patch_user(user_id: uuid.UUID, body: UserPatch, ctx: Ctx = Depends(require("admin.users")),
               db: Session = Depends(get_db)):
    u = _get(db, User, user_id)
    changes = body.model_dump(exclude_unset=True)
    if u.id == ctx.user.id and (changes.get("is_active") is False or changes.get("is_admin") is False):
        raise AppError("CONFLICT", "You cannot deactivate or demote yourself")
    for k, v in changes.items():
        setattr(u, k, v)
    db.commit()
    if changes.get("is_active") is False:
        destroy_user_sessions(db, u.id)
    record(db, "user.update", actor_id=ctx.user.id, target_type="user", target_id=u.id, ip=ctx.ip,
           detail=changes)
    return _user(u)


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: uuid.UUID, ctx: Ctx = Depends(require("admin.users")),
                   db: Session = Depends(get_db)):
    u = _get(db, User, user_id)
    temp = passwords.temp_password()
    u.password_hash, u.must_change_pw, u.failed_attempts, u.locked_until = (
        passwords.hash_password(temp), True, 0, None)
    db.commit()
    destroy_user_sessions(db, u.id)
    record(db, "user.reset_password", actor_id=ctx.user.id, target_type="user", target_id=u.id, ip=ctx.ip)
    return {"temp_password": temp}


@router.get("/users/{user_id}/permissions")
def get_user_permissions(user_id: uuid.UUID, ctx: Ctx = Depends(require("admin.users")),
                         db: Session = Depends(get_db)):
    _get(db, User, user_id)
    rows = db.scalars(select(UserPermission).where(UserPermission.user_id == user_id))
    return {"permissions": [{"permission": r.permission, "scope_id": str(r.scope_id) if r.scope_id else None}
                            for r in rows]}


@router.put("/users/{user_id}/permissions")
def set_user_permissions(user_id: uuid.UUID, body: PermsIn, ctx: Ctx = Depends(require("admin.users")),
                         db: Session = Depends(get_db)):
    _get(db, User, user_id)
    _check_perms(body.permissions)
    db.execute(delete(UserPermission).where(UserPermission.user_id == user_id))
    db.add_all(UserPermission(user_id=user_id, permission=p.permission, scope_id=p.scope_id)
               for p in body.permissions)
    db.commit()
    record(db, "permission.change", actor_id=ctx.user.id, target_type="user", target_id=user_id, ip=ctx.ip,
           detail={"permissions": [p.permission for p in body.permissions]})
    return {"permissions": [p.model_dump(mode="json") for p in body.permissions]}


# ---- groups ---------------------------------------------------------------
@router.get("/groups")
def list_groups(ctx: Ctx = Depends(require("admin.groups")), db: Session = Depends(get_db)):
    return [{"id": str(g.id), "name": g.name, "description": g.description,
             "members": [str(u.id) for u in g.users]} for g in db.scalars(select(Group).order_by(Group.name))]


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, ctx: Ctx = Depends(require("admin.groups")), db: Session = Depends(get_db)):
    if db.scalar(select(Group).where(Group.name == body.name)):
        raise AppError("CONFLICT", "Group name already exists")
    g = Group(name=body.name, description=body.description)
    db.add(g)
    db.commit()
    record(db, "group.create", actor_id=ctx.user.id, target_type="group", target_id=g.id, ip=ctx.ip,
           detail={"name": g.name})
    return {"id": str(g.id), "name": g.name, "description": g.description, "members": []}


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(group_id: uuid.UUID, ctx: Ctx = Depends(require("admin.groups")),
                 db: Session = Depends(get_db)):
    g = _get(db, Group, group_id)
    db.execute(delete(UserGroup).where(UserGroup.group_id == g.id))
    db.execute(delete(GroupPermission).where(GroupPermission.group_id == g.id))
    db.delete(g)
    db.commit()
    record(db, "group.delete", actor_id=ctx.user.id, target_type="group", target_id=group_id, ip=ctx.ip)


@router.put("/groups/{group_id}/members")
def set_members(group_id: uuid.UUID, body: MembersIn, ctx: Ctx = Depends(require("admin.groups")),
                db: Session = Depends(get_db)):
    g = _get(db, Group, group_id)
    users = list(db.scalars(select(User).where(User.id.in_(set(body.user_ids)))))
    if len(users) != len(set(body.user_ids)):
        raise AppError("VALIDATION", "Unknown user id in members")
    g.users = users
    db.commit()
    record(db, "group.members", actor_id=ctx.user.id, target_type="group", target_id=g.id, ip=ctx.ip,
           detail={"count": len(users)})
    return {"members": [str(u.id) for u in users]}


@router.get("/groups/{group_id}/permissions")
def get_group_permissions(group_id: uuid.UUID, ctx: Ctx = Depends(require("admin.groups")),
                          db: Session = Depends(get_db)):
    _get(db, Group, group_id)
    rows = db.scalars(select(GroupPermission).where(GroupPermission.group_id == group_id))
    return {"permissions": [{"permission": r.permission, "scope_id": str(r.scope_id) if r.scope_id else None}
                            for r in rows]}


@router.put("/groups/{group_id}/permissions")
def set_group_permissions(group_id: uuid.UUID, body: PermsIn, ctx: Ctx = Depends(require("admin.groups")),
                          db: Session = Depends(get_db)):
    _get(db, Group, group_id)
    _check_perms(body.permissions)
    db.execute(delete(GroupPermission).where(GroupPermission.group_id == group_id))
    db.add_all(GroupPermission(group_id=group_id, permission=p.permission, scope_id=p.scope_id)
               for p in body.permissions)
    db.commit()
    record(db, "permission.change", actor_id=ctx.user.id, target_type="group", target_id=group_id, ip=ctx.ip,
           detail={"permissions": [p.permission for p in body.permissions]})
    return {"permissions": [p.model_dump(mode="json") for p in body.permissions]}


# ---- audit ----------------------------------------------------------------
def _audit_query(actor_id, action, since, until):
    q = select(AuditLog).order_by(AuditLog.at.desc())
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    if action:
        q = q.where(AuditLog.action == action)
    if since:
        q = q.where(AuditLog.at >= since)
    if until:
        q = q.where(AuditLog.at <= until)
    return q


def _audit_row(a: AuditLog) -> dict:
    return {"id": str(a.id), "at": a.at.isoformat(), "actor_id": str(a.actor_id) if a.actor_id else None,
            "action": a.action, "target_type": a.target_type, "target_id": a.target_id, "ip": a.ip,
            "detail": a.detail}


@router.get("/audit")
def list_audit(actor_id: uuid.UUID | None = None, action: str | None = None, since: datetime | None = None,
               until: datetime | None = None, limit: int = Query(100, ge=1, le=1000), offset: int = 0,
               ctx: Ctx = Depends(require("admin.audit")), db: Session = Depends(get_db)):
    rows = db.scalars(_audit_query(actor_id, action, since, until).limit(limit).offset(offset))
    return [_audit_row(a) for a in rows]


@router.get("/audit/export.csv")
def export_audit(actor_id: uuid.UUID | None = None, action: str | None = None, since: datetime | None = None,
                 until: datetime | None = None, ctx: Ctx = Depends(require("admin.audit")),
                 db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "actor_id", "action", "target_type", "target_id", "ip", "detail"])
    for a in db.scalars(_audit_query(actor_id, action, since, until).limit(100000)):
        r = _audit_row(a)
        # Prefix formula-looking cells so spreadsheets do not execute them.
        cells = [r["at"], r["actor_id"] or "", r["action"], r["target_type"], r["target_id"], r["ip"], str(r["detail"])]
        w.writerow(["'" + c if c[:1] in "=+-@" and c else c for c in cells])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=audit.csv"})


# ---- settings -------------------------------------------------------------------------------
class AllowlistIn(Strict):
    entries: list[str] = Field(max_length=200)


@router.get("/settings/ssrf-allowlist")
def get_ssrf_allowlist(ctx: Ctx = Depends(require("admin.settings")), db: Session = Depends(get_db)):
    from app.resources.service import ssrf_allowlist
    return {"entries": ssrf_allowlist(db)}


@router.put("/settings/ssrf-allowlist")
def put_ssrf_allowlist(body: AllowlistIn, ctx: Ctx = Depends(require("admin.settings")),
                       db: Session = Depends(get_db)):
    """Hostnames/CIDRs that API resources may reach even though they are loopback/link-local (spec 6.2)."""
    import ipaddress
    for e in body.entries:
        try:
            ipaddress.ip_network(e, strict=False)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", e):
                raise AppError("VALIDATION", f"Not a hostname or CIDR: {e[:40]}") from None
    row = db.scalar(select(Setting).where(Setting.key == "api.ssrf_allowlist"))
    if row is None:
        db.add(Setting(key="api.ssrf_allowlist", value={"entries": body.entries}))
    else:
        row.value = {"entries": body.entries}
    db.commit()
    record(db, "settings.update", actor_id=ctx.user.id, target_type="setting", target_id="api.ssrf_allowlist",
           ip=ctx.ip, detail={"entries": body.entries})
    return {"entries": body.entries}
