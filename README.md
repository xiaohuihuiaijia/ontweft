<p align="center"><img src="frontend/public/brand/ontweft-favicon.svg" width="64" height="64" alt="序枢标志" /></p>
<h1 align="center">序枢 Ontweft</h1>
<p align="center"><strong>让知识连接业务。</strong><br />从文档和数据库，构建有来源、可编辑、持续更新的业务本体。</p>
<p align="center"><strong>企业本体 · 知识图谱 · 知识管理</strong><br />AI 辅助建模 · 自部署 · Apache-2.0</p>
<p align="center">简体中文 · <a href="README.en.md">English</a> · <a href="#开始使用">开始使用</a> · <a href="docs/deployment.md">部署文档</a> · <a href="https://github.com/xiaohuihuiaijia/ontweft/issues">反馈场景</a></p>

![Ontweft 真实工作台：探索具名业务关系，查看并编辑客户全景对象](docs/images/graph-workbench.png)

<p align="center"><sub>真实应用与 API，22 个合成对象、30 条关系。通过实际拖动整理个人视图；点击节点进入详情与编辑。非客户数据。</sub></p>

> **当前版本：Enterprise Beta。** 默认 `main` 分支提供可运行的工作台，克隆后即可按下方步骤运行。界面以中文为主；复杂材料质量与生产部署仍需按自己的场景验收。

## 从资料到可追溯的知识图谱

一份产品手册、一组业务表、一套数据库，往往描述着同一个业务的不同部分。Ontweft 帮你定义其中的类型、属性和关系，再把原始记录映射成可以检索、追溯与编辑的对象。

**选择材料 → 描述业务目标 → 审阅模型与事实候选 → 发布、导入 → 持续更新与图谱探索。**

| 工作环节 | 你可以做什么 |
| --- | --- |
| **理解资料** | 读取 PDF、Office、图片与文本；按所选范围自动归纳，或按自定义业务要求提取；候选可跳回来源 |
| **起草本体** | AI 建议类型、属性、关系和映射；复用已有模型，编辑后进入既有校验、审批与发布流程 |
| **接入数据** | CSV/XLSX 与 PostgreSQL；明确业务键、不可变文件版本、来源映射和精确跨来源绑定 |
| **维护关系** | 持续规则随字段变化增量重算；区分人工关系与来源贡献，保留历史依据 |
| **在图谱中工作** | 授权全库搜索、按需展开邻域、拖动与固定节点、恢复个人视图；单击节点编辑；查看具名关系，新增或删除人工关系 |
| **团队协作** | 项目与字段权限、模型修订、版本化证据和审计；企业模式默认异人模型审批 |

## 哪些场景适合 Ontweft？

| 你正在解决的问题 | 在 Ontweft 中形成什么 |
| --- | --- |
| **产品资料与规格治理**：型号、参数、条件散落在 PDF 和表格中 | 审阅有原文出处的候选，建立产品本体，再按属性检索和查看关系 |
| **业务知识图谱**：客户、订单、产品分散在不同系统中 | 用业务键连接确定性的记录，通过具名关系探索业务对象 |
| **知识管理**：同一个概念叫法不一致，结论无法回查 | 明确类型与属性、保留版本和来源，确认哪些内容可以成为业务事实 |
| **知识库管理**：希望组织文档、知识页面并控制访问 | 按需启用知识扩展，使用页面、全文检索和证据关联；不是开箱即用的问答机器人 |

**本体**规定“有哪些东西、有什么属性、可以如何关联”；**知识图谱**呈现这些规则下的具体对象；**知识库**保存资料与知识页面。Ontweft 将它们连接起来，不把文件上传等同于知识已经治理完成。

只有 **数据、本体、对象、图谱、设置** 五个主入口。核心是一套 PostgreSQL、一个 API 服务和现有 worker。无需额外图数据库或向量服务；手工建模和确定性采集无需 AI Key。非核心连接器、知识与自动化按需启用。

## 开始使用

准备 **Python 3.12、uv、Node.js 22.12+**，运行：

```sh
git clone https://github.com/xiaohuihuiaijia/ontweft.git
cd ontweft
uv sync --locked --no-dev
npm --prefix frontend ci
npm --prefix frontend run build
uv run --locked --no-dev python scripts/start_local_demo.py
```

