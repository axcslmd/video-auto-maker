---
name: video-auto-maker
description: This skill should be used when a Tencent WorkBuddy task starts or resumes the packaged marketing-video workflow, including requests for /video-auto-maker, digital-human videos, product videos, explainers, or brand TVCs.
agent_created: true
disable: false
---

# WorkBuddy thin adapter

Treat this file only as WorkBuddy discovery metadata. Keep all business behavior in the shared package.

1. Set the task workspace to the package directory containing `AGENT_ENTRY_PROTOCOL.md` and `START_HERE_AGENT.py`.
2. Read `AGENT_ENTRY_PROTOCOL.md`, `AGENTS.md`, and `skills/video-auto-maker/SKILL.md` completely.
3. Run `python3 scripts/agent_entry.py prepare` from that package directory and retain its `PUBLIC_SESSION_ID` as task state. When WorkBuddy has no native secure credential component, add `--secure-key-prompt` so the entry opens an OS password dialog if the task has no key. A native host credential is piped through non-echoing stdin to `python3 scripts/key_setup.py save --stdin --session-id <PUBLIC_SESSION_ID>`; the OS fallback uses `python3 scripts/key_setup.py save --secure-prompt --session-id <PUBLIC_SESSION_ID>`. Rerun prepare with the same ID. Never request or accept a key in ordinary chat, command arguments, logs, or project files. On `QUARANTINED`, require server-side revocation/rotation and a different replacement fingerprint through a secure channel. If both secure channels are unavailable, remain blocked.
4. Use `python3 scripts/workflow_api.py next --manifest <manifest.json>` for every resumed step. This stable API delegates business routing to the sole source of truth, `scripts/workflow_router.py`. Read only the deeper file selected from `guidance_index[]` when the current stage actually needs it.
5. Execute only the returned code entry and confirmation functions. Submit creative changes through the returned Patch contract. Keep WorkBuddy identity diagnostic-only.

Use WorkBuddy Plan mode to review a new multi-stage task and Craft mode for approved execution. Keep Default Permissions enabled; request the normal per-command approval when WorkBuddy requires it. If the task is in Ask mode and cannot execute Python, explain that the workflow requires execution capability and ask the customer to switch the same task to Plan or Craft. Never ask the customer to operate a terminal.

Use WorkBuddy's artifact panel for previews while also returning absolute Markdown file links. Stream progress for image and video generation. Never store a BasicRouter key in project files, WorkBuddy Skill files, command arguments, or logs.
