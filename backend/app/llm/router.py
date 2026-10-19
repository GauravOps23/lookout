import json
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import db as dbmod
from app.audit.service import record
from app.authz.access import group_ids, level_on
from app.db import get_db, now
from app.deps import Ctx, require
from app.errors import AppError
from app.llm import agent, config
from app.llm import client as llm_client
from app.models import Dashboard, DashboardAcl, LlmConversation, LlmMessage, User, View, ViewAcl

admin_router = APIRouter(prefix="/api/admin/llm", tags=["llm"])
router = APIRouter(prefix="/api/llm", tags=["llm"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- admin -----------------------------------------------------------------------------------
@admin_router.get("")
def get_config(ctx: Ctx = Depends(require("admin.llm")), db: Session = Depends(get_db)):
    return config.public(db)


@admin_router.put("")
def put_config(body: config.LlmConfigIn, ctx: Ctx = Depends(require("admin.llm")), db: Session = Depends(get_db)):
    out = config.save(db, body)
    record(db, "llm.config", actor_id=ctx.user.id, ip=ctx.ip,
           detail={"enabled": body.enabled, "model": body.model, "base_url": body.base_url,
                   "api_key_changed": body.api_key is not None})
    return out


@admin_router.post("/test")
def test_config(ctx: Ctx = Depends(require("admin.llm")), db: Session = Depends(get_db)):
    cfg, key = config.load(db), config.api_key(db)
    probe = {"name": "ping", "description": "Reply by calling this tool.", "parameters":
             {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}
    try:
        pong = llm_client.CLIENT.chat(cfg, key, [{"role": "user", "content": "Reply with the word pong."}], None)
        tool = llm_client.CLIENT.chat(cfg, key, [{"role": "user", "content": "Call the ping tool now."}], [probe])
    except AppError as e:
        return {"ok": False, "error": e.message}
    return {"ok": True, "reply": pong["content"][:200],
            "tool_calls_supported": any(c["name"] == "ping" for c in tool["tool_calls"])}


# ---- chat ------------------------------------------------------------------------------------
def _gate(db: Session, user: User) -> dict:
    cfg = config.load(db)
    if not cfg["enabled"]:
        raise AppError("FORBIDDEN", "Sensei is not enabled")
    allowed = {uuid.UUID(g) for g in cfg["enabled_group_ids"]}
    if not user.is_admin and not (allowed & group_ids(db, user)):
        raise AppError("FORBIDDEN", "Sensei is not enabled for your groups")
    return cfg


def _conversation(db: Session, ctx: Ctx, cid: uuid.UUID) -> LlmConversation:
    c = db.get(LlmConversation, cid)
    if c is None or c.user_id != ctx.user.id:  # own conversations only
        raise AppError("NOT_FOUND", "Not found")
    return c


class ChatContext(Strict):
    view_id: uuid.UUID | None = None
    dashboard_id: uuid.UUID | None = None


class MessageIn(Strict):
    text: str = Field(min_length=1, max_length=4000)
    context: ChatContext | None = None


def _context_note(db: Session, user: User, c: ChatContext | None) -> str:
    if c is None:
        return ""
    if c.view_id:
        v = db.get(View, c.view_id)
        if v is None or level_on(db, user, v, ViewAcl) is None:
            raise AppError("NOT_FOUND", "Context not found")
        return f"The user is looking at the view {v.name!r} (id {v.id}); questions like 'summarise this' refer to it."
    if c.dashboard_id:
        d = db.get(Dashboard, c.dashboard_id)
        if d is None or level_on(db, user, d, DashboardAcl) is None:
            raise AppError("NOT_FOUND", "Context not found")
        return f"The user is looking at the dashboard {d.name!r}; 'summarise this' refers to its views."
    return ""


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/conversations", status_code=201)
def create_conversation(ctx: Ctx = Depends(require("llm.use")), db: Session = Depends(get_db)):
    _gate(db, ctx.user)
    c = LlmConversation(user_id=ctx.user.id)
    db.add(c)
    db.commit()
    return {"id": str(c.id)}


@router.get("/conversations")
def list_conversations(ctx: Ctx = Depends(require("llm.use")), db: Session = Depends(get_db)):
    rows = db.scalars(select(LlmConversation).where(LlmConversation.user_id == ctx.user.id)
                      .order_by(LlmConversation.updated_at.desc()).limit(100))
    return [{"id": str(c.id), "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows]


@router.get("/conversations/{cid}")
def get_conversation(cid: uuid.UUID, ctx: Ctx = Depends(require("llm.use")), db: Session = Depends(get_db)):
    c = _conversation(db, ctx, cid)
    msgs = db.scalars(select(LlmMessage).where(LlmMessage.conversation_id == c.id)
                      .order_by(LlmMessage.created_at, LlmMessage.role.desc()))
    return {"id": str(c.id), "title": c.title,
            "messages": [{"role": m.role, "content": m.content, "sources": m.sources} for m in msgs]}


@router.delete("/conversations/{cid}", status_code=204)
def delete_conversation(cid: uuid.UUID, ctx: Ctx = Depends(require("llm.use")), db: Session = Depends(get_db)):
    db.delete(_conversation(db, ctx, cid))
    db.commit()


@router.post("/conversations/{cid}/messages")
def send_message(cid: uuid.UUID, body: MessageIn, ctx: Ctx = Depends(require("llm.use")),
                 db: Session = Depends(get_db)):
    cfg = _gate(db, ctx.user)
    c = _conversation(db, ctx, cid)
    sent = db.scalar(select(func.count()).select_from(LlmMessage).join(LlmConversation)
                     .where(LlmConversation.user_id == ctx.user.id, LlmMessage.role == "user",
                            LlmMessage.created_at >= now() - timedelta(minutes=10)))
    if sent >= cfg["rate_limit_per_10min"]:
        raise AppError("RATE_LIMITED", "Too many Sensei messages, try again in a few minutes")
    note = _context_note(db, ctx.user, body.context)
    key = config.api_key(db)
    record(db, "llm.message", actor_id=ctx.user.id, target_type="conversation", target_id=c.id, ip=ctx.ip,
           detail={"chars": len(body.text)})
    uid, text = ctx.user.id, body.text

    def stream():
        # The request's DB session is closed once the handler returns, so the stream opens its own.
        with dbmod.session_factory()() as s:
            user, conv = s.get(User, uid), s.get(LlmConversation, cid)
            try:
                for ev, data in agent.run_turn(s, user, conv, text, cfg, key, note):
                    yield _sse(ev, data)
            except Exception:  # noqa: BLE001
                agent.log.exception("sensei turn failed")
                yield _sse("error", {"code": "UPSTREAM_ERROR", "message": "Sensei failed unexpectedly"})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
