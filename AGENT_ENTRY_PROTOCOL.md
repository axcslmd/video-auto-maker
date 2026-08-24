# Agent-Independent Entry Protocol

This is the public host contract for any compatible Agent.

## Customer entry

The customer enters `/video-auto-maker` in the current Agent conversation. The host adapter must load this protocol and `AGENTS.md`; it must not duplicate the creative workflow or expose a host-branded customer entry.

## Package root

Locate the directory containing `AGENTS.md`, `scripts/setup_env.py`, `START_HERE_AGENT.py`, and `deploy.sh`. All commands below run from that directory.

## Key gate and preparation

1. Run `python3 scripts/agent_entry.py prepare`. When the host has no native secure credential component, run `python3 scripts/agent_entry.py prepare --secure-key-prompt` so the entry can open an OS password dialog if the key is missing. When the host exposes a stable opaque task/session ID, pass it as `--host-session-id <opaque-id>`; when resuming after a blocked key prompt, pass the exact `--session-id <PUBLIC_SESSION_ID>` returned by the first call.
2. Capture `PUBLIC_SESSION_ID=...` as public task state; do not show that diagnostic line to the customer. The command executes `key_setup.py gate` against that exact ID before environment bootstrap or workflow routing.
3. On `BLOCKED`, use the host's secure credential input and pipe it through non-echoing standard input to `python3 scripts/key_setup.py save --stdin --session-id <PUBLIC_SESSION_ID>`. If the host has no such component, execute `python3 scripts/key_setup.py save --secure-prompt --session-id <PUBLIC_SESSION_ID>` or rerun prepare with `--secure-key-prompt`; the customer enters the key only in the native password dialog. Rerun prepare with the same ID after `SAVED`. Never ask for or accept a key in ordinary chat, command arguments, logs, or project files. On `QUARANTINED`, stop until the exposed credential is revoked or rotated server-side, then accept only a different fingerprint through one of the same secure channels. If neither secure channel is available, remain blocked.
4. On `STORED`, the same command checks the complete local environment and performs inline/offline bootstrap when required. If preparation still fails, stop with a customer-readable error. Do not continue, pretend success, create a venv in inline mode, or change the host Agent.

## Conversation handoff

After preparation, read `AGENTS.md` and `skills/video-auto-maker/SKILL.md` once, then execute the compact code-selected packet returned by
`python3 scripts/workflow_api.py next --manifest <manifest.json>`. The packet contains the current stage only: its entry functions, confirmation function, evidence, editable paths, protected fields, response schema and the index of deeper guidance. Do not feed every project reference to the host LLM on every turn. The host adapter must:

- preserve the exact Agent-independent `PUBLIC_SESSION_ID` returned by `agent_entry.py` for the current conversation only;
- rely on `agent_entry.py prepare` to run the common key gate before bootstrap or guidance; real API keys are never bundled and must be entered by the customer after initialization;
- ask for `CLIENT` when it is not known;
- use the common staged confirmation and handoff protocol;
- preserve the same run when the customer changes Agent hosts;
- record host name only as diagnostic metadata, with unknown fallback.
- submit creative changes only through `workflow_api.py submit-patch`/`refine`; never rewrite a manifest or approval record from model text;
- reduce work to the packet's `max_items_per_turn` when the current LLM has not earned a larger observed behavior profile.

If the host has no slash-command support, it must execute the equivalent conversation workflow from this file rather than ask the customer to use a terminal.

Tencent WorkBuddy discovers the project adapter at `.workbuddy/skills/video-auto-maker/SKILL.md`. Use Plan mode to review a new multi-stage run and Craft mode for execution, keep Default Permissions enabled, and let the host request normal per-command approvals. Ask mode may inspect status but cannot complete stages that require Python execution.

## Forbidden host behavior

No host may authorize, skip, downgrade, recover, or reject a run based only on the host Agent name. No host may bypass environment, key, asset, script, storyboard, reference, OCR, or final delivery gates. Host-specific files are thin adapters only, not customer-facing alternate workflows.
