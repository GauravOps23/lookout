import uuid

from app.authz.acl import AclEntry, Level, can, effective_level
from app.authz.permissions import has_capability
from app.models import Group, GroupPermission, User, UserGroup, UserPermission

U, G, G2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def lvl(entries, owner=None, groups=frozenset(), admin=False):
    return effective_level(owner_id=owner, user_id=U, group_ids=set(groups), entries=entries, is_admin=admin)


def test_default_deny():
    assert lvl([]) is None
    assert not can(lvl([]), Level.view)


def test_owner_and_admin_manage():
    assert lvl([], owner=U) == Level.manage
    assert lvl([], admin=True) == Level.manage


def test_max_over_user_and_group_rows():
    entries = [AclEntry("user", U, Level.view), AclEntry("group", G, Level.edit), AclEntry("group", G2, Level.manage)]
    assert lvl(entries, groups={G}) == Level.edit  # G2 not a member
    assert lvl(entries, groups={G, G2}) == Level.manage


def test_level_ordering():
    assert can(Level.use, Level.view) and can(Level.use, Level.use)
    assert not can(Level.use, Level.edit)


def test_other_principals_ignored():
    assert lvl([AclEntry("user", uuid.uuid4(), Level.manage)]) is None


def test_capability_direct_group_and_default_deny(db):
    u = User(login="bob", password_hash="x")
    g = Group(name="ops")
    db.add_all([u, g])
    db.flush()
    assert not has_capability(db, u, "dashboard.create")
    db.add(UserGroup(user_id=u.id, group_id=g.id))
    db.add(GroupPermission(group_id=g.id, permission="dashboard.create"))
    db.add(UserPermission(user_id=u.id, permission="link.create"))
    db.commit()
    assert has_capability(db, u, "dashboard.create")  # via group
    assert has_capability(db, u, "link.create")  # direct
    assert not has_capability(db, u, "llm.use")
    assert not has_capability(db, u, "not.a.permission")


def test_scoped_permission(db):
    u, tile, other = User(login="carol", password_hash="x"), uuid.uuid4(), uuid.uuid4()
    db.add(u)
    db.flush()
    db.add(UserPermission(user_id=u.id, permission="panel.access", scope_id=tile))
    db.commit()
    assert has_capability(db, u, "panel.access", tile)
    assert not has_capability(db, u, "panel.access", other)
    assert not has_capability(db, u, "panel.access")


def test_admin_implies_all(db):
    assert has_capability(db, User(login="a", password_hash="x", is_admin=True), "admin.backup")
