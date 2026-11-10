# 项目维护约定

- 不加载、调用或遵循 `superpowers:` 技能，除非用户明确重新启用。
- 采用轻核心、按需扩展；每项抽象、依赖和校验必须对应具体需求或实际故障。保持五个默认主入口，不新增通用插件、规则、事件或审批平台。
- 后端按 `backend/ontology/` 业务模块组织；前端业务位于 `frontend/src/features/`，公共代码位于 `frontend/src/shared/`。
- 保持 `ontology.api:create_app`、`ontology.cli:main`、`ontology.worker`、`ontology.parse_task` 入口；新 ORM 模型登记到 `core/registry.py`。历史迁移不改写。
- `backend/ontology/functions/runtime.py` 是已发布 v1 的源码哈希载体，禁止格式化、整理导入或改变换行符；相邻兼容导入仍被它使用。行为变更通过新函数版本及审批发布。
- 保留来源原件、历史数据、发布契约和许可证证据。停用扩展不删除历史数据；不清理 `var/` 中未知的资产或数据库。
- 不读取、输出或提交真实凭据和客户数据。验证使用独立临时目录、合成账号和明确的测试数据库；不默认连接已有服务。
- 后端测试按领域组织，跨用例辅助函数放在 `backend/tests/support/`，不得从 `test_*.py` 或 `conftest.py` 导入。前端测试与功能同目录。
- 验证命令和源码清单维护见 [CONTRIBUTING.md](CONTRIBUTING.md)。只声明实际运行的测试，不把跳过、模拟或局部通过写成完整验收。
- 修改现有文档中的事实，避免追加互相冲突的说明；早期记录保留为历史。当前能力见 `docs/enterprise-beta.md`，实测边界见 `docs/beta-verification.md`。
