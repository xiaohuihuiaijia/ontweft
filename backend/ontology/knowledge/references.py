from ontology.content_access.service import require_document
from ontology.documents.history import canonical_references


def validate_references(session, ctx, references):
    for reference in references:
        require_document(session, ctx, reference.get("document_id"))
    return canonical_references(session, ctx, references)
