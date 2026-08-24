# Hermes / Other Agent Thin Entry

When the customer enters `/video-auto-maker`, read `AGENT_ENTRY_PROTOCOL.md` and the common files it selects. Follow the compact current-stage packet returned by `scripts/workflow_api.py next`; do not copy the workflow into this adapter.

Use `python3 scripts/agent_entry.py prepare` from the package root. If the host has no native secure credential component, add `--secure-key-prompt`; the command will open an OS password dialog only when the exact session is missing a key. A host credential value is piped through non-echoing stdin; the native fallback uses `key_setup.py save --secure-prompt`. Never collect a key in ordinary chat. If both secure channels are unavailable, remain blocked. A quarantined key must be revoked or rotated server-side and replaced by a different fingerprint through a secure channel. Continue only after `READY`.

If the host has no slash-command support, treat this file as the equivalent entry. The customer must not be sent to a terminal. Host identity is diagnostic only and must never change authorization, approvals, recovery, model selection, or asset handoff.
