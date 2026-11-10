# 项目展示截图

`graph-workbench.png` 于 2026-11-10 从企业 Beta 的实际构建界面截取，画面为 1600 × 1200 像素。使用全新本机 SQLite、随机临时密码与真实认证 API，创建并发布四类模型、22 个合成对象和 30 条关系。没有客户数据、外部图片或生成式图像素材。

图中坐标通过真实鼠标拖动调整，并使用现有“保存个人视图”操作保存。节点、关系、统计和属性均来自实际 API；未替换页面文案、样式或接口响应。截图后验证了“编辑对象”打开真实表单。它展示交互能力，不是生产规模验收。

复现使用 Beta 分支的 [capture_showcase.cjs](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/scripts/capture_showcase.cjs)，先按 [贡献指南](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/CONTRIBUTING.md) 安装依赖、构建前端并准备可选 Playwright/Chromium，然后设置 `PLAYWRIGHT_MODULE` 并运行 `node scripts/capture_showcase.cjs`。该工具不属于运行时依赖。

结果保存在忽略目录 `var/reports/showcase/`，含概览、选中节点截图、捕获记录和独立合成数据库；维护者确认画面后仅复制所选 PNG 到本目录。不要提交临时数据库、日志或凭据。截图文件名固定，后续替换不需要修改中英文 README。

原有 `workbench.png`、`table-intake.png`、`data-graph.png` 保留为先前合成场景的图片及相关文档引用，不代表当前主视觉。
