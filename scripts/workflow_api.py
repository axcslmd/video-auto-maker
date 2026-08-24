#!/usr/bin/env python3
"""Stable host-neutral workflow API over the existing formal business gates."""
import argparse
import copy
import json
import os

import approval_preview
import interaction_contract
import artifact_registry
import model_behavior
import pipeline
import review_task
import run_manifest
import stage_packet
import workflow_router
from project_utils import require_contained_path, sha256_file


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _record(path):
    return {"path": os.path.abspath(path), "sha256": sha256_file(path),
            "bytes": os.path.getsize(path)}


def _route(manifest, model_profile=None):
    return workflow_router.route_workflow(manifest=manifest, model_profile=model_profile)


def workflow_status(manifest, model_profile=None):
    return _route(manifest, model_profile=model_profile)


def workflow_next(manifest, model_profile=None):
    """Legacy V1 packet retained for thin adapters during migration."""
    return stage_packet.build_packet(manifest=manifest, model_profile=model_profile)


def workflow_next_v2(manifest, model_profile=None):
    return stage_packet.build_experience_packet(
        manifest=manifest, model_profile=model_profile)


def workflow_impact_preview(manifest, changed_artifact_ids, segments=None):
    return artifact_registry.preview_invalidation(
        manifest, changed_artifact_ids, segments)


def artifact_get(manifest, model_profile=None):
    route = _route(manifest, model_profile=model_profile)
    artifacts = []
    blocked = 0
    for path in route.get("customer_preview") or []:
        try:
            trusted = require_contained_path(
                ROOT, path, label="preview_artifact", must_exist=True)
        except (TypeError, ValueError):
            blocked += 1
            continue
        artifacts.append(_record(trusted))
    return {"ok": True, "stage": route["current_stage"], "artifacts": artifacts,
            "blocked_outside_project": blocked,
            "must_show_before_confirmation": True}


def workflow_submit_patch(manifest, target, proposal, model_profile=None):
    route = _route(manifest, model_profile=model_profile)
    target = require_contained_path(ROOT, target, label="patch_target", must_exist=True)
    return interaction_contract.apply_proposal(
        route["interaction_contract"], proposal, target, target_schema=None)


def workflow_refine(manifest, target, proposal, model_profile=None):
    """Refinement is the same protected Patch transaction with explicit intent."""
    return workflow_submit_patch(manifest, target, proposal, model_profile)


def _current_preview_paths(route):
    paths = []
    for path in route.get("customer_preview") or []:
        try:
            trusted = require_contained_path(
                ROOT, path, label="review_artifact", must_exist=True)
        except (TypeError, ValueError):
            continue
        if trusted not in paths:
            paths.append(trusted)
    return paths


def workflow_review_present(manifest, manifest_path, *, stage=None,
                            channel="markdown", model_profile=None):
    """Persist proof that the exact current bytes were presented for review."""
    route = _route(manifest, model_profile=model_profile)
    current = route["current_stage"]
    stage = stage or current
    if stage != current:
        raise ValueError("WORKFLOW_STAGE_STALE: 当前为 %s，不能展示 %s" % (current, stage))
    # Build the receipt on an isolated snapshot. A failed CAS save must not
    # leave the caller's in-memory manifest claiming a presentation that was
    # never durably committed.
    candidate = copy.deepcopy(manifest)
    prepared = approval_preview.ensure_stage_preview(candidate, stage)
    if prepared:
        route = _route(candidate, model_profile=model_profile)
    paths = _current_preview_paths(route)
    if not paths:
        raise ValueError("APPROVAL_PREVIEW_REQUIRED: 当前阶段没有可展示的真实产物")
    receipt = review_task.present(
        candidate, stage, paths, channel=channel,
        expected_stage_input_fingerprint=(prepared or {}).get(
            "stage_input_fingerprint"))
    run_manifest.save_manifest(candidate, manifest_path)
    manifest.clear()
    manifest.update(candidate)
    return {"ok": True, "stage": stage, "presentation": receipt,
            "prepared_preview": prepared,
            "artifacts": [_record(path) for path in paths]}


