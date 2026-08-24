#!/usr/bin/env python3
"""Dependency-free MCP stdio adapter for the host-neutral workflow API.

This module is deliberately a thin protocol boundary.  It owns capability
negotiation, JSON-RPC validation, path containment, rate limiting, redaction,
and idempotency receipts; all business decisions remain in ``workflow_api``.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import sys
import time

import model_behavior
import runtime_audit
import run_manifest
import workflow_api
from project_utils import FileLock, atomic_write_json, require_contained_path
from schema_validate import validate_inline


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_NAME = "basicrouter-video-workflow"
SERVER_VERSION = "1.1.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_PROJECT_JSON_BYTES = 16 * 1024 * 1024
DEFAULT_CALLS_PER_MINUTE = 120
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$"
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|password|secret|session[_-]?key|basicrouter[_-]?key|token)$",
    re.IGNORECASE,
)


class RpcError(ValueError):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.data = data


def _object_schema(properties, required=()):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
    }


_PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
_REQUEST_ID = {
    "type": "string", "minLength": 8, "maxLength": 128,
    "pattern": REQUEST_ID_PATTERN,
    "description": "Caller-generated stable idempotency key for exactly one logical write.",
}
_MODEL_PROFILE = {"type": "string", "minLength": 1, "maxLength": 4096}
_GENERIC_OUTPUT = {"type": "object"}


def _tool(name, title, description, input_schema, *, read_only, idempotent):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": input_schema,
        "outputSchema": _GENERIC_OUTPUT,
        "annotations": {
            "readOnlyHint": bool(read_only),
            "destructiveHint": False,
            "idempotentHint": bool(idempotent),
            "openWorldHint": False,
        },
    }


TOOLS = (
    _tool(
        "workflow_status", "Workflow status",
        "Read the current formal business stage. Does not bypass any gate.",
        _object_schema({"manifest": _PATH, "model_profile": _MODEL_PROFILE}, ["manifest"]),
        read_only=True, idempotent=True,
    ),
    _tool(
        "workflow_next", "Next stage packet",
        "Return the legacy V1 model-neutral stage packet during adapter migration.",
        _object_schema({"manifest": _PATH, "model_profile": _MODEL_PROFILE}, ["manifest"]),
        read_only=True, idempotent=True,
    ),
    _tool(
        "workflow_next_v2", "Next experience packet",
        "Return one customer milestone, one primary action, progress and current review pack.",
        _object_schema({"manifest": _PATH, "model_profile": _MODEL_PROFILE}, ["manifest"]),
        read_only=True, idempotent=True,
    ),
    _tool(
        "workflow_impact_preview", "Preview change impact",
        "Show exactly which downstream artifacts, reviews and paid tasks a proposed change would reopen.",
        _object_schema({
            "manifest": _PATH,
            "changed_artifact_ids": {
                "type": "array", "minItems": 1, "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        }, ["manifest", "changed_artifact_ids"]),
        read_only=True, idempotent=True,
    ),
    _tool(
        "artifact_get", "Approval artifacts",
        "List current approval artifacts and their hashes; never returns arbitrary file contents.",
        _object_schema({"manifest": _PATH, "model_profile": _MODEL_PROFILE}, ["manifest"]),
        read_only=True, idempotent=True,
    ),
    _tool(
        "task_status", "Generation task status",
        "Read durable generation status already recorded in the run manifest.",
        _object_schema({"manifest": _PATH, "stage": {"type": "string", "maxLength": 64}},
                       ["manifest"]),
        read_only=True, idempotent=True,
    ),
    _tool(
        "workflow_submit_patch", "Submit bounded patch",
        "Atomically apply a schema/evidence/stale-hash checked creative patch. request_id is mandatory for replay safety.",
        _object_schema({"manifest": _PATH, "target": _PATH, "proposal": {"type": "object"},
                        "model_profile": _MODEL_PROFILE, "request_id": _REQUEST_ID},
                       ["manifest", "target", "proposal", "request_id"]),
        read_only=False, idempotent=True,
    ),
    _tool(
        "workflow_refine", "Refine with bounded patch",
        "Apply customer refinement through the same protected patch transaction. request_id is mandatory.",
        _object_schema({"manifest": _PATH, "target": _PATH, "proposal": {"type": "object"},
                        "model_profile": _MODEL_PROFILE, "request_id": _REQUEST_ID},
                       ["manifest", "target", "proposal", "request_id"]),
        read_only=False, idempotent=True,
    ),
    _tool(
        "workflow_review_present", "Present approval artifacts",
        "Bind a presentation receipt to the exact current artifact bytes before asking for approval.",
        _object_schema({
            "manifest": _PATH,
            "stage": {"type": "string", "maxLength": 64},
            "channel": {"type": "string", "minLength": 1, "maxLength": 128},
            "model_profile": _MODEL_PROFILE,
            "request_id": _REQUEST_ID,
        }, ["manifest", "channel", "request_id"]),
        read_only=False, idempotent=True,
    ),
    _tool(
        "workflow_confirm", "Record customer confirmation",
        "Compatibility confirmation entry. Requires a hash-bound presentation receipt and explicit customer words.",
        _object_schema({
            "manifest": _PATH,
            "customer_confirmation": {"type": "string", "minLength": 1, "maxLength": 2000},
            "presentation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "stage": {"type": "string", "maxLength": 64},
            "storyboard_result": _PATH,
            "caption_manifest": _PATH,
            "geometry_review": _PATH,
            "model_profile": _MODEL_PROFILE,
            "request_id": _REQUEST_ID,
        }, ["manifest", "customer_confirmation", "presentation_id", "request_id"]),
        read_only=False, idempotent=True,
    ),
    _tool(
        "workflow_confirm_v2", "Confirm current review pack",
        "Confirm only the exact bytes named by a current presentation receipt; stale receipts are rejected.",
        _object_schema({
            "manifest": _PATH,
            "customer_confirmation": {"type": "string", "minLength": 1, "maxLength": 2000},
            "presentation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "stage": {"type": "string", "maxLength": 64},
            "storyboard_result": _PATH,
            "caption_manifest": _PATH,
            "geometry_review": _PATH,
            "model_profile": _MODEL_PROFILE,
            "request_id": _REQUEST_ID,
        }, ["manifest", "customer_confirmation", "presentation_id", "request_id"]),
        read_only=False, idempotent=True,
    ),
)
TOOL_BY_NAME = {item["name"]: item for item in TOOLS}
WRITE_TOOLS = frozenset((
    "workflow_submit_patch", "workflow_refine", "workflow_review_present",
    "workflow_confirm", "workflow_confirm_v2",
))


def _trusted_json(path, label, *, max_bytes=MAX_PROJECT_JSON_BYTES):
    trusted = require_contained_path(ROOT, path, label=label, must_exist=True)
    size = os.path.getsize(trusted)
    if size > max_bytes:
        raise ValueError("%s_TOO_LARGE: %d bytes" % (label.upper(), size))
    return trusted


def _load_inputs(arguments):
    manifest_path = _trusted_json(arguments["manifest"], "manifest")
    manifest = run_manifest.load_manifest(manifest_path)
    profile = None
    if arguments.get("model_profile"):
        profile_path = _trusted_json(arguments["model_profile"], "model_profile")
        profile = model_behavior.load_profile(profile_path)
    return manifest_path, manifest, profile


def _redact(value, parent_key=""):
    """Fail closed on common secret-bearing keys before values reach an LLM."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = _redact(item, str(key))
        return clean
    if isinstance(value, list):
        return [_redact(item, parent_key) for item in value]
    if isinstance(value, str) and os.path.isabs(value) and (
            parent_key == "path" or parent_key.endswith("_path") or
            parent_key in {"customer_preview", "preview_sources", "stage_sources",
                           "artifacts_to_show", "outputs"}):
        try:
            require_contained_path(ROOT, value, label="result_path", must_exist=False)
        except ValueError:
            return "[BLOCKED_OUTSIDE_PROJECT]"
    if isinstance(value, str) and re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b", value):
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    return value


