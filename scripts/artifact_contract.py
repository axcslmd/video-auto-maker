#!/usr/bin/env python3
"""Canonical fingerprints shared by storyboard, render and review handoffs."""
import hashlib
import json
import os


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_json(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path):
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_record(reference):
    if isinstance(reference, str):
        reference = {"url": reference}
    source = reference.get("url") or reference.get("source")
    record = {
        "id": reference.get("id"),
        "tag": reference.get("tag"),
        "type": reference.get("type"),
        "scope": reference.get("scope"),
        "label": reference.get("label"),
        "role": reference.get("role"),
        "intent": reference.get("intent"),
        "origin": reference.get("source"),
        "source": source,
        "sha256": file_sha256(source) if isinstance(source, str) else None,
    }
    return record


def _reference_priority(tag, reference, order):
    ref_type = (reference or {}).get("type") or ""
    type_rank = {
        "product_usage_identity": 0,
        "character_board": 1,
        "actor_board": 1,
        "cast_board": 1,
        "product_board": 2,
        "scene_board": 3,
    }.get(ref_type, 4)
    tag_rank = {
        "@usage": 0,
        "@mina": 1,
        "@host": 1,
        "@product_hero": 2,
        "@product": 2,
        "@product_angle": 3,
    }.get(tag, 4)
    return (min(type_rank, tag_rank), order)


def storyboard_image_input_tags(segment, max_reference_images=3):
    """Pick source tags for one generated panel/keyframe under image limits.

    The storyboard/contact sheet consumes one image slot, so most providers
    leave three tag-bound slots.  Selection is deterministic and audit-friendly:
    product-use relationship anchors win over extra product angles.
    """
    tags = segment.get("ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    refs = {item.get("tag"): item
            for item in (segment.get("references") or [])
            if isinstance(item, dict) and item.get("tag") in tags}
    ranked = sorted(
        ((tag, refs.get(tag), index) for index, tag in enumerate(tags)),
        key=lambda item: _reference_priority(item[0], item[1] or {}, item[2]))
    selected = [tag for tag, _ref, _index in ranked[:max_reference_images]]
    selected_set = set(selected)
    omitted = [tag for tag in tags if tag not in selected_set]
    return {"selected": selected, "omitted": omitted, "max_reference_images": max_reference_images}


def validate_storyboard_binding(segment, *, strict=False):
    """Validate the complete storyboard-to-video binding in one preflight.

    ``strict`` is used for compiled/formal segments before a paid request.  The
    returned error contains every missing or inconsistent field so an operator
    fixes the contract once instead of discovering five failures sequentially.
    """
    segment = segment or {}
    sid = segment.get("id") or "unknown"
    role = str(segment.get("generation_role") or
               (segment.get("video_model_contract") or {}).get("role") or "")
    design_board = bool(
        segment.get("design_board") or role == "avatar_plate_only" or
        segment.get("storyboard_usage") == "design_board")
    storyboard_fields = (
        "storyboard_path", "storyboard_dir", "storyboard_url",
        "storyboard_retrieve_url", "storyboard_ref_mode")
    errors = []
    if design_board:
        # video_engine owns the established avatar-plate error vocabulary;
        # keep this shared validator focused on positive storyboard bindings.
        return {"ok": True, "segment_id": sid, "design_board": True}
    elif segment.get("storyboard_ref"):
        if not strict:
            return {"ok": True, "segment_id": sid, "design_board": False}
        path = segment.get("storyboard_path")
        directory = segment.get("storyboard_dir")
        remote = segment.get("storyboard_retrieve_url") or segment.get("storyboard_url")
        panel_index = segment.get("storyboard_panel_index")
        mode = segment.get("storyboard_ref_mode")
        if not path:
            errors.append("missing_storyboard_path")
        elif not os.path.isfile(path):
            errors.append("storyboard_path_not_found")
        if not directory:
            errors.append("missing_storyboard_dir")
        elif path:
            try:
                if os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) != os.path.realpath(directory):
                    errors.append("storyboard_path_outside_storyboard_dir")
            except ValueError:
                errors.append("storyboard_path_outside_storyboard_dir")
        if not isinstance(remote, str) or not remote.startswith(("http://", "https://")):
            errors.append("missing_remote_storyboard_retrieve_url")
        if not isinstance(panel_index, int) or isinstance(panel_index, bool) or panel_index < 1:
            errors.append("invalid_storyboard_panel_index")
        if mode not in {"native_storyboard", "expanded_panel"}:
            errors.append("invalid_storyboard_ref_mode")
        tags = segment.get("ref_tags") or []
        if isinstance(tags, str):
            tags = [tags]
        refs = {
            item.get("tag"): item for item in segment.get("references") or []
            if (isinstance(item, dict) and item.get("tag") and
                item.get("type") != "storyboard_composition")
        }
        if strict and refs and not tags:
            errors.append("missing_ref_tags_for_declared_references")
        for tag in tags:
            ref = refs.get(tag)
            if ref is None:
                errors.append("unbound_ref_tag:%s" % tag)
                continue
            url = ref.get("url") or ref.get("source")
            if strict and (not isinstance(url, str) or
                           not url.startswith(("http://", "https://"))):
                errors.append("non_remote_ref_tag:%s" % tag)
    elif strict and segment.get("storyboard_usage") == "video_storyboard":
        errors.append("video_storyboard_requires_storyboard_ref")
    if errors:
        raise ValueError(
            "STORYBOARD_HANDOFF_INVALID: segment %s: %s" %
            (sid, "; ".join(errors)))
    return {"ok": True, "segment_id": sid, "design_board": design_board}


