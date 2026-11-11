# 项目展示截图

`graph-workbench.png` 于 2026-11-11 从当前构建的真实工作台截取，1600 × 1200 像素。画面包含 4 类业务模型、22 个合成对象、30 条具名关系，以客户全景连接业务系统、数据资产、负责人和团队。

来源为 [capture_showcase.cjs](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/scripts/capture_showcase.cjs)：全新本机 SQLite、随机临时密码、真实认证 API；发布模型并创建记录后，通过真实鼠标拖动整理个人视图，选择“客户全景”显示属性。节点、边、统计和属性均来自应用，不替换页面样式、文字或接口响应。截图直接复制，未经图像加工；没有客户资料、私有 PDF、识别正文或外部图像素材。

本次捕获确认无页面错误、无横向溢出，并打开了实际对象编辑表单。它证明展示与编辑入口来自真实产品，不替代质量或规模验收。独立 [图谱浏览器回归](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/scripts/verify_graph_workbench.cjs) 另覆盖拖动、触摸、固定、恢复、节点编辑、关系新增与旧边保留、展开收起以及 320–1440px 布局；独立 [AI 浏览器回归](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/scripts/verify_ai_workbench.cjs) 使用本地合成模型验证来源跳转与导入。

先按[贡献指南](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/CONTRIBUTING.md)安装依赖、构建前端并准备可选 Playwright/Chromium，设置 `PLAYWRIGHT_MODULE` 后运行 `node scripts/capture_showcase.cjs`。结果保存在忽略目录 `var/reports/showcase/`，其中 `capture.json` 记录实际场景与尺寸。只将选定 PNG 复制到本目录；临时数据库、日志和凭据不提交。浏览器工具不属于产品运行时依赖。

`workbench.png`、`table-intake.png`、`data-graph.png` 保留此前合成场景的相关文档引用，不代表当前主视觉。
