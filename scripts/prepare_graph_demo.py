"""Prepare an isolated synthetic graph through the local authenticated API.

Run with .venv/Scripts/python.exe scripts/prepare_graph_demo.py. Re-running
validates/reuses this exact fixture and only creates missing fixture records.
It never edits existing objects, resets credentials, or touches other projects.
An unexpected model/record stops preparation rather than overwriting it.
"""

import json
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "var" / "graph-demo.json"
BASE_URL = "http://127.0.0.1:8000"
NAME = "UI联调-图谱设计验证"
FIXTURE = "ontology-graph-demo-v1"
TITLE = "图谱视觉交互合成模型 v1"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def prop(key, label, required=False):
    return {
        "key": key,
        "label": label,
        "data_type": "string",
        "required": required,
        "enum": [],
        "unit": None,
        "read_roles": [],
        "write_roles": [],
        "source_owned": False,
    }


def relation(key, label, target, cardinality="many"):
    return {"key": key, "label": label, "target_type": target, "cardinality": cardinality}


def definition():
    types = [
        ("Department", "部门", [relation("owns", "承办", "Project")]),
        (
            "Employee",
            "员工",
            [
                relation("member_of", "隶属", "Department", "one"),
                relation("participates", "参与", "Project"),
                relation("leads", "负责", "Project"),
            ],
        ),
        ("Project", "项目", [relation("uses", "使用", "System"), relation("depends_on", "依赖", "Project")]),
        ("System", "系统", [relation("integrates", "集成", "System")]),
    ]
    return {
        "types": [
            {
                "key": key,
                "label": label,
                "description": f"{FIXTURE}：合成{label}，仅供图谱交互验收。",
                "properties": [
                    prop("name", "名称", True),
                    prop("demo_key", "合成标识", True),
                    prop("description", "说明"),
                ],
                "relations": relations,
            }
            for key, label, relations in types
        ],
        "mappings": [],
    }


NODES = [
    ("dept_platform", "Department", "平台部", "合成部门：承办两个项目。"),
    ("dept_research", "Department", "研究部", "合成部门：承办评审项目。"),
    ("employee_lin", "Employee", "林悦", "合成员工：同时参与和负责星图项目。"),
    ("employee_chen", "Employee", "陈远", "合成员工：参与星图项目。"),
    ("employee_wang", "Employee", "王宁", "合成员工：参与模型评审。"),
    ("project_atlas", "Project", "星图", "合成项目：与系统星图同名，身份不同。"),
    ("project_migration", "Project", "数据迁移", "合成项目：项目依赖环的第二个节点。"),
    ("project_review", "Project", "模型评审", "合成项目：项目依赖环的第三个节点。"),
    ("system_atlas", "System", "星图", "合成系统：与项目星图同名，身份不同。"),
    ("system_crm", "System", "客户管理", "合成系统：被数据迁移项目使用。"),
    ("system_identity", "System", "统一身份", "合成系统：被模型评审项目使用。"),
    ("system_isolated", "System", "离线沙箱", "合成孤立节点：故意没有任何关系。"),
]
EDGES = [
    ("employee_lin", "member_of", "dept_platform"),
    ("employee_chen", "member_of", "dept_platform"),
    ("employee_wang", "member_of", "dept_research"),
    ("dept_platform", "owns", "project_atlas"),
    ("dept_platform", "owns", "project_migration"),
    ("dept_research", "owns", "project_review"),
    ("employee_lin", "participates", "project_atlas"),
    ("employee_chen", "participates", "project_atlas"),
    ("employee_wang", "participates", "project_review"),
    ("employee_lin", "leads", "project_atlas"),
    ("project_atlas", "uses", "system_atlas"),
    ("project_migration", "uses", "system_crm"),
    ("project_review", "uses", "system_identity"),
    ("project_atlas", "depends_on", "project_migration"),
    ("project_migration", "depends_on", "project_review"),
    ("project_review", "depends_on", "project_atlas"),
    ("system_atlas", "integrates", "system_identity"),
    ("system_identity", "integrates", "system_crm"),
]


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    # Never print response bodies, request payloads, cookies, or exception objects.
    require(response.is_success, f"API {method} {path} failed (HTTP {response.status_code})")
    return response.json()


