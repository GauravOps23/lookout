from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import logging_filter
from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.backup.router import router as backup_router
from app.config import get_settings
from app.db import get_db
from app.dashboards.router import router as dashboards_router
from app.errors import AppError, install_handlers
from app.home.router import router as home_router
from app.links.router import router as links_router
from app.llm.router import admin_router as llm_admin_router, router as llm_router
from app.resources.router import router as resources_router
from app.views.router import router as views_router


def create_app() -> FastAPI:
    logging_filter.install()
    s = get_settings()
    app = FastAPI(title="Lookout", docs_url=None, redoc_url=None, openapi_url=None)
    install_handlers(app)
    if s.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=s.allowed_origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Content-Type", "X-CSRF-Token"])

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if s.cookie_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(db: Session = Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            raise AppError("UPSTREAM_ERROR", "Database not reachable") from None
        return {"status": "ready"}

    for r in (auth_router, admin_router, backup_router, resources_router, views_router,
              dashboards_router, links_router, home_router, llm_admin_router, llm_router):
        app.include_router(r)
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))  # last: API routes win
    return app


app = create_app()
