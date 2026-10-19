"""Sensei admin configuration (spec 9.1): one Setting row; the API key is sealed with the vault."""
import base64
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting
from app.resources import service

KEY = "llm"


class LlmConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    base_url: str = Field("http://localhost:11434/v1", pattern=r"^https?://[^\s]+$", max_length=512)
    model: str = Field("", max_length=128)
    api_key: str | None = Field(None, max_length=512)  # None keeps the stored key, "" clears it
    temperature: float = Field(0.2, ge=0, le=2)
    max_tokens: int = Field(1024, ge=64, le=8192)
    timeout_s: int = Field(60, ge=5, le=300)
    system_prompt: str = Field("", max_length=4000)
    tool_mode: Literal["native", "json"] = "native"
    enabled_group_ids: list[uuid.UUID] = []
    rate_limit_per_10min: int = Field(20, ge=1, le=1000)
    max_tool_calls: int = Field(6, ge=1, le=20)
    max_rows: int = Field(50, ge=1, le=200)
    max_chars: int = Field(8000, ge=500, le=50000)


def _row(db: Session) -> Setting | None:
    return db.scalar(select(Setting).where(Setting.key == KEY))


def load(db: Session) -> dict:
    """Stored config merged over the defaults. Never contains the API key (see api_key)."""
    d = LlmConfigIn().model_dump(mode="json", exclude={"api_key"})
    row = _row(db)
    if row:
        d.update({k: v for k, v in row.value.items() if k in d})
    return d


def api_key(db: Session) -> str:
    row = _row(db)
    blob = (row.value.get("api_key_blob") if row else None)
    return service.vault().open(base64.b64decode(blob), row.id).decode() if blob else ""


def save(db: Session, body: LlmConfigIn) -> dict:
    row = _row(db)
    if row is None:
        row = Setting(key=KEY, value={})
        db.add(row)
        db.flush()
    value = body.model_dump(mode="json", exclude={"api_key"})
    old = row.value.get("api_key_blob")
    if body.api_key is None:
        blob = old
    else:
        blob = base64.b64encode(service.vault().seal(body.api_key.encode(), row.id)).decode() if body.api_key else None
    value["api_key_blob"] = blob
    row.value = value
    db.commit()
    return public(db)


def public(db: Session) -> dict:
    row = _row(db)
    return {**load(db), "has_api_key": bool(row and row.value.get("api_key_blob"))}
