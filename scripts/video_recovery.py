#!/usr/bin/env python3
"""Contract-bound recompilation for failed formal video takes."""
import copy
import json
import os
from datetime import datetime, timezone

import artifact_contract
import run_manifest as rm
import take_review
import video_prompts
from project_utils import atomic_write_json


PRIVACY_FALLBACK_STOP_POLICY = "stop_without_paid_fallback"
EMPIRICAL_AUDIO_CANARY_POLICY = "empirical_canary_unknown_output_audio"
EMPIRICAL_AUDIO_RETEST_POLICY = (
    "empirical_retest_disproved_output_audio_after_prompt_change")
EMPIRICAL_AUDIO_POLICIES = {
    EMPIRICAL_AUDIO_CANARY_POLICY, EMPIRICAL_AUDIO_RETEST_POLICY}
FORMAL_BATCH_PRIVACY_SCOPE = "formal_batch_after_approved_canary"
PERSON_REFERENCE_TYPES = {
    "character_identity", "digital_human_portrait", "presenter_identity"}


def _read_current_record(path, missing_code):
    record = rm.file_record(path)
    if not record or not rm.file_record_is_current(record):
        raise ValueError(missing_code)
    with open(record["path"], encoding="utf-8") as handle:
        return record, json.load(handle)


def _matching_failure_task(contract, manifest, current_handoff,
                           current_segments=None):
    failure = contract.get("failure") or {}
    matching = [
        task for task in (manifest.get("tasks") or [])
        if task.get("stage") == "video" and
        str(task.get("unit_id")) == str(failure.get("segment_id")) and
        str(task.get("task_id")) == str(failure.get("task_id")) and
        int(task.get("attempt") or 1) == int(failure.get("attempt") or 1)]
    if not matching:
        raise ValueError("VIDEO_RECOVERY_FAILURE_EVIDENCE_REQUIRED")
    task = matching[-1]
    if task.get("status") in ("failed", "fallback_approval_required"):
        return task
    if (task.get("status") != "succeeded" or
            failure.get("status") != "media_qc_failed"):
        raise ValueError("VIDEO_RECOVERY_FAILURE_EVIDENCE_REQUIRED")
    review_record = failure.get("rejected_take_review") or {}
    qc_record = failure.get("media_qc_report") or {}
    if (not rm.file_record_is_current(review_record) or
            not rm.file_record_is_current(qc_record)):
        raise ValueError("VIDEO_RECOVERY_REJECTED_TAKE_EVIDENCE_STALE")
    with open(review_record["path"], encoding="utf-8") as handle:
        review = json.load(handle)
    with open(qc_record["path"], encoding="utf-8") as handle:
        qc = json.load(handle)
    artifact = review.get("artifact") or {}
    segment_id = str(failure.get("segment_id"))
    expected_handoff = (current_handoff.get("segments") or {}).get(segment_id)
    authorized = (manifest.get("video_attempts") or {}).get(segment_id) or {}
    # A recovery canary may already have an attempt-specific output bound when
    # a later local guard discovers that the provider needs a different
    # supported input mode (for example avatar plate type 2 -> type 5).  Before
    # any provider task exists, allow the same confirmed recovery/model to be
    # recompiled while validating the rejected take against the immutable
    # previous-take handoff stored in the output isolation contract.
    current_segment = next((
        item for item in (current_segments or [])
        if isinstance(item, dict) and str(item.get("id")) == segment_id), None)
    output_contract = ((current_segment or {}).get(
        "output_artifact_contract") or {})
    segment_recovery = ((current_segment or {}).get("recovery_contract") or {})
    current_attempt = int(authorized.get("attempt") or 0)
    current_attempt_tasks = [
        item for item in (manifest.get("tasks") or [])
        if item.get("stage") == "video" and
        str(item.get("unit_id")) == segment_id and
        int(item.get("attempt") or 1) == current_attempt]
    pending_reconfigure = bool(
        current_segment and
        current_segment.get("video_handoff_fingerprint") == expected_handoff and
        segment_recovery.get("recovery_id") == contract.get("recovery_id") and
        segment_recovery.get("target_model") ==
        (contract.get("strategy") or {}).get("target_model") and
        output_contract.get("mode") == "attempt_specific_no_overwrite" and
        int(output_contract.get("attempt") or 0) == current_attempt and
        output_contract.get("previous_take_fingerprint") ==
        failure.get("take_fingerprint") and
        output_contract.get("previous_take_handoff_fingerprint") and
        not os.path.exists(str(output_contract.get("path") or "")) and
        not current_attempt_tasks)
    if pending_reconfigure:
        expected_handoff = output_contract[
            "previous_take_handoff_fingerprint"]
    if (review.get("decision", {}).get("verdict") != "rejected" or
            str(review.get("segment_id")) != segment_id or
            str(artifact.get("task_id")) != str(failure.get("task_id")) or
            artifact.get("video_handoff_fingerprint") != expected_handoff or
            artifact.get("take_fingerprint") != failure.get("take_fingerprint") or
            str(artifact.get("model_used")) != str(task.get("model")) or
            not take_review.artifact_is_current(review) or
            not take_review.artifact_fingerprint_is_current(review) or
            qc.get("passed") is not False or
            (qc.get("file") or {}).get("sha256") != artifact.get("sha256") or
            authorized.get("previous_take_fingerprint") !=
            artifact.get("take_fingerprint") or
            int(authorized.get("attempt") or 1) <=
            int(failure.get("attempt") or 1)):
        raise ValueError("VIDEO_RECOVERY_REJECTED_TAKE_EVIDENCE_INVALID")
    return task


def _validate_recovery_contract_identity(contract, manifest):
    if (contract.get("kind") != "video_recovery_contract" or
            int(contract.get("schema_version") or 0) != 1 or
            str(contract.get("client")) != str(manifest.get("client")) or
            str(contract.get("run_id")) != str(manifest.get("run_id")) or
            contract.get("confirmed_by_customer") is not True or
            contract.get("confirmation_source") != "customer_dialogue" or
            not str(contract.get("customer_confirmation") or "").strip()):
        raise ValueError("VIDEO_RECOVERY_CONTRACT_IDENTITY_MISMATCH")