def _canonical_digest(tool_name, arguments):
    filtered = {key: value for key, value in arguments.items() if key != "request_id"}
    payload = json.dumps({"tool": tool_name, "arguments": filtered},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _receipt_path(manifest_path):
    output_root = os.path.join(ROOT, "output")
    if os.path.lexists(output_root):
        require_contained_path(ROOT, output_root, label="operation_root",
                               must_exist=False)
    else:
        os.mkdir(output_root, 0o700)
    directory = os.path.join(output_root, ".workflow-operations")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    require_contained_path(ROOT, directory, label="operation_directory",
                           must_exist=False)
    key = hashlib.sha256(os.path.abspath(manifest_path).encode("utf-8")).hexdigest()
    return os.path.join(directory, "%s.json" % key)


def _execute_idempotent(tool_name, arguments, manifest_path, callback):
    request_id = arguments["request_id"]
    fingerprint = _canonical_digest(tool_name, arguments)
    path = _receipt_path(manifest_path)
    with FileLock(path + ".lock", timeout=15.0, stale_after=300.0):
        ledger = {"schema_version": 1, "operations": {}}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict) and isinstance(loaded.get("operations"), dict):
                    ledger = loaded
            except (OSError, ValueError, TypeError):
                raise ValueError("IDEMPOTENCY_LEDGER_INVALID")
        previous = ledger["operations"].get(request_id)
        if previous:
            if previous.get("fingerprint") != fingerprint or previous.get("tool") != tool_name:
                raise ValueError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INPUT")
            result = previous.get("result")
            if not isinstance(result, dict):
                raise ValueError("IDEMPOTENCY_RECEIPT_INVALID")
            replay = dict(result)
            replay["idempotent_replay"] = True
            return replay
        result = callback()
        if not isinstance(result, dict):
            raise ValueError("WORKFLOW_RESULT_INVALID")
        ledger["operations"][request_id] = {
            "tool": tool_name,
            "fingerprint": fingerprint,
            "completed_at": int(time.time()),
            "result": _redact(result),
        }
        # Bound the local receipt ledger without weakening request uniqueness.
        if len(ledger["operations"]) > 2000:
            ordered = sorted(ledger["operations"].items(),
                             key=lambda pair: pair[1].get("completed_at", 0))
            ledger["operations"] = dict(ordered[-2000:])
        atomic_write_json(path, ledger, mode=0o600)
        return result


