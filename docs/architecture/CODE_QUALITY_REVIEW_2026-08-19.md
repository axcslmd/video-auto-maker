# 代码架构与质量治理报告

日期：2026-08-20
范围：`scripts/` 公共业务运行时、状态/审批合同、宿主适配接口、生成编排、确定性渲染、QC 与发布闸门。

## 结论

本轮已完成第一、二阶段治理，并把持续实测报告中的 NK‑48 至 NK‑115 逐项反推到公共代码合同；另外提前修复 Canary 通过后无法晋级正式批次的状态迁移死锁。权威执行面现为 23 个阶段（包含文档讲解身份图的四个独立 pseudo-stage）、7 条业务链、三个售卖包和 Agent 无关的 `BASICROUTER_*` 协议。

当前静态审计结果：135 个 Python 模块、68,280 行脚本代码、0 个内部导入环、0 个审批状态越权写入、0 个宿主专属业务条件、0 个大型函数热点、0 个宽泛异常捕获；284 项全量回归和 36 个业务评测夹具通过。复杂度与宽泛异常预算均为 0，新增同类债务会直接阻断发布。

## 分层架构与依赖方向

| 层 | 主要模块 | 职责 | 允许依赖 |
|---|---|---|---|
| 宿主适配 | `agent_entry`、`workflow_api`、`workflow_mcp_server` | 把 Codex/Kilo/Hermes/WorkBuddy 能力映射为公共协议 | 编排层、合同层；不得拥有业务状态 |
| 流程编排 | `workflow_router`、`pipeline`、`stage_packet` | 决定当前阶段、动作、事务与 Experience Packet | 合同层、创作 IR；不得绕过审批 API |
| 状态与合同 | `workflow_contract`、`run_manifest`、`policy_check`、`artifact_registry`、`review_task` | 阶段、身份、审批、失效、证据、付费准入 | 只依赖基础工具与 schema；是业务真相源 |
| 创作中间表示 | brief、script、storyboard plan、segments、motion spec | 把客户意图编译成可验证结构化合同 | schema、事实与引用合同 |
| 生成适配 | `br_client`、`storyboard`、`video_engine` | 模型选择、提交、轮询、恢复、回落 | 合同层、任务/成本账本；不得修改已确认语义 |
| 确定性渲染 | Remotion、HyperFrames、`final_edit`、`fuse` | 运镜、排版、字幕、特效、拼接与多比例派生 | 已确认媒体与冻结 spec |
| QC 与交付 | `media_qc`、`take_review`、OCR、delivery | 技术验收、客户验片、waiver、交付封装 | 当前哈希、上游审批与任务证据 |

核心依赖原则是“适配层向内调用，合同层不反向认识宿主”。无法识别宿主时记录为 `unknown`，不得因此改变审批、密钥、恢复或生成授权。

## 状态所有权

| 状态 | 唯一/受控所有者 | 原因 |
|---|---|---|
| 阶段集合与 profile | `workflow_contract.py` | 防止不同入口各自发明阶段或业务分支 |
| `approvals`、`approval_hashes`、`approval_evidence` | `run_manifest.py` | 原子失效并同步清理下游证据 |
| 展示凭证创建 | `review_task.py` | 绑定客户真正看到的文件字节与上游审批指纹 |
| 展示凭证失效 | `run_manifest.py` | 上游变化时与审批状态在同一事务内清理 |
| 外部生成任务 | generation/task ledger | 支持幂等提交、中断恢复和付费任务追踪 |
| 成本授权 | cost ledger + `policy_check.py` | 付费请求必须 fail-closed |
| 路径与原子文件操作 | `project_utils.py` | 统一目录包含、锁、原子写和文件身份 |

## 本轮已修复的代码质量问题

