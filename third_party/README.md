# 第三方与研究来源

根目录 Apache-2.0 适用于自有项目代码。第三方组件、所附许可文本和版权通知保持原许可。

## 实际分发范围

本仓库分发自有 Python、TypeScript/React、Java 适配代码、原创 SVG、必要文档与合成测试。没有复制研究项目的应用源码，没有捆绑 Python wheels、node_modules、JAR、操作系统镜像、模型权重、词典包或其他上游源码下载树。

Python 与前端安装由 `uv.lock`、`frontend/package-lock.json` 约束；Java 依赖由适配器 `pom.xml` 声明。安装时另行从对应包仓库获取，下载内容继续适用其各自许可证。选择“仅源码发布”不表示实际部署或后续二进制分发义务自动免除。

## 保留的通知

- `frontend/public/THIRD_PARTY_NOTICES.txt`：实际锁定前端运行依赖的许可证，构建时从已安装包重新生成并随页面分发。
- `integrations/debezium/upstream/LICENSE-Debezium.txt`：固定 Debezium 上游许可文本；不代表全部 Java 传递依赖或容器已获统一放行。
- `upstream-references.json`：研究项目的固定版本和采用边界，是出处记录，不是复制授权或法律批准。

## 尚未核清的分发问题

jieba 的 Paddle 权重、飞书 SDK 内嵌 Protobuf、PyCryptodome/websockets 原生组件，以及 Docling 可选解析链、OCR 权重、Java JNI/JDK/OS 等，需要针对实际部署和交付产物逐项处理。它们未作为二进制或权重打包进入本仓库；安装依赖仍可能带入相关内容，部署者必须核对用途与条款。

已有许可清单的 pending 状态不表示发现侵权，也不应被改写为全部批准。此前商业发行审核材料及研究下载包留在开发环境，没有上传。本次源码开放不宣布完整商业发行、商标或专利清查通过。
