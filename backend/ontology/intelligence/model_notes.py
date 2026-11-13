"""Ground model explanations in the selected material without creating another ontology."""

from ontology.modeling.diff import normalized_type
from ontology.sources.ingestion import fingerprint


def target_definition(definition, note):
    type_def = next((item for item in definition.get("types", []) if item["key"] == note["type_key"]), None)
    if not type_def:
        return None
    type_def = normalized_type(type_def)
    if note["target_kind"] == "type":
        return type_def
    collection = "properties" if note["target_kind"] == "property" else "relations"
    return next((item for item in type_def.get(collection, []) if item["key"] == note["key"]), None)


def normalize_model_notes(raw_notes, materials, pages, baseline, definition):
    """Only code can resolve model-supplied material ids into real source locators."""
    material_by_id = {item["material_id"]: item for item in materials}
    pages_by_id = {page.id: page for page in pages}
    notes, gaps = [], []
    for raw in raw_notes:
        note = {key: raw[key] for key in ("target_kind", "type_key", "key", "question", "reason")}
        target = target_definition(definition, note)
        if target is None:
            gaps.append("建模说明引用了不存在的概念，未保留该说明")
            continue
        previous = target_definition(baseline, note)
        note["change"] = "added" if previous is None else "reused" if previous == target else "modified"
        evidence = []
        for ref in raw.get("references", []):
            material = material_by_id.get(ref["material_id"])
            if material is None:
                continue
            if "database" in material:
                source = material["database"]
                available = {field["name"] for field in source["resource"]["fields"]}
                fields = list(dict.fromkeys(ref.get("fields", [])))
                if not fields or not set(fields) <= available:
                    continue
                resolved = {"source_id": source["source_id"], "resource_id": source["resource"]["resource_id"], "fields": fields}
            else:
                page = pages_by_id[material["page_id"]]
                quote = ref.get("quote", "").strip()
                if not quote or "".join(quote.split()) not in "".join(material["text"].split()):
                    continue
                resolved = {"document_id": page.document_id, "page": page.page_number, "quote": quote,
                            "parse_id": page.id}
                if page.result.get("native_parse_id"):
                    resolved["native_parse_id"] = page.result["native_parse_id"]
            if resolved not in evidence:
                evidence.append(resolved)
        note["evidence"] = evidence
        note["needs_review"] = not evidence
        note["definition_fingerprint"] = fingerprint(target)
        if not evidence:
            gaps.append(f"{target.get('label', note['type_key'])} 的建模说明缺少可核对依据，请结合材料审阅")
        notes.append(note)
    return notes, gaps


def merge_notes(previous, incoming):
    result = list(previous)
    for note in incoming:
        matching = next((item for item in result if all(
            item.get(key) == note.get(key)
            for key in ("target_kind", "type_key", "key", "question", "reason")
        )), None)
        if matching is None:
            result.append(note)
        else:
            matching["evidence"] = matching["evidence"] + [ref for ref in note["evidence"] if ref not in matching["evidence"]]
            matching["needs_review"] = not matching["evidence"]
    return result


def notes_view(notes, definition):
    return [{**note, "stale": note.get("definition_fingerprint") != fingerprint(target_definition(definition, note))}
            for note in notes]
