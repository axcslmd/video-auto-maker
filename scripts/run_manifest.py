#!/usr/bin/env python3
"""Persistent run manifest for approvals, inputs, and generated outputs."""
import hashlib
import json
import os
import argparse
import tempfile
import copy
import re
import functools
import inspect
import subprocess
from datetime import datetime
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artifact_contract import verify_video_handoff  # noqa: E402
import take_review as _take_review  # noqa: E402
import take_review  # noqa: E402
import workflow_contract  # noqa: E402
import prompt_review_contract  # noqa: E402
import stage_output_contract  # noqa: E402
import identity_review_contract  # noqa: E402
from project_utils import (FileLock, validate_client, validate_run_id,
                           require_contained_path)
from agent_runtime import detect_agent_runtime
from task_contract import (task_dependency_matches, task_key,
                           task_model_matches, upsert_task)


STAGES = (
    "brief", "script", "content_scaffold", "motion_design", "styleframe", "audio",
    "product_board", "cast_board", "product_usage", "storyboard", "render_plan",
    "test_segment", "video", "shotcraft_packaging", "captions", "final", "derive",
)
VISUAL_CUSTOMER_EVIDENCE_STAGES = frozenset((
    "product_board", "cast_board", "product_usage", "storyboard",
))
STABLE_VISUAL_BOARD_STAGES = frozenset((
    "product_board", "cast_board", "product_usage",
))
GENERIC_CUSTOMER_EVIDENCE_STAGES = frozenset((
    "brief", "script", "motion_design", "styleframe", "audio", "render_plan", "video",
    "test_segment", "captions", "final", "derive",
))
CUSTOMER_EVIDENCE_STAGES = frozenset(
    VISUAL_CUSTOMER_EVIDENCE_STAGES | GENERIC_CUSTOMER_EVIDENCE_STAGES)
SPECIALIZED_FINISH_STAGES = frozenset((
    "audio", "product_board", "cast_board", "product_usage", "storyboard", "video",
    "test_segment", "shotcraft_packaging", "captions", "final", "derive",
))
TRANSACTIONAL_GENERIC_STAGES = frozenset((
    "brief", "script", "content_scaffold", "motion_design", "styleframe",
    "render_plan",
))


class ManifestConflictError(ValueError):
    """The manifest changed on disk after the caller loaded its snapshot."""

# Generation dependencies are deliberately stricter than the approval list. A
# stage may only start after its inputs have been approved and recorded. The
# product board is conditional because pure talking-head projects do not need it.
BASE_DEPENDENCIES = {
    "brief": (),
    "script": ("brief",),
    "content_scaffold": ("script",),
    # Every motion plan is script-bound. Document explainers additionally add
    # content_scaffold below; non-document kinetic overlays must not deadlock on
    # a stage that their contract intentionally omits.
    "motion_design": ("script",),
    "styleframe": ("script",),
    "audio": ("script",),
    "cast_board": ("script",),
    "product_board": ("brief",),
    "product_usage": ("script", "product_board", "cast_board"),
    "storyboard": ("script",),
    "render_plan": ("storyboard",),
    "test_segment": ("storyboard", "render_plan"),
    "video": ("storyboard", "render_plan"),
    "shotcraft_packaging": ("video",),
    "captions": ("video",),
    "final": ("captions",),
    "derive": ("final",),
}


def _downstream_stages(stage, manifest=None):
    """Return graph descendants whose inputs include ``stage``."""
    result = []
    pending = [stage]
    while pending:
        current = pending.pop(0)
        for candidate in BASE_DEPENDENCIES:
            dependencies = (generation_dependencies(manifest, candidate)
                            if manifest is not None else BASE_DEPENDENCIES[candidate])
            if current in dependencies and candidate not in result:
                result.append(candidate)
                pending.append(candidate)
    return result


def invalidate_approvals(manifest, stages):
    """Invalidate approvals and every customer-evidence surface in one place.

    Approval booleans, input hashes, customer markers and presentation receipts
    are one logical state. Mutating only a subset creates green-but-stale UI or
    lets a host reuse evidence that no longer belongs to the current contract.
    """
    if not isinstance(manifest, dict):
        raise ValueError("RUN_MANIFEST_INVALID")
    normalized = list(dict.fromkeys(str(stage) for stage in (stages or [])))
    unknown = [stage for stage in normalized if stage not in STAGES]
    if unknown:
        raise ValueError("APPROVAL_INVALIDATION_STAGE_UNKNOWN: %s" %
                         ", ".join(unknown))
    stage_set = set(normalized)
    approvals = manifest.setdefault("approvals", {})
    hashes = manifest.setdefault("approval_hashes", {})
    evidence = manifest.setdefault("approval_evidence", {})
    for stage in normalized:
        active_evidence = copy.deepcopy(evidence.get(stage))
        active_hash = hashes.get(stage)
        if approvals.get(stage) is True or active_evidence or active_hash:
            snapshot = {
                "kind": "invalidated_customer_approval",
                "stage": stage,
                "approval_hash": active_hash,
                "approval_evidence": active_evidence,
                "invalidated_at": datetime.now().isoformat(timespec="seconds"),
                "reason": "upstream_or_artifact_contract_changed",
            }
            snapshot["history_fingerprint"] = _digest(json.dumps(
                {key: value for key, value in snapshot.items()
                 if key not in {"invalidated_at", "history_fingerprint"}},
                ensure_ascii=False, sort_keys=True))
            history = manifest.setdefault("approval_history", [])
            if not any(item.get("history_fingerprint") ==
                       snapshot["history_fingerprint"] for item in history
                       if isinstance(item, dict)):
                history.append(snapshot)
        approvals[stage] = False
        hashes.pop(stage, None)
        evidence.pop(stage, None)
    if "render_plan" in stage_set:
        identity_state = manifest.pop("identity_plate_generation", None)
        if identity_state:
            archived = copy.deepcopy(identity_state)
            archived.update({
                "status": "invalidated",
                "invalidated_at": datetime.now().isoformat(timespec="seconds"),
                "invalidation_reason": "render_plan_contract_changed",
            })
            manifest.setdefault("identity_plate_generation_history", []).append(archived)
        gates = manifest.get("pre_video_gates") or {}
        gates.pop("identity_plate", None)
        gates.pop("video_prompt_review", None)

    presentations = manifest.get("review_presentations")
    removed_presentations = []
    if presentations is not None:
        if not isinstance(presentations, dict):
            raise ValueError("RUN_MANIFEST_REVIEW_PRESENTATIONS_INVALID")
        for presentation_id, receipt in list(presentations.items()):
            if isinstance(receipt, dict) and receipt.get("stage") in stage_set:
                removed_presentations.append(presentation_id)
                presentations.pop(presentation_id, None)
    return {"stages": normalized,
            "removed_presentations": removed_presentations}


def invalidate_from(manifest, stage, *, include_stage=True):
    """Invalidate one stage and its actual profile-specific descendants."""
    if stage not in STAGES:
        raise ValueError("APPROVAL_INVALIDATION_STAGE_UNKNOWN: %s" % stage)
    stages = ([stage] if include_stage else []) + _downstream_stages(stage, manifest)
    return invalidate_approvals(manifest, stages)


