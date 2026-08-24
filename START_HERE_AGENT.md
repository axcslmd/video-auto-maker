# 通用 Agent 启动入口

本文件是营销数字员工的宿主无关入口。所有兼容 Agent 都执行同一业务流程；宿主名称只用于诊断，不参与密钥、客户、审批、恢复或生成授权。

1. 读取 `AGENT_ENTRY_PROTOCOL.md`、`AGENTS.md`、`skills/video-auto-maker/SKILL.md` 和 `prompts/video-auto-maker.md`。宿主适配器不得复制或改写业务规则。
2. 在包根执行 `python3 scripts/setup_env.py full-check`。缺环境时，macOS/Linux 执行 `AGENT_INLINE_BOOTSTRAP=1 bash deploy.sh`；Windows PowerShell 执行 `$env:AGENT_INLINE_BOOTSTRAP=1; .\deploy.ps1`。若包内自带 `dist/*initready.tar.gz`，部署会先用本地加速包水合重资产。
3. 为本次对话取得稳定的公共会话 ID：执行 `python3 scripts/key_setup.py init --host-session-id <宿主稳定会话ID>`。宿主没有稳定 ID 时省略该参数并使用命令生成的新 ID。
4. 后续每次密钥命令都显式传 `--session-id <公共会话ID>`。宿主有安全凭证组件时，使用 `python3 scripts/key_setup.py save --stdin --session-id <ID>`；没有时由 Agent 执行 `python3 scripts/key_setup.py save --secure-prompt --session-id <ID>`，让客户在本机系统密码弹窗中录入。也可直接以 `python3 scripts/agent_entry.py prepare --secure-key-prompt` 启动。不要依赖环境变量跨工具继承，也不要把 key 写进普通聊天或命令参数。
5. 确定 `CLIENT` 与稳定 `run-id`，执行 `python3 scripts/workflow_api.py next --manifest <manifest.json>` 取得当前紧凑阶段包，再按其中的入口、补丁合同和确认函数推进。未知宿主安全记录为 `unknown`，核心流程必须继续可用。

若宿主支持 slash command，可把 `/video-auto-maker` 做成只读取本文件和 `AGENT_ENTRY_PROTOCOL.md` 的薄入口；若不支持，也必须在当前对话中执行同一工作流，不得要求客户先打开终端。Tencent WorkBuddy 读取 `.workbuddy/skills/video-auto-maker/SKILL.md`，说明位于 `adapters/workbuddy/START_HERE.md`。宿主专用入口不得包装成新的客户入口。新项目先执行：

```bash
python3 START_HERE_AGENT.py init --client <client> --run-id <run-id> \
  --out output/<client>/<run-id>/run_manifest.json
```

再按 `status` 输出推进，不得以 `--draft` 代替正式交付。
