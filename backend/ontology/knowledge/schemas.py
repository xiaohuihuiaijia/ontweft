from pydantic import Field

from ontology.core.schemas import RevisionInput, StrictModel


class KnowledgeInput(StrictModel):
    project_id: str
    title: str = Field(min_length=1, max_length=250)
    body: str = Field(default="", max_length=200000)
    references: list[dict] = Field(default_factory=list, max_length=500)


class KnowledgeUpdate(RevisionInput):
    title: str = Field(min_length=1, max_length=250)
    body: str = Field(max_length=200000)
    references: list[dict] = Field(default_factory=list, max_length=500)
