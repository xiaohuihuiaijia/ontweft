"""Checkpointed inference in the existing worker; no separate scheduler or agent runtime."""

import copy
import json
import time
from contextlib import closing

from sqlalchemy import select

from ontology.core.domain import validate_source_properties, wire_value
from ontology.core.errors import DomainError, conflict
from ontology.documents.history_models import DocumentParseVersion
from ontology.documents.models import ParseLimits
from ontology.documents.pages import file_digest, iter_page_inputs, page_count, page_structure
from ontology.documents.parser import detect_format_path
from ontology.documents.storage_models import Document
from ontology.intelligence.client import ModelCallError, ModelClient
from ontology.intelligence.connections import model_for
from ontology.intelligence.models import ModelConnection
from ontology.intelligence.prompts import (
    FACT_INSTRUCTION,
    FACT_SCHEMA,
    MODEL_INSTRUCTION,
    MODEL_SCHEMA,
    PAGE_INSTRUCTION,
    PAGE_SCHEMA,
    VERSION,
    merge_model,
)
from ontology.intelligence.proposal_models import AIPage, AIProposal
from ontology.jobs.service import check_job_authority, fenced_job, finish
from ontology.sources.ingestion import fingerprint, schema_fingerprint
from ontology.sources.models import Source
from ontology.sources.service import connector_for


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class Run:
    def __init__(self, db, settings, run):
        self.db, self.settings, self.run = db, settings, run
        self.reservations = []
        with db.transaction(run["tenant_id"]) as session:
            job = fenced_job(session, run)
            self.proposal = session.get(AIProposal, job.payload["proposal_id"])
            self.connection = session.get(ModelConnection, self.proposal.connection_id)
            self.verify(session)
        self.client = ModelClient(self.connection, settings)

    def verify(self, session):
        job = fenced_job(session, self.run)
        ctx = check_job_authority(session, job)
        proposal = session.get(AIProposal, job.payload["proposal_id"])
        connection = session.get(ModelConnection, proposal.connection_id)
        if not connection.enabled or connection.revision != proposal.connection_revision:
            conflict("模型连接已改变或停用，旧任务不能继续发送材料")
        if ctx.project.active_release_id != proposal.baseline_release_id:
            conflict("本体基线已改变，请保留本次结果并基于新模型重新生成")
        job.lease_until = time.time() + 360
        return job, proposal

    def checkpoint(self):
        with self.db.transaction(self.run["tenant_id"]) as session:
            job, _ = self.verify(session)
            return copy.deepcopy(job.checkpoint)

    def save(self, **updates):
        with self.db.transaction(self.run["tenant_id"], write=True) as session:
            job, proposal = self.verify(session)
            job.checkpoint = {**job.checkpoint, **updates}
            proposal.status = "running"

    def reserve(self, charge):
        cost = charge["estimated_cost"]
        if cost is None:
            raise DomainError(409, "model_pricing_required", "请配置模型价格以执行预算限制")
        with self.db.transaction(self.run["tenant_id"], write=True) as session:
            job, proposal = self.verify(session)
            cp = copy.deepcopy(job.checkpoint)
            if cp.get("cost_cny", 0) + cp.get("reserved_cny", 0) + cost > proposal.inputs["budget_cny"]:
                raise DomainError(409, "ai_budget_exhausted", "本次预算不足，已保留完成的页面与任务进度")
            cp["reserved_cny"] = cp.get("reserved_cny", 0) + cost
            cp["requests"] = cp.get("requests", 0) + 1
            job.checkpoint = cp
        self.reservations.append(cost)

    def generate(self, role, instruction, text, schema, images=None, limit=12000):
        self.reservations = []

        def received(response):
            self.record_usage(**response, reservation=self.reservations.pop())

        return self.client.generate(
            role, instruction=instruction, text=text, images=images, schema=schema,
            max_output_tokens=limit, before_request=self.reserve, after_response=received,
        )

    def record_usage(self, usage, cost, *, reservation, model=None, elapsed_ms=0):
        with self.db.transaction(self.run["tenant_id"], write=True) as session:
            job, _ = self.verify(session)
            cp = copy.deepcopy(job.checkpoint)
            if cost is not None:
                cp["reserved_cny"] = max(0, cp.get("reserved_cny", 0) - reservation)
                cp["cost_cny"] = cp.get("cost_cny", 0) + cost
            totals = dict(cp.get("usage", {}))
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
            cp["usage"] = totals
            if model:
                cp["last_model"] = model
                models = cp.setdefault("models_used", {})
                measured = models.setdefault(model, {"calls": 0, "elapsed_ms": 0})
                measured["calls"] += 1
                measured["elapsed_ms"] += elapsed_ms
            job.checkpoint = cp