def build_storyboard_panel_recipe(segment):
    """Build the paid-request identity for an img2img storyboard panel.

    It is computable before the panel exists, so the approved contact sheet and
    exact tag-bound source bytes are part of the formal video handoff.
    """
    tags = segment.get("ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    refs = {item.get("tag"): reference_record(item)
            for item in (segment.get("references") or [])
            if isinstance(item, dict) and item.get("tag") in tags}
    budget = storyboard_image_input_tags(segment)
    storyboard_path = segment.get("storyboard_path")
    return {
        "segment_id": segment.get("id"),
        "source_shot_ids": segment.get("source_shot_ids") or [],
        "panel_mode": segment.get("storyboard_panel_mode") or "shot_representative_keyframe",
        "panel_index": segment.get("storyboard_panel_index"),
        "storyboard_path": storyboard_path,
        "storyboard_sha256": file_sha256(storyboard_path),
        "director_text_sha256": sha256_json({
            "text": segment.get("text"),
            "dialogue": segment.get("dialogue"),
            "ref_tags": list(tags),
        }),
        "ref_tags": list(tags),
        "image_input_ref_tags": budget["selected"],
        "omitted_ref_tags": budget["omitted"],
        "image_input_budget": {"storyboard_slots": 1,
                               "reference_slots": budget["max_reference_images"]},
        "references": [refs[tag] for tag in tags if tag in refs],
    }


def sequence_state_fingerprint(value):
    return sha256_json(value or {})


