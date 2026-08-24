#!/usr/bin/env python3
"""Crash-tolerant append-only generation event ledger."""
import json
import os
import uuid
import hashlib
from datetime import datetime

from project_utils import FileLock
import schema_validate
from task_contract import (task_dependency_matches, task_model_matches,
                           upsert_task)


class LedgerCorruptionError(ValueError):
    """A complete ledger record is malformed and cannot be ignored safely."""


def _lock_path(path):
    return os.path.abspath(path) + ".lock"


def _valid_event(value, *, legacy=False):
    return (isinstance(value, dict) and (legacy or value.get("schema_version") == 1) and
            isinstance(value.get("event"), str) and bool(value["event"]) and
            (legacy or (isinstance(value.get("event_id"), str) and bool(value["event_id"]) and
                        isinstance(value.get("timestamp"), str) and bool(value["timestamp"]))))


def append_event(path, event, *, lock_timeout=10.0, stale_after=300.0, **fields):
    for key in ("video_url", "videoUrl", "signed_url", "signedUrl"):
        value = fields.pop(key, None)
        if value:
            fields[key + "_sha256"] = hashlib.sha256(
                str(value).encode("utf-8")).hexdigest()
    item = {"schema_version": 1, "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(timespec="seconds"), "event": event}
    item.update(fields)
    # 契约强制：generation-run schema 运行时校验（fail-closed，防畸形事件污染账本）
    try:
        schema_validate.enforce(item, "generation-run",
                                context="generation_ledger.append_event")
    except schema_validate.SchemaContractError as exc:
        raise LedgerCorruptionError(str(exc))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if os.path.exists(path) and os.path.islink(path):
        raise LedgerCorruptionError("LEDGER_SYMLINK_BLOCKED")
    encoded = (json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with FileLock(_lock_path(path), timeout=lock_timeout, stale_after=stale_after):
        # A crashed writer may leave an incomplete tail. Remove only that tail
        # before appending so it cannot become corrupt data in the middle.
        if os.path.isfile(path):
            with open(path, "rb+") as handle:
                data = handle.read()
                if data and not data.endswith(b"\n"):
                    tail_start = data.rfind(b"\n") + 1
                    try:
                        value = json.loads(data[tail_start:].decode("utf-8"))
                        complete = _valid_event(value)
                    except (ValueError, UnicodeError):
                        complete = False
                    if complete:
                        handle.seek(0, os.SEEK_END)
                        handle.write(b"\n")
                    else:
                        handle.truncate(tail_start)
                    handle.flush()
                    os.fsync(handle.fileno())
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "ab")
        except OSError:
            os.close(fd)
            raise
        with handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return item


def read_events(path, *, lock_timeout=10.0, stale_after=300.0):
    if not path or not os.path.isfile(path):
        return []
    events = []
    with FileLock(_lock_path(path), timeout=lock_timeout, stale_after=stale_after):
        with open(path, "rb") as handle:
            lines = handle.read().splitlines(keepends=True)
        for index, raw_line in enumerate(lines, 1):
            terminated = raw_line.endswith((b"\n", b"\r"))
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                if index == len(lines) and not terminated:
                    break
                raise LedgerCorruptionError(
                    "LEDGER_CORRUPT_LINE: %s:%s" % (os.path.abspath(path), index)) from exc
            # Old ledgers lacked schema metadata. Read their terminated records
            # for migration compatibility, while requiring new tail records to
            # have the complete v1 event envelope before repairing them.
            if not _valid_event(value, legacy="schema_version" not in value):
                raise LedgerCorruptionError(
                    "LEDGER_INVALID_RECORD: %s:%s" % (os.path.abspath(path), index))
            events.append(value)
    return events


def latest_task(events, unit_id, handoff_fingerprint):
    matches = [event for event in events
               if event.get("unit_id") == unit_id
               and event.get("handoff_fingerprint") == handoff_fingerprint
               and event.get("task_id")]
    return matches[-1] if matches else None


def _task_matches_event(task, event):
    """Prefer exact replay matches while keeping legacy null-metadata tasks readable."""
    if event.get("task_id") and task.get("task_id") != event.get("task_id"):
        return False
    if event.get("request_id") and task.get("request_id") != event.get("request_id"):
        return False
    event_model = event.get("model")
    task_model = task.get("model")
    if event_model is not None and task_model is not None \
            and not task_model_matches(task, event_model):
        return False
    event_dependency = event.get("dependency_fingerprint")
    task_dependency = task.get("dependency_fingerprint")
    if event_dependency is not None and task_dependency is not None \
            and not task_dependency_matches(task, event_dependency):
        return False
    return True


def reconcile_manifest_tasks(manifest, ledger, *, lock_timeout=10.0,
                             stale_after=300.0):
    """Replay task events from a ledger path or event iterable into manifest."""
    events = (read_events(ledger, lock_timeout=lock_timeout, stale_after=stale_after)
              if isinstance(ledger, (str, bytes, os.PathLike)) else list(ledger))
    reconciled = []
    for event in events:
        name = event.get("event", "")
        if not name.startswith("task_") or not (event.get("task_id") or event.get("request_id")):
            continue
        status = name[len("task_"):]
        if status == "resumed":
            status = "running"
        existing = next((item for item in reversed(manifest.get("tasks", []))
                         if _task_matches_event(item, event)), {})
        task = dict(existing)
        task.update({key: value for key, value in event.items()
                     if key not in ("schema_version", "event_id", "timestamp", "event")})
        task["stage"] = task.get("stage") or "video"
        task["unit_id"] = task.get("unit_id")
        task["handoff_fingerprint"] = task.get("handoff_fingerprint")
        task["status"] = status
        task["ledger_event_id"] = event.get("event_id")
        task["ledger_timestamp"] = event.get("timestamp")
        if not task.get("task_key") and not task.get("handoff_fingerprint"):
            task["task_key"] = "ledger:%s:%s" % (
                task["stage"], task.get("task_id") or task.get("request_id"))
        reconciled.append(upsert_task(manifest, task))
    return reconciled
