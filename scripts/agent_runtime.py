#!/usr/bin/env python3
"""Best-effort host-agent detection for diagnostics, never authorization."""
import json
import os
import re

from schema_validate import enforce


RUNTIME_ENV = "BASICROUTER_AGENT_RUNTIME"

# Host variables are intentionally signals rather than requirements. Agents may
# add or rename variables, so callers must always support the unknown fallback.
_HOST_SIGNALS = (
    ("workbuddy", ("WORKBUDDY_SESSION_ID", "WORKBUDDY_TASK_ID", "WORKBUDDY_CONFIG_DIR")),
    ("kilo", ("KILO_SESSION_ID", "KILO_AGENT_ID", "KILO_CONFIG_DIR")),
    ("codex", ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_HOME")),
    ("hermes", ("HERMES_SESSION_ID", "HERMES_AGENT_ID", "HERMES_HOME")),
)


def _normalize(value):
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-._")
    return value or "unknown"


def detect_agent_runtime(explicit=None, environ=None):
    """Return agent runtime diagnostics without assuming a specific host.

    Explicit/canonical values accept future or private agent names. Host signal
    detection only provides a convenience fallback and is never a security gate.
    """
    env = os.environ if environ is None else environ
    if explicit and str(explicit).strip():
        return {"name": _normalize(explicit), "source": "explicit"}
    configured = env.get(RUNTIME_ENV)
    if configured and configured.strip():
        return {"name": _normalize(configured), "source": RUNTIME_ENV}
    for name, variables in _HOST_SIGNALS:
        matched = next((variable for variable in variables if env.get(variable)), None)
        if matched:
            return {"name": name, "source": matched}
    return {"name": "unknown", "source": "unknown"}


_CAPABILITY_KEYS = (
    "local_files", "python", "stdout_json", "stdin_secret",
    "image_preview", "video_preview", "progress_stream", "tool_calling",
    "mcp", "slash_commands",
)


def host_capabilities(explicit_runtime=None, declared=None, environ=None):
    """Return transport/UI capabilities separately from LLM intelligence.

    The host name remains diagnostic metadata.  A caller may pass a declared
    capability mapping from a thin adapter, but no capability grants approval,
    key access, paid generation, or any other business authorization.
    """
    env = os.environ if environ is None else environ
    runtime = detect_agent_runtime(explicit=explicit_runtime, environ=env)
    defaults = {
        "local_files": True,
        "python": True,
        "stdout_json": True,
        "stdin_secret": True,
        "image_preview": runtime["name"] in {"codex", "kilo", "workbuddy"},
        "video_preview": runtime["name"] in {"codex", "kilo", "workbuddy"},
        "progress_stream": runtime["name"] in {"codex", "kilo", "workbuddy", "hermes"},
        "tool_calling": True,
        "mcp": False,
        "slash_commands": runtime["name"] in {"codex", "kilo", "workbuddy"},
    }
    configured = env.get("BASICROUTER_HOST_CAPABILITIES")
    if configured and declared is None:
        try:
            parsed = json.loads(configured)
            declared = parsed if isinstance(parsed, dict) else None
        except ValueError:
            declared = None
    for key, value in (declared or {}).items():
        if key in _CAPABILITY_KEYS and isinstance(value, bool):
            defaults[key] = value
    payload = {
        "schema_version": 1,
        "host_id": runtime["name"],
        "source": runtime["source"],
        "capabilities": defaults,
    }
    enforce(payload, "host-capabilities", context="agent_runtime.host_capabilities")
    return payload
