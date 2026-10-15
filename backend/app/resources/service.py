import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import now
from app.models import Resource, Setting
from app.resources.connectors.base import ConnectorError, Handle, sanitize
from app.resources.connectors.http import HttpConnector
from app.resources.connectors.oracle import OracleConnector
from app.resources.connectors.ssh import SshConnector
from app.secrets.vault import Vault

# Replaceable in tests. HttpConnector gets the admin SSRF allow-list at use time (see connector_for).
CONNECTORS: dict = {"oracle": OracleConnector(), "ssh": SshConnector(), "api": None}

_vault: Vault | None = None


def vault() -> Vault:
    global _vault
    if _vault is None:
        _vault = Vault.from_settings()
    return _vault


def reset_vault() -> None:
    global _vault
    _vault = None


def seal_secret(resource: Resource, secret: dict | None) -> None:
    resource.secret_blob = vault().seal(json.dumps(secret).encode(), resource.id) if secret else None
    resource.key_version = vault().current_version


def open_secret(resource: Resource) -> dict:
    if not resource.secret_blob:
        return {}
    return json.loads(vault().open(resource.secret_blob, resource.id))


def ssrf_allowlist(db: Session) -> list[str]:
    row = db.scalar(select(Setting).where(Setting.key == "api.ssrf_allowlist"))
    return list(row.value.get("entries", [])) if row else []


def connector_for(db: Session, rtype: str):
    c = CONNECTORS[rtype]
    return c if c is not None else HttpConnector(ssrf_allowlist(db))


def handle(resource: Resource) -> Handle:
    return Handle(id=str(resource.id), name=resource.name, type=resource.type,
                  config=dict(resource.config), secret=open_secret(resource))


def apply_observed(db: Session, resource: Resource, h: Handle, error: str = "") -> None:
    """Persist what a connector learned: TOFU host-key pin, or a changed-key failure."""
    fp = h.observed.get("host_key_fingerprint")
    if fp and not resource.config.get("host_key_fingerprint"):
        resource.config = {**resource.config, "host_key_fingerprint": fp}
    if error == "HOST_KEY_CHANGED":
        resource.status, resource.last_error, resource.last_checked_at = "down", error, now()
    db.commit()


def test_resource(db: Session, resource: Resource) -> tuple[bool, str]:
    """Run the connector self-test and store status; the error text is sanitised."""
    ok, err = True, ""
    h = None
    try:
        h = handle(resource)
        connector_for(db, resource.type).ping(h)
    except Exception as e:  # noqa: BLE001 - every failure becomes a stored, sanitised status
        ok, err = False, sanitize(e)
    resource.status, resource.last_error, resource.last_checked_at = ("up" if ok else "down"), err, now()
    if h is not None:
        apply_observed(db, resource, h, err)
    db.commit()
    return ok, err