def collect_pages(state):
    cp = state.checkpoint()
    pages = dict(cp.get("pages", {}))
    documents = []
    total = 0
    input_versions = dict(cp.get("page_input_versions", {}))
    limits = ParseLimits(max_bytes=state.settings.max_upload_bytes)
    for selected in state.proposal.inputs["documents"]:
        with state.db.transaction(state.run["tenant_id"]) as session:
            state.verify(session)
            doc = session.get(Document, selected["id"])
            ir = doc.ir
            if "native_parse_id" in selected:
                snapshot = session.get(DocumentParseVersion, selected["native_parse_id"]) if selected["native_parse_id"] else None
                if selected["native_parse_id"] and (
                    not snapshot or snapshot.document_id != doc.id
                    or snapshot.source_revision != doc.revision or snapshot.content_hash != doc.content_hash
                ):
                    conflict("提案绑定的原生解析版本不可用，请重新选择来源")
                ir = snapshot.ir if snapshot else None
        path = state.settings.data_dir / "assets" / doc.storage_key
        if file_digest(path) != selected["hash"]:
            conflict("来源文件内容已改变，不能沿用旧解析证据")
        fmt = detect_format_path(path, doc.filename, limits)
        if fmt not in {"pdf", "png", "jpeg"}:
            ir = page_structure(
                path, doc.filename, source_id=doc.id,
                source_revision=str(doc.revision), ir=ir, limits=limits,
            )
            input_version = fingerprint(ir)
            if doc.id in input_versions and input_versions[doc.id] != input_version:
                conflict("逻辑分页解析已改变，不能混用旧任务的页面进度")
            if doc.id not in input_versions and any(key.startswith(doc.id + ":") for key in pages):
                conflict("旧任务的逻辑分页未绑定解析版本，请保留结果并重新生成")
            input_versions[doc.id] = input_version
        else:
            input_version = None  # Physical pages come directly from immutable source bytes.
        count = page_count(path, doc.filename, source_id=doc.id, source_revision=str(doc.revision), ir=ir, limits=limits)
        chosen = state.proposal.inputs.get("pages", {}).get(doc.id, list(range(1, count + 1)))
        if any(n < 1 or n > count for n in chosen):
            raise DomainError(422, "invalid_page_selection", "页码超出文档范围")
        native_locator = (selected["native_parse_id"] or "native") if "native_parse_id" in selected else None
        documents.append((doc, path, chosen, ir, input_version, native_locator))
        total += len(chosen)
    state.save(stage="reading", pages=pages, page_input_versions=input_versions, pages_total=total, pages_completed=len(pages))
    for doc, path, chosen, ir, input_version, native_locator in documents:
        for number in chosen:
            location = f"{doc.id}:{number}"
            if location in pages:
                continue
            role = "synthesis" if state.proposal.inputs["quality"] == "high" else "extraction"
            key = fingerprint(
                {
                    "document": doc.id,
                    "hash": doc.content_hash,
                    "revision": doc.revision,
                    "page": number,
                    "connection": state.connection.id,
                    "config_revision": state.connection.revision,
                    "quality": state.proposal.inputs["quality"],
                    "prompt": VERSION,
                    **({"native_parse": input_version} if input_version else {}),
                }
            )
            with state.db.transaction(state.run["tenant_id"]) as session:
                state.verify(session)
                cached = session.scalar(
                    select(AIPage).where(
                        AIPage.tenant_id == state.run["tenant_id"],
                        AIPage.project_id == state.run["project_id"],
                        AIPage.cache_key == key,
                    )
                )
            if cached:
                pages[location] = cached.id
            else:
                with closing(
                    iter_page_inputs(
                        path,
                        doc.filename,
                        source_id=doc.id,
                        source_revision=str(doc.revision),
                        ir=ir,
                        page_numbers=[number],
                        include_images=True,
                        verified_hash=doc.content_hash,
                        limits=limits,
                    )
                ) as inputs:
                    page = next(inputs)
                images = page.get("image_data_urls") or (
                    [page["image_data_url"]] if page.get("image_data_url") else []
                )
                page_context = {"page": number, "label": page["label"], "native_text": page["text"]}
                raw = encoded(page_context)
                result = state.generate(role, PAGE_INSTRUCTION, raw, PAGE_SCHEMA, images)
                if role == "extraction" and (result.data["needs_review"] or result.data["continued_table"]):
                    if images:
                        with closing(
                            iter_page_inputs(
                                path,
                                doc.filename,
                                source_id=doc.id,
                                source_revision=str(doc.revision),
                                ir=ir,
                                page_numbers=[number],
                                image_max_edge=3900,
                                include_images=True,
                                detail_images=True,
                                verified_hash=doc.content_hash,
                                limits=limits,
                            )
                        ) as inputs:
                            detailed = next(inputs)
                            images = detailed["image_data_urls"]
                            page_context["image_regions"] = detailed.get("image_regions", [])
                    result = state.generate(
                        "synthesis",
                        PAGE_INSTRUCTION
                        + "\n重点解决已发现问题，不猜测缺失信息。image_regions 按 image_index 标识整页缩略与局部细节图，"
                        "范围为本页左上原点的归一化图块范围；重叠内容只抄录一次，不把图块范围当成单元格证据坐标。",
                        encoded({**page_context, "issues": result.data["issues"]}),
                        PAGE_SCHEMA,
                        images,
                    )
                with state.db.transaction(state.run["tenant_id"], write=True) as session:
                    state.verify(session)
                    cached = session.scalar(
                        select(AIPage).where(
                            AIPage.tenant_id == state.run["tenant_id"],
                            AIPage.project_id == state.run["project_id"],
                            AIPage.cache_key == key,
                        )
                    )
                    if not cached:
                        cached = AIPage(
                            tenant_id=state.run["tenant_id"],
                            project_id=state.run["project_id"],
                            document_id=doc.id,
                            source_revision=doc.revision,
                            page_number=number,
                            cache_key=key,
                            model=result.model,
                            result={**result.data, **({"native_parse_id": native_locator} if native_locator else {})},
                            usage=result.usage,
                        )
                        session.add(cached)
                        session.flush()
                    pages[location] = cached.id
            state.save(pages=pages, pages_completed=len(pages), current_page=number)
    return pages


