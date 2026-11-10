# 建立自己的第一个本体

这条入门路径使用真实前后端、独立持久化 SQLite、表单和图谱。它用于本机学习，不需要数据库账号、AI Key 或飞书授权。生产使用仍需 PostgreSQL 和单独的部署验证。

已在 Windows 的独立代码副本中重新安装锁定依赖、构建并走通桌面/手机浏览器流程，未借用原有数据库或账号。核心建模无需安装 Docling；飞书 SDK 在使用对应功能时才加载。Linux/macOS 的干净安装尚未在本轮实测。

## 启动

准备 Python 3.12、uv、Node 22.12+，在仓库根目录执行：

```text
uv sync --locked --extra dev
npm --prefix frontend ci
npm --prefix frontend run build
uv run --locked --extra dev python scripts/start_local_demo.py
```

首次运行输入两次至少 12 个字符的密码。打开 `http://127.0.0.1:8020`，用户名为 `owner`。终端不会显示密码，也不生成明文密码文件。

后续只需重跑最后一条。数据保存在 `var/local-demo/`，停止进程不会删除数据。可用 `--port 8021` 换端口，用 `--data-dir var/my-ontology` 创建另一份工作空间；不会重置已有账号，也不会接管含其他文件的目录。忘记密码时可另建学习目录；不要删除原目录里的业务数据。

## 从结构到对象

1. **定义问题。** 例如“一个项目有哪些任务”“每个任务处于什么状态”。先限定小范围。
2. **建立结构。** 打开“本体模型 → 新建变更集”，选择“项目与任务”。模板仅填入可编辑的结构，不自动创建或发布数据。
3. **自定义。** 将“项目”的显示名称改成自己的业务术语，检查字段类型和枚举。稳定标识用于引用，已有数据后不要随意改标识。也可选择“资产与位置”或从空白手工建模。
4. **发布模型。** 保存草稿 → 验证 → 批准当前修订 → 发布。失败时修正后重试，不能跳过错误。账号必须具有对应权限。
5. **创建对象。** 在“业务对象”先建立一个项目，再建立任务。打开任务的“关系与目标对象”，选择“所属项目”和刚创建的项目，保存关系。
6. **查看与修改。** 在“图谱探索”的“业务实例”选择任务，查看字段和关系，再从详情打开对象表单。修改后保存并刷新，核对数据仍然存在。切换“本体模型”可查看类型之间的结构关系。

“任务状态”为枚举不会自动驱动业务流程；“所属项目”的 `one` 表示一个任务至多连到一个项目，不表示关系必填。当前没有跨对象唯一约束，也没有完整 OWL 推理。更完整的决策规则见 [构建方法论](ontology-methodology.md)。

图谱默认载入最多 200 个节点，可用“载入节点上限”切换为 100 或 500，单次最多 1000 条关系。界面会明确提示截断；“查找当前子图”只筛选本次已载入的数据，不是全库搜索。超过 40 个节点时，键盘节点列表默认收起，点击标题即可展开；缩小子图后更容易核对具体关系。

## 可用范围与扩展

| 本机体验可做 | 需要另外配置或尚未完成 |
| --- | --- |
| 自定义类型、字段、关系与模型发布 | PostgreSQL 租户隔离、生产账号与 TLS 部署 |
| 类型化对象、具名关系、图谱与对象编辑 | 外部数据库连接和 CDC 服务 |
| 工作空间内的角色权限及变更流程 | 真实飞书授权、导入和外发适配 |
| 手工知识编辑；配 worker 后运行基础后台任务 | Docling 可选依赖、离线模型、OCR 与复杂版面验收 |

后台任务另开终端：

```text
uv run --locked --extra dev python scripts/start_local_demo.py worker
```

若用了自定义目录，API 与 worker 使用相同 `--data-dir`。此入口只监听回环地址；不要把它当作公网部署服务。需要保留或迁移数据时先备份整个体验目录，SQLite 不自动转换成 PostgreSQL。

## 可复现的开发验证

```text
uv run --locked --extra dev python scripts/test_backend.py backend/tests/modeling/test_templates.py backend/tests/platform/test_local_demo.py -q
npm --prefix frontend test -- --maxWorkers=2
npm --prefix frontend run build
node scripts/verify_model_starters.cjs
```

最后一条另需已安装的 Playwright 与 Chromium，可用 `PLAYWRIGHT_MODULE` 指定模块。它使用随机密码、独立临时目录及真实构建页面，测试完成后清理自身测试数据库；不会操作 `var/local-demo/` 或开发数据库。

较大合成图的独立验证为 `node scripts/verify_graph_scale.cjs`，同样需要已构建前端和 Playwright；它创建 501 个合成节点及 1100 条关系，记录 200/500 节点的查询数量、局部耗时与桌面/手机预览。该单用户 SQLite 检查不替代生产压测。需要验证 PostgreSQL 合同，可在 Docker 已启动且本地已有 `postgres:17` 镜像时运行 `uv run --locked --extra dev python scripts/verify_model_starters_postgres.py --include-graph`，仅使用本次临时容器与随机凭据。

本次开放的是源码 Alpha。生产部署、其他环境验证及二进制分发限制见 [Alpha 范围与验证](alpha-status.md)。
