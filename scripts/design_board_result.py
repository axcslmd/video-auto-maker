#!/usr/bin/env python3
"""Register an approved deterministic execution preview as a design-board result.

The document-explainer renderer produces an execution-preview bundle.  The
storyboard approval gate consumes the host-neutral storyboard-result envelope.
This adapter joins those two contracts without rendering or altering media.
"""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import storyboard  # noqa: E402


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def build(plan_path, execution_path, manifest_path, out_path):
    plan_path = os.path.abspath(plan_path)
    execution_path = os.path.abspath(execution_path)
    manifest_path = os.path.abspath(manifest_path)
    out_path = os.path.abspath(out_path)
    for label, path in (
            ("plan", plan_path), ("execution preview", execution_path),
            ("manifest", manifest_path)):
        if not os.path.isfile(path):
            raise ValueError("DESIGN_BOARD_RESULT_INPUT_MISSING: %s=%s" % (label, path))

    plan = _load(plan_path)
    execution = _load(execution_path)
    manifest = _load(manifest_path)
    binding = execution.get("manifest_binding") or {}
    if (binding.get("status") != "bound" or
            binding.get("approval_eligible") is not True or
            os.path.abspath(binding.get("manifest_path") or "") != manifest_path):
        raise ValueError("DESIGN_BOARD_EXECUTION_NOT_APPROVAL_ELIGIBLE")
    if (manifest.get("client") != plan.get("client") or
            manifest.get("run_id") != plan.get("run_id")):
        raise ValueError("DESIGN_BOARD_RESULT_IDENTITY_MISMATCH")
    generation = (manifest.get("generation") or {}).get("storyboard") or {}
    if generation.get("status") != "pending_approval":
        raise ValueError("DESIGN_BOARD_RESULT_PENDING_APPROVAL_REQUIRED")

    plan_shots = list(plan.get("shots") or [])
    by_id = {str(shot.get("id") or ""): shot for shot in plan_shots}
    rendered = []
    for record in execution.get("shots") or []:
        shot_id = str(record.get("id") or "")
        shot = by_id.get(shot_id)
        if not shot:
            raise ValueError("DESIGN_BOARD_RESULT_UNKNOWN_SHOT: %s" % shot_id)
        path = os.path.abspath(record.get("path") or record.get("abspath") or "")
        if (record.get("renderer") != "remotion.IntegratedExplainer" or
                record.get("deterministic_render") is not True or
                not os.path.isfile(path)):
            raise ValueError("DESIGN_BOARD_RESULT_INVALID_SHOT: %s" % shot_id)
        item = dict(record)
        item.update({
            "path": path,
            "abspath": path,
            "shot": shot,
            "plan_fingerprint": storyboard.plan_fingerprint(plan),
            "shot_fingerprint": storyboard.shot_fingerprint(shot),
            "design_board_qc": {
                "ok": True,
                "method": "deterministic_remotion_render+manual_visual_qa+commercial_qc",
                "commercial_qc": os.path.join(os.path.dirname(execution_path),
                                               "commercial_qc.json"),
            },
        })
        rendered.append(item)
    expected_ids = [str(shot.get("id") or "") for shot in plan_shots]
    if [item.get("id") for item in rendered] != expected_ids:
        raise ValueError("DESIGN_BOARD_RESULT_SHOT_SET_MISMATCH")

    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    composite_plan = storyboard._build_design_board_composite_plan(plan, rendered)
    composite_plan.setdefault("contract", {})["renderer_source_of_truth"] = (
        "remotion.IntegratedExplainer")
    composite_plan["contract"]["animatic_required_for_approval"] = True
    composite_path = os.path.join(out_dir, "composite_plan.json")
    storyboard.atomic_write_json(composite_path, composite_plan)

    result = {
        "schema_version": 2,
        "type": "deterministic_design_board_storyboard_result",
        "client": plan.get("client"),
        "run_id": plan.get("run_id"),
        "out_dir": out_dir,
        "result_json": out_path,
        "source_plan": plan_path,
        "plan_fingerprint": storyboard.plan_fingerprint(plan),
        "shots": rendered,
        "layout_boards": rendered,
        "expected_shot_ids": expected_ids,
        "storyboard_role": "design_board",
        "storyboard_usage": "design_board",
        "video_model_usage": "do_not_submit_as_storyboard_ref",
        "design_board_renderer": "remotion.IntegratedExplainer",
        "design_board_artifact_contract": execution.get("artifact_contract") or {},
        "animatic": execution.get("animatic") or {},
        "content_layer": execution.get("content_layer") or {},
        "motion_layer": execution.get("motion_layer") or {},
        "hyperframes_spec": execution.get("hyperframes_spec") or {},
        "audio_timing_contract": execution.get("audio_timing_contract") or {},
        "approved_render_contract": execution.get("approved_render_contract") or {},
        "execution_preview_result": execution_path,
        "manifest_path": manifest_path,
        "manifest_binding": binding,
        "integrated_previs_spec": execution.get("integrated_spec") or {},
        "generation_keyframe_contract": (
            execution.get("generation_keyframe_contract") or {}),
        "composite_plan": composite_plan,
        "composite_plan_path": composite_path,
        "needs_confirmation": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    storyboard.atomic_write_json(out_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把正式 execution preview 登记为可确认的 design-board result")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--execution-preview", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = build(args.plan, args.execution_preview, args.manifest, args.out)
    print(json.dumps({
        "ok": True,
        "storyboard_result": os.path.abspath(args.out),
        "shots": len(result.get("shots") or []),
        "storyboard_role": result.get("storyboard_role"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
