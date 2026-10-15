import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.sessions import load_session
from app.authz.permissions import has_capability
from app.db import get_db
from app.errors import AppError
from app.models import SessionRow, User

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Ctx:
    user: User
    session: SessionRow
    ip: str


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _context(request: Request, db: Session) -> Ctx:
    row = load_session(db, request.cookies.get("sid"))
    if row is None or not row.user.is_active:
        raise AppError("UNAUTHENTICATED", "Not signed in")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, row.csrf_token):
            raise AppError("FORBIDDEN", "CSRF token missing or invalid")
    return Ctx(user=row.user, session=row, ip=client_ip(request))


def current_ctx_allow_pw_change(request: Request, db: Session = Depends(get_db)) -> Ctx:
    return _context(request, db)


def current_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    ctx = _context(request, db)
    if ctx.user.must_change_pw:
        raise AppError("PASSWORD_CHANGE_REQUIRED", "Password change required")
    return ctx


def require(permission: str):
    """Route dependency: every route goes through the authorization layer (default deny)."""

    def dep(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)) -> Ctx:
        if not has_capability(db, ctx.user, permission):
            raise AppError("FORBIDDEN", "Permission denied")
        return ctx

    return dep


class RateLimiter:
    """Sliding-window limiter, per key. In-process: fine for a single app instance."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        t, q = time.monotonic(), self._hits[key]
        while q and q[0] <= t - self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise AppError("RATE_LIMITED", "Too many attempts, try again later")
        q.append(t)
