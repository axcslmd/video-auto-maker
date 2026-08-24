#!/usr/bin/env python3
"""Executable, host-neutral artifact view over the legacy stage manifest.

The legacy stage chain remains the compatibility surface.  This module derives
artifact readiness, dependency edges, milestone progress, and change impact
without mutating the manifest.  It is intentionally free of renderer/provider
logic so every supported host observes the same business state.
"""
from __future__ import annotations

from collections import deque
import argparse
import json
import os

import schema_validate


MILESTONES = (
    ("plan", "方案定稿"),
    ("previs", "素材与预演"),
    ("generation_review", "生成与验片"),
    ("finish_delivery", "后期与交付"),
)

STAGE_MILESTONES = {
    "brief": "plan",
    "script": "plan",
    "content_scaffold": "plan",
    "motion_design": "plan",
    "styleframe": "plan",
    "audio": "plan",
    "product_board": "previs",
    "cast_board": "previs",
    "product_usage": "previs",
    "storyboard": "previs",
    "render_plan": "previs",
    "test_segment": "generation_review",
    "video": "generation_review",
    "shotcraft_packaging": "finish_delivery",
    "captions": "finish_delivery",
    "final": "finish_delivery",
    "derive": "finish_delivery",
    "delivery": "finish_delivery",
}

_GENERATION_STATES = {
    "pending_approval": "awaiting_review",
    "pending_confirmation": "awaiting_review",
    "running": "running",
    "in_progress": "running",
    "submitted": "running",
    "completed": "produced",
    "succeeded": "produced",
    "finished": "produced",
    "failed": "failed",
    "error": "failed",
    "invalidated": "stale",
    "stale": "stale",
    "rejected": "rejected",
    "skipped": "skipped",
    "approved": "stale",
    "confirmed": "stale",
}

_COMPLETE_STATES = frozenset(("approved", "skipped"))
_ACTIVE_STATES = frozenset(("running", "awaiting_review", "produced"))


def artifact_id(stage):
    return stage if str(stage).startswith("artifact:") else "artifact:%s" % stage


def stage_from_artifact(value):
    value = str(value or "")
    return value.split(":", 1)[1] if value.startswith("artifact:") else value


def milestone_for_stage(stage):
    return STAGE_MILESTONES.get(str(stage or ""), "plan")


def _approval_current(run_manifest, manifest, stage):
    try:
        return bool(run_manifest.approval_is_current(manifest, stage))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _records(generation):
    records = []
    for item in generation.get("artifacts") or []:
        if isinstance(item, dict):
            records.append(dict(item))
    if records:
        return records
    for path in generation.get("outputs") or []:
        if isinstance(path, str) and path:
            records.append({"path": os.path.abspath(path), "exists": os.path.isfile(path)})
    return records


def _initial_state(run_manifest, manifest, stage):
    if _approval_current(run_manifest, manifest, stage):
        return "approved"
    generation = (manifest.get("generation") or {}).get(stage) or {}
    raw = str(generation.get("status") or "").strip().lower()
    if raw:
        return _GENERATION_STATES.get(raw, "planned")
    return "planned"


def _delivery_node(manifest, dependency):
    delivery = manifest.get("delivery") or {}
    path = delivery.get("path")
    current = bool(path and os.path.isfile(path))
    return {
        "artifact_id": artifact_id("delivery"),
        "artifact_type": "delivery",
        "stage": "delivery",
        "milestone": "finish_delivery",
        "state": "approved" if current and delivery.get("status") == "verified" else "blocked",
        "dependencies": [dependency] if dependency else [],
        "outputs": ([{"path": os.path.abspath(path), "exists": True}] if current else []),
        "approval_required": False,
        "cost_class": "local_deterministic",
    }


def _milestone_status(nodes):
    if not nodes:
        return "completed"
    states = [item["state"] for item in nodes]
    if all(state in _COMPLETE_STATES for state in states):
        return "completed"
    if any(state == "failed" for state in states):
        return "failed"
    if any(state == "stale" for state in states):
        return "stale"
    if any(state in _ACTIVE_STATES for state in states):
        return "in_progress"
    if any(state == "ready" for state in states):
        return "ready"
    return "blocked"