def _validate_recovery_source(segments_spec, contract, manifest):
    current_handoff = (manifest.get("handoffs") or {}).get("video") or {}
    source = contract.get("source") or {}
    current_file = current_handoff.get("file") or {}
    if (source.get("handoff_sha256") != current_handoff.get("sha256") or
            source.get("segments_sha256") != current_file.get("sha256") or
            not rm.file_record_is_current(current_file)):
        raise ValueError("VIDEO_RECOVERY_SOURCE_HANDOFF_STALE")
    candidate_fingerprints = {
        str(segment.get("id")): segment.get("video_handoff_fingerprint")
        for segment in (segments_spec.get("segments") or [])
        if isinstance(segment, dict)}
    if candidate_fingerprints != {
            str(key): value for key, value in
            (current_handoff.get("segments") or {}).items()}:
        raise ValueError("VIDEO_RECOVERY_SOURCE_SEGMENTS_MISMATCH")
    _matching_failure_task(
        contract, manifest, current_handoff,
        current_segments=segments_spec.get("segments") or [])
    return source


def _recovery_strategy_config(contract):
    strategy = contract.get("strategy") or {}
    if (strategy.get("mode") != "independent_identity_anchor" or
            strategy.get("preserve_confirmed_identity_reference") is not True or
            strategy.get("tail_chain") is not False):
        raise ValueError("VIDEO_RECOVERY_STRATEGY_UNSUPPORTED")
    canary_id = str(strategy.get("canary_segment_id") or "").strip()
    if (strategy.get("require_canary_before_batch") is True and not canary_id):
        raise ValueError("VIDEO_RECOVERY_CANARY_SEGMENT_REQUIRED")
    target_model = str(strategy.get("target_model") or "").strip()
    if not target_model:
        raise ValueError("VIDEO_RECOVERY_TARGET_MODEL_REQUIRED")
    audio_policy = str(strategy.get("audio_capability_policy") or "").strip()
    if audio_policy and (
            audio_policy not in EMPIRICAL_AUDIO_POLICIES or
            strategy.get("require_canary_before_batch") is not True):
        raise ValueError("VIDEO_RECOVERY_AUDIO_CAPABILITY_POLICY_UNSUPPORTED")
    target_resolution = str(strategy.get("resolution") or "").strip()
    target_ratio = str(strategy.get("output_ratio") or "").strip()
    target_video_type = strategy.get("video_type")
    if target_video_type not in (None, ""):
        try:
            target_video_type = int(target_video_type)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "VIDEO_RECOVERY_VIDEO_TYPE_INVALID") from exc
        if target_video_type not in {1, 2, 3, 4, 5}:
            raise ValueError("VIDEO_RECOVERY_VIDEO_TYPE_INVALID")
    return {
        "strategy": strategy,
        "canary_id": canary_id,
        "target_model": target_model,
        "audio_policy": audio_policy,
        "target_resolution": target_resolution,
        "target_ratio": target_ratio,
        "target_video_type": target_video_type,
    }


def _recovery_model_hooks(pick_video_model, validate_empirical_audio_route,
                          audio_policy):
    if pick_video_model is None:
        import video_engine
        pick_video_model = video_engine._pick_video_model
        validate_empirical_audio_route = (
            validate_empirical_audio_route or
            video_engine._validate_empirical_audio_route)
    if audio_policy and validate_empirical_audio_route is None:
        raise ValueError("VIDEO_RECOVERY_AUDIO_CAPABILITY_VALIDATOR_REQUIRED")
    return pick_video_model, validate_empirical_audio_route


def _validate_recovery_identity_reference(source_segment):
    urls = list(source_segment.get("urls") or [])
    identity_refs = [
        ref for ref in (source_segment.get("references") or [])
        if isinstance(ref, dict) and
        ref.get("type") in PERSON_REFERENCE_TYPES and
        ref.get("url") in urls]
    if len(urls) != 1 or len(identity_refs) != 1:
        raise ValueError(
            "VIDEO_RECOVERY_IDENTITY_REFERENCE_MUST_BE_PRESERVED: %s" %
            source_segment.get("id"))


def _select_recovery_model(source_segment, config, pick_video_model,
                           validate_empirical_audio_route):
    target_model = config["target_model"]
    audio_policy = config["audio_policy"]
    dialogue = ((source_segment.get("audio_contract") or {}).get("dialogue")
                or source_segment.get("dialogue"))
    selection_segment = copy.deepcopy(source_segment)
    selection_segment["model"] = target_model
    target_video_type = config["target_video_type"]
    effective_video_type = (
        target_video_type if target_video_type is not None else
        source_segment.get("video_type"))
    selection_segment["video_type"] = effective_video_type
    if config["target_resolution"]:
        selection_segment["resolution"] = config["target_resolution"]
    if config["target_ratio"]:
        selection_segment["ratio"] = config["target_ratio"]
    selected = (
        validate_empirical_audio_route(
            selection_segment,
            allow_disproved=(
                audio_policy == EMPIRICAL_AUDIO_RETEST_POLICY))
        if audio_policy else
        pick_video_model(
            target_model, video_type=effective_video_type,
            dialogue=dialogue, formal=True, allow_fallback=False,
            reference_count=1))
    if selected != target_model:
        raise ValueError(
            "VIDEO_RECOVERY_TARGET_MODEL_CANONICAL_MISMATCH: %s != %s" %
            (target_model, selected))
    return selected, effective_video_type


def _apply_recovery_audio_policy(updated, record, contract, config):
    audio_policy = config["audio_policy"]
    canary_id = config["canary_id"]
    if audio_policy and str(updated.get("id") or "") == canary_id:
        updated["audio_capability_policy"] = audio_policy
        updated["audio_capability_policy_contract"] = {
            "file": record,
            "policy": audio_policy,
            "scope": "formal_test_segment_only",
            "segment_id": str(updated.get("id") or ""),
            "recovery_id": contract.get("recovery_id"),
        }
        return
    updated.pop("audio_capability_policy", None)
    updated.pop("audio_capability_policy_contract", None)


