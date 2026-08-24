#!/usr/bin/env python3
"""Build a compact stage packet so host LLMs do not reread the whole project."""
import argparse
import json
import os

import agent_runtime
import artifact_registry
import model_behavior
import workflow_router
from project_utils import atomic_write_json, require_contained_path, sha256_file
from schema_validate import enforce


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


_CAPABILITY_ALIASES = {
    "local_image_preview": "image_preview",
    "local_video_preview": "video_preview",
    "progress_updates": "progress_stream",
}


def _read(path, default=None):
    if not path:
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _artifact(path):
    try:
        path = require_contained_path(ROOT, path, label="stage_artifact",
                                      must_exist=False)
    except (TypeError, ValueError):
        return {"path": "[BLOCKED_OUTSIDE_PROJECT]", "exists": False,
                "blocked": True}
    record = {"path": path, "exists": os.path.exists(path)}
    if os.path.isfile(path):
        record.update({"sha256": sha256_file(path), "bytes": os.path.getsize(path)})
    return record


def build_packet(*, manifest=None, brief=None, storyboard_result=None, segments=None,
                 events=None, source_facts=None, model_profile=None,
                 host_capability_record=None):
    route = workflow_router.route_workflow(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result,
        segments=segments, events=events, source_facts=source_facts,
        model_profile=model_profile)
    host = host_capability_record or agent_runtime.host_capabilities()
    enforce(host, "host-capabilities", context="stage_packet.host")
    available = host["capabilities"]
    missing = []
    for required in route.get("required_host_capabilities") or []:
        key = _CAPABILITY_ALIASES.get(required, required)
        if not available.get(key, False):
            missing.append(required)
    flow = route.get("flow") if isinstance(route.get("flow"), dict) else {}
    paths = list(dict.fromkeys((route.get("customer_preview") or []) +
                               (route.get("stage_sources") or [])))
    packet = {
        "schema_version": 1,
        "protocol_version": route["protocol_version"],
        "stage": route["current_stage"],
        "next_action": route["next_action"],
        "chain_profile": route.get("chain_profile") or {},
        "host": host,
        "missing_host_capabilities": missing,
        "interaction_contract": route["interaction_contract"],
        "entry_functions": flow.get("entry_functions") or [],
        "transaction": flow.get("transaction") or {},
        "confirm_function": flow.get("confirm_function"),
        "artifacts": [_artifact(path) for path in paths if isinstance(path, str)],
        "guidance_index": route.get("guidance_resources") or [],
    }
    enforce(packet, "stage-packet", context="stage_packet.build")
    return packet


def _cost_class(registry, stage):
    match = next((node for node in registry.get("nodes") or []
                  if node.get("stage") == stage), None)
    return (match or {}).get("cost_class") or "local_deterministic"


def _review_pack(manifest, stage, artifacts):
    existing = [item for item in (manifest.get("review_presentations") or {}).values()
                if isinstance(item, dict) and item.get("stage") == stage]
    status = "needs_presentation"
    presentation_id = None
    if existing:
        presentation_id = existing[-1].get("presentation_id")
        try:
            import review_task
            review_task.validate_presentation(
                manifest, stage, presentation_id,
                [item["path"] for item in artifacts if item.get("exists")])
            status = "presented"
        except (OSError, TypeError, ValueError, KeyError):
            status = "stale"
    return {
        "stage": stage,
        "status": status,
        "presentation_id": presentation_id,
        "artifact_count": sum(bool(item.get("exists")) for item in artifacts),
    } if artifacts else None


def build_experience_packet(*, manifest=None, brief=None, storyboard_result=None,
                            segments=None, events=None, source_facts=None,
                            model_profile=None, host_capability_record=None,
                            impact_preview=None):
    """Build the V2 UX contract: one milestone and one primary next action.

    The detailed legacy stage chain is preserved in the manifest, while this
    packet gives every host the same compact customer-facing experience.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    route = workflow_router.route_workflow(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result,
        segments=segments, events=events, source_facts=source_facts,
        model_profile=model_profile)
    host = host_capability_record or agent_runtime.host_capabilities()
    enforce(host, "host-capabilities", context="experience_packet.host")
    available = host["capabilities"]
    missing = []
    for required in route.get("required_host_capabilities") or []:
        key = _CAPABILITY_ALIASES.get(required, required)
        if not available.get(key, False):
            missing.append(required)

    registry = artifact_registry.compile_registry(manifest, segments)
    milestone_id = artifact_registry.milestone_for_stage(route["current_stage"])
    milestone = next(
        item for item in registry["milestones"] if item["id"] == milestone_id)
    paths = list(dict.fromkeys((route.get("customer_preview") or []) +
                               (route.get("stage_sources") or [])))
    artifacts = [_artifact(path) for path in paths if isinstance(path, str)]
    action_id = str(route.get("next_action") or "inspect_workflow")
    requires_customer = action_id.startswith((
        "review_", "approve_", "confirm_", "choose_", "provide_"))
    blockers = ["HOST_CAPABILITY_MISSING:%s" % item for item in missing]
    if route.get("workflow_contract_error"):
        blockers.append(str(route["workflow_contract_error"]))
    completed = sum(item["state"] == "completed"
                    for item in registry["milestones"])
    packet = {
        "schema_version": 2,
        "protocol_version": str(route["protocol_version"]),
        "stage": route["current_stage"],
        "milestone": milestone,
        "progress": {
            "completed_milestones": completed,
            "total_milestones": 4,
            "label": "%d/4 个交付里程碑已完成" % completed,
        },
        "primary_action": {
            "id": action_id,
            "label": str(route.get("customer_message") or action_id),
            "stage": route["current_stage"],
            "enabled": not blockers,
            "requires_customer": requires_customer,
            "cost_class": _cost_class(registry, route["current_stage"]),
        },
        "secondary_actions": ([{
            "id": "view_artifacts", "label": "查看当前审批物"
        }] if artifacts else []),
        "chain_profile": route.get("chain_profile") or {},
        "host": host,
        "missing_host_capabilities": missing,
        "blockers": blockers,
        "artifacts": artifacts,
        "review_pack": _review_pack(manifest, route["current_stage"], artifacts),
        "impact_preview": impact_preview,
        "interaction_contract": route.get("interaction_contract") or {},
        "guidance_index": route.get("guidance_resources") or [],
    }
    enforce(packet, "experience-packet", context="stage_packet.build_v2")
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    parser.add_argument("--brief")
    parser.add_argument("--storyboard-result")
    parser.add_argument("--segments")
    parser.add_argument("--events")
    parser.add_argument("--source-facts")
    parser.add_argument("--model-profile")
    parser.add_argument("--host-capabilities")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    segment_payload = _read(args.segments, [])
    if isinstance(segment_payload, dict):
        segment_payload = segment_payload.get("segments") or []
    packet = build_packet(
        manifest=_read(args.manifest, {}), brief=_read(args.brief, {}),
        storyboard_result=_read(args.storyboard_result, {}),
        segments=segment_payload, events=_read(args.events, []),
        source_facts=_read(args.source_facts, {}),
        model_profile=model_behavior.load_profile(args.model_profile) if args.model_profile else None,
        host_capability_record=_read(args.host_capabilities, None))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        atomic_write_json(args.out, packet)
    print(json.dumps(packet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
