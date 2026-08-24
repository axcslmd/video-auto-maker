#!/usr/bin/env python3
"""Pure, file-backed output contracts for multi-artifact workflow stages.

The router describes which files a stage should produce.  This module makes
that description executable at the manifest boundary so a caller cannot finish
with a convenient subset and later approve bytes that omit critical evidence.
"""
import hashlib
import json
import os

import identity_request_contract
import dialogue_timing
import audio_contract


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("STAGE_OUTPUT_JSON_INVALID: %s" % path) from exc
    if not isinstance(value, dict):
        raise ValueError("STAGE_OUTPUT_JSON_OBJECT_REQUIRED: %s" % path)
    return value


def _current_plan(manifest):
    record = manifest.get("storyboard_plan") or {}
    path = record.get("path")
    if (not path or not os.path.isfile(path) or
            _sha256_file(path) != record.get("sha256")):
        raise ValueError("MOTION_OUTPUT_AUTHORED_PLAN_STALE")
    return _load_json(path), record


def _approved_script_sha256s(manifest):
    generation = (manifest.get("generation") or {}).get("script") or {}
    records = generation.get("artifacts") or []
    return {str(item.get("sha256") or "") for item in records
            if isinstance(item, dict) and item.get("sha256")}


def _validate_dialogue_binding(manifest, plan, plan_record, binding):
    if binding.get("kind") != "approved_dialogue_motion_execution_binding":
        raise ValueError("MOTION_OUTPUT_DIALOGUE_BINDING_KIND_INVALID")
    unsigned = {key: value for key, value in binding.items()
                if key != "binding_sha256"}
    if binding.get("binding_sha256") != _json_sha256(unsigned):
        raise ValueError("MOTION_OUTPUT_DIALOGUE_BINDING_HASH_MISMATCH")
    if binding.get("authored_plan_sha256") != plan_record.get("sha256"):
        raise ValueError("MOTION_OUTPUT_DIALOGUE_BINDING_PLAN_MISMATCH")
    approved_sources = _approved_script_sha256s(manifest)
    if plan_record.get("sha256") not in approved_sources:
        raise ValueError("MOTION_OUTPUT_AUTHORED_PLAN_NOT_SCRIPT_ARTIFACT")
    director_sha = str(binding.get("director_plan_sha256") or "")
    if not director_sha or director_sha not in approved_sources:
        raise ValueError("MOTION_OUTPUT_DIRECTOR_PLAN_NOT_SCRIPT_ARTIFACT")
    guide_sha = str(binding.get("guide_sha256") or "")
    if guide_sha and guide_sha not in approved_sources:
        raise ValueError("MOTION_OUTPUT_GUIDE_NOT_SCRIPT_ARTIFACT")
    items = binding.get("items") or []
    by_id = {str(item.get("shot_id") or ""): item for item in items
             if isinstance(item, dict)}
    plan_ids = [str(shot.get("id") or "") for shot in plan.get("shots") or []]
    if (not plan_ids or any(not shot_id for shot_id in plan_ids) or
            set(plan_ids) != set(by_id) or len(items) != len(by_id)):
        raise ValueError("MOTION_OUTPUT_DIALOGUE_BINDING_SHOT_MISMATCH")
    for shot_id in plan_ids:
        dialogue = str(by_id[shot_id].get("dialogue") or "").strip()
        if (not dialogue or by_id[shot_id].get("dialogue_sha256") !=
                hashlib.sha256(dialogue.encode("utf-8")).hexdigest()):
            raise ValueError(
                "MOTION_OUTPUT_DIALOGUE_BINDING_ITEM_INVALID: %s" % shot_id)
        shot = next(item for item in plan.get("shots") or []
                    if str(item.get("id") or "") == shot_id)
        duration = shot.get("duration") or shot.get("seconds")
        if (duration is None or abs(
                float(by_id[shot_id].get("duration_seconds") or 0) -
                float(duration)) > 0.001):
            raise ValueError(
                "MOTION_OUTPUT_DIALOGUE_DURATION_MISMATCH: %s" % shot_id)
    timing_sources = {
        shot_id: {
            "dialogue": by_id[shot_id]["dialogue"],
            "duration_seconds": by_id[shot_id]["duration_seconds"],
        } for shot_id in plan_ids}
    timing = dialogue_timing.validate_contract(
        binding.get("timing_contract"), plan.get("shots") or [],
        timing_sources, plan_voice_type=binding.get("plan_voice_type"))
    dialogue_timing.assert_fits(
        timing, error_code="MOTION_OUTPUT_DIALOGUE_DURATION_OVERFLOW")
    timing_by_id = {item["shot_id"]: item for item in timing["items"]}
    for shot_id in plan_ids:
        if by_id[shot_id].get("dialogue_timing") != timing_by_id[shot_id]:
            raise ValueError(
                "MOTION_OUTPUT_DIALOGUE_TIMING_ITEM_MISMATCH: %s" % shot_id)


