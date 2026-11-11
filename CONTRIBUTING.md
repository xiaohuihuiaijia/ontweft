# 贡献说明

欢迎报告可复现问题、改进文档和提交小范围 PR。先说明业务问题、实际行为、预期行为与版本；示例仅使用合成数据。

提交贡献前，请确保有权按本项目 Apache-2.0 许可证提交相关代码。引入第三方文件时保留其原许可证、来源、固定版本及修改说明；不把研究参考自动当作可复制代码。

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

浏览器检查在独立临时数据目录运行，不使用开发账号。下面把测试工具安装到忽略目录，不修改应用锁文件：

```sh
npm install --prefix var/browser-tools --no-save playwright@1.60.0
node var/browser-tools/node_modules/playwright/cli.js install chromium
```

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

更新 GitHub 展示截图时，复用上面的 Playwright 环境运行 `node scripts/capture_showcase.cjs`。它通过真实 API 创建独立合成图，使用实际拖动和编辑入口，结果写入忽略目录 `var/reports/showcase/`。确认画面后只复制所选 PNG 到 `docs/images/graph-workbench.png`，并更新 [截图来源](docs/images/README.md)；不提交临时数据库或日志。中英文 README 共用这一张图。

- 保留 `ontology.api:create_app`、CLI、worker 和 parse_task 入口。
- 新 ORM 类加入 `core/registry.py`；历史 Alembic revision 不重写。
- `backend/ontology/functions/runtime.py` 是已发布的 v1 源码哈希载体，不能自动格式化或改换行符；函数行为变更通过新版本及审批发布。
- 跨测试辅助代码放入 `backend/tests/support`，不能从其他 `test_*.py` 或 `conftest.py` 导入。
- 不以截图替代权限、持久化或并发合同测试；不把模拟与局部通过描述为真实外部集成通过。
- 凭据、运行数据、客户资料、未明确许可的图片/权重不得提交。

新增文件先按路径加入 Git 暂存区，然后运行 `python scripts/source_manifest.py` 更新源码清单，并提交清单。`python scripts/source_manifest.py --check` 仅核对文件与摘要，不代表凭据或许可证审查通过。
