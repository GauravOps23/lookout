import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupPermission, User, UserGroup, UserPermission

# Spec section 4.1. `panel.access` is scoped to a link tile or dashboard id.
CATALOG: frozenset[str] = frozenset({
    "admin.users", "admin.groups", "admin.settings", "admin.backup", "admin.llm", "admin.audit",
    "inventory.ssh.create", "inventory.oracle.create", "inventory.api.create",
    "view.ssh_health.create", "view.ssh.custom", "view.db_table.create",
    "view.api_response.create", "view.trend.create",
    "dashboard.create", "link.create", "panel.access", "llm.use",
})
SCOPED = frozenset({"panel.access"})


def has_capability(db: Session, user: User, perm: str, scope_id: uuid.UUID | None = None) -> bool:
    """Capability permission check: admin, else direct or via any group. Default deny."""
    if user.is_admin:
        return True
    if perm not in CATALOG:
        return False

    def matches(model_scope) -> bool:
        return model_scope is None or scope_id is not None and model_scope == scope_id

    direct = db.scalars(
        select(UserPermission).where(UserPermission.user_id == user.id, UserPermission.permission == perm)
    ).all()
    if any(matches(p.scope_id) for p in direct):
        return True
    via_groups = db.scalars(
        select(GroupPermission)
        .join(UserGroup, UserGroup.group_id == GroupPermission.group_id)
        .where(UserGroup.user_id == user.id, GroupPermission.permission == perm)
    ).all()
    return any(matches(p.scope_id) for p in via_groups)
