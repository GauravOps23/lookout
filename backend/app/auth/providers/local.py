from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import DUMMY_HASH, verify_password
from app.auth.providers.base import ExternalIdentity
from app.models import User


class LocalProvider:
    def authenticate(self, db: Session, login: str, password: str) -> ExternalIdentity | None:
        user = db.scalar(select(User).where(User.login == login.strip().lower()))
        # Always run one verification so unknown logins cost the same as bad passwords.
        ok = verify_password(password, user.password_hash if user else DUMMY_HASH)
        if user is None or not ok or not user.is_active:
            return None
        return ExternalIdentity(user=user)