def _record_identity(path):
    if not os.path.isfile(path):
        raise ValueError("STAGE_OUTPUT_FILE_MISSING: %s" % path)
    return {"path": os.path.abspath(path), "sha256": _sha256_file(path)}


def validate_motion_design_outputs(manifest, paths):
    """Require the design, optional/required dialogue binding and execution plan."""
    plan, plan_record = _current_plan(manifest)
    json_items = [(os.path.abspath(path), _load_json(path)) for path in paths
                  if str(path).lower().endswith(".json")]
    bindings = [(path, value) for path, value in json_items
                if value.get("kind") ==
                "approved_dialogue_motion_execution_binding"]
    execution_plans = [(path, value) for path, value in json_items
                       if (value.get("_execution_plan") or {}).get("kind") ==
                       "motion_enriched_storyboard_plan"]
    designs = [(path, value) for path, value in json_items
               if isinstance(value.get("shots"), list) and
               path not in {item[0] for item in execution_plans} and
               value.get("kind") != "approved_dialogue_motion_execution_binding"]
    if len(designs) != 1:
        raise ValueError("MOTION_OUTPUT_EXACTLY_ONE_DESIGN_REQUIRED")
    design_path, design = designs[0]
    plan_ids = [str(shot.get("id") or "") for shot in plan.get("shots") or []]
    design_ids = [str(shot.get("shot_id") or shot.get("id") or "")
                  for shot in design.get("shots") or []]
    if (not plan_ids or any(not item for item in plan_ids) or
            plan_ids != design_ids):
        raise ValueError("MOTION_OUTPUT_DESIGN_SHOT_ORDER_MISMATCH")

    missing_authored_dialogue = any(
        not str(shot.get("dialogue") or shot.get("voiceover") or "").strip()
        for shot in plan.get("shots") or [])
    if len(bindings) > 1:
        raise ValueError("MOTION_OUTPUT_MULTIPLE_DIALOGUE_BINDINGS")
    binding = bindings[0][1] if bindings else None
    if missing_authored_dialogue and binding is None:
        raise ValueError("MOTION_OUTPUT_DIALOGUE_BINDING_ARTIFACT_REQUIRED")
    embedded_binding = design.get("dialogue_contract")
    if not isinstance(embedded_binding, dict):
        raise ValueError("MOTION_OUTPUT_DESIGN_DIALOGUE_CONTRACT_REQUIRED")
    _validate_dialogue_binding(
        manifest, plan, plan_record, embedded_binding)
    if binding is not None:
        _validate_dialogue_binding(manifest, plan, plan_record, binding)
        if embedded_binding != binding:
            raise ValueError("MOTION_OUTPUT_DESIGN_DIALOGUE_CONTRACT_MISMATCH")

    role = str(plan.get("storyboard_role") or
               (plan.get("storyboard_contract") or {}).get("kind") or "")
    profile = str((manifest.get("chain_profile") or {}).get("profile") or "")
    requires_execution = (
        profile == "document_explainer" or
        role.strip().lower().replace("-", "_") == "design_board")
    if requires_execution and len(execution_plans) != 1:
        raise ValueError("MOTION_OUTPUT_EXECUTION_PLAN_ARTIFACT_REQUIRED")
    if len(execution_plans) > 1:
        raise ValueError("MOTION_OUTPUT_MULTIPLE_EXECUTION_PLANS")
    execution_record = None
    if execution_plans:
        execution_path, execution = execution_plans[0]
        contract = execution.get("_execution_plan") or {}
        source = contract.get("source_plan") or {}
        motion = contract.get("motion_design") or {}
        design_record = _record_identity(design_path)
        if ((source.get("path"), source.get("sha256")) !=
                (plan_record.get("path"), plan_record.get("sha256")) or
                (motion.get("path"), motion.get("sha256")) !=
                (design_record["path"], design_record["sha256"])):
            raise ValueError("MOTION_OUTPUT_EXECUTION_PLAN_SOURCE_MISMATCH")
        execution_ids = [str(shot.get("id") or "")
                         for shot in execution.get("shots") or []]
        if execution_ids != plan_ids or any(
                not isinstance(shot.get("motion_plan"), dict)
                for shot in execution.get("shots") or []):
            raise ValueError("MOTION_OUTPUT_EXECUTION_PLAN_SHOT_MISMATCH")
        execution_record = _record_identity(execution_path)

    return {
        "kind": "motion_design_stage_output_contract",
        "authored_plan": {
            "path": plan_record.get("path"), "sha256": plan_record.get("sha256")},
        "motion_design": _record_identity(design_path),
        "dialogue_binding": (_record_identity(bindings[0][0]) if bindings else None),
        "dialogue_contract_sha256": _json_sha256(embedded_binding),
        "execution_plan": execution_record,
        "requires_dialogue_binding": missing_authored_dialogue,
        "requires_execution_plan": requires_execution,
    }