def _build_recovery_segment(source_segment, record, contract, source, config,
                            selected, effective_video_type):
    strategy = config["strategy"]
    updated = copy.deepcopy(source_segment)
    updated.update({
        "model": selected,
        "video_type": effective_video_type,
        "chain_required": False,
        "chain_contract": {},
        "avatar_plate_chain": False,
        "extend_video": False,
        "extend_from_previous": False,
        "take_review_required": True,
        "continuity_in": (
            "独立生成；使用同一张已确认身份与背景锚，保持人物、服装、机位、"
            "灯光、色温、声音人设和 BGM 一致。"),
        "continuity_out": (
            "以自然静止动作结束，供本地确定性剪辑；不依赖上一段尾帧。"),
        "recovery_contract": {
            "path": record["path"],
            "sha256": record["sha256"],
            "recovery_id": contract.get("recovery_id"),
            "mode": strategy.get("mode"),
            "source_handoff_sha256": source.get("handoff_sha256"),
            "failure_task_id": (contract.get("failure") or {}).get(
                "task_id"),
            "target_model": selected,
            "target_video_type": effective_video_type,
        },
    })
    if config["target_resolution"]:
        updated["resolution"] = config["target_resolution"]
        updated["recovery_contract"]["target_resolution"] = (
            config["target_resolution"])
    if config["target_ratio"]:
        updated["ratio"] = config["target_ratio"]
    if config["audio_policy"]:
        updated["recovery_contract"]["audio_capability_policy"] = (
            config["audio_policy"])
    _apply_recovery_audio_policy(updated, record, contract, config)
    clip = ((updated.get("clip_contract") or {}).get("scopes") or {}).get(
        "clip")
    if isinstance(clip, dict):
        clip["continuation_mode"] = "fresh_segment_same_identity_anchor"
    return updated


def _refresh_attempt_output_contract(updated, selected, contract, manifest):
    output_contract = copy.deepcopy(
        updated.get("output_artifact_contract") or {})
    segment_id = str(updated.get("id") or "")
    authorized = ((manifest.get("video_attempts") or {}).get(segment_id) or {})
    current_attempt = int(authorized.get("attempt") or 0)
    current_attempt_tasks = [
        task for task in (manifest.get("tasks") or [])
        if task.get("stage") == "video" and
        str(task.get("unit_id")) == segment_id and
        int(task.get("attempt") or 1) == current_attempt]
    if (output_contract.get("mode") != "attempt_specific_no_overwrite" or
            int(output_contract.get("attempt") or 0) != current_attempt or
            output_contract.get("previous_take_fingerprint") !=
            (contract.get("failure") or {}).get("take_fingerprint") or
            output_contract.get("recovery_id") !=
            contract.get("recovery_id") or
            os.path.exists(str(output_contract.get("path") or "")) or
            current_attempt_tasks):
        return
    # Keep the already-authorized attempt/output path, but refresh the source
    # handoff proof after a pre-submit provider input-mode correction changes
    # fields included in the handoff fingerprint.
    base_segment = copy.deepcopy(updated)
    source_output = copy.deepcopy(
        output_contract.get("source_output_artifact_contract") or {})
    if source_output:
        base_segment["output_artifact_contract"] = source_output
    else:
        base_segment.pop("output_artifact_contract", None)
    base_segment["out_path"] = output_contract.get("source_out_path")
    base_segment.pop("video_handoff_fingerprint", None)
    output_contract["source_video_handoff_fingerprint"] = (
        artifact_contract.build_video_handoff(base_segment)["fingerprint"])
    output_contract["target_model"] = selected
    updated["output_artifact_contract"] = output_contract


def _compile_recovery_segment(source_segment, record, contract, source,
                              config, manifest, pick_video_model,
                              validate_empirical_audio_route):
    _validate_recovery_identity_reference(source_segment)
    selected, effective_video_type = _select_recovery_model(
        source_segment, config, pick_video_model,
        validate_empirical_audio_route)
    updated = _build_recovery_segment(
        source_segment, record, contract, source, config, selected,
        effective_video_type)
    _refresh_attempt_output_contract(updated, selected, contract, manifest)
    updated["video_handoff_fingerprint"] = (
        artifact_contract.build_video_handoff(updated)["fingerprint"])
    return updated


def apply_contract(segments_spec, contract_path, manifest,
                   *, pick_video_model=None,
                   validate_empirical_audio_route=None):
    """Recompile a rejected take to one exact model and canary strategy."""
    record, contract = _read_current_record(
        contract_path, "VIDEO_RECOVERY_CONTRACT_REQUIRED")
    _validate_recovery_contract_identity(contract, manifest)
    source = _validate_recovery_source(segments_spec, contract, manifest)
    config = _recovery_strategy_config(contract)
    pick_video_model, validate_empirical_audio_route = _recovery_model_hooks(
        pick_video_model, validate_empirical_audio_route,
        config["audio_policy"])

    compiled = [
        _compile_recovery_segment(
            source_segment, record, contract, source, config, manifest,
            pick_video_model, validate_empirical_audio_route)
        for source_segment in (segments_spec.get("segments") or [])]
    strategy = config["strategy"]
    if (not compiled or
            (strategy.get("require_canary_before_batch") is True and
             config["canary_id"] not in {
                 str(item.get("id")) for item in compiled})):
        raise ValueError("VIDEO_RECOVERY_CANARY_SEGMENT_REQUIRED")
    output = copy.deepcopy(segments_spec)
    output["segments"] = compiled
    output["video_recovery_contract"] = {
        "file": record, "recovery_id": contract.get("recovery_id"),
        "source": source, "failure": contract.get("failure") or {},
        "strategy": strategy,
        "customer_confirmation": contract.get("customer_confirmation"),
        "confirmation_source": contract.get("confirmation_source"),
    }
    return output


