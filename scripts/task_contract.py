#!/usr/bin/env python3
"""Pure task identity and manifest upsert helpers.

This module intentionally has no dependency on ``run_manifest`` or the
generation ledger.  Both persistence layers use the same task-key semantics
without importing each other, which keeps crash recovery deterministic and
breaks the former manifest/ledger import cycle.
"""
from datetime import datetime


def _normalize_token(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def task_model_matches(task, model):
    requested = _normalize_token(model)
    current = _normalize_token((task or {}).get("model"))
    if requested is None:
        return current is None
    return current == requested


def task_dependency_matches(task, dependency_fingerprint):
    requested = _normalize_token(dependency_fingerprint)
    current = _normalize_token((task or {}).get("dependency_fingerprint"))
    if requested is None:
        return current is None
    return current == requested


def task_key(stage, unit_id, handoff_fingerprint, attempt=1, model=None,
             dependency_fingerprint=None):
    key = "%s:%s:%s:%s" % (stage, unit_id, handoff_fingerprint, attempt)
    if stage == "video":
        normalized_model = _normalize_token(model)
        if normalized_model:
            key += ":%s" % normalized_model
        normalized_dependency = _normalize_token(dependency_fingerprint)
        if normalized_dependency:
            key += ":dep:%s" % normalized_dependency
    return key


def upsert_task(manifest, task):
    """Insert or replace one task using the canonical content-bound key."""
    task = dict(task)
    stage = task.get("stage", "video")
    key = task.get("task_key") or task_key(
        stage,
        task.get("unit_id"),
        task.get("handoff_fingerprint"),
        task.get("attempt", 1),
        task.get("model") if stage == "video" else None,
        task.get("dependency_fingerprint") if stage == "video" else None,
    )
    task["task_key"] = key
    task["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tasks = manifest.setdefault("tasks", [])
    for index, existing in enumerate(tasks):
        if existing.get("task_key") == key:
            tasks[index] = dict(existing, **task)
            return tasks[index]
    tasks.append(task)
    return task
