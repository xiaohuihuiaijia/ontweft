"""Native cloud snapshots are versioned structures, not exported Word files."""

from ontology.documents import parse_document
from ontology.documents.feishu_native import SNAPSHOT_VERSION, snapshot_bytes


def text_block(key, parent, text, kind=2):
    field = {1: "page", 2: "text", 3: "heading1", 12: "bullet", 14: "code"}.get(kind, "text")
    return {
        "block_id": key,
        "block_type": kind,
        "parent_id": parent,
        field: {"elements": [{"text_run": {"content": text}}]},
    }


def native_fixture(*, resources=False):
    root = text_block("root", "", "设备资料", 1)
    root["children"] = ["heading", "paragraph", "table", "after"]
    table = {
        "block_id": "table",
        "block_type": 31,
        "parent_id": "root",
        "children": ["c00", "c01", "c10", "c11"],
        "table": {
            "cells": ["c00", "c01", "c10", "c11"],
            "property": {
                "row_size": 2,
                "column_size": 2,
                "header_row": True,
                "merge_info": [
                    {"row_span": 2, "col_span": 1},
                    {"row_span": 1, "col_span": 1},
                    {"row_span": 0, "col_span": 0},
                    {"row_span": 1, "col_span": 1},
                ],
            },
        },
    }
    cells = [
        {
            "block_id": key,
            "parent_id": "table",
            "block_type": 32,
            "table_cell": {},
            "children": [] if key == "c10" else ["t" + key],
        }
        for key in table["children"]
    ]
    blocks = [
        root,
        text_block("heading", "root", "检修安排", 3),
        text_block("paragraph", "root", "阀门需要定期维护。"),
        table,
        *cells,
        text_block("tc00", "c00", "阀门"),
        text_block("tc01", "c01", "季度检查"),
        text_block("tc11", "c11", "年度更换"),
        text_block("after", "root", "结束说明"),
    ]
    if resources:
        root["children"].extend(["picture", "attachment", "future"])
        blocks += [
            {
                "block_id": "picture",
                "parent_id": "root",
                "block_type": 27,
                "image": {"token": "image-private", "width": 100},
            },
            {
                "block_id": "attachment",
                "parent_id": "root",
                "block_type": 23,
                "file": {"token": "file-private", "name": "资料.pdf"},
            },
            {
                "block_id": "future",
                "parent_id": "root",
                "block_type": 999,
                "future_type": {"new_field": "不可丢弃"},
            },
        ]
    # Traversal must use declared children, not pagination/array order.
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "document_id": "remote-doc",
        "revision_id": 7,
        "title": "设备资料",
        "blocks": list(reversed(blocks)),
    }


def parsed(value=None, **kwargs):
    data = snapshot_bytes(value or native_fixture())
    return data, parse_document(
        data, "设备.feishu-docx.json", source_id="platform-doc", source_revision="1", **kwargs
    )
