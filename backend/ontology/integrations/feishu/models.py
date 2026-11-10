"""Tenant application configuration and private employee OAuth state."""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import uid


class FeishuApplication(Base):
    __tablename__ = "feishu_applications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_feishu_application_scope"),
        UniqueConstraint("tenant_id", "service", "app_id", name="uq_feishu_application_client"),
        CheckConstraint("revision >= 1", name="ck_feishu_application_revision"),
        CheckConstraint("service IN ('feishu', 'lark')", name="ck_feishu_application_service"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    service: Mapped[str] = mapped_column(String(16))
    app_id: Mapped[str] = mapped_column(String(200))
    external_tenant_key: Mapped[str] = mapped_column(String(200))
    secret: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class FeishuAccount(Base):
    __tablename__ = "feishu_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_feishu_account_scope"),
        UniqueConstraint("tenant_id", "application_id", "user_id", name="uq_feishu_account_owner"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"], ["feishu_applications.tenant_id", "feishu_applications.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        CheckConstraint("revision >= 1", name="ck_feishu_account_revision"),
        CheckConstraint(
            "status IN ('pending', 'active', 'refreshing', 'reauthorize', 'revoked')",
            name="ck_feishu_account_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    application_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    user_revision: Mapped[int] = mapped_column(Integer)
    application_revision: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    external_user_id: Mapped[str] = mapped_column(String(200), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    secret: Mapped[str] = mapped_column(Text, default="")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[float] = mapped_column(Float, default=0)
    refresh_expires_at: Mapped[float] = mapped_column(Float, default=0)
    refresh_deadline: Mapped[float] = mapped_column(Float, default=0)


class FeishuAuthorization(Base):
    __tablename__ = "feishu_authorizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "account_id"], ["feishu_accounts.tenant_id", "feishu_accounts.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
    )
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str] = mapped_column(String(36))
    session_hash: Mapped[str] = mapped_column(String(64))
    account_revision: Mapped[int] = mapped_column(Integer)
    verifier: Mapped[str] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
