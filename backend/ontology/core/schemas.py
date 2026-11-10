from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RevisionInput(StrictModel):
    expected_revision: int = Field(ge=1)
