# 贡献说明

欢迎报告可复现问题、改进文档和提交小范围 PR。先说明业务问题、实际行为、预期行为与版本；示例仅使用合成数据。

提交贡献前，请确保有权按本项目 Apache-2.0 许可证提交相关代码。引入第三方文件时保留其原许可证、来源、固定版本及修改说明；不把研究参考自动当作可复制代码。

## 本机开发体验

按照 README 安装核心依赖并构建前端后，可在独立目录启动同一份合成产品样例：

```sh
uv run --locked --no-dev python scripts/start_local_demo.py experience --data-dir var/experience
```

`experience` 统一启动 API 和现有 worker，通过实际接口加载样例；首次设置 `owner` 密码，初始化完成后输出可直接进入样例图谱的链接。重复启动不再索取密码、不重建或覆盖已完成样例中的修改。初始化中断后会保留进度，再次运行时输入当前密码继续；Ctrl+C 只停止本次启动的子进程，数据继续保留。端口被占用时用 `--port 8021` 指定另一端口。

普通 `serve` 仍创建空白工作空间，`worker`、`migrate` 保持原用途。需要升级旧体验目录时，停止服务后对该目录执行 `migrate --data-dir <原目录>`；它保留 SQLite 备份并补齐已知的历史列，不会重新调用模型。`--container` 仅供体验容器内部监听，宿主发布端口仍须限定回环地址。正式镜像和生产部署不自动加载样例。

## 验证

按照 README 安装依赖，然后运行：

```sh
uv run --locked --extra dev --extra knowledge --extra integrations python scripts/test_backend.py backend/tests -q
npm --prefix frontend test -- --maxWorkers=2
npm --prefix frontend run build
```

真实 PostgreSQL、连接器和外部来源测试需要显式环境，未配置时跳过不代表通过。独立 PostgreSQL 合同工具需要 Docker 已启动且本地已有 `postgres:17` 镜像：

```sh
uv run --locked --extra dev python scripts/verify_model_starters_postgres.py --include-graph
uv run --locked --extra dev python scripts/verify_model_starters_postgres.py --include-intake --include-relations
uv run --locked --extra dev python scripts/verify_model_starters_postgres.py --include-database-intake
```

企业 Beta 的核心安装、Linux/PostgreSQL、持续关系、文件版本、异人审批和恢复证据见 [验证记录](docs/beta-verification.md)。`scripts/beta_benchmark.py` 只使用显式测试数据库环境，创建独立随机数据库，默认测试 10 万对象、100 万关系和 50 个用户；结束后清理自己的数据库。它使用合成预建索引，不能代表首次导入或全量索引构建速度。部署与备份恢复命令见 [部署说明](docs/deployment.md)。

本机启动、样例或 SQLite 升级发生变化时，选择相关领域测试：

```sh
uv run --locked --extra dev python scripts/test_backend.py backend/tests/platform/test_local_demo.py backend/tests/platform/test_local_experience.py backend/tests/platform/test_experience_sample.py -q
```

镜像构建或体验入口变化时，在 Docker 已启动的环境验证实际容器：

```sh
docker build --target demo -t ontweft-experience:beta-test .
uv run --locked --extra dev python scripts/verify_experience_container.py --image ontweft-experience:beta-test
```

可用 `--report <路径>` 指定本地报告位置。容器烟测与浏览器操作、首次用户试用分别记录，不把其中一种通过当作其他验证也通过。

浏览器检查在独立临时数据目录运行，不使用开发账号。下面把测试工具安装到忽略目录，不修改应用锁文件：

```sh
npm install --prefix var/browser-tools --no-save playwright@1.60.0
node var/browser-tools/node_modules/playwright/cli.js install chromium
```

按改动选择回归，不把以下工具当作每次修改都要依次执行的清单。它们的独有覆盖不同，不因都能打开图谱就互相替代；公共鉴权、发布或来源契约变化时，再运行涉及的多个流程。