def validate_render_plan_outputs(manifest, paths):
    """Validate the two-artifact document plan and mandatory pre-video gates."""
    profile = str((manifest.get("chain_profile") or {}).get("profile") or "")
    json_paths = [os.path.abspath(path) for path in paths
                  if str(path).lower().endswith(".json")]
    markdown_paths = [os.path.abspath(path) for path in paths
                      if str(path).lower().endswith((".md", ".markdown"))]
    document_plans = [(path, _load_json(path)) for path in json_paths]
    document_plans = [(path, value) for path, value in document_plans
                      if value.get("kind") == "document_explainer_render_plan"]
    if profile != "document_explainer" and not document_plans:
        return None
    if len(document_plans) != 1 or len(markdown_paths) != 1:
        raise ValueError("RENDER_PLAN_EXACT_JSON_AND_MARKDOWN_REQUIRED")
    plan_path, plan = document_plans[0]
    preview_path = markdown_paths[0]
    if (plan.get("schema_version") != 2 or
            plan.get("client") != manifest.get("client") or
            str(plan.get("run_id")) != str(manifest.get("run_id")) or
            plan.get("local_only_build") is not True or
            plan.get("external_model_calls_made") is not False or
            plan.get("approval_scope") !=
            "direction_and_local_composition_not_paid_submission"):
        raise ValueError("RENDER_PLAN_DOCUMENT_IDENTITY_OR_SCOPE_INVALID")
    preview = plan.get("customer_preview") or {}
    preview_record = _record_identity(preview_path)
    unsigned_plan = {key: value for key, value in plan.items()
                     if key != "customer_preview"}
    canonical = _json_sha256(unsigned_plan)
    if (preview.get("path") != preview_record["path"] or
            preview.get("sha256") != preview_record["sha256"] or
            preview.get("canonical_plan_fingerprint") != canonical):
        raise ValueError("RENDER_PLAN_CUSTOMER_PREVIEW_BINDING_MISMATCH")
    try:
        with open(preview_path, encoding="utf-8") as handle:
            preview_text = handle.read()
    except OSError as exc:
        raise ValueError("RENDER_PLAN_CUSTOMER_PREVIEW_UNREADABLE") from exc
    if canonical not in preview_text:
        raise ValueError("RENDER_PLAN_CUSTOMER_PREVIEW_CANONICAL_HASH_MISSING")

    identity = plan.get("identity_plate_generation") or {}
    request = identity.get("request") or {}
    if (identity.get("required_before_video") is not True or
            identity.get("status") !=
            "awaiting_independent_customer_confirmation" or
            request.get("model") != "gpt-image-2" or
            request.get("ratio") != "16:9"):
        raise ValueError("RENDER_PLAN_IDENTITY_GATE_REQUIRED")
    identity_request_contract.assert_request_matches_plan(request)
    source_references = request.get("source_references") or []
    if (len(source_references) != 1 or request.get("source_fingerprint") !=
            _json_sha256({
                "prompt": request.get("prompt_zh"),
                "reference": source_references[0]})):
        raise ValueError("RENDER_PLAN_IDENTITY_SOURCE_FINGERPRINT_INVALID")
    catalog = request.get("catalog_contract") or {}
    catalog_unsigned = {key: value for key, value in catalog.items()
                        if key != "fingerprint"}
    if (not catalog_unsigned or catalog.get("fingerprint") !=
            _json_sha256(catalog_unsigned) or
            catalog.get("model") != request.get("model") or
            catalog.get("selected_ratio") != request.get("ratio") or
            catalog.get("selected_resolution") != request.get("resolution") or
            request.get("ratio") not in (catalog.get("ratios") or []) or
            request.get("resolution") not in (catalog.get("resolutions") or [])):
        raise ValueError("RENDER_PLAN_IDENTITY_CATALOG_CONTRACT_INVALID")
    gates = plan.get("pre_video_gates") or {}
    if (gates.get("identity_plate") !=
            "required_and_independently_customer_confirmed" or
            gates.get("exact_video_prompt_review") !=
            "required_after_preflight"):
        raise ValueError("RENDER_PLAN_PRE_VIDEO_GATES_REQUIRED")
    segments = plan.get("segments") or []
    if (not segments or any(
            not str(item.get("id") or "").strip() or
            not str(item.get("dialogue") or "").strip() or
            not isinstance(item.get("duration_seconds"), (int, float)) or
            float(item.get("duration_seconds") or 0) <= 0 or
            item.get("requires_identity_plate") is not True or
            item.get("video_model_usage") != "avatar_plate_only" or
            item.get("storyboard_ref") is not False
            for item in segments if isinstance(item, dict)) or
            any(not isinstance(item, dict) for item in segments)):
        raise ValueError("RENDER_PLAN_SEGMENT_IDENTITY_CONTRACT_INVALID")
    segment_ids = [str(item["id"]) for item in segments]
    if len(segment_ids) != len(set(segment_ids)):
        raise ValueError("RENDER_PLAN_SEGMENT_IDS_NOT_UNIQUE")
    timing_shots = [{
        "id": item["id"], "dialogue": item["dialogue"],
        "duration": item["duration_seconds"],
        "audio_contract": item.get("audio_contract") or {},
        "voice_type": item.get("voice_type"),
    } for item in segments]
    timing = dialogue_timing.validate_contract(
        plan.get("dialogue_timing_contract"), timing_shots,
        plan_voice_type=plan.get("dialogue_timing_voice_type"))
    dialogue_timing.assert_fits(
        timing, error_code="RENDER_PLAN_DIALOGUE_DURATION_OVERFLOW")
    timing_by_id = {item["shot_id"]: item for item in timing["items"]}
    for item in segments:
        if item.get("dialogue_timing") != timing_by_id[str(item["id"])]:
            raise ValueError(
                "RENDER_PLAN_SEGMENT_DIALOGUE_TIMING_MISMATCH: %s" %
                item["id"])
    voice = plan.get("voice_continuity_contract") or {}
    content_voice = voice.get("content_full_bleed") or {}
    if (voice.get("owner") != "video_model_integrated_audio" or
            voice.get("image_does_not_lock_voice") is not True or
            content_voice.get("audio_required") is not True or
            content_voice.get("presenter_visible") is not False or
            content_voice.get("visual_owner") != "Remotion content layer"):
        raise ValueError("RENDER_PLAN_VOICE_VISUAL_OWNERSHIP_INVALID")
    assembly = plan.get("assembly") or {}
    if (assembly.get("owner") != "video_use_bridge --formal" or
            assembly.get("timing_change_requires_reapproval") is not True):
        raise ValueError("RENDER_PLAN_ASSEMBLY_CONTRACT_INVALID")
    for label, record in (plan.get("source_contracts") or {}).items():
        if (not isinstance(record, dict) or not record.get("path") or
                _record_identity(record["path"]) != {
                    "path": os.path.abspath(record["path"]),
                    "sha256": record.get("sha256")}):
            raise ValueError("RENDER_PLAN_SOURCE_CONTRACT_STALE: %s" % label)
    if not plan.get("source_contracts"):
        raise ValueError("RENDER_PLAN_SOURCE_CONTRACTS_REQUIRED")
    return {
        "kind": "document_render_plan_stage_output_contract",
        "render_plan": _record_identity(plan_path),
        "customer_preview": preview_record,
        "canonical_plan_fingerprint": canonical,
        "identity_request_fingerprint": request.get(
            "planned_submission_request_fingerprint"),
        "source_contracts": plan.get("source_contracts"),
        "segment_ids": segment_ids,
        "dialogue_timing_contract_sha256": timing["contract_sha256"],
    }


