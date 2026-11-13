from typing import Any, Literal

from pydantic import Field

from ontology.core.schemas import RevisionInput, StrictModel
from ontology.modeling.schemas import Definition


class Selection(StrictModel):
    source_id: str
    resource_ids: list[str] = Field(min_length=1, max_length=100)


class ProposalInput(StrictModel):
    project_id: str
    connection_id: str
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    source_selections: list[Selection] = Field(default_factory=list, max_length=100)
    instructions: str = Field(default="", max_length=12000)
    quality: Literal["balanced", "high"] = "balanced"
    budget_cny: float = Field(default=20, gt=0, le=1000)
    pages: dict[str, list[int]] = Field(default_factory=dict)


class Evidence(StrictModel):
    document_id: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=12000)
    parse_id: str | None = None
    native_parse_id: str | None = None


class Candidate(StrictModel):
    id: str = Field(min_length=1, max_length=150)
    type_key: str
    properties: dict[str, Any]
    business_key: dict[str, Any] = Field(default_factory=dict)
    selected: bool = True
    evidence: list[Evidence] = Field(min_length=1)


class CandidateRelation(StrictModel):
    id: str = Field(min_length=1, max_length=150)
    source_candidate_id: str
    target_candidate_id: str
    key: str
    properties: dict[str, Any] = Field(default_factory=dict)
    selected: bool = True
    evidence: list[Evidence] = Field(min_length=1)


class ProposalUpdate(RevisionInput):
    expected_changeset_revision: int | None = Field(default=None, ge=1)
    definition: Definition | None = None
    candidates: list[Candidate] | None = Field(default=None, max_length=50000)
    relations: list[CandidateRelation] | None = Field(default=None, max_length=100000)
