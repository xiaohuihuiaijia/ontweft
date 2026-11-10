from contextlib import contextmanager

import pytest

from ontology.sources.models import Source
from tests.support.ingestion import make_source


@pytest.mark.parametrize("operation", ["test", "discover", "sample"])
def test_source_io_releases_local_transaction_and_rechecks_revision(platform, monkeypatch, operation):
    from ontology.sources import api

    client, app, owner, _ = platform
    source, resource, _, _ = make_source(client, app, owner["project_id"])
    transaction = app.state.db.transaction
    active = 0

    @contextmanager
    def tracked(*args, **kwargs):
        nonlocal active
        active += 1
        try:
            with transaction(*args, **kwargs) as session:
                yield session
        finally:
            active -= 1

    factory = api.connector_for

    def connector_for(*args):
        assert active == 0
        connector = factory(*args)
        method = "test_connection" if operation == "test" else "discover"
        original = getattr(connector, method)

        def network(*args, **kwargs):
            assert active == 0
            with app.state.db.transaction(owner["tenant_id"]) as session:
                session.get(Source, source["id"]).revision += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(connector, method, network)
        return connector

    monkeypatch.setattr(app.state.db, "transaction", tracked)
    monkeypatch.setattr(api, "connector_for", connector_for)
    url = f"/api/sources/{source['id']}/{operation}"
    response = (
        client.get(url, params={"resource": resource["resource_id"]})
        if operation == "sample"
        else client.post(url)
    )
    assert response.status_code == 409, response.text
