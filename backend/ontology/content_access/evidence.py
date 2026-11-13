"""Filter embedded document excerpts using the existing source authorization."""

from copy import copy

from sqlalchemy import select

from ontology.content_access.service import document_condition
from ontology.documents.storage_models import Document


def visible_document_ids(session, ctx, document_ids):
    ids = set(document_ids)
    if not ids:
        return set()
    # A context is scoped to one request/transaction and its locked authority.
    # Collection callers can prime all references once before serializing rows.
    known = getattr(ctx, "_evidence_documents", None)
    if known is None:
        known = ctx._evidence_documents = {}
    missing = list(ids - known.keys())
    scoped_ctx = ctx
    if not hasattr(ctx, "content_application_ids"):
        scoped_ctx = copy(ctx)
        scoped_ctx.content_application_ids = []
    for start in range(0, len(missing), 500):
        batch = missing[start : start + 500]
        allowed = set(
            session.scalars(
                select(Document.id).where(
                    Document.tenant_id == ctx.user.tenant_id,
                    Document.project_id == ctx.project.id,
                    Document.id.in_(batch),
                    document_condition(scoped_ctx),
                )
            )
        )
        known.update((identifier, identifier in allowed) for identifier in batch)
    return {identifier for identifier in ids if known[identifier]}


def filter_evidence(session, ctx, references):
    allowed = visible_document_ids(
        session, ctx, (ref["document_id"] for ref in references if ref.get("document_id"))
    )
    return [ref for ref in references if "document_id" not in ref or ref["document_id"] in allowed]
