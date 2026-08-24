#!/usr/bin/env python3
"""Storyboard run-directory and resume pointer helpers.

These helpers are intentionally dependency-injected by ``storyboard.py`` so the
public storyboard module keeps its old function names while the filesystem
resume logic no longer lives inside the generation state machine.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from project_utils import atomic_write_json


def read_result(path):
    """Read a checkpoint without making a damaged/legacy file fatal."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def legacy_plan_matches(plan, previous):
    """Best-effort compatibility check for checkpoints created before fingerprints."""
    old_shots = previous.get("shots") or []
    new_shots = plan.get("shots") or []
    if len(old_shots) > len(new_shots):
        return False
    current_by_id = {str(s.get("id")): s for s in new_shots}
    for item in old_shots:
        old_shot = item.get("shot") or {}
        if current_by_id.get(str(old_shot.get("id"))) != old_shot:
            return False
    return True


def resolve_run_output_dir(base_out_dir, plan, run_id=None, flat=False, *,
                           run_output_dir_fn, plan_fingerprint_fn, safe_name_fn):
    """Resolve resume versus revision without overwriting an approved preview."""
    target = run_output_dir_fn(base_out_dir, plan, run_id=run_id, flat=flat)
    if flat or not run_id:
        return target
    fingerprint = plan_fingerprint_fn(plan)
    previous = read_result(os.path.join(target, "storyboard_result.json"))
    if not previous:
        return target
    if (previous.get("plan_fingerprint") == fingerprint or
            (not previous.get("plan_fingerprint") and legacy_plan_matches(plan, previous))):
        return target
    previous_shots = previous.get("shots") or []
    if previous_shots and all(item.get("shot_fingerprint") for item in previous_shots):
        return target
    revision = 2
    while True:
        candidate = os.path.join(target, "..", "%s__r%02d" % (safe_name_fn(run_id), revision))
        candidate = os.path.abspath(candidate)
        candidate_result = read_result(os.path.join(candidate, "storyboard_result.json"))
        if candidate_result and candidate_result.get("plan_fingerprint") == fingerprint:
            return candidate
        if not os.path.exists(candidate):
            return candidate
        revision += 1


def run_pointer_path(base_out_dir, run_id, *, safe_name_fn):
    if not run_id:
        return None
    return os.path.join(os.path.abspath(base_out_dir),
                        ".%s_current.json" % safe_name_fn(run_id))


def write_run_pointer(base_out_dir, run_id, plan, out_dir, result_path, *, stage=None,
                      plan_fingerprint_value=None,
                      visual_plan_fingerprint_value=None,
                      safe_name_fn, client_slug_fn, plan_fingerprint_fn,
                      visual_plan_fingerprint_fn):
    """Record the authoritative storyboard revision for a logical run_id."""
    pointer_path = run_pointer_path(base_out_dir, run_id, safe_name_fn=safe_name_fn)
    if not pointer_path:
        return None
    record = {
        "schema_version": 1,
        "run_id": safe_name_fn(run_id),
        "client": plan.get("client") or client_slug_fn(plan),
        "plan_fingerprint": plan_fingerprint_value or plan_fingerprint_fn(plan),
        "visual_plan_fingerprint": (visual_plan_fingerprint_value or
                                    visual_plan_fingerprint_fn(plan)),
        "out_dir": os.path.abspath(out_dir),
        "result_json": os.path.abspath(result_path),
        "stage": stage,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(pointer_path) or ".", exist_ok=True)
    atomic_write_json(pointer_path, record)
    return pointer_path


def resolve_current_storyboard_dir(base_out_dir, run_id, plan=None, *,
                                   safe_name_fn, plan_fingerprint_fn,
                                   canonical_storyboard_plan_fn,
                                   expand_product_sku_refs_fn):
    """Return the current revision directory recorded for a run_id, if valid."""
    pointer_path = run_pointer_path(base_out_dir, run_id, safe_name_fn=safe_name_fn)
    if not pointer_path or not os.path.isfile(pointer_path):
        return None
    try:
        record = json.loads(Path(pointer_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    out_dir = record.get("out_dir")
    result_json = record.get("result_json") or (
        os.path.join(out_dir, "storyboard_result.json") if out_dir else None)
    if not out_dir or not result_json or not os.path.isdir(out_dir):
        return None
    result = read_result(result_json)
    if not result:
        return None
    if str(record.get("run_id")) != safe_name_fn(run_id):
        return None
    if str(result.get("run_id")) != safe_name_fn(run_id):
        return None
    if plan is not None:
        expected = plan_fingerprint_fn(
            expand_product_sku_refs_fn(canonical_storyboard_plan_fn(plan)))
        if record.get("plan_fingerprint") != expected:
            return None
        if result.get("plan_fingerprint") != expected:
            return None
    return os.path.abspath(out_dir)
