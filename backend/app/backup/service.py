"""Backup / restore (spec section 10).

Archive = tar(export.json, manifest.json), encrypted with a key derived (scrypt) from an admin-supplied
passphrase and AES-256-GCM. NOTE: the spec names `age`; this uses `cryptography` primitives instead
(no extra native dependency). Password hashes are never exported; restored users get an unusable
password and must be reset by an admin.
"""
import hashlib
import io
import json
import os
import tarfile
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import passwords
from app.config import get_settings
from app.db import now
from app.errors import AppError
from app.models import (Backup, Dashboard, DashboardAcl, DashboardWidget, Group, GroupPermission, LinkTile, Resource,
                        ResourceAcl, Setting, User, UserGroup, UserPermission, View, ViewAcl)
from app.resources import service as rsvc

SCHEMA_VERSION = 3  # v2: resources, views, ACLs, secrets.json; v3: dashboards, link tiles
MAGIC = b"EPB1"
MAX_PLAINTEXT = 256 * 1024 * 1024


def _kdf(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def encrypt(data: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_kdf(passphrase, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    if blob[:4] != MAGIC or len(blob) < 4 + 16 + 12 + 16:
        raise AppError("VALIDATION", "Not a backup archive")
    salt, nonce, ct = blob[4:20], blob[20:32], blob[32:]
    try:
        return AESGCM(_kdf(passphrase, salt)).decrypt(nonce, ct, MAGIC)
    except InvalidTag:
        raise AppError("VALIDATION", "Wrong passphrase or corrupted archive") from None


def build_export(db: Session) -> dict:
    users = list(db.scalars(select(User).order_by(User.login)))
    groups = list(db.scalars(select(Group).order_by(Group.name)))
    gname = {g.id: g.name for g in groups}
    ulogin = {u.id: u.login for u in users}
    sc = lambda s: str(s) if s else None  # noqa: E731
    return {
        "users": [{"login": u.login, "display_name": u.display_name, "is_admin": u.is_admin,
                   "is_active": u.is_active} for u in users],
        "groups": [{"name": g.name, "description": g.description} for g in groups],
        "memberships": [{"login": ulogin[m.user_id], "group": gname[m.group_id]}
                        for m in db.scalars(select(UserGroup))],
        "group_permissions": [{"group": gname[p.group_id], "permission": p.permission, "scope_id": sc(p.scope_id)}
                              for p in db.scalars(select(GroupPermission)) if p.group_id in gname],
        "user_permissions": [{"login": ulogin[p.user_id], "permission": p.permission, "scope_id": sc(p.scope_id)}
                             for p in db.scalars(select(UserPermission)) if p.user_id in ulogin],
        "settings": [{"key": s.key, "value": s.value} for s in db.scalars(select(Setting))],
        **_inventory_export(db, ulogin, gname),
    }


def rkey(r: Resource) -> str:
    return f"{r.type}:{r.name}"


def _inventory_export(db: Session, ulogin: dict, gname: dict) -> dict:
    """Resources, views and their ACLs, keyed by natural keys (type:name, login, group name). No secrets."""
    resources = list(db.scalars(select(Resource).order_by(Resource.type, Resource.name)))
    views = list(db.scalars(select(View).order_by(View.type, View.name)))
    rk = {str(r.id): rkey(r) for r in resources}
    vk = {v.id: f"{v.type}:{v.name}" for v in views}
    principal = lambda a: ulogin.get(a.principal_id) if a.principal_type == "user" else gname.get(a.principal_id)  # noqa: E731
    return {
        "resources": [{"type": r.type, "name": r.name, "tags": r.tags, "owner": ulogin.get(r.owner_id),
                       "config": r.config, "llm_allowed": r.llm_allowed} for r in resources],
        "resource_acl": [{"resource": rk[str(a.resource_id)], "principal_type": a.principal_type,
                          "principal": principal(a), "level": a.level}
                         for a in db.scalars(select(ResourceAcl)) if str(a.resource_id) in rk and principal(a)],
        "views": [{"type": v.type, "name": v.name, "description": v.description, "owner": ulogin.get(v.owner_id),
                   "config": v.config, "sources": [rk[str(i)] for i in v.source_resource_ids if str(i) in rk],
                   "renderer": v.renderer, "schedule_cron": v.schedule_cron, "cache_ttl_s": v.cache_ttl_s,
                   "llm_allowed": v.llm_allowed} for v in views],
        "view_acl": [{"view": vk[a.view_id], "principal_type": a.principal_type, "principal": principal(a),
                      "level": a.level} for a in db.scalars(select(ViewAcl)) if a.view_id in vk and principal(a)],
        **_dashboards_export(db, ulogin, principal, vk),
    }


def _dashboards_export(db: Session, ulogin: dict, principal, vk: dict) -> dict:
    """Dashboards (with widgets, ACLs) and link tiles. Share tokens and per-user credentials are NOT exported."""
    ds = list(db.scalars(select(Dashboard).order_by(Dashboard.name)))
    dk = {d.id: d.name for d in ds}
    widgets: dict = {}
    for w in db.scalars(select(DashboardWidget).order_by(DashboardWidget.created_at)):
        if w.view_id in vk:
            widgets.setdefault(w.dashboard_id, []).append({
                "view": vk[w.view_id], "title": w.title, "renderer": w.renderer, "grid": w.grid,
                "options": w.options, "params": w.params, "refresh_s": w.refresh_s})
    return {
        "dashboards": [{"name": d.name, "description": d.description, "owner": ulogin.get(d.owner_id),
                        "widgets": widgets.get(d.id, [])} for d in ds],
        "dashboard_acl": [{"dashboard": dk[a.dashboard_id], "principal_type": a.principal_type,
                           "principal": principal(a), "level": a.level}
                          for a in db.scalars(select(DashboardAcl)) if a.dashboard_id in dk and principal(a)],
        "links": [{"name": t.name, "url": t.url, "tags": t.tags, "credential_mode": t.credential_mode,
                   "owner": ulogin.get(t.owner_id)} for t in db.scalars(select(LinkTile).order_by(LinkTile.name))],
    }


def build_archive(db: Session, passphrase: str, include_secrets: bool = True) -> bytes:
    export = json.dumps(build_export(db), sort_keys=True).encode()
    members = [("export.json", export)]
    if include_secrets:  # resource credentials, decrypted with the master key; the archive itself is encrypted
        secrets = {rkey(r): rsvc.open_secret(r) for r in db.scalars(select(Resource)) if r.secret_blob}
        for t in db.scalars(select(LinkTile)):
            if t.shared_secret_blob:
                secrets[f"link:{t.name}"] = json.loads(rsvc.vault().open(t.shared_secret_blob, t.id))
        members.append(("secrets.json", json.dumps(secrets, sort_keys=True).encode()))
    manifest = json.dumps({
        "schema_version": SCHEMA_VERSION, "created_at": now().isoformat(),
        "counts": {k: len(v) for k, v in json.loads(export).items()},
        "sha256": {n: hashlib.sha256(d).hexdigest() for n, d in members},
    }).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in (*members, ("manifest.json", manifest)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return encrypt(buf.getvalue(), passphrase)


def create_backup(db: Session, passphrase: str, kind: str = "manual", include_secrets: bool = True) -> Backup:
    blob = build_archive(db, passphrase, include_secrets)
    d = Path(get_settings().backup_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = f"backup-{now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}.tar.enc"
    (d / name).write_bytes(blob)
    row = Backup(filename=name, size=len(blob), kind=kind)
    db.add(row)
    db.commit()
    return row


def backup_path(row: Backup) -> Path:
    return Path(get_settings().backup_dir) / Path(row.filename).name


def read_archive(blob: bytes, passphrase: str) -> dict:
    plain = decrypt(blob, passphrase)
    if len(plain) > MAX_PLAINTEXT:
        raise AppError("VALIDATION", "Archive too large")
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r") as tar:
            names = set(tar.getnames())
            for name in ("export.json", "manifest.json", "secrets.json"):  # by name only; nothing is extracted
                if name == "secrets.json" and name not in names:
                    continue  # optional
                m = tar.getmember(name)
                f = tar.extractfile(m) if m.isfile() else None
                if f is None:
                    raise KeyError(name)
                files[name] = f.read()
        manifest, export = json.loads(files["manifest.json"]), json.loads(files["export.json"])
        secrets = json.loads(files["secrets.json"]) if "secrets.json" in files else {}
    except (KeyError, tarfile.TarError, ValueError):
        raise AppError("VALIDATION", "Archive is malformed") from None
    if manifest.get("schema_version", 0) > SCHEMA_VERSION:
        raise AppError("CONFLICT", "Archive was made by a newer version of the app")
    for name, data in files.items():
        if name != "manifest.json" and hashlib.sha256(data).hexdigest() != manifest.get("sha256", {}).get(name):
            raise AppError("VALIDATION", "Archive checksum mismatch")
    export["_secrets"] = secrets  # consumed by restore(); never returned to API callers
    return export


def _restore_inventory(db: Session, export: dict, users: dict, groups: dict, actor: User, report: dict) -> None:
    """Resources, views and ACLs, matched by natural key. Missing owners fall back to the restoring admin."""
    secrets = export.get("_secrets", {})
    res = {rkey(r): r for r in db.scalars(select(Resource))}
    for e in export.get("resources", []):
        key = f"{e['type']}:{e['name']}"
        if key in res:
            report["resources"]["skip"] += 1
            continue
        owner = users.get(e.get("owner")) or actor
        r = Resource(id=uuid.uuid4(), type=e["type"], name=e["name"], tags=e.get("tags", []), owner_id=owner.id,
                     config=e.get("config", {}), llm_allowed=e.get("llm_allowed", False))
        if secrets.get(key):
            rsvc.seal_secret(r, secrets[key])  # sealed under THIS host's master key
        db.add(r)
        res[key] = r
        report["resources"]["create"] += 1
    db.flush()

    views = {f"{v.type}:{v.name}": v for v in db.scalars(select(View))}
    for e in export.get("views", []):
        key = f"{e['type']}:{e['name']}"
        if key in views:
            report["views"]["skip"] += 1
            continue
        if any(s not in res for s in e.get("sources", [])) or not e.get("sources"):
            report["views"]["skip"] += 1  # a source is missing from the archive: cannot restore this view
            continue
        owner = users.get(e.get("owner")) or actor
        v = View(type=e["type"], name=e["name"], description=e.get("description", ""), owner_id=owner.id,
                 config=e.get("config", {}), source_resource_ids=[str(res[s].id) for s in e["sources"]],
                 renderer=e.get("renderer", "table"), schedule_cron=e.get("schedule_cron"),
                 cache_ttl_s=e.get("cache_ttl_s", 60), llm_allowed=e.get("llm_allowed", False))
        db.add(v)
        views[key] = v
        report["views"]["create"] += 1
    db.flush()

    def principal_id(e):
        return (users if e["principal_type"] == "user" else groups).get(e["principal"])

    have_r = {(a.resource_id, a.principal_type, a.principal_id, a.level) for a in db.scalars(select(ResourceAcl))}
    for e in export.get("resource_acl", []):
        r, p = res.get(e["resource"]), principal_id(e)
        if r is None or p is None:
            continue
        k = (r.id, e["principal_type"], p.id, e["level"])
        if k in have_r:
            report["acl"]["skip"] += 1
        else:
            db.add(ResourceAcl(resource_id=r.id, principal_type=e["principal_type"], principal_id=p.id, level=e["level"]))
            have_r.add(k)
            report["acl"]["create"] += 1
    have_v = {(a.view_id, a.principal_type, a.principal_id, a.level) for a in db.scalars(select(ViewAcl))}
    for e in export.get("view_acl", []):
        v, p = views.get(e["view"]), principal_id(e)
        if v is None or p is None:
            continue
        k = (v.id, e["principal_type"], p.id, e["level"])
        if k in have_v:
            report["acl"]["skip"] += 1
        else:
            db.add(ViewAcl(view_id=v.id, principal_type=e["principal_type"], principal_id=p.id, level=e["level"]))
            have_v.add(k)
            report["acl"]["create"] += 1


def _restore_dashboards(db: Session, export: dict, users: dict, groups: dict, actor: User, report: dict) -> None:
    views = {f"{v.type}:{v.name}": v for v in db.scalars(select(View))}
    secrets = export.get("_secrets", {})
    ds = {d.name: d for d in db.scalars(select(Dashboard))}
    for e in export.get("dashboards", []):
        if e["name"] in ds:
            report["dashboards"]["skip"] += 1
            continue
        d = Dashboard(name=e["name"], description=e.get("description", ""), owner_id=(users.get(e.get("owner")) or actor).id)
        db.add(d)
        db.flush()
        for w in e.get("widgets", []):
            if w["view"] in views:  # a widget whose view is not in this database is dropped
                db.add(DashboardWidget(dashboard_id=d.id, view_id=views[w["view"]].id, title=w.get("title", ""),
                                       renderer=w.get("renderer", "table"), grid=w.get("grid", {}),
                                       options=w.get("options", {}), params=w.get("params", {}),
                                       refresh_s=w.get("refresh_s", 0)))
        ds[d.name] = d
        report["dashboards"]["create"] += 1
    db.flush()
    have = {(a.dashboard_id, a.principal_type, a.principal_id, a.level) for a in db.scalars(select(DashboardAcl))}
    for e in export.get("dashboard_acl", []):
        d, p = ds.get(e["dashboard"]), (users if e["principal_type"] == "user" else groups).get(e["principal"])
        if d is None or p is None or (d.id, e["principal_type"], p.id, e["level"]) in have:
            continue
        db.add(DashboardAcl(dashboard_id=d.id, principal_type=e["principal_type"], principal_id=p.id, level=e["level"]))
        have.add((d.id, e["principal_type"], p.id, e["level"]))
        report["acl"]["create"] += 1

    tiles = {t.name for t in db.scalars(select(LinkTile))}
    for e in export.get("links", []):
        if e["name"] in tiles:
            report["links"]["skip"] += 1
            continue
        t = LinkTile(id=uuid.uuid4(), name=e["name"], url=e["url"], tags=e.get("tags", []),
                     credential_mode=e.get("credential_mode", "none"), owner_id=(users.get(e.get("owner")) or actor).id)
        if t.credential_mode == "shared" and secrets.get(f"link:{t.name}"):
            t.shared_secret_blob = rsvc.vault().seal(json.dumps(secrets[f"link:{t.name}"]).encode(), t.id)
        db.add(t)
        report["links"]["create"] += 1


def restore(db: Session, export: dict, apply: bool, actor: User | None = None) -> dict:
    """Diff or apply an export. Rows are matched by natural key; existing rows are never deleted.

    Both modes run the same code inside one transaction; a dry run rolls it back.
    """
    report = {k: {"create": 0, "update": 0, "skip": 0}
              for k in ("users", "groups", "memberships", "permissions", "settings", "resources", "views", "acl",
               "dashboards", "links")}
    try:
        users = {u.login: u for u in db.scalars(select(User))}
        groups = {g.name: g for g in db.scalars(select(Group))}

        for e in export.get("users", []):
            if e["login"] in users:
                report["users"]["skip"] += 1
                continue
            report["users"]["create"] += 1
            u = User(login=e["login"], display_name=e.get("display_name", ""),
                     is_admin=e.get("is_admin", False), is_active=e.get("is_active", True),
                     password_hash=passwords.hash_password(passwords.temp_password()), must_change_pw=True)
            db.add(u)
            users[u.login] = u

        for e in export.get("groups", []):
            g = groups.get(e["name"])
            if g is None:
                report["groups"]["create"] += 1
                g = Group(name=e["name"], description=e.get("description", ""))
                db.add(g)
                groups[g.name] = g
            elif g.description != e.get("description", ""):
                report["groups"]["update"] += 1
                g.description = e.get("description", "")
            else:
                report["groups"]["skip"] += 1
        db.flush()  # assigns ids to new users/groups

        have = {(m.user_id, m.group_id) for m in db.scalars(select(UserGroup))}
        for e in export.get("memberships", []):
            u, g = users.get(e["login"]), groups.get(e["group"])
            if u is None or g is None:
                continue
            if (u.id, g.id) in have:
                report["memberships"]["skip"] += 1
            else:
                report["memberships"]["create"] += 1
                db.add(UserGroup(user_id=u.id, group_id=g.id))
                have.add((u.id, g.id))

        gp = {(p.group_id, p.permission, p.scope_id) for p in db.scalars(select(GroupPermission))}
        up = {(p.user_id, p.permission, p.scope_id) for p in db.scalars(select(UserPermission))}
        for e in export.get("group_permissions", []):
            g, scope = groups.get(e["group"]), (uuid.UUID(e["scope_id"]) if e.get("scope_id") else None)
            if g is None:
                continue
            if (g.id, e["permission"], scope) in gp:
                report["permissions"]["skip"] += 1
            else:
                report["permissions"]["create"] += 1
                db.add(GroupPermission(group_id=g.id, permission=e["permission"], scope_id=scope))
                gp.add((g.id, e["permission"], scope))
        for e in export.get("user_permissions", []):
            u, scope = users.get(e["login"]), (uuid.UUID(e["scope_id"]) if e.get("scope_id") else None)
            if u is None:
                continue
            if (u.id, e["permission"], scope) in up:
                report["permissions"]["skip"] += 1
            else:
                report["permissions"]["create"] += 1
                db.add(UserPermission(user_id=u.id, permission=e["permission"], scope_id=scope))
                up.add((u.id, e["permission"], scope))

        cur = {s.key: s for s in db.scalars(select(Setting))}
        for e in export.get("settings", []):
            s = cur.get(e["key"])
            if s is None:
                report["settings"]["create"] += 1
                db.add(Setting(key=e["key"], value=e["value"]))
            elif s.value != e["value"]:
                report["settings"]["update"] += 1
                s.value = e["value"]
            else:
                report["settings"]["skip"] += 1

        if actor is None:
            actor = next((u for u in users.values() if u.is_admin and u.id), None)
        if actor is not None:
            _restore_inventory(db, export, users, groups, actor, report)
            db.flush()
            _restore_dashboards(db, export, users, groups, actor, report)
        elif any(export.get(k) for k in ("resources", "views", "dashboards", "links")):
            raise AppError("VALIDATION", "Restoring resources needs an acting admin")

        db.commit() if apply else db.rollback()
    except Exception:
        db.rollback()
        raise
    return report
