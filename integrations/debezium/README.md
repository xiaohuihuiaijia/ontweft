# PostgreSQL 原生 CDC 适配

自有传输胶水运行固定 Debezium **3.3.2.Final Embedded AsyncEngine**，使用官方 Connect SourceRecord。这是实施计划中“Debezium Server + 原生 sink”的具体运行时替代：避免默认 Server HTTP 输出遗漏 key/offset，不重写 WAL、快照或复制协议。平台业务、收件箱、权限和投影仍为 Python。

版本对应 `debezium/debezium@a9398538861cc4986906c424700edb2300447cc8`。当前只开放 PostgreSQL/pgoutput；不代表 MySQL/Mongo CDC。源角色需要 SELECT+REPLICATION 和预先建立的指定 publication/slot 权限；测试未授予业务写权限。

## 构建与验证

```powershell
docker build -t ontology-debezium-adapter:3.3.2-snapshot-notifications integrations/debezium
$env:ONTOLOGY_LIVE_CDC='1'
.venv\Scripts\python.exe -m pytest backend/tests/live/test_live_cdc.py -q --tb=short
```

Dockerfile 固定 Maven/JDK 21 镜像 digest，构建执行自有 Java 测试。真实测试只创建带运行标签的 `ontology-source-test-cdc-*` PostgreSQL 和 adapter，数据库仅映射127.0.0.1，结束核对标签后清理。随机凭据、offset、测试数据库在忽略目录 `var/live-cdc/`；无凭据的版本/镜像摘要记录在 `result-context.json`。不修改其他用户容器。

## 运行配置

传入只读挂载的 UTF-8 `.properties` 文件；Bearer 独立保存在 `inbox.token.file`。

- `inbox.stream.url=https://允许主机/api/cdc/streams/UUID`，`inbox.allowed.hosts` 必填；HTTPS默认，HTTP须 `inbox.allow.http=true` 显式例外。禁止重定向/URI用户信息。数据库主机须出现在 `source.allowed.hosts`。部署层仍须限制网络出口。
- `inbox.generation` 为服务端稳定UUID，普通重启不得更换。`inbox.max.events` 为1–1000，`inbox.max.bytes`最高8MiB；单事件超限失败，不截断。
- 标准 `name/database.hostname/database.port/database.user/database.password/database.dbname/topic.prefix/table.include.list/slot.name/publication.name/offset.storage.file.filename` 必填。offset置独立持久卷，UID10001必须可写，不能由多个进程并发共用。
- 强制transaction metadata、精确decimal、bytes、tombstone、ORDERED和单线程；禁止SMT丢弃证据；snapshot.mode=initial、snapshot.query.mode=select_all、snapshot.isolation.mode=repeatable_read、snapshot.max.threads=1；不自动删除slot。拒绝自定义 snapshot SELECT overrides/query 和独立 snapshot.include.collection.list，避免受过滤的扫描伪装成完整表基线。
- 原生 `notification.enabled.channels=sink`，topic 固定为 `topic.prefix + .__ontology_snapshot_notifications`。与行事件走同一 SourceRecord 队列和持久 ACK；没有另建通知数据库或随机请求标记。

## Wire v1 与 ACK

POST `.../lease` `{holder_id:进程UUID}`取得fencing_token/lease_seconds/generation；逐批串行 POST `.../append` `{holder_id,fencing_token,generation,events:[...]}`。重试前续租；409/鉴权失败关闭采集；网络异常/429/5xx原记录退避重试。请求时间和响应字节受限，日志不输出凭据或行数据。

完整保留 `source_partition/source_offset/topic/kafka_partition/timestamp`、key/value的Connect schema+payload、有序可重名headers；另提供event_kind/operation/transaction/snapshot。Decimal保留schema.scale和精确bytes；Long不经浏览器Number中转；tombstone为null payload。事务BEGIN/END完整payload进入transaction。

`identity_material`为对象键排序、UTF-8、不转义Unicode的JSON，固定字段identity_version=1、generation、source_partition、source_position、topic、event_kind、key_payload、transaction、operation、snapshot。source_position仅选择offset实际存在的lsn/txId/ts_usec/messageType/snapshot；完整offset另保留。lsn_proc/lsn_commit是恢复水位，不进入身份。event_id=SHA256(identity_material)；接收端还须按解析后语义身份去重，防JSON排版变更绕过。`protocol/identity-vector.json`是Java真实输出，含中文、超过2^53的Long、Decimal、bytes、null和重复header。

