import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record
from app.backup import service
from app.db import get_db
from app.deps import Ctx, require
from app.errors import AppError
from app.models import Backup

router = APIRouter(prefix="/api/admin", tags=["backup"])
MAX_UPLOAD = 100 * 1024 * 1024


class BackupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: str = Field(min_length=12)
    include_secrets: bool = True  # resource credentials; off = restore leaves resources without secrets


def _out(b: Backup) -> dict:
    return {"id": str(b.id), "filename": b.filename, "size": b.size, "kind": b.kind,
            "status": b.status, "created_at": b.created_at}


@router.post("/backup", status_code=201)
def make_backup(body: BackupIn, ctx: Ctx = Depends(require("admin.backup")), db: Session = Depends(get_db)):
    row = service.create_backup(db, body.passphrase, include_secrets=body.include_secrets)
    record(db, "backup.create", actor_id=ctx.user.id, target_type="backup", target_id=row.id, ip=ctx.ip)
    return _out(row)


@router.get("/backups")
def list_backups(ctx: Ctx = Depends(require("admin.backup")), db: Session = Depends(get_db)):
    return [_out(b) for b in db.scalars(select(Backup).order_by(Backup.created_at.desc()))]


@router.get("/backups/{backup_id}/download")
def download(backup_id: uuid.UUID, ctx: Ctx = Depends(require("admin.backup")), db: Session = Depends(get_db)):
    row = db.get(Backup, backup_id)
    path = service.backup_path(row) if row else None
    if row is None or not path.is_file():
        raise AppError("NOT_FOUND", "Backup not found")
    return FileResponse(path, filename=row.filename, media_type="application/octet-stream")


@router.post("/restore")
async def restore(file: UploadFile = File(...), passphrase: str = Form(...), dry_run: bool = Form(True),
                  ctx: Ctx = Depends(require("admin.backup")), db: Session = Depends(get_db)):
    blob = await file.read(MAX_UPLOAD + 1)
    if len(blob) > MAX_UPLOAD:
        raise AppError("VALIDATION", "Upload too large")
    export = service.read_archive(blob, passphrase)
    report = service.restore(db, export, apply=not dry_run, actor=ctx.user)
    record(db, "backup.restore" if not dry_run else "backup.restore_dry_run", actor_id=ctx.user.id,
           target_type="backup", ip=ctx.ip, detail={"report": report})
    return {"dry_run": dry_run, "report": report}