def _dispatch(tool_name, arguments):
    manifest_path, manifest, profile = _load_inputs(arguments)
    if tool_name == "workflow_status":
        return workflow_api.workflow_status(manifest, profile)
    if tool_name == "workflow_next":
        return workflow_api.workflow_next(manifest, profile)
    if tool_name == "workflow_next_v2":
        return workflow_api.workflow_next_v2(manifest, profile)
    if tool_name == "workflow_impact_preview":
        return workflow_api.workflow_impact_preview(
            manifest, arguments["changed_artifact_ids"])
    if tool_name == "artifact_get":
        return workflow_api.artifact_get(manifest, profile)
    if tool_name == "task_status":
        return workflow_api.task_status(manifest, arguments.get("stage"))

    def mutation():
        if tool_name in ("workflow_submit_patch", "workflow_refine"):
            target = _trusted_json(arguments["target"], "patch_target")
            function = (workflow_api.workflow_refine if tool_name == "workflow_refine"
                        else workflow_api.workflow_submit_patch)
            return function(manifest, target, arguments["proposal"], profile)
        if tool_name == "workflow_review_present":
            return workflow_api.workflow_review_present(
                manifest, manifest_path, stage=arguments.get("stage"),
                channel=arguments["channel"], model_profile=profile)
        optional_paths = {}
        for key in ("storyboard_result", "caption_manifest", "geometry_review"):
            optional_paths[key] = (_trusted_json(arguments[key], key)
                                   if arguments.get(key) else None)
        confirm_function = (workflow_api.workflow_confirm_v2
                            if tool_name == "workflow_confirm_v2"
                            else workflow_api.workflow_confirm)
        return confirm_function(
            manifest, manifest_path, arguments["customer_confirmation"],
            stage=arguments.get("stage"),
            presentation_id=arguments.get("presentation_id"),
            model_profile=profile, **optional_paths)

    return _execute_idempotent(tool_name, arguments, manifest_path, mutation)


