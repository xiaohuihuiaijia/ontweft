from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ontology.core.schemas import StrictModel

Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=64,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    ),
]


class LoginInput(StrictModel):
    tenant_slug: str | None = Field(default=None, min_length=1, max_length=80)
    username: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def one_identifier(self):
        if (self.username is None) == (self.email is None):
            raise ValueError("请提供用户名或邮箱中的一项")
        return self


class ProjectInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)


class UserInput(StrictModel):
    username: Username | None = None
    email: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=1024)


class MemberInput(StrictModel):
    project_id: str
    user_id: str
    role: Literal["owner", "modeler", "reviewer", "operator", "member", "auditor"]