def materials(state, pages):
    with state.db.transaction(state.run["tenant_id"]) as session:
        state.verify(session)
        records = {
            row.id: row
            for row in session.scalars(
                select(AIPage).where(
                    AIPage.id.in_(list(pages.values())), AIPage.tenant_id == state.run["tenant_id"]
                )
            )
        }
    return [records[id] for id in pages.values()]


def generate_model(state, pages):
    cp = state.checkpoint()
    sources = cp.get("source_profiles")
    if sources is None:
        sources = []
        for selected in state.proposal.inputs["sources"]:
            with state.db.transaction(state.run["tenant_id"]) as session:
                state.verify(session)
                source = session.get(Source, selected["id"])
            connector = connector_for(source, state.settings)
            try:
                for resource in selected["resources"]:
                    sample = connector.sample(resource["resource_id"], limit=12).model_dump(mode="json")
                    sources.append(
                        {"source_id": source.id, "resource": resource, "samples": sample["records"]}
                    )
            finally:
                connector.close()
        state.save(source_profiles=sources)
    # Every page participates, not just a prefix or the summary of a document.
    batches, batch, length = [], [], 0
    for page in pages:
        # Split exceptionally long logical Office pages without dropping the tail.
        text = page.result["text"]
        parts = [text[start : start + 12000] for start in range(0, len(text), 12000)] or [""]
        for part, content in enumerate(parts, 1):
            material = {
                "document_id": page.document_id,
                "page": page.page_number,
                **page.result,
                "text": content,
                "part": part,
                "parts": len(parts),
            }
            size = len(encoded(material).encode())
            if batch and length + size > 24000:
                batches.append(batch)
                batch, length = [], 0
            batch.append(material)
            length += size
    if batch:
        batches.append(batch)
    batches.extend([[{"database": source}] for source in sources])
    definition = cp.get("definition", state.proposal.definition)
    gaps = cp.get("gaps", [])
    mappings = cp.get("suggested_mappings", [])
    state.save(stage="modeling", model_batches_total=len(batches))
    for i, batch in enumerate(batches):
        if i < cp.get("model_batches_completed", 0):
            continue
        result = state.generate(
            "synthesis",
            MODEL_INSTRUCTION,
            encoded(
                {
                    "goal": state.proposal.inputs["instructions"],
                    "current_definition": definition,
                    "materials": batch,
                }
            ),
            MODEL_SCHEMA,
            limit=18000,
        )
        definition = merge_model(definition, result.data)
        gaps = list(dict.fromkeys(gaps + result.data["gaps"]))
        mappings.extend(result.data["mappings"])
        state.save(
            stage="modeling",
            definition=definition,
            gaps=gaps,
            suggested_mappings=mappings,
            model_batches_completed=i + 1,
            model_batches_total=len(batches),
        )
    # Source schema and mapping identifiers are resolved by code, never trusted from model text.
    for suggested in mappings:
        source = next(
            (
                s
                for s in sources
                if s["source_id"] == suggested["source_id"]
                and s["resource"]["resource_id"] == suggested["resource_id"]
            ),
            None,
        )
        if not source:
            gaps.append("模型建议的来源映射无法匹配所选资源，未采纳")
            continue
        key = (
            "ai_"
            + fingerprint([suggested["source_id"], suggested["resource_id"], suggested["type_key"]])[:24]
        )
        if any(
            m["source_id"] == suggested["source_id"]
            and m["resource_id"] == suggested["resource_id"]
            and m["type_key"] == suggested["type_key"]
            for m in definition.get("mappings", [])
        ):
            continue
        fields = {f["name"] for f in source["resource"]["fields"]}
        if any(f["source_field"] not in fields for f in suggested["fields"]):
            gaps.append("模型建议引用不存在的字段，未采纳该映射")
            continue
        definition.setdefault("mappings", []).append(
            {
                "key": key,
                "source_id": suggested["source_id"],
                "resource_id": suggested["resource_id"],
                "type_key": suggested["type_key"],
                "schema_fingerprint": schema_fingerprint(source["resource"]),
                "identity_fields": [],
                "fields": {
                    f["target"]: {"source_field": f["source_field"], "transform": f["transform"]}
                    for f in suggested["fields"]
                },
            }
        )
    state.save(definition=definition, gaps=list(dict.fromkeys(gaps)))
    return definition


