# LLM 无关 Agent 运行时：三轮最佳实践对照与修复记录

日期：2026-08-18  
范围：宿主适配、模型输出、工作流恢复、审批安全、可观测性、离线发布。  
不变量：阶段集合由 `workflow_contract` 与可执行 pseudo-stage 唯一派生（当前 23 阶段），保留 7 条业务链、现有客户确认闸门、生成模型与素材依赖规则；发布与文档不得另行硬编码阶段数。

## 第 1 轮：不同 LLM 的结构化输出与创作边界

对照来源：

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Anthropic strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)
- [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [MCP Tools 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

发现的缺陷：不同供应商的严格 Schema 子集、工具调用回包和拒绝/截断语义不一致；模型名称曾承担了过多能力推断；自由文本可直接污染执行结构；故事板专业字段在旧计划上只能告警。

完成的修复：

- 新增基于网关能力与实测分数的模型行为画像；未知模型安全回退为单项原子补丁。
- 建立跨供应商 strict-schema/tool/JSON 三级策略，只进行一次有界修复；拒绝和截断不触发付费回退。
- 建立事实、简报、脚本节拍、镜头补丁、故事板计划五层 IR；LLM 只提交受限创作补丁，确定性编译器拥有执行结构。
- 生产计划必须通过严格 Schema、事实引用、素材引用、镜头差异和专业字段校验；历史计划保留兼容模式。
- 动效决策与 Remotion/HyperFrames 执行轨分离，模型不能直接生成或改写执行代码。

## 第 2 轮：薄入口协议、恢复、幂等和授权

对照来源：

- [MCP Lifecycle 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- [MCP Tools 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP Tasks 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
- [OWASP AI Agent Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html)

发现的缺陷：通用 Python API 已存在，但支持 MCP 的宿主没有标准发现入口；协议初始化、能力协商、输入验证、速率限制、写操作幂等与敏感输出清洗尚未形成统一边界；预览路径可能被污染为项目外路径。

完成的修复：

- 新增零第三方依赖的 MCP stdio 薄入口，支持 2025-06-18/2025-11-25 版本协商、初始化状态机、ping、tools/list 和 tools/call。
- MCP 工具统一声明 inputSchema、outputSchema 与只读/幂等/开放世界提示；业务逻辑全部委托给 `workflow_api.py`。
- 写工具强制稳定 `request_id`，持久化输入指纹和结果票据；同键同输入安全回放，同键异输入阻断。
- 增加 1 MiB 协议消息限制、项目内 JSON 路径约束、文件大小限制、调用速率限制和密钥模式清洗。
- 客户确认与技术确认保持两套动作；MCP 不能把技术校验伪装成客户批准。
- 阶段包和审批素材接口不再读取或哈希项目目录外的路径。

## 第 3 轮：可观测性、发布证据和归档安全

对照来源：

- [OpenTelemetry Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)
- [SLSA Build Track Basics 1.2](https://slsa.dev/spec/v1.2/build-track-basics)
- [SLSA Provenance](https://slsa.dev/spec/v1.1/provenance)
- [Reproducible Builds](https://reproducible-builds.org/)
- [OWASP AI Agent Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html)

发现的缺陷：协议调用缺少统一 trace/severity/event 结构；现有 ZIP 继承源文件时间戳，无法稳定复现；目录校验不等于归档校验，恶意路径、重复成员、异常链接和归档内秘密需要独立阻断；测试结果未与待发布源码指纹绑定。

完成的修复：

- 新增 OpenTelemetry 形态的 JSONL 审计事件：时间、观察时间、trace、严重度、事件名、资源与安全属性；属性采用短标量白名单，不记录提示词、客户原话或密钥。
- ZIP 使用排序成员、固定/`SOURCE_DATE_EPOCH` 时间戳和规范化权限；相同输入与相同发布报告产生相同归档字节。
- 新增归档内验证：单一根目录、路径穿越、重复成员、加密成员、条目/解压体积上限、符号链接逃逸、文件大小/SHA-256、秘密扫描和目录清单一致性。
- 新增发布审计：语法、当前权威阶段合同/7 链路、11 个 Skill、5 个宿主入口、模块边界、36 个业务评测、离线运行时和全量测试必须全部通过；阶段数从合同动态读取，不能硬编码。
- 发布报告绑定当前通用运行时源码指纹；源码发生任何可交付字节变化后，旧报告自动失效并阻断正式打包。

说明：发布报告采用 SLSA 的“制品、构建步骤与材料摘要可追溯”思想，但本地单机打包不宣称达到某个正式 SLSA 等级；正式等级还需要受信任构建平台和外部可验证证明。

## 第 4 轮：产物驱动的客户体验与确认证据

发现的缺陷：内部阶段数量直接暴露给客户；策略检查把“没有上下文”当作不适用；“文件存在”无法证明客户实际看过当前版本；流程 profile 与售卖 package 一对一硬绑定，无法表达“同一制作链服务不同已购套餐”。

完成的修复：

- 新增可执行产物注册表，从现有 manifest 派生依赖边、ready action、成本类别、四个里程碑和变更影响预览，不另建第二份业务状态。
- 新增 Experience Packet V2，客户层默认只展示“方案定稿→素材与预演→生成与验片→后期与交付”，保留当前 23 个阶段用于执行与审计。
- 审批改为“展示回执→客户原话确认”两步；回执绑定文件 SHA-256、字节数、client/run/stage 和上游审批指纹，变更后旧回执立即失效。
- 策略决策增加 allow/deny/indeterminate 三态；正式付费提交对证据不足 fail-closed，并实际读取 manifest 的当前审批和指纹。
- profile 保留为执行链，package 改为显式授权集；`product_with_human` 可在口播包或 TVC 包中运行，显式入口仍会缩窄授权边界。

## 最终验证标准

1. 业务路由以 `workflow_contract` 与可执行 pseudo-stage 合并结果为唯一真相源（当前 23 个阶段、7 条业务链），发布审计不得另行硬编码阶段数。
2. 所有 11 个 Skill 绑定代码合同，5 个宿主适配均只做薄委托。
3. 36 个业务评测覆盖授权、事实、物理关系、素材身份、并发、恢复、付费生成、QC 与交付。
4. 单元/契约测试零失败；不得删除断言、放宽 Schema 或使用跳过标记换取通过。
5. 离线运行时按平台、Python ABI、wheel tag 与 SHA-256 校验。
6. 正式包必须同时通过目录清单复验和 ZIP 内部复验，并携带当前源码指纹对应的 `RELEASE_VALIDATION.json`。

## 最终目录收口补充

最终交付不再沿用历史平铺目录：客户、交付、架构文档进入 `docs/`，人工宿主入口进入 `adapters/`；`tests/` 与 `evals/` 一并交付；Remotion 临时 `_props*.json` 被过滤。`agent-project-layout.v1` 会阻断缺失必需目录、旧文档回到根目录、意外根目录文件、测试缺失或临时渲染输入泄漏。`scripts/` 仍是业务内核的稳定 CLI 根路径，这是兼容性合同，不是未完成的目录迁移。
