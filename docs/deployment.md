# 自部署、升级与恢复

部署保持 PostgreSQL、一个 API（同时提供前端静态文件）和现有 worker；迁移容器一次性执行，不是常驻服务。核心镜像包含 PDFium、Pillow 和原生 Office 文档处理依赖，用于本地分页、图像和结构读取；不默认安装 MySQL、MongoDB、Neo4j、飞书 SDK、独立 OCR 引擎或本地模型服务。

## 首次启动

1. 从 `deploy/compose.env.example` 复制一份配置到版本控制之外。填写不同的、URL 安全的随机数据库所有者密码和运行账户密码，生成 Fernet 密钥，设置实际 HTTPS Origin。数据库所有者仅供迁移／初始化使用，服务和 worker 使用 NOBYPASSRLS 运行账户。
2. 准备自己的 HTTPS 反向代理，将它转发到 `127.0.0.1:8000`。默认 Cookie 为 Secure，HTTP 浏览器登录不适用于这个 production 配置。
3. 在仓库执行：

```sh
docker compose --env-file /secure/ontweft.env build
docker compose --env-file /secure/ontweft.env up -d
docker compose --env-file /secure/ontweft.env run --rm --entrypoint ontology migrate init --tenant my-company --name 我的企业 --email admin@example.com --username admin
```

初始化命令交互读取管理员密码，不覆盖已有企业。此命令使用迁移角色，日常 API 与 worker 不使用数据库所有者。创建另一位账号并授予审核角色后，即可执行企业模式的异人模型审批。

密钥必须另行保留，否则恢复数据库后无法解密已有来源凭据。不要提交实际环境配置。默认不暴露数据库端口；API 只绑定回环地址。

核心来源主机通过 `ONTOLOGY_ALLOWED_SOURCE_HOSTS` 指定；需要访问内网来源时还需配置 `ONTOLOGY_ALLOW_PRIVATE_SOURCE_ADDRESSES=true`，API 与 worker 保持一致。Compose 的 runtime 环境块可以加入这些既有设置。扩展安装与固定开关见 [核心说明](enterprise-beta.md)。

`API_WORKERS` 默认 1，直接使用 Uvicorn 已有多进程功能；增加时仍是一项 API 服务。按实际 CPU、内存和并发测量选择数量，参考 [性能记录](beta-verification.md)，不要把单进程的资源配置当作 50 并发性能承诺。

文档上传默认 256 MiB，API 与 worker 的 `ONTOLOGY_MAX_UPLOAD_BYTES` 必须一致。原生 PDF 解析直接打开源文件，每批最多 32 页；完整页范围仍受默认 1000 页、1000 万字符等预算限制。批次结果保存在数据目录的 `temporary/pdf-<任务标识>/`，任务仅记录进度和摘要引用；超时、取消或失败后的重试从已完成批次继续。成功写入完整解析版本后清理中间文件，未完成任务的目录应保留以便续跑。任务完成不表示图像、复杂表格已理解：缺少原生文字的页面仍明确标记为待视觉处理。

管理员在“设置 → 模型连接”配置兼容服务和场景模型后，现有 worker 执行文档理解与本体提案；密钥只在后端加密保存。无模型连接时手工建模、确定性采集与原生文档读取仍可使用。不要把私有原件或页面结果写入 CI、公开截图或仓库。

Office／文本的新提案固定其逻辑分页所用的解析版本，原文跳转与正式导入证据沿用该版本。旧提案没有版本标识时不推测历史绑定；已有逻辑页进度但无法确认输入版本的未完成旧任务会暂停，需要保留结果后重新起草。

## 升级

先备份数据库与文件资产，然后停止 API、worker，更新源码并构建镜像，执行 `docker compose run --rm migrate`，再启动服务。迁移失败时保留数据库与备份，修正原因后重试；不要修改历史迁移或直接覆盖数据文件。

从早期 Alpha 升级到 Beta 时，已有迁移包括业务唯一键、增量校验序号、文件版本、持续规则和检索词项索引。原文件位置不变，旧表格自动登记为版本 1。对象检索的派生字段会清空重建；业务对象、来源证据、发布模型与冻结函数不清空。准备好 worker，并在检索索引完成重建前保留“更新中”提示。

已有迁移 `ef311029a500` 至 `a700` 分别建立共享登录计数、CDC 历史采集代及当前对象排序索引，不删除业务数据。登录预算保存在现有数据库：每 IP／企业／账号 5 分钟 10 次尝试，每 IP 每分钟 100 次；成功登录清除账号桶，保留 IP 桶，每次最多回收 256 个过期桶。Compose 的 `FORWARDED_ALLOW_IPS` 只填写 API 实际看到的可信代理地址（默认回环）；避免通配符，不能让用户伪造客户端 IP，也不要把所有用户都当作同一个代理 IP。

