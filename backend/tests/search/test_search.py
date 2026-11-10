"""Actual object mutation, Chinese field index, authorization and PG query path."""

import copy

import ontology.search.objects.index as search_index
import pytest
from fastapi.testclient import TestClient
from ontology.core.domain import save_entity_revision
from ontology.identity.models import Project, User
from ontology.objects.models import Entity
from ontology.search.objects.index import process_one_index
from ontology.search.objects.models import (
    ObjectSearchDocument,
    ObjectSearchField,
    ObjectSearchRequest,
    SearchProjectState,
)
from ontology.search.tokens import TOKENIZER_VERSION, query_tokens
from ontology.worker import run_worker
from sqlalchemy import delete, func, select, text

from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


@pytest.fixture(params=["platform", "pg_platform"])
def search_platform(request):
    return request.getfixturevalue(request.param)


def setup(platform):
    client, app, owner, _ = platform
    definition = {
        "types": [
            {
                "key": "Item",
                "label": "业务对象",
                "properties": [
                    {"key": "name", "label": "名称", "data_type": "string", "required": True},
                    {"key": "description", "label": "描述", "data_type": "string"},
                    {"key": "secret", "label": "内部描述", "data_type": "string", "read_roles": ["owner"]},
                ],
            }
        ]
    }
    publish(client, owner["project_id"], definition)
    return client, app, owner, definition