def login(client, path):
    data = json.loads(path.read_text(encoding="utf-8"))
    body = {key: data[key] for key in ("tenant_slug", "email", "password")}
    return request(client, "POST", "/api/auth/login", json=body)["user"]


def save_manifest(data):
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(OUTPUT)


def scoped_get(client, path, project_id, **params):
    return request(client, "GET", path, params={"project_id": project_id, **params})


def prepare_project(owner, desired):
    matches = [p for p in request(owner, "GET", "/api/projects")["items"] if p["name"] == NAME]
    require(len(matches) <= 1, "Multiple projects have the reserved fixture name; no project changed")
    if matches:
        project = matches[0]
        current = scoped_get(owner, "/api/ontology/definition", project["id"])
        drafts = scoped_get(owner, "/api/changesets", project["id"])["items"]
        known = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
        owned = known.get("fixture") == FIXTURE and known.get("project_id") == project["id"]
        owned |= current["definition"] == desired or any(
            c["title"] == TITLE and c["definition"] == desired for c in drafts
        )
        require(owned, "Same-name project is not this fixture; no project changed")
    else:
        project = request(owner, "POST", "/api/projects", json={"name": NAME})
        save_manifest(
            {
                "fixture": FIXTURE,
                "project_id": project["id"],
                "project_name": NAME,
                "status": "preparing",
                "ui_verified": False,
            }
        )
    return project


def publish_model(owner, reviewer, reviewer_user, project_id, desired):
    current = scoped_get(owner, "/api/ontology/definition", project_id)
    if current["release_id"]:
        require(current["definition"] == desired, "Existing published fixture model changed; no overwrite")
        return current
    require(
        current["definition"] in ({"types": []}, {"types": [], "mappings": []}),
        "Unexpected unpublished model; no overwrite",
    )
    changes = scoped_get(owner, "/api/changesets", project_id)["items"]
    require(
        all(c["title"] == TITLE and c["definition"] == desired for c in changes),
        "Unrelated draft exists in reserved fixture project; no overwrite",
    )
    require(len(changes) <= 1, "Multiple fixture drafts exist; reconcile before continuing")
    members = scoped_get(owner, "/api/members", project_id)["items"]
    member = next((m for m in members if m["user_id"] == reviewer_user["id"]), None)
    if member:
        require(member["role"] in {"owner", "reviewer"}, "Existing reviewer role differs; no role overwrite")
    else:
        request(
            owner,
            "POST",
            "/api/members",
            json={
                "project_id": project_id,
                "user_id": reviewer_user["id"],
                "role": "reviewer",
            },
        )
    change = (
        changes[0]
        if changes
        else request(
            owner,
            "POST",
            "/api/changesets",
            json={
                "project_id": project_id,
                "title": TITLE,
                "definition": desired,
                "expected_baseline_release_id": None,
            },
        )
    )
    transitions = {
        "draft": ("validate", owner),
        "validated": ("approve", reviewer),
        "approved": ("publish", owner),
    }
    for _ in range(3):
        if change["status"] == "published":
            break
        require(change["status"] in transitions, "Unknown draft state; no transition guessed")
        operation, actor = transitions[change["status"]]
        change = request(
            actor,
            "POST",
            f"/api/changesets/{change['id']}/{operation}",
            json={"expected_revision": change["revision"]},
        )
        require(change.get("validation", {}).get("valid"), "Model validation failed; preparation stopped")
    require(change["status"] == "published", "Model publication did not complete")
    require(change["author_id"] != change["approved_by"], "Expected independent reviewer was not recorded")
    current = scoped_get(owner, "/api/ontology/definition", project_id)
    require(
        current["definition"] == desired and current["release_id"] == change["release_id"],
        "Published model does not match fixture",
    )
    return current


