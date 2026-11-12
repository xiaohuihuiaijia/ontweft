"""The small public contract shared by model settings and inference jobs."""

from typing import Literal

from pydantic import Field, SecretStr

from ontology.core.schemas import StrictModel

ModelRole = Literal["extraction", "synthesis"]


class ModelSpec(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=200)
    text: bool = True
    vision: bool = True
    json_schema: bool = True
    context_window: int = Field(default=128000, ge=1024, le=10000000)
    max_output_tokens: int = Field(default=8192, ge=128, le=262144)
    parameters: dict = Field(default_factory=dict)
    input_price_per_million: float | None = Field(default=None, ge=0, le=1000000)
    output_price_per_million: float | None = Field(default=None, ge=0, le=1000000)


def default_models():
    return [
        ModelSpec(
            id="qwen3.8-flash", label="常规抽取", context_window=1000000,
            parameters={"enable_thinking": False},
            input_price_per_million=0.8, output_price_per_million=2.7,
        ),
        ModelSpec(
            id="qwen3.8-max", label="复杂理解与本体综合", context_window=1000000,
            parameters={"enable_thinking": True, "thinking_budget": 4096},
            input_price_per_million=12, output_price_per_million=36,
        ),
    ]


class ConnectionInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = None
    enabled: bool = True
    allow_private_network: bool = False
    models: list[ModelSpec] = Field(default_factory=default_models, min_length=1, max_length=30)
    roles: dict[ModelRole, str] = Field(
        default_factory=lambda: {"extraction": "qwen3.8-flash", "synthesis": "qwen3.8-max"}
    )


class ConnectionUpdate(ConnectionInput):
    expected_revision: int = Field(ge=1)
    clear_credentials: bool = False


class ConnectionTest(StrictModel):
    expected_revision: int = Field(ge=1)
    role: ModelRole = "extraction"
