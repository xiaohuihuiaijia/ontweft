from typing import Literal

from pydantic import Field

from ontology.core.schemas import RevisionInput, StrictModel


class PropertyDefinition(StrictModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=120)
    data_type: Literal["string", "integer", "number", "boolean", "date", "datetime", "enum", "json"]
    required: bool = False
    enum: list[str] = Field(default_factory=list, max_length=500)
    unit: str | None = None
    read_roles: list[str] = Field(default_factory=list)
    write_roles: list[str] = Field(default_factory=list)
    source_owned: bool = False


class RelationDefinition(StrictModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=120)
    target_type: str
    cardinality: Literal["one", "many"] = "many"


class TypeDefinition(StrictModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.]{0,119}$")
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    properties: list[PropertyDefinition] = Field(default_factory=list, max_length=200)
    unique_keys: list[list[str]] = Field(default_factory=list, max_length=16)
    relations: list[RelationDefinition] = Field(default_factory=list, max_length=100)


class FieldMapping(StrictModel):
    source_field: str = Field(min_length=1, max_length=500)
    transform: Literal["identity", "trim", "lower", "upper", "integer", "number", "boolean", "date"] = (
        "identity"
    )


class MappingDefinition(StrictModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,79}$")
    source_id: str
    resource_id: str = Field(min_length=1, max_length=500)
    type_key: str
    schema_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_fields: list[str] = Field(default_factory=list, max_length=16)
    fields: dict[str, FieldMapping] = Field(min_length=1, max_length=200)


class Definition(StrictModel):
    types: list[TypeDefinition] = Field(max_length=500)
    mappings: list[MappingDefinition] = Field(default_factory=list, max_length=500)


class ChangeSetInput(StrictModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    definition: Definition
    expected_baseline_release_id: str | None = None


class ChangeSetUpdate(RevisionInput):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    definition: Definition | None = None