def _tool_result(payload, *, error=False):
    safe = _redact(payload if isinstance(payload, dict) else {"message": str(payload)})
    return {
        "content": [{"type": "text", "text": json.dumps(
            safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}],
        "structuredContent": safe,
        "isError": bool(error),
    }


class WorkflowMCPServer:
    """Stateful MCP lifecycle handler, separable from stdio for exact tests."""

    def __init__(self, *, calls_per_minute=None, clock=None):
        self.protocol_version = None
        self.initialized = False
        self.ready = False
        self.client_capabilities = {}
        self.clock = clock or time.monotonic
        limit = calls_per_minute
        if limit is None:
            try:
                limit = int(os.environ.get("WORKFLOW_MCP_CALLS_PER_MINUTE",
                                           DEFAULT_CALLS_PER_MINUTE))
            except ValueError:
                limit = DEFAULT_CALLS_PER_MINUTE
        self.calls_per_minute = min(10000, max(1, int(limit)))
        self.call_times = collections.deque()

    def _rate_limit(self):
        now = self.clock()
        while self.call_times and now - self.call_times[0] >= 60.0:
            self.call_times.popleft()
        if len(self.call_times) >= self.calls_per_minute:
            raise RpcError(-32001, "Tool call rate limit exceeded")
        self.call_times.append(now)

    def _initialize(self, params):
        if self.initialized:
            raise RpcError(-32600, "Server already initialized")
        if not isinstance(params, dict):
            raise RpcError(-32602, "initialize params must be an object")
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not requested:
            raise RpcError(-32602, "protocolVersion is required")
        selected = (requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                    else SUPPORTED_PROTOCOL_VERSIONS[0])
        self.protocol_version = selected
        self.client_capabilities = (params.get("capabilities")
                                    if isinstance(params.get("capabilities"), dict) else {})
        self.initialized = True
        return {
            "protocolVersion": selected,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "BasicRouter Video Workflow",
                           "version": SERVER_VERSION},
            "instructions": (
                "Call workflow_next first. Only call actions listed by its interaction_contract. "
                "Never infer customer confirmation or bypass a technical gate."
            ),
        }

    def _handle_method(self, method, params):
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            if not self.ready:
                raise RpcError(-32002, "Server is not ready; send notifications/initialized")
            if params not in (None, {}) and not isinstance(params, dict):
                raise RpcError(-32602, "tools/list params must be an object")
            return {"tools": list(TOOLS)}
        if method == "tools/call":
            if not self.ready:
                raise RpcError(-32002, "Server is not ready; send notifications/initialized")
            if not isinstance(params, dict):
                raise RpcError(-32602, "tools/call params must be an object")
            name = params.get("name")
            if name not in TOOL_BY_NAME:
                raise RpcError(-32602, "Unknown tool: %s" % name)
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                raise RpcError(-32602, "Tool arguments must be an object")
            errors = validate_inline(arguments, TOOL_BY_NAME[name]["inputSchema"])
            if errors:
                raise RpcError(-32602, "Invalid tool arguments", {"errors": errors[:20]})
            self._rate_limit()
            audit_trace = arguments.get("request_id") or (
                "%s:%s" % (name, self.clock()))
            started = self.clock()
            try:
                try:
                    runtime_audit.emit(
                        "workflow.tool.invoked", trace=audit_trace,
                        body="Workflow tool invocation accepted",
                        attributes={"tool.name": name,
                                    "protocol.version": self.protocol_version,
                                    "tool.write": name in WRITE_TOOLS})
                except (OSError, ValueError, TypeError, RuntimeError):
                    if name in WRITE_TOOLS:
                        raise
                result = _dispatch(name, arguments)
                try:
                    runtime_audit.emit(
                        "workflow.tool.completed", trace=audit_trace,
                        body="Workflow tool invocation completed",
                        attributes={"tool.name": name,
                                    "protocol.version": self.protocol_version,
                                    "tool.write": name in WRITE_TOOLS,
                                    "duration.ms": int((self.clock() - started) * 1000)})
                except (OSError, ValueError, TypeError, RuntimeError):
                    if name in WRITE_TOOLS:
                        raise
                return _tool_result(result)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError,
                    json.JSONDecodeError) as exc:  # Business errors belong in a tool result.
                try:
                    runtime_audit.emit(
                        "workflow.tool.failed", trace=audit_trace, severity="ERROR",
                        body="Workflow tool invocation failed",
                        attributes={"tool.name": name, "protocol.version": self.protocol_version,
                                    "tool.write": name in WRITE_TOOLS,
                                    "error.type": type(exc).__name__,
                                    "duration.ms": int((self.clock() - started) * 1000)})
                except (OSError, ValueError, TypeError, RuntimeError):
                    pass
                return _tool_result({"error": type(exc).__name__, "message": str(exc)},
                                    error=True)
        raise RpcError(-32601, "Method not found: %s" % method)

    def handle_message(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, RpcError(-32600, "Invalid JSON-RPC request"))
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message
        try:
            if method == "notifications/initialized":
                if not self.initialized:
                    raise RpcError(-32002, "initialize must be called first")
                self.ready = True
                return None if is_notification else {"jsonrpc": "2.0", "id": request_id,
                                                     "result": {}}
            if method == "notifications/cancelled":
                return None
            result = self._handle_method(method, message.get("params"))
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            if is_notification:
                return None
            return self._error(request_id, exc)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError,
                json.JSONDecodeError) as exc:
            if is_notification:
                return None
            return self._error(request_id, RpcError(-32603, "Internal error",
                                                     {"type": type(exc).__name__}))

    @staticmethod
    def _error(request_id, exc):
        error = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = _redact(exc.data)
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def run_stdio(stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    server = WorkflowMCPServer()
    for raw in stdin:
        if len(raw) > MAX_MESSAGE_BYTES:
            response = server._error(None, RpcError(-32600, "Message exceeds size limit"))
        else:
            try:
                message = json.loads(raw.decode("utf-8"))
                response = server.handle_message(message)
            except (UnicodeDecodeError, ValueError, TypeError):
                response = server._error(None, RpcError(-32700, "Parse error"))
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_stdio())
