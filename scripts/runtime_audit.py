#!/usr/bin/env python3
"""Small OpenTelemetry-shaped JSONL audit sink with a strict safe allowlist."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time

from project_utils import FileLock, require_contained_path
from schema_validate import enforce


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVICE_NAME = "basicrouter-video-workflow"
SERVICE_VERSION = "1.0.0"
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.]{2,95}$")
_ATTRIBUTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_SEVERITY = {"INFO": 9, "WARN": 13, "ERROR": 17}


def trace_id(value):
    return hashlib.sha256(str(value or "anonymous").encode("utf-8")).hexdigest()[:32]


def _safe_attributes(attributes):
    safe = {}
    for key, value in (attributes or {}).items():
        key = str(key)
        if not _ATTRIBUTE_KEY_RE.fullmatch(key):
            continue
        # Never accept free-form objects or long strings in the audit channel.
        if isinstance(value, bool) or value is None:
            safe[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, str) and len(value) <= 128 and not value.startswith("sk-"):
            safe[key] = value
        if len(safe) >= 24:
            break
    return safe


def _audit_path():
    output_root = os.path.join(ROOT, "output")
    if os.path.lexists(output_root):
        require_contained_path(ROOT, output_root, label="audit_root", must_exist=False)
    else:
        os.mkdir(output_root, 0o700)
    directory = os.path.join(output_root, ".workflow-audit")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    require_contained_path(ROOT, directory, label="audit_directory", must_exist=False)
    return os.path.join(directory, "events.jsonl")


def build_event(event_name, *, trace, severity="INFO", body=None, attributes=None,
                timestamp=None):
    if not _EVENT_RE.fullmatch(str(event_name or "")):
        raise ValueError("AUDIT_EVENT_NAME_INVALID")
    severity = str(severity or "INFO").upper()
    if severity not in _SEVERITY:
        raise ValueError("AUDIT_SEVERITY_INVALID")
    now = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds")
    event = {
        "schema_version": 1,
        "timestamp": now,
        "observed_timestamp": now,
        "trace_id": trace_id(trace),
        "severity_text": severity,
        "severity_number": _SEVERITY[severity],
        "event_name": event_name,
        "body": str(body or event_name)[:256],
        "resource": {"service.name": SERVICE_NAME, "service.version": SERVICE_VERSION},
        "attributes": _safe_attributes(attributes),
    }
    enforce(event, "runtime-audit-event", context="runtime_audit.build_event")
    return event


def emit(event_name, *, trace, severity="INFO", body=None, attributes=None,
         path=None):
    event = build_event(event_name, trace=trace, severity=severity, body=body,
                        attributes=attributes)
    path = os.path.abspath(path or _audit_path())
    require_contained_path(ROOT, path, label="audit_log", must_exist=False)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")
    with FileLock(path + ".lock", timeout=10.0, stale_after=300.0):
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
    return {"ok": True, "trace_id": event["trace_id"], "event_name": event_name}
