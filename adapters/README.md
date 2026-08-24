# 宿主 Agent 薄适配层

这里仅存放宿主发现与启动说明，不复制业务流程。所有宿主最终委托：

1. `AGENT_ENTRY_PROTOCOL.md`
2. `scripts/workflow_api.py next`
3. `scripts/workflow_mcp_server.py`（支持 MCP stdio 时）

自动发现文件因宿主约定必须保留在根目录的隐藏目录：

- Codex：`.codex/prompts/video-auto-maker.md`
- Kilo：`.kilo/command/video-auto-maker.md`
- WorkBuddy：`.workbuddy/skills/video-auto-maker/SKILL.md`

人工启动说明：`hermes/START_HERE.md`、`workbuddy/START_HERE.md`。宿主名称只用于诊断和入口发现，不能成为审批、恢复、密钥读取或生成授权条件。