def normalize_facts(data, page, definition, context_pages=()):
    types = {t["key"]: t for t in definition["types"]}
    items, relations, gaps, ids = [], [], list(data["gaps"]), {}
    source_texts = [(origin, "".join(origin.result["text"].split())) for origin in (page, *context_pages)]

    def evidence(quotes):
        refs = []
        for quote in dict.fromkeys(quotes):
            if not quote.strip():
                continue
            normalized_quote = "".join(quote.split())
            for origin, text in source_texts:
                if normalized_quote in text:
                    refs.append(
                        {
                            "document_id": origin.document_id,
                            "page": origin.page_number,
                            "quote": quote,
                            "parse_id": origin.id,
                            **({"native_parse_id": origin.result["native_parse_id"]} if origin.result.get("native_parse_id") else {}),
                        }
                    )
                    break
        return refs

    for item in data["candidates"]:
        type_def = types.get(item["type_key"])
        refs = evidence(item["quotes"])
        if not type_def or not refs or item["id"] in ids:
            gaps.append(f"第 {page.page_number} 页有候选缺少有效类型或原文证据，未采纳")
            continue
        try:
            kinds = {prop["key"]: prop["data_type"] for prop in type_def["properties"]}
            values = {
                v["key"]: json.loads(v["value_json"], parse_float=str)
                if kinds.get(v["key"]) == "number"
                else json.loads(v["value_json"])
                for v in item["values"]
            }
            if len(values) != len(item["values"]):
                raise ValueError("duplicate property")
            json.dumps(values, allow_nan=False)
            values = wire_value(validate_source_properties(type_def, values))
        except (ValueError, TypeError, DomainError):
            gaps.append(f"第 {page.page_number} 页有候选属性格式不满足模型，待复核")
            continue
        fields = item["business_key_fields"]
        key = (
            {k: values[k] for k in fields}
            if fields and all(k in values and values[k] not in (None, "") for k in fields)
            else {}
        )
        identity = fingerprint([item["type_key"], key]) if key else fingerprint([page.id, item])
        ids[item["id"]] = identity
        items.append(
            {
                "id": identity,
                "type_key": item["type_key"],
                "properties": values,
                "business_key": key,
                "selected": bool(key),
                "evidence": refs,
            }
        )
        if not key:
            gaps.append(f"第 {page.page_number} 页有候选尚无明确业务键，默认未选择")
    by_identity = {item["id"]: item for item in items}
    for relation in data["relations"]:
        refs = evidence(relation["quotes"])
        if relation["source_id"] not in ids or relation["target_id"] not in ids or not refs:
            gaps.append(f"第 {page.page_number} 页有关系缺少可定位端点或证据，未采纳")
            continue
        source, target = ids[relation["source_id"]], ids[relation["target_id"]]
        source_item, target_item = by_identity[source], by_identity[target]
        declared = next(
            (
                item
                for item in types[source_item["type_key"]].get("relations", [])
                if item["key"] == relation["key"]
            ),
            None,
        )
        if not declared or declared["target_type"] != target_item["type_key"]:
            gaps.append(f"第 {page.page_number} 页有关系不符合本体端点定义，未采纳")
            continue
        relations.append(
            {
                "id": fingerprint([source, target, relation["key"]]),
                "source_candidate_id": source,
                "target_candidate_id": target,
                "key": relation["key"],
                "selected": source_item["selected"] and target_item["selected"],
                "evidence": refs,
            }
        )
    return items, relations, gaps


