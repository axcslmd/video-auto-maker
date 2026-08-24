# AI 营销视频 Agent 深度复审与修复报告

> 历史审查快照：本文件中的测试数量与协议口径保留 2026-08-18 当日结果；当前架构指标和质量基线见 `architecture/CODE_QUALITY_REVIEW_2026-08-19.md`。

日期：2026-08-18  
审查范围：业务路由、阶段状态机、客户确认合同、付费生成、素材引用、模型降级、并发/幂等、OCR/QC、交付、多宿主兼容、CLI/文档一致性、发布包。

## 结论

项目原有问题不是单一函数缺陷，而是“业务意图→状态机→确认证据→付费请求→验收交付”之间存在多处合同分叉。本轮已将公共流程收敛到 host-neutral 权威合同，修复所有本轮发现的 P0/P1 问题，并通过 117 个全量单元/回归测试、7 类业务 profile、19 个阶段、36 个业务评测样例复核。

代码层面已达到“对未授权变更失败关闭、对已付费任务可恢复、对客户确认可追溯、对交付产物可校验”的状态。由于当前环境未提供会话级 BasicRouter 密钥，本轮未执行会产生费用的真实出图/出片；供应商实际视觉质量、延迟和实时模型可用性仍属运营验收项，不是本地代码可伪造的通过项。

## 一、业务流程与状态机

### 已发现问题

1. 路由器、manifest、提示词和个别脚本重复推断业务 profile，文档关键词可将实物产品流程误分为文档讲解。
2. 产品展示与人物+产品流程对 `product_usage` 的要求不一致，可造成产品纯展示永远等待不存在的人物板。
3. `derive` 跳过态不是终态，路由可重复要求客户确认一个未生成的派生版。
4. 通用 `finish-stage` 可绕过视觉板、视频、字幕、终片等专用闭包验证。
5. 交付文件生成后 manifest 没有稳定的 delivered 终态。

### 修复

- 新增 `scripts/workflow_contract.py`，统一 7 个 profile、19 个阶段、可选阶段条件和三个售卖包绑定；当前协议版本为 `2026-08-18.5`。
- 正式 plan 必须显式绑定 `oral-broadcast` / `explainer` / `brand-tvc`，不允许“推断 profile 等于客户购买授权”。
- 根据客观素材事实决定产品板/人物板/使用图；产品无人物时不再伪造使用图阶段。
- 建立 `begin → generate → finish → customer approve` 事务边界，专用阶段禁止通用 finish。
- `derive` 支持可校验 skipped 终态；交付成功后写入哈希绑定的 `delivery` 并进入 `delivered`。

## 二、客户确认与付费请求合同

### 已发现问题

1. 视频提示词审批曾只绑定基础文本，生成前还可追加模型条款、负向提示、续接方式或参考图。
2. 较短 CTA 更改不一定使旧审批失效。
3. `--seed`、`--negative`、`--chain`、draft `locked_refs` 可在客户确认后改变实际提交。
4. 旧单段 `--text` 入口、多个旧的单值 CLI 参数可绕过 batch/handoff 闭环。
5. 交接指纹未完整绑定 reference tag/role/intent、原始 urls 和所有可影响提交的字段。

### 修复

- `prompt_review.py capture-video` 现在生成主模型和全部可降级模型的精确提交文本、引用包、故事板模式、panel index 和续接变体；正式生成逐字使用已确认文本。
- 新增完整 submission source fingerprint，对短 CTA、语言、声音合同、运镜、故事板、参考图、负向提示、seed 和 chain 变化都失败关闭。
- 正式与草稿的付费生成都需要已确认 prompt review；禁用旧单段入口和事后 CLI 引用覆写。
- `artifact_contract` 视频交接 schema 升级到 v6，绑定引用语义、字节身份与所有创作源字段。

## 三、故事板、人物板和产品使用几何

### 已发现问题

1. 历史逻辑仍有固定 12 格的补齐/合并痕迹，可改变已确认剧本分镜数。
2. Kling 回落路径可在付费任务启动后才发现缺单格展开图。
3. 单格 QA 回复缺少检查项时仍可能被解读为通过；人工 QA 缺少当前图片哈希绑定。
4. 尾帧串联加入第五张参考图时，运行时会静默丢掉已确认的身份/产品锚。

### 修复

- 故事板格数仅来自 `len(shots)`；移除 12 格补齐、复制、截断和过度合并。
- 新增视频 `--preflight` 和 `panel_expansion.py` 闭环：`gpt-image-2` img2img 展开、七项 QA、preview 展示、客户原话确认、panel/recipe/QA 三重哈希绑定。
- 自动 QA 必须显式通过 `photorealistic/no_grid_or_text/identity/product/interaction/composition/no_extra_people` 七项；人工 QA 必须绑定 panel SHA-256、reviewer、reviewed_at、note 和七项 true。视频消费端也再验一次，不信任手工改写的 `pass:true`。
- 串联前预留尾帧图槽；超过模型引用上限时在任何前置片段扣费前阻断，正式与草稿均不得静默丢图。

## 四、模型选择、降级与音画一体

