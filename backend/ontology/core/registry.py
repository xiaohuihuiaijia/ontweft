"""The complete ORM metadata shared by runtime, migrations and provisioning."""

from importlib import import_module

MODEL_MODULES = (
    "ontology.identity.models",
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