def fact_segments(text, target=1600):
    """Bound verbose instance JSON without removing the full-page context."""
    if len(text) <= 2200:
        return [text]
    segments, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > target:
            segments.append(current)
            current = ""
        # Very long machine-generated lines still retain their surrounding page
        # in the request; only the extraction focus is split here.
        while len(line) > target:
            segments.append(line[:target])
            line = line[target:]
        current += line
    if current:
        segments.append(current)
    return segments


def fact_ranges(page):
    """Keep legacy root IDs, but persist offsets rather than another copy of page text."""
    ranges, start = [], 0
    for part, focus in enumerate(fact_segments(page.result["text"])):
        end = start + len(focus)
        ranges.append({"id": f"{page.id}:{part}", "start": start, "end": end, "split": False})
        start = end
    return ranges


def bisect_fact_range(segment):
    middle = (segment["start"] + segment["end"]) // 2
    return [
        {"id": segment["id"] + "/0", "start": segment["start"], "end": middle, "split": True},
        {"id": segment["id"] + "/1", "start": middle, "end": segment["end"], "split": True},
    ]


def run_proposal(db, settings, run):
    state = Run(db, settings, run)
    pages = materials(state, collect_pages(state))
    definition = generate_model(state, pages)
    cp = state.checkpoint()
    completed = set(cp.get("facts_completed", []))
    segments_completed = set(cp.get("facts_segments_completed", []))
    ranges_by_page = copy.deepcopy(cp.get("facts_ranges", {}))
    with db.transaction(run["tenant_id"]) as session:
        proposal = session.get(AIProposal, state.proposal.id)
        candidates = {x["id"]: x for x in proposal.candidates}
        relations = {x["id"]: x for x in proposal.relations}
    gaps = cp.get("gaps", [])
    for index, page in enumerate(pages):
        if page.id in completed:
            continue
        previous = pages[index - 1] if index else None
        context = (
            [previous]
            if previous
            and previous.document_id == page.document_id
            and previous.page_number + 1 == page.page_number
            else []
        )
        if page.id not in ranges_by_page:
            ranges_by_page[page.id] = fact_ranges(page)
            state.save(facts_ranges=ranges_by_page)
        segments = ranges_by_page[page.id]
        part = 0
        role = (
            "synthesis"
            if state.proposal.inputs["quality"] == "high" or page.result["needs_review"]
            else "extraction"
        )
        while part < len(segments):
            segment = segments[part]
            segment_id = segment["id"]
            if segment_id in segments_completed:
                part += 1
                continue
            try:
                result = state.generate(
                    role,
                    FACT_INSTRUCTION,
                    encoded(
                        {
                            "goal": state.proposal.inputs["instructions"],
                            "definition": definition,
                            "page": page.page_number,
                            "text": page.result["text"],
                            "focus_text": page.result["text"][segment["start"] : segment["end"]],
                            "focus_start": segment["start"],
                            "focus_end": segment["end"],
                            "part": part + 1,
                            "parts": len(segments),
                            "previous_pages": [
                                {"page": p.page_number, "text": p.result["text"]} for p in context
                            ],
                        }
                    ),
                    FACT_SCHEMA,
                )
            except ModelCallError as exc:
                if exc.code != "model_output_truncated":
                    raise
                if not segment["split"] and segment["end"] - segment["start"] > 1:
                    segments[part : part + 1] = bisect_fact_range(segment)
                    # Save the split before dispatching either child; retries keep completed leaves.
                    state.save(facts_ranges=ranges_by_page)
                    continue
                output_limit = min(12000, model_for(state.connection, role).max_output_tokens)
                blocked = {
                    "page": page.page_number,
                    "parse_id": page.id,
                    "start": segment["start"],
                    "end": segment["end"],
                    "max_output_tokens": output_limit,
                }
                state.save(facts_blocked_range=blocked)
                raise DomainError(
                    422,
                    "model_output_truncated",
                    f"第 {page.page_number} 页字符 {segment['start'] + 1}–{segment['end']} 的事实输出仍被截断"
                    f"（输出上限 {output_limit} Token）。已停止自动拆分并保留完成片段，请调整处理范围后重新起草。",
                    blocked,
                ) from None
            new_items, new_relations, new_gaps = normalize_facts(result.data, page, definition, context)
            for item in new_items:
                previous = candidates.get(item["id"])
                if previous:
                    clashes = [
                        k
                        for k, v in item["properties"].items()
                        if k in previous["properties"] and previous["properties"][k] != v
                    ]
                    if clashes:
                        previous["selected"] = False
                        gaps.append("同一业务键存在不同属性值，已暂停该候选：" + ", ".join(clashes))
                    else:
                        previous["properties"].update(item["properties"])
                    previous["evidence"].extend(
                        ref for ref in item["evidence"] if ref not in previous["evidence"]
                    )
                else:
                    candidates[item["id"]] = item
            for relation in new_relations:
                if relation["id"] in relations:
                    existing = relations[relation["id"]]["evidence"]
                    existing.extend(ref for ref in relation["evidence"] if ref not in existing)
                else:
                    relations[relation["id"]] = relation
            gaps = list(dict.fromkeys(gaps + new_gaps))
            for relation in relations.values():
                if not (
                    candidates[relation["source_candidate_id"]]["selected"]
                    and candidates[relation["target_candidate_id"]]["selected"]
                ):
                    relation["selected"] = False
            segments_completed.add(segment_id)
            if all(item["id"] in segments_completed for item in segments):
                completed.add(page.id)
            with db.transaction(run["tenant_id"], write=True) as session:
                job, proposal = state.verify(session)
                proposal.candidates, proposal.relations = list(candidates.values()), list(relations.values())
                proposal.gaps, proposal.definition = gaps, definition
                job.checkpoint = {
                    **job.checkpoint,
                    "stage": "extracting",
                    "facts_completed": list(completed),
                    "facts_segments_completed": list(segments_completed),
                    "facts_ranges": ranges_by_page,
                    "facts_pages_completed": len(completed),
                }
                job.checkpoint.pop("facts_blocked_range", None)
            part += 1

    def apply(session, job):
        proposal = session.get(AIProposal, state.proposal.id)
        proposal.definition, proposal.gaps, proposal.status = definition, gaps, "ready"
        proposal.revision += 1
        job.checkpoint = {**job.checkpoint, "stage": "ready"}

    finish(
        db,
        run,
        result={"proposal_id": state.proposal.id, "pages": len(pages), "candidates": len(candidates)},
        apply=apply,
    )
