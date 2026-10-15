import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import as_utc, now
from app.models import SessionRow, User


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User, ip: str, user_agent: str) -> tuple[str, SessionRow]:
    s = get_settings()
    token = secrets.token_urlsafe(32)  # 256 bits
    row = SessionRow(
        token_hash=_hash(token), user_id=user.id, csrf_token=secrets.token_urlsafe(24),
        expires_at=now() + timedelta(hours=s.session_absolute_hours), last_seen=now(),
        ip=ip, user_agent=user_agent[:256],
    )
    db.add(row)
    db.commit()
    return token, row


def load_session(db: Session, token: str | None) -> SessionRow | None:
    """Return a live session (and refresh last_seen), or None if missing/expired/idle."""
    if not token:
        return None
    row = db.scalar(select(SessionRow).where(SessionRow.token_hash == _hash(token)))
    if row is None:
        return None
    t = now()
    idle = timedelta(minutes=get_settings().session_idle_minutes)
    if as_utc(row.expires_at) <= t or as_utc(row.last_seen) + idle <= t:
        db.delete(row)
        db.commit()
        return None
    row.last_seen = t
    db.commit()
    return row


def destroy_session(db: Session, row: SessionRow) -> None:
    db.delete(row)
    db.commit()


def destroy_user_sessions(db: Session, user_id, keep: SessionRow | None = None) -> None:
    stmt = delete(SessionRow).where(SessionRow.user_id == user_id)
    if keep is not None:
        stmt = stmt.where(SessionRow.id != keep.id)
    db.execute(stmt)
    db.commit()