def prepare_records(owner, project_id):
    desired = {
        key: {"type_key": kind, "properties": {"demo_key": key, "name": name, "description": description}}
        for key, kind, name, description in NODES
    }
    response = scoped_get(owner, "/api/objects", project_id, limit=200)
    require(response["total"] == len(response["items"]), "Unexpected object inventory size")
    objects = {}
    for row in response["items"]:
        key = row["properties"].get("demo_key")
        require(key in desired and key not in objects, "Unexpected or duplicate fixture object; no overwrite")
        require(
            row["type_key"] == desired[key]["type_key"]
            and row["properties"] == desired[key]["properties"]
            and not row["evidence"],
            "Existing fixture object changed; no overwrite",
        )
        objects[key] = row
    # Check all existing relations before any new object/relationship mutation.
    existing = scoped_get(owner, "/api/relations", project_id, limit=1000)
    require(existing["total"] == len(existing["items"]), "Unexpected relation inventory size")
    reverse = {row["id"]: key for key, row in objects.items()}
    edges = {}
    for row in existing["items"]:
        signature = (reverse.get(row["source_id"]), row["type_key"], reverse.get(row["target_id"]))
        require(
            signature in EDGES and signature not in edges and not row["properties"] and not row["evidence"],
            "Unexpected or changed fixture relation; no overwrite",
        )
        edges[signature] = row
    for key, payload in desired.items():
        if key not in objects:
            objects[key] = request(owner, "POST", "/api/objects", json={"project_id": project_id, **payload})
    for source, predicate, target in EDGES:
        signature = (source, predicate, target)
        if signature not in edges:
            edges[signature] = request(
                owner,
                "POST",
                "/api/relations",
                json={
                    "project_id": project_id,
                    "source_id": objects[source]["id"],
                    "type_key": predicate,
                    "target_id": objects[target]["id"],
                },
            )
    return objects, edges


def verify_graph(owner, project_id, model, objects, edges):
    graph = scoped_get(owner, "/api/graph", project_id, kind="instances", limit=200)
    schema = scoped_get(owner, "/api/graph", project_id, kind="model", limit=200)
    require(not graph["truncated"] and not schema["truncated"], "Fixture graph unexpectedly truncated")
    require(len(graph["nodes"]) == 12 and len(graph["edges"]) == 18, "Unexpected instance graph counts")
    require(len(schema["nodes"]) == 4 and len(schema["edges"]) == 7, "Unexpected model graph counts")
    require({n["id"] for n in graph["nodes"]} == {o["id"] for o in objects.values()}, "Graph node IDs differ")
    require({e["id"] for e in graph["edges"]} == {e["id"] for e in edges.values()}, "Graph edge IDs differ")
    types = {t["key"]: t for t in definition()["types"]}
    require({n["id"] for n in schema["nodes"]} == set(types), "Model node IDs differ")
    expected_model_edges = {
        (f"{t['key']}.{r['key']}", t["key"], r["target_type"], r["label"])
        for t in types.values()
        for r in t["relations"]
    }
    require(
        {(e["id"], e["source"], e["target"], e["label"]) for e in schema["edges"]} == expected_model_edges,
        "Named model relations differ",
    )
    reverse = {obj["id"]: obj for obj in objects.values()}
    expected_edges = {(objects[s]["id"], p, objects[t]["id"]) for s, p, t in EDGES}
    require(
        {(e["source"], e["type_key"], e["target"]) for e in graph["edges"]} == expected_edges,
        "Directed relation topology differs",
    )
    for edge in graph["edges"]:
        relations = types[reverse[edge["source"]]["type_key"]]["relations"]
        rel = next(r for r in relations if r["key"] == edge["type_key"])
        # The currently running older API uses relation keys as instance labels.
        # Record this explicitly; once release metadata is present require the
        # updated contract. The persisted model still has verified named edges.
        permitted_labels = {rel["label"]} if "release_id" in graph else {rel["label"], rel["key"]}
        require(edge["label"] in permitted_labels, "Relationship label differs")
        if "cardinality" in edge:
            require(edge["cardinality"] == rel["cardinality"], "Relationship cardinality differs")
    for node in graph["nodes"]:
        require(node["label"] == reverse[node["id"]]["properties"]["name"], "Node label differs")
        if "type_label" in node:
            require(node["type_label"] == types[node["type_key"]]["label"], "Node type label differs")
    for result in (graph, schema):
        if "release_id" in result:
            require(
                result["release_id"] == model["release_id"] and result["version"] == model["version"],
                "Graph release differs from published model",
            )
    incidence = Counter(endpoint for edge in graph["edges"] for endpoint in (edge["source"], edge["target"]))
    require(incidence[objects["system_isolated"]["id"]] == 0, "Expected isolated node has a relation")
    return graph, schema


