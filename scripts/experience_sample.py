"""Load the one published product sample through the normal authenticated API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import monotonic, sleep

import httpx

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "product-inventory"
STATE_FILE = ".ontweft-product-inventory.json"


def read_sample_state(data_dir: Path) -> dict | None:
    path = data_dir / STATE_FILE
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("sample_id") != "product-inventory" or state.get("version") != 1:
        raise ValueError("样例状态版本不匹配；保留原目录，使用新的体验目录。")
    return state


def sample_is_complete(data_dir: Path) -> bool:
    return (read_sample_state(data_dir) or {}).get("status") == "complete"


def _save(data_dir: Path, state: dict):
    path = data_dir / STATE_FILE
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def _request(client, method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def _one(rows: list[dict], description: str) -> dict | None:
    if len(rows) > 1:
        raise ValueError(f"{description}存在多个匹配项，已停止样例初始化，原数据保留。")
    return rows[0] if rows else None


def _ready_document(client, document_id: str) -> dict:
    deadline = monotonic() + 120
    while True:
        document = _request(client, "GET", f"documents/{document_id}")
        if document["state"] == "ready":
            return document
        if document["state"] == "failed":
            raise RuntimeError("合成资料解析失败。请查看数据中的任务详情；原件和初始化进度已保留。")
        if monotonic() >= deadline:
            raise TimeoutError("等待合成资料解析超时。请确认 worker 正常；下次启动会继续已有进度。")
        sleep(0.25)


def _publish_sample(client, state, sample, data_dir):
    project_id = state["project_id"]
    if state.get("release_id"):
        return
    if state.get("changeset_id"):
        change = _request(client, "GET", f"changesets/{state['changeset_id']}")
    else:
        changes = _request(client, "GET", "changesets", params={"project_id": project_id})["items"]
        change = _one([row for row in changes if row["title"] == sample["title"]], "样例变更集")
        if change is None:
            change = _request(
                client,
                "POST",
                "changesets",
                json={
                    "project_id": project_id,
                    "title": sample["title"],
                    "expected_baseline_release_id": None,
                    "definition": sample["definition"],
                },
            )
        state["changeset_id"] = change["id"]
        _save(data_dir, state)
    for status, action in (("draft", "validate"), ("validated", "approve"), ("approved", "publish")):
        if change["status"] == status:
            change = _request(
                client,
                "POST",
                f"changesets/{change['id']}/{action}",
                json={"expected_revision": change["revision"]},
            )
    if change["status"] != "published":
        raise RuntimeError("样例模型尚未发布。请查看本体校验或任务结果，再用同一目录继续。")
    state["release_id"] = change["release_id"]
    _save(data_dir, state)


def ensure_sample(client: httpx.Client, data_dir: Path) -> dict:
    """Client is authenticated and has an /api/ base URL; never reset completed data.

    Progress is local to this learning workspace. On interrupted requests, normal
    API reads recover already-created rows before another POST. Existing values
    are never patched by the loader, including values edited during a partial run.
    """
    state = read_sample_state(data_dir)
    if state and state["status"] == "complete":
        return state
    sample = json.loads((SAMPLE_DIR / "sample.json").read_text(encoding="utf-8"))
    projects = _request(client, "GET", "projects")["items"]
    matching = [row for row in projects if row["name"] == sample["project_name"]]
    if state is None:
        if matching:
            raise ValueError("已有同名样例项目但没有本机初始化记录；不会接管现有项目，请使用新的体验目录。")
        state = {
            "sample_id": sample["id"],
            "version": sample["version"],
            "status": "pending",
            "object_ids": {},
            "relation_ids": {},
        }
        _save(data_dir, state)
    if not state.get("project_id"):
        project = _one(matching, "样例项目") or _request(
            client, "POST", "projects", json={"name": sample["project_name"]}
        )
        state["project_id"] = project["id"]
        _save(data_dir, state)
    project_id = state["project_id"]
    _publish_sample(client, state, sample, data_dir)

    source = (SAMPLE_DIR / sample["source"]).read_bytes()
    if not state.get("document_id"):
        documents = _request(client, "GET", "documents", params={"project_id": project_id})["items"]
        digest = hashlib.sha256(source).hexdigest()
        document = _one([row for row in documents if row["content_hash"] == digest], "样例原件")
        if document is None:
            document = _request(
                client,
                "POST",
                "documents/upload",
                data={"project_id": project_id},
                files={"file": ("product-inventory-v1.md", source, "text/markdown")},
            )["document"]
        state["document_id"] = document["id"]
        _save(data_dir, state)
    document = _ready_document(client, state["document_id"])
    evidence = [
        {key: document[key] for key in ("revision", "parse_revision", "ir_fingerprint", "content_hash")}
        | {"document_id": document["id"], "parse_id": document["current_parse_id"]}
    ]

    objects = _request(client, "GET", "objects", params={"project_id": project_id, "limit": 200})["items"]
    for item in sample["objects"]:
        if item["id"] in state["object_ids"]:
            continue
        existing = _one(
            [
                row
                for row in objects
                if row["type_key"] == item["type_key"]
                and row["properties"].get("name") == item["properties"]["name"]
            ],
            "样例对象",
        )
        if existing is None:
            existing = _request(
                client,
                "POST",
                "objects",
                json={
                    "project_id": project_id,
                    "expected_model_release_id": state["release_id"],
                    "type_key": item["type_key"],
                    "properties": item["properties"],
                    "evidence": evidence if item.get("evidence") else [],
                },
            )
            objects.append(existing)
        state["object_ids"][item["id"]] = existing["id"]
        _save(data_dir, state)
    relations = _request(client, "GET", "relations", params={"project_id": project_id})["items"]
    for item in sample["relations"]:
        if item["id"] in state["relation_ids"]:
            continue
        source_id, target_id = state["object_ids"][item["source"]], state["object_ids"][item["target"]]
        existing = _one(
            [
                row
                for row in relations
                if row["type_key"] == item["type_key"]
                and row["source_id"] == source_id
                and row["target_id"] == target_id
            ],
            "样例关系",
        )
        if existing is None:
            existing = _request(
                client,
                "POST",
                "relations",
                json={
                    "project_id": project_id,
                    "expected_model_release_id": state["release_id"],
                    "type_key": item["type_key"],
                    "source_id": source_id,
                    "target_id": target_id,
                    "properties": item["properties"],
                    "evidence": evidence if item.get("evidence") else [],
                },
            )
            relations.append(existing)
        state["relation_ids"][item["id"]] = existing["id"]
        _save(data_dir, state)
    state.update(status="complete", objects=len(sample["objects"]), relations=len(sample["relations"]))
    _save(data_dir, state)
    return state