AI 文档迁移 `ef311111a100` 增加模型连接、提案、不可变页面结果和文档关系事实。当前迁移入口、开发初始化、测试与压测共用同一授权策略，重新执行时会撤销运行角色的过时表权限；历史解析及 AI 页面结果只允许读取与追加。此轮授权收敛不新增迁移，不删除已保存记录。

本机 SQLite 体验目录升级：先停止体验 API 和 worker，执行 `python scripts/start_local_demo.py migrate --data-dir <原目录>`，再照常启动。该命令先生成可打开的 SQLite 备份，再升级历史采集代与查询索引；未升级时入口给出明确提示，不自动改写旧库。数据库备份文件需要按自己的保留策略管理。

## 文件存储维护

默认保留所有有数据库引用的原件、历史版本与未应用预览。界面合计为原件逻辑字节数，不等于去重后的磁盘占用；实际占用使用下面清单。

停止 API、worker 后，在同一部署环境执行 `ontology storage`，按引用、近期未引用、过期孤儿列出已知上传目录及字节数。默认 7 天保留期；`ontology storage --quarantine` 只将过期且无引用的上传目录移动到资产卷内的 `quarantine/<运行标识>/`，并保留 `manifest.json`。不永久删除，不处理未知目录或符号链接，不清理文档原件；数据库引用版本永远不会因文件年龄而被隔离。

恢复隔离项时保持服务停止，按清单将相对路径移回原位置，已有同名目录先核对，不覆盖。隔离目录仍占空间；确认不再需要的孤儿最终删除由部署者按备份／保留制度处理。无新增定时任务或维护后台。

## 健康与任务状态

- `/api/health`：进程和数据库连接健康检查，Compose 会调用。
- `/api/jobs/metrics?project_id=…`：需正常登录及 `jobs:manage` 权限；返回按类型／状态计数、待处理数、失败数、最早积压秒数和 worker 心跳年龄。
- 心跳文件在共享资产卷，worker 每轮更新。缺失表示未观察到 worker；长时间未更新需要结合任务状态判断，不等同于任务成功。
- 任务详情和重试仍在设置中，没有新增治理后台。禁用扩展的历史任务保留，不能把这部分积压误报成核心任务故障。

## 备份与恢复

`deploy/backup.py` 使用当前 Compose 项目和环境；在运行它之前设置 `COMPOSE_PROJECT_NAME`、`COMPOSE_FILE`、`COMPOSE_ENV_FILES`（指向上述配置）。使用其他配置管理方式时，也可把相同值通过环境提供。

```sh
python deploy/backup.py create /backup/ontweft-2026-11-10
```

输出 `database.dump`、`assets.tar` 与 SHA-256 清单。数据库先使用一致性 dump；资产原件和文件版本按追加方式保留，归档排除解析临时目录和 worker 心跳文件；随后归档可以包含少量尚未引用的新文件，不会用这些额外文件改写数据库。保留备份期间不能另行手工清理历史资产。密钥与部署环境需单独安全保留，备份工具不把它们写入清单。

恢复使用**新的 Compose 项目、空数据库和空资产卷**；工具拒绝覆盖已有表或资产。

```sh
docker compose up -d --wait db
python deploy/backup.py restore /backup/ontweft-2026-11-10
docker compose up -d
```

恢复校验文件摘要，导入数据库与原件，重新执行迁移和受限角色授权；恢复后核对登录、对象、历史引用和原件下载。RPO 取决于实际备份调度间隔与备份完成时间。工具不自动建立计划任务，也没有把一次小样本恢复的秒数当作全库 RTO。

未完成的原生 PDF 任务依赖临时批次，这些文件不在备份中。恢复后重试会在继续解析前发现缺失并停止；从文档详情重新提交解析以建立新任务，不能把缺失批次算作已完成页面。已经提交的解析版本和 AI 页面记录按原有持久化资产恢复。

## 可复现验收

```sh
uv sync --locked --extra dev --extra knowledge --extra integrations
python scripts/verify_deployment.py
python scripts/verify_deployment.py --scale
```

该工具使用已构建的 `ontweft:beta-test` 镜像（可设置 `ONTOLOGY_TEST_IMAGE`），仅创建随机命名的独立 Compose 项目和合成记录，验证异人审批、worker、健康、指标、备份、恢复与原件一致性。`--scale` 增加到 10 万合成对象和 100 万关系，附件仍只有一份真实上传 CSV，因此不代表大体积文档库恢复。结束时只清理它创建的两个测试项目及卷。实际结果写入 `var/reports/`，不能替代生产数据分布和灾难条件验收。
