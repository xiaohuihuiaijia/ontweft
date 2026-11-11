"""Synthetic deterministic model answers; these helpers never call a provider."""

import copy
import io
import json
from typing import ClassVar

from ontology.intelligence.client import GenerationResult, ModelClient
from ontology.intelligence.prompts import FACT_SCHEMA, MODEL_SCHEMA, PAGE_SCHEMA


def blank_pdf(pages=3):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def product_definition():
    return {
        "types": [
            {
                "key": "Product",
                "label": "产品",
                "description": "合成测试产品",
                "properties": [
                    {
                        "key": key,
                        "label": key,
                        "data_type": kind,
                        "required": False,
                        "enum": [],
                        "unit": None,
                        "source_owned": True,
                    }
                    for key, kind in (("code", "string"), ("rating", "number"))
                ],
                "unique_keys": [["code"]],
                "relations": [
                    {"key": "parent", "label": "所属", "target_type": "Product", "cardinality": "one"}
                ],
            }
        ],
        "mappings": [],
    }


class SyntheticModel:
    texts: ClassVar = {
        1: "Product A has rating 12.50.",
        2: "Product B belongs to Product A.",
        3: "Product C has rating 24.",
    }

    def __init__(self, monkeypatch):
        self.calls = []
        self.after_reserve = None
        self.before_reserve = None
        self.cost = 0.01

        def generate(_client, role, **kwargs):
            return self.generate(role, **kwargs)

        monkeypatch.setattr(ModelClient, "generate", generate)

    def generate(self, role, *, schema, text, before_request=None, **_kwargs):
        kind = "page" if schema == PAGE_SCHEMA else "model" if schema == MODEL_SCHEMA else "facts"
        assert schema in (PAGE_SCHEMA, MODEL_SCHEMA, FACT_SCHEMA)
        payload = json.loads(text)
        if self.before_reserve:
            self.before_reserve(kind, payload)
        if before_request:
            before_request({"estimated_cost": self.cost})
        self.calls.append((kind, role, payload))
        if self.after_reserve:
            self.after_reserve(kind, payload)
        if kind == "page":
            data = {
                "text": self.texts[payload["page"]],
                "title": "Synthetic page",
                "issues": [],
                "needs_review": False,
                "continued_table": False,
            }
        elif kind == "model":
            data = {**copy.deepcopy(product_definition()), "gaps": []}
        else:
            number = payload["page"]

            def candidate(code, quote, rating=None):
                values = [{"key": "code", "value_json": json.dumps(code)}]
                if rating is not None and payload["goal"] != "只提取型号":
                    values.append({"key": "rating", "value_json": rating})
                return {
                    "id": code,
                    "type_key": "Product",
                    "values": values,
                    "business_key_fields": ["code"],
                    "quotes": [quote],
                }

            if number == 2:
                data = {
                    "candidates": [candidate("B", self.texts[2]), candidate("A", self.texts[1])],
                    "relations": [
                        {"source_id": "B", "target_id": "A", "key": "parent", "quotes": [self.texts[2]]}
                    ],
                    "gaps": [],
                }
            else:
                data = {
                    "candidates": [
                        candidate(
                            "A" if number == 1 else "C", self.texts[number], "12.50" if number == 1 else "24"
                        )
                    ],
                    "relations": [],
                    "gaps": [],
                }
        return GenerationResult(
            data, "synthetic-model", {"prompt_tokens": 10, "completion_tokens": 20}, cost=self.cost
        )


def create_case(platform, *, pages=3):
    client, app, ids, _ = platform
    result = client.post(
        "/api/model-connections",
        json={"name": "Synthetic compatible service", "base_url": "https://example.test/v1"},
    )
    assert result.status_code == 201, result.text
    connection = result.json()
    result = client.post(
        "/api/documents/upload",
        data={"project_id": ids["project_id"]},
        files={"file": ("synthetic.pdf", blank_pdf(pages), "application/pdf")},
    )
    assert result.status_code == 201, result.text
    document = result.json()["document"]
    # The workflow under test reads source pages itself; cancel the unrelated native parse job.
    jobs = client.get("/api/jobs", params={"project_id": ids["project_id"]}).json()["items"]
    for job in jobs:
        if job["kind"] == "document.parse":
            assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    return {"client": client, "app": app, "ids": ids, "connection": connection, "document": document}


def create_proposal(case, **updates):
    result = case["client"].post(
        "/api/ai/proposals",
        json={
            "project_id": case["ids"]["project_id"],
            "connection_id": case["connection"]["id"],
            "document_ids": [case["document"]["id"]],
            "instructions": "产品与明确的所属关系",
            **updates,
        },
    )
    assert result.status_code == 202, result.text
    return result.json()


def run_worker(case, proposal):
    from ontology.worker import process_one

    assert process_one(case["app"].state.db, case["app"].state.settings, worker_id="synthetic-ai-worker")
    response = case["client"].get(f"/api/ai/proposals/{proposal['id']}")
    assert response.status_code == 200, response.text
    return response.json()