ACK的accepted_event_ids须与请求顺序完全一致（含重复），并有received_sequence。全部拆分HTTP chunks取得持久ACK后才逐条markProcessed，原Debezium batch末尾仅一次markBatchFinished。已提交前缀会在后续chunk失败后重投，由Python去重。收件ACK不代表业务投影/索引完成，也不是跨系统事务。

## 原生基线完成证据

初始快照通知使用现有 `event_kind=control` 原样保存 key/value/offset；首次 STARTED 的实际 offset 可为空，仅已识别的原生 Notification schema 允许这一情况。通知 UUID 是上游控制记录身份，重启可能生成新 UUID；不据此宣称业务事件具有跨尝试稳定身份。

Python `next_cdc_window(..., expected_snapshot_resources=[...])` 必须收到 STARTED、匹配显式预期范围的 IN_PROGRESS、每张表的 SUCCEEDED/行数，以及全局 COMPLETED 才给出完整 SnapshotProof。空表由零行扫描通知证明；非空表还须收到最后 r 的两项完成 flag。原生逐表通知可能先于该表的最后缓冲行，故只在全局完成时核总数。首个 IN_PROGRESS 与之后行的 ts_usec 可以不同，LSN/txId 必须保持同一快照，行之间及最终完成位点的源时间仍须一致。

实测存在另一种恢复证明：最后 r 已持久确认并提交其完成 offset，而 COMPLETED 尚未持久时硬杀，重启只发 SKIPPED。仅在上述 STARTED/范围/逐表计数/last r 全部已保留，且 SKIPPED 的 lsn/txId/ts_usec 与完成 last r 精确相同时，返回 `resumed_completed_offset`。孤立 SKIPPED、只有首个完整事务或只有 r/last 均不能建立基线。

初始扫描中断后的新 STARTED 开启同 generation 的新尝试；旧未完前缀以 `superseded_ranges` 保留，`ProjectionWindow.records` 包含全部证据，`changes` 只包含最终完整尝试。只有新尝试完整时才能连同旧前缀一次交给上层原子投影；不单独跳过旧事件推进 cursor。原生数据与通知的完整真实样本见 `protocol/pg-snapshot-events.json`。

## 边界

真实验收覆盖快照/精确值、同事务同键重复更新、PK变更、delete/tombstone/newkey headers、提交后丢ACK硬重启去重续接、拒收期间offset不前进、ALTER ADD事件schema、TRUNCATE。接收是实际Python API+独立SQLite inbox；PG并发/RLS另有后端测试。

该版本PG transaction.id实为 `txId:当前LSN`，事务内也变化；必须依BEGIN/COMMIT、source txId、源commit时间和ordinal/计数聚合，不能按显示ID全等。snapshot可为first，不能只识别bool。已实际验证空表、多表含空表、初始扫描中断同 generation 重扫和完成通知间隙恢复。当前基线证明限原生完整表 initial snapshot、无歧义 schema.table 标识；上游逗号列表无法安全表达包含逗号/额外点号的标识，予以拒绝。空表的完整schema仍须另有版本化发现；空表DDL、WAL过期重基线、超过预算的大快照/大事务业务投影、TOAST治理和生产性能待补。不得据局部事件推断缺行删除或自动发布本体。心跳暂关闭，未验收源提交→对象查询P95≤30秒。

`upstream/`保存固定上游许可证；本仓库不包含此前归档的依赖/JAR/镜像研究材料，公开范围见 [源码发行说明](../../third_party/README.md)。原 shade JAR 不能自动保留各依赖的全部 LICENSE/NOTICE；治理已补取无内嵌正文组件的固定上游材料，并核验旧应用的补充 notices 镜像。新增 snapshot-notifications 构建必须重新绑定应用/镜像哈希；不能沿用旧镜像材料完整性结论。JNI、适用源码与 JDK/OS 等履约仍待审，当前测试产物不标为已获商业发行批准。未运行上游完整测试。
