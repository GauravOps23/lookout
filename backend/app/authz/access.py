"""Object-level access against the ACL tables (resources, views). Wraps the pure logic in acl.py."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz.acl import AclEntry, Level, can, effective_level
from app.errors import AppError
from app.models import DashboardAcl, ResourceAcl, User, UserGroup, ViewAcl

ACL_FK = {ResourceAcl: "resource_id", ViewAcl: "view_id", DashboardAcl: "dashboard_id"}


def group_ids(db: Session, user: User) -> set:
    return set(db.scalars(select(UserGroup.group_id).where(UserGroup.user_id == user.id)))


def levels_for(db: Session, user: User, objs: list, acl_model) -> dict:
    """{obj.id: Level} for objects the user can see at all (owner, admin or ACL). Others are absent."""
    if not objs:
        return {}
    fk = getattr(acl_model, ACL_FK[acl_model])
    rows = db.scalars(select(acl_model).where(fk.in_([o.id for o in objs]))).all()
    by_obj: dict = {}
    for r in rows:
        by_obj.setdefault(getattr(r, ACL_FK[acl_model]), []).append(
            AclEntry(r.principal_type, r.principal_id, Level[r.level]))
    gids = group_ids(db, user)
    out = {}
    for o in objs:
        lv = effective_level(owner_id=o.owner_id, user_id=user.id, group_ids=gids,
                             entries=by_obj.get(o.id, []), is_admin=user.is_admin)
        if lv is not None:
            out[o.id] = lv
    return out


def level_on(db: Session, user: User, obj, acl_model) -> Level | None:
    return levels_for(db, user, [obj], acl_model).get(obj.id)


def require_level(db: Session, user: User, obj, acl_model, required: Level) -> Level:
    """Hidden objects look like they do not exist; visible-but-insufficient is FORBIDDEN."""
    held = level_on(db, user, obj, acl_model)
    if held is None:
        raise AppError("NOT_FOUND", "Not found")
    if not can(held, required):
        raise AppError("FORBIDDEN", f"Requires '{required.name}' access")
    return held
