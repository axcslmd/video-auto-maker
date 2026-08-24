#!/usr/bin/env python3
"""Hash-bound proof that exact approval artifacts were presented to a customer."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import uuid

import schema_validate
from project_utils import sha256_file


def _file_record(path):
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise ValueError("REVIEW_ARTIFACT_MISSING: %s" % path)
    return {"path": path, "sha256": sha256_file(path),
            "bytes": os.path.getsize(path)}


def _content_fingerprint(stage, artifacts):
    payload = {"stage": stage, "artifacts": artifacts}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _dependency_fingerprint(run_manifest, manifest, stage):
    try:
        dependencies = run_manifest.generation_dependencies(manifest, stage)
    except (KeyError, TypeError, ValueError):
        dependencies = []
    payload = {
        "stage": stage,
        "dependencies": [{
            "stage": dependency,
            "approval_hash": (manifest.get("approval_hashes") or {}).get(dependency),
            "approval_current": bool(run_manifest.approval_is_current(
                manifest, dependency)),
        } for dependency in dependencies],
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def present(manifest, stage, artifact_paths, *, channel,
            expected_stage_input_fingerprint=None):
    """Record an idempotent presentation receipt in the in-memory manifest."""
    import run_manifest

    if stage not in run_manifest.CUSTOMER_EVIDENCE_STAGES:
        raise ValueError("REVIEW_PRESENTATION_STAGE_INVALID: %s" % stage)
    if not str(channel or "").strip():
        raise ValueError("REVIEW_PRESENTATION_CHANNEL_REQUIRED")
    artifacts = [_file_record(path) for path in (artifact_paths or [])]
    if not artifacts:
        raise ValueError("REVIEW_PRESENTATION_ARTIFACT_REQUIRED")
    stage_input_fingerprint = run_manifest.stage_input_fingerprint(
        manifest, stage, refresh=True)
    if (expected_stage_input_fingerprint is not None and
            stage_input_fingerprint != expected_stage_input_fingerprint):
        raise ValueError("REVIEW_PRESENTATION_STAGE_INPUT_CHANGED")
    fingerprint = _content_fingerprint(stage, artifacts)
    dependency_fingerprint = _dependency_fingerprint(run_manifest, manifest, stage)
    presentations = manifest.setdefault("review_presentations", {})
    for existing in presentations.values():
        if (existing.get("stage") == stage and
                existing.get("content_fingerprint") == fingerprint and
                existing.get("stage_input_fingerprint") == stage_input_fingerprint and
                existing.get("dependency_fingerprint") == dependency_fingerprint and
                existing.get("channel") == channel and
                existing.get("client") == manifest.get("client") and
                existing.get("run_id") == manifest.get("run_id")):
            return existing
    receipt = {
        "schema_version": 2,
        "presentation_id": "presentation-%s" % uuid.uuid4().hex[:16],
        "stage": stage,
        "client": str(manifest.get("client") or "unknown"),
        "run_id": str(manifest.get("run_id") or "unknown"),
        "channel": str(channel),
        "content_fingerprint": fingerprint,
        "stage_input_fingerprint": stage_input_fingerprint,
        "dependency_fingerprint": dependency_fingerprint,
        "artifacts": artifacts,
        "presented_at": datetime.now().isoformat(timespec="seconds"),
    }
    schema_validate.enforce(receipt, "review-presentation", context="review_task.present")
    presentations[receipt["presentation_id"]] = receipt
    return receipt


def validate_presentation(manifest, stage, presentation_id, artifact_paths=None):
    import run_manifest

    receipt = (manifest.get("review_presentations") or {}).get(presentation_id)
    if not isinstance(receipt, dict):
        raise ValueError("REVIEW_PRESENTATION_REQUIRED")
    if receipt.get("schema_version") != 2:
        raise ValueError("REVIEW_PRESENTATION_STALE: 请重新展示当前产物")
    schema_validate.enforce(
        receipt, "review-presentation", context="review_task.validate_presentation")
    if (receipt.get("stage") != stage or
            receipt.get("client") != str(manifest.get("client") or "unknown") or
            receipt.get("run_id") != str(manifest.get("run_id") or "unknown")):
        raise ValueError("REVIEW_PRESENTATION_IDENTITY_MISMATCH")
    current = [_file_record(path) for path in (
        artifact_paths if artifact_paths is not None else
        [item["path"] for item in receipt.get("artifacts") or []])]
    if (_content_fingerprint(stage, current) != receipt.get("content_fingerprint") or
            current != receipt.get("artifacts")):
        raise ValueError("REVIEW_PRESENTATION_STALE")
    if (run_manifest.stage_input_fingerprint(manifest, stage, refresh=True) !=
            receipt.get("stage_input_fingerprint")):
        raise ValueError("REVIEW_PRESENTATION_STALE: 待审批源文件已变更")
    if (_dependency_fingerprint(run_manifest, manifest, stage) !=
            receipt.get("dependency_fingerprint")):
        raise ValueError("REVIEW_PRESENTATION_STALE: 上游审批合同已变更")
    return True