def _digest(value):
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _archive_generation_attempt(manifest, stage, *, superseded_by):
    """Preserve one generation transaction before its active slot is replaced.

    ``generation[stage]`` is intentionally the current transaction view.  Paid
    provider task IDs, download recovery state, settlement records, and billing
    uncertainty must nevertheless survive a retry.  This append-only snapshot
    prevents a fresh ``running`` record from erasing the evidence needed to
    resume an existing remote task or audit a newly authorized attempt.
    """
    current = (manifest.get("generation") or {}).get(stage)
    if not isinstance(current, dict) or not current:
        return None
    snapshot = {
        "kind": "generation_attempt",
        "stage": stage,
        "record": copy.deepcopy(current),
        "superseded_by": str(superseded_by or "unknown"),
        "source_revision": int(manifest.get("revision") or 0),
    }
    snapshot["transaction_fingerprint"] = _digest(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    history_by_stage = manifest.setdefault("generation_history", {})
    history = history_by_stage.setdefault(stage, [])
    fingerprint = snapshot["transaction_fingerprint"]
    if any(isinstance(item, dict) and
           item.get("transaction_fingerprint") == fingerprint for item in history):
        return next(item for item in history if isinstance(item, dict) and
                    item.get("transaction_fingerprint") == fingerprint)
    snapshot["archived_at"] = datetime.now().isoformat(timespec="seconds")
    snapshot["history_fingerprint"] = _digest(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    history.append(snapshot)
    return snapshot


def _validate_generation_history(manifest):
    """Fail closed if append-only generation evidence was altered."""
    history_by_stage = manifest.get("generation_history")
    if history_by_stage is None:
        return True
    if not isinstance(history_by_stage, dict):
        raise ValueError("GENERATION_HISTORY_INVALID")
    for stage, history in history_by_stage.items():
        if stage not in STAGES or not isinstance(history, list):
            raise ValueError("GENERATION_HISTORY_INVALID: %s" % stage)
        for entry in history:
            if not isinstance(entry, dict):
                raise ValueError("GENERATION_HISTORY_INVALID: %s" % stage)
            actual = entry.get("history_fingerprint")
            canonical = {key: value for key, value in entry.items()
                         if key != "history_fingerprint"}
            expected = _digest(json.dumps(
                canonical, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")))
            if (entry.get("kind") != "generation_attempt" or
                    entry.get("stage") != stage or actual != expected):
                raise ValueError("GENERATION_HISTORY_TAMPERED: %s" % stage)
    return True


def _session_digest(session_id=None):
    value = session_id if session_id is not None else os.environ.get(
        "BASICROUTER_SESSION_ID", "")
    value = value.strip()
    return _digest(value) if value else None


def plan_requirements_from_plan(plan):
    """Return canonical optional-stage flags for one authored plan object."""
    return dict(workflow_contract.plan_requirements(plan)["requirements"])


def plan_requirements(plan_path):
    """Load an authored plan and return its complete canonical workflow contract."""
    return workflow_contract.plan_requirements(_load_plan_for_requirements(plan_path))


def _requirement(plan_path, name, *, unavailable=False):
    try:
        return bool(plan_requirements(plan_path)["requirements"][name])
    except ValueError:
        return bool(unavailable)


def _plan_requires_product_board(plan_path):
    return _requirement(plan_path, "requires_product_board")


def _plan_requires_product_usage(plan_path):
    return _requirement(plan_path, "requires_product_usage")


def _plan_requires_cast_board(plan_path):
    return _requirement(plan_path, "requires_cast_board")


def _load_plan_for_requirements(plan_path):
    if not plan_path or not os.path.isfile(plan_path):
        raise ValueError("PLAN_UNAVAILABLE")
    try:
        with open(plan_path, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("PLAN_UNAVAILABLE") from exc
    if not isinstance(plan, dict):
        raise ValueError("PLAN_UNAVAILABLE")
    return plan


def _frozen_chain_profile(plan_path):
    """Infer the canonical route once from the authored plan.

    Runtime artifacts may be incomplete while a stage is running, so routing
    cannot safely be re-inferred from whatever files happen to exist.  Persist
    this profile in the manifest and let explicit migration own any later route
    change.
    """
    try:
        plan = _load_plan_for_requirements(plan_path)
    except ValueError:
        return None
    inferred = workflow_contract.infer_profile(plan)
    profile, reason = inferred["profile"], inferred["reason"]
    plan_record = _file_record(plan_path)
    return {
        "profile": profile,
        "reason": reason,
        "contract_version": workflow_contract.CONTRACT_VERSION,
        "frozen": True,
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "plan_path": plan_record.get("path") if plan_record else None,
        "plan_sha256": plan_record.get("sha256") if plan_record else None,
    }


def _flatten_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_values(item)
    else:
        yield value


def _text_blob(*values):
    parts = []
    for value in values:
        for item in _flatten_values(value):
            if isinstance(item, str) and item.strip():
                parts.append(item.strip().lower())
    return " ".join(parts)


def _plan_requires_styleframe(plan_path):
    return _requirement(plan_path, "requires_styleframe")


def _plan_requires_content_scaffold(plan_path):
    return _requirement(plan_path, "requires_content_scaffold")


def _plan_requires_motion_design(plan_path):
    return _requirement(plan_path, "requires_motion_design")


def _plan_requires_audio(plan_path):
    return _requirement(plan_path, "requires_audio")


def create_manifest(client, run_id, *, script_path=None, plan_path=None):
    """Create a resumable manifest without overwriting an existing run."""
    validate_client(client)
    validate_run_id(run_id)
    now = datetime.now().isoformat(timespec="seconds")
    contract = (plan_requirements(plan_path) if plan_path and os.path.isfile(plan_path)
                else workflow_contract.plan_requirements({}))
    manifest = {
        "manifest_version": 4,
        "revision": 0,
        "run_id": run_id,
        "client": client,
        "created_at": now,
        "updated_at": now,
        "status": "created",
        "script": _file_record(script_path),
        "storyboard_plan": _file_record(plan_path),
        "approvals": {stage: False for stage in STAGES},
        "assets": [],
        "outputs": [],
        "tasks": [],
        "handoffs": {},
        "accepted_takes": {},
        "ocr_checks": {},
        "ocr_waivers": {},
        "package": contract.get("authored_package"),
        "expected_package": contract.get("expected_package"),
        "allowed_packages": contract.get("allowed_packages") or [],
        **contract["requirements"],
        "workflow_contract_version": workflow_contract.CONTRACT_VERSION,
        "identity": {
            "client": client,
            "asset_dir": os.path.abspath(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "assets", client)),
            "session_id_sha256": _session_digest(),
            "agent_runtime": detect_agent_runtime(),
        },
    }
    frozen_profile = _frozen_chain_profile(plan_path)
    if frozen_profile:
        manifest["chain_profile"] = frozen_profile
    if plan_path:
        # Reuse the same identity/schema gate as later rebinding so a newly
        # created run cannot start with a plan that formal stages would reject.
        bind_storyboard_plan(manifest, plan_path)
    return manifest


def _file_record(path):
    if not path:
        return None
    record = {"path": os.path.abspath(path), "exists": os.path.isfile(path)}
    if record["exists"]:
        with open(path, "rb") as handle:
            record["sha256"] = _digest(handle.read())
    return record


def _resolve_artifact_path(value, *, base_dir=None):
    if not value:
        return None
    path = str(value)
    if os.path.isabs(path):
        return path
    cwd_path = os.path.abspath(path)
    if os.path.exists(cwd_path) or not base_dir:
        return cwd_path
    return os.path.abspath(os.path.join(base_dir, path))


def _storyboard_result_artifact_paths(result_path):
    """Expand storyboard results into every approval-bound visual/timeline artifact."""
    result_path = os.path.abspath(result_path)
    paths = [result_path]
    try:
        with open(result_path, encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return paths
    base_dir = os.path.dirname(result_path)

    def add(value):
        path = _resolve_artifact_path(value, base_dir=base_dir)
        if path and path not in paths:
            paths.append(path)

    for key in ("cast_board", "product_board", "product_usage_image"):
        item = result.get(key) or {}
        if isinstance(item, dict):
            add(item.get("abspath") or item.get("path"))
    for item in result.get("shots") or []:
        if isinstance(item, dict):
            add(item.get("abspath") or item.get("path"))
    # Document explainers approve deterministic layout stills together with
    # the Animatic and exact Remotion specs, not an image-model concept frame.
    for key in (
            "animatic", "content_layer", "integrated_previs_spec", "integrated_spec",
            "content_contract_spec", "content_previs_spec", "motion_layer",
            "hyperframes_spec"):
        item = result.get(key) or {}
        if isinstance(item, dict):
            add(item.get("abspath") or item.get("path"))
            add(item.get("master_path"))
    for item in result.get("layout_boards") or []:
        if isinstance(item, dict):
            add(item.get("abspath") or item.get("path"))
    for key in ("execution_preview_result", "composite_plan_path"):
        add(result.get(key))
    return paths


def _expand_stage_outputs(stage, outputs):
    expanded = []
    for path in outputs:
        stage_paths = [path]
        if stage == "storyboard" and str(path).endswith(".json"):
            stage_paths = _storyboard_result_artifact_paths(path)
        for item in stage_paths:
            resolved = _resolve_artifact_path(item)
            if resolved and resolved not in expanded:
                expanded.append(resolved)
    return expanded


def file_record(path):
    """Public content identity for a file-backed handoff artifact."""
    return _file_record(path)


def file_record_is_current(record):
    """Return whether a persisted file identity still matches disk bytes."""
    current = _refresh_file_record(record)
    return bool(current and current.get("exists") and current == record)


def _refresh_file_record(record):
    """Re-read one persisted file record instead of trusting cached metadata."""
    if not record or not record.get("path"):
        return None
    return _file_record(record["path"])


def _stage_artifacts(manifest, stage, *, refresh=False):
    """Return the real artifacts whose bytes are approved for ``stage``."""
    generation = manifest.get("generation", {}).get(stage) or {}
    records = generation.get("artifacts")
    if records is None:
        records = [_file_record(path) for path in generation.get("outputs", [])]
    records = [record for record in (records or []) if record]
    if refresh:
        records = [_refresh_file_record(record) for record in records]
        records = [record for record in records if record]
    return records


def _validate_persisted_stage_output_contract(manifest, stage, artifacts):
    computed = stage_output_contract.validate_stage_outputs(
        manifest, stage, [item["path"] for item in artifacts])
    if computed is None:
        return None
    stored = ((manifest.get("generation") or {}).get(stage) or {}).get(
        "output_contract")
    if stored != computed:
        raise ValueError("STAGE_OUTPUT_CONTRACT_STALE: %s" % stage)
    return computed


def _assert_stage_artifact_ownership(manifest, stage, paths):
    requested = {os.path.abspath(path) for path in paths}
    collisions = []
    for owner, generation in (manifest.get("generation") or {}).items():
        if owner == stage or not isinstance(generation, dict):
            continue
        for record in generation.get("artifacts") or []:
            path = record.get("path") if isinstance(record, dict) else None
            if path and os.path.abspath(path) in requested:
                collisions.append("%s:%s" % (owner, os.path.abspath(path)))
    if collisions:
        raise ValueError(
            "STAGE_ARTIFACT_OWNERSHIP_CONFLICT: %s；派生阶段必须写独立文件，"
            "不能重写上游审批产物。" % "；".join(collisions))
    return True


def replace_pending_stage_artifacts(manifest, stage, paths):
    """Explicitly repair an unapproved stage's artifact ownership set."""
    generation = (manifest.get("generation") or {}).get(stage) or {}
    if (generation.get("status") != "pending_approval" or
            manifest.get("approvals", {}).get(stage)):
        raise ValueError("PENDING_STAGE_ARTIFACT_REPAIR_BLOCKED: %s" % stage)
    resolved = [os.path.abspath(path) for path in paths]
    records = [_file_record(path) for path in resolved]
    if not records or any(not item or not item.get("exists") for item in records):
        raise ValueError("PENDING_STAGE_ARTIFACT_REPAIR_MISSING: %s" % stage)
    _assert_stage_artifact_ownership(manifest, stage, resolved)
    output_contract = stage_output_contract.validate_stage_outputs(
        manifest, stage, resolved)
    previous = [item.get("path") for item in generation.get("artifacts") or []
                if isinstance(item, dict)]
    generation["outputs"] = resolved
    generation["artifacts"] = records
    if output_contract is not None:
        generation["output_contract"] = output_contract
    manifest["outputs"] = [
        item for item in manifest.get("outputs") or []
        if not (isinstance(item, dict) and item.get("kind") == stage)]
    for path in resolved:
        add_output(manifest, path, kind=stage)
    invalidate_from(manifest, stage)
    manifest.setdefault("migrations", []).append({
        "kind": "pending_stage_artifact_ownership_v1",
        "stage": stage,
        "migrated_at": datetime.now().isoformat(timespec="seconds"),
        "previous_paths": previous,
        "current_paths": resolved,
    })
    manifest["status"] = "needs_%s_approval" % stage
    return {"stage": stage, "artifacts": records}


def add_stage_artifacts(manifest, stage, paths):
    """Add artifacts to a stage's generation record and invalidate downstream approvals.

    Use this when additional artifacts (e.g., manually generated boards) need to be
    included in a stage after its initial mark_generation_finished. The function:
      1. Merges new artifacts with existing ones (dedup by path)
      2. Recomputes the stage's approval hash
      3. Invalidates all downstream stage approvals (they depend on this stage's hash)

    This is the ONLY safe way to add artifacts post-generation. Directly modifying
    manifest["generation"][stage]["artifacts"] without calling this function will
    cause approval_is_current to fail (hash mismatch).
    """
    if stage not in BASE_DEPENDENCIES and stage != "video":
        raise ValueError("未知生成阶段: %s" % stage)
    _assert_stage_artifact_ownership(manifest, stage, paths)
    generation = manifest.setdefault("generation", {}).setdefault(stage, {})
    existing = list(generation.get("artifacts") or [])
    existing_index = {
        record.get("path"): index for index, record in enumerate(existing)
        if isinstance(record, dict) and record.get("path")
    }
    changed = False

    new_records = []
    for path in paths:
        record = _file_record(path)
        if record and record.get("exists"):
            if record["path"] not in existing_index:
                new_records.append(record)
                existing_index[record["path"]] = len(existing)
                existing.append(record)
                changed = True
            elif existing[existing_index[record["path"]]] != record:
                # The same checkpoint path may be atomically rewritten when a
                # confirmation record is embedded. Refresh its content identity
                # instead of leaving a stale hash in the manifest.
                existing[existing_index[record["path"]]] = record
                changed = True

    if not changed:
        return manifest  # nothing new to add

    generation["artifacts"] = existing
    generation.setdefault("outputs", []).extend(
        r["path"] for r in new_records if r["path"] not in (generation.get("outputs") or []))
    output_contract = stage_output_contract.validate_stage_outputs(
        manifest, stage, [record["path"] for record in existing])
    if output_contract is not None:
        generation["output_contract"] = output_contract

    # Recompute approval hash with the expanded artifact set
    if manifest.get("approvals", {}).get(stage):
        manifest.setdefault("approval_hashes", {})[stage] = _approval_input_hash(manifest, stage)
        # Invalidate downstream approvals — they were built on the old hash
        invalidate_from(manifest, stage, include_stage=False)

    return manifest


def _approval_input_hash(manifest, stage, *, refresh=False):
    """Hash stage outputs plus the exact upstream approvals they were built on."""
    records = _stage_artifacts(manifest, stage, refresh=refresh)
    dependencies = {
        dependency: manifest.get("approval_hashes", {}).get(dependency)
        for dependency in generation_dependencies(manifest, stage)
    }
    return _digest(json.dumps(
        {"artifacts": records, "dependencies": dependencies},
        sort_keys=True, ensure_ascii=False))


def stage_input_fingerprint(manifest, stage, *, refresh=True):
    """Return the exact stage-input identity used by customer approval.

    Presentation receipts use the same fingerprint so a readable preview can
    never authorize machine-readable source bytes that changed after display.
    """
    if stage not in STAGES:
        raise ValueError("REVIEW_PRESENTATION_STAGE_INVALID: %s" % stage)
    generation = (manifest.get("generation") or {}).get(stage) or {}
    if generation.get("status") not in {"pending_approval", "approved"}:
        raise ValueError("REVIEW_PRESENTATION_STAGE_INPUT_REQUIRED: %s" % stage)
    artifacts = _stage_artifacts(manifest, stage, refresh=refresh)
    if not artifacts or any(not item.get("exists") for item in artifacts):
        raise ValueError("REVIEW_PRESENTATION_STAGE_INPUT_STALE: %s" % stage)
    _validate_persisted_stage_output_contract(manifest, stage, artifacts)
    return _approval_input_hash(manifest, stage, refresh=refresh)


def approval_is_current(manifest, stage):
    """Check approval against bytes currently on disk, not cached manifest data."""
    if not manifest.get("approvals", {}).get(stage):
        return False
    artifacts = _stage_artifacts(manifest, stage, refresh=True)
    if not artifacts or any(not item.get("exists") for item in artifacts):
        return False
    if any(not approval_is_current(manifest, dependency)
           for dependency in generation_dependencies(manifest, stage)):
        return False
    if (stage in CUSTOMER_EVIDENCE_STAGES and
            not customer_approval_evidence_is_current(manifest, stage)):
        return False
    return manifest.get("approval_hashes", {}).get(stage) == _approval_input_hash(
        manifest, stage, refresh=True)


def identity_gate(manifest, *, client=None, asset_dir=None):
    """Reusable run/client identity gate for orchestrators and generation code.

    Agent runtime metadata is deliberately not gated: a run may be resumed by
    Kilo, Codex, Hermes, WorkBuddy, or another compatible host.
    """
    manifest_client = manifest.get("client")
    validate_client(manifest_client)
    validate_run_id(manifest.get("run_id"))
    identity = manifest.get("identity") or {}
    if not manifest_client or identity.get("client") != manifest_client:
        raise ValueError("RUN_IDENTITY_MISMATCH: manifest client 与 identity 不一致")
    if client is not None and client != manifest_client:
        raise ValueError("RUN_IDENTITY_MISMATCH: 当前 client 与 run 不一致")
    expected_assets = os.path.abspath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", manifest_client))
    recorded_assets = identity.get("asset_dir")
    if recorded_assets and os.path.abspath(recorded_assets) != expected_assets:
        raise ValueError("RUN_IDENTITY_MISMATCH: manifest asset_dir 与 client 不一致")
    if asset_dir is not None and os.path.abspath(asset_dir) != expected_assets:
        raise ValueError("RUN_IDENTITY_MISMATCH: 当前 asset_dir 与 run 不一致")
    return True


def run_output_root(manifest):
    identity_gate(manifest)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, "output", manifest["client"], manifest["run_id"])


def require_run_output(manifest, path, *, label="output", must_exist=False):
    return require_contained_path(
        run_output_root(manifest), path, label=label, must_exist=must_exist)


IDENTITY_PLATE_PSEUDO_STAGES = (
    "identity_plate_reference", "identity_plate_submission_capture",
    "identity_plate_generation", "identity_plate_review",
)


def _document_identity_contract(manifest):
    profile = str((manifest.get("chain_profile") or {}).get("profile") or "")
    if profile != "document_explainer":
        return None
    generation = (manifest.get("generation") or {}).get("render_plan") or {}
    primary = generation.get("primary_artifact") or {}
    path = primary.get("path")
    current = file_record(path)
    if (not current or not current.get("exists") or
            current.get("sha256") != primary.get("sha256")):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    identity = plan.get("identity_plate_generation") or {}
    request = identity.get("request") or {}
    if identity.get("required_before_video") is not True or not request:
        return None
    return {"plan": plan, "plan_record": current, "request": request}


def identity_plate_reference_required(manifest):
    return _document_identity_contract(manifest) is not None


def _approval_marker_is_current(marker, approval, *, expected_kind):
    path = approval.get("approval_path")
    current = file_record(path)
    if (not current or not current.get("exists") or
            current.get("sha256") != approval.get("marker_sha256") or
            marker.get("kind") != expected_kind or
            marker.get("status") != "confirmed" or
            marker.get("confirmed_by_customer") is not True or
            marker.get("confirmation_source") != "customer_dialogue" or
            not str(marker.get("customer_confirmation") or "").strip()):
        return False
    identity = marker.get("approval_artifact_sha256")
    if (not isinstance(identity, str) or len(identity) != 64 or
            marker.get("artifact_short_id") != identity[:12]):
        return False
    canonical = {key: value for key, value in marker.items()
                 if key not in {"approval_artifact_sha256", "artifact_short_id"}}
    return _digest(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"))) == identity


def identity_plate_reference_is_current(manifest):
    contract = _document_identity_contract(manifest)
    if not contract:
        return not identity_plate_reference_required(manifest)
    state = manifest.get("identity_plate_generation") or {}
    if state.get("status") not in {
            "reference_confirmed_pending_submission_capture",
            "submission_capture_pending_customer_confirmation",
            "submission_confirmed_ready_to_create", "submitting", "submitted",
            "generated_pending_customer_confirmation", "confirmed", "failed"}:
        return False
    request = contract["request"]
    expected_content = {
        "render_plan": contract["plan_record"],
        "planned_request_fingerprint": request.get(
            "planned_submission_request_fingerprint"),
        "planned_request_spec": request.get("planned_submission_request_spec"),
        "source_references": request.get("source_references") or [],
        "catalog_contract": request.get("catalog_contract") or {},
    }
    approval = state.get("reference_approval") or {}
    try:
        with open(approval.get("approval_path"), encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        state.get("render_plan") == contract["plan_record"] and
        state.get("planned_request_fingerprint") ==
        expected_content["planned_request_fingerprint"] and
        marker.get("content") == expected_content and
        marker.get("content_fingerprint") == _digest(json.dumps(
            expected_content, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))) and
        _approval_marker_is_current(
            marker, approval,
            expected_kind="identity_plate_reference_customer_approval"))


def identity_plate_submission_is_current(manifest):
    if not identity_plate_reference_is_current(manifest):
        return False
    contract = _document_identity_contract(manifest)
    if not contract:
        return False
    state = manifest.get("identity_plate_generation") or {}
    if state.get("status") not in {
            "submission_confirmed_ready_to_create", "submitting", "submitted",
            "generated_pending_customer_confirmation", "confirmed", "failed"}:
        return False
    capture = state.get("submission_capture") or {}
    capture_record = capture.get("packet") or {}
    preview_record = capture.get("preview") or {}
    if (not file_record_is_current(capture_record) or
            not file_record_is_current(preview_record)):
        return False
    try:
        with open(capture_record.get("path"), encoding="utf-8") as handle:
            packet = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    payload = packet.get("payload") or {}
    payload_fingerprint = _digest(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    request = contract["request"]
    if (packet.get("kind") != "identity_plate_submission_capture" or
            packet.get("status") != "pending_customer_confirmation" or
            packet.get("render_plan") != contract["plan_record"] or
            packet.get("planned_request_fingerprint") != request.get(
                "planned_submission_request_fingerprint") or
            packet.get("payload_fingerprint") != payload_fingerprint or
            capture.get("payload_fingerprint") != payload_fingerprint):
        return False
    approval = state.get("submission_approval") or {}
    try:
        with open(approval.get("approval_path"), encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    expected_content = {
        "render_plan": contract["plan_record"],
        "packet": capture_record,
        "preview": preview_record,
        "payload_fingerprint": payload_fingerprint,
        "catalog_fingerprint": packet.get("catalog_fingerprint"),
        "source_fingerprint": request.get("source_fingerprint"),
    }
    return bool(
        marker.get("content") == expected_content and
        marker.get("content_fingerprint") == _digest(json.dumps(
            expected_content, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))) and
        _approval_marker_is_current(
            marker, approval,
            expected_kind="identity_plate_submission_customer_approval"))


def _current_json_record(record):
    if not file_record_is_current(record):
        return None
    try:
        with open(record.get("path"), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def identity_plate_generation_candidate_is_current(manifest):
    """Verify the paid task result and deterministic QC, without approving it."""
    if not identity_plate_submission_is_current(manifest):
        return False
    contract = _document_identity_contract(manifest)
    state = manifest.get("identity_plate_generation") or {}
    if (not contract or state.get("status") not in {
            "generated_pending_customer_confirmation", "confirmed"}):
        return False
    output = state.get("output") or {}
    candidate = state.get("candidate") or {}
    result_record = state.get("generation_result") or {}
    result = _current_json_record(result_record)
    if (not file_record_is_current(output) or candidate != output or
            not result):
        return False
    capture_record = ((state.get("submission_capture") or {}).get("packet") or {})
    packet = _current_json_record(capture_record)
    if not packet:
        return False
    payload = packet.get("payload") or {}
    try:
        actual_qc = identity_review_contract.inspect_image(
            output["path"], expected_ratio=payload.get("ratio") or "16:9")
    except (OSError, TypeError, ValueError, RuntimeError, KeyError):
        return False
    media_qc = state.get("deterministic_media_qc") or {}
    required_qc = {"decodable", "minimum_dimensions", "expected_aspect_ratio"}
    return bool(
        actual_qc.get("passed") is True and actual_qc == media_qc and
        required_qc.issubset(set((actual_qc.get("checks") or {}).keys())) and
        all((actual_qc.get("checks") or {}).get(name) is True
            for name in required_qc) and
        result.get("kind") == "identity_plate_generation_result" and
        result.get("status") == "pending_visual_review" and
        result.get("client") == manifest.get("client") and
        str(result.get("run_id")) == str(manifest.get("run_id")) and
        result.get("task_id") == state.get("task_id") and
        result.get("submission_request_id") == state.get("submission_request_id") and
        result.get("attempt") == state.get("attempt") and
        result.get("payload_fingerprint") == packet.get("payload_fingerprint") and
        result.get("request_spec") == packet.get("request_spec") and
        result.get("candidate") == output and
        result.get("remote_url") == state.get("remote_url") and
        result.get("deterministic_media_qc") == media_qc)


def identity_plate_generation_is_complete(manifest):
    """Verify visual review plus content-bound customer confirmation."""
    if not identity_plate_generation_candidate_is_current(manifest):
        return False
    contract = _document_identity_contract(manifest)
    state = manifest.get("identity_plate_generation") or {}
    gate = (manifest.get("pre_video_gates") or {}).get("identity_plate") or {}
    asset = gate.get("asset") or {}
    output = state.get("output") or {}
    result_record = state.get("generation_result") or {}
    review_record = state.get("visual_review") or {}
    review = _current_json_record(review_record)
    media_qc = state.get("deterministic_media_qc") or {}
    approval = state.get("output_approval") or {}
    approval_record = file_record(approval.get("approval_path"))
    marker = (_current_json_record(approval_record)
              if approval_record and approval_record.get("exists") and
              approval_record.get("sha256") == approval.get("marker_sha256")
              else None)
    if not review or not marker:
        return False
    try:
        identity_review_contract.validate_visual_review(
            review, candidate=output, generation_result=result_record,
            media_qc=media_qc,
            identity_references=((contract["request"].get(
                "planned_submission_request_spec") or {}).get(
                    "image_references") or []))
    except (TypeError, ValueError):
        return False
    expected_marker_content = {
        "render_plan": contract["plan_record"], "output": output,
        "generation_result": result_record, "visual_review": review_record,
        "deterministic_media_qc": media_qc, "task_id": state.get("task_id"),
        "payload_fingerprint": ((state.get("submission_capture") or {})
                                .get("payload_fingerprint")),
    }
    current_asset = file_record(asset.get("path"))
    expected_asset_fields = {
        "path": output.get("path"), "sha256": output.get("sha256"),
        "status": "confirmed", "confirmed_by_customer": True,
        "customer_confirmation": marker.get("customer_confirmation"),
        "confirmation_source": "customer_dialogue",
        "confirmed_at": marker.get("confirmed_at"),
        "approval_path": approval.get("approval_path"),
        "marker_sha256": approval.get("marker_sha256"),
        "source_fingerprint": contract["request"].get("source_fingerprint"),
        "model": contract["request"].get("model"), "tag": "identity_plate",
        "generation_result": result_record, "visual_review": review_record,
        "deterministic_media_qc": media_qc,
    }
    return bool(
        state.get("status") == "confirmed" and
        marker.get("content") == expected_marker_content and
        marker.get("content_fingerprint") == _digest(json.dumps(
            expected_marker_content, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))) and
        _approval_marker_is_current(
            marker, approval, expected_kind="identity_plate_customer_approval") and
        gate.get("status") == "confirmed" and
        gate.get("render_plan") == contract["plan_record"] and
        gate.get("identity_request_fingerprint") ==
        contract["request"].get("planned_submission_request_fingerprint") and
        all(asset.get(key) == value for key, value in expected_asset_fields.items()) and
        current_asset and current_asset.get("exists") and
        current_asset.get("path") == output.get("path") and
        current_asset.get("sha256") == output.get("sha256"))


def workflow_requirements(manifest):
    """Return the frozen optional-stage flags recorded for this run."""
    requirements = {
        flag: bool(manifest.get(flag))
        for flag in workflow_contract.OPTIONAL_STAGE_FLAGS.values()
    }
    if recovery_test_segment_requirement_is_current(manifest):
        requirements["requires_test_segment"] = True
    return requirements


def recovery_test_segment_requirement_is_current(manifest):
    """Validate a dynamic recovery canary against current handoff bytes."""
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    segment_id = str(requirement.get("segment_id") or "").strip()
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    expected = (handoff.get("segments") or {}).get(segment_id)
    contract = requirement.get("contract") or {}
    handoff_file = handoff.get("file") or {}
    if (not segment_id or not expected or
            requirement.get("video_handoff_fingerprint") != expected or
            not file_record_is_current(contract) or
            not file_record_is_current(handoff_file)):
        return False
    try:
        with open(handoff_file["path"], encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return False
    matching = [
        segment for segment in (spec.get("segments") or [])
        if isinstance(segment, dict) and
        str(segment.get("id") or "") == segment_id]
    if len(matching) != 1:
        return False
    segment = matching[0]
    recovery = segment.get("recovery_contract") or {}
    return bool(
        segment.get("video_handoff_fingerprint") == expected and
        verify_video_handoff(segment).get("ok") and
        recovery.get("recovery_id") == requirement.get("recovery_id") and
        recovery.get("path") == contract.get("path") and
        recovery.get("sha256") == contract.get("sha256"))


def stage_required(manifest, stage):
    """Return whether a stage belongs to this run's frozen business contract."""
    if stage not in STAGES:
        raise ValueError("未知生成阶段: %s" % stage)
    generation = (manifest.get("generation") or {}).get(stage) or {}
    # Never hide a stage that already owns outputs/status, even if it came from
    # an older contract or an explicit migration.
    if generation.get("status") or generation.get("outputs") or generation.get("artifacts"):
        return True
    return workflow_contract.stage_required(stage, workflow_requirements(manifest))


def required_stage_order(manifest):
    profile = str((manifest.get("chain_profile") or {}).get("profile") or "generic_video")
    ordered = workflow_contract.required_stage_order(
        profile, workflow_requirements(manifest), include_boundaries=False)
    # Preserve any already-started legacy stage even when a newer profile no
    # longer includes it. Its evidence must be closed or explicitly migrated.
    selected = set(ordered)
    for stage in STAGES:
        if stage_required(manifest, stage):
            selected.add(stage)
    return [stage for stage in STAGES if stage in selected]


def stage_is_complete(manifest, stage):
    if approval_is_current(manifest, stage):
        return True
    generation = (manifest.get("generation") or {}).get(stage) or {}
    if generation.get("status") == "skipped":
        if stage != "derive":
            raise ValueError("UNSUPPORTED_SKIPPED_STAGE: %s" % stage)
        validate_derive_closure(manifest)
        return True
    return False


def next_required_stage(manifest):
    for stage in required_stage_order(manifest):
        if not stage_is_complete(manifest, stage):
            return stage
        if stage == "render_plan" and identity_plate_reference_required(manifest):
            if not identity_plate_reference_is_current(manifest):
                return "identity_plate_reference"
            if not identity_plate_submission_is_current(manifest):
                return "identity_plate_submission_capture"
            if not identity_plate_generation_candidate_is_current(manifest):
                return "identity_plate_generation"
            if not identity_plate_generation_is_complete(manifest):
                return "identity_plate_review"
    return None


def generation_dependencies(manifest, stage):
    """Return the approved stages required before starting ``stage``."""
    if stage not in BASE_DEPENDENCIES:
        raise ValueError("未知生成阶段: %s" % stage)
    dependencies = list(BASE_DEPENDENCIES[stage])
    requirements = workflow_requirements(manifest)
    requires_board = requirements["requires_product_board"]
    requires_usage = requirements["requires_product_usage"]
    requires_cast = requirements["requires_cast_board"]
    requires_content = requirements["requires_content_scaffold"]
    requires_motion = requirements["requires_motion_design"]
    requires_styleframe = requirements["requires_styleframe"]
    requires_audio = requirements["requires_audio"]
    def require(dependency):
        if dependency not in dependencies:
            dependencies.append(dependency)
    if stage in ("motion_design", "styleframe", "audio", "storyboard", "render_plan",
                 "test_segment", "video", "shotcraft_packaging", "captions", "final",
                 "derive") and requires_content:
        require("content_scaffold")
    if stage in ("styleframe", "audio", "storyboard", "render_plan", "test_segment",
                 "video", "shotcraft_packaging", "captions", "final", "derive") and requires_motion:
        require("motion_design")
    if stage in ("storyboard", "video") and requires_board:
        require("product_board")
    if stage in ("storyboard", "video") and requires_cast:
        require("cast_board")
    if stage in ("storyboard", "video") and requires_usage:
        require("product_usage")
    if stage in ("storyboard", "render_plan", "video", "shotcraft_packaging", "captions", "final", "derive") and requires_styleframe:
        require("styleframe")
    if stage in ("render_plan", "test_segment", "video", "shotcraft_packaging", "captions", "final", "derive") and requires_audio:
        require("audio")
    if stage == "video" and requirements["requires_test_segment"]:
        require("test_segment")
    if stage in ("captions", "final", "derive") and manifest.get("requires_shotcraft_packaging"):
        require("shotcraft_packaging")
    return tuple(dependencies)


def record_generation_requirements(manifest):
    """Persist plan-derived optional-stage requirements explicitly."""
    plan_record = manifest.get("storyboard_plan") or {}
    plan_path = plan_record.get("path") if isinstance(plan_record, dict) else None
    if plan_path and os.path.isfile(plan_path):
        recovery_test_required = recovery_test_segment_requirement_is_current(
            manifest)
        contract = plan_requirements(plan_path)
        manifest.update(contract["requirements"])
        if recovery_test_required:
            manifest["requires_test_segment"] = True
        manifest["package"] = contract.get("authored_package")
        manifest["expected_package"] = contract.get("expected_package")
        manifest["allowed_packages"] = contract.get("allowed_packages") or []
        manifest["workflow_contract_version"] = contract["contract_version"]
        if not manifest.get("chain_profile"):
            manifest["chain_profile"] = _frozen_chain_profile(plan_path)
    return manifest


def ensure_workflow_contract(manifest):
    """Migrate an in-memory manifest to the current deterministic contract.

    The migration never trusts changed plan bytes: only the exact plan record
    already bound to the run may change profile/requirements.  Callers save the
    manifest through the normal CAS path when they next mutate run state.
    """
    if not isinstance(manifest, dict):
        raise ValueError("RUN_MANIFEST_INVALID")
    previous_version = manifest.get("workflow_contract_version")
    recovery_test_required = recovery_test_segment_requirement_is_current(
        manifest)
    plan_record = manifest.get("storyboard_plan") or {}
    plan_is_current = bool(
        isinstance(plan_record, dict) and file_record_is_current(plan_record))
    if plan_is_current:
        plan = _load_plan_for_requirements(plan_record["path"])
        package_validation = workflow_contract.validate_package_binding(
            plan, require_authored=True)
        contract = package_validation["contract"]
        manifest.update(contract["requirements"])
        if recovery_test_required:
            manifest["requires_test_segment"] = True
        manifest["package"] = contract.get("authored_package")
        manifest["expected_package"] = contract.get("expected_package")
        manifest["allowed_packages"] = contract.get("allowed_packages") or []
        manifest["workflow_contract_version"] = contract["contract_version"]
        profile = manifest.get("chain_profile") or {}
        profile_stale = bool(
            profile.get("contract_version") != workflow_contract.CONTRACT_VERSION or
            profile.get("plan_path") != plan_record.get("path") or
            profile.get("plan_sha256") != plan_record.get("sha256") or
            profile.get("profile") != contract.get("profile"))
        if profile_stale:
            if profile:
                history = manifest.setdefault("workflow_contract_history", [])
                snapshot = copy.deepcopy(profile)
                snapshot["migrated_at"] = datetime.now().isoformat(timespec="seconds")
                if snapshot not in history:
                    history.append(snapshot)
            manifest["chain_profile"] = _frozen_chain_profile(plan_record["path"])
        if package_validation["ok"]:
            manifest.pop("workflow_contract_error", None)
        else:
            manifest["workflow_contract_error"] = package_validation["errors"][0]
        return manifest

    # A stale bound plan must be rebound explicitly; inferring from its current
    # bytes would silently authorize a different workflow.  Preserve recorded
    # flags so routing can lead the user back to bind-plan.
    if isinstance(plan_record, dict) and plan_record.get("path"):
        manifest["workflow_contract_error"] = "BOUND_PLAN_STALE_OR_MISSING"
    for flag in workflow_contract.OPTIONAL_STAGE_FLAGS.values():
        if flag not in manifest:
            # Legacy plan-less runs historically assumed a presenter. Keep that
            # conservative gate, while current manifests carry explicit flags.
            manifest[flag] = bool(
                not previous_version and flag == "requires_cast_board")
    manifest.setdefault("workflow_contract_version", previous_version or "legacy-unbound")
    manifest.setdefault("chain_profile", {
        "profile": "generic_video",
        "reason": "legacy_manifest_without_current_plan",
        "contract_version": manifest["workflow_contract_version"],
        "frozen": True,
    })
    return manifest


def bind_storyboard_plan(manifest, plan_path):
    """Bind the authored plan and derive one authoritative stage contract.

    A formal run must not infer its route from a PPT/brief while the manifest
    still carries the default optional-stage flags.  The plan is also part of
    the customer-approved script contract: adding or replacing it after script
    approval therefore reopens script approval instead of silently preserving
    an approval that never covered these bytes.
    """
    identity_gate(manifest)
    record = _file_record(plan_path)
    if not record or not record.get("exists"):
        raise ValueError("PLAN_UNAVAILABLE: %s" % os.path.abspath(str(plan_path)))
    plan = _load_plan_for_requirements(record["path"])
    if isinstance(plan.get("_execution_plan"), dict):
        raise ValueError(
            "EXECUTION_PLAN_CANNOT_REPLACE_AUTHORED_PLAN: 动效派生执行计划属于 "
            "motion_design 阶段，不能覆盖客户确认的 storyboard_plan。")
    if (plan.get("client") and str(plan.get("client")) != str(manifest.get("client"))):
        raise ValueError("PLAN_RUN_IDENTITY_MISMATCH: client")
    if (plan.get("run_id") and str(plan.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("PLAN_RUN_IDENTITY_MISMATCH: run_id")
    package_validation = workflow_contract.validate_package_binding(
        plan, require_authored=True)
    if not package_validation["ok"]:
        raise ValueError("PLAN_VALIDATION_FAILED: %s" %
                         "；".join(package_validation["errors"]))
    from storyboard_validator import validate_plan
    validation = validate_plan(plan)
    if not validation.get("ok"):
        raise ValueError("PLAN_VALIDATION_FAILED: %s" %
                         "；".join(validation.get("errors") or []))
    contract_keys = (
        "storyboard_plan", "requires_product_board", "requires_cast_board",
        "requires_product_usage", "requires_content_scaffold",
        "requires_motion_design", "requires_styleframe", "requires_audio",
        "requires_test_segment", "requires_shotcraft_packaging",
        "workflow_contract_version", "chain_profile", "package",
        "expected_package", "allowed_packages",
    )
    contract_before = {key: copy.deepcopy(manifest.get(key)) for key in contract_keys}
    previous = manifest.get("storyboard_plan") or {}
    unchanged = bool(
        previous.get("path") == record.get("path") and
        previous.get("sha256") == record.get("sha256"))
    manifest["storyboard_plan"] = record
    record_generation_requirements(manifest)
    manifest.pop("workflow_contract_error", None)
    profile = manifest.get("chain_profile") or {}
    if (not unchanged or profile.get("plan_sha256") != record.get("sha256") or
            profile.get("plan_path") != record.get("path")):
        frozen = _frozen_chain_profile(record["path"])
        if frozen:
            manifest["chain_profile"] = frozen
    contract_after = {key: copy.deepcopy(manifest.get(key)) for key in contract_keys}
    contract_changed = contract_before != contract_after
    if unchanged:
        return {"changed": contract_changed, "plan_changed": False,
                "script_reapproval_required": False,
                "plan": record, "chain_profile": manifest.get("chain_profile")}

    script_generation = (manifest.get("generation") or {}).get("script") or {}
    script_was_approved = bool(
        manifest.get("approvals", {}).get("script") or
        script_generation.get("status") == "approved")
    if script_generation:
        add_stage_artifacts(manifest, "script", [record["path"]])
    if script_was_approved:
        invalidate_from(manifest, "script")
        manifest.setdefault("generation", {}).setdefault("script", {})[
            "status"] = "pending_approval"
        manifest["status"] = "needs_script_approval"
    return {"changed": True, "plan_changed": True,
            "script_reapproval_required": script_was_approved,
            "plan": record, "chain_profile": manifest.get("chain_profile")}


def validate_execution_plan_binding(manifest, execution_plan_path):
    """Validate a motion-enriched plan without rebinding script approval."""
    identity_gate(manifest)
    execution_record = _file_record(execution_plan_path)
    if not execution_record or not execution_record.get("exists"):
        raise ValueError("EXECUTION_PLAN_UNAVAILABLE")
    plan = _load_plan_for_requirements(execution_record["path"])
    metadata = plan.get("_execution_plan") or {}
    if metadata.get("kind") != "motion_enriched_storyboard_plan":
        raise ValueError("EXECUTION_PLAN_CONTRACT_MISSING")
    if (str(metadata.get("client") or "") != str(manifest.get("client") or "") or
            str(metadata.get("run_id") or "") != str(manifest.get("run_id") or "")):
        raise ValueError("EXECUTION_PLAN_RUN_IDENTITY_MISMATCH")
    source_record = metadata.get("source_plan") or {}
    design_record = metadata.get("motion_design") or {}
    if (source_record != (manifest.get("storyboard_plan") or {}) or
            not file_record_is_current(source_record) or
            not file_record_is_current(design_record)):
        raise ValueError("EXECUTION_PLAN_SOURCE_STALE")
    if not approval_is_current(manifest, "motion_design"):
        raise ValueError("EXECUTION_PLAN_MOTION_APPROVAL_REQUIRED")
    motion_artifacts = {
        (item.get("path"), item.get("sha256"))
        for item in _stage_artifacts(manifest, "motion_design", refresh=True)
        if isinstance(item, dict) and item.get("exists")
    }
    design_identity = (design_record.get("path"), design_record.get("sha256"))
    execution_identity = (execution_record.get("path"), execution_record.get("sha256"))
    bound_execution = (
        manifest.get("generation", {}).get("motion_design", {}).get("execution_plan")
        or {})
    execution_is_bound = (
        bound_execution == execution_record and
        file_record_is_current(bound_execution))
    if (design_identity not in motion_artifacts or
            (execution_identity not in motion_artifacts and not execution_is_bound)):
        raise ValueError("EXECUTION_PLAN_NOT_BOUND_TO_MOTION_STAGE")
    return {"plan": execution_record, "source_plan": source_record,
            "motion_design": design_record}


def generation_gate(manifest, stage, *, client=None, asset_dir=None):
    """Validate a generation transition without mutating the manifest.

    This is the single gate for UI/orchestrator code. It prevents a batch job
    from submitting dependent image tasks before the previous asset was
    approved, while still allowing projects that do not require a product board.
    """
    identity_gate(manifest, client=client, asset_dir=asset_dir)
    if manifest.get("workflow_contract_error"):
        raise ValueError(
            "WORKFLOW_CONTRACT_BLOCKED: %s" % manifest["workflow_contract_error"])
    if (str((manifest.get("chain_profile") or {}).get("profile") or "") ==
            "document_explainer" and stage in {"test_segment", "video"} and
            identity_plate_reference_required(manifest) and
            not identity_plate_generation_is_complete(manifest)):
        raise ValueError(
            "GENERATION_BLOCKED_IDENTITY_PLATE_REVIEW_REQUIRED: %s" % stage)
    missing = [dependency for dependency in generation_dependencies(manifest, stage)
                if not approval_is_current(manifest, dependency)]
    if missing:
        raise ValueError(
            "GENERATION_BLOCKED: %s 依赖已确认阶段: %s" %
            (stage, ", ".join(missing))
        )
    return True


def validate_pre_video_gates(manifest, prompt_review_path=None):
    """Enforce document-explainer paid prerequisites outside the UI router."""
    profile = str((manifest.get("chain_profile") or {}).get("profile") or "")
    if profile != "document_explainer":
        return True
    if not approval_is_current(manifest, "render_plan"):
        raise ValueError("PRE_VIDEO_RENDER_PLAN_APPROVAL_REQUIRED")
    generation = (manifest.get("generation") or {}).get("render_plan") or {}
    primary = generation.get("primary_artifact") or {}
    current_plan = file_record(primary.get("path"))
    if (not current_plan or not current_plan.get("exists") or
            current_plan.get("sha256") != primary.get("sha256")):
        raise ValueError("PRE_VIDEO_RENDER_PLAN_STALE")
    if not identity_plate_submission_is_current(manifest):
        raise ValueError("PRE_VIDEO_IDENTITY_SUBMISSION_APPROVAL_REQUIRED")
    if not identity_plate_generation_is_complete(manifest):
        raise ValueError("PRE_VIDEO_GENERATED_IDENTITY_APPROVAL_REQUIRED")
    gates = manifest.get("pre_video_gates") or {}
    identity = gates.get("identity_plate") or {}
    asset = identity.get("asset") or {}
    current_asset = file_record(asset.get("path"))
    if (identity.get("status") != "confirmed" or
            identity.get("render_plan") != current_plan or
            asset.get("confirmed_by_customer") is not True or
            asset.get("confirmation_source") != "customer_dialogue" or
            not current_asset or not current_asset.get("exists") or
            current_asset.get("path") != asset.get("path") or
            current_asset.get("sha256") != asset.get("sha256")):
        raise ValueError("PRE_VIDEO_IDENTITY_PLATE_CONFIRMATION_REQUIRED")
    prompt_gate = gates.get("video_prompt_review") or {}
    review_record = prompt_gate.get("review") or {}
    if (prompt_gate.get("status") != "confirmed" or
            prompt_gate.get("render_plan") != current_plan or
            not file_record_is_current(review_record)):
        raise ValueError("PRE_VIDEO_EXACT_PROMPT_REVIEW_REQUIRED")
    if not isinstance(prompt_review_path, str) or not os.path.isfile(prompt_review_path):
        raise ValueError("PRE_VIDEO_PROMPT_REVIEW_PATH_REQUIRED")
    supplied = file_record(prompt_review_path)
    if (supplied.get("path"), supplied.get("sha256")) != (
            review_record.get("path"), review_record.get("sha256")):
        raise ValueError("PRE_VIDEO_PROMPT_REVIEW_BINDING_MISMATCH")
    try:
        with open(prompt_review_path, encoding="utf-8") as handle:
            review = json.load(handle)
        prompt_review_contract.assert_current(review, path=prompt_review_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("PRE_VIDEO_PROMPT_REVIEW_STALE: %s" % exc) from exc
    if (review.get("stage") != "video" or review.get("status") != "confirmed" or
            review.get("confirmed_by_customer") is not True):
        raise ValueError("PRE_VIDEO_PROMPT_REVIEW_NOT_CONFIRMED")
    return True


def mark_generation_started(manifest, stage):
    """Record an in-flight stage only after its dependency gate passes."""
    generation_gate(manifest, stage)
    _archive_generation_attempt(manifest, stage, superseded_by="running")
    invalidate_from(manifest, stage)
    manifest.setdefault("generation", {})[stage] = {
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    manifest["status"] = "generating_%s" % stage
    return manifest


def mark_generation_failed(manifest, stage, error, *, error_code=None,
                           retryable=True, diagnostics=()):
    """Close a running generation transaction without losing its plan.

    A failed render is a terminal state for that attempt, not an in-flight
    render.  Planned inputs/outputs remain on the generation record so the
    router can offer an explicit retry or repair action.  Diagnostic paths are
    optional internal evidence and never become customer approval artifacts.
    """
    if stage not in BASE_DEPENDENCIES:
        raise ValueError("GENERATION_FAILURE_STAGE_UNKNOWN: %s" % stage)
    generation = (manifest.get("generation") or {}).get(stage)
    if not isinstance(generation, dict) or generation.get("status") != "running":
        raise ValueError("GENERATION_FAILURE_REQUIRES_RUNNING: %s" % stage)
    message = str(error or "").strip()
    if not message:
        raise ValueError("GENERATION_FAILURE_REASON_REQUIRED: %s" % stage)
    diagnostic_records = []
    for path in diagnostics or ():
        record = _file_record(path)
        if record and record.get("exists"):
            diagnostic_records.append(record)
    attempts = list(generation.get("failed_attempts") or [])
    failure = {
        "failed_at": datetime.now().isoformat(timespec="seconds"),
        "error_code": str(error_code or "GENERATION_FAILED"),
        "message": message[:1000],
        "retryable": bool(retryable),
        "diagnostics": diagnostic_records,
    }
    attempts.append(failure)
    generation.update({
        "status": "failed",
        "failed_at": failure["failed_at"],
        "failure": failure,
        "failed_attempts": attempts,
    })
    generation.pop("lease", None)
    manifest["status"] = "failed_%s" % stage
    return manifest


def mark_generation_blocked(manifest, stage, reason, *, blocker_code,
                            recovery_action, diagnostics=(), evidence=None):
    """Close a running paid stage that needs a new customer-approved contract."""
    if stage not in BASE_DEPENDENCIES:
        raise ValueError("GENERATION_BLOCK_STAGE_UNKNOWN: %s" % stage)
    generation = (manifest.get("generation") or {}).get(stage)
    if not isinstance(generation, dict) or generation.get("status") != "running":
        raise ValueError("GENERATION_BLOCK_REQUIRES_RUNNING: %s" % stage)
    message = str(reason or "").strip()
    action = str(recovery_action or "").strip()
    if not message or not action or not str(blocker_code or "").strip():
        raise ValueError("GENERATION_BLOCK_CONTRACT_REQUIRED: %s" % stage)
    diagnostic_records = []
    for path in diagnostics or ():
        record = _file_record(path)
        if record and record.get("exists"):
            diagnostic_records.append(record)
    blocked = {
        "blocked_at": datetime.now().isoformat(timespec="seconds"),
        "blocker_code": str(blocker_code),
        "message": message[:1000],
        "recovery_action": action[:1500],
        "diagnostics": diagnostic_records,
        "evidence": copy.deepcopy(evidence or {}),
    }
    history = list(generation.get("blocked_attempts") or [])
    history.append(blocked)
    generation.update({
        "status": "blocked", "blocked_at": blocked["blocked_at"],
        "blocker": blocked, "blocked_attempts": history,
    })
    generation.pop("lease", None)
    manifest["status"] = "blocked_%s" % stage
    return manifest


def record_generation_blocked_if_running(
        manifest_path, stage, reason, *, blocker_code, recovery_action,
        diagnostics=(), evidence=None, retries=3):
    """CAS-safely replace stale running state with an explicit recovery gate."""
    for _ in range(max(1, int(retries))):
        manifest = load_manifest(manifest_path)
        generation = (manifest.get("generation") or {}).get(stage) or {}
        if generation.get("status") != "running":
            return manifest
        expected_revision = int(manifest.get("revision") or 0)
        mark_generation_blocked(
            manifest, stage, reason, blocker_code=blocker_code,
            recovery_action=recovery_action, diagnostics=diagnostics,
            evidence=evidence)
        try:
            save_manifest(
                manifest, manifest_path, expected_revision=expected_revision)
            return manifest
        except ManifestConflictError:
            continue
    raise ManifestConflictError("GENERATION_BLOCK_CONFLICT: %s" % stage)


def _video_settlement_summary(results):
    rows = [item for item in (results or []) if isinstance(item, dict)]
    succeeded = [item for item in rows if item.get("ok") is True]
    failed = [item for item in rows if item.get("ok") is not True]
    return {
        "total": len(rows),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "failed_segment_ids": [str(item.get("segment_id")) for item in failed],
        "resumable_segment_ids": [
            str(item.get("segment_id")) for item in failed
            if item.get("resume_available")],
        "provider_privacy_segment_ids": [
            str(item.get("segment_id")) for item in failed
            if item.get("provider_error")],
        "task_ids": [str(item.get("taskId")) for item in rows if item.get("taskId")],
    }


def settle_video_generation(manifest, results_path, results):
    """Close the raw paid-video worker transaction from persisted results.

    Segment generation is not the final video approval: successful takes still
    require take review and assembly.  Failures route to recovery, preserving
    remote task identities so a timeout/download error cannot be mistaken for
    permission to submit a second paid request.
    """
    generation = manifest.setdefault("generation", {}).get("video") or {}
    result_record = _file_record(results_path)
    if not result_record or not result_record.get("exists"):
        raise ValueError("VIDEO_SETTLEMENT_RESULTS_REQUIRED")
    if not isinstance(results, list) or not results:
        raise ValueError("VIDEO_SETTLEMENT_RESULTS_EMPTY")
    expected_ids = set(
        (((manifest.get("handoffs") or {}).get("video") or {})
         .get("segments") or {}).keys())
    result_ids = [str(item.get("segment_id") or "") for item in results
                  if isinstance(item, dict)]
    if (not expected_ids or len(result_ids) != len(results) or
            set(result_ids) != expected_ids or len(result_ids) != len(set(result_ids))):
        raise ValueError("VIDEO_SETTLEMENT_SEGMENT_COVERAGE_MISMATCH")
    summary = _video_settlement_summary(results)
    settlement_fingerprint = _digest(json.dumps({
        "results": result_record, "summary": summary,
        "handoff": ((manifest.get("handoffs") or {}).get("video") or {}),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    existing = generation.get("settlement") or {}
    if existing.get("fingerprint") == settlement_fingerprint:
        return existing
    if generation.get("status") != "running":
        raise ValueError("VIDEO_SETTLEMENT_REQUIRES_RUNNING")
    settlement = {
        "fingerprint": settlement_fingerprint,
        "settled_at": datetime.now().isoformat(timespec="seconds"),
        "results": result_record,
        "summary": summary,
    }
    history = list(generation.get("settlement_history") or [])
    history.append(copy.deepcopy(settlement))
    generation["settlement"] = settlement
    generation["settlement_history"] = history
    generation.pop("lease", None)
    if summary["provider_privacy_segment_ids"]:
        first = next(item for item in results if item.get("provider_error"))
        blocked = {
            "blocked_at": settlement["settled_at"],
            "blocker_code": "PRIVACY_FALLBACK_REAPPROVAL_REQUIRED",
            "message": str(first.get("error") or "provider privacy rejection")[:1000],
            "recovery_action": str(
                first.get("action") or
                "重新选择可执行模型/引用合同，重新 preflight、capture-video 并由客户确认。")[:1500],
            "diagnostics": [result_record],
            "evidence": {
                "segments": [{
                    "segment_id": item.get("segment_id"),
                    "task_id": item.get("taskId"),
                    "provider_error": item.get("provider_error"),
                    "fallback_target_model": item.get("fallback_target_model"),
                    "billing_status": item.get("billing_status") or "unknown",
                } for item in results if item.get("provider_error")],
            },
        }
        generation.update({
            "status": "blocked", "blocked_at": blocked["blocked_at"],
            "blocker": blocked,
            "blocked_attempts": list(generation.get("blocked_attempts") or []) +
                                [copy.deepcopy(blocked)],
        })
        manifest["status"] = "blocked_video"
    elif summary["failed"]:
        generation["status"] = "recovery_required"
        generation["recovery"] = {
            "required_at": settlement["settled_at"],
            "results": result_record,
            "failed_segment_ids": summary["failed_segment_ids"],
            "resumable_segment_ids": summary["resumable_segment_ids"],
            "billing_status": "unknown",
            "action": (
                "优先恢复已登记 task/download；终态失败须形成 rejected take review "
                "并用 new-video-attempt 授权，禁止直接重提。"),
        }
        manifest["status"] = "needs_video_recovery"
    else:
        generation["status"] = "awaiting_take_review"
        generation["take_review"] = {
            "required_at": settlement["settled_at"],
            "results": result_record,
            "segment_ids": result_ids,
        }
        manifest["status"] = "needs_video_take_review"
    return settlement


def record_video_generation_settlement(manifest_path, results_path, results,
                                       *, retries=3):
    """CAS-safe crash-recovery and normal closeout entrypoint."""
    for _ in range(max(1, int(retries))):
        manifest = load_manifest(manifest_path)
        expected_revision = int(manifest.get("revision") or 0)
        settlement = settle_video_generation(manifest, results_path, results)
        try:
            save_manifest(manifest, manifest_path,
                          expected_revision=expected_revision)
            return manifest, settlement
        except ManifestConflictError:
            continue
    raise ManifestConflictError("VIDEO_SETTLEMENT_CONFLICT")


def record_generation_failure(manifest_path, stage, error, *, error_code=None,
                              retryable=True, diagnostics=(), retries=3):
    """CAS-safe convenience wrapper for closing a persisted transaction."""
    for _ in range(max(1, int(retries))):
        manifest = load_manifest(manifest_path)
        expected_revision = int(manifest.get("revision") or 0)
        mark_generation_failed(
            manifest, stage, error, error_code=error_code,
            retryable=retryable, diagnostics=diagnostics)
        try:
            save_manifest(
                manifest, manifest_path, expected_revision=expected_revision)
            return manifest
        except ManifestConflictError:
            continue
    raise ManifestConflictError("GENERATION_FAILURE_CONFLICT: %s" % stage)


def record_generation_failure_if_running(manifest_path, stage, error, *,
                                         error_code=None, retryable=True,
                                         diagnostics=(), retries=3,
                                         owner_token=None):
    """CAS-safely close a transaction when an exception escapes its worker.

    Preflight failures happen before a transaction exists and concurrent workers
    may already have closed an attempt.  Those states are intentionally no-ops;
    only an actually persisted ``running`` record is changed to ``failed``.
    """
    for _ in range(max(1, int(retries))):
        manifest = load_manifest(manifest_path)
        generation = (manifest.get("generation") or {}).get(stage) or {}
        if generation.get("status") != "running":
            return manifest
        lease = generation.get("lease") or {}
        if (owner_token is not None and
                lease.get("owner_token") != str(owner_token)):
            # A stale worker must never close a newer worker's transaction.
            return manifest
        expected_revision = int(manifest.get("revision") or 0)
        mark_generation_failed(
            manifest, stage, error, error_code=error_code,
            retryable=retryable, diagnostics=diagnostics)
        try:
            save_manifest(
                manifest, manifest_path, expected_revision=expected_revision)
            return manifest
        except ManifestConflictError:
            continue
    raise ManifestConflictError("GENERATION_FAILURE_CONFLICT: %s" % stage)


def close_failed_generation(stage, *, error_code="GENERATION_FAILED",
                            retryable=True):
    """Decorate a worker that has a named ``manifest_path`` argument.

    This is the shared exception boundary for local rendering stages.  It keeps
    library callers and CLI callers consistent and preserves the original
    exception even if failure closeout itself encounters a concurrent update.
    """
    def decorator(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (OSError, ValueError, RuntimeError, TypeError,
                    subprocess.SubprocessError) as exc:
                try:
                    bound = signature.bind_partial(*args, **kwargs)
                    manifest_path = bound.arguments.get("manifest_path")
                    if manifest_path and os.path.isfile(manifest_path):
                        record_generation_failure_if_running(
                            manifest_path, stage, exc, error_code=error_code,
                            retryable=retryable)
                except (OSError, ValueError, RuntimeError, TypeError) as closeout_exc:
                    if hasattr(exc, "add_note"):
                        exc.add_note("generation closeout failed: %s" % closeout_exc)
                raise

        return wrapped
    return decorator


def acquire_generation_lease(manifest_path, stage, owner_token, *, force=False,
                             ttl_seconds=300, retries=3):
    """Atomically acquire one paid-generation owner for a stage.

    A second Agent/process may resume only after the active lease expires. The
    deterministic remote task/request id remains the actual paid-work identity;
    this lease closes the smaller race before that id has been persisted.
    """
    owner_token = str(owner_token or "").strip()
    if not owner_token:
        raise ValueError("GENERATION_OWNER_REQUIRED")
    for _ in range(max(1, int(retries))):
        manifest = load_manifest(manifest_path)
        generation = (manifest.get("generation") or {}).get(stage) or {}
        now_epoch = datetime.now().timestamp()
        lease = generation.get("lease") or {}
        active = bool(
            generation.get("status") == "running" and lease.get("owner_token") and
            float(lease.get("expires_at_epoch") or 0) > now_epoch)
        if active and lease.get("owner_token") != owner_token:
            raise ValueError(
                "GENERATION_ALREADY_RUNNING: %s 当前由另一执行者处理；请等待或恢复其远程 task" % stage)
        expected_revision = int(manifest.get("revision") or 0)
        if generation.get("status") != "running" or force:
            mark_generation_started(manifest, stage)
            generation = manifest["generation"][stage]
        generation["lease"] = {
            "owner_token": owner_token,
            "acquired_at": datetime.now().isoformat(timespec="seconds"),
            "expires_at_epoch": now_epoch + max(30, int(ttl_seconds)),
        }
        try:
            save_manifest(manifest, manifest_path, expected_revision=expected_revision)
            return manifest
        except ManifestConflictError:
            continue
    raise ManifestConflictError("GENERATION_LEASE_CONFLICT: %s" % stage)


def heartbeat_generation_lease(manifest_path, stage, owner_token, *, ttl_seconds=300):
    """Refresh a lease only for its current owner; stale workers fail closed."""
    for _ in range(3):
        manifest = load_manifest(manifest_path)
        generation = (manifest.get("generation") or {}).get(stage) or {}
        lease = generation.get("lease") or {}
        if (generation.get("status") != "running" or
                lease.get("owner_token") != owner_token):
            raise ValueError("GENERATION_LEASE_LOST: %s" % stage)
        expected_revision = int(manifest.get("revision") or 0)
        lease["heartbeat_at"] = datetime.now().isoformat(timespec="seconds")
        lease["expires_at_epoch"] = datetime.now().timestamp() + max(30, int(ttl_seconds))
        try:
            save_manifest(manifest, manifest_path, expected_revision=expected_revision)
            return manifest
        except ManifestConflictError:
            continue
    raise ManifestConflictError("GENERATION_LEASE_HEARTBEAT_CONFLICT: %s" % stage)


def mark_generation_finished(manifest, stage, outputs=(), *, allow_bootstrap=False):
    """Record outputs and leave the stage awaiting explicit customer approval."""
    if stage == "video":
        raise ValueError("VIDEO_STAGE_SPECIALIZED_FINISH_REQUIRED")
    if stage not in BASE_DEPENDENCIES:
        raise ValueError("未知生成阶段: %s" % stage)
    current = (manifest.get("generation") or {}).get(stage) or {}
    if (stage in TRANSACTIONAL_GENERIC_STAGES and not allow_bootstrap and
            current.get("status") != "running"):
        raise ValueError(
            "GENERATION_FINISH_REQUIRES_RUNNING: %s 必须先提交 start-stage；"
            "失败/未开始事务不能直接伪装成待审批" % stage)
    outputs = list(outputs)
    if stage == "script":
        plan_record = manifest.get("storyboard_plan") or {}
        if (not isinstance(plan_record, dict) or
                not file_record_is_current(plan_record)):
            raise ValueError(
                "SCRIPT_PLAN_BINDING_REQUIRED: script 完成前必须先用 bind-plan 绑定当前 "
                "storyboard_plan.json；脚本审批必须覆盖同一份逐段分镜字节。")
        if plan_record["path"] not in outputs:
            outputs.append(plan_record["path"])
    outputs = _expand_stage_outputs(stage, outputs)
    _assert_stage_artifact_ownership(manifest, stage, outputs)
    output_records = [_file_record(path) for path in outputs]
    missing = [os.path.abspath(path) for path, record in zip(outputs, output_records)
               if not record or not record.get("exists")]
    if missing:
        raise ValueError("GENERATION_OUTPUT_MISSING: %s" % ", ".join(missing))
    if not output_records:
        raise ValueError("GENERATION_OUTPUT_MISSING: %s 阶段没有可审批产物" % stage)
    output_contract = stage_output_contract.validate_stage_outputs(
        manifest, stage, outputs)
    generation = manifest.setdefault("generation", {})
    generation[stage] = {
        "status": "pending_approval",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "outputs": [os.path.abspath(path) for path in outputs],
        "artifacts": output_records,
    }
    if output_contract is not None:
        generation[stage]["output_contract"] = output_contract
    for path in outputs:
        add_output(manifest, path, kind=stage)
    manifest["status"] = "needs_%s_approval" % stage
    return manifest


def mark_video_generation_finished(manifest, outputs=()):
    """Internal specialized finish used only after the video closure is built."""
    stage = "video"
    outputs = list(outputs)
    records = [_file_record(path) for path in outputs]
    if not records or any(not record or not record.get("exists") for record in records):
        raise ValueError("GENERATION_OUTPUT_MISSING: video")
    manifest.setdefault("generation", {})[stage] = {
        "status": "pending_approval",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "outputs": [os.path.abspath(path) for path in outputs],
        "artifacts": records,
    }
    for path in outputs:
        add_output(manifest, path, kind=stage)
    manifest["status"] = "needs_video_approval"
    return manifest


def record_test_segment_finished(manifest, results_path, results):
    """Bind one successful preview take as the test-segment approval artifact."""
    if not manifest.get("requires_test_segment"):
        raise ValueError("TEST_SEGMENT_NOT_REQUIRED")
    successful = [item for item in (results or []) if item and item.get("ok")]
    if len(results or []) != 1 or len(successful) != 1:
        raise ValueError("TEST_SEGMENT_REQUIRES_ONE_SUCCESSFUL_TAKE")
    result = successful[0]
    segment_id = str(result.get("segment_id") or "").strip()
    local_path = result.get("localPath") or result.get("absPath")
    take_fingerprint = str(result.get("take_fingerprint") or "").strip()
    expected_handoff = (((manifest.get("handoffs") or {}).get("video") or {})
                        .get("segments") or {}).get(segment_id)
    if (not segment_id or not expected_handoff or
            result.get("video_handoff_fingerprint") != expected_handoff or
            not take_fingerprint or not local_path):
        raise ValueError("TEST_SEGMENT_HANDOFF_OR_TAKE_IDENTITY_MISMATCH")
    media = result.get("media_qc") or {}
    if not media.get("passed"):
        raise ValueError("TEST_SEGMENT_MEDIA_QC_REQUIRED")
    model_used = str(result.get("model") or "").strip()
    if manifest.get("requires_audio"):
        audio_streams = int(
            (((media.get("media") or {}).get("audio") or {}).get(
                "stream_count") or 0))
        audio_check = (media.get("checks") or {}).get("audio_track") or {}
        silence_check = (media.get("checks") or {}).get("silence") or {}
        if (not model_used or audio_streams < 1 or
                audio_check.get("passed") is not True or
                silence_check.get("passed") is not True):
            raise ValueError("TEST_SEGMENT_EMBEDDED_AUDIO_PROOF_REQUIRED")
    audio_policy = result.get("audio_capability_policy")
    if (audio_policy and
            audio_policy != "empirical_canary_unknown_output_audio"):
        raise ValueError("TEST_SEGMENT_AUDIO_CAPABILITY_POLICY_UNSUPPORTED")
    ocr_texts = result.get("ocr_texts") or []
    frames_checked = int(result.get("ocr_frames_checked") or 0)
    expected = int(result.get("ocr_expected") or 0)
    ocr_available = bool(result.get("ocr_available"))
    if result.get("ocr_warning") and ocr_texts:
        ocr_status = "detected"
    elif result.get("ocr_warning"):
        # A warning without the detected text cannot receive a precise waiver.
        ocr_status = "error"
        ocr_available = False
    elif ocr_available and expected > 0 and frames_checked == expected:
        ocr_status = "clear"
    else:
        ocr_status = "unavailable"
        ocr_available = False
    record_ocr_result(
        manifest, segment_id, take_fingerprint, ocr_status, ocr_texts,
        available=ocr_available, frames_checked=frames_checked,
        expected=expected, error=result.get("ocr_error"), source="automated")
    outputs = [results_path, local_path]
    report = result.get("media_qc_report") or media.get("report_path")
    if report:
        outputs.append(report)
    mark_generation_finished(manifest, "test_segment", outputs)
    manifest["test_segment_artifact"] = {
        "status": "pending_approval",
        "segment_id": segment_id,
        "take_fingerprint": take_fingerprint,
        "model_used": model_used or None,
        "audio_capability_policy": audio_policy,
        "audio_capability_policy_contract": copy.deepcopy(
            result.get("audio_capability_policy_contract") or {}),
        "video_handoff_fingerprint": expected_handoff,
        "results": _file_record(results_path),
        "video": _file_record(local_path),
        "media_qc": media,
        "ocr_check": copy.deepcopy((manifest.get("ocr_checks") or {}).get(segment_id)),
    }
    return manifest["test_segment_artifact"]


def settle_test_segment_failure(manifest, results_path, results):
    """Close one failed canary with exact handoff and provider evidence."""
    if not manifest.get("requires_test_segment"):
        raise ValueError("TEST_SEGMENT_NOT_REQUIRED")
    if (not isinstance(results, list) or len(results) != 1 or
            not isinstance(results[0], dict) or results[0].get("ok") is True):
        raise ValueError("TEST_SEGMENT_FAILURE_RESULT_REQUIRED")
    result_record = _file_record(results_path)
    if not result_record or not result_record.get("exists"):
        raise ValueError("TEST_SEGMENT_RESULTS_FILE_REQUIRED")
    try:
        with open(result_record["path"], encoding="utf-8") as handle:
            persisted_results = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("TEST_SEGMENT_RESULTS_FILE_INVALID") from exc
    if persisted_results != results:
        raise ValueError("TEST_SEGMENT_RESULTS_CONTENT_MISMATCH")
    result = results[0]
    segment_id = str(result.get("segment_id") or "").strip()
    expected_handoff = (((manifest.get("handoffs") or {}).get("video") or {})
                        .get("segments") or {}).get(segment_id)
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    if requirement:
        if (not recovery_test_segment_requirement_is_current(manifest) or
                segment_id != str(requirement.get("segment_id") or "")):
            raise ValueError("TEST_SEGMENT_RECOVERY_CANARY_STALE")
    if (not segment_id or not expected_handoff or
            result.get("video_handoff_fingerprint") != expected_handoff):
        raise ValueError("TEST_SEGMENT_HANDOFF_IDENTITY_MISMATCH")
    policy = result.get("privacy_fallback_policy")
    if (policy == "stop_without_paid_fallback" and
            (result.get("privacy_fallback_attempted") is not False or
             not result.get("provider_error") or
             result.get("resume_available") is not False)):
        raise ValueError("TEST_SEGMENT_PRIVACY_STOP_EVIDENCE_INVALID")
    error = str(result.get("error") or
                "TEST_SEGMENT_GENERATION_FAILED").strip()
    error_code = ("TEST_SEGMENT_PRIVACY_STOPPED" if
                  policy == "stop_without_paid_fallback" else
                  error.split(":", 1)[0])
    generation = manifest.setdefault("generation", {}).setdefault(
        "test_segment", {})
    settlement_fingerprint = _digest(json.dumps({
        "results": result_record,
        "segment_id": segment_id,
        "handoff": expected_handoff,
        "task_id": result.get("taskId"),
        "error_code": error_code,
        "provider_error": result.get("provider_error"),
        "privacy_fallback_policy": policy,
        "privacy_fallback_attempted": result.get(
            "privacy_fallback_attempted"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    existing = generation.get("settlement") or {}
    if existing.get("fingerprint") == settlement_fingerprint:
        return existing
    if generation.get("status") != "running":
        raise ValueError("TEST_SEGMENT_SETTLEMENT_REQUIRES_RUNNING")
    settled_at = datetime.now().isoformat(timespec="seconds")
    settlement = {
        "fingerprint": settlement_fingerprint,
        "settled_at": settled_at,
        "results": result_record,
        "segment_id": segment_id,
        "task_id": result.get("taskId"),
        "video_handoff_fingerprint": expected_handoff,
        "error": error[:1000], "error_code": error_code,
        "retryable": policy != "stop_without_paid_fallback",
        "provider_error": copy.deepcopy(result.get("provider_error")),
        "privacy_fallback_policy": policy,
        "privacy_fallback_attempted": result.get(
            "privacy_fallback_attempted"),
        "resume_available": bool(result.get("resume_available")),
        "regeneration_required": bool(result.get("regeneration_required")),
    }
    history = list(generation.get("settlement_history") or [])
    history.append(copy.deepcopy(settlement))
    generation.update({
        "status": "failed_pending_recovery",
        "settled_at": settled_at,
        "settlement": settlement,
        "settlement_history": history,
        "results": result_record,
        "segment_id": segment_id,
        "task_id": result.get("taskId"),
        "error": error[:1000], "error_code": error_code,
        "failure": {
            "failed_at": settled_at, "error_code": error_code,
            "message": error[:1000],
            "retryable": policy != "stop_without_paid_fallback",
            "diagnostics": [result_record],
        },
        "recovery": {
            "required_at": settled_at,
            "action": (
                "保留当前 provider/task/策略证据，重新选择可执行路线并由客户"
                "授权新的单段 attempt；测试通过前不得进入完整批次。"),
        },
    })
    generation.pop("lease", None)
    old_artifact = manifest.pop("test_segment_artifact", None)
    if old_artifact:
        archived = copy.deepcopy(old_artifact)
        archived["invalidated_at"] = settled_at
        archived["reason"] = "test_segment_generation_failed"
        manifest.setdefault("test_segment_artifact_history", []).append(
            archived)
    invalidate_approvals(manifest, ["test_segment"])
    add_output(manifest, results_path, kind="test_segment_failure")
    manifest["status"] = "needs_test_segment_recovery"
    return settlement


def record_test_segment_failure_settlement(
        manifest_path, results_path, results, *, retries=3):
    """CAS-safe failed-canary settlement for workers and recovery commands."""
    for _ in range(max(1, int(retries))):
        manifest = load_manifest(manifest_path)
        expected_revision = int(manifest.get("revision") or 0)
        settlement = settle_test_segment_failure(
            manifest, results_path, results)
        try:
            save_manifest(
                manifest, manifest_path,
                expected_revision=expected_revision)
            return manifest, settlement
        except ManifestConflictError:
            continue
    raise ManifestConflictError("TEST_SEGMENT_SETTLEMENT_CONFLICT")


def _formal_batch_promotion_is_current(manifest, artifact, expected_handoff):
    """Verify the exact canary→batch transition without rewriting test proof."""
    promotion = artifact.get("formal_batch_promotion") or {}
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    source = promotion.get("source") or {}
    target = promotion.get("target") or {}
    proof = promotion.get("audio_canary_proof") or {}
    segment_id = str(artifact.get("segment_id") or "")
    if (promotion.get("kind") != "audio_canary_formal_batch_promotion" or
            int(promotion.get("schema_version") or 0) != 1 or
            promotion.get("status") != "fulfilled" or
            source.get("video_handoff_fingerprint") !=
            artifact.get("video_handoff_fingerprint") or
            target.get("video_handoff_fingerprint") != expected_handoff or
            target.get("aggregate_handoff_sha256") != handoff.get("sha256") or
            target.get("segments") != (handoff.get("segments") or {}) or
            target.get("segments_file") != handoff.get("file") or
            not file_record_is_current(handoff.get("file") or {}) or
            promotion.get("segment_id") != segment_id or
            promotion.get("model_used") != artifact.get("model_used") or
            promotion.get("take_fingerprint") !=
            artifact.get("take_fingerprint") or
            proof.get("fingerprint") != promotion.get(
                "audio_canary_proof_fingerprint")):
        return False
    canonical_proof = {key: value for key, value in proof.items()
                       if key != "fingerprint"}
    if (proof.get("kind") != "approved_empirical_audio_canary" or
            int(proof.get("schema_version") or 0) != 1 or
            proof.get("status") != "fulfilled" or
            proof.get("segment_id") != segment_id or
            proof.get("model_used") != artifact.get("model_used") or
            proof.get("take_fingerprint") != artifact.get(
                "take_fingerprint") or
            proof.get("results") != artifact.get("results") or
            proof.get("video") != artifact.get("video") or
            proof.get("review") != artifact.get("review") or
            proof.get("fingerprint") != _digest(json.dumps(
                canonical_proof, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")))):
        return False
    if any(not file_record_is_current(proof.get(name) or {})
           for name in ("results", "video", "review")):
        return False
    try:
        with open(handoff["file"]["path"], encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return False
    segments = spec.get("segments") if isinstance(spec, dict) else None
    if not isinstance(segments, list) or not segments:
        return False
    recovery_strategy = ((spec.get("video_recovery_contract") or {}).get(
        "strategy") or {})
    if (recovery_strategy.get("require_canary_before_batch") is not False or
            recovery_strategy.get("canary_status") != "fulfilled" or
            recovery_strategy.get("audio_canary_proof_fingerprint") !=
            proof.get("fingerprint")):
        return False
    by_id = {str(item.get("id") or ""): item for item in segments
             if isinstance(item, dict)}
    if set(by_id) != set((handoff.get("segments") or {}).keys()):
        return False
    for sid, fingerprint in (handoff.get("segments") or {}).items():
        segment = by_id.get(str(sid)) or {}
        audio = segment.get("audio_contract") or {}
        dialogue = audio.get("dialogue") or segment.get("dialogue")
        audio_required = bool(
            str(dialogue or "").strip() or audio.get("track") == "required" or
            any(audio.get(key) not in (None, "", False)
                for key in ("voice", "bgm", "sfx", "lip_sync")))
        if (segment.get("video_handoff_fingerprint") != fingerprint or
                not verify_video_handoff(segment).get("ok") or
                bool(segment.get("audio_canary_proof")) != audio_required):
            return False
        if segment.get("audio_canary_proof"):
            policy = segment.get("privacy_fallback_policy_contract") or {}
            if (segment.get("audio_canary_proof") != proof or
                    segment.get("model") != proof.get("model_used") or
                    segment.get("audio_capability_policy") or
                    segment.get("audio_capability_policy_contract") or
                    segment.get("privacy_fallback_policy") !=
                    "stop_without_paid_fallback" or
                    policy.get("kind") !=
                    "promoted_canary_privacy_stop_policy" or
                    policy.get("scope") !=
                    "formal_batch_after_approved_canary" or
                    policy.get("audio_canary_proof_fingerprint") !=
                    proof.get("fingerprint") or
                    str(policy.get("segment_id") or "") != str(sid)):
                return False
    promoted_task = promotion.get("promoted_task") or {}
    matching_tasks = [
        task for task in (manifest.get("tasks") or [])
        if task.get("stage") == "video" and
        str(task.get("unit_id") or "") == segment_id and
        task.get("handoff_fingerprint") == expected_handoff and
        task.get("task_id") == promoted_task.get("task_id") and
        task.get("model") == artifact.get("model_used") and
        int(task.get("attempt") or 1) == int(
            promoted_task.get("attempt") or 1) and
        task.get("status") == "succeeded" and
        task.get("media_qc_failed") is not True and
        task.get("promotion_proof_fingerprint") == proof.get("fingerprint")]
    return len(matching_tasks) == 1


def validate_test_segment_closure(manifest):
    artifact = manifest.get("test_segment_artifact") or {}
    if artifact.get("status") not in ("pending_approval", "approved"):
        raise ValueError("TEST_SEGMENT_ARTIFACT_REQUIRED")
    for name in ("results", "video"):
        if not file_record_is_current(artifact.get(name)):
            raise ValueError("TEST_SEGMENT_ARTIFACT_STALE: %s" % name)
    segment_id = str(artifact.get("segment_id") or "")
    expected = (((manifest.get("handoffs") or {}).get("video") or {})
                .get("segments") or {}).get(segment_id)
    if not expected:
        raise ValueError("TEST_SEGMENT_HANDOFF_STALE")
    if artifact.get("video_handoff_fingerprint") != expected:
        if not _formal_batch_promotion_is_current(
                manifest, artifact, expected):
            raise ValueError("TEST_SEGMENT_HANDOFF_STALE")
    if manifest.get("requires_audio"):
        media = artifact.get("media_qc") or {}
        audio_streams = int(
            (((media.get("media") or {}).get("audio") or {}).get(
                "stream_count") or 0))
        audio_check = (media.get("checks") or {}).get("audio_track") or {}
        silence_check = (media.get("checks") or {}).get("silence") or {}
        if (not artifact.get("model_used") or audio_streams < 1 or
                audio_check.get("passed") is not True or
                silence_check.get("passed") is not True):
            raise ValueError("TEST_SEGMENT_EMBEDDED_AUDIO_PROOF_STALE")
    if (artifact.get("audio_capability_policy") and
            artifact.get("audio_capability_policy") !=
            "empirical_canary_unknown_output_audio"):
        raise ValueError("TEST_SEGMENT_AUDIO_CAPABILITY_POLICY_STALE")
    if artifact.get("audio_capability_policy"):
        policy_contract = artifact.get("audio_capability_policy_contract") or {}
        if (policy_contract.get("policy") !=
                "empirical_canary_unknown_output_audio" or
                policy_contract.get("scope") != "formal_test_segment_only" or
                str(policy_contract.get("segment_id") or "") != segment_id or
                not file_record_is_current(policy_contract.get("file") or {})):
            raise ValueError("TEST_SEGMENT_AUDIO_CAPABILITY_POLICY_STALE")
    review_record = artifact.get("review")
    if artifact.get("status") == "approved":
        if not file_record_is_current(review_record):
            raise ValueError("TEST_SEGMENT_REVIEW_STALE")
        with open(review_record["path"], encoding="utf-8") as handle:
            review = json.load(handle)
        if (not take_review.is_accepted(review, artifact.get("take_fingerprint")) or
                not ocr_take_is_clear_or_waived(
                    manifest, segment_id, artifact.get("take_fingerprint"))):
            raise ValueError("TEST_SEGMENT_REVIEW_CLOSURE_FAILED")
    return True


def validate_audio_canary_promotion_segment(manifest, segment):
    """Authorize one formal segment against the current approved canary proof."""
    validate_test_segment_closure(manifest)
    artifact = manifest.get("test_segment_artifact") or {}
    promotion = artifact.get("formal_batch_promotion") or {}
    proof = segment.get("audio_canary_proof") or {}
    sid = str(segment.get("id") or "")
    expected = (((manifest.get("handoffs") or {}).get("video") or {})
                .get("segments") or {}).get(sid)
    policy = segment.get("privacy_fallback_policy_contract") or {}
    if (promotion.get("status") != "fulfilled" or
            proof != (promotion.get("audio_canary_proof") or {}) or
            proof.get("fingerprint") != promotion.get(
                "audio_canary_proof_fingerprint") or
            segment.get("model") != artifact.get("model_used") or
            segment.get("video_handoff_fingerprint") != expected or
            not verify_video_handoff(segment).get("ok") or
            segment.get("privacy_fallback_policy") !=
            "stop_without_paid_fallback" or
            policy.get("kind") != "promoted_canary_privacy_stop_policy" or
            policy.get("scope") != "formal_batch_after_approved_canary" or
            str(policy.get("segment_id") or "") != sid or
            policy.get("audio_canary_proof_fingerprint") !=
            proof.get("fingerprint")):
        raise ValueError("AUDIO_CANARY_FORMAL_BATCH_PROMOTION_STALE")
    return True


def validate_video_audio_canary(manifest, preflight):
    """Bind multi-segment paid video to the approved canary provider route."""
    if not (manifest.get("requires_audio") and
            manifest.get("requires_test_segment")):
        return True
    if not approval_is_current(manifest, "test_segment"):
        raise ValueError("VIDEO_AUDIO_CANARY_APPROVAL_REQUIRED")
    validate_test_segment_closure(manifest)
    artifact = manifest.get("test_segment_artifact") or {}
    proven_model = str(artifact.get("model_used") or "")
    rows = [row for row in (preflight.get("segments") or [])
            if isinstance(row, dict)]
    required_rows = [row for row in rows if row.get("audio_required") is True]
    proven_rows = [row for row in required_rows
                   if row.get("integrated_audio") is True]
    selected_models = {str(row.get("model") or "") for row in proven_rows}
    if (not proven_model or not required_rows or
            len(proven_rows) != len(required_rows) or
            selected_models != {proven_model}):
        raise ValueError(
            "VIDEO_AUDIO_CANARY_MODEL_MISMATCH: approved=%s selected=%s" %
            (proven_model or "<missing>", ",".join(sorted(selected_models))))
    return True


def approved_video_audio_canary_models(manifest):
    """Return exact provider route identities proven by the approved canary."""
    if not (manifest.get("requires_audio") and
            manifest.get("requires_test_segment")):
        return ()
    if not approval_is_current(manifest, "test_segment"):
        raise ValueError("VIDEO_AUDIO_CANARY_APPROVAL_REQUIRED")
    validate_test_segment_closure(manifest)
    model = str((manifest.get("test_segment_artifact") or {}).get(
        "model_used") or "").strip()
    if not model:
        raise ValueError("VIDEO_AUDIO_CANARY_MODEL_REQUIRED")
    return (model,)


def approve_test_segment(manifest, review_path, customer_confirmation):
    confirmation = require_explicit_customer_confirmation(
        customer_confirmation, "test_segment")
    artifact = manifest.get("test_segment_artifact") or {}
    validate_test_segment_closure(manifest)
    with open(review_path, encoding="utf-8") as handle:
        review = json.load(handle)
    if (str(review.get("segment_id") or "") != str(artifact.get("segment_id") or "") or
            not take_review.is_accepted(review, artifact.get("take_fingerprint"))):
        raise ValueError("TEST_SEGMENT_ACCEPTED_REVIEW_REQUIRED")
    if not ocr_take_is_clear_or_waived(
            manifest, artifact["segment_id"], artifact["take_fingerprint"]):
        raise ValueError("TEST_SEGMENT_OCR_CLEAR_OR_WAIVER_REQUIRED")
    artifact["review"] = _file_record(review_path)
    artifact["status"] = "approved"
    artifact["customer_confirmation"] = confirmation
    artifact["confirmed_by_customer"] = True
    add_stage_artifacts(manifest, "test_segment", [review_path])
    record_customer_approval_evidence(manifest, "test_segment", confirmation)
    approve(manifest, "test_segment", strict=True)
    validate_test_segment_closure(manifest)
    return artifact


def bootstrap_pending_approval(manifest, stage, outputs):
    """Register pre-existing brief/script files as generated approval inputs.

    This is intended for files authored before a run manifest was created. It
    deliberately has the same existence checks as a normal generation finish.
    """
    if stage not in ("brief", "script"):
        raise ValueError("APPROVAL_BOOTSTRAP_BLOCKED: 仅 brief/script 可登记既有产物")
    return mark_generation_finished(
        manifest, stage, outputs, allow_bootstrap=True)


def load_manifest(path):
    """Load a manifest snapshot, treating pre-revision files as revision zero."""
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest.setdefault("revision", 0)
    _validate_generation_history(manifest)
    return ensure_workflow_contract(manifest)


def save_manifest(manifest, path, *, expected_revision=None, lock_timeout=10.0,
                  stale_after=300.0, create_only=False):
    """Atomically save with revision CAS so stale snapshots cannot overwrite."""
    path = os.path.abspath(path)
    expected = int(manifest.get("revision", 0) if expected_revision is None
                   else expected_revision)
    candidate = copy.deepcopy(manifest)
    _validate_generation_history(candidate)
    candidate["updated_at"] = datetime.now().isoformat(timespec="seconds")
    identity = candidate.setdefault("identity", {})
    identity_gate(candidate)
    expected_assets = os.path.abspath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", candidate["client"]))
    if identity.get("asset_dir") and os.path.abspath(identity["asset_dir"]) != expected_assets:
        raise ValueError("RUN_IDENTITY_MISMATCH: manifest asset_dir 与 client 不一致")
    identity["asset_dir"] = expected_assets
    current_session = _session_digest()
    history = list(identity.get("session_id_sha256_history", []))
    for digest in (identity.get("session_id_sha256"), current_session):
        if digest and digest not in history:
            history.append(digest)
    identity["session_id_sha256_history"] = history
    current_runtime = detect_agent_runtime()
    runtime_history = list(identity.get("agent_runtime_history", []))
    for runtime in (identity.get("agent_runtime"), current_runtime):
        if runtime and runtime not in runtime_history:
            runtime_history.append(runtime)
    identity["agent_runtime"] = current_runtime
    identity["agent_runtime_history"] = runtime_history
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with FileLock(path + ".lock", timeout=lock_timeout, stale_after=stale_after):
        if create_only and os.path.exists(path):
            raise ManifestConflictError("RUN_EXISTS: manifest 已存在")
        disk_revision = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                current = json.load(handle)
            disk_revision = int(current.get("revision", 0))
        if ((disk_revision is None and expected != 0) or
                (disk_revision is not None and disk_revision != expected)):
            raise ManifestConflictError(
                "MANIFEST_REVISION_CONFLICT: expected=%s actual=%s" %
                (expected, disk_revision))
        candidate["revision"] = expected + 1
        fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(candidate, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    manifest.clear()
    manifest.update(candidate)
    return path


def _write_customer_evidence_marker(path, payload):
    """Atomically persist a customer decision marker without touching secrets."""
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".customer-approval-", suffix=".json",
                               dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def bind_customer_approval_evidence(manifest, stage, evidence):
    """Bind already-written customer evidence through the manifest state owner."""
    if stage not in CUSTOMER_EVIDENCE_STAGES:
        raise ValueError("CUSTOMER_APPROVAL_EVIDENCE_STAGE_INVALID: %s" % stage)
    if not isinstance(evidence, dict):
        raise ValueError("CUSTOMER_APPROVAL_EVIDENCE_INVALID: %s" % stage)
    approval_path = evidence.get("approval_path")
    if (evidence.get("confirmed_by_customer") is not True or
            not str(evidence.get("customer_confirmation") or "").strip() or
            evidence.get("confirmation_source") != "customer_dialogue" or
            not approval_path or not os.path.isfile(approval_path)):
        raise ValueError("CUSTOMER_APPROVAL_EVIDENCE_INCOMPLETE: %s" % stage)
    stored = copy.deepcopy(evidence)
    stored["approval_path"] = os.path.abspath(approval_path)
    manifest.setdefault("approval_evidence", {})[stage] = stored
    return stored


_VAGUE_CONFIRMATIONS = frozenset({
    "继续", "下一步", "好的", "好", "可以", "行", "ok", "okay", "go", "next",
})


def require_explicit_customer_confirmation(value, stage="approval"):
    """Reject acknowledgements that do not explicitly approve locked work."""
    confirmation = str(value or "").strip()
    normalized = re.sub(r"[\s，。！!、,.]+", "", confirmation).lower()
    if not confirmation or normalized in _VAGUE_CONFIRMATIONS:
        raise ValueError(
            "CUSTOMER_CONFIRMATION_NOT_EXPLICIT: %s 需要客户明确说“确认/同意/批准/接受”，"
            "“继续/好的/OK”仅表示已读，不能作为审批证据" % stage)
    explicit_terms = (
        "确认", "同意", "批准", "接受", "认可", "审核通过", "可以定稿",
        "confirm", "approve", "approved", "accept", "accepted",
    )
    natural_go_ahead = bool(
        "可以了" in normalized and "就这样" in normalized and
        any(term in normalized for term in ("进行", "进入", "开始", "生成", "出片")))
    no_problem_go_ahead = bool(
        normalized.startswith(("没问题", "没有问题")) and
        any(term in normalized for term in (
            "继续", "后续", "下一步", "进行", "进入", "开始", "生成", "出片")))
    if not (any(term in normalized for term in explicit_terms) or
            natural_go_ahead or no_problem_go_ahead):
        raise ValueError(
            "CUSTOMER_CONFIRMATION_NOT_EXPLICIT: %s 未包含明确审批语义" % stage)
    return confirmation


def record_customer_approval_evidence(manifest, stage, customer_confirmation):
    """Bind one generic customer decision to current stage bytes.

    Visual boards use their richer storyboard-owned markers.  Generic stages
    use an immutable, content-addressed marker inside the run directory so a
    failed manifest CAS cannot overwrite an earlier valid decision.
    """
    if stage not in GENERIC_CUSTOMER_EVIDENCE_STAGES:
        raise ValueError("GENERIC_CUSTOMER_APPROVAL_STAGE_INVALID: %s" % stage)
    confirmation = require_explicit_customer_confirmation(
        customer_confirmation, stage)
    identity_gate(manifest)
    missing = [dependency for dependency in generation_dependencies(manifest, stage)
               if not approval_is_current(manifest, dependency)]
    if missing:
        raise ValueError("审批顺序错误：请先完成 %s" % ", ".join(missing))
    generation = (manifest.get("generation") or {}).get(stage) or {}
    if generation.get("status") != "pending_approval":
        raise ValueError("APPROVAL_BLOCKED: %s 尚未处于 pending_approval" % stage)
    artifacts = _stage_artifacts(manifest, stage, refresh=True)
    if not artifacts or any(not item.get("exists") for item in artifacts):
        raise ValueError("APPROVAL_BLOCKED: %s 的待审批产物不存在" % stage)
    _validate_persisted_stage_output_contract(manifest, stage, artifacts)
    fingerprint = _approval_input_hash(manifest, stage, refresh=True)
    confirmed_at = datetime.now().isoformat(timespec="seconds")
    marker = {
        "status": "confirmed",
        "kind": "stage_customer_approval",
        "stage": stage,
        "client": manifest.get("client"),
        "run_id": manifest.get("run_id"),
        "content_fingerprint": fingerprint,
        "confirmed_by_customer": True,
        "customer_confirmation": confirmation,
        "confirmation_source": "customer_dialogue",
        "confirmed_at": confirmed_at,
    }
    decision_id = _digest(json.dumps(marker, ensure_ascii=False, sort_keys=True))[:12]
    marker_name = "%s.%s.%s.customer-confirmed.json" % (
        stage, fingerprint[:12], decision_id)
    marker_path = require_run_output(
        manifest,
        os.path.join(run_output_root(manifest), ".approvals", marker_name),
        label="customer_approval_marker")
    _write_customer_evidence_marker(marker_path, marker)
    marker_record = _file_record(marker_path)
    return bind_customer_approval_evidence(manifest, stage, {
        **marker,
        "approval_path": marker_path,
        "marker_sha256": marker_record.get("sha256"),
    })


def customer_approval_evidence_is_current(manifest, stage):
    """Validate customer evidence against current stage bytes and identity."""
    if stage not in CUSTOMER_EVIDENCE_STAGES:
        return True
    evidence = (manifest.get("approval_evidence") or {}).get(stage) or {}
    approval_path = evidence.get("approval_path")
    if (evidence.get("confirmed_by_customer") is not True or
            not str(evidence.get("customer_confirmation") or "").strip() or
            evidence.get("confirmation_source") != "customer_dialogue" or
            not approval_path or not os.path.isfile(approval_path)):
        return False
    try:
        with open(approval_path, encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if stage in GENERIC_CUSTOMER_EVIDENCE_STAGES:
        try:
            contained = require_run_output(
                manifest, approval_path, label="customer_approval_marker",
                must_exist=True)
        except ValueError:
            return False
        current_fingerprint = _approval_input_hash(manifest, stage, refresh=True)
        marker_record = _file_record(contained) or {}
        common = {
            "status": "confirmed",
            "kind": "stage_customer_approval",
            "stage": stage,
            "client": manifest.get("client"),
            "run_id": manifest.get("run_id"),
            "content_fingerprint": current_fingerprint,
            "confirmed_by_customer": True,
            "confirmation_source": "customer_dialogue",
        }
        if any(marker.get(key) != value for key, value in common.items()):
            return False
        if any(evidence.get(key) != value for key, value in common.items()):
            return False
        if (str(marker.get("customer_confirmation") or "").strip() !=
                str(evidence.get("customer_confirmation") or "").strip() or
                marker.get("confirmed_at") != evidence.get("confirmed_at") or
                marker_record.get("sha256") != evidence.get("marker_sha256")):
            return False
        return True
    expected_kinds = {
        "product_board": {"product"},
        "cast_board": {"cast"},
        "product_usage": {"usage"},
        "storyboard": {"storyboard", "design_board"},
    }
    if (marker.get("status") != "confirmed" or
            marker.get("kind") not in expected_kinds[stage] or
            marker.get("confirmed_by_customer") is not True or
            marker.get("confirmation_source") != "customer_dialogue" or
            str(marker.get("customer_confirmation") or "").strip() !=
            str(evidence.get("customer_confirmation") or "").strip()):
        return False
    marker_identity = (marker.get("approval_artifact_sha256") or
                       marker.get("board_sha256") or
                       marker.get("storyboard_sha256") or
                       marker.get("plan_fingerprint"))
    if not marker_identity or marker_identity != evidence.get("artifact_sha256"):
        return False
    if stage == "storyboard":
        if (len(str(marker_identity)) != 64 or
                marker.get("artifact_short_id") != str(marker_identity)[:12]):
            return False
        marker_payload = {
            key: value for key, value in marker.items()
            if key not in {"approval_artifact_sha256", "artifact_short_id"}
        }
        if _digest(json.dumps(
                marker_payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))) != marker_identity:
            return False
    # A visual approval marker is only meaningful while the exact customer-
    # viewed media still exists.  Previously we checked that the marker file
    # was tracked but did not re-hash marker["path"], so a replaced board could
    # survive the evidence check until a separate aggregate hash happened to
    # catch it.
    if stage in STABLE_VISUAL_BOARD_STAGES:
        board_path = marker.get("path")
        try:
            board_record = _file_record(board_path)
        except (OSError, TypeError, ValueError):
            return False
        if (not board_record or not board_record.get("exists") or
                board_record.get("sha256") != marker.get("board_sha256")):
            return False
    artifact_paths = {
        os.path.abspath(record.get("path"))
        for record in _stage_artifacts(manifest, stage, refresh=True)
        if isinstance(record, dict) and record.get("path") and record.get("exists")
    }
    return os.path.abspath(approval_path) in artifact_paths


def repair_visual_board_approval(manifest, stage):
    """Migrate a legacy board approval away from mutable shared checkpoints.

    Older storyboard transactions tracked storyboard_result/index/preview/audit
    files as product/cast/usage approval inputs.  Those files are intentionally
    rewritten by the next stage, so their hashes made a valid customer decision
    look stale.  This explicit repair keeps only the immutable board bytes,
    optional usage-panel evidence, and the customer marker.  It never creates
    approval: the existing marker, board hash, identity, and upstream approvals
    must all validate first.
    """
    if stage not in STABLE_VISUAL_BOARD_STAGES:
        raise ValueError("VISUAL_BOARD_REPAIR_STAGE_INVALID: %s" % stage)
    if not manifest.get("approvals", {}).get(stage):
        raise ValueError("VISUAL_BOARD_REPAIR_APPROVAL_MISSING: %s" % stage)
    evidence = (manifest.get("approval_evidence") or {}).get(stage) or {}
    approval_path = evidence.get("approval_path")
    if not approval_path or not os.path.isfile(approval_path):
        raise ValueError("VISUAL_BOARD_REPAIR_MARKER_MISSING: %s" % stage)
    try:
        with open(approval_path, encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("VISUAL_BOARD_REPAIR_MARKER_INVALID: %s" % stage) from exc
    expected_kind = {
        "product_board": "product",
        "cast_board": "cast",
        "product_usage": "usage",
    }[stage]
    board_path = marker.get("path")
    board_record = _file_record(board_path)
    if (marker.get("status") != "confirmed" or
            marker.get("kind") != expected_kind or
            marker.get("confirmed_by_customer") is not True or
            marker.get("confirmation_source") != "customer_dialogue" or
            not str(marker.get("customer_confirmation") or "").strip() or
            not board_record or not board_record.get("exists") or
            board_record.get("sha256") != marker.get("board_sha256") or
            evidence.get("artifact_sha256") != marker.get("board_sha256")):
        raise ValueError("VISUAL_BOARD_REPAIR_EVIDENCE_STALE: %s" % stage)
    missing_dependencies = [
        dependency for dependency in generation_dependencies(manifest, stage)
        if not approval_is_current(manifest, dependency)]
    if missing_dependencies:
        raise ValueError("VISUAL_BOARD_REPAIR_DEPENDENCY_STALE: %s" %
                         ", ".join(missing_dependencies))

    paths = [board_record["path"]]
    if stage == "product_usage":
        paths.extend(marker.get("panel_paths") or [])
        if marker.get("geometry_review_path"):
            paths.append(marker["geometry_review_path"])
    paths.append(os.path.abspath(approval_path))
    records = []
    for path in paths:
        record = _file_record(path)
        if not record or not record.get("exists"):
            raise ValueError("VISUAL_BOARD_REPAIR_ARTIFACT_MISSING: %s" % path)
        if record not in records:
            records.append(record)
    generation = manifest.setdefault("generation", {}).setdefault(stage, {})
    previous_paths = [item.get("path") for item in generation.get("artifacts") or []
                      if isinstance(item, dict)]
    generation["artifacts"] = records
    generation["outputs"] = [item["path"] for item in records]
    generation["status"] = "approved"
    manifest.setdefault("approval_hashes", {})[stage] = _approval_input_hash(
        manifest, stage)
    invalidate_from(manifest, stage, include_stage=False)
    manifest.setdefault("migrations", []).append({
        "kind": "stable_visual_board_approval_v1",
        "stage": stage,
        "migrated_at": datetime.now().isoformat(timespec="seconds"),
        "removed_mutable_paths": [path for path in previous_paths
                                  if path not in generation["outputs"]],
    })
    manifest["status"] = stage
    return {"stage": stage, "approval_current": approval_is_current(manifest, stage),
            "artifacts": records}


def approve(manifest, stage, approved=True, strict=False):
    if stage not in STAGES:
        raise ValueError("未知审批阶段: %s" % stage)
    if approved and stage in CUSTOMER_EVIDENCE_STAGES and not strict:
        raise ValueError(
            "CUSTOMER_APPROVAL_STRICT_REQUIRED: %s 客户决策阶段禁止写入 status-only 审批" %
            stage)
    if approved and stage == "video":
        validate_video_closure(manifest)
    if approved and stage == "test_segment":
        validate_test_segment_closure(manifest)
    if approved and stage == "shotcraft_packaging":
        validate_shotcraft_closure(manifest)
    if approved and stage == "final":
        validate_final_closure(manifest)
    if approved and stage == "derive":
        validate_derive_closure(manifest)
    if approved and strict:
        missing = [dependency for dependency in generation_dependencies(manifest, stage)
                   if not approval_is_current(manifest, dependency)]
        if missing:
            raise ValueError("审批顺序错误：请先完成 %s" % ", ".join(missing))
        generation = manifest.get("generation", {}).get(stage) or {}
        if generation.get("status") != "pending_approval":
            raise ValueError("APPROVAL_BLOCKED: %s 尚未处于 pending_approval" % stage)
        artifacts = _stage_artifacts(manifest, stage, refresh=True)
        if not artifacts or any(not item.get("exists") for item in artifacts):
            raise ValueError("APPROVAL_BLOCKED: %s 的待审批产物不存在" % stage)
        _validate_persisted_stage_output_contract(manifest, stage, artifacts)
        if (stage in CUSTOMER_EVIDENCE_STAGES and
                not customer_approval_evidence_is_current(manifest, stage)):
            raise ValueError(
                "CUSTOMER_CONFIRMATION_EVIDENCE_REQUIRED: %s 必须绑定客户确认原话、"
                "当前 marker 与产物哈希" % stage)
        # Capture refreshed records so the approval and subsequent checks use
        # exactly the same bytes. Any later disk edit invalidates this hash.
        generation["artifacts"] = artifacts
        manifest.setdefault("approval_hashes", {})[stage] = _approval_input_hash(manifest, stage)
    if approved:
        invalidate_from(manifest, stage, include_stage=False)
        manifest.setdefault("approvals", {})[stage] = True
        if strict:
            manifest.setdefault("generation", {}).setdefault(stage, {})[
                "status"] = "approved"
            manifest["generation"][stage]["approved_at"] = datetime.now().isoformat(
                timespec="seconds")
    else:
        invalidate_from(manifest, stage)
    manifest["status"] = stage if approved else "needs_%s_approval" % stage
    return manifest


def _persisted_report_matches(path, report):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle) == report
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _closure_status_is_valid(manifest, stage, generation):
    """Accept one closed transaction both before and after strict approval."""
    status = generation.get("status")
    if status == "pending_approval":
        return True
    return status == "approved" and approval_is_current(manifest, stage)


def _record_matches_identity(record, identity):
    """Compare the stable path/SHA identity shared by manifest and QC records."""
    return bool(
        isinstance(record, dict) and isinstance(identity, dict) and
        record.get("exists") and record.get("path") == identity.get("path") and
        record.get("sha256") and record.get("sha256") == identity.get("sha256"))


def _current_caption_timeline(manifest):
    """Load and revalidate the approved caption artifact without circular imports."""
    recorded = manifest.get("caption_artifact") or {}
    path = recorded.get("path")
    try:
        with open(path, encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    files = artifact.get("files") or {}
    names = ("segments", "basecut", "lines", "srt", "motion")
    payload = {
        "client": artifact.get("client"),
        "run_id": artifact.get("run_id"),
        "video_handoff_sha256": artifact.get("video_handoff_sha256"),
        "files": {name: (files.get(name) or {}).get("sha256") for name in names},
    }
    identity = _digest(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if (artifact.get("client") != manifest.get("client") or
            str(artifact.get("run_id")) != str(manifest.get("run_id")) or
            artifact.get("status") != "approved" or
            artifact.get("confirmed_by_customer") is not True or
            not str(artifact.get("customer_confirmation") or "").strip() or
            artifact.get("confirmation_source") != "customer_dialogue" or
            artifact.get("caption_identity") != identity or
            recorded.get("caption_identity") != identity or
            recorded.get("status") != "approved" or
            recorded.get("customer_confirmation") !=
            artifact.get("customer_confirmation") or
            any(not file_record_is_current(files.get(name)) for name in names)):
        return None
    return artifact


def validate_final_closure(manifest):
    generation = (manifest.get("generation") or {}).get("final") or {}
    artifacts = _stage_artifacts(manifest, "final", refresh=True)
    artifact_pairs = {(item.get("path"), item.get("sha256")) for item in artifacts}
    qc = generation.get("media_qc") or {}
    qc_file = qc.get("file") or {}
    qc_path = generation.get("planned_qc")
    inputs = manifest.get("final_inputs") or {}
    captioned = inputs.get("captioned_basecut") or {}
    scheme = inputs.get("scheme") or {}
    caption_render = manifest.get("caption_render") or {}
    caption_output = caption_render.get("output") or {}
    caption_artifact = _current_caption_timeline(manifest) or {}
    output_record = file_record(generation.get("planned_output"))
    qc_record = file_record(qc_path)
    expected_records = [output_record, qc_record]
    expected_pairs = {
        (item.get("path"), item.get("sha256")) for item in expected_records if item
    }
    caption_source = ((caption_artifact.get("files") or {}).get("basecut") or {})
    if (not _closure_status_is_valid(manifest, "final", generation) or
            not approval_is_current(manifest, "captions") or
            generation.get("render_quality") != "high" or
            generation.get("planned_output") != qc_file.get("path") or
            generation.get("planned_qc") != os.path.abspath(
                str(generation.get("planned_output") or "")) + ".qc.json" or
            generation.get("scheme") != scheme or
            generation.get("captioned_basecut") != captioned or
            generation.get("caption_identity") != inputs.get("caption_identity") or
            not artifacts or any(not item.get("exists") for item in artifacts) or
            any(not item or not item.get("exists") for item in expected_records) or
            len(artifacts) != 2 or artifact_pairs != expected_pairs or
            not _record_matches_identity(output_record, qc_file) or
            qc.get("profile") != "formal" or not qc.get("passed") or
            not (qc.get("expectations") or {}).get("audio_required") or
            not _persisted_report_matches(qc_path, qc) or
            manifest.get("delivery_qc") != qc or
            not file_record_is_current(scheme) or not file_record_is_current(captioned) or
            caption_render.get("status") != "pending_final_approval" or
            caption_output != captioned or not file_record_is_current(caption_output) or
            inputs.get("caption_identity") != caption_artifact.get("caption_identity") or
            inputs.get("timeline_basecut_sha256") != caption_source.get("sha256")):
        raise ValueError("FINAL_CLOSURE_REQUIRED_OR_STALE")
    return True


def validate_shotcraft_closure(manifest):
    generation = (manifest.get("generation") or {}).get("shotcraft_packaging") or {}
    artifacts = _stage_artifacts(manifest, "shotcraft_packaging", refresh=True)
    artifact_pairs = {(item.get("path"), item.get("sha256")) for item in artifacts}
    spec = generation.get("spec") or {}
    qc = generation.get("qc") or {}
    frame_qc = generation.get("shotcraft_frame_qc") or {}
    output_path = generation.get("planned_output")
    output = file_record(output_path) if output_path else None
    qc_path = generation.get("planned_qc")
    frame_qc_path = generation.get("planned_frame_qc")
    qc_record = file_record(qc_path)
    frame_qc_record = file_record(frame_qc_path)
    frames = frame_qc.get("frames") or []
    frame_records = [file_record(item.get("path")) for item in frames]
    expected_records = [output, qc_record, frame_qc_record] + frame_records
    expected_pairs = {
        (item.get("path"), item.get("sha256")) for item in expected_records if item
    }
    if (not _closure_status_is_valid(manifest, "shotcraft_packaging", generation) or
            not approval_is_current(manifest, "video") or
            generation.get("render_quality") != "high" or
            not file_record_is_current(spec) or not output or not output.get("exists") or
            generation.get("planned_qc") != output.get("path") + ".qc.json" or
            generation.get("planned_frame_qc") != output.get("path") + ".shotcraft-qc.json" or
            not frames or any(not item or not item.get("exists") for item in expected_records) or
            len(artifacts) != len(expected_records) or artifact_pairs != expected_pairs or
            qc.get("profile") != "formal" or not qc.get("passed") or
            not _record_matches_identity(output, qc.get("file") or {}) or
            not qc_path or not _persisted_report_matches(qc_path, qc) or
            not frame_qc_path or not _persisted_report_matches(frame_qc_path, frame_qc) or
            frame_qc.get("passed") is not True or
            frame_qc.get("video") != output.get("path") or
            frame_qc.get("video_sha256") != output.get("sha256")):
        raise ValueError("SHOTCRAFT_CLOSURE_REQUIRED_OR_STALE")
    for item in frames:
        current = file_record(item.get("path"))
        if (not current or current.get("sha256") != item.get("sha256") or
                (current.get("path"), current.get("sha256")) not in artifact_pairs):
            raise ValueError("SHOTCRAFT_CLOSURE_REQUIRED_OR_STALE")
    return True


def validate_derive_closure(manifest):
    generation = (manifest.get("generation") or {}).get("derive") or {}
    final_qc = manifest.get("delivery_qc") or {}
    final_file = final_qc.get("file") or {}
    source = generation.get("source") or {}
    source_current = file_record(source.get("path")) if source.get("path") else None
    if generation.get("status") == "skipped":
        reason = generation.get("reason")
        target_ratios = generation.get("target_ratios") or []
        if (not approval_is_current(manifest, "final") or
                not validate_final_closure(manifest) or
                manifest.get("approvals", {}).get("derive") or
                generation.get("outputs") or generation.get("artifacts") or
                generation.get("derived_details") or generation.get("planned_outputs") or
                reason not in ("no_target_ratios", "targets_match_source_ratio") or
                not _record_matches_identity(source_current, final_file) or
                (reason == "no_target_ratios" and target_ratios) or
                (reason == "targets_match_source_ratio" and
                 (not target_ratios or not generation.get("source_ratio") or
                  any(ratio != generation.get("source_ratio") for ratio in target_ratios)))):
            raise ValueError("DERIVE_CLOSURE_REQUIRED_OR_STALE")
        return True
    artifacts = _stage_artifacts(manifest, "derive", refresh=True)
    artifact_pairs = {(item.get("path"), item.get("sha256")) for item in artifacts}
    details = generation.get("derived_details") or []
    planned = generation.get("planned_outputs") or []
    target_ratios = generation.get("target_ratios") or []
    detail_paths = [os.path.abspath(item.get("path")) for item in details if item.get("path")]
    expected_ratios = [ratio for ratio in target_ratios
                       if ratio != generation.get("source_ratio")]
    output_records = [file_record(item.get("path")) for item in details]
    qc_records = [file_record(item.get("media_qc_path")) for item in details]
    expected_records = output_records + qc_records
    expected_pairs = {
        (item.get("path"), item.get("sha256")) for item in expected_records if item
    }
    if (not _closure_status_is_valid(manifest, "derive", generation) or
            not approval_is_current(manifest, "final") or
            not validate_final_closure(manifest) or
            source != source_current or not _record_matches_identity(source_current, final_file) or
            not target_ratios or len(target_ratios) != len(set(target_ratios)) or
            not details or len(details) != len(expected_ratios) or
            [item.get("ratio") for item in details] != expected_ratios or
            planned != detail_paths or len(detail_paths) != len(details) or
            not artifacts or any(not item.get("exists") for item in artifacts) or
            any(not item or not item.get("exists") for item in expected_records) or
            len(artifacts) != len(expected_records) or artifact_pairs != expected_pairs):
        raise ValueError("DERIVE_CLOSURE_REQUIRED_OR_STALE")
    for item in details:
        current = file_record(item.get("path"))
        qc = item.get("media_qc") or {}
        qc_path = item.get("media_qc_path")
        qc_record = file_record(qc_path)
        if (not current or current.get("sha256") != item.get("sha256") or
                (current.get("path"), current.get("sha256")) not in artifact_pairs or
                qc.get("profile") != "formal" or not qc.get("passed") or
                not _record_matches_identity(current, qc.get("file") or {}) or
                (qc.get("expectations") or {}).get("aspect_ratio") != item.get("ratio") or
                not qc_path or qc_path != current.get("path") + ".qc.json" or
                not qc_record or not qc_record.get("exists") or
                (qc_record.get("path"), qc_record.get("sha256")) not in artifact_pairs or
                not _persisted_report_matches(qc_path, qc)):
            raise ValueError("DERIVE_CLOSURE_REQUIRED_OR_STALE")
    return True


def add_output(manifest, path, kind="file"):
    item = _file_record(path) or {"path": os.path.abspath(path), "exists": False}
    item["kind"] = kind
    manifest.setdefault("outputs", []).append(item)
    return manifest


def find_resumable_task(manifest, stage, unit_id, handoff_fingerprint, model=None,
                        dependency_fingerprint=None):
    attempt = current_video_attempt(manifest, unit_id) if stage == "video" else None
    candidates = [task for task in manifest.get("tasks", [])
                  if task.get("stage") == stage and task.get("unit_id") == unit_id
                  and task.get("handoff_fingerprint") == handoff_fingerprint
                   and task_model_matches(task, model)
                   and task_dependency_matches(task, dependency_fingerprint)
                   and task.get("status") in (
                       "submitted", "running", "timed_out",
                       "fallback_approval_required")
                   and (attempt is None or int(task.get("attempt", 1)) == attempt)
                  and task.get("task_id")]
    return candidates[-1] if candidates else None


def find_submission_intent(manifest, stage, unit_id, handoff_fingerprint, model=None,
                           dependency_fingerprint=None):
    attempt = current_video_attempt(manifest, unit_id) if stage == "video" else None
    candidates = [task for task in manifest.get("tasks", [])
                  if task.get("stage") == stage and task.get("unit_id") == unit_id
                  and task.get("handoff_fingerprint") == handoff_fingerprint
                   and task_model_matches(task, model)
                   and task_dependency_matches(task, dependency_fingerprint)
                   and task.get("status") == "submitting"
                   and (attempt is None or int(task.get("attempt", 1)) == attempt)
                  and task.get("request_id")]
    return candidates[-1] if candidates else None


def current_video_attempt(manifest, segment_id):
    authorization = (manifest.get("video_attempts") or {}).get(str(segment_id)) or {}
    return int(authorization.get("attempt") or 1)


def _validate_failed_attempt_evidence(manifest, segment_id, results_path,
                                      *, expected_attempt=None,
                                      expected_handoff=None):
    """Bind an exact settled no-media provider failure to a paid retry.

    A free-form error string is never sufficient authorization.  The results
    artifact, generation settlement and durable task ledger must all identify
    the same segment, task, attempt and immutable video handoff.
    """
    record = _file_record(results_path)
    if not record or not file_record_is_current(record):
        raise ValueError("VIDEO_ATTEMPT_FAILURE_RESULTS_STALE")
    generation_stage = None
    generation = None
    _validate_generation_history(manifest)
    for stage in ("test_segment", "video"):
        candidates = [(manifest.get("generation") or {}).get(stage) or {}]
        candidates.extend(
            entry.get("record") or {}
            for entry in reversed(
                ((manifest.get("generation_history") or {}).get(stage) or []))
            if isinstance(entry, dict))
        for candidate in candidates:
            if (candidate.get("results") == record and
                    candidate.get("status") in (
                        "failed_pending_recovery",
                        "blocked_pending_recovery")):
                generation_stage, generation = stage, candidate
                break
        if generation is not None:
            break
    if generation is None:
        raise ValueError("VIDEO_ATTEMPT_FAILURE_NOT_SETTLED")
    try:
        with open(record["path"], encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("VIDEO_ATTEMPT_FAILURE_RESULTS_INVALID") from exc
    results = payload.get("results") if isinstance(payload, dict) else payload
    matches = [
        item for item in (results or [])
        if isinstance(item, dict) and
        str(item.get("segment_id") or "") == str(segment_id)]
    if len(matches) != 1:
        raise ValueError("VIDEO_ATTEMPT_FAILURE_RESULT_REQUIRED")
    result = matches[0]
    task_id = str(result.get("taskId") or result.get("task_id") or "")
    expected_handoff = (expected_handoff or
        ((((manifest.get("handoffs") or {}).get("video") or {})
          .get("segments") or {}).get(str(segment_id))))
    if (result.get("ok") is not False or
            not str(result.get("error") or "").strip() or
            result.get("localPath") or result.get("absPath") or
            result.get("videoUrl") or result.get("resume_available") is True or
            result.get("video_handoff_fingerprint") != expected_handoff or
            (generation.get("segment_id") and
             str(generation.get("segment_id")) != str(segment_id)) or
            (generation.get("task_id") and
             str(generation.get("task_id")) != task_id) or
            generation.get("video_handoff_fingerprint", expected_handoff) !=
            expected_handoff):
        raise ValueError("VIDEO_ATTEMPT_FAILURE_IDENTITY_MISMATCH")
    attempt = (int(expected_attempt) if expected_attempt is not None else
               current_video_attempt(manifest, segment_id))
    def _matches_provider_request(task):
        if task_id:
            return str(task.get("task_id") or "") == task_id
        # A submit-time provider rejection may happen before an upstream
        # taskId exists.  In that case bind the retry to exactly one durable
        # failed request ledger entry; never authorize from an error string
        # alone.
        return not task.get("task_id") and bool(task.get("request_id"))

    tasks = [
        task for task in (manifest.get("tasks") or [])
        if task.get("stage") == "video" and
        str(task.get("unit_id")) == str(segment_id) and
        _matches_provider_request(task) and
        int(task.get("attempt") or 1) == attempt and
        task.get("handoff_fingerprint") == expected_handoff and
        task.get("status") in ("failed", "fallback_approval_required")]
    if len(tasks) != 1:
        raise ValueError("VIDEO_ATTEMPT_FAILURE_TASK_MISMATCH")
    return {
        "stage": generation_stage,
        "results": record,
        "result": result,
        "task": tasks[0],
    }


def video_attempt_failure_evidence_is_current(manifest, segment_id,
                                              authorization=None):
    """Revalidate a stored no-media retry grant after its attempt increments."""
    segment_id = str(segment_id)
    authorization = (authorization or
                     (manifest.get("video_attempts") or {}).get(segment_id) or {})
    record = authorization.get("failure_results") or {}
    attempt = int(authorization.get("attempt") or 0)
    if (attempt < 2 or authorization.get("failure_stage") not in {
            "test_segment", "video"} or
            not isinstance(record, dict) or not record.get("path")):
        return False
    try:
        evidence = _validate_failed_attempt_evidence(
            manifest, segment_id, record["path"],
            expected_attempt=attempt - 1,
            expected_handoff=authorization.get(
                "failure_video_handoff_fingerprint"))
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return bool(
        evidence["stage"] == authorization.get("failure_stage") and
        evidence["results"] == record and
        str(evidence["task"].get("task_id") or "") ==
        str(authorization.get("previous_task_id") or ""))


def _validate_rejected_review_evidence(manifest, segment_id, review,
                                       review_path, expected_handoff):
    """Require the rejected decision to be a current persisted artifact."""
    if not review_path:
        raise ValueError("VIDEO_ATTEMPT_REVIEW_ARTIFACT_REQUIRED")
    review_record = _file_record(review_path)
    if not review_record or not file_record_is_current(review_record):
        raise ValueError("VIDEO_ATTEMPT_REVIEW_ARTIFACT_STALE")
    try:
        with open(review_record["path"], encoding="utf-8") as handle:
            persisted = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("VIDEO_ATTEMPT_REVIEW_ARTIFACT_INVALID") from exc
    artifact = (review or {}).get("artifact") or {}
    if (persisted != review or
            str((review or {}).get("segment_id")) != str(segment_id) or
            artifact.get("video_handoff_fingerprint") != expected_handoff or
            ((review or {}).get("decision") or {}).get("verdict") != "rejected" or
            ((review or {}).get("decision") or {}).get("acceptance_mode") != "formal" or
            not _take_review.artifact_is_current(review) or
            not _take_review.artifact_fingerprint_is_current(review)):
        raise ValueError("VIDEO_ATTEMPT_REVIEW_IDENTITY_MISMATCH")
    return review_record


def request_video_attempt(manifest, segment_id, *, actor, reason, review=None,
                          review_path=None, failure_results=None):
    """Authorize one paid retry from exact accepted/rejected/failure evidence."""
    segment_id = str(segment_id)
    if not actor or not reason:
        raise ValueError("VIDEO_ATTEMPT_ACTOR_REASON_REQUIRED")
    if review is not None and failure_results is not None:
        raise ValueError("VIDEO_ATTEMPT_EVIDENCE_AMBIGUOUS")
    accepted = (manifest.get("accepted_takes") or {}).get(segment_id)
    rejected = (review or {}).get("decision", {}).get("verdict") == "rejected"
    expected = (((manifest.get("handoffs") or {}).get("video") or {})
                .get("segments") or {}).get(segment_id)
    if not expected:
        raise ValueError("VIDEO_HANDOFF_REQUIRED")
    failure_evidence = (_validate_failed_attempt_evidence(
        manifest, segment_id, failure_results) if failure_results else None)
    if not accepted and not rejected and not failure_evidence:
        raise ValueError("VIDEO_ATTEMPT_REQUIRES_ACCEPTED_OR_REJECTED_TAKE")
    review_record = (_validate_rejected_review_evidence(
        manifest, segment_id, review, review_path, expected) if review else None)
    previous = max([int(task.get("attempt", 1)) for task in manifest.get("tasks", [])
                    if task.get("stage") == "video" and str(task.get("unit_id")) == segment_id]
                   + [current_video_attempt(manifest, segment_id)])
    item = {"attempt": previous + 1, "actor": actor, "reason": reason,
            "previous_take_fingerprint": (accepted or {}).get("take_fingerprint") or
            ((review or {}).get("artifact") or {}).get("take_fingerprint"),
            "authorized_at": datetime.now().isoformat(timespec="seconds")}
    if review_record:
        item["rejected_review"] = review_record
    if failure_evidence:
        item["previous_task_id"] = failure_evidence["task"].get("task_id")
        item["failure_stage"] = failure_evidence["stage"]
        item["failure_results"] = failure_evidence["results"]
        item["failure_error_code"] = str(
            failure_evidence["result"].get("error") or "").split(":", 1)[0]
        item["failure_video_handoff_fingerprint"] = expected
    manifest.setdefault("video_attempts", {})[segment_id] = item
    manifest.setdefault("accepted_takes", {}).pop(segment_id, None)
    manifest.setdefault("ocr_checks", {}).pop(segment_id, None)
    manifest.setdefault("ocr_waivers", {}).pop(segment_id, None)
    # Authorizing new paid bytes reopens video and every dependent approval.
    # Keeping an old green approval here would let routing skip the replacement
    # take even though validate_video_closure can no longer succeed.
    invalidate_from(manifest, "video")
    video_generation = manifest.setdefault("generation", {}).setdefault("video", {})
    video_generation["status"] = "authorized_retry"
    video_generation["authorized_retry_segment"] = segment_id
    video_generation["authorized_retry_at"] = item["authorized_at"]
    manifest["status"] = (
        "needs_test_segment_recovery"
        if failure_evidence and failure_evidence["stage"] == "test_segment"
        else "needs_video_generation")
    return item


def reconcile_authorized_test_segment_recovery(manifest, segment_id):
    """Repair top-level routing only from a still-current canary failure grant."""
    segment_id = str(segment_id)
    authorization = (manifest.get("video_attempts") or {}).get(segment_id) or {}
    generation = (manifest.get("generation") or {}).get("test_segment") or {}
    failure_record = authorization.get("failure_results") or {}
    expected_handoff = (((manifest.get("handoffs") or {}).get("video") or {})
                        .get("segments") or {}).get(segment_id)
    task_id = str(authorization.get("previous_task_id") or "")
    tasks = [
        task for task in (manifest.get("tasks") or [])
        if task.get("stage") == "video" and
        str(task.get("unit_id")) == segment_id and
        str(task.get("task_id")) == task_id and
        task.get("handoff_fingerprint") == expected_handoff and
        task.get("status") in ("failed", "fallback_approval_required")]
    if (authorization.get("failure_stage") != "test_segment" or
            generation.get("status") != "failed_pending_recovery" or
            str(generation.get("segment_id") or "") != segment_id or
            str(generation.get("task_id") or "") != task_id or
            generation.get("video_handoff_fingerprint") != expected_handoff or
            generation.get("results") != failure_record or
            not file_record_is_current(failure_record) or len(tasks) != 1 or
            int(authorization.get("attempt") or 0) !=
            int(tasks[0].get("attempt") or 1) + 1):
        raise ValueError("TEST_SEGMENT_RECOVERY_AUTHORIZATION_STALE")
    manifest["status"] = "needs_test_segment_recovery"
    return {
        "segment_id": segment_id,
        "attempt": int(authorization["attempt"]),
        "previous_task_id": task_id,
        "status": manifest["status"],
    }


def validate_video_closure(manifest):
    """Fail closed unless every formal video artifact and take review is current."""
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    artifact = manifest.get("video_artifact") or {}
    for name in ("segments", "results", "basecut", "reviews"):
        if not file_record_is_current(artifact.get(name)):
            raise ValueError("VIDEO_APPROVAL_ARTIFACT_REQUIRED_OR_STALE: %s" % name)
    assembly_qc = artifact.get("assembly_qc") or {}
    assembly_qc_file = artifact.get("assembly_qc_file") or {}
    basecut = artifact.get("basecut") or {}
    if (assembly_qc.get("profile") != "formal" or not assembly_qc.get("passed") or
            not (assembly_qc.get("expectations") or {}).get("audio_required") or
            (assembly_qc.get("file") or {}).get("path") != basecut.get("path") or
            (assembly_qc.get("file") or {}).get("sha256") != basecut.get("sha256") or
            not file_record_is_current(assembly_qc_file)):
        raise ValueError("VIDEO_ASSEMBLY_QC_REQUIRED_OR_STALE")
    try:
        with open(assembly_qc_file["path"], encoding="utf-8") as handle:
            persisted_assembly_qc = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("VIDEO_ASSEMBLY_QC_REQUIRED_OR_STALE") from exc
    if persisted_assembly_qc != assembly_qc:
        raise ValueError("VIDEO_ASSEMBLY_QC_REQUIRED_OR_STALE")
    if artifact.get("handoff_sha256") != handoff.get("sha256"):
        raise ValueError("VIDEO_APPROVAL_HANDOFF_MISMATCH")
    with open(artifact["segments"]["path"], encoding="utf-8") as handle:
        segments = json.load(handle).get("segments") or []
    with open(artifact["results"]["path"], encoding="utf-8") as handle:
        raw = json.load(handle)
        results = raw.get("results") if isinstance(raw, dict) else raw
    with open(artifact["reviews"]["path"], encoding="utf-8") as handle:
        reviews = json.load(handle)
    ids = {str(segment.get("id")) for segment in segments}
    if not ids or ids != set((handoff.get("segments") or {}).keys()) \
            or ids != set((manifest.get("accepted_takes") or {}).keys()) \
            or ids != set(map(str, (reviews or {}).keys())):
        raise ValueError("VIDEO_APPROVAL_CLOSURE_INCOMPLETE")
    by_id = {str(result.get("segment_id")): result for result in results or []}
    for segment in segments:
        sid = str(segment.get("id"))
        result, review = by_id.get(sid) or {}, reviews.get(sid) or {}
        accepted = manifest["accepted_takes"][sid]
        audio = segment.get("audio_contract") or {}
        authored_audio = [key for key in ("voice", "language", "bgm", "sfx")
                          if audio.get(key) not in (None, "", False)]
        if audio.get("lip_sync"):
            authored_audio.append("lip_sync")
        audio_review = ((review.get("quality") or {}).get("audio_contract") or {})
        if any(audio_review.get(key) is not True for key in authored_audio):
            raise ValueError("VIDEO_AUDIO_CONTRACT_REVIEW_REQUIRED: %s (%s)" %
                             (sid, ",".join(authored_audio)))
        if (not result.get("ok") or not (result.get("media_qc") or {}).get("passed") or
                result.get("take_fingerprint") != accepted.get("take_fingerprint") or
                not take_review.is_accepted(review, accepted.get("take_fingerprint")) or
                not ocr_take_is_clear_or_waived(manifest, sid, accepted.get("take_fingerprint"))):
            raise ValueError("VIDEO_APPROVAL_TAKE_CLOSURE_FAILED: %s" % sid)
    return True


def reconcile_tasks_from_ledger(manifest, ledger, **kwargs):
    """Restore manifest task state from durable ledger events."""
    import generation_ledger
    return generation_ledger.reconcile_manifest_tasks(manifest, ledger, **kwargs)


def _bind_recovery_canary_requirement(manifest, segments_spec):
    """Promote a contract-bound uncertain recovery route to test_segment."""
    recovery = (segments_spec or {}).get("video_recovery_contract") or {}
    strategy = recovery.get("strategy") or {}
    if strategy.get("require_canary_before_batch") is not True:
        manifest.pop("recovery_test_segment_requirement", None)
        return False
    canary_id = str(strategy.get("canary_segment_id") or "").strip()
    segments = (segments_spec or {}).get("segments") or []
    matching = [segment for segment in segments
                if isinstance(segment, dict) and
                str(segment.get("id") or "") == canary_id]
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    fingerprint = (handoff.get("segments") or {}).get(canary_id)
    contract = recovery.get("file") or {}
    if (len(matching) != 1 or not fingerprint or
            matching[0].get("video_handoff_fingerprint") != fingerprint or
            not verify_video_handoff(matching[0]).get("ok") or
            not file_record_is_current(contract) or
            (matching[0].get("recovery_contract") or {}).get("recovery_id") !=
            recovery.get("recovery_id")):
        raise ValueError("VIDEO_RECOVERY_CANARY_HANDOFF_REQUIRED")
    previous = manifest.get("recovery_test_segment_requirement") or {}
    if (str(previous.get("segment_id") or "") == canary_id and
            previous.get("video_handoff_fingerprint") == fingerprint and
            previous.get("recovery_id") == recovery.get("recovery_id") and
            previous.get("contract") == contract):
        # A different recovery segment may receive an attempt-specific output
        # after this exact canary was approved.  Preserve that proof when the
        # canary's own model/handoff/contract did not change.
        manifest["requires_test_segment"] = True
        return False
    _archive_generation_attempt(
        manifest, "test_segment", superseded_by="recovery_canary_handoff")
    manifest.setdefault("generation", {}).pop("test_segment", None)
    manifest["requires_test_segment"] = True
    manifest["recovery_test_segment_requirement"] = {
        "reason": "empirical_integrated_audio_and_quality_canary",
        "segment_id": canary_id,
        "video_handoff_fingerprint": fingerprint,
        "recovery_id": recovery.get("recovery_id"),
        "contract": copy.deepcopy(contract),
        "bound_at": datetime.now().isoformat(timespec="seconds"),
    }
    invalidate_from(manifest, "test_segment")
    manifest["status"] = "needs_test_segment_generation"
    return True


def record_video_handoff(manifest, segments_spec, path=None):
    previous_handoff = copy.deepcopy(
        (manifest.get("handoffs") or {}).get("video") or {})
    segments = segments_spec.get("segments") if isinstance(segments_spec, dict) else segments_spec
    if not isinstance(segments_spec, dict):
        raise ValueError("VIDEO_HANDOFF_SPEC_REQUIRED")
    approval = segments_spec.get("storyboard_approval") or {}
    if approval.get("status") != "confirmed":
        raise ValueError("STORYBOARD_APPROVAL_REQUIRED")
    if (not approval_is_current(manifest, "storyboard") or
            not customer_approval_evidence_is_current(manifest, "storyboard")):
        raise ValueError("STORYBOARD_MANIFEST_APPROVAL_REQUIRED")
    evidence = (manifest.get("approval_evidence") or {}).get("storyboard") or {}
    approval_path = evidence.get("approval_path")
    try:
        with open(approval_path, encoding="utf-8") as handle:
            marker = json.load(handle)
    except (TypeError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("STORYBOARD_APPROVAL_REQUIRED") from exc
    marker_record = _file_record(approval_path) or {}
    bound_fields = (
        "kind", "client", "run_id", "confirmed_by_customer",
        "customer_confirmation", "confirmation_source", "confirmed_at",
    )
    if (approval.get("approval_path") != os.path.abspath(approval_path) or
            approval.get("approval_sha256") != marker_record.get("sha256") or
            any(approval.get(field) != marker.get(field) for field in bound_fields) or
            marker.get("confirmed_by_customer") is not True or
            not str(marker.get("customer_confirmation") or "").strip() or
            marker.get("confirmation_source") != "customer_dialogue"):
        raise ValueError("STORYBOARD_APPROVAL_EVIDENCE_MISMATCH")
    if (segments_spec.get("client") != manifest.get("client") or
            str(segments_spec.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("VIDEO_HANDOFF_RUN_IDENTITY_MISMATCH")
    if (approval.get("client") != manifest.get("client") or
            str(approval.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("STORYBOARD_APPROVAL_IDENTITY_MISMATCH")
    if segments_spec.get("missing_images") or segments_spec.get("needs_image") or not segments:
        raise ValueError("VIDEO_HANDOFF_INCOMPLETE")
    for segment in segments:
        if (segment.get("client") != manifest.get("client") or
                str(segment.get("run_id")) != str(manifest.get("run_id")) or
                segment.get("storyboard_approval") != approval):
            raise ValueError("VIDEO_HANDOFF_SEGMENT_IDENTITY_MISMATCH")
        if not verify_video_handoff(segment).get("ok"):
            raise ValueError("STALE_VIDEO_HANDOFF: %s" % segment.get("id"))
        validate_run_id(str(segment.get("id")))
        if segment.get("out_path"):
            require_run_output(manifest, segment["out_path"], label="segment_output")
    payload = {segment.get("id"): segment.get("video_handoff_fingerprint")
               for segment in segments or []}
    record = _file_record(path) if path else None
    if path and (not record or not record.get("exists")):
        raise ValueError("VIDEO_HANDOFF_FILE_MISSING: %s" % os.path.abspath(path))
    manifest.setdefault("handoffs", {})["video"] = {
        "path": os.path.abspath(path) if path else None,
        "file": record,
        "sha256": _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        "segments": payload, "recorded_at": datetime.now().isoformat(timespec="seconds")}
    if (previous_handoff.get("segments") and
            previous_handoff.get("segments") != payload):
        prompt_gate = (manifest.get("pre_video_gates") or {}).pop(
            "video_prompt_review", None)
        if prompt_gate:
            archived = copy.deepcopy(prompt_gate)
            archived["invalidated_at"] = datetime.now().isoformat(
                timespec="seconds")
            archived["reason"] = "video_handoff_changed"
            manifest.setdefault("pre_video_gate_history", []).append(
                archived)
        invalidate_from(manifest, "video")
    _bind_recovery_canary_requirement(manifest, segments_spec)
    return manifest


def accept_take(manifest, segment_id, review, review_path=None):
    """Persist one content-bound accepted take; recommendations are not approvals."""
    if review.get("segment_id") != segment_id:
        raise ValueError("TAKE_SEGMENT_MISMATCH")
    artifact = review.get("artifact") or {}
    take_fingerprint = artifact.get("take_fingerprint")
    if not take_fingerprint:
        raise ValueError("TAKE_FINGERPRINT_REQUIRED")
    expected_handoff = ((manifest.get("handoffs") or {}).get("video") or {}).get("segments", {}).get(segment_id)
    if not expected_handoff or artifact.get("video_handoff_fingerprint") != expected_handoff:
        raise ValueError("VIDEO_HANDOFF_MISMATCH")
    if (not _take_review.artifact_is_current(review)
            or not _take_review.artifact_fingerprint_is_current(review)):
        raise ValueError("STALE_TAKE_ARTIFACT")
    if (review.get("decision") or {}).get("acceptance_mode") != "formal":
        raise ValueError("FORMAL_TAKE_REVIEW_REQUIRED")
    if not _take_review.is_accepted(review):
        raise ValueError("TAKE_REVIEW_REQUIRED: take 尚未通过验片")
    if not ocr_take_is_clear_or_waived(manifest, segment_id, take_fingerprint):
        raise ValueError("OCR_NOT_CLEAR_OR_WAIVED")
    item = {"segment_id": segment_id, "take_id": review.get("take_id"),
            "review_id": review.get("review_id"),
            "take_fingerprint": review.get("artifact", {}).get("take_fingerprint"),
            "video_handoff_fingerprint": review.get("artifact", {}).get("video_handoff_fingerprint"),
            "accepted_at": datetime.now().isoformat(timespec="seconds")}
    if review_path:
        item["review"] = _file_record(review_path)
        if not item["review"] or not item["review"].get("exists"):
            raise ValueError("TAKE_REVIEW_FILE_MISSING: %s" % os.path.abspath(review_path))
    manifest.setdefault("accepted_takes", {})[segment_id] = item
    return item


def record_ocr_result(manifest, segment_id, take_fingerprint, status, ocr_texts=(),
                      *, available=None, frames_checked=0, expected=0, error=None,
                      source="automated", reviewer=None, reason=None):
    """Persist OCR evidence for one exact accepted/generated take."""
    if not segment_id or not take_fingerprint:
        raise ValueError("OCR_IDENTITY_REQUIRED: segment_id/take_fingerprint 缺失")
    legacy_bool = isinstance(status, bool)
    if legacy_bool:
        status = "detected" if status else "unavailable"
    if status not in ("clear", "detected", "unavailable", "error"):
        raise ValueError("OCR_STATUS_INVALID")
    texts = [str(text) for text in (ocr_texts or [])]
    if status == "detected" and not texts:
        raise ValueError("OCR_DETECTED_TEXT_REQUIRED")
    if available is None:
        available = status in ("clear", "detected")
    if status in ("clear", "detected") and not available:
        raise ValueError("OCR_AVAILABILITY_CONFLICT")
    if status == "clear" and source == "automated" \
            and (int(expected) <= 0 or int(frames_checked) != int(expected)):
        raise ValueError("OCR_COVERAGE_INCOMPLETE")
    item = {
        "segment_id": str(segment_id), "take_fingerprint": str(take_fingerprint),
        "status": status, "available": bool(available),
        "frames_checked": int(frames_checked), "expected": int(expected),
        "error": error, "ocr_warning": status == "detected", "ocr_texts": texts,
        "legacy_boolean_input": legacy_bool,
        "source": source, "reviewer": reviewer, "reason": reason,
        "ocr_texts_sha256": _digest(json.dumps(texts, ensure_ascii=False, sort_keys=True)),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    manifest.setdefault("ocr_checks", {})[str(segment_id)] = item
    return item


def record_manual_ocr_review(manifest, segment_id, take_fingerprint, status, *,
                              reviewer, reason, ocr_texts=(), frame_sha256s=()):
    """Persist attributed human OCR evidence bound to one immutable take."""
    import ocr_check
    record = ocr_check.manual_review(
        take_fingerprint, reviewer, reason, status, ocr_texts, frame_sha256s)
    item = record_ocr_result(
        manifest, segment_id, take_fingerprint, record["status"], record["texts"],
        available=True, frames_checked=record["frames_checked"],
        expected=record["expected"], source="manual",
        reviewer=record["reviewer"], reason=record["reason"])
    item["frame_sha256s"] = record["frame_sha256s"]
    item["first_frame_sha256"] = record["first_frame_sha256"]
    item["last_frame_sha256"] = record["last_frame_sha256"]
    return item


def grant_ocr_waiver(manifest, segment_id, take_fingerprint, ocr_texts, *, actor, reason):
    """Waive OCR only for the exact segment/take/text evidence currently recorded."""
    check = (manifest.get("ocr_checks") or {}).get(str(segment_id)) or {}
    texts = [str(text) for text in (ocr_texts or [])]
    if check.get("status") != "detected":
        raise ValueError("OCR_WAIVER_BLOCKED: 当前 take 没有 OCR warning")
    if check.get("take_fingerprint") != take_fingerprint:
        raise ValueError("OCR_WAIVER_TAKE_MISMATCH")
    if check.get("ocr_texts") != texts:
        raise ValueError("OCR_WAIVER_TEXT_MISMATCH")
    if not actor or not reason:
        raise ValueError("OCR_WAIVER_REASON_REQUIRED")
    waiver = {
        "segment_id": str(segment_id), "take_fingerprint": str(take_fingerprint),
        "ocr_texts": texts, "ocr_texts_sha256": check["ocr_texts_sha256"],
        "actor": actor, "reason": reason,
        "granted_at": datetime.now().isoformat(timespec="seconds"),
    }
    manifest.setdefault("ocr_waivers", {})[str(segment_id)] = waiver
    return waiver


def ocr_take_is_clear_or_waived(manifest, segment_id, take_fingerprint):
    """Validate clean OCR or an exact, non-transferable waiver for one take."""
    check = (manifest.get("ocr_checks") or {}).get(str(segment_id)) or {}
    if check.get("take_fingerprint") != take_fingerprint:
        return False
    automated_clear = (check.get("source") == "automated"
                       and check.get("frames_checked") == check.get("expected")
                       and check.get("expected", 0) > 0)
    manual_clear = (check.get("source") == "manual"
                    and bool(check.get("reviewer")) and bool(check.get("reason"))
                    and check.get("frames_checked") == check.get("expected")
                    and check.get("expected", 0) >= 12
                    and len(set(check.get("frame_sha256s") or [])) == check.get("expected"))
    if check.get("status") == "clear" and check.get("available") \
            and (automated_clear or manual_clear):
        return True
    if check.get("status") != "detected":
        return False
    waiver = (manifest.get("ocr_waivers") or {}).get(str(segment_id)) or {}
    return bool(
        waiver.get("take_fingerprint") == take_fingerprint
        and waiver.get("ocr_texts_sha256") == check.get("ocr_texts_sha256")
        and waiver.get("ocr_texts") == check.get("ocr_texts")
    )


def _main(argv=None):
    import run_manifest_cli
    return run_manifest_cli.main(sys.modules[__name__], argv)


def main(argv=None):
    try:
        return _main(argv)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc),
                          "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
