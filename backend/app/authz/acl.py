"""Object-level access (spec 4.2 step 3). Pure logic; the ACL tables arrive with resources in M2."""
import uuid
from dataclasses import dataclass
from enum import IntEnum


class Level(IntEnum):
    view = 1
    use = 2
    edit = 3
    manage = 4


@dataclass(frozen=True)
class AclEntry:
    principal_type: str  # "user" | "group"
    principal_id: uuid.UUID
    level: Level


def effective_level(
    *, owner_id: uuid.UUID | None, user_id: uuid.UUID, group_ids: set[uuid.UUID],
    entries: list[AclEntry], is_admin: bool = False,
) -> Level | None:
    """Highest level the user holds on an object, or None (default deny)."""
    if is_admin or (owner_id is not None and owner_id == user_id):
        return Level.manage
    best: Level | None = None
    for e in entries:
        hit = (e.principal_type == "user" and e.principal_id == user_id) or (
            e.principal_type == "group" and e.principal_id in group_ids
        )
        if hit and (best is None or e.level > best):
            best = e.level
    return best


def can(level_held: Level | None, required: Level) -> bool:
    return level_held is not None and level_held >= required
