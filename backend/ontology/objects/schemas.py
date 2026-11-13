from typing import Any, Literal

from pydantic import Field

from ontology.core.schemas import RevisionInput, StrictModel


class ModelBaselineInput(StrictModel):
    # Omission preserves legacy current-model writes; explicitly supplied null
    # is a real expected baseline, not an instruction to skip validation.
    expected_model_release_id: str | None = Field(default=None, min_length=1, max_length=36)


class ModelRevisionInput(RevisionInput, ModelBaselineInput):
    pass


class ObjectInput(ModelBaselineInput):
    project_id: str
    type_key: str
    properties: dict[str, Any]
    evidence: list[dict] = Field(default_factory=list, max_length=100)


class ObjectUpdate(ModelRevisionInput):
    properties: dict[str, Any]
    reason: str = Field(default="", max_length=2000)


class RelationInput(ModelBaselineInput):
    project_id: str
    type_key: str
    source_id: str
    target_id: str
    properties: dict = Field(default_factory=dict)
    evidence: list[dict] = Field(default_factory=list, max_length=100)


class RelationUpdate(ModelRevisionInput):
    properties: dict[str, Any] = Field(default_factory=dict)
    remove_properties: list[str] = Field(default_factory=list, max_length=200)
    evidence: list[dict] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=2000)


class Filter(StrictModel):
    field: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "is_null"] = "eq"
    value: Any = None


class ObjectQuery(StrictModel):
    project_id: str
    type_key: str
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    sort_field: str | None = None
    sort_desc: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1000000)
    release_id: str | None = None
