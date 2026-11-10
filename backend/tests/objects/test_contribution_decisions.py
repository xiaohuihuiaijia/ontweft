"""API adjudication over real isolated ledgers, not live source collection.

Fixtures persist immutable raw records and exercise the production contribution
aggregator. PostgreSQL cases require the existing explicit admin/runtime URLs;
they create isolated databases and never use the development tenant.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import ontology.search.objects.index as search_index
import pytest
from ontology.modeling.models import Release
from ontology.objects.contributions.decision_models import ContributionDecision
from ontology.objects.contributions.models import SourceContribution
from ontology.objects.models import Entity
from ontology.search.objects.index import process_one_index
from ontology.search.objects.models import ObjectSearchDocument, ObjectSearchRequest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.support.contribution_decisions import (
    assert_suspended,
    conflict_fixture,
    context,
    decide,
    ledger_snapshot,
    path,
    payload,
    preview,
    role_client,
    source_version,
)
from tests.support.contribution_decisions import decision_platform as decision_platform  # noqa: PLC0414 - pytest fixture registration
from tests.support.platform import publish
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414 - pytest fixture registration
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414 - pytest fixture registration


def test_preview_is_read_only_and_commit_preserves_raw_contributions_and_manual_fields(decision_platform):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    original = ledger_snapshot(app, owner)
    body, result = preview(client, ids)
    assert result["before"]["present"] is False
    assert result["after"]["present"] is True and result["after"]["value"] == "12"
    assert result["data_status_after"] == "active"
    assert ledger_snapshot(app, owner) == original
    response = client.post(path(ids), json=body)
    assert response.status_code in {200, 201}, response.text
    obj = response.json()["object"]
    assert obj["properties"] == {"name": "A", "quantity": "12", "note": "manual retained"}
    assert obj["data_status"] == "active"
    after = ledger_snapshot(app, owner)
    for name in (
        "source_record_revisions",
        "source_identities",
        "source_contributions",
        "contribution_revisions",
    ):
        assert after[name] == original[name]
    assert len(after["contribution_decisions"]) == 1
    decision = after["contribution_decisions"][0]
    assert decision["selected_contribution_id"] == ids["contributions"][1]
    assert decision["selected_contribution_revision"] == 1
    assert decision["actor_id"] == owner["user_id"]
    assert decision["reason"] == body["reason"]
    assert decision["preview_fingerprint"] == body["expected_preview_fingerprint"]
    assert len(after["entity_revisions"]) == len(original["entity_revisions"]) + 1
    assert len(after["audit_events"]) > len(original["audit_events"])
    assert after["object_search_requests"][0]["requested_revision"] == obj["revision"]
    supporting = [
        ref for ref in obj["evidence"] if "source_record_id" in ref and "quantity" in ref.get("fields", [])
    ]
    assert supporting and all(ref.get("source_record_id") == ids["records"][1] for ref in supporting)
    assert any(
        ref.get("contribution_decision_id") == decision["id"] and ref.get("decision_state") == "selected"
        for ref in obj["evidence"]
    )
    assert all("content_hash" not in ref for ref in obj["evidence"] if "source_record_id" in ref)


def test_hold_persists_across_source_changes_and_clear_restores_source_aggregation(decision_platform):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    decide(client, ids)
    held = decide(client, ids, operation="hold", reason="存在待核对凭证，暂停此字段")
    assert held["object"]["data_status"] == "conflict"
    assert_suspended(app, owner, ids)
    source_version(app, owner, ids, 0, values={"name": "A", "quantity": "12"})
    assert_suspended(app, owner, ids)  # Equal sources must not erase an explicit hold.
    cleared = decide(client, ids, operation="clear", reason="解除暂停，恢复一致来源值")
    assert cleared["object"]["properties"]["quantity"] == "12"
    assert cleared["object"]["data_status"] == "active"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        history = session.scalars(
            select(ContributionDecision)
            .where(ContributionDecision.entity_id == ids["entity"])
            .order_by(ContributionDecision.revision)
        ).all()
        assert [row.operation for row in history] == ["select", "hold", "clear"]
        assert [row.revision for row in history] == [1, 2, 3]


def test_clear_selected_value_restores_conflict_instead_of_rewriting_source(decision_platform):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    decide(client, ids)
    cleared = decide(client, ids, operation="clear", reason="撤销选用结论，重新核对双方")
    assert cleared["object"]["data_status"] == "conflict"
    assert_suspended(app, owner, ids)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert sorted(
            row.mapped_values["quantity"]
            for row in session.scalars(
                select(SourceContribution).where(SourceContribution.project_id == owner["project_id"])
            )
        ) == ["10.00", "12"]


@pytest.mark.parametrize("updated_source", [0, 1], ids=["unselected", "selected"])
def test_any_same_value_new_source_record_stales_selection(decision_platform, updated_source):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    selected = decide(client, ids)
    body, _ = preview(client, ids, operation="clear", reason="旧页面待提交")
    old_history = ledger_snapshot(app, owner)["contribution_decisions"]
    new_record = source_version(app, owner, ids, updated_source)
    assert new_record != ids["records"][updated_source]
    assert_suspended(app, owner, ids)
    response = client.post(path(ids), json=body)
    assert response.status_code == 409, response.text
    assert ledger_snapshot(app, owner)["contribution_decisions"] == old_history
    # The current object must not continue consuming an obsolete selected value.
    assert selected["object"]["properties"]["quantity"] == "12"
    restored = decide(client, ids, reason="核对新原件后重新选用")
    assert restored["object"]["data_status"] == "active"
    assert restored["object"]["properties"]["quantity"] == "12"


def test_contribution_fingerprint_fences_same_value_event_when_entity_revision_is_unchanged(
    decision_platform,
):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    body, _ = preview(client, ids)
    source_version(app, owner, ids, 0)
    updated = context(client, ids)
    assert updated["expected_revision"] == body["expected_revision"]
    assert updated["expected_contribution_fingerprint"] != body["expected_contribution_fingerprint"]
    original = ledger_snapshot(app, owner)
    assert client.post(path(ids), json=body).status_code == 409
    assert ledger_snapshot(app, owner) == original


def test_selected_source_withdrawal_does_not_silently_switch_to_other_source(decision_platform):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    decide(client, ids)
    source_version(app, owner, ids, 1, withdraw=True)
    assert_suspended(app, owner, ids)
    cleared = decide(client, ids, operation="clear", reason="确认撤回，恢复剩余有效来源")
    assert cleared["object"]["data_status"] == "active"
    assert cleared["object"]["properties"]["quantity"] == "10.00"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.get(SourceContribution, ids["contributions"][1]).active is False


def test_publication_invalidates_selection_and_old_preview(platform):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner)
    decide(client, ids)
    body, _ = preview(client, ids, operation="clear", reason="发布前的旧预览")
    with app.state.db.transaction(owner["tenant_id"]) as session:
        definition = deepcopy(session.get(Release, ids["release"]).definition)
    definition["types"][0]["label"] = "库存新版模型"
    publish(client, owner["project_id"], definition)
    assert context(client, ids)["expected_model_release_id"] != ids["release"]
    assert client.post(path(ids), json=body).status_code == 409
    assert_suspended(app, owner, ids)
    assert decide(client, ids)["object"]["data_status"] == "active"


@pytest.mark.parametrize(
    "changed",
    [
        {"selected_contribution_id": "00000000-0000-0000-0000-000000000000"},
        {"reason": "预览后替换原因"},
        {"operation": "hold", "selected_contribution_id": None},
        {"expected_preview_fingerprint": "0" * 64},
    ],
)
def test_preview_receipt_is_bound_to_complete_intent(platform, changed):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner)
    body, _ = preview(client, ids)
    original = ledger_snapshot(app, owner)
    response = client.post(path(ids), json=body | changed)
    assert response.status_code in {404, 409, 422}, response.text
    assert ledger_snapshot(app, owner) == original


@pytest.mark.parametrize(
    "operation,selected,reason,field",
    [
        ("select", None, "缺少来源", "quantity"),
        ("hold", "selected", "暂停不接受来源选择", "quantity"),
        ("clear", "selected", "撤销不接受来源选择", "quantity"),
        ("select", "selected", " \t\n ", "quantity"),
        ("select", "selected", "不能裁决人工属性", "note"),
    ],
)
def test_invalid_decision_does_not_write(platform, operation, selected, reason, field):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner)
    body = payload(client, ids)
    body.update(
        operation=operation,
        selected_contribution_id=ids["contributions"][1] if selected else None,
        reason=reason,
        field_key=field,
    )
    original = ledger_snapshot(app, owner)
    response = client.post(path(ids) + "/preview", json=body)
    assert response.status_code in {403, 404, 422}, response.text
    assert ledger_snapshot(app, owner) == original


@pytest.mark.parametrize("role", ["operator", "member", "auditor"])
def test_roles_without_source_governance_cannot_read_preview_submit_or_history(platform, role):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner)
    body, _ = preview(client, ids)
    decide(client, ids)
    reader, _ = role_client(client, app, owner, role)
    try:
        original = ledger_snapshot(app, owner)
        calls = [
            reader.get(path(ids)),
            reader.get(path(ids) + "/quantity/history"),
            reader.post(
                path(ids) + "/preview",
                json={k: v for k, v in body.items() if k != "expected_preview_fingerprint"},
            ),
            reader.post(path(ids), json=body),
        ]
        assert [response.status_code for response in calls] == [403] * 4
        assert ledger_snapshot(app, owner) == original
    finally:
        reader.close()


def test_revocation_after_preview_is_checked_again_at_commit(platform):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner)
    reviewer, user_id = role_client(client, app, owner, "modeler")
    try:
        body, _ = preview(reviewer, ids)
        changed = client.post(
            "/api/members", json={"project_id": owner["project_id"], "user_id": user_id, "role": "member"}
        )
        assert changed.status_code == 200, changed.text
        original = ledger_snapshot(app, owner)
        assert reviewer.post(path(ids), json=body).status_code == 403
        assert ledger_snapshot(app, owner) == original
    finally:
        reviewer.close()


@pytest.mark.parametrize("restricted_operation", ["read", "write"])
def test_field_permissions_filter_context_and_guard_history_and_decisions(platform, restricted_operation):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner)
    decide(client, ids)
    with app.state.db.transaction(owner["tenant_id"]) as session:
        definition = deepcopy(session.get(Release, ids["release"]).definition)
    quantity = next(prop for prop in definition["types"][0]["properties"] if prop["key"] == "quantity")
    quantity[f"{restricted_operation}_roles"] = ["owner"]
    publish(client, owner["project_id"], definition)
    body, _ = preview(client, ids)
    reader, _ = role_client(client, app, owner, "modeler")
    try:
        original = ledger_snapshot(app, owner)
        visible = context(reader, ids)
        fields = {row["field_key"]: row for row in visible["fields"]}
        if restricted_operation == "read":
            assert "quantity" not in fields
            assert reader.get(path(ids) + "/quantity/history").status_code == 404
            status = 404
        else:
            assert fields["quantity"]["can_decide"] is False
            assert reader.get(path(ids) + "/quantity/history").status_code == 200
            status = 403
        assert (
            reader.post(
                path(ids) + "/preview",
                json={k: v for k, v in body.items() if k != "expected_preview_fingerprint"},
            ).status_code
            == status
        )
        assert reader.post(path(ids), json=body).status_code == status
        assert ledger_snapshot(app, owner) == original
    finally:
        reader.close()


def test_explicit_null_is_selectable_without_withdrawing_either_source(platform):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner, quantity=None)
    body, proposed = preview(client, ids)
    assert proposed["after"] == {"present": True, "value": None}
    response = client.post(path(ids), json=body)
    assert response.status_code == 201, response.text
    assert response.json()["object"]["properties"]["quantity"] is None
    assert response.json()["object"]["data_status"] == "active"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert all(row.active for row in session.scalars(select(SourceContribution)))


def test_numeric_equivalence_does_not_create_a_false_conflict(platform):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner, quantity="10.0")
    current = context(client, ids)
    quantity = next(field for field in current["fields"] if field["field_key"] == "quantity")
    assert quantity["state"] == "unopposed" and current["data_status"] == "active"
    assert [candidate["value"] for candidate in quantity["candidates"]].count("10.00") == 1
    assert [candidate["value"] for candidate in quantity["candidates"]].count("10.0") == 1


def test_cross_tenant_and_other_project_candidates_cannot_be_selected(decision_platform):
    client, app, owner, other = decision_platform
    ids = conflict_fixture(app, owner)
    response = client.post("/api/projects", json={"name": "另一来源治理项目"})
    assert response.status_code == 201, response.text
    other_project_owner = {**owner, "project_id": response.json()["id"]}
    other_project = conflict_fixture(app, other_project_owner)
    foreign = conflict_fixture(app, other)
    original = ledger_snapshot(app, owner)
    assert client.get(path(foreign)).status_code == 404
    assert client.get(path(foreign) + "/quantity/history").status_code == 404
    for candidate in (foreign, other_project):
        body = payload(client, ids)
        body["selected_contribution_id"] = candidate["contributions"][1]
        response = client.post(path(ids) + "/preview", json=body)
        assert response.status_code in {404, 422}, response.text
    assert ledger_snapshot(app, owner) == original


def test_history_is_immutable_and_paginated_without_duplicate_decisions(platform):
    client, app, owner, _ = platform
    ids = conflict_fixture(app, owner)
    operations = ["select", "hold", "clear", "select"]
    expected_ids = [decide(client, ids, operation=operation)["decision"]["id"] for operation in operations]
    received, cursor = [], None
    for _ in range(5):
        response = client.get(
            path(ids) + "/quantity/history", params={"limit": 2, **({"before": cursor} if cursor else {})}
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= 2
        received.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and received == list(reversed(expected_ids))
    with app.state.db.transaction(owner["tenant_id"]) as session:
        rows = session.scalars(
            select(ContributionDecision)
            .where(ContributionDecision.entity_id == ids["entity"])
            .order_by(ContributionDecision.revision)
        ).all()
        assert [row.operation for row in rows] == operations


def test_failed_index_enqueue_rolls_back_decision_object_history_and_audit(decision_platform, monkeypatch):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    body, _ = preview(client, ids)
    before = ledger_snapshot(app, owner)

    def reject_enqueue(session, obj):
        # Verify failure happens after the transaction has attempted a decision,
        # not before the API enters its mutation path.
        session.flush()
        assert (
            session.scalar(select(ContributionDecision.id).where(ContributionDecision.entity_id == obj.id))
            is not None
        )
        raise RuntimeError("adjudication index enqueue deliberately failed")

    monkeypatch.setattr(search_index, "enqueue_entity", reject_enqueue)
    with pytest.raises(RuntimeError, match="adjudication index enqueue deliberately failed"):
        client.post(path(ids), json=body)
    assert ledger_snapshot(app, owner) == before


def test_committed_decision_enqueues_real_index_and_hold_removes_projection(decision_platform):
    client, app, owner, _ = decision_platform
    ids = conflict_fixture(app, owner)
    committed = decide(client, ids)
    for _ in range(20):
        if not process_one_index(app.state.db, owner["tenant_id"]):
            break
    else:
        pytest.fail("Object index did not drain")
    with app.state.db.transaction(owner["tenant_id"]) as session:
        receipt = session.scalar(
            select(ObjectSearchDocument).where(ObjectSearchDocument.entity_id == ids["entity"])
        )
        assert receipt.status == "ready" and receipt.entity_revision == committed["object"]["revision"]
        assert (
            session.scalar(select(ObjectSearchRequest).where(ObjectSearchRequest.entity_id == ids["entity"]))
            is None
        )
    held = decide(client, ids, operation="hold", reason="待审暂停索引消费")
    for _ in range(20):
        if not process_one_index(app.state.db, owner["tenant_id"]):
            break
    with app.state.db.transaction(owner["tenant_id"]) as session:
        receipt = session.scalar(
            select(ObjectSearchDocument).where(ObjectSearchDocument.entity_id == ids["entity"])
        )
        assert receipt.status == "removed" and receipt.entity_revision == held["object"]["revision"]


def test_postgres_concurrent_decisions_have_one_winner_and_reject_history_mutation(request):
    client, app, owner, other = request.getfixturevalue("pg_platform")
    ids = conflict_fixture(app, owner)
    bodies = [preview(client, ids, selected=index)[0] for index in (0, 1)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: client.post(path(ids), json=body), bodies))
    assert sorted(response.status_code for response in results) in ([200, 409], [201, 409])
    with app.state.db.transaction(owner["tenant_id"]) as session:
        rows = list(
            session.scalars(
                select(ContributionDecision).where(ContributionDecision.entity_id == ids["entity"])
            )
        )
        assert len(rows) == 1
        chosen = session.get(SourceContribution, rows[0].selected_contribution_id)
        assert session.get(Entity, ids["entity"]).properties["quantity"] == chosen.mapped_values["quantity"]
    with app.state.db.transaction(other["tenant_id"]) as session:
        assert session.scalars(select(ContributionDecision)).all() == []
    with app.state.db.transaction() as session:
        assert session.scalars(select(ContributionDecision)).all() == []
    admin, _ = request.getfixturevalue("pg_databases")
    for statement in (
        "UPDATE contribution_decisions SET reason='rewritten'",
        "DELETE FROM contribution_decisions",
    ):
        with pytest.raises(DBAPIError), app.state.db.transaction(owner["tenant_id"]) as session:
            session.execute(text(statement))
        with pytest.raises(DBAPIError, match="Immutable"), admin.transaction(owner["tenant_id"]) as session:
            session.execute(text(statement))
