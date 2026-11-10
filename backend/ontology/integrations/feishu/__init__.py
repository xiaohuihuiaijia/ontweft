"""Employee-only Feishu SDK reads; persistent binding and source ACL remain external."""

from ontology.integrations.feishu.gateway import (
    BindingReference,
    DocumentMetadata,
    EmployeeBindingContext,
    FeishuGatewayError,
    FeishuUserGateway,
    SdkPage,
)

__all__ = [
    "BindingReference",
    "DocumentMetadata",
    "EmployeeBindingContext",
    "FeishuGatewayError",
    "FeishuUserGateway",
    "SdkPage",
]