def validate_audio_outputs(manifest, paths):
    """Require one complete, honest and customer-readable audio direction set."""
    absolute_paths = [os.path.abspath(path) for path in paths]
    json_items = [(path, _load_json(path)) for path in absolute_paths
                  if path.lower().endswith(".json")]
    contracts = [(path, value) for path, value in json_items
                 if value.get("kind") == audio_contract.CONTRACT_KIND]
    # Historical v1 contracts stay readable. New v2 transactions are strict.
    if not contracts:
        return None
    choices = [(path, value) for path, value in json_items
               if value.get("kind") == audio_contract.CHOICE_KIND]
    briefs = [(path, value) for path, value in json_items
              if value.get("kind") == audio_contract.VOICE_BRIEF_KIND]
    previews = [path for path in absolute_paths
                if path.lower().endswith((".md", ".markdown"))]
    if not (len(contracts) == len(choices) == len(briefs) == len(previews) == 1):
        raise ValueError("AUDIO_OUTPUT_EXACT_CHOICE_BRIEF_CONTRACT_PREVIEW_REQUIRED")

    choice_path, choice = choices[0]
    brief_path, brief = briefs[0]
    contract_path, contract = contracts[0]
    normalized_choice = audio_contract.normalize_choice(choice)
    if choice != normalized_choice:
        raise ValueError("AUDIO_OUTPUT_CHOICE_NOT_NORMALIZED")
    expected_brief = audio_contract.compile_voice_brief(choice)
    if brief != expected_brief or contract.get("voice_brief") != brief:
        raise ValueError("AUDIO_OUTPUT_VOICE_BRIEF_MISMATCH")
    audio_contract.validate(contract)
    master = contract.get("master_audio") or {}
    expected_contract = audio_contract.create(
        choice["mode"], voice_brief=brief,
        master_path=master.get("path") if master else None)
    if contract != expected_contract:
        raise ValueError("AUDIO_OUTPUT_CONTRACT_MISMATCH")

    expected_paths = {choice_path, brief_path, contract_path, previews[0]}
    if master:
        expected_paths.add(os.path.abspath(master["path"]))
    if set(absolute_paths) != expected_paths or len(absolute_paths) != len(expected_paths):
        raise ValueError("AUDIO_OUTPUT_UNEXPECTED_ARTIFACT")
    expected_preview = audio_contract.compile_preview(
        choice, contract,
        choice_sha256=_sha256_file(choice_path),
        voice_brief_sha256=_sha256_file(brief_path),
        contract_sha256=_sha256_file(contract_path),
        client=manifest.get("client"), run_id=manifest.get("run_id"))
    try:
        with open(previews[0], encoding="utf-8") as handle:
            preview_text = handle.read()
    except OSError as exc:
        raise ValueError("AUDIO_OUTPUT_PREVIEW_UNREADABLE") from exc
    if preview_text != expected_preview:
        raise ValueError("AUDIO_OUTPUT_PREVIEW_BINDING_MISMATCH")
    return {
        "kind": "audio_direction_stage_output_contract",
        "choice": _record_identity(choice_path),
        "voice_brief": _record_identity(brief_path),
        "audio_contract": _record_identity(contract_path),
        "customer_preview": _record_identity(previews[0]),
        "master_audio": _record_identity(master["path"]) if master else None,
        "voice_owner": audio_contract.VOICE_OWNER,
        "first_generated_audition_stage":
            audio_contract.FIRST_GENERATED_AUDITION_STAGE,
    }


def validate_stage_outputs(manifest, stage, paths):
    if stage == "audio":
        return validate_audio_outputs(manifest, paths)
    if stage == "motion_design":
        return validate_motion_design_outputs(manifest, paths)
    if stage == "render_plan":
        return validate_render_plan_outputs(manifest, paths)
    return None
