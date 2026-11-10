"""Re-resolve selected Wiki locations and check current import prerequisites.

This is a live preview only. It does not write documents, authorize a future job,
claim complete-space enumeration or turn metadata visibility into content access.
"""

import asyncio
import time
from dataclasses import asdict

from ontology.core.errors import DomainError
from ontology.integrations.feishu.gateway import FeishuGatewayError, FeishuUserGateway
from ontology.integrations.feishu.service import remote_error


async def _gather(coroutines):
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def preview_selection(service, project_id, application_id, selections, expected_revision):
    context, region = await service.context(project_id, application_id)
    if context.credential_revision != expected_revision:
        raise DomainError(409, "feishu_selection_stale", "员工授权已改变，请重新加载目录后选择")
    gateway = FeishuUserGateway(context, service=region)
    slots = asyncio.Semaphore(4)

    async def resolve(selection):
        async with slots:
            try:
                node = await gateway.get_wiki_node(selection.node_token)
                if node.space_id != selection.space_id:
                    return {"node_token": selection.node_token, "status": "location_changed"}
                return {"node_token": selection.node_token, "node": node}
            except FeishuGatewayError as exc:
                if exc.code in {"credential_invalid", "credential_expired"}:
                    raise
                # No upstream body/title is copied to an error row.
                return {
                    "node_token": selection.node_token,
                    "status": "denied" if exc.code == "permission_denied" else "unknown",
                    "error_code": exc.code,
                }

    async def inspect_resource(resource_type, resource_token):
        async with slots:
            try:
                view = await gateway.check_permission(resource_type, resource_token, action="view")
                if not view.allowed:
                    from ontology.content_access.service import invalidate_observed_resource

                    invalidate_observed_resource(
                        service, application_id, resource_type, resource_token, denied=True
                    )
                    return {"status": "denied", "permissions": {"view": False}}
                copy = await gateway.check_permission(resource_type, resource_token, action="copy")
                permissions = {"view": True, "copy": copy.allowed}
                if not copy.allowed:
                    return {"status": "denied", "permissions": permissions}
                if resource_type != "docx":
                    return {
                        "status": "format_pending",
                        "permissions": permissions,
                        "resource_type": resource_type,
                    }
                doc = await gateway.get_document(resource_token)
                return {
                    "status": "eligible",
                    "permissions": permissions,
                    "resource_type": resource_type,
                    "resource_token": resource_token,
                    "title": doc.title,
                    "revision_id": doc.revision_id,
                }
            except FeishuGatewayError as exc:
                if exc.code in {"credential_invalid", "credential_expired"}:
                    raise
                from ontology.content_access.service import invalidate_observed_resource

                invalidate_observed_resource(service, application_id, resource_type, resource_token)
                return {
                    "status": "denied" if exc.code == "permission_denied" else "unknown",
                    "error_code": exc.code,
                }

    try:
        async with asyncio.timeout(45):
            locations = await _gather(resolve(item) for item in selections)
            # Canonical resource identity, not title or Wiki location, deduplicates shortcuts.
            keys = sorted(
                {
                    (item["node"].resource_type, item["node"].resource_token)
                    for item in locations
                    if "node" in item
                }
            )
            resources = dict(zip(keys, await _gather(inspect_resource(*key) for key in keys), strict=True))
    except TimeoutError:
        raise DomainError(504, "feishu_preview_timeout", "导入条件检查超时，请减少选择后重试") from None
    except FeishuGatewayError as exc:
        service._fail(application_id, context.credential_revision)
        remote_error(exc)
    with service.db.transaction(service.user.tenant_id) as session:
        service._current(session, project_id, application_id, context.credential_revision)
        if context.expires_at.timestamp() <= time.time():
            raise DomainError(409, "feishu_reauthorization_required", "员工授权已过期，请重新连接")
    result = []
    for item in locations:
        node = item.get("node")
        if node:
            outcome = resources[(node.resource_type, node.resource_token)]
            item = {"node_token": node.node_token, "space_id": node.space_id, **outcome}
            if outcome["status"] == "eligible":
                item.update(node_type=node.node_type, has_child=node.has_child)
        result.append(item)
    return {
        "items": result,
        "account_revision": context.credential_revision,
        "binding": asdict(context.reference()),
        "checked_at": time.time(),
        "selected_nodes": len(selections),
        "eligible_unique_resources": sum(outcome["status"] == "eligible" for outcome in resources.values()),
        "scope": "selected_nodes_only",
        "documents_written": 0,
    }
