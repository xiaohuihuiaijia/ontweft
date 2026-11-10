"""Business-use boundary for materialized objects; status changes belong to contributions."""

from ontology.core.errors import DomainError


def require_active_entity(entity):
    if entity.data_status != "active":
        raise DomainError(
            409,
            "object_not_active",
            "对象数据存在来源冲突、不完整或已撤回，需先修复贡献并重新物化；不能作为当前业务输入",
            {"data_status": entity.data_status},
        )