首次运行设置至少 12 个字符的密码，终端不回显。打开 **http://127.0.0.1:8020**，使用 `owner` 登录。

- **先体验图谱：** 在“本体”选择模板并编辑、验证、发布，在“对象”添加记录，再进入“图谱”拖动与编辑。
- **从自己的数据开始：** 另开终端启动下面的 worker，从“数据”上传 CSV/XLSX，选择业务键并生成模型建议。
- **让 AI 辅助起草：** 在“设置 → 模型连接”配置兼容服务，在“数据 → 文档理解”或“本体 → AI 起草”选择材料、范围与业务要求。

```sh
uv run --locked --no-dev python scripts/start_local_demo.py worker
```

本机体验使用持久化 SQLite，数据保存在 `var/local-demo/`，仅监听回环地址。团队部署使用 PostgreSQL 与 HTTPS；见[部署指南](docs/deployment.md)。

## AI 提建议，业务事实由你确认

接入层使用可配置的 OpenAI-compatible Chat Completions，文字、视觉与结构化输出能力分别声明和测试。百炼是首个实测服务；不把接口兼容视作其他供应商已通过验证。

PDF 在本地分页渲染并提取原生文字，表格结构与复杂页面交给所配置的视觉模型；Office 和表格文件保留可直接读取的结构。支持 PDF、DOCX、XLSX、PPTX、PNG/JPEG、CSV、文本/Markdown；Office 页码是逻辑单元。文档上传默认 **256 MiB**，确定性表格导入另有[行列及大小预算](docs/data-to-ontology.md)。

页面进度与证据版本可追溯；修改业务要求可复用基础识别。模型产出进入可编辑候选，不能执行任意 SQL、直接发布模型或绕过实例导入确认。**无需独立 OCR、GPU 或常驻本地模型服务；配置外部模型后，调用按供应商计费。**

## 已验证什么

已运行隔离数据库回归、真实 Chromium 工作流、Linux/PostgreSQL CI，以及合成数据部署与恢复验证。持续修复围绕可复现问题：连续文件更新、来源撤回预览、关系匹配、表单状态、模型费用记账及列表读取，详见[项目审查](docs/project-audit.md)与[当前 CI](https://github.com/xiaohuihuiaijia/ontweft/actions/workflows/ci.yml)。

已有 **10 万对象 / 100 万关系 / 50 用户**的合成规模测量；唯一编码搜索 P95 仍高于 1 秒目标，不能据此宣称生产 SLA。复杂文档的人工准确率、整本处理、真实 CDC 故障恢复与大附件灾难恢复仍需专项验收。[完整测量与限制 →](docs/beta-verification.md)

## 文档与参与

| 想了解什么 | 入口 |
| --- | --- |
| 第一次建立本体 | [入门指南](docs/getting-started.md) · [从数据建模](docs/data-to-ontology.md) |
| 模型与图谱如何工作 | [本体方法论](docs/ontology-methodology.md) · [图谱操作](docs/graph.md) |
| 部署、升级与扩展 | [部署指南](docs/deployment.md) · [核心与扩展](docs/enterprise-beta.md) |
| 核对实现或提交代码 | [贡献指南](CONTRIBUTING.md) · [审查与待审核项](docs/project-audit.md) |

**Python / FastAPI / SQLAlchemy / PostgreSQL · React / TypeScript / Sigma / Graphology。** 保持模块化单体，优先解决具体场景，不建设额外的平台层。

如果你正在寻找自部署的企业本体、知识图谱或可追溯的知识管理工具，欢迎 **Star** 关注进展。也欢迎[带着一个业务场景来提 Issue](https://github.com/xiaohuihuiaijia/ontweft/issues)：你要连接什么数据、回答什么问题、在哪一步遇到了阻碍。请使用脱敏示例；安全问题按[安全说明](SECURITY.md)私下报告。

自有代码采用 [Apache-2.0](LICENSE)，第三方组件保留原许可与[必要通知](frontend/public/THIRD_PARTY_NOTICES.txt)。[截图来源](docs/images/README.md)与 [NOTICE](NOTICE)随仓库提供。当前不宣称生产可用性、完整安全审计、商标清查或全部二进制/模型再分发验收通过。