def build_video_handoff(segment):
    """Bind the semantic request and local artifacts handed to video generation."""
    storyboard_path = segment.get("storyboard_path")
    storyboard_sha = file_sha256(storyboard_path)
    # Segments without a storyboard reference (for example draft utility tests)
    # have no storyboard artifact contract. Once a path is declared, however,
    # it must resolve to a real file and never fingerprint as None.
    if storyboard_path and not storyboard_sha:
        raise ValueError("STALE_STORYBOARD_ARTIFACT_MISSING: %s" % storyboard_path)
    payload = {
        "schema_version": 7,
        "client": segment.get("client"),
        "run_id": segment.get("run_id"),
        "segment_id": segment.get("id"),
        "scene_id": segment.get("scene_id"),
        "source_shot_ids": segment.get("source_shot_ids") or [],
        "storyboard": {
            "path": storyboard_path,
            "dir": segment.get("storyboard_dir"),
            "url": segment.get("storyboard_url"),
            "retrieve_url": segment.get("storyboard_retrieve_url"),
            "ref_mode": segment.get("storyboard_ref_mode"),
            "sha256": storyboard_sha,
        },
        "storyboard_panel_index": segment.get("storyboard_panel_index"),
        "ref_tags": segment.get("ref_tags") or [],
        "reference_bindings": segment.get("reference_bindings") or [],
        "storyboard_panel_recipe": (build_storyboard_panel_recipe(segment)
                                    if segment.get("storyboard_ref") else None),
        "storyboard_plan_fingerprint": segment.get("storyboard_plan_fingerprint"),
        "storyboard_result_fingerprint": segment.get("storyboard_result_fingerprint"),
        "storyboard_approval": segment.get("storyboard_approval") or {},
        "references": [reference_record(item) for item in segment.get("references") or []],
        "urls": [reference_record(item) for item in segment.get("urls") or []],
        "submission_source": {
            key: segment.get(key) for key in (
                "negative_prompt", "seedance_native", "storyboard_ref",
                "use_storyboard_reference", "extend_from_previous", "extend_video",
                "product_sku", "style", "action", "visual", "camera",
                "camera_movement", "character_action", "scene_prompt",
                "continuity_in", "clothing_lock", "product_facts",
                "seed",
            ) if key in segment
        },
        "text": segment.get("text"),
        "dialogue": segment.get("dialogue"),
        "language": segment.get("language") or (segment.get("audio_contract") or {}).get("language"),
        "language_code": segment.get("language_code") or (segment.get("audio_contract") or {}).get("language_code"),
        "audio_contract": segment.get("audio_contract") or {},
        "background_identity_lock": segment.get("background_identity_lock") or {},
        "chain_required": bool(
            segment.get("chain_required") or segment.get("avatar_plate_chain") or
            segment.get("extend_video") or segment.get("extend_from_previous")),
        "timeline": segment.get("timeline") or [],
        "clip_contract": segment.get("clip_contract") or {},
        "sequence_state": segment.get("sequence_state") or {},
        "continuity_graph": segment.get("continuity_graph") or {},
        "chain_contract": segment.get("chain_contract") or {},
        "render_plan": segment.get("render_plan") or {},
        "render_plan_fingerprint": segment.get("render_plan_fingerprint"),
        "duration": segment.get("duration"),
        "ratio": segment.get("ratio"),
        "resolution": segment.get("resolution"),
        "video_type": segment.get("video_type"),
        # The selected provider route and recovery strategy materially change
        # the paid request.  They must invalidate older handoffs and prompt
        # approvals instead of being injected as mutable runtime flags.
        "model": segment.get("model"),
        "recovery_contract": segment.get("recovery_contract") or {},
        "generation_role": segment.get("generation_role") or
                           (segment.get("video_model_contract") or {}).get("role"),
        "composition_contract": segment.get("composition_contract") or {},
        "video_model_contract": segment.get("video_model_contract") or {},
        "motion_plan": segment.get("motion_plan") or {},
        "asset_slots": segment.get("asset_slots") or [],
    }
    # Optional recovery controls are canonical only when explicitly authored.
    # Otherwise adding the feature would invalidate every existing schema-v7
    # handoff even though its paid request and runtime behavior did not change.
    if (segment.get("privacy_fallback_policy") or
            segment.get("privacy_fallback_policy_contract")):
        payload["privacy_fallback_policy"] = segment.get(
            "privacy_fallback_policy")
        payload["privacy_fallback_policy_contract"] = (
            segment.get("privacy_fallback_policy_contract") or {})
    # An empirical audio canary changes formal model eligibility.  Keep it
    # optional for backward-compatible handoffs, but immutable once authored.
    if (segment.get("audio_capability_policy") or
            segment.get("audio_capability_policy_contract")):
        payload["audio_capability_policy"] = segment.get(
            "audio_capability_policy")
        payload["audio_capability_policy_contract"] = (
            segment.get("audio_capability_policy_contract") or {})
    # Once an empirical canary has passed, the proof replaces its test-only
    # audio policy.  It is part of the formal paid-request handoff so stale or
    # cross-run evidence cannot silently authorize the batch route.
    if segment.get("audio_canary_proof"):
        payload["audio_canary_proof"] = segment.get("audio_canary_proof")
    # The destination is evidence identity for a paid attempt: a recovery take
    # may never overwrite the bytes referenced by a rejected review/QC report.
    if segment.get("output_artifact_contract"):
        payload["output_artifact_contract"] = segment.get(
            "output_artifact_contract")
    return {"payload": payload, "fingerprint": sha256_json(payload)}


def build_generation_dependency(predecessor=None, *, tail_path=None,
                                extend_source=None):
    """Bind a chained paid request to the exact predecessor it consumes."""
    predecessor = predecessor or {}
    payload = {
        "predecessor_segment_id": predecessor.get("segment_id"),
        "predecessor_task_id": predecessor.get("taskId") or predecessor.get("task_id"),
        "predecessor_take_fingerprint": predecessor.get("take_fingerprint"),
        "tail_sha256": file_sha256(tail_path),
        "extend_source": extend_source,
    }
    return {"payload": payload, "fingerprint": sha256_json(payload)}


def verify_video_handoff(segment):
    expected = segment.get("video_handoff_fingerprint")
    actual = build_video_handoff(segment)["fingerprint"]
    return {"ok": bool(expected) and expected == actual,
            "expected": expected, "actual": actual,
            "strict": bool(expected)}
