# /video-auto-maker — thin host adapter

This file is only a host adapter for slash-command discovery. It is not a
separate customer workflow and must not duplicate business rules.

Read and execute the project's agent-independent entry protocol from
`AGENT_ENTRY_PROTOCOL.md`.

Read the common files selected by that protocol, then follow the compact current
stage packet returned by `scripts/workflow_api.py next`. Continue directly in this conversation after
preparation; the customer must not be told to open a terminal.