def create(client, owner, **properties):
    response = client.post(
        "/api/objects",
        json={
            "project_id": owner["project_id"],
            "type_key": "Item",
            "properties": properties,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def search(client, owner, q, **params):
    definition = client.get("/api/ontology/types", params={"project_id": owner["project_id"]}).json()
    return client.post(
        "/api/search/objects",
        json={
            "project_id": owner["project_id"],
            "q": q,
            "expected_release_id": definition["release_id"],
            "expected_model_version": definition["version"],
            **params,
        },
    )


def drain(app, owner, **options):
    for _ in range(30):
        if not process_one_index(app.state.db, owner["tenant_id"], **options):
            break
    else:
        pytest.fail("Index worker did not reach a terminal idle state")


def test_chinese_cross_field_terms_and_normalization(search_platform):
    client, app, owner, _ = setup(search_platform)
    first = create(client, owner, name="南京市长江大桥", description="企业知识图谱 ＡＩ检索 OpenAI")
    create(client, owner, name="南京市业务", description="普通文件")
    pending = search(client, owner, "南京 长江大桥").json()
    assert pending["items"] == [] and pending["index"]["status"] == "not_ready"
    drain(app, owner)
    response = search(client, owner, "南京 长江大桥 AI openai")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 1 and result["items"][0]["id"] == first["id"]
    assert result["matches"][first["id"]]["fields"] == ["description", "name"]
    assert result["index"]["status"] == "ready" and result["index"]["tokenizer_version"] == TOKENIZER_VERSION
    assert query_tokens("ＡＩ") == query_tokens("ai")
    assert search(client, owner, "不存在的罕见业务词").json()["total"] == 0
    assert search(client, owner, "!!!").status_code == 422
    # Operators are ordinary text and never alter the SQL/query language.
    assert search(client, owner, "' OR 1=1 --").json()["total"] == 0


def test_stale_index_hidden_until_current_revision_commits(search_platform):
    client, app, owner, _ = setup(search_platform)
    entity = create(client, owner, name="旧业务名称", description="知识图谱")
    drain(app, owner)
    changed = client.patch(
        f"/api/objects/{entity['id']}",
        json={
            "expected_revision": 1,
            "properties": {"name": "最新采购订单"},
        },
    )
    assert changed.status_code == 200
    assert search(client, owner, "旧业务名称").json()["total"] == 0
    assert search(client, owner, "最新采购订单").json()["total"] == 0
    assert search(client, owner, "知识图谱").json()["index"]["status"] == "updating"
    # Official entrypoint services the same durable queue.
    run_worker(app.state.settings, once=True)
    assert search(client, owner, "最新采购订单").json()["total"] == 1
    with app.state.db.transaction(owner["tenant_id"]) as session:
        stored = session.get(Entity, entity["id"])
        stored.deleted = True  # Simulates immediate revocation before cleanup.
    assert search(client, owner, "最新采购订单").json()["total"] == 0


def test_hidden_fields_cannot_supply_terms_count_or_matches(search_platform):
    client, app, owner, definition = setup(search_platform)
    first = create(client, owner, name="公开项目", secret="机密量子材料")
    second = create(client, owner, name="公开量子材料", description="合作项目")
    drain(app, owner)
    assert search(client, owner, "量子材料").json()["total"] == 2
    response = client.post(
        "/api/users",
        json={
            "email": "search-reader@example.test",
            "display_name": "读者",
            "password": "Read-Search-Password",
        },
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    assert (
        client.post(
            "/api/members", json={"project_id": owner["project_id"], "user_id": user_id, "role": "member"}
        ).status_code
        == 200
    )
    slug = client.get("/api/auth/me").json()["tenant"]["slug"]
    with TestClient(app) as reader:
        assert (
            reader.post(
                "/api/auth/login",
                json={
                    "tenant_slug": slug,
                    "email": "search-reader@example.test",
                    "password": "Read-Search-Password",
                },
            ).status_code
            == 200
        )
        bounded = search(reader, owner, "量子材料", limit=1, count_total=False).json()
        assert bounded["total"] is None and bounded["has_more"] is False
        assert [row["id"] for row in bounded["items"]] == [second["id"]]
        result = search(reader, owner, "量子材料", limit=1).json()
        assert result["total"] == 1 and [item["id"] for item in result["items"]] == [second["id"]]
        assert first["id"] not in result["matches"] and "secret" not in result["items"][0]["properties"]
        assert search(reader, owner, "机密").json()["total"] == 0
        # Changing field ACL requires no index cleanup to block old candidates.
        newer = copy.deepcopy(definition)
        newer["types"][0]["properties"][0]["read_roles"] = ["owner"]
        publish(client, owner["project_id"], newer)
        assert search(reader, owner, "量子材料").json()["total"] == 0
        drain(app, owner)
        assert search(reader, owner, "量子材料").json()["total"] == 0
        assert search(reader, owner, "合作").json()["items"][0]["id"] == second["id"]
        assert client.post(f"/api/users/{user_id}/disable").status_code == 200
        assert (
            reader.post(
                "/api/search/objects",
                json={
                    "project_id": owner["project_id"],
                    "q": "合作",
                    "expected_release_id": None,
                    "expected_model_version": 0,
                },
            ).status_code
            == 401
        )


def test_outbox_and_index_failure_are_atomic_and_rebuildable(search_platform, monkeypatch):
    client, app, owner, _ = setup(search_platform)
    entity = create(client, owner, name="事务原子知识库")
    with pytest.raises(RuntimeError), app.state.db.transaction(owner["tenant_id"]) as session:
        stored = session.get(Entity, entity["id"])
        stored.revision += 1
        stored.properties = {"name": "必须回滚"}
        save_entity_revision(session, stored, session.get(User, owner["user_id"]))
        raise RuntimeError("test rollback")
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(ObjectSearchRequest.requested_revision)) == 1
    original = search_index._advance_checkpoints
    monkeypatch.setattr(
        search_index,
        "_advance_checkpoints",
        lambda *args: (_ for _ in ()).throw(RuntimeError("safe failure")),
    )
    assert not process_one_index(app.state.db, owner["tenant_id"])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(ObjectSearchDocument)) == 0
        assert session.scalar(select(func.count()).select_from(ObjectSearchField)) == 0
        assert session.scalar(select(ObjectSearchRequest.requested_revision)) == 1
        assert session.scalar(select(SearchProjectState.error)) is not None
    monkeypatch.setattr(search_index, "_advance_checkpoints", original)
    model = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()
    assert (
        client.post(
            "/api/search/rebuild",
            json={
                "project_id": owner["project_id"],
                "expected_release_id": model["release_id"],
                "expected_model_version": model["version"],
            },
        ).status_code
        == 202
    )
    drain(app, owner)
    assert search(client, owner, "原子 知识库").json()["total"] == 1


def test_backfill_and_failed_object_do_not_truncate_or_starve(search_platform):
    client, app, owner, _ = setup(search_platform)
    good = create(client, owner, name="企业检索可用")
    bad = create(client, owner, name="企业检索超限", description="a" * 600)
    # A pre-migration database has entities but no new outbox records.
    with app.state.db.transaction(owner["tenant_id"]) as session:
        session.execute(delete(ObjectSearchRequest))
    drain(app, owner, batch_size=1)
    result = search(client, owner, "企业检索").json()
    assert result["index"]["status"] == "failed"
    assert [row["id"] for row in result["items"]] == [good["id"]]
    assert (
        client.patch(
            f"/api/objects/{bad['id']}",
            json={"expected_revision": 1, "properties": {"description": "已修复文本"}},
        ).status_code
        == 200
    )
    drain(app, owner, batch_size=1)
    result = search(client, owner, "企业检索").json()
    assert result["index"]["status"] == "ready" and result["total"] == 2


def test_search_baseline_and_scope_are_enforced(search_platform):
    client, app, owner, other = search_platform
    setup(search_platform)
    create(client, owner, name="模型发布隔离")
    drain(app, owner)
    assert search(client, owner, "隔离", expected_model_version=0).status_code == 409
    assert (
        client.post(
            "/api/search/objects",
            json={
                "project_id": other["project_id"],
                "q": "隔离",
                "expected_release_id": None,
                "expected_model_version": 0,
            },
        ).status_code
        == 404
    )
    assert search(client, owner, "隔离", offset=1001).status_code == 422
    assert search(client, owner, "隔离", type_key="missing").status_code == 422


def test_postgres_search_lock_budget_returns_error_not_empty(pg_platform):
    from concurrent.futures import ThreadPoolExecutor

    client, app, owner, _ = setup(pg_platform)
    create(client, owner, name="受预算约束的查询")
    drain(app, owner)
    model = client.get("/api/ontology/definition", params={"project_id": owner["project_id"]}).json()
    with ThreadPoolExecutor(max_workers=1) as executor:  # noqa: SIM117 -- release DB lock before joining thread.
        with app.state.db.transaction(owner["tenant_id"]) as session:
            session.scalar(select(Project).where(Project.id == owner["project_id"]).with_for_update())
            future = executor.submit(
                client.post,
                "/api/search/objects",
                json={
                    "project_id": owner["project_id"],
                    "q": "查询",
                    "expected_release_id": model["release_id"],
                    "expected_model_version": model["version"],
                },
            )
            response = future.result(timeout=6)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "search_time_budget"
    assert search(client, owner, "查询").json()["total"] == 1
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(text("SHOW statement_timeout")) == "0"


def test_postgres_native_total_budget_releases_connection(pg_platform):
    import time
    from ontology.core.errors import DomainError
    from ontology.search.objects.api import read_transaction

    _, app, owner, _ = pg_platform
    started = time.monotonic()
    with pytest.raises(DomainError) as error, read_transaction(app.state.db, owner["tenant_id"]) as session:
        session.execute(text("SELECT pg_sleep(2)"))
        session.execute(text("SELECT pg_sleep(2)"))
    assert error.value.status == 503
    assert error.value.code in {"search_time_budget", "search_unavailable"}
    assert time.monotonic() - started < 4.5
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(text("SELECT 1")) == 1
        assert session.scalar(text("SHOW statement_timeout")) == "0"


def test_bounded_search_pages_without_claiming_an_exact_total(search_platform):
    client, app, owner, _ = setup(search_platform)
    ids = {create(client, owner, name="sharedasset " + str(i))["id"] for i in range(3)}
    drain(app, owner)
    first = search(client, owner, "sharedasset", count_total=False, limit=2).json()
    last = search(client, owner, "sharedasset", count_total=False, limit=2, offset=2).json()
    assert first["total"] is None and last["total"] is None
    assert first["has_more"] is True and last["has_more"] is False
    assert len(first["items"]) == 2 and len(last["items"]) == 1
    assert {item["id"] for item in first["items"] + last["items"]} == ids
    assert search(client, owner, "sharedasset").json()["total"] == 3


def test_postgres_browse_order_has_an_eligible_index(pg_platform):
    client, app, owner, _ = setup(pg_platform)
    obj = create(client, owner, name="Ordered browse")
    with app.state.db.transaction(owner["tenant_id"]) as session:
        # Tiny fixtures normally prefer a sequential scan. This verifies that
        # the production predicate admits an ordered index path at all: PG
        # does not infer IS FALSE from a partial-index predicate using = false.
        session.execute(text("SET LOCAL enable_seqscan = off"))
        query = (
            select(Entity.id)
            .where(
                Entity.tenant_id == owner["tenant_id"],
                Entity.project_id == owner["project_id"],
                Entity.deleted.is_(False),
                Entity.data_status == "active",
            )
            .order_by(Entity.updated_at.desc(), Entity.id)
            .limit(21)
        )
        assert list(session.scalars(query)) == [obj["id"]]
        sql = query.compile(dialect=session.bind.dialect, compile_kwargs={"literal_binds": True})
        plan = "\n".join(session.scalars(text("EXPLAIN " + str(sql))))
        assert "ix_entity_browse" in plan and "Sort" not in plan


def test_index_waits_for_brief_read_lock_and_preserves_work_on_timeout(pg_platform):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from sqlalchemy import event

    client, app, owner, _ = setup(pg_platform)
    create(client, owner, name="Queued indexing")
    attempted = Event()

    def observe(_connection, _cursor, statement, *_):
        if "FOR UPDATE OF projects" in statement:
            attempted.set()

    event.listen(app.state.db.engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with app.state.db.transaction(owner["tenant_id"]) as session:
                session.scalar(
                    select(Project).where(Project.id == owner["project_id"]).with_for_update(read=True)
                )
                future = pool.submit(process_one_index, app.state.db, owner["tenant_id"])
                assert attempted.wait(3)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.2)
            assert future.result(timeout=5)
            create(client, owner, name="Retained on contention")
            with app.state.db.transaction(owner["tenant_id"]) as session:
                session.scalar(
                    select(Project).where(Project.id == owner["project_id"]).with_for_update(read=True)
                )
                assert not pool.submit(process_one_index, app.state.db, owner["tenant_id"]).result(timeout=4)
            assert process_one_index(app.state.db, owner["tenant_id"])
            assert search(client, owner, "contention").json()["total"] == 1
    finally:
        event.remove(app.state.db.engine, "before_cursor_execute", observe)
