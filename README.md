# 序枢 Ontweft

**让知识连接业务。** 一个可编辑、可运行的企业本体工作台：从业务问题出发，定义类型、字段和具名关系，经版本化校验与发布，建立自己的对象和图谱。

**状态：企业 Beta 改造版，按实测范围交付。** 核心安装、持续更新、规则关系与独立部署已实现；规模指标和剩余验收见 [验证记录](docs/beta-verification.md)。界面以中文为主。

![序枢工作台：合成数据的实例图](docs/images/workbench.png)

## 现在可以做什么

- 上传 CSV/XLSX 或选择已发现的数据库资源，生成可编辑的类型与来源映射，发布后导入对象；保留原文件、行号和来源证据。[用自己的数据建模](docs/data-to-ontology.md)
- 按业务键更新同一文件来源，保留原件版本；精确业务键可绑定已有对象。持续关联规则维护自己的关系贡献，保留人工关系与其他来源贡献。
- 从“项目与任务”“资产与位置”模板或空白开始，自定义类型、属性、枚举和关系。
- 编辑并发布模型，使用组合唯一键与关系基数约束；大规模数据后台分批校验，企业部署默认异人审批。
- 在授权全库中搜索对象，分页展开邻居、固定节点并保存本浏览器个人视图；画布保持有界。知识引用图按需启用。
- 默认五个入口：数据、本体、对象、图谱、设置；任务与审计归入设置。知识、集成、自动化通过固定配置按需启用。
- 阅读并开发数据库连接、来源映射、CDC、飞书、文档处理、动作与可信函数模块；这些模块各有配置及验收限制。

## 本机启动

需要 **Python 3.12、uv、Node.js 22.12+**。本机体验使用独立 SQLite；企业自部署使用 [Linux / PostgreSQL 部署说明](docs/deployment.md)。

```sh
git clone https://github.com/xiaohuihuiaijia/ontweft.git
cd ontweft
uv sync --locked --no-dev
npm --prefix frontend ci
npm --prefix frontend run build
uv run --locked --no-dev python scripts/start_local_demo.py
```

首次启动设置至少 12 个字符的密码，终端不回显。打开 **http://127.0.0.1:8020**，用户名 `owner`。

在“本体 → 新建变更集”选择模板，编辑后依次保存、验证、批准、发布；然后到“对象”创建记录和关系，在“图谱”中查看和继续编辑。[完整入门步骤](docs/getting-started.md) · [本体构建方法论](docs/ontology-methodology.md)

数据保存在 `var/local-demo/`，重启保留账号与数据；不用数据库账号或 AI Key。已有非体验数据目录不会被接管或重置。此入口只监听回环地址，**不用于公网生产部署**。采集、持续关系、全文索引和大规模模型校验需要 worker，另开终端：

```sh
uv run --locked --no-dev python scripts/start_local_demo.py worker
```

上传表格不需要开放网络来源。连接自己的 PostgreSQL 时，须为服务和 worker 指定相同的来源主机名单；本机或内网地址另需明确启用。见 [数据库接入步骤](docs/data-to-ontology.md#连接自己的数据库)。

## 架构与源码

| 目录 | 内容 |
| --- | --- |
| `backend/ontology` | Python / FastAPI 模块化后端、SQLAlchemy、权限与业务合同 |
| `backend/migrations` | PostgreSQL 的 Alembic 迁移 |
| `backend/tests` | 按领域组织的测试；外部服务用例需显式配置 |
| `frontend/src` | React / TypeScript 工作台，Sigma / Graphology 图谱 |
| `integrations/debezium` | 自有 Java CDC 适配代码、协议样本和测试；没有预构建镜像 |
| `scripts` | 本机体验、验证、可配置开发工具 |
| `docs` | 入门、建模方法、图谱、企业核心与验收记录 |

生产存储设计使用 PostgreSQL；SQLite 是独立学习和测试路径，不等同于 PostgreSQL RLS 验收。历史迁移和已发布函数的哈希载体需要保持稳定，见 [贡献说明](CONTRIBUTING.md)。

## 范围与限制

- 持续文件更新要求在首次投影前指定业务键。旧行号来源保留原身份，可建立新来源并精确绑定已有对象；复杂历史对象合并没有加入本轮。
- 文件结构改变需显式审核新来源映射。完整替换不等于任意状态回滚；撤权与来源撤回分别处理。
- 图谱画布仍最多 500 节点、1,000 条关系；全库搜索与邻域查询在服务端执行。个人视图仅保存在当前浏览器，恢复时重新鉴权。
- RDF／SHACL、推理、自然语言与向量能力按实际用例后续扩展，不作为核心部署条件。飞书真实员工场景与复杂 OCR 独立配置、独立验收。
- 10 万对象／100 万关系、约 50 并发用户及查询／传播／恢复目标以实际测量为准；8 vCPU／32 GB 是参考环境，不是最低安装配置。
- 源码开放与局部验收不表示生产可用性、安全审计、商标或完整第三方二进制／模型分发已通过。

[核心设计与迁移边界](docs/enterprise-beta.md) · [验证事实](docs/beta-verification.md) · [早期 Alpha 记录](docs/alpha-status.md)

## 开发与验证

```sh
uv run --locked --extra dev --extra knowledge --extra integrations python scripts/test_backend.py backend/tests -q
npm --prefix frontend test -- --maxWorkers=2
npm --prefix frontend run build
```

图谱和入门的真实浏览器验证需要单独安装 Playwright/Chromium，详见 [开发验证](CONTRIBUTING.md)。测试只使用合成数据；不要提交 `.env`、`var/`、客户原件或访问凭据。

## 许可证与来源

自有代码采用 [Apache-2.0](LICENSE)，版权声明见 [NOTICE](NOTICE)。第三方包继续适用各自许可证；前端通知保存在 [THIRD_PARTY_NOTICES.txt](frontend/public/THIRD_PARTY_NOTICES.txt)，构建时从锁定安装包重新生成。

本仓库发布自有源码和必要通知，不捆绑 Python wheels、JAR、Docker 镜像、第三方源码下载树、模型权重或开发数据库。运行依赖由包管理器根据锁文件获取；这不表示其模型或二进制分发义务已全部核清。研究来源与依赖范围见 [第三方说明](third_party/README.md)。

问题与功能建议请提交 [Issue](https://github.com/xiaohuihuiaijia/ontweft/issues)；安全问题按 [SECURITY.md](SECURITY.md) 私下报告。
