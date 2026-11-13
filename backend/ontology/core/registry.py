"""The complete ORM metadata shared by runtime, migrations and provisioning."""

from importlib import import_module

from sqlalchemy import text

IMMUTABLE_TABLES = frozenset(
    {
        "releases",
        "entity_revisions",
        "relation_revisions",
        "knowledge_revisions",
        "document_sources",
        "knowledge_source_dependencies",
        "document_parse_versions",
        "ai_document_pages",
        "audit_events",
        "source_record_revisions",
        "action_attempts",
        "function_runs",
        "cdc_events",
        "contribution_revisions",
        "contribution_decisions",
        "cdc_projection_bindings",
        "cdc_projection_windows",
    }
)

MODEL_MODULES = (
    "ontology.identity.models",
    "ontology.intelligence.models",
    "ontology.intelligence.proposal_models",
    "ontology.modeling.models",
    "ontology.objects.models",
    "ontology.objects.rule_models",
    "ontology.sources.models",
    "ontology.documents.storage_models",
    "ontology.documents.history_models",
    "ontology.knowledge.models",
    "ontology.jobs.models",
    "ontology.audit.models",
    "ontology.actions.run_models",
    "ontology.actions.models",
    "ontology.functions.models",
    "ontology.cdc.models",
    "ontology.cdc.projection_models",
    "ontology.objects.contributions.models",
    "ontology.objects.contributions.decision_models",
    "ontology.search.objects.models",
    "ontology.search.content.models",
    "ontology.content_access.models",
    "ontology.integrations.feishu.models",
)


def register_models():
    # Imports are idempotent; definitions exist only in their owning module.
    from ontology.core.db import Base

    for name in MODEL_MODULES:
        import_module(name)
    return Base.metadata


def grant_runtime_access(connection, role_name):
    """Use the same PostgreSQL table policy in deployment, development and tests."""
    quote = connection.dialect.identifier_preparer.quote
    role = quote(role_name)
    connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    for table in register_models().sorted_tables:
        grant = (
            "SELECT"
            if table.name == "tenants"
            else ("SELECT, INSERT" if table.name in IMMUTABLE_TABLES else "SELECT, INSERT, UPDATE, DELETE")
        )
        name = quote(table.name)
        connection.execute(text(f"REVOKE ALL ON TABLE {name} FROM {role}"))
        connection.execute(text(f"GRANT {grant} ON TABLE {name} TO {role}"))
    connection.execute(text(f"GRANT SELECT ON alembic_version TO {role}"))
