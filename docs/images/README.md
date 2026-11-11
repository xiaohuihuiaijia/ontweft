# 项目展示截图

`graph-workbench.png` 于 2026-11-11 从实际构建的深色图谱工作台截取，画面为 1440 × 1411 像素。使用全新本机 SQLite、随机临时密码与真实认证 API，创建并发布四类模型、18 个合成对象和 28 条关系。全部内容为合成场景，不含客户数据、私有 PDF、模型识别正文、外部图片或生成式图像素材。

本图展示实际初始布局。节点、关系、统计和属性均来自真实 API，未替换页面文案、样式或接口响应。同一次浏览器验证随后完成鼠标及触屏拖动、节点固定、个人视图恢复、单击编辑并保存对象、关系修改、键盘操作与邻域展开/收起；320–1440px 布局无横向溢出，页面与画布错误均为零。这是交互与集成验证，不是生产规模或真实业务数据质量验收。

复现使用 [verify_graph_workbench.cjs](../../scripts/verify_graph_workbench.cjs)，先按[贡献指南](../../CONTRIBUTING.md)安装依赖、构建前端并准备可选 Playwright/Chromium，然后设置 `PLAYWRIGHT_MODULE` 并运行 `node scripts/verify_graph_workbench.cjs`。浏览器工具不属于产品运行时依赖。

结果保存在忽略目录 `var/reports/graph-workbench/`，含截图、实际通过项与 `result.json`；临时合成数据库在验证结束后清理。本图直接复制自其中的 `graph-overview-1440.png`，未经图像加工。不要提交临时数据库、日志或凭据。展示文件名固定，替换不需要修改中英文 README 引用。

原有 `workbench.png`、`table-intake.png`、`data-graph.png` 保留为先前合成场景的图片及相关文档引用，不代表当前主视觉。
