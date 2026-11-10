from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, SecretStr, model_validator

from ontology.core.schemas import StrictModel
from ontology.identity.security import authenticated
from ontology.integrations.feishu.selection import preview_selection
from ontology.integrations.feishu.service import EmployeeService

router = APIRouter(prefix="/integrations/feishu", tags=["Feishu employee access"])


class ApplicationInput(StrictModel):
    project_id: str
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    service: Literal["feishu", "lark"] = "feishu"
    app_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    external_tenant_key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    app_secret: SecretStr = Field(min_length=1, max_length=8192)
    enabled: bool = True
    expected_revision: int | None = Field(default=None, ge=1)


class ProjectInput(StrictModel):
    project_id: str


class SelectedNode(StrictModel):
    space_id: str = Field(min_length=1, max_length=512)
    node_token: str = Field(min_length=1, max_length=512)


class SelectionInput(ProjectInput):
    selections: list[SelectedNode] = Field(min_length=1, max_length=20)
    expected_account_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def unique_locations(self):
        if len({item.node_token for item in self.selections}) != len(self.selections):
            raise ValueError("Duplicate Wiki locations")
        return self


@router.post("/applications/{application_id}/selection-preview")
async def selection_preview(
    application_id: str, body: SelectionInput, request: Request, user=Depends(authenticated)
):
    return await preview_selection(
        EmployeeService(request, user),
        body.project_id,
        application_id,
        body.selections,
        body.expected_account_revision,
    )


class CallbackInput(StrictModel):
    state: str = Field(min_length=32, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    code: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    error: Literal["access_denied"] | None = None

    @model_validator(mode="after")
    def exactly_one_outcome(self):
        if (self.code is None) == (self.error is None):
            raise ValueError("Exactly one authorization outcome is required")
        return self


@router.get("/applications")
def applications(request: Request, project_id: str, user=Depends(authenticated)):
    return EmployeeService(request, user, permission="objects:read").list_applications(project_id)


@router.post("/applications", status_code=201)
def create_application(body: ApplicationInput, request: Request, user=Depends(authenticated)):
    return EmployeeService(request, user).configure(body.project_id, body)


@router.post("/applications/{application_id}")
def update_application(
    application_id: str, body: ApplicationInput, request: Request, user=Depends(authenticated)
):
    return EmployeeService(request, user).configure(body.project_id, body, application_id)


@router.post("/applications/{application_id}/authorize")
def authorize(application_id: str, body: ProjectInput, request: Request, user=Depends(authenticated)):
    return EmployeeService(request, user, permission="objects:read").start(body.project_id, application_id)


@router.post("/applications/{application_id}/disconnect")
def disconnect(application_id: str, body: ProjectInput, request: Request, user=Depends(authenticated)):
    return EmployeeService(request, user, permission="objects:read").revoke(body.project_id, application_id)


@router.post("/callback")
async def callback(body: CallbackInput, request: Request, user=Depends(authenticated)):
    return await EmployeeService(request, user, permission="objects:read").complete(
        body.state, body.code, body.error is not None
    )


@router.get("/applications/{application_id}/spaces")
async def spaces(
    application_id: str,
    request: Request,
    project_id: str,
    cursor: str | None = Query(None, max_length=20000),
    user=Depends(authenticated),
):
    return await EmployeeService(request, user, permission="objects:read").browse(
        project_id, application_id, cursor=cursor
    )


@router.get("/applications/{application_id}/nodes")
async def nodes(
    application_id: str,
    request: Request,
    project_id: str,
    space_id: str = Query(min_length=1, max_length=512),
    parent_node: str | None = Query(None, min_length=1, max_length=512),
    cursor: str | None = Query(None, max_length=20000),
    user=Depends(authenticated),
):
    return await EmployeeService(request, user, permission="objects:read").browse(
        project_id, application_id, space_id=space_id, parent_node=parent_node, cursor=cursor
    )
