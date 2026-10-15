from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.auth import passwords
from app.auth.providers.local import LocalProvider
from app.auth.sessions import create_session, destroy_session, destroy_user_sessions
from app.config import get_settings
from app.db import as_utc, get_db, now
from app.deps import Ctx, RateLimiter, client_ip, current_ctx_allow_pw_change
from app.errors import AppError
from app.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])
provider = LocalProvider()
_limiter: RateLimiter | None = None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Strict):
    login: str
    password: str


class ChangePasswordIn(Strict):
    current_password: str
    new_password: str


def user_out(u: User) -> dict:
    return {"id": str(u.id), "login": u.login, "display_name": u.display_name,
            "is_admin": u.is_admin, "must_change_pw": u.must_change_pw}


def _limit(ip: str) -> None:
    global _limiter
    s = get_settings()
    if _limiter is None or _limiter.limit != s.login_rate_per_minute:
        _limiter = RateLimiter(s.login_rate_per_minute)
    _limiter.check(ip)


def reset_rate_limiter() -> None:
    global _limiter
    _limiter = None


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    s, ip = get_settings(), client_ip(request)
    _limit(ip)
    generic = AppError("UNAUTHENTICATED", "Invalid login or password")
    user = db.scalar(select(User).where(User.login == body.login.strip().lower()))

    # Locked accounts get the same generic error, even with the right password.
    if user and user.locked_until and as_utc(user.locked_until) > now():
        record(db, "login.locked", target_type="user", target_id=user.id, ip=ip)
        raise generic

    ident = provider.authenticate(db, body.login, body.password)
    if ident is None:
        if user:
            user.failed_attempts += 1
            if user.failed_attempts >= s.max_failed_attempts:
                user.locked_until = now() + timedelta(minutes=s.lockout_minutes)
                user.failed_attempts = 0
                record(db, "login.lockout", target_type="user", target_id=user.id, ip=ip, commit=False)
            db.commit()
        record(db, "login.fail", target_type="user", target_id=user.id if user else "", ip=ip,
               detail={"login": body.login[:128]})
        raise generic

    user = ident.user
    user.failed_attempts, user.locked_until, user.last_login = 0, None, now()
    db.commit()
    token, row = create_session(db, user, ip, request.headers.get("user-agent", ""))
    response.set_cookie(
        "sid", token, httponly=True, secure=s.cookie_secure, samesite="strict",
        max_age=s.session_absolute_hours * 3600, path="/",
    )
    record(db, "login.success", actor_id=user.id, target_type="user", target_id=user.id, ip=ip)
    return {"user": user_out(user), "csrf_token": row.csrf_token}


@router.post("/logout", status_code=204)
def logout(response: Response, ctx: Ctx = Depends(current_ctx_allow_pw_change), db: Session = Depends(get_db)):
    destroy_session(db, ctx.session)
    response.delete_cookie("sid", path="/")


@router.get("/me")
def me(ctx: Ctx = Depends(current_ctx_allow_pw_change)):
    return {"user": user_out(ctx.user), "csrf_token": ctx.session.csrf_token}


@router.post("/change-password", status_code=204)
def change_password(body: ChangePasswordIn, ctx: Ctx = Depends(current_ctx_allow_pw_change),
                    db: Session = Depends(get_db)):
    u = ctx.user
    if not passwords.verify_password(body.current_password, u.password_hash):
        raise AppError("FORBIDDEN", "Current password is incorrect")
    if (err := passwords.validate_strength(body.new_password)):
        raise AppError("VALIDATION", err)
    if body.new_password == body.current_password:
        raise AppError("VALIDATION", "New password must differ from the current one")
    u.password_hash, u.must_change_pw = passwords.hash_password(body.new_password), False
    db.commit()
    destroy_user_sessions(db, u.id, keep=ctx.session)
    record(db, "password.change", actor_id=u.id, target_type="user", target_id=u.id, ip=ctx.ip)
