import pytest
from fastapi.testclient import TestClient
from ontology.api import create_app
from ontology.cli import bootstrap
from ontology.core.config import Settings


@pytest.fixture
def platform(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", data_dir=tmp_path / "data"))
    a = bootstrap(
        app.state.db,
        tenant_slug="alpha",
        tenant_name="Alpha",
        email="owner@example.com",
        password="Safe-Test-Password-2026",
    )
    b = bootstrap(
        app.state.db,
        tenant_slug="beta",
        tenant_name="Beta",
        email="owner@example.com",
        password="Safe-Test-Password-2026",
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={
                "tenant_slug": "alpha",
                "email": "owner@example.com",
                "password": "Safe-Test-Password-2026",
            },
        )
        assert response.status_code == 200, response.text
        yield client, app, a, b


@pytest.fixture
def definition():
    return {
        "types": [
            {
                "key": "Project",
                "label": "项目",
                "properties": [
                    {"key": "name", "label": "名称", "data_type": "string", "required": True},
                    {"key": "budget", "label": "预算", "data_type": "number", "read_roles": ["owner"]},
                    {"key": "status", "label": "状态", "data_type": "enum", "enum": ["open", "done"]},
                ],
                "relations": [
                    {"key": "parent", "label": "父项目", "target_type": "Project", "cardinality": "one"}
                ],
            }
        ]
    }
