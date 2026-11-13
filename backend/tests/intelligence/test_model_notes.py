import copy
from types import SimpleNamespace
from typing import ClassVar

import pytest
from ontology.intelligence.model_notes import normalize_model_notes, notes_view
from ontology.intelligence.prompts import FACT_SCHEMA, MODEL_SCHEMA, merge_model
from ontology.modeling.diff import normalized_type
from tests.support.intelligence import (
    SyntheticModel,
    create_case,
    create_proposal,
    product_definition,
    run_worker,
)


def note(reference):
    return {"target_kind": "type", "type_key": "Product", "key": "", "question": "哪些产品符合规格？",
            "reason": "复用产品概念，避免为每份文件建一个类型", "references": [reference]}


def test_notes_resolve_only_selected_material_and_do_not_invent_source_locators():
    page = SimpleNamespace(id="page1", document_id="doc", page_number=2, result={"native_parse_id": "native1"})
    materials = [{"material_id": "m1", "page_id": page.id, "text": "Product A has rating 12.50."}]
    raw = [note({"material_id": "m1", "quote": "Product A has rating 12.50.", "fields": []}),
           note({"material_id": "outside", "quote": "Product A", "fields": []}),
           note({"material_id": "m1", "quote": "Product A fits every connector", "fields": []})]
    definition = product_definition()
    notes, gaps = normalize_model_notes(raw, materials, [page], definition, definition)
    assert notes[0]["change"] == "reused"
    assert notes[0]["evidence"] == [{"document_id": "doc", "page": 2, "quote": "Product A has rating 12.50.",
                                     "parse_id": "page1", "native_parse_id": "native1"}]
    assert all(item["evidence"] == [] and item["needs_review"] for item in notes[1:])
    assert len(gaps) == 2
    assert not notes_view(notes, definition)[0]["stale"]
    changed = copy.deepcopy(definition)
    changed["types"][0]["label"] = "Changed"
    assert notes_view(notes, changed)[0]["stale"]


def test_database_explanations_bind_existing_fields_and_missing_target_is_not_retained():
    materials = [{"material_id": "db1", "database": {"source_id": "source", "resource": {
        "resource_id": "public.products", "fields": [{"name": "sku"}]}}}]
    notes, gaps = normalize_model_notes([
        note({"material_id": "db1", "fields": ["sku"], "quote": ""}),
        note({"material_id": "db1", "fields": ["imagined_column"], "quote": ""}),
        {**note({"material_id": "db1", "fields": ["sku"]}), "type_key": "Missing"},
    ], materials, [], {"types": []}, product_definition())
    assert notes[0]["evidence"] == [{"source_id": "source", "resource_id": "public.products", "fields": ["sku"]}]
    assert notes[0]["change"] == "added" and not notes[0]["needs_review"]
    assert notes[1]["needs_review"] and len(notes) == 2 and len(gaps) == 2


def test_omitted_historical_defaults_do_not_make_reused_concepts_modified_or_stale():
    old = product_definition()
    canonical = {**old, "types": [normalized_type(item) for item in old["types"]]}
    notes, _ = normalize_model_notes([note({"material_id": "absent", "fields": [], "quote": ""})],
                                   [], [], old, canonical)
    assert notes[0]["change"] == "reused"
    assert not notes_view(notes, old)[0]["stale"]


def test_model_delta_can_extend_relation_conditions_without_overwriting_existing_contract():
    old = product_definition()
    old["types"][0]["relations"][0]["properties"] = [
        {"key": "voltage", "label": "电压限制", "data_type": "number", "read_roles": ["owner"], "write_roles": ["owner"]}
    ]
    delta = copy.deepcopy(old)
    relation = delta["types"][0]["relations"][0]
    relation["cardinality"] = "many"
    relation["properties"] = [{"key": "voltage", "label": "Untrusted rename", "data_type": "string"},
                              {"key": "condition", "label": "安装条件", "data_type": "string"}]
    merged = merge_model(old, delta)["types"][0]["relations"][0]
    assert merged["cardinality"] == "one"
    assert merged["properties"][0]["data_type"] == "number"
    assert merged["properties"][0]["read_roles"] == ["owner"]
    assert merged["properties"][1]["key"] == "condition"


def test_worker_persists_grounded_reasons_in_existing_call_and_reuses_page_cache(platform, monkeypatch):
    class ExplainedModel(SyntheticModel):
        def generate(self, role, **kwargs):
            result = super().generate(role, **kwargs)
            if kwargs["schema"] == MODEL_SCHEMA:
                payload = self.calls[-1][2]
                material = payload["materials"][0]
                result.data["model_notes"] = [{**note({"material_id": material["material_id"],
                                                       "quote": material["text"], "fields": []}),
                                               "question": payload["goal"]}]
            return result

    model = ExplainedModel(monkeypatch)
    case = create_case(platform, pages=1)
    first = run_worker(case, create_proposal(case, instructions="按额定值筛选产品"))
    assert first["status"] == "ready", first["job"]
    assert first["model_notes"][0]["question"] == "按额定值筛选产品"
    assert first["model_notes"][0]["evidence"][0]["page"] == 1
    assert not first["model_notes"][0]["stale"]
    second = run_worker(case, create_proposal(case, instructions="只提取型号"))
    assert second["model_notes"][0]["question"] == "只提取型号"
    assert [call[0] for call in model.calls].count("page") == 1
    assert [call[0] for call in model.calls].count("model") == 2
    assert [call[0] for call in model.calls].count("facts") == 2
    # A legacy proposal response with no notes remains usable and is never regenerated just for notes.
    assert notes_view([], product_definition()) == []


@pytest.mark.parametrize(("second_value", "selected"), [("12.500", True), ("24", False)])
def test_cross_page_relation_conditions_compare_values_and_keep_conflicting_evidence(platform, monkeypatch, second_value, selected):
    class RelationModel(SyntheticModel):
        texts: ClassVar = {1: "Product B belongs to Product A at 12.50 V.", 2: "Product B belongs to Product A at a stated voltage."}

        def generate(self, role, **kwargs):
            result = super().generate(role, **kwargs)
            if kwargs["schema"] == MODEL_SCHEMA:
                result.data["types"][0]["relations"][0]["properties"] = [
                    {"key": "voltage", "label": "电压", "data_type": "number"}
                ]
            elif kwargs["schema"] == FACT_SCHEMA:
                page = self.calls[-1][2]["page"]
                result.data = {"candidates": [
                    {"id": code, "type_key": "Product", "values": [{"key": "code", "value_json": f'"{code}"'}],
                     "business_key_fields": ["code"], "quotes": [self.texts[page]]} for code in ("A", "B")
                ], "relations": [{"source_id": "B", "target_id": "A", "key": "parent",
                                  "values": [{"key": "voltage", "value_json": "12.50" if page == 1 else second_value}],
                                  "quotes": [self.texts[page]]}], "gaps": []}
            return result

    RelationModel(monkeypatch)
    case = create_case(platform, pages=2)
    result = run_worker(case, create_proposal(case))
    assert result["status"] == "ready", result["job"]
    assert len(result["relations"]) == 1
    relation = result["relations"][0]
    assert relation["selected"] is selected
    assert len(relation["evidence"]) == 2
    assert bool(result["gaps"]) is not selected