def workflow_confirm(manifest, manifest_path, customer_confirmation, stage=None,
                     presentation_id=None, storyboard_result=None,
                     caption_manifest=None, geometry_review=None,
                     test_review=None, model_profile=None):
    route = _route(manifest, model_profile=model_profile)
    current = route["current_stage"]
    stage = stage or current
    if stage != current:
        raise ValueError("WORKFLOW_STAGE_STALE: 当前为 %s，不能确认 %s" % (current, stage))
    if stage not in run_manifest.CUSTOMER_EVIDENCE_STAGES:
        raise ValueError("WORKFLOW_CUSTOMER_CONFIRM_UNAVAILABLE: 请执行阶段包返回的技术确认函数")
    previews = _current_preview_paths(route)
    if stage not in {"idea", "delivery"} and not previews:
        raise ValueError("APPROVAL_PREVIEW_REQUIRED: 必须先生成并展示当前阶段产物")
    if not str(customer_confirmation or "").strip():
        raise ValueError("CUSTOMER_CONFIRMATION_EVIDENCE_REQUIRED")
    if not presentation_id:
        raise ValueError(
            "REVIEW_PRESENTATION_REQUIRED: 请先调用 workflow_review_present 展示当前产物")
    review_task.validate_presentation(manifest, stage, presentation_id, previews)
    pipeline.approve_stage(
        manifest, manifest_path, stage, storyboard_result=storyboard_result,
        customer_confirmation=customer_confirmation, caption_manifest=caption_manifest,
        geometry_review=geometry_review, test_review=test_review)
    refreshed = run_manifest.load_manifest(manifest_path)
    return {"ok": True, "stage": stage,
            "approval_current": run_manifest.approval_is_current(refreshed, stage),
            "next": _route(refreshed, model_profile=model_profile)}


def workflow_confirm_v2(*args, **kwargs):
    """Named V2 confirmation entry; delegates to the same formal approval gate."""
    return workflow_confirm(*args, **kwargs)


def task_status(manifest, stage=None):
    stage = stage or workflow_router.route_workflow(manifest=manifest)["current_stage"]
    generation = (manifest.get("generation") or {}).get(stage) or {}
    return {"ok": True, "stage": stage, "status": generation.get("status") or "not_started",
            "outputs": generation.get("outputs") or [],
            "task_ids": generation.get("task_ids") or generation.get("tasks") or []}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "next", "next-v2", "impact", "artifact-get", "task-status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--manifest", required=True)
        cmd.add_argument("--model-profile")
        if name == "task-status":
            cmd.add_argument("--stage")
        if name == "impact":
            cmd.add_argument("--changed", action="append", required=True)
    for name in ("submit-patch", "refine"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--manifest", required=True)
        cmd.add_argument("--target", required=True)
        cmd.add_argument("--proposal", required=True)
        cmd.add_argument("--model-profile")
    present = sub.add_parser("review-present")
    present.add_argument("--manifest", required=True)
    present.add_argument("--stage")
    present.add_argument("--channel", default="markdown")
    present.add_argument("--model-profile")
    for command in ("confirm", "confirm-v2"):
        confirm = sub.add_parser(command)
        confirm.add_argument("--manifest", required=True)
        confirm.add_argument("--stage")
        confirm.add_argument("--customer-confirmation", required=True)
        confirm.add_argument("--presentation-id", required=True)
        confirm.add_argument("--storyboard-result")
        confirm.add_argument("--caption-manifest")
        confirm.add_argument("--geometry-review")
        confirm.add_argument("--test-review")
        confirm.add_argument("--model-profile")
    args = parser.parse_args(argv)
    manifest = run_manifest.load_manifest(args.manifest)
    profile = model_behavior.load_profile(args.model_profile) if args.model_profile else None
    if args.command == "status":
        result = workflow_status(manifest, profile)
    elif args.command == "next":
        result = workflow_next(manifest, profile)
    elif args.command == "next-v2":
        result = workflow_next_v2(manifest, profile)
    elif args.command == "impact":
        result = workflow_impact_preview(manifest, args.changed)
    elif args.command == "artifact-get":
        result = artifact_get(manifest, profile)
    elif args.command == "task-status":
        result = task_status(manifest, args.stage)
    elif args.command in ("submit-patch", "refine"):
        function = workflow_refine if args.command == "refine" else workflow_submit_patch
        result = function(manifest, args.target, _read(args.proposal), profile)
    elif args.command == "review-present":
        result = workflow_review_present(
            manifest, args.manifest, stage=args.stage, channel=args.channel,
            model_profile=profile)
    else:
        confirm_function = (workflow_confirm_v2 if args.command == "confirm-v2"
                            else workflow_confirm)
        result = confirm_function(
            manifest, args.manifest, args.customer_confirmation, stage=args.stage,
            presentation_id=args.presentation_id,
            storyboard_result=args.storyboard_result,
            caption_manifest=args.caption_manifest,
            geometry_review=args.geometry_review, test_review=args.test_review,
            model_profile=profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
