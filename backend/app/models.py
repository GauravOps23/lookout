import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, IdMixin


class User(IdMixin, Base):
    __tablename__ = "users"
    login: Mapped[str] = mapped_column(String(128), unique=True, index=True)  # stored lower-case
    display_name: Mapped[str] = mapped_column(String(256), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    must_change_pw: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    groups: Mapped[list["Group"]] = relationship(secondary="user_groups", back_populates="users")


class Group(IdMixin, Base):
    __tablename__ = "groups"
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    users: Mapped[list[User]] = relationship(secondary="user_groups", back_populates="groups")


class UserGroup(Base):
    __tablename__ = "user_groups"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)


class GroupPermission(IdMixin, Base):
    __tablename__ = "group_permissions"
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    permission: Mapped[str] = mapped_column(String(64))
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class UserPermission(IdMixin, Base):
    __tablename__ = "user_permissions"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    permission: Mapped[str] = mapped_column(String(64))
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class SessionRow(IdMixin, Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 of cookie value
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(256), default="")
    user: Mapped[User] = relationship()


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_log"
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Setting(IdMixin, Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), unique=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class Backup(IdMixin, Base):
    __tablename__ = "backups"
    filename: Mapped[str] = mapped_column(String(256))
    size: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="ok")


class Resource(IdMixin, Base):
    __tablename__ = "resources"
    type: Mapped[str] = mapped_column(String(16), index=True)  # ssh | oracle | api
    name: Mapped[str] = mapped_column(String(128), index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # non-secret
    secret_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    llm_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # up | down | unknown
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class ResourceAcl(IdMixin, Base):
    __tablename__ = "resource_acl"
    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    principal_type: Mapped[str] = mapped_column(String(8))  # user | group
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    level: Mapped[str] = mapped_column(String(8))  # view | use | edit | manage


class View(IdMixin, Base):
    __tablename__ = "views"
    type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    source_resource_ids: Mapped[list] = mapped_column(JSON, default=list)  # >1 means fan-out
    renderer: Mapped[str] = mapped_column(String(32), default="table")
    schedule_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cache_ttl_s: Mapped[int] = mapped_column(Integer, default=60)
    llm_allowed: Mapped[bool] = mapped_column(Boolean, default=False)


class ViewAcl(IdMixin, Base):
    __tablename__ = "view_acl"
    view_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("views.id", ondelete="CASCADE"), index=True)
    principal_type: Mapped[str] = mapped_column(String(8))
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    level: Mapped[str] = mapped_column(String(8))


class ViewRun(IdMixin, Base):
    __tablename__ = "view_runs"
    view_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("views.id", ondelete="CASCADE"), index=True)
    params_hash: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(8))  # ok | partial | error
    error: Mapped[str] = mapped_column(Text, default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # capped; purged after retention
    triggered_by: Mapped[str] = mapped_column(String(16), default="user")  # schedule | user | llm


class Dashboard(IdMixin, Base):
    __tablename__ = "dashboards"
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    share_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DashboardWidget(IdMixin, Base):
    __tablename__ = "dashboard_widgets"
    dashboard_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dashboards.id", ondelete="CASCADE"), index=True)
    view_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("views.id"), index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    renderer: Mapped[str] = mapped_column(String(32), default="table")
    grid: Mapped[dict] = mapped_column(JSON, default=dict)  # 12-column grid: x, y, w, h
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # view variable values
    refresh_s: Mapped[int] = mapped_column(Integer, default=0)


class DashboardAcl(IdMixin, Base):
    __tablename__ = "dashboard_acl"
    dashboard_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dashboards.id", ondelete="CASCADE"), index=True)
    principal_type: Mapped[str] = mapped_column(String(8))
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    level: Mapped[str] = mapped_column(String(8))


class LinkTile(IdMixin, Base):
    __tablename__ = "link_tiles"
    name: Mapped[str] = mapped_column(String(128), unique=True)
    url: Mapped[str] = mapped_column(String(2048))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    credential_mode: Mapped[str] = mapped_column(String(16), default="none")  # none | shared | per_user
    shared_secret_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)


class LinkUserCredential(IdMixin, Base):
    __tablename__ = "link_user_credentials"
    tile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("link_tiles.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    secret_blob: Mapped[bytes] = mapped_column(LargeBinary)


class LlmConversation(IdMixin, Base):
    __tablename__ = "llm_conversations"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(128), default="")


class LlmMessage(IdMixin, Base):
    __tablename__ = "llm_messages"
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("llm_conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)  # views/resources touched while answering


class LlmAudit(IdMixin, Base):
    """One row per tool call (spec 9.3); sensitive parameter values are never stored."""
    __tablename__ = "llm_audit"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    tool: Mapped[str] = mapped_column(String(32))
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16))  # ok | error
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
