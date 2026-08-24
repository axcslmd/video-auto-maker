# Tencent WorkBuddy 薄入口

将本包目录设为 WorkBuddy 当前任务的工作目录，然后在同一任务中输入
`/video-auto-maker` 或“开始营销视频自动制作流程”。项目级 Skill 位于
`.workbuddy/skills/video-auto-maker/SKILL.md`。

WorkBuddy 适配层只负责发现入口、执行 Python、展示产物和汇报进度。业务阶段、
确认闸门和恢复逻辑统一来自 `AGENT_ENTRY_PROTOCOL.md`、`AGENTS.md` 与
`scripts/workflow_router.py`。

首次执行 `python3 scripts/agent_entry.py prepare` 会返回本任务的
`PUBLIC_SESSION_ID` 并立即检查密钥。若返回 `BLOCKED`，WorkBuddy 必须停止引导；
优先通过宿主安全凭证输入录入并以不回显的标准输入保存到同一公共会话。宿主没有该组件时，执行 `python3 scripts/key_setup.py save --secure-prompt --session-id <PUBLIC_SESSION_ID>` 拉起本机密码弹窗。严禁在普通聊天中收集；两种安全通道都不可用时保持阻断。`QUARANTINED` 时还必须先在服务端撤销或轮换旧 key，并通过安全通道提交不同指纹的新 key，
再重跑 prepare。新任务不能读取旧任务的 key。

新任务建议先使用 Plan 模式确认多阶段范围，再在同一任务切换到 Craft 模式执行；
保持默认权限，由 WorkBuddy 在必要的脚本或网络操作前请求确认。Ask 模式只能用于
查看状态，不能作为正式生成执行路径。
