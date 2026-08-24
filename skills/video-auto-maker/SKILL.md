---
name: video-auto-maker
description: 启动或恢复本项目的 AI 营销视频流程，并严格按代码返回的阶段、素材、确认和交付合同推进。用于客户输入 /video-auto-maker、提出营销视频需求、恢复既有 run，或需要在 Codex、Kilo、Hermes、WorkBuddy 及未知兼容宿主之间继续同一任务时。
---

# BasicRouter 视频流程

把本 Skill 当作宿主无关入口说明，不把它当作另一份工作流真源。

## 启动

1. 定位同时包含 `AGENT_ENTRY_PROTOCOL.md`、`AGENTS.md`、`START_HERE_AGENT.py` 和 `scripts/workflow_router.py` 的包根目录。
2. 完整读取 `AGENT_ENTRY_PROTOCOL.md` 和 `AGENTS.md`。
3. 执行 `python3 scripts/agent_entry.py prepare`，保存它返回的 `PUBLIC_SESSION_ID`。宿主没有原生安全凭证组件时改用 `python3 scripts/agent_entry.py prepare --secure-key-prompt`，让入口在缺 key 时直接拉起本机系统密码框。该命令先对当前会话执行密钥闸门，再做环境准备。
4. 返回 `BLOCKED` 时，优先使用宿主安全凭证输入并以不回显的标准输入执行 `python3 scripts/key_setup.py save --stdin --session-id <PUBLIC_SESSION_ID>`；宿主没有该组件时，Agent 执行 `python3 scripts/key_setup.py save --secure-prompt --session-id <PUBLIC_SESSION_ID>`。以同一 ID 重跑 prepare，显示 `SAVED`/`STORED` 后才能继续。严禁在普通聊天中索取或接收 key；两种安全通道均不可用时保持阻断。`QUARANTINED` 还要求先在服务端撤销/轮换旧 key，并通过安全通道提交不同指纹的新 key。不得从旧会话或其他宿主任务复用 key。
5. 未知客户时只询问一次品牌英文代号，再创建或恢复稳定 run。

## 按代码推进

已有 manifest 时先取得紧凑阶段包：

```bash
python3 scripts/workflow_api.py next --manifest <manifest.json>
```

只调用阶段包 `entry_functions[]` 中列出的入口，并使用 `confirm_function`
关闭阶段。`guidance_index[]` 只是需要深入该阶段时的精准索引，不要每轮把全部长文档
重新放进模型上下文。创意修改必须按 `interaction_contract.response_schema` 提交，并通过
`workflow_api.py submit-patch` 或 `refine` 原子写入；不得让模型直接重写 manifest、审批证据
或完整故事板合同。不要从本 Skill 的文字、宿主名或历史记忆推断下一阶段。

没有 manifest 时从 `idea -> brief` 开始；不要因为宿主不支持 Slash Command 就让客户打开终端。

## 不变量

- 把宿主名只写入诊断或交接历史；不用于密钥、审批、模型、恢复或生成授权。
- 每个阶段必须同时具备输入产物、代码入口和确认/审批证据，缺任一项都不算完成。
- 严格逐阶段生成产品板、人物板、产品使用图和故事板，不一次性生成全部素材。
- 正式付费生成前展示完整提交提示词并取得客户原话确认。
- 强模型只能增加每轮可处理项数，不能减少任何确认闸门；未知模型按原子补丁运行。
- 展示图片和视频时使用脚本返回的绝对路径；长任务持续报告进度。
- 对 Skill 与代码是否一致有疑问时执行：

```bash
python3 scripts/skill_contract.py validate
python3 scripts/skill_contract.py contract --skill video-auto-maker
```

以命令返回的代码合同为准。
