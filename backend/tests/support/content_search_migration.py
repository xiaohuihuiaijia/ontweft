"""Actual content-search migrations, scoped references and restricted PostgreSQL."""

from uuid import uuid4

import pytest
from ontology.core.models import now
from ontology.documents.storage_models import Document
from ontology.knowledge.models import KnowledgePage, KnowledgeRevision
from ontology.search.content.models import (
    ContentSearchChunk,
    ContentSearchDocument,
    ContentSearchRequest,
    ContentSearchState,
)
from sqlalchemy import MetaData, Table

from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration

MODELS = (ContentSearchState, ContentSearchDocument, ContentSearchChunk, ContentSearchRequest)

HEAD = "e8a42b6d901c"


def parents(db, owner, *, project_id=None):
    scope = {"tenant_id": owner["tenant_id"], "project_id": project_id or owner["project_id"]}
    with db.transaction(owner["tenant_id"]) as session:
        document = Document(
            **scope,
            filename="合成资料.md",
            content_hash="a" * 64,
            storage_key="synthetic.md",
            size=14,
            uploader_id=owner["user_id"],
            state="ready",
            ir={"elements": []},
        )
        page = KnowledgePage(**scope, title="已发布知识", body="草稿正文", author_id=owner["user_id"])
        second_page = KnowledgePage(**scope, title="其他页", body="另一页正文", author_id=owner["user_id"])
        session.add_all([document, page, second_page])
        session.flush()
        revisions = [
            KnowledgeRevision(
                **scope,
                page_id=p.id,
                revision=1,
                title=p.title,
                body="发布正文",
                references=[],
                author_id=owner["user_id"],
            )
            for p in (page, second_page)
        ]
        session.add_all(revisions)
        session.flush()
        return {
            "scope": scope,
            "document": document.id,
            "page": page.id,
            "revision": revisions[0].id,
            "other_revision": revisions[1].id,
            "generation": str(uuid4()),
            "receipt": str(uuid4()),
        }


def values(parent, kind="document"):
    scope = parent["scope"]
    asset = {
        "asset_kind": kind,
        "asset_id": parent["document"] if kind == "document" else parent["page"],
        "document_id": parent["document"] if kind == "document" else None,
        "page_id": parent["page"] if kind == "knowledge" else None,
    }
    versions = {
        "generation": parent["generation"],
        "tokenizer_version": "token-test-v1",
        "chunker_version": "chunk-test-v1",
    }
    return {
        ContentSearchState: dict(**scope, kind=kind, **versions),
        ContentSearchDocument: dict(
            **scope,
            **asset,
            **versions,
            id=parent["receipt"],
            knowledge_revision_id=parent["revision"] if kind == "knowledge" else None,
            source_revision=1,
            source_fingerprint="b" * 64,
            title="合成标题",
            status="ready",
        ),
        ContentSearchChunk: dict(
            **scope,
            receipt_id=parent["receipt"],
            ordinal=0,
            text="中文原文",
            token_text="中文 原文",
            anchor={"element_path": "paragraph/0", "char_start": 0, "char_end": 4},
        ),
        ContentSearchRequest: dict(**scope, **asset),
    }


def insert_projection(session, rows):
    # The reference is a scalar FK, not an ORM relationship; flush the parent
    # first so this test is not dependent on unit-of-work object ordering.
    receipt = ContentSearchDocument(**rows[ContentSearchDocument])
    session.add(receipt)
    session.flush()
    session.add_all(model(**fields) for model, fields in rows.items() if model != ContentSearchDocument)
    session.flush()
    return receipt


@pytest.fixture(params=["platform", "pg_platform"])
def content_fixture(request):
    _, app, owner, other = request.getfixturevalue(request.param)
    return app.state.db, owner, other, parents(app.state.db, owner)


def seed_legacy_content(session, legacy):
    scope = legacy["scope"]
    documents, pages, revisions = (
        Table(name, MetaData(), autoload_with=session.connection())
        for name in ("documents", "knowledge_pages", "knowledge_revisions")
    )
    doc_id, page_id, revision_id = (str(uuid4()) for _ in range(3))
    session.execute(
        documents.insert().values(
            id=doc_id,
            **scope,
            created_at=now(),
            filename="旧证据.docx",
            content_hash="c" * 64,
            storage_key="legacy.docx",
            revision=3,
            state="ready",
            size=123,
            uploader_id=legacy["owner"]["user_id"],
            ir={"elements": [{"text": "旧原文😀", "path": "paragraph/0"}]},
        )
    )
    refs = [{"document_id": doc_id, "revision": 3, "element_path": "paragraph/0"}]
    session.execute(
        pages.insert().values(
            id=page_id,
            **scope,
            created_at=now(),
            title="未发布新草稿",
            body="草稿不可冒充旧发布",
            revision=2,
            published_revision=1,
            references=refs,
            author_id=legacy["owner"]["user_id"],
        )
    )
    session.execute(
        revisions.insert().values(
            id=revision_id,
            **scope,
            created_at=now(),
            page_id=page_id,
            revision=1,
            title="旧发布",
            body="不可变知识原文",
            references=refs,
            author_id=legacy["owner"]["user_id"],
        )
    )
    return doc_id, page_id, revision_id