1. 审批失效逻辑曾散落在多个编排模块，容易只清布尔值、不清哈希或展示凭证。现在统一使用 `invalidate_approvals` / `invalidate_from`，并增加精确保留无关审批物的测试。
2. 客户确认证据曾允许业务模块直接修改 manifest。现在通过 `bind_customer_approval_evidence` 校验阶段、原话、来源与审批路径后统一写入。
3. `workflow_review_present` 曾先修改调用方对象再保存；CAS 冲突会留下“仅内存存在”的展示凭证。现在先修改深拷贝，磁盘原子提交成功后才回写调用方。
4. 文件 SHA-256 在 API、review 和 stage packet 中重复实现。现在统一使用 `project_utils.sha256_file`，保持流式读取并避免大文件一次性载入。
5. 图像模型发现、选择和模型 JSON 提取在两个工具中重复且异常边界过宽。现在复用 `br_client` 与 `text_provider` 的公共实现，并把可预期异常收窄到具体类型。
6. 新增 `architecture_audit.py`，在发布审计中强制检查导入环、非法阶段、状态越权写入、宿主业务耦合、复杂函数和宽泛异常预算。
7. 文档中的 12 个客户里程碑、48 项测试等旧口径已与 Experience Packet V2 的 4 个里程碑和当前测试基线对齐。
8. 文档讲解新增身份引用、精确请求捕获、异步生成、媒体/视觉复核和客户确认的完整状态机；失败恢复复用同一 task，不会凭“看起来已失败”重复扣费。
9. 视频能力预检形成哈希绑定合同：实际 provider `modelName`、输入模态与内嵌输出音频的分离证据、引用容量、隐私回落、尾帧槽和 segment 源指纹一次冻结；`multimodelTypes` 含 audio 不再被误当作 MP4 必有音轨，素材、台词或 chain 改动都要求重新预检和确认。
10. 视频 worker 统一通过 settlement 事务收口：成功进入逐段验片，下载/超时/普通失败进入原 task 恢复，隐私拒绝进入新合同审批；所有路径清理 lease 并保留 task、provider error 和未知计费证据。
11. `capture_video_segments` 复用单份能力合同，不再按镜头和候选重复联网；相关回归耗时从约 152 秒降至约 0.13 秒。
12. `status` 与 `contract` 复用同一视频事务路由，冻结的业务 profile 优先于任务历史关键词；时长改为解析明确数值与单位，避免 `60s` 因包含字符“6”被误判为 6 秒快剪。
13. 已完成 provider 任务返回的标准 HTTP VOD 地址只在受信响应边界升级为 HTTPS；通用下载器仍拒绝 HTTP、凭据 URL 与自定义端口，不以恢复便利换取安全边界。
14. 新 attempt 启动前把当前 generation 事务写入按阶段、去重且带指纹的历史；旧 task ID、结算、恢复和未知计费证据不会被新的 `running` 槽覆盖。
15. 多段含音频项目自动加入一个可复用 `test_segment`：未知输出音频能力的路由只能先做 canary；真实音轨 QC、正式台词/口型验片和客户确认通过后，原子晋级事务才会退休测试专用策略、绑定非静音证据、复用已完成 task，并强制重新确认完整 batch。
16. 单段媒体 QC 失败不再异常中止整批；所有已付费 task 都会继续完成下载/QC 并统一 settlement，避免留下 running lease 或缺失证据。
17. 模型目录不再把跨 provider 复用的 `modelId` 合并为一条记录：每个唯一 `modelName` 是独立 route，共享 ID 仅为歧义 alias；显式首选歧义立即阻断，次级 fallback 歧义安全跳过。preflight v8、提示词审核、音频 canary 与提交重试都绑定同一唯一 route，模型不存在重试也不会退回共享 ID。
18. provider 成功但媒体 QC 失败的 take 不再被当成可继续下载的成功任务；结果绑定精确 task/model/handoff/take/QC，并明确要求 rejected review、新 attempt 授权和独立输出路径，旧失败媒体永不被覆盖。
19. 恢复 canary、隐私拒绝即停策略与 attempt 输出合同都进入 handoff v7；失败 test worker 使用专用 settlement 清 lease、保存 provider 证据并路由到 `recover_test_segment`，高频恢复 CLI 只输出紧凑摘要。
20. 实时模型目录完整保留分辨率、输入模态和输出模态；同一目录快照贯穿选择、preflight、fallback 与 paid submit。声明不支持的分辨率或 image/video 模态会在 `createVideo` 前失败关闭。
21. `status` 与 `contract` 在恢复状态合并静态流程来源和动态失败结果，客户与恢复器均能看到同一份精确证据。提示词预览、能力预检与提交恢复再次拆分，架构大函数预算保持为 0。
22. 密钥只允许经 stdin 进入会话缓存，CLI/日志输出保持脱敏。聊天渠道暴露的值会按 SHA-256 指纹进入权限 `0600` 的全局隔离表；同值跨 session、环境变量或兼容读取均失败关闭，不同新值必须在线验证后才可解锁。供应商侧撤销与轮换仍由操作者完成。
23. recovery privacy 合同改为 post-split finalizer：只有当前 segments SHA、aggregate SHA、canary handoff 与已确认模板同时匹配才可绑定；旧预绑定、`do_not_bind` 和 superseded 文件全部拒绝。
24. 音频 Canary 通过后新增显式 `promote-recovery-audio-canary` 事务；测试 artifact 保留原 handoff，正式 handoff 绑定 proof，已付费 task 仅在 provider 请求语义等价时克隆到新 handoff，旧 prompt review 必然失效。
25. JSON-only brief 不再形成“可审批但不可展示”的死路：`approval_preview.py` 从当前 brief 确定性编译客户可读 Markdown，采用内容寻址文件避免 CAS 冲突覆盖已审批字节，并把源 JSON 与预览同时登记为审批输入。展示回执升级为 v2，除可见文件和上游审批外还绑定完整阶段输入指纹；隐藏 JSON、预览或上游任一变化都要求重新展示。

