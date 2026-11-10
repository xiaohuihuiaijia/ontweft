import pytest
from sqlalchemy import func, select

from ontology.objects.models import Entity
from ontology.sources import ingestion
from ontology.sources.models import Source, SourceRecordRevision
from tests.support.ingestion import capture, make_source
from tests.support.postgres import pg_databases as pg_databases  # noqa: PLC0414
from tests.support.postgres import pg_platform as pg_platform  # noqa: PLC0414


@pytest.mark.parametrize("fixture_name", ["platform", "pg_platform"])
def test_source_change_after_last_page_prevents_stale_projection(request, fixture_name, monkeypatch):
    client, app, owner, _ = request.getfixturevalue(fixture_name)
    source, resource, _, _ = make_source(client, app, owner["project_id"])
    finish = ingestion.finish

    def change_before_commit(db, run, **kwargs):
        # Change the configuration after capture but before the projection lock.
        with db.transaction(owner["tenant_id"], write=True) as session:
            session.get(Source, source["id"]).revision += 1
        return finish(db, run, **kwargs)

    monkeypatch.setattr(ingestion, "finish", change_before_commit)
    result = capture(client, app, source, resource)
    assert result["snapshot"]["status"] == "failed"
    with app.state.db.transaction(owner["tenant_id"]) as session:
        assert session.scalar(select(func.count()).select_from(Entity)) == 0
        assert session.scalar(select(func.count()).select_from(SourceRecordRevision)) == 2
