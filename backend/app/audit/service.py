import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db import now
from app.models import AuditLog

_SENSITIVE_KEY = re.compile(r"password|secret|token|key|authorization", re.I)


def scrub(value: Any) -> Any:
    """Drop values of sensitive-looking keys so audit detail can never hold a secret."""
    if isinstance(value, dict):
        return {k: "***" if _SENSITIVE_KEY.search(str(k)) else scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def record(
    db: Session, action: str, *, actor_id: uuid.UUID | None = None, target_type: str = "",
    target_id: Any = "", ip: str = "", detail: dict | None = None, commit: bool = True,
) -> None:
    db.add(AuditLog(
        at=now(), actor_id=actor_id, action=action, target_type=target_type,
        target_id=str(target_id), ip=ip, detail=scrub(detail or {}),
    ))
    if commit:
        db.commit()