def compile_registry(manifest, segments=None, *, validate=True):
    """Derive one executable artifact registry without mutating ``manifest``."""
    import run_manifest

    manifest = manifest if isinstance(manifest, dict) else {}
    segments = segments if isinstance(segments, list) else []
    stages = list(run_manifest.required_stage_order(manifest))
    stage_set = set(stages)
    nodes = []
    node_by_stage = {}

    for stage in stages:
        dependencies = [dependency for dependency in
                        run_manifest.generation_dependencies(manifest, stage)
                        if dependency in stage_set]
        generation = (manifest.get("generation") or {}).get(stage) or {}
        node = {
            "artifact_id": artifact_id(stage),
            "artifact_type": stage,
            "stage": stage,
            "milestone": milestone_for_stage(stage),
            "state": _initial_state(run_manifest, manifest, stage),
            "dependencies": [artifact_id(item) for item in dependencies],
            "outputs": _records(generation),
            "approval_required": stage in run_manifest.CUSTOMER_EVIDENCE_STAGES,
            "cost_class": ("paid_video" if stage in {"test_segment", "video"}
                           else "paid_image" if stage in {
                               "product_board", "cast_board", "product_usage", "storyboard"}
                           else "local_deterministic"),
        }
        nodes.append(node)
        node_by_stage[stage] = node

    state_by_id = {item["artifact_id"]: item["state"] for item in nodes}
    for node in nodes:
        if node["state"] != "planned":
            continue
        dependency_states = [state_by_id.get(item, "blocked")
                             for item in node["dependencies"]]
        node["state"] = ("ready" if all(
            state in _COMPLETE_STATES for state in dependency_states) else "blocked")
        state_by_id[node["artifact_id"]] = node["state"]

    terminal_dependency = artifact_id(stages[-1]) if stages else None
    delivery = _delivery_node(manifest, terminal_dependency)
    if terminal_dependency and state_by_id.get(terminal_dependency) in _COMPLETE_STATES \
            and delivery["state"] == "blocked":
        delivery["state"] = "ready"
    nodes.append(delivery)

    edges = [{"from": dependency, "to": node["artifact_id"]}
             for node in nodes for dependency in node["dependencies"]]
    ready_actions = [{
        "artifact_id": node["artifact_id"],
        "stage": node["stage"],
        "milestone": node["milestone"],
        "action": ("review" if node["state"] in {"awaiting_review", "produced"}
                   else "produce"),
        "requires_customer": bool(
            node["state"] in {"awaiting_review", "produced"} and
            node["approval_required"]),
        "cost_class": node["cost_class"],
    } for node in nodes if node["state"] in {"ready", "awaiting_review", "produced"}]

    milestones = []
    for milestone_id, title in MILESTONES:
        members = [node for node in nodes if node["milestone"] == milestone_id]
        milestones.append({
            "id": milestone_id,
            "title": title,
            "state": _milestone_status(members),
            "completed_units": sum(
                node["state"] in _COMPLETE_STATES for node in members),
            "total_units": len(members),
        })

    payload = {
        "schema_version": 1,
        "client": str(manifest.get("client") or "unknown"),
        "run_id": str(manifest.get("run_id") or "unknown"),
        "nodes": nodes,
        "edges": edges,
        "milestones": milestones,
        "ready_actions": ready_actions,
    }
    if validate:
        schema_validate.enforce(payload, "artifact-registry", context="compile_registry")
    return payload


def preview_invalidation(manifest, changed_artifact_ids, segments=None):
    """Return graph descendants affected by a proposed artifact change."""
    registry = compile_registry(manifest, segments)
    known = {node["artifact_id"] for node in registry["nodes"]}
    changed = {artifact_id(item) for item in (changed_artifact_ids or [])}
    unknown = sorted(changed - known)
    if unknown:
        raise ValueError("ARTIFACT_CHANGE_UNKNOWN: %s" % ", ".join(unknown))
    children = {}
    for edge in registry["edges"]:
        children.setdefault(edge["from"], []).append(edge["to"])
    descendants = set()
    pending = deque(changed)
    while pending:
        current = pending.popleft()
        for child in children.get(current, []):
            if child not in changed and child not in descendants:
                descendants.add(child)
                pending.append(child)
    preserved = known - changed - descendants
    node_map = {node["artifact_id"]: node for node in registry["nodes"]}
    return {
        "changed": sorted(changed),
        "will_invalidate": sorted(descendants),
        "will_preserve": sorted(preserved),
        "reopened_reviews": sorted(
            item for item in descendants if node_map[item]["approval_required"]),
        "estimated_paid_tasks": sum(
            node_map[item]["cost_class"].startswith("paid_") for item in descendants),
    }


def _read_json(path, default):
    if not path:
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compile", "impact"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--segments")
    parser.add_argument("--changed", action="append", default=[])
    args = parser.parse_args(argv)
    manifest = _read_json(args.manifest, {})
    segment_payload = _read_json(args.segments, [])
    if isinstance(segment_payload, dict):
        segment_payload = segment_payload.get("segments") or []
    result = (compile_registry(manifest, segment_payload)
              if args.command == "compile" else
              preview_invalidation(manifest, args.changed, segment_payload))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
