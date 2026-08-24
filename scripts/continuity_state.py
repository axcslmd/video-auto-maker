#!/usr/bin/env python3
"""Accepted-take continuity state, parent gate and scene re-anchoring."""
import argparse
import copy
import json
import os
from datetime import datetime

import take_review
import schema_validate
from artifact_contract import file_sha256
from project_utils import atomic_write_json


class ContinuityGateError(ValueError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def _now():
    return datetime.now().isoformat(timespec="seconds")


def create_state(project_id, run_id, policy=None):
    merged = {"require_accepted_parent": True, "max_chain_depth": 3,
              "reanchor_on_scene_boundary": True, "require_observed_end_state": True}
    merged.update(policy or {})
    now = _now()
    return {"schema_version": 1, "project_id": project_id, "run_id": run_id,
            "created_at": now, "updated_at": now, "policy": merged,
            "scene_anchors": {}, "takes": {}, "scene_heads": {}, "events": []}


def register_scene_anchor(state, scene_id, anchor):
    state = copy.deepcopy(state)
    anchor = dict(anchor)
    anchor.setdefault("anchor_id", "anchor-%s" % scene_id)
    anchor.setdefault("scene_id", scene_id)
    anchor.setdefault("approved", False)
    state["scene_anchors"][scene_id] = anchor
    state["updated_at"] = _now()
    return state


def _approved_anchor(state, scene_id):
    anchor = state.get("scene_anchors", {}).get(scene_id)
    if not anchor or not anchor.get("approved"):
        raise ContinuityGateError("SCENE_ANCHOR_NOT_APPROVED",
                                  "场景 %s 缺少已确认锚点" % scene_id)
    return anchor


def _anchor_plan(state, segment, reason):
    scene_id = segment.get("scene_id") or "scene_default"
    anchor = _approved_anchor(state, scene_id)
    references = anchor.get("references") or [
        {"url": url, "type": role if index < len(anchor.get("reference_roles") or []) else "generic_visual",
         "scope": "scene"}
        for index, url in enumerate(anchor.get("reference_urls") or [])
        for role in [(anchor.get("reference_roles") or [])[index]
                     if index < len(anchor.get("reference_roles") or []) else "generic_visual"]
    ]
    return {"allowed": True, "mode": "scene_reanchor", "scene_id": scene_id,
            "segment_id": segment.get("id"), "next_chain_depth": 0,
            "reanchor_reason": reason, "anchor_id": anchor.get("anchor_id"),
            "references": references, "parent_take_id": None,
            "continuity_in": anchor.get("initial_state") or {}}


def _flatten(value, prefix=""):
    output = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            output.update(_flatten(item, path))
    elif value is not None:
        output[prefix] = value
    return output


def compare_states(observed, planned_entry):
    """Classify immutable continuity conflicts separately from transient drift."""
    actual = _flatten(observed or {})
    expected = _flatten(planned_entry or {})
    immutable_terms = ("character_id", "identity", "wardrobe", "sku", "product_id",
                       "geometry", "location", "scene_id")
    transient_terms = ("pose", "screen_position", "orientation", "gaze", "expression",
                       "camera", "lighting", "motion", "direction", "action_phase")
    errors, warnings = [], []
    for path, expected_value in expected.items():
        if path not in actual or actual[path] == expected_value:
            continue
        issue = {"path": path, "observed": actual[path], "planned": expected_value}
        if any(term in path.lower() for term in immutable_terms):
            issue["code"] = "IMMUTABLE_STATE_MISMATCH"
            errors.append(issue)
        elif any(term in path.lower() for term in transient_terms):
            issue["code"] = "TRANSIENT_STATE_MISMATCH"
            warnings.append(issue)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def plan_generation(state, segment, parent_take_id=None, parent_review=None):
    scene_id = segment.get("scene_id") or "scene_default"
    if not parent_take_id:
        return _anchor_plan(state, segment, "scene_root")
    parent = state.get("takes", {}).get(parent_take_id)
    if not parent:
        raise ContinuityGateError("PARENT_NOT_FOUND")
    if parent.get("scene_id") != scene_id:
        return _anchor_plan(state, segment, "scene_boundary")
    if not parent_review or not take_review.is_accepted(
            parent_review, parent.get("take_fingerprint")):
        raise ContinuityGateError("PARENT_NOT_ACCEPTED")
    if state.get("scene_heads", {}).get(scene_id, {}).get("accepted_take_id") != parent_take_id:
        raise ContinuityGateError("STALE_SCENE_HEAD")
    observed = parent_review.get("observed_end_state")
    if state.get("policy", {}).get("require_observed_end_state", True) and not observed:
        raise ContinuityGateError("PARENT_END_STATE_MISSING")
    depth = int(parent.get("chain", {}).get("depth", 0)) + 1
    if depth > int(state.get("policy", {}).get("max_chain_depth", 3)):
        return _anchor_plan(state, segment, "chain_depth_limit")
    frame = (observed or {}).get("frame_path") or (parent.get("observed_end_state") or {}).get("frame_path")
    expected_hash = (observed or {}).get("frame_sha256")
    if frame and expected_hash and file_sha256(frame) != expected_hash:
        raise ContinuityGateError("PARENT_TAIL_ARTIFACT_MISMATCH")
    if not frame:
        raise ContinuityGateError("PARENT_TAIL_ARTIFACT_MISSING")
    observed_state = (observed or {}).get("state") or observed or {}
    planned_entry = (segment.get("sequence_state") or {}).get("entry") or {}
    comparison = compare_states(observed_state, planned_entry)
    if not comparison["ok"]:
        raise ContinuityGateError("CONTINUITY_CONTRADICTION",
                                  json.dumps(comparison["errors"], ensure_ascii=False))
    return {"allowed": True, "mode": "tail_frame", "scene_id": scene_id,
            "segment_id": segment.get("id"), "parent_take_id": parent_take_id,
            "parent_review_id": parent_review.get("review_id"),
            "parent_take_fingerprint": parent.get("take_fingerprint"),
            "next_chain_depth": depth,
            "references": [{"url": frame, "type": "continuation_frame", "scope": "clip",
                            "sha256": expected_hash or file_sha256(frame)}],
            "continuity_in": observed_state, "warnings": comparison["warnings"]}


def register_take(state, segment, generation_result, generation_plan):
    state = copy.deepcopy(state)
    take_id = generation_result.get("take_id") or "%s-take-01" % segment.get("id")
    record = {"take_id": take_id, "segment_id": segment.get("id"),
              "scene_id": segment.get("scene_id") or "scene_default",
              "take_fingerprint": generation_result.get("take_fingerprint"),
              "parent": {"take_id": generation_plan.get("parent_take_id"),
                         "review_id": generation_plan.get("parent_review_id"),
                         "relation": generation_plan.get("mode")},
              "chain": {"depth": generation_plan.get("next_chain_depth", 0),
                        "reanchored": generation_plan.get("mode") == "scene_reanchor",
                        "reanchor_reason": generation_plan.get("reanchor_reason")},
              "generation": {"status": "succeeded" if generation_result.get("ok") else "failed",
                             "task_id": generation_result.get("taskId"),
                             "video_url": generation_result.get("videoUrl"),
                             "local_path": generation_result.get("localPath")},
              "review": {"verdict": "pending"}, "created_at": _now()}
    state["takes"][take_id] = record
    state["events"].append({"event": "take_registered", "take_id": take_id, "at": _now()})
    state["updated_at"] = _now()
    return state


def accept_take(state, review):
    state = copy.deepcopy(state)
    take_id = review.get("take_id")
    record = state.get("takes", {}).get(take_id)
    if not record:
        raise ContinuityGateError("TAKE_NOT_FOUND")
    if not take_review.is_accepted(review, record.get("take_fingerprint")):
        raise ContinuityGateError("TAKE_NOT_ACCEPTED")
    record["review"] = {"review_id": review.get("review_id"), "verdict": "accepted"}
    record["observed_end_state"] = review.get("observed_end_state")
    record["accepted_at"] = _now()
    state["scene_heads"][record["scene_id"]] = {
        "accepted_take_id": take_id, "review_id": review.get("review_id"),
        "chain_depth": record.get("chain", {}).get("depth", 0)}
    state["events"].append({"event": "take_accepted", "take_id": take_id,
                            "review_id": review.get("review_id"), "at": _now()})
    state["updated_at"] = _now()
    return state


def save_state(state, path):
    state = dict(state)
    state["updated_at"] = _now()
    # 契约强制：continuity-state schema 运行时校验（fail-closed）
    schema_validate.enforce(state, "continuity-state", context="continuity_state.save_state")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    atomic_write_json(path, state)
    return os.path.abspath(path)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--project-id", required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--out", required=True)
    init.add_argument("--max-chain-depth", type=int, default=3)
    anchor_cmd = sub.add_parser("add-anchor")
    anchor_cmd.add_argument("--state", required=True)
    anchor_cmd.add_argument("--scene-id", required=True)
    anchor_cmd.add_argument("--anchor", required=True)
    anchor_cmd.add_argument("--out", required=True)
    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("--state", required=True)
    plan_cmd.add_argument("--segment", required=True)
    plan_cmd.add_argument("--parent-take-id")
    plan_cmd.add_argument("--parent-review")
    plan_cmd.add_argument("--out", required=True)
    accept_cmd = sub.add_parser("accept")
    accept_cmd.add_argument("--state", required=True)
    accept_cmd.add_argument("--review", required=True)
    accept_cmd.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "init":
        state = create_state(args.project_id, args.run_id,
                             {"max_chain_depth": args.max_chain_depth})
        save_state(state, args.out)
        output = state
    elif args.command == "add-anchor":
        with open(args.state, encoding="utf-8") as handle:
            state = json.load(handle)
        with open(args.anchor, encoding="utf-8") as handle:
            anchor = json.load(handle)
        output = register_scene_anchor(state, args.scene_id, anchor)
        save_state(output, args.out)
    elif args.command == "plan":
        with open(args.state, encoding="utf-8") as handle:
            state = json.load(handle)
        with open(args.segment, encoding="utf-8") as handle:
            segment = json.load(handle)
        review = None
        if args.parent_review:
            with open(args.parent_review, encoding="utf-8") as handle:
                review = json.load(handle)
        output = plan_generation(state, segment, args.parent_take_id, review)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        atomic_write_json(args.out, output)
    else:
        with open(args.state, encoding="utf-8") as handle:
            state = json.load(handle)
        with open(args.review, encoding="utf-8") as handle:
            review = json.load(handle)
        output = accept_take(state, review)
        save_state(output, args.out)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
