"""Collect one fixed Docx revision through the official employee SDK gateway.

The caller must revalidate its persisted employee binding/task through guard.
This module does not persist, share, refresh credentials, or authorize a job.
"""

import asyncio

from ontology.documents.feishu_native import SNAPSHOT_VERSION, load_snapshot, snapshot_bytes
from ontology.documents.models import DocumentError, ParseLimits
from ontology.integrations.feishu.gateway import FeishuGatewayError


async def collect_docx(gateway, document_id, *, guard, limits=None, max_pages=200, timeout_seconds=120):
    limits = limits or ParseLimits()
    if (
        type(max_pages) is not int
        or not 1 <= max_pages <= 200
        or type(timeout_seconds) not in {int, float}
        or not 0 < timeout_seconds <= 120
    ):
        raise ValueError("Invalid snapshot collection budget")
    reference = gateway.context.reference()

    async def checked(call):
        await guard()
        gateway.context.validate()
        result = await call()
        await guard()
        gateway.context.validate()
        if result.binding != reference:
            raise FeishuGatewayError("invalid_context")
        return result

    async def permission():
        for action in ("view", "copy"):
            result = await checked(
                lambda action=action: gateway.check_permission("docx", document_id, action=action)
            )
            if not result.allowed:
                raise FeishuGatewayError("permission_denied")

    try:
        async with asyncio.timeout(timeout_seconds):
            await permission()
            initial = await checked(lambda: gateway.get_document(document_id))
            blocks, ids, cursors, cursor, size = [], set(), set(), None, 0
            for _ in range(max_pages):
                page = await checked(
                    lambda cursor=cursor: gateway.list_document_blocks(
                        document_id, revision_id=initial.revision_id, page_token=cursor
                    )
                )
                if page.document_revision_id != initial.revision_id:
                    raise FeishuGatewayError("invalid_response")
                for block in page.items:
                    key = block["block_id"]
                    if key in ids:
                        raise DocumentError("invalid_feishu_snapshot", "分页结果包含重复块，未保存部分快照")
                    ids.add(key)
                    size += len(snapshot_bytes(block, limits))
                    if size > limits.max_bytes or len(ids) > limits.max_elements:
                        raise DocumentError("resource_limit", "飞书快照超过采集预算")
                    blocks.append(block)
                if not page.has_more:
                    break
                cursor = page.next_page_token
                if not cursor or cursor in cursors:
                    raise DocumentError("invalid_feishu_snapshot", "分页游标重复或缺失，未保存部分快照")
                cursors.add(cursor)
            else:
                raise DocumentError("resource_limit", "飞书快照超过分页预算")
            final = await checked(lambda: gateway.get_document(document_id))
            if (final.document_id, final.revision_id, final.title) != (
                initial.document_id,
                initial.revision_id,
                initial.title,
            ):
                raise DocumentError(
                    "feishu_revision_changed", "采集期间文档已变化，请重新采集；未混用不同版本"
                )
            await permission()
            data = snapshot_bytes(
                {
                    "snapshot_version": SNAPSHOT_VERSION,
                    "document_id": document_id,
                    "revision_id": initial.revision_id,
                    "title": initial.title,
                    "blocks": blocks,
                },
                limits,
            )
            load_snapshot(data, limits)  # Reject truncated trees before returning any usable artifact.
            await guard()
            gateway.context.validate()
            return data
    except TimeoutError:
        raise DocumentError("feishu_snapshot_timeout", "飞书文档采集超时，未保存部分快照") from None
