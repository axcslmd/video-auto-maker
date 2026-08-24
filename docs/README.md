# 文档中心

文档按读者和职责分层；运行时协议、Prompt、Schema 与代码不放在这里，避免说明文档与机器合同混淆。

| 目录 | 面向对象 | 内容 |
|---|---|---|
| `customer/` | 客户与实施人员 | 客户使用、客户资料接入 |
| `operations/` | 交付与运维人员 | 交付清单、交付包说明 |
| `architecture/` | 技术负责人 | 项目概览、代码质量审计、能力展示 |

渲染前质量编译、Design Token v3、跨画幅布局和不可变 RenderSpec 见
[`architecture/RENDER_QUALITY_COMPILER.md`](architecture/RENDER_QUALITY_COMPILER.md)。

Agent 行为入口仍位于项目根目录的 `AGENTS.md` 和 `AGENT_ENTRY_PROTOCOL.md`；宿主适配说明在 `adapters/`；可被运行时按阶段加载的专业资料保留在 `references/`。