## 剩余风险与优先级

### P1：供应商运行时事实会变化

模型目录、provider `modelName`、模态与图片容量属于实时事实。本地代码已改为显式证据优先、提交前重新校验和不匹配失败关闭，但离线测试不能证明某天的在线路由仍可用。预发布 canary 需要持续验证目录字段、request-id 幂等和任务恢复，不能把上次成功缓存成永久能力。

### P1：视觉质量必须经过真实验片

预渲染编译器已控制安全区、排版容量、字幕禁则、身份/产品锚和编码规格；这些只能减少可预防缺陷，不能替代对模型生成的人脸、手部、物理关系、口型和动态纹理的逐 take 验收。正式流程仍必须展示真实媒体、执行 QC/OCR，并绑定客户选择。

### P2：模块物理体积仍大，但复杂函数债务已清零

`storyboard.py`、`script_splitter.py`、`run_manifest.py` 和 `video_engine.py` 仍是大型领域门面；它们的 CLI、长链编排、状态事务和提示词策略已拆到独立模块，架构审计当前无热点。后续继续采用兼容门面 + 小模块迁移，不进行一次性重写，也不允许借拆分复制第二份状态真相源。

### P2：测试文件可继续按领域分组

284 项测试运行快速且覆盖状态迁移、CAS、模型回落和付费阻断；少数回归文件仍较大。后续可按 contracts/workflow/generation/rendering/delivery 迁移测试，但不得牺牲现有断言或发布预算。

## 分阶段实施方案

1. 基线与低风险拆分（完成）：架构审计、零债务预算、状态所有权、纯校验器、CLI facade 和发布闸门。
2. 生成编排拆分（完成）：视频 chain runner、batch 任务生命周期、模型能力/提示词/任务证据和 settlement 分层。
3. 预渲染质量治理（完成）：结构化视觉意图、布局容量、安全区、字幕禁则、身份底片和确定性渲染合同。
4. 运营验证（持续）：在有有效 BasicRouter 密钥的预发布环境运行匿名 canary，测真实模型可用性、延迟、回落、OCR 与最终渲染；本地测试不能替代这一步。

当前完成定义：公开 CLI 与 schema 兼容；架构审计零新增债务；284 回归、36 业务评测、离线 canary、23/7 工作流合同和当前源码绑定的发布报告全部通过；涉及视觉输出时仍需真实预演/成片的人工视觉验收。
