"""Small versioned contracts: transcription, ontology proposal, grounded instances."""

from ontology.modeling.schemas import Definition

VERSION = "business-document-v2"


def obj(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def arr(items):
    return {"type": "array", "items": items}


TEXT = {"type": "string"}
BOOL = {"type": "boolean"}
PAGE_SCHEMA = obj(
    {"text": TEXT, "title": TEXT, "issues": arr(TEXT), "needs_review": BOOL, "continued_table": BOOL}
)
PAGE_INSTRUCTION = """忠实读取本页全部业务内容，输出中文 JSON。text 使用保留结构的 Markdown，表格不能只做摘要：
逐行保留型号、数值、单位、范围、表头层次、脚注与适用条件。图片只写可确认的标注和结构；不从外观推断参数或适配关系。
提供的原生文字用于校对数值；页面图是版面依据。无法辨认的内容标记[无法辨认]并记录 issues，不猜测。
空白页明确写[空白页]。continued_table 标明存在需要相邻页才能解释的表格。"""

PROPERTY = obj(
    {
        "key": TEXT,
        "label": TEXT,
        "data_type": {
            "type": "string",
            "enum": ["string", "integer", "number", "boolean", "date", "datetime", "enum", "json"],
        },
        "required": BOOL,
        "enum": arr(TEXT),
        "unit": {"type": ["string", "null"]},
        "source_owned": BOOL,
    }
)
RELATION = obj(
    {
        "key": TEXT,
        "label": TEXT,
        "target_type": TEXT,
        "cardinality": {"type": "string", "enum": ["one", "many"]},
    }
)
TYPE = obj(
    {
        "key": TEXT,
        "label": TEXT,
        "description": TEXT,
        "properties": arr(PROPERTY),
        "unique_keys": arr(arr(TEXT)),
        "relations": arr(RELATION),
    }
)
MAPPING = obj(
    {
        "source_id": TEXT,
        "resource_id": TEXT,
        "type_key": TEXT,
        "fields": arr(
            obj(
                {
                    "target": TEXT,
                    "source_field": TEXT,
                    "transform": {
                        "type": "string",
                        "enum": [
                            "identity",
                            "trim",
                            "lower",
                            "upper",
                            "integer",
                            "number",
                            "boolean",
                            "date",
                        ],
                    },
                }
            )
        ),
    }
)
MODEL_SCHEMA = obj({"types": arr(TYPE), "mappings": arr(MAPPING), "gaps": arr(TEXT)})
MODEL_INSTRUCTION = """你为企业设计最小可用本体。按用户业务目标阅读全部当前材料，生成需要新增/扩展的 types，优先复用已有类型和属性。
返回增量：仅需改变的类型；改变已有类型时保留其原有属性和关系。不能因为一张表/一页/一个单元格就新增类型。
description 说明业务用途和材料依据。标识符使用以字母开头的 ASCII 字母数字下划线。
文档提取的属性 source_owned=true；required 通常为 false。唯一键仅在明确业务身份存在时建议，不能用页码、行号当业务键。
关系必须有明确材料支持；不要凭常识补充型号兼容性。区分对象、属性、对象关系和适用条件。资料不足写 gaps。
对于结构化来源生成 fields 映射，source_id/resource_id/source_field 必须来自输入。文档来源的映射由系统按已确认候选创建。
不推测资料之外的知识。所有输出仅是供人审核的建议，不执行任何操作。"""
FACT = obj(
    {
        "id": TEXT,
        "type_key": TEXT,
        "values": arr(obj({"key": TEXT, "value_json": TEXT})),
        "business_key_fields": arr(TEXT),
        "quotes": arr(TEXT),
    }
)
FACT_RELATION = obj({"source_id": TEXT, "target_id": TEXT, "key": TEXT, "quotes": arr(TEXT)})
FACT_SCHEMA = obj({"candidates": arr(FACT), "relations": arr(FACT_RELATION), "gaps": arr(TEXT)})
FACT_INSTRUCTION = """根据给定模型和用户目标提取本页明确陈述的实例。只使用已有 type_key、属性 key 和关系 key。
只抽取 focus_text 中的事实；text 是整页上下文，用于理解表头、单位、脚注与条件，不能重复抽取 focus_text 之外的无关行。
previous_pages 仅提供相邻页衔接上下文，可用于表头、条件及明确指代的解析。只在材料明确连接时输出跨页关系；
关系引用上一页对象时也在本次 candidates 中列出该端点及其原文引文。不得仅因两页相邻就推断关系，不输出无关的上一页实例。
values 中 value_json 是正确类型值的 JSON 字符串（字符串要带引号；数字保留精度；缺失字段不要输出）。
每个候选提供原文中连续存在的最短必要 quotes（对应行、表头及条件），不要反复复制整页或整张表，不要改写引文。
business_key_fields 只列明确业务身份字段，没有则留空。编码规则和选项组合不是已存在的产品实例；禁止枚举资料未逐项列出的可能型号或组合。
将可选、要求、示例、典型值、条件或区间明确保留，不把它们当无条件事实。不根据外观推断参数和兼容性。
relations 两端必须是本次候选 id，且有明确原文引文。表格关联可引用表头加对应连续行，不编造跨段引文。
不输出与目标无关的实例。资料不支持的问题写 gaps。"""


def merge_model(current, delta):
    import copy

    result = copy.deepcopy(current)
    types = {t["key"]: t for t in result["types"]}
    for incoming in delta["types"]:
        previous = types.get(incoming["key"], {})
        # Model-generated incremental updates never remove an existing field or permission.
        props = {p["key"]: p for p in previous.get("properties", [])}
        for prop in incoming["properties"]:
            props[prop["key"]] = {**prop, **props.get(prop["key"], {})}
        relations = {r["key"]: r for r in previous.get("relations", [])}
        for relation in incoming["relations"]:
            relations.setdefault(relation["key"], relation)
        types[incoming["key"]] = {
            **incoming,
            **{k: v for k, v in previous.items() if k not in {"properties", "relations"}},
            "properties": list(props.values()),
            "relations": list(relations.values()),
        }
    result["types"] = list(types.values())
    return Definition.model_validate(result).model_dump()