| 改动范围 | 选择脚本 | 独有覆盖 |
| --- | --- | --- |
| 本机启动与 SQLite 升级 | `test_local_demo.py`、`test_local_experience.py` | 旧提案说明列升级与备份、完成后免密重启、端口冲突、初始化失败和本次子进程退出 |
| 合成产品样例 | `test_experience_sample.py` | 实际 API 的单一样例加载、中断续跑、重启不覆盖用户修改；不调用外部模型 |
| 体验镜像与发行 | `verify_experience_container.py` | 实际容器中的 API/worker、样例初始化、停止与重启；需要已构建的 demo 镜像 |
| 模型编辑、关系定义与图谱命中 | `verify_model_starters.cjs` | 建模起步、关系属性定义发布、自环关系、缩放及模型/实例切换 |
| 文件接入和持续更新 | `verify_table_intake.cjs` | 真实上传、业务键、映射发布、worker导入、未匹配分页、文件版本与持续规则 |
| 图谱交互 | `verify_graph_workbench.cjs` | 拖动、固定/恢复、键盘/触摸、对象及人工关系管理、关系条件/修订历史与手机编辑 |
| 图谱预算或渲染性能 | `verify_graph_scale.cjs` | 200/500节点预算、查询计数、布局与交互时延；不作为普通文案修改的必跑项 |
| 文档与AI审阅 | `verify_ai_workbench.cjs` | 合成模型建模依据、类型化候选编辑、发布导入和版本化证据回跳 |

一次性设置环境后，只执行与本次改动相关的命令。测试失败先定位实际行为，不通过重复点击、延长等待或放松断言掩盖产品问题。

PowerShell：

```powershell
$env:PLAYWRIGHT_MODULE = (Resolve-Path var/browser-tools/node_modules/playwright).Path
node scripts/verify_model_starters.cjs
node scripts/verify_graph_scale.cjs
node scripts/verify_graph_workbench.cjs
node scripts/verify_ai_workbench.cjs
node scripts/verify_table_intake.cjs
```

POSIX shell：

```sh
PLAYWRIGHT_MODULE="$PWD/var/browser-tools/node_modules/playwright" node scripts/verify_model_starters.cjs
PLAYWRIGHT_MODULE="$PWD/var/browser-tools/node_modules/playwright" node scripts/verify_graph_scale.cjs
PLAYWRIGHT_MODULE="$PWD/var/browser-tools/node_modules/playwright" node scripts/verify_graph_workbench.cjs
PLAYWRIGHT_MODULE="$PWD/var/browser-tools/node_modules/playwright" node scripts/verify_ai_workbench.cjs
PLAYWRIGHT_MODULE="$PWD/var/browser-tools/node_modules/playwright" node scripts/verify_table_intake.cjs
```

## 维护边界

AI 浏览器回归使用本机合成兼容服务，不发送外部请求或使用真实凭据。`scripts/probe_model_connection.py` 与 `scripts/evaluate_private_document.py` 是需明确授权的真实模型测试，使用不回显的密钥输入与本地忽略目录；不在 CI 自动运行。评估续跑只复用同一文档、相同页范围、相同目标与质量档的提案；换页范围建立新提案，并继续计入同一测试总账，不能重置历史花费或未知预留。私有材料不得加入截图、测试夹具或源码清单；模型一致性也不能替代独立原文标注。

更新 GitHub 展示截图时，复用上面的 Playwright 环境运行 `node scripts/capture_showcase.cjs`。它通过真实 API 创建独立合成图，使用实际拖动和编辑入口，结果写入忽略目录 `var/reports/showcase/`。确认画面后将主图与分享图分别复制到 `docs/images/graph-workbench.png`、`docs/images/social-preview.png`，将 AI 脚本中的依据审阅截图复制到 `docs/images/ai-model-review.png`，并更新 [截图来源](docs/images/README.md)；不提交临时数据库或日志。中英文 README 共用这一张图。

- 保留 `ontology.api:create_app`、CLI、worker 和 parse_task 入口。
- 新 ORM 类加入 `core/registry.py`；历史 Alembic revision 不重写。
- `backend/ontology/functions/runtime.py` 是已发布的 v1 源码哈希载体，不能自动格式化或改换行符；函数行为变更通过新版本及审批发布。
- 跨测试辅助代码放入 `backend/tests/support`，不能从其他 `test_*.py` 或 `conftest.py` 导入。
- 不以截图替代权限、持久化或并发合同测试；不把模拟与局部通过描述为真实外部集成通过。
- 凭据、运行数据、客户资料、未明确许可的图片/权重不得提交。

新增文件先按路径加入 Git 暂存区，然后运行 `python scripts/source_manifest.py` 更新源码清单，并提交清单。`python scripts/source_manifest.py --check` 仅核对文件与摘要，不代表凭据或许可证审查通过。