def bind_privacy_policy(manifest, manifest_path, segments_path,
                        policy_contract_path, *, commit_handoff):
    """Atomically bind a stop-only policy to the current recovery canary."""
    segments_record, spec = _read_current_record(
        segments_path, "RECOVERY_PRIVACY_POLICY_INPUT_STALE")
    policy_record, policy = _read_current_record(
        policy_contract_path, "RECOVERY_PRIVACY_POLICY_INPUT_STALE")
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    canary_id = str(requirement.get("segment_id") or "")
    current_fp = (handoff.get("segments") or {}).get(canary_id)
    if (policy.get("kind") != "privacy_fallback_policy_contract" or
            policy.get("do_not_bind") is True or
            str(policy.get("status") or "").startswith("superseded") or
            int(policy.get("schema_version") or 0) != 1 or
            policy.get("policy") != PRIVACY_FALLBACK_STOP_POLICY or
            policy.get("scope") != "formal_test_segment_only" or
            str(policy.get("client")) != str(manifest.get("client")) or
            str(policy.get("run_id")) != str(manifest.get("run_id")) or
            str(policy.get("segment_id")) != canary_id or
            policy.get("recovery_id") != requirement.get("recovery_id") or
            policy.get("source_video_handoff_fingerprint") != current_fp or
            policy.get("source_segments_sha256") != segments_record.get("sha256") or
            policy.get("source_aggregate_handoff_sha256") != handoff.get("sha256") or
            policy.get("confirmed_by_customer") is not True or
            policy.get("confirmation_source") != "customer_dialogue" or
            not str(policy.get("customer_confirmation") or "").strip() or
            manifest.get("requires_test_segment") is not True or
            not rm.recovery_test_segment_requirement_is_current(manifest) or
            (handoff.get("file") or {}).get("sha256") !=
            segments_record.get("sha256")):
        raise ValueError("RECOVERY_PRIVACY_POLICY_CONTRACT_INVALID")
    updated_segments, matched = [], 0
    for original in spec.get("segments") or []:
        segment = copy.deepcopy(original)
        if str(segment.get("id") or "") == canary_id:
            recovery = segment.get("recovery_contract") or {}
            if (recovery.get("recovery_id") != requirement.get("recovery_id") or
                    segment.get("video_handoff_fingerprint") != current_fp or
                    artifact_contract.build_video_handoff(
                        segment)["fingerprint"] != current_fp):
                raise ValueError("RECOVERY_PRIVACY_POLICY_CANARY_STALE")
            segment["privacy_fallback_policy"] = PRIVACY_FALLBACK_STOP_POLICY
            segment["privacy_fallback_policy_contract"] = {
                "file": policy_record, "policy": policy.get("policy"),
                "scope": policy.get("scope"), "segment_id": canary_id,
                "recovery_id": policy.get("recovery_id"),
                "source_video_handoff_fingerprint": current_fp,
            }
            segment["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(segment)["fingerprint"])
            matched += 1
        else:
            segment.pop("privacy_fallback_policy", None)
            segment.pop("privacy_fallback_policy_contract", None)
        updated_segments.append(segment)
    if matched != 1:
        raise ValueError("RECOVERY_PRIVACY_POLICY_CANARY_REQUIRED")
    updated = copy.deepcopy(spec)
    updated["segments"] = updated_segments
    updated["privacy_fallback_policy_contract"] = {
        "file": policy_record, "policy": policy.get("policy"),
        "scope": policy.get("scope"), "segment_id": canary_id,
        "recovery_id": policy.get("recovery_id"),
    }
    commit_handoff(manifest, manifest_path, segments_path, updated)
    return updated


def finalize_privacy_policy_contract(manifest, segments_path, template_path,
                                     out_path):
    """Derive a bindable stop-policy from the post-recovery handoff.

    Recovery compilation changes every segment fingerprint, the segments file
    SHA and the aggregate handoff SHA.  Customer approval is therefore stored
    as a policy template; this finalizer derives all byte identities only after
    the recovered handoff has been committed.
    """
    segments_record, spec = _read_current_record(
        segments_path, "RECOVERY_PRIVACY_POLICY_FINALIZE_SOURCE_STALE")
    template_record, template = _read_current_record(
        template_path, "RECOVERY_PRIVACY_POLICY_FINALIZE_SOURCE_STALE")
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    handoff_file = handoff.get("file") or {}
    if (not rm.file_record_is_current(handoff_file) or
            handoff_file.get("sha256") != segments_record.get("sha256") or
            not rm.recovery_test_segment_requirement_is_current(manifest)):
        raise ValueError("RECOVERY_PRIVACY_POLICY_FINALIZE_SOURCE_STALE")

    canary_id = str(requirement.get("segment_id") or "").strip()
    matches = [
        segment for segment in (spec.get("segments") or [])
        if isinstance(segment, dict) and
        str(segment.get("id") or "") == canary_id]
    if len(matches) != 1:
        raise ValueError("RECOVERY_PRIVACY_POLICY_CANARY_REQUIRED")
    canary = matches[0]
    current_fp = (handoff.get("segments") or {}).get(canary_id)
    recovery = canary.get("recovery_contract") or {}
    recovery_file = requirement.get("contract") or {}
    if (not current_fp or
            canary.get("video_handoff_fingerprint") != current_fp or
            artifact_contract.build_video_handoff(
                canary)["fingerprint"] != current_fp or
            recovery.get("recovery_id") != requirement.get("recovery_id") or
            recovery.get("path") != recovery_file.get("path") or
            recovery.get("sha256") != recovery_file.get("sha256")):
        raise ValueError("RECOVERY_PRIVACY_POLICY_CANARY_STALE")

    if (template.get("kind") != "privacy_fallback_policy_template" or
            template.get("template_status") !=
            "awaiting_post_recovery_handoff_binding" or
            int(template.get("schema_version") or 0) != 1 or
            template.get("policy") != PRIVACY_FALLBACK_STOP_POLICY or
            template.get("scope") != "formal_test_segment_only" or
            str(template.get("client")) != str(manifest.get("client")) or
            str(template.get("run_id")) != str(manifest.get("run_id")) or
            str(template.get("segment_id")) != canary_id or
            template.get("recovery_id") != requirement.get("recovery_id") or
            str(template.get("target_model") or "") !=
            str(canary.get("model") or "") or
            template.get("confirmed_by_customer") is not True or
            template.get("confirmation_source") != "customer_dialogue" or
            not str(template.get("customer_confirmation") or "").strip() or
            not str(template.get("action") or "").strip()):
        raise ValueError("RECOVERY_PRIVACY_POLICY_TEMPLATE_INVALID")

    out_path = os.path.abspath(out_path)
    run_dir = os.path.dirname(segments_record["path"])
    try:
        outside_run = os.path.commonpath((run_dir, out_path)) != run_dir
    except ValueError as exc:
        raise ValueError(
            "RECOVERY_PRIVACY_POLICY_OUTPUT_OUTSIDE_RUN") from exc
    if (out_path == template_record["path"] or outside_run or
            os.path.islink(out_path)):
        raise ValueError("RECOVERY_PRIVACY_POLICY_OUTPUT_OUTSIDE_RUN")

    contract = {
        "schema_version": 1,
        "kind": "privacy_fallback_policy_contract",
        "policy": template["policy"],
        "scope": template["scope"],
        "client": template["client"],
        "run_id": template["run_id"],
        "segment_id": canary_id,
        "recovery_id": template["recovery_id"],
        "source_video_handoff_fingerprint": current_fp,
        "source_segments_sha256": segments_record["sha256"],
        "source_aggregate_handoff_sha256": handoff.get("sha256"),
        "target_model": template["target_model"],
        "action": template["action"],
        "confirmed_by_customer": True,
        "confirmation_source": "customer_dialogue",
        "customer_confirmation": template["customer_confirmation"],
        "template": template_record,
    }

    # Recheck both sources immediately before publication so a concurrent
    # mutation cannot produce a contract for bytes that were never current at
    # the same time.
    if (not rm.file_record_is_current(segments_record) or
            not rm.file_record_is_current(template_record) or
            not rm.recovery_test_segment_requirement_is_current(manifest)):
        raise ValueError("RECOVERY_PRIVACY_POLICY_FINALIZE_SOURCE_STALE")
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as handle:
            existing = json.load(handle)
        if existing != contract:
            raise ValueError("RECOVERY_PRIVACY_POLICY_OUTPUT_CONFLICT")
    else:
        atomic_write_json(out_path, contract)
    record = rm.file_record(out_path)
    if not record or not rm.file_record_is_current(record):
        raise ValueError("RECOVERY_PRIVACY_POLICY_OUTPUT_STALE")
    return record


def _segment_requires_embedded_audio(segment):
    audio = segment.get("audio_contract") or {}
    dialogue = audio.get("dialogue") or segment.get("dialogue")
    return bool(str(dialogue or "").strip() or
                audio.get("track") == "required" or
                any(audio.get(key) not in (None, "", False)
                    for key in ("voice", "bgm", "sfx", "lip_sync")))


def _provider_request_identity(segment):
    """Compare request semantics while excluding local authorization metadata."""
    normalized = copy.deepcopy(segment)
    for key in (
            "video_handoff_fingerprint", "privacy_fallback_policy",
            "privacy_fallback_policy_contract", "audio_capability_policy",
            "audio_capability_policy_contract", "audio_canary_proof",
            "output_artifact_contract"):
        normalized.pop(key, None)
    return video_prompts._submission_source_fingerprint(normalized)


def _approved_canary_task(manifest, artifact):
    matches = [
        task for task in (manifest.get("tasks") or [])
        if task.get("stage") == "video" and
        str(task.get("unit_id") or "") == str(
            artifact.get("segment_id") or "") and
        task.get("handoff_fingerprint") ==
        artifact.get("video_handoff_fingerprint") and
        task.get("model") == artifact.get("model_used") and
        task.get("status") == "succeeded" and
        task.get("media_qc_failed") is not True and
        task.get("task_id") and
        task.get("take_fingerprint") == artifact.get("take_fingerprint")]
    if len(matches) != 1:
        raise ValueError("AUDIO_CANARY_EXACT_SUCCEEDED_TASK_REQUIRED")
    return matches[0]


def _build_audio_canary_proof(manifest, artifact, handoff, canary,
                              canary_task):
    proof = {
        "kind": "approved_empirical_audio_canary",
        "schema_version": 1,
        "status": "fulfilled",
        "client": manifest.get("client"),
        "run_id": manifest.get("run_id"),
        "segment_id": artifact.get("segment_id"),
        "model_used": artifact.get("model_used"),
        "take_fingerprint": artifact.get("take_fingerprint"),
        "task_id": canary_task.get("task_id"),
        "attempt": int(canary_task.get("attempt") or 1),
        "source_video_handoff_fingerprint":
            artifact.get("video_handoff_fingerprint"),
        "source_aggregate_handoff_sha256": handoff.get("sha256"),
        "provider_request_identity": _provider_request_identity(canary),
        "media_qc_sha256": artifact_contract.sha256_json(
            artifact.get("media_qc") or {}),
        "results": copy.deepcopy(artifact.get("results") or {}),
        "video": copy.deepcopy(artifact.get("video") or {}),
        "review": copy.deepcopy(artifact.get("review") or {}),
        "privacy_fallback_action": PRIVACY_FALLBACK_STOP_POLICY,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
    proof["fingerprint"] = artifact_contract.sha256_json(proof)
    return proof


def _completed_canary_output_contract(canary, artifact, canary_task, proof):
    """Retire the generation-time no-overwrite guard into exact reuse proof."""
    source = canary.get("output_artifact_contract") or {}
    if not source:
        return None
    video = artifact.get("video") or {}
    path = os.path.abspath(str(source.get("path") or ""))
    if (source.get("mode") != "attempt_specific_no_overwrite" or
            not path or path != os.path.abspath(str(video.get("path") or "")) or
            not rm.file_record_is_current(video) or
            artifact_contract.file_sha256(path) != video.get("sha256") or
            int(source.get("attempt") or 0) !=
            int(canary_task.get("attempt") or 1) or
            source.get("target_model") != artifact.get("model_used")):
        raise ValueError("AUDIO_CANARY_OUTPUT_REUSE_SOURCE_STALE")
    prior_path = str(source.get("prior_take_path") or "").strip()
    if (prior_path and source.get("prior_take_sha256") !=
            artifact_contract.file_sha256(os.path.abspath(prior_path))):
        raise ValueError("AUDIO_CANARY_OUTPUT_REUSE_SOURCE_STALE")
    return {
        "mode": "approved_canary_exact_media_reuse",
        "path": path,
        "sha256": video.get("sha256"),
        "task_id": canary_task.get("task_id"),
        "attempt": int(canary_task.get("attempt") or 1),
        "take_fingerprint": artifact.get("take_fingerprint"),
        "source_video_handoff_fingerprint": artifact.get(
            "video_handoff_fingerprint"),
        "audio_canary_proof_fingerprint": proof.get("fingerprint"),
        "source_output_artifact_contract": copy.deepcopy(source),
    }


def _promote_canary_manifest(candidate, previous, *, proof, source_handoff,
                             canary_task, old_segments):
    artifact = candidate.get("test_segment_artifact") or {}
    new_handoff = (candidate.get("handoffs") or {}).get("video") or {}
    segment_id = str(artifact.get("segment_id") or "")
    target_fp = (new_handoff.get("segments") or {}).get(segment_id)
    if (candidate.get("recovery_test_segment_requirement") or
            not target_fp or
            artifact.get("video_handoff_fingerprint") !=
            (source_handoff.get("segments") or {}).get(segment_id)):
        raise ValueError("AUDIO_CANARY_PROMOTION_HANDOFF_MISMATCH")
    promoted_task = copy.deepcopy(canary_task)
    promoted_task.pop("task_key", None)
    promoted_task.update({
        "handoff_fingerprint": target_fp,
        "promoted_from_handoff": artifact.get(
            "video_handoff_fingerprint"),
        "promotion_proof_fingerprint": proof["fingerprint"],
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    })
    promoted_task = rm.upsert_task(candidate, promoted_task)
    artifact["formal_batch_promotion"] = {
        "kind": "audio_canary_formal_batch_promotion",
        "schema_version": 1,
        "status": "fulfilled",
        "segment_id": segment_id,
        "model_used": artifact.get("model_used"),
        "take_fingerprint": artifact.get("take_fingerprint"),
        "audio_canary_proof_fingerprint": proof["fingerprint"],
        "audio_canary_proof": copy.deepcopy(proof),
        "source": {
            "video_handoff_fingerprint": artifact.get(
                "video_handoff_fingerprint"),
            "aggregate_handoff_sha256": source_handoff.get("sha256"),
            "segments": copy.deepcopy(old_segments),
            "segments_file": copy.deepcopy(source_handoff.get("file") or {}),
        },
        "target": {
            "video_handoff_fingerprint": target_fp,
            "aggregate_handoff_sha256": new_handoff.get("sha256"),
            "segments": copy.deepcopy(new_handoff.get("segments") or {}),
            "segments_file": copy.deepcopy(new_handoff.get("file") or {}),
        },
        "promoted_task": {
            key: promoted_task.get(key) for key in (
                "task_id", "attempt", "model", "handoff_fingerprint",
                "promoted_from_handoff", "promotion_proof_fingerprint")
        },
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
    candidate["status"] = "needs_video_prompt_review"


def promote_audio_canary_to_batch(manifest, manifest_path, segments_path, *,
                                  commit_handoff):
    """Atomically retire test-only policy and authorize the exact formal batch.

    The transition reuses the already-paid canary task only when the provider
    request semantics are unchanged.  The changed handoff deliberately
    invalidates the old full-batch prompt review.
    """
    segments_record, spec = _read_current_record(
        segments_path, "AUDIO_CANARY_PROMOTION_SOURCE_STALE")
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    artifact = manifest.get("test_segment_artifact") or {}
    existing = artifact.get("formal_batch_promotion") or {}
    if existing.get("status") == "fulfilled":
        try:
            rm.validate_test_segment_closure(manifest)
            return {"already_promoted": True, "segments": spec,
                    "promotion": existing}
        except ValueError as exc:
            if str(exc) != "TEST_SEGMENT_HANDOFF_STALE":
                raise

        # A later, explicitly-authorized retry may change only another
        # segment's attempt-specific output path.  That legitimately changes
        # the aggregate handoff and the promotion target file hash, while the
        # already-paid canary bytes, provider request, promoted task and proof
        # remain identical.  Refresh only those aggregate target fields and
        # let the full closure validator prove that the canary itself did not
        # change.  This keeps retries fail-closed without forcing a second
        # paid audio canary merely because shot_N received a new output path.
        candidate = copy.deepcopy(manifest)
        candidate_artifact = candidate.get("test_segment_artifact") or {}
        candidate_promotion = copy.deepcopy(
            candidate_artifact.get("formal_batch_promotion") or {})
        segment_id = str(candidate_artifact.get("segment_id") or "")
        expected_handoff = (handoff.get("segments") or {}).get(segment_id)
        if (not expected_handoff or
                not rm.file_record_is_current(handoff.get("file") or {}) or
                (handoff.get("file") or {}).get("sha256") !=
                segments_record.get("sha256")):
            raise ValueError("AUDIO_CANARY_PROMOTION_REFRESH_SOURCE_STALE")
        candidate_promotion["target"] = {
            "video_handoff_fingerprint": expected_handoff,
            "aggregate_handoff_sha256": handoff.get("sha256"),
            "segments": copy.deepcopy(handoff.get("segments") or {}),
            "segments_file": copy.deepcopy(handoff.get("file") or {}),
        }
        candidate_artifact["formal_batch_promotion"] = candidate_promotion
        rm.validate_test_segment_closure(candidate)

        def refresh_target(current, _previous):
            refreshed_artifact = current.get("test_segment_artifact") or {}
            refreshed_artifact["formal_batch_promotion"] = copy.deepcopy(
                candidate_promotion)

        commit_handoff(
            manifest, manifest_path, segments_path, spec,
            manifest_mutator=refresh_target)
        rm.validate_test_segment_closure(manifest)
        return {"already_promoted": True, "refreshed": True,
                "segments": spec,
                "promotion": (manifest.get("test_segment_artifact") or {}).get(
                    "formal_batch_promotion") or {}}
    if (not rm.file_record_is_current(handoff.get("file") or {}) or
            (handoff.get("file") or {}).get("sha256") !=
            segments_record.get("sha256") or
            not rm.approval_is_current(manifest, "test_segment")):
        raise ValueError("AUDIO_CANARY_PROMOTION_SOURCE_STALE")
    rm.validate_test_segment_closure(manifest)
    artifact_policy = artifact.get("audio_capability_policy")
    if (artifact.get("status") != "approved" or
            artifact_policy not in EMPIRICAL_AUDIO_POLICIES):
        raise ValueError("APPROVED_EMPIRICAL_AUDIO_CANARY_REQUIRED")
    recovery = spec.get("video_recovery_contract") or {}
    strategy = recovery.get("strategy") or {}
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    canary_id = str(artifact.get("segment_id") or "")
    if (strategy.get("require_canary_before_batch") is not True or
            str(strategy.get("canary_segment_id") or "") != canary_id or
            not rm.recovery_test_segment_requirement_is_current(manifest)):
        raise ValueError("AUDIO_CANARY_PROMOTION_REQUIREMENT_STALE")
    by_id = {str(item.get("id") or ""): item
             for item in (spec.get("segments") or [])
             if isinstance(item, dict)}
    canary = by_id.get(canary_id) or {}
    current_fp = (handoff.get("segments") or {}).get(canary_id)
    if (not canary or canary.get("video_handoff_fingerprint") != current_fp or
            artifact.get("video_handoff_fingerprint") != current_fp or
            canary.get("audio_capability_policy") != artifact_policy or
            not artifact_contract.verify_video_handoff(canary).get("ok")):
        raise ValueError("AUDIO_CANARY_PROMOTION_CANARY_STALE")
    model = str(artifact.get("model_used") or "")
    audio_segments = [item for item in by_id.values()
                      if _segment_requires_embedded_audio(item)]
    if (not model or not audio_segments or
            any(str(item.get("model") or "") != model
                for item in audio_segments)):
        raise ValueError("AUDIO_CANARY_FORMAL_BATCH_MODEL_MISMATCH")
    canary_task = _approved_canary_task(manifest, artifact)
    proof = _build_audio_canary_proof(
        manifest, artifact, handoff, canary, canary_task)
    completed_output = _completed_canary_output_contract(
        canary, artifact, canary_task, proof)

    updated = copy.deepcopy(spec)
    promoted_segments = []
    for source in updated.get("segments") or []:
        segment = copy.deepcopy(source)
        segment.pop("audio_capability_policy", None)
        segment.pop("audio_capability_policy_contract", None)
        if str(segment.get("id") or "") == canary_id and completed_output:
            segment["output_artifact_contract"] = copy.deepcopy(
                completed_output)
        if _segment_requires_embedded_audio(segment):
            segment["audio_canary_proof"] = copy.deepcopy(proof)
            segment["privacy_fallback_policy"] = PRIVACY_FALLBACK_STOP_POLICY
            segment["privacy_fallback_policy_contract"] = {
                "kind": "promoted_canary_privacy_stop_policy",
                "schema_version": 1,
                "policy": PRIVACY_FALLBACK_STOP_POLICY,
                "scope": FORMAL_BATCH_PRIVACY_SCOPE,
                "segment_id": str(segment.get("id") or ""),
                "canary_segment_id": canary_id,
                "recovery_id": recovery.get("recovery_id"),
                "audio_canary_proof_fingerprint": proof["fingerprint"],
            }
        segment["video_handoff_fingerprint"] = (
            artifact_contract.build_video_handoff(segment)["fingerprint"])
        promoted_segments.append(segment)
    promoted_canary = next(
        item for item in promoted_segments
        if str(item.get("id") or "") == canary_id)
    if (_provider_request_identity(canary) !=
            _provider_request_identity(promoted_canary)):
        raise ValueError("AUDIO_CANARY_PROVIDER_REQUEST_CHANGED")
    updated["segments"] = promoted_segments
    updated.pop("privacy_fallback_policy_contract", None)
    updated.setdefault("retired_test_segment_policies", []).append({
        "audio_capability_policy": artifact_policy,
        "privacy_fallback_policy_contract": copy.deepcopy(
            spec.get("privacy_fallback_policy_contract") or {}),
        "retired_by": proof["fingerprint"],
    })
    updated_recovery = copy.deepcopy(recovery)
    updated_strategy = updated_recovery.setdefault("strategy", {})
    updated_strategy["require_canary_before_batch"] = False
    updated_strategy["canary_status"] = "fulfilled"
    updated_strategy["audio_canary_proof_fingerprint"] = proof["fingerprint"]
    updated["video_recovery_contract"] = updated_recovery
    old_segments = copy.deepcopy(handoff.get("segments") or {})

    def mutate(candidate, previous):
        _promote_canary_manifest(
            candidate, previous, proof=proof,
            source_handoff=handoff, canary_task=canary_task,
            old_segments=old_segments)

    commit_handoff(
        manifest, manifest_path, segments_path, updated,
        manifest_mutator=mutate)
    rm.validate_test_segment_closure(manifest)
    return {"already_promoted": False, "segments": updated,
            "promotion": (manifest.get("test_segment_artifact") or {}).get(
                "formal_batch_promotion")}


def bind_attempt_output(manifest, manifest_path, segments_path, *,
                        commit_handoff, segment_id=None,
                        rejected_review_path=None, failure_results_path=None):
    """Give one authorized recovery segment a new, non-overwriting output."""
    segments_record, spec = _read_current_record(
        segments_path, "RECOVERY_CANARY_OUTPUT_SOURCE_STALE")
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    target_id = str(segment_id or requirement.get("segment_id") or "").strip()
    is_dynamic_canary = target_id == str(
        requirement.get("segment_id") or "").strip()
    recovery = spec.get("video_recovery_contract") or {}
    recovery_file = recovery.get("file") or {}
    if (not target_id or not rm.file_record_is_current(
            handoff.get("file") or {}) or
            (handoff.get("file") or {}).get("sha256") !=
            segments_record.get("sha256") or
            not rm.file_record_is_current(recovery_file) or
            (is_dynamic_canary and
             not rm.recovery_test_segment_requirement_is_current(manifest))):
        raise ValueError("RECOVERY_CANARY_OUTPUT_SOURCE_STALE")
    authorized = (manifest.get("video_attempts") or {}).get(target_id) or {}
    attempt = int(authorized.get("attempt") or 0)
    previous_take_fingerprint = authorized.get("previous_take_fingerprint")
    if attempt < 2:
        raise ValueError("RECOVERY_CANARY_OUTPUT_ATTEMPT_REQUIRED")

    failure = recovery.get("failure") or {}
    evidence_type = None
    artifact = {}
    failure_record = authorized.get("failure_results") or {}
    if previous_take_fingerprint:
        review_record = (rm.file_record(rejected_review_path)
                         if rejected_review_path else
                         authorized.get("rejected_review") or
                         failure.get("rejected_take_review") or {})
        if ((not rejected_review_path and
             str(failure.get("segment_id") or target_id) != target_id) or
                not rm.file_record_is_current(review_record)):
            raise ValueError("RECOVERY_CANARY_OUTPUT_REJECTED_TAKE_REQUIRED")
        with open(review_record["path"], encoding="utf-8") as handle:
            review = json.load(handle)
        artifact = review.get("artifact") or {}
        if (review.get("decision", {}).get("verdict") != "rejected" or
                str(review.get("segment_id") or "") != target_id or
                artifact.get("take_fingerprint") != previous_take_fingerprint or
                not take_review.artifact_is_current(review) or
                not take_review.artifact_fingerprint_is_current(review)):
            raise ValueError("RECOVERY_CANARY_OUTPUT_REJECTED_TAKE_STALE")
        prior_path = os.path.abspath(str(artifact.get("local_path") or ""))
        prior_sha = artifact.get("sha256")
        if (not prior_path or not prior_sha or
                artifact_contract.file_sha256(prior_path) != prior_sha):
            raise ValueError("RECOVERY_CANARY_OUTPUT_REJECTED_TAKE_STALE")
        evidence_type = "rejected_take"
    else:
        supplied_failure = (rm.file_record(failure_results_path)
                            if failure_results_path else failure_record)
        if (supplied_failure != failure_record or
                not rm.video_attempt_failure_evidence_is_current(
                    manifest, target_id, authorized)):
            raise ValueError("RECOVERY_CANARY_OUTPUT_FAILURE_EVIDENCE_STALE")
        evidence_type = "settled_provider_failure"
        prior_path = ""
        prior_sha = None

    updated_segments = []
    matched = 0
    output_path = None
    source_handoff = (handoff.get("segments") or {}).get(target_id)
    for source in spec.get("segments") or []:
        segment = copy.deepcopy(source)
        if str(segment.get("id") or "") == target_id:
            raw_authored_prior = str(segment.get("out_path") or "").strip()
            if not raw_authored_prior:
                raise ValueError("RECOVERY_CANARY_OUTPUT_SOURCE_PATH_REQUIRED")
            authored_prior = os.path.abspath(raw_authored_prior)
            if evidence_type == "settled_provider_failure":
                prior_path = authored_prior
                # Preserve any unexpected bytes at the failed destination as
                # audit evidence; never reuse or overwrite that path.
                prior_sha = artifact_contract.file_sha256(prior_path)
            segment_recovery = segment.get("recovery_contract") or {}
            if (not source_handoff or
                    (evidence_type == "rejected_take" and
                     authored_prior != prior_path) or
                    segment.get("video_handoff_fingerprint") != source_handoff or
                    segment_recovery.get("recovery_id") !=
                    recovery.get("recovery_id") or
                    segment_recovery.get("path") != recovery_file.get("path") or
                    segment_recovery.get("sha256") != recovery_file.get("sha256") or
                    artifact_contract.build_video_handoff(
                        segment)["fingerprint"] != source_handoff):
                raise ValueError("RECOVERY_CANARY_OUTPUT_SOURCE_STALE")
            suffix = os.path.splitext(prior_path)[1] or ".mp4"
            output_path = os.path.join(
                os.path.dirname(prior_path),
                "%s_attempt%d_recovery%s" %
                (target_id, attempt, suffix))
            output_path = os.path.abspath(output_path)
            if output_path == prior_path or os.path.exists(output_path):
                raise ValueError(
                    "RECOVERY_CANARY_OUTPUT_ALREADY_EXISTS: %s" % output_path)
            segment["out_path"] = output_path
            segment["output_artifact_contract"] = {
                "mode": "attempt_specific_no_overwrite",
                "evidence_type": evidence_type,
                "attempt": attempt,
                "path": output_path,
                "must_not_exist_before_generation": True,
                "prior_take_path": prior_path,
                "prior_take_sha256": prior_sha,
                "previous_take_fingerprint": previous_take_fingerprint,
                "previous_take_handoff_fingerprint": artifact.get(
                    "video_handoff_fingerprint"),
                "source_video_handoff_fingerprint": source_handoff,
                "source_out_path": authored_prior,
                "source_output_artifact_contract": copy.deepcopy(
                    source.get("output_artifact_contract") or {}),
                "recovery_id": segment_recovery.get("recovery_id"),
                "target_model": segment.get("model"),
            }
            if evidence_type == "settled_provider_failure":
                segment["output_artifact_contract"].update({
                    "previous_task_id": authorized.get("previous_task_id"),
                    "failure_stage": authorized.get("failure_stage"),
                    "failure_results": failure_record,
                    "failure_video_handoff_fingerprint": authorized.get(
                        "failure_video_handoff_fingerprint"),
                })
            segment["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(segment)["fingerprint"])
            matched += 1
        updated_segments.append(segment)
    if matched != 1 or not output_path:
        raise ValueError("RECOVERY_CANARY_OUTPUT_SEGMENT_REQUIRED")
    updated = copy.deepcopy(spec)
    updated["segments"] = updated_segments
    commit_handoff(manifest, manifest_path, segments_path, updated)
    target = next(
        segment for segment in updated_segments
        if str(segment.get("id") or "") == target_id)
    return {
        "segment_id": target_id, "attempt": attempt,
        "out_path": output_path,
        "video_handoff_fingerprint": target[
            "video_handoff_fingerprint"],
        "segments": updated,
    }


def bind_canary_output(manifest, manifest_path, segments_path, *,
                       commit_handoff):
    """Compatibility wrapper for the current dynamic recovery canary."""
    return bind_attempt_output(
        manifest, manifest_path, segments_path,
        commit_handoff=commit_handoff)