def main():
    # These are existing synthetic development users; no account/password changes.
    with (
        httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False) as owner,
        httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False) as reviewer,
    ):
        owner_user = login(owner, ROOT / "var" / "development-login.json")
        reviewer_user = login(reviewer, ROOT / "var" / "development-reviewer-login.json")
        require(owner_user["id"] != reviewer_user["id"], "Reviewer must be a separate existing user")
        desired = definition()
        project = prepare_project(owner, desired)
        model = publish_model(owner, reviewer, reviewer_user, project["id"], desired)
        objects, edges = prepare_records(owner, project["id"])
        graph, schema = verify_graph(owner, project["id"], model, objects, edges)
        changes = scoped_get(owner, "/api/changesets", project["id"])["items"]
        published = next(c for c in changes if c.get("release_id") == model["release_id"])
        require(
            published["status"] == "published" and published["validation"]["valid"],
            "Publication audit missing",
        )
        require(published["author_id"] != published["approved_by"], "Independent approval audit missing")
        manifest = {
            "fixture": FIXTURE,
            "synthetic_only": True,
            "status": "api_verified",
            "ui_verified": False,
            "project_id": project["id"],
            "project_name": NAME,
            "release_id": model["release_id"],
            "model_version": model["version"],
            "changeset_id": published["id"],
            "model_node_ids": [n["id"] for n in schema["nodes"]],
            "model_edge_ids": [e["id"] for e in schema["edges"]],
            "nodes": [
                {
                    "demo_key": key,
                    "id": row["id"],
                    "type_key": row["type_key"],
                    "name": row["properties"]["name"],
                }
                for key, row in objects.items()
            ],
            "edges": [
                {
                    "id": row["id"],
                    "source_key": key[0],
                    "type_key": key[1],
                    "target_key": key[2],
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                }
                for key, row in edges.items()
            ],
            "counts": {"types": 4, "model_relations": 7, "nodes": 12, "edges": 18},
            "cases": {
                "isolated_node_id": objects["system_isolated"]["id"],
                "cycle_node_ids": [
                    objects[k]["id"] for k in ("project_atlas", "project_migration", "project_review")
                ],
                "parallel_edge_ids": [
                    edges[("employee_lin", p, "project_atlas")]["id"] for p in ("participates", "leads")
                ],
                "same_label_node_ids": [objects[k]["id"] for k in ("project_atlas", "system_atlas")],
            },
            "graph_metadata_observed": {
                "release": "release_id" in graph,
                "type_label": all("type_label" in n for n in graph["nodes"]),
                "cardinality": all("cardinality" in e for e in graph["edges"]),
                "named_instance_labels": all(e["label"] != e["type_key"] for e in graph["edges"]),
            },
        }
        save_manifest(manifest)
        request(reviewer, "POST", "/api/auth/logout")
        request(owner, "POST", "/api/auth/logout")
    print(
        json.dumps(
            {
                "project_id": project["id"],
                "counts": manifest["counts"],
                "manifest": str(OUTPUT),
                "ui_verified": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, httpx.HTTPError, OSError, ValueError, KeyError) as error:
        # RuntimeError messages above are deliberately credential-free.
        print(
            str(error) if isinstance(error, RuntimeError) else f"Preparation stopped: {type(error).__name__}"
        )
        raise SystemExit(1) from None
