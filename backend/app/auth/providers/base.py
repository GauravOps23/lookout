from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from app.models import User


@dataclass
class ExternalIdentity:
    user: User
    groups: list[str] = field(default_factory=list)  # external groups, mapped to app groups by LDAP provider


class AuthProvider(Protocol):
    """Pluggable authentication. v1 ships LocalProvider; an LdapProvider can be added later."""

    def authenticate(self, db: Session, login: str, password: str) -> ExternalIdentity | None: ...