### 已发现问题

1. 模型降级文档和实际音频能力不一致，可将有台词段落回落到不支持集成音频的模型。
2. batch 和 chain 的 model-not-found/privacy fallback 行为不一致。
3. chain 降级成功后覆写 task 记录，丢失实际已提交提示词快照，甚至可沿用上一段 task 变量。
4. 真人隐私晚回落可在客户确认 Kling 单格前继续付费提交。

### 修复

- 模型选择优先使用实时 capability catalog；有台词的正式段只能选网关明确声明集成音频的模型。
- batch/chain 都支持 model-not-found 受控降级，且必须存储实际目标模型的已确认精确提示词快照和降级原因。
- chain 每段循环开始清空 task 局部状态，成功后仅增量更新记录，不再丢快照。
- 同步或轮询期间发生隐私回落时，先准备 Kling 所需 panel 和精确提示词；缺客户证据则记为 `fallback_approval_required`，保留原 task 供恢复，不发起第二次付费请求。

## 五、并发、幂等、预算和中断恢复

### 已发现问题

1. 多 Agent/多进程可在远程 taskId 写入前重复提交付费任务。
2. 提交超时可能是“远程已接收、本地丢失响应”；自动增加 attempt 会制造新 request ID 和重复扣费风险。
3. chain 未执行 batch 预算闸门；batch 在预算检查前已获取生成锁，预算拒绝后留下假运行态。
4. chain 在中途失败后可继续消耗后续片段。

### 修复

- 付费 storyboard/video 阶段使用 owner lease + heartbeat，并使用稳定 handoff/attempt/dependency 生成确定性 request ID。
- 模糊提交失败不再自动创建新 attempt；重试复用同一 attempt/request ID。只有客户拒绝 take 或已验收 take 需替换时，才通过 `new-video-attempt` 显式授权新付费尝试。
- batch 与 chain 统一在获取生成锁前完成素材、模型、故事板、引用容量和预算检查。
- chain 任一前置段失败立即终止后续段，并每段原子写入结果，支持中断恢复。

## 六、OCR、QC、take 验收与交付

### 修复结果

- 正式流程禁止全局 `allow_ocr_warning`；waiver 必须绑定精确 `segment_id + take_fingerprint + OCR texts`。
- OCR 不可用时，人工 clear 必须绑定至少 12 个唯一帧 SHA-256，包含首尾帧。
- 禁用无验收闭环的旧 `--candidates N`/best-of-N 视频路径；每个 take 必须具有任务、提示词、素材、媒体 QC、OCR、客户 review 和当前哈希。
- 测试片段建立“生成完成→技术验收→客户确认”闭环；正式 video/final/delivery 都校验当前文件哈希和上游交接指纹。

## 七、多宿主兼容、密钥与文档/代码冲突

### 修复结果

- 公共业务仅使用 `BASICROUTER_*` 协议；Codex/Kilo/Hermes/WorkBuddy 只保留薄适配和诊断元数据，未知宿主安全回落 `unknown`。
- 密钥禁止位置参数，仅接受 `save --stdin --session-id`；新会话不读取旧全局密钥缓存。
- 清理文档中已停用的 `video_engine --text/--type/--urls/--resolution/...` 用法；视频类型、引用、清晰度和台词改为在审批前写入 segments。
- 清理包定义中与 take 验收冲突的 best-of-N 声明，统一 `gpt-image-2` 故事板/人物板规则与动态 N 格合同。

## 八、验证矩阵

| 验证项 | 结果 |
|---|---:|
| Python compileall | 通过 |
| unittest discover | 117/117 通过 |
| workflow router | 19 stages / 7 profiles，0 错误（历史快照；当前 23 阶段见 2026-08-19 报告） |
| module package boundary | 3 个售卖包，0 错误 |
| skill/code contract | 11/11 绑定，0 警告 |
| business eval fixtures | 36/36 通过，覆盖 13 类场景 |
| offline setup | darwin-arm64 / Python 3.9 通过 |
| dependency/runtime check | Python、ffmpeg、HyperFrames、Remotion、Chrome、OCR 就绪 |

## 九、运行前提

- 当前会话 `BasicRouter key` 状态为 `MISSING`；这不影响离线代码、状态机和交付包验证，但会正常阻断真实付费生成。
- 首次真实业务验收应选一个最小客户 run，在密钥 gate 通过后逐步完成图板、单格回落、1 段测试片、OCR/QC、客户验收和交付；不应用未实际调用供应商的本地测试替代视觉质量验收。

## 十、后续非阻塞性优化建议

以下不是本轮未修复缺陷，而是运营成熟度增强项：

1. 在 CI 中固定执行本报告的全部验证矩阵，并要求 `RELEASE_VALIDATION.json --require-current` 通过后才可打包。
2. 在有密钥的预发布环境建立“不生成客户数据”的供应商 canary，定期验证 catalog 能力字段、request-id 幂等、隐私拒绝语义和任务恢复。
3. 用真实项目的非敏感匿名指标补充供应商延迟、失败率、OCR 警告率、panel 返工率和每成片成本的运营基线。
