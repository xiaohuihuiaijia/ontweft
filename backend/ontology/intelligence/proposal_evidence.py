"""Re-use immutable page interpretations when a user confirms relation evidence."""

from sqlalchemy import select

from ontology.content_access.service import require_document
from ontology.core.errors import DomainError
from ontology.intelligence.proposal_models import AIPage


def validate_page_evidence(session, ctx, references):
    pages = {
        page.id: page
        for page in session.scalars(
            select(AIPage).where(
                AIPage.tenant_id == ctx.user.tenant_id,
                AIPage.project_id == ctx.project.id,
                AIPage.id.in_([ref.get("parse_id") for ref in references]),
            )
        )
    }
    for document_id in {ref.get("document_id") for ref in references}:
        require_document(session, ctx, document_id)
    result = []
    for ref in references:
        page = pages.get(ref.get("parse_id"))
        quote = ref.get("quote")
        if (
            not page
            or page.document_id != ref.get("document_id")
            or page.page_number != ref.get("page")
            or page.result.get("native_parse_id") != ref.get("native_parse_id")
            or not isinstance(quote, str)
            or not quote.strip()
            or "".join(quote.split()) not in "".join(page.result.get("text", "").split())
        ):
            raise DomainError(422, "invalid_ai_evidence", "证据须对应已有解析页及其中的原文摘录")
        result.append(
            {
                "document_id": page.document_id,
                "parse_id": page.id,
                "native_parse_id": page.result.get("native_parse_id"),
                "page": page.page_number,
                "quote": quote,
            }
        )
    return result
