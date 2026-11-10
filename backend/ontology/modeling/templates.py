"""Original, editable starter definitions; no imported records or publication side effects."""

from ontology.modeling.schemas import Definition


def starter_templates():
    # Construct fresh values for each response. Templates never read tenant data.
    templates = [
        {
            "id": "project_delivery",
            "version": 1,
            "title": "项目与任务",
            "description": "先回答任务属于哪个项目，再定义状态与计划日期。",
            "questions": ["一个项目有哪些任务？", "每个任务的状态和计划完成日期是什么？"],
            "definition": {
                "types": [
                    {
                        "key": "project",
                        "label": "项目",
                        "description": "有明确交付目标的一项业务工作；具体项目是该类型的对象。",
                        "properties": [
                            {"key": "name", "label": "项目名称", "data_type": "string", "required": True},
                            {
                                "key": "status",
                                "label": "项目状态",
                                "data_type": "enum",
                                "enum": ["规划中", "进行中", "已完成"],
                            },
                        ],
                        "relations": [],
                    },
                    {
                        "key": "task",
                        "label": "任务",
                        "description": "项目内可跟踪的工作事项；计划日期不是已完成事实。",
                        "properties": [
                            {"key": "name", "label": "任务名称", "data_type": "string", "required": True},
                            {
                                "key": "status",
                                "label": "任务状态",
                                "data_type": "enum",
                                "enum": ["待处理", "进行中", "已完成"],
                            },
                            {"key": "due_date", "label": "计划完成日期", "data_type": "date"},
                        ],
                        "relations": [
                            {
                                "key": "belongs_to",
                                "label": "所属项目",
                                "target_type": "project",
                                "cardinality": "one",
                            }
                        ],
                    },
                ],
                "mappings": [],
            },
        },
        {
            "id": "asset_locations",
            "version": 1,
            "title": "资产与位置",
            "description": "用资产、位置和存放关系组织设备台账。",
            "questions": ["某处存放了哪些资产？", "资产目前处于什么使用状态？"],
            "definition": {
                "types": [
                    {
                        "key": "location",
                        "label": "位置",
                        "description": "可命名的存放或使用位置；位置分类不等同于类型继承。",
                        "properties": [
                            {"key": "name", "label": "位置名称", "data_type": "string", "required": True}
                        ],
                        "relations": [],
                    },
                    {
                        "key": "asset",
                        "label": "资产",
                        "description": "需要管理的具体设备或物品。资产编号是业务字段，当前不自动执行跨对象唯一校验。",
                        "properties": [
                            {"key": "name", "label": "资产名称", "data_type": "string", "required": True},
                            {"key": "code", "label": "资产编号", "data_type": "string", "required": True},
                            {
                                "key": "status",
                                "label": "使用状态",
                                "data_type": "enum",
                                "enum": ["在用", "维护中", "已停用"],
                            },
                        ],
                        "relations": [
                            {
                                "key": "located_at",
                                "label": "存放位置",
                                "target_type": "location",
                                "cardinality": "one",
                            }
                        ],
                    },
                ],
                "mappings": [],
            },
        },
    ]
    for item in templates:
        item["definition"] = Definition.model_validate(item["definition"]).model_dump()
    return templates
