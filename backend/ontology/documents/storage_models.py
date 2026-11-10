from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ontology.core.db import Base
from ontology.core.models import ProjectRecord


class Document(ProjectRecord, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_document_scope"),
        CheckConstraint("parse_revision >= 0", name="ck_document_parse_revision"),
    )
    filename: Mapped[str] = mapped_column(String(250))
    content_hash: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(250))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    parse_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ir_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    ir: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    size: Mapped[int] = mapped_column(Integer)
    uploader_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
