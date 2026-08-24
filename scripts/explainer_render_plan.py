#!/usr/bin/env python3
"""Transactional, customer-readable render plan for document explainers.

This module is local-only.  It freezes the approved deterministic composition,
declares (but does not submit) the identity-plate image request, and creates the
two pre-video sub-gates that were previously implicit in conversation:
confirmed identity plate and confirmed exact video prompt packet.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import asset_prep
import br_client
import key_setup
import prompt_review
import run_manifest
import storyboard
import identity_request_contract
import dialogue_timing
from project_utils import atomic_write_bytes, atomic_write_json


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(path):
    path = os.path.abspath(str(path or ""))
    if not os.path.isfile(path):
        raise ValueError("RENDER_PLAN_INPUT_MISSING: %s" % path)
    return {"path": path, "sha256": _sha256(path)}


def _json_sha256(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _submission_request_spec(request, *, resolution=None):
    """Canonical identity for the exact paid request, excluding data bytes."""
    return identity_request_contract.submission_request_spec(
        request, resolution=resolution)


def _current_render_plan(manifest):
    generation = (manifest.get("generation") or {}).get("render_plan") or {}
    primary = generation.get("primary_artifact") or {}
    path = primary.get("path")
    current = run_manifest.file_record(path)
    if (not current or not current.get("exists") or
            current.get("sha256") != primary.get("sha256")):
        raise ValueError("RENDER_PLAN_PRIMARY_ARTIFACT_STALE")
    plan = _load(path)
    if (plan.get("client") != manifest.get("client") or
            str(plan.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("RENDER_PLAN_IDENTITY_MISMATCH")
    return plan, current


def _confirmed_cast_reference(manifest):
    if not run_manifest.approval_is_current(manifest, "cast_board"):
        raise ValueError("RENDER_PLAN_CAST_APPROVAL_REQUIRED")
    evidence = (manifest.get("approval_evidence") or {}).get("cast_board") or {}
    artifacts = ((manifest.get("generation") or {}).get("cast_board") or {}).get(
        "artifacts") or []
    board = next((item for item in artifacts
                  if os.path.basename(str(item.get("path") or "")) == "cast_board.jpg"),
                 None)
    if not board or not board.get("exists"):
        raise ValueError("RENDER_PLAN_CAST_BOARD_MISSING")
    current = run_manifest.file_record(board.get("path"))
    marker = _record(evidence.get("approval_path"))
    if (not current or current.get("sha256") != board.get("sha256") or
            evidence.get("artifact_sha256") != current.get("sha256")):
        raise ValueError("RENDER_PLAN_CAST_EVIDENCE_STALE")
    return {
        "path": current["path"], "sha256": current["sha256"],
        "approval_marker": marker, "customer_confirmed": True,
        "usage": "gpt_image_2_upstream_img2img_only_not_video_input",
    }


def _clean_clause(value):
    return str(value or "").strip().rstrip("。！？；;，, ")


def _visual_persona(value):
    """A still image can lock appearance, never language or voice."""
    text = _clean_clause(value)
    text = re.sub(r"[，,。；;]\s*(普通话|粤语|英文)[^。；;]*$", "", text)
    return _clean_clause(text) or "专业讲师"


def _identity_prompt(presenter, continuity):
    return (
        "生成一张横版16:9写实商务数字人身份锚定图，仅包含一位讲师。"
        "人物：%s。服装：%s。背景：%s。中景正视，上半身和脸清晰可见，"
        "自然静止表情，双手不遮脸。干净画面，无PPT、字幕、文字、Logo、图表、"
        "卡片或水印。这是后续avatar plate的单一身份参考，不是故事板。" %
        (_visual_persona(presenter),
         _clean_clause(continuity.get("wardrobe") or "商务讲师着装"),
         _clean_clause(continuity.get("background") or "干净讲解空间")))


def _segment_prompt(segment, continuity):
    dialogue = str(segment.get("dialogue") or "").strip()
    if not dialogue:
        raise ValueError("RENDER_PLAN_DIALOGUE_REQUIRED: %s" % segment.get("id"))
    content_only = segment.get("visual_mode") == "content_full_bleed"
    visual_owner = (
        "本镜最终视觉完全由冻结的本地内容层承担，人物底片仅提供同一声线音频。"
        if content_only else
        "人物底片将由冻结的本地合成规格放入已确认人物安全区。")
    return (
        "生成%s的干净单人数字人讲师底片，时长%ss，横版16:9。身份锚图需先生成、"
        "展示并由客户确认，之后方可用它锁定同一张脸、发型、服装和背景。自然稳定口播，克制手势。"
        "%s口播原文：%s。绝不生成PPT、流程图、字幕、文字、标题、Logo、数据卡片或"
        "水印；这些内容由已确认的本地内容与字幕动效合同负责。" %
        (segment.get("id"), segment.get("duration"), visual_owner, dialogue))


def build(manifest_path, storyboard_result_path, execution_preview_path,
          director_plan_path, out_path, preview_out):
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest.load_manifest(manifest_path)
    if not run_manifest.approval_is_current(manifest, "storyboard"):
        raise ValueError("RENDER_PLAN_STORYBOARD_APPROVAL_REQUIRED")
    result_path = os.path.abspath(storyboard_result_path)
    result = _load(result_path)
    if not storyboard.storyboard_approval_is_current(result_path):
        raise ValueError("RENDER_PLAN_STORYBOARD_MARKER_STALE")
    approval = result.get("storyboard_approval") or {}
    if (approval.get("kind") != "design_board" or
            approval.get("video_model_usage") != "do_not_submit_as_storyboard_ref"):
        raise ValueError("RENDER_PLAN_DOCUMENT_DESIGN_BOARD_CONTRACT_REQUIRED")
    execution_path = os.path.abspath(execution_preview_path)
    execution = _load(execution_path)
    approved_execution = ((approval.get("execution_artifacts") or {}).get(
        "execution_preview_result") or {})
    if approved_execution != _record(execution_path):
        raise ValueError("RENDER_PLAN_EXECUTION_PREVIEW_NOT_APPROVED")
    binding = execution.get("manifest_binding") or {}
    if not binding.get("approval_eligible"):
        raise ValueError("RENDER_PLAN_EXECUTION_VISUAL_QA_REQUIRED")
    qc_path = os.path.join(os.path.dirname(execution_path), "commercial_qc.json")
    qc = _load(qc_path)
    if not qc.get("passed"):
        raise ValueError("RENDER_PLAN_COMMERCIAL_QC_REQUIRED")

    director_path = os.path.abspath(director_plan_path)
    director = _load(director_path)
    if (director.get("client") != manifest.get("client") or
            str(director.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("RENDER_PLAN_DIRECTOR_IDENTITY_MISMATCH")
    segments = list(director.get("segments") or [])
    if not segments or any(not str(item.get("dialogue") or "").strip()
                           for item in segments):
        raise ValueError("RENDER_PLAN_APPROVED_DIALOGUE_REQUIRED")
    expected = float((((approval.get("execution_artifacts") or {}).get(
        "commercial_render_contract") or {}).get("audio_timing_contract") or {}).get(
            "expected_duration") or 0)
    actual = round(sum(float(item.get("duration") or 0) for item in segments), 3)
    if not expected or actual != round(expected, 3):
        raise ValueError("RENDER_PLAN_DURATION_CONTRACT_MISMATCH")
    timing_shots = [{
        "id": item.get("id"), "dialogue": item.get("dialogue"),
        "duration": item.get("duration"),
        "audio_contract": item.get("audio_contract") or {},
        "voice_type": item.get("voice_type"),
    } for item in segments]
    timing_voice_type = ((director.get("presenter") or {}).get("voice_type") or
                         (director.get("presenter") or {}).get("voice"))
    timing_contract = dialogue_timing.build_contract(
        timing_shots, plan_voice_type=timing_voice_type)
    timing_proposal = dialogue_timing.propose_reallocation(
        timing_shots, plan_voice_type=timing_voice_type)
    dialogue_timing.assert_fits(
        timing_contract,
        error_code="RENDER_PLAN_DIALOGUE_DURATION_OVERFLOW",
        proposal=timing_proposal)
    timing_by_id = {
        item["shot_id"]: item for item in timing_contract["items"]}

    continuity = director.get("continuity") or {}
    presenter = director.get("presenter") or {}
    cast_reference = _confirmed_cast_reference(manifest)
    identity_prompt = _identity_prompt(
        presenter.get("persona") or presenter.get("name") or "专业讲师", continuity)
    key_setup.ensure_session_id()
    catalog_key = key_setup.load_key()
    if not catalog_key:
        raise ValueError("RENDER_PLAN_IMAGE_CATALOG_KEY_REQUIRED")
    catalog = br_client.list_image_models(catalog_key)
    spec = next((item for item in catalog if item.get("id") == "gpt-image-2"), None)
    ratios = (spec or {}).get("ratios") or []
    resolutions = (spec or {}).get("resolutions") or []
    if "16:9" not in ratios or not resolutions:
        raise ValueError("RENDER_PLAN_GPT_IMAGE_2_SPEC_UNAVAILABLE")
    resolution = "2k" if "2k" in resolutions else resolutions[0]
    catalog_contract = {
        "model": "gpt-image-2", "ratios": ratios,
        "resolutions": resolutions, "selected_ratio": "16:9",
        "selected_resolution": resolution,
    }
    identity_request = {
        "model": "gpt-image-2", "ratio": "16:9", "resolution": resolution,
        "prompt_zh": identity_prompt, "source_references": [cast_reference],
        "source_fingerprint": _json_sha256({
            "prompt": identity_prompt, "reference": cast_reference}),
        "catalog_contract": dict(
            catalog_contract, fingerprint=_json_sha256(catalog_contract)),
        "submission_status": "not_submitted",
    }
    planned_submission = _submission_request_spec(identity_request)
    identity_request["planned_submission_request_spec"] = planned_submission
    identity_request["planned_submission_request_fingerprint"] = (
        _json_sha256(planned_submission))
    video_segments = [{
        "id": item.get("id"), "duration_seconds": item.get("duration"),
        "dialogue": item.get("dialogue"),
        "dialogue_timing": timing_by_id[str(item.get("id"))],
        "audio_contract": item.get("audio_contract") or {},
        "voice_type": item.get("voice_type"),
        "visual_mode": item.get("visual_mode"),
        "submission_prompt_zh": _segment_prompt(item, continuity),
        "video_model_usage": "avatar_plate_only", "storyboard_ref": False,
        "requires_identity_plate": True,
        "forbidden_visual_inputs": [
            "PPT", "design_board", "subtitle", "graphic_card", "logo_overlay"],
    } for item in segments]
    plan = {
        "schema_version": 2, "kind": "document_explainer_render_plan",
        "client": manifest["client"], "run_id": manifest["run_id"],
        "ratio": "16:9", "resolution": [1920, 1080],
        "duration_seconds": expected, "local_only_build": True,
        "external_model_calls_made": False,
        "approval_scope": "direction_and_local_composition_not_paid_submission",
        "source_contracts": {
            "storyboard_result": _record(result_path),
            "director_plan": _record(director_path),
            "execution_preview": _record(execution_path),
            "commercial_qc": _record(qc_path),
        },
        "dialogue_timing_contract": timing_contract,
        "dialogue_timing_voice_type": timing_voice_type,
        "identity_plate_generation": {
            "required_before_video": True,
            "status": "awaiting_independent_customer_confirmation",
            "request": identity_request,
            "input_policy": "confirmed cast board is upstream img2img only",
        },
        "voice_continuity_contract": {
            "voice_profile": presenter.get("voice") or "已确认同一讲师声线",
            "owner": "video_model_integrated_audio",
            "image_does_not_lock_voice": True,
            "content_full_bleed": {
                "presenter_visible": False, "audio_required": True,
                "visual_owner": "Remotion content layer"},
        },
        "segments": video_segments,
        "pre_video_gates": {
            "identity_plate": "required_and_independently_customer_confirmed",
            "exact_video_prompt_review": "required_after_preflight",
        },
        "assembly": {
            "owner": "video_use_bridge --formal",
            "approved_previs_spec": (approval.get("execution_artifacts") or {}).get(
                "integrated_previs_spec"),
            "timing_change_requires_reapproval": True,
        },
    }
    canonical = _json_sha256(plan)
    lines = [
        "# 正式渲染计划（待客户确认）", "",
        "本文件只冻结方向与本地合成合同；未调用外部生成模型，也不授权视频提交。", "",
        "- Canonical JSON 指纹：`%s`" % canonical,
        "- JSON 与本阅读版会作为同一 render_plan 阶段的审批字节。", "",
        "## 身份锚图请求（生成后仍需单独看图确认）", "", identity_prompt, "",
        "- 上游人物板：`%s`" % cast_reference["path"],
        "- 人物板 SHA-256：`%s`" % cast_reference["sha256"],
        "- 人物确认凭据：`%s`" % cast_reference["approval_marker"]["path"],
        "- 请求来源指纹：`%s`" % identity_request["source_fingerprint"],
        "- 完整计划提交指纹：`%s`" % identity_request[
            "planned_submission_request_fingerprint"],
        "- 实时规格：`gpt-image-2 / 16:9 / %s`" % resolution, "",
        "## 声音与画面职责", "",
        "- 声音由视频模型音画一体底片负责；静态身份图不锁定声音。",
        "- 全屏内容镜头只使用底片音频，最终视觉由已确认的本地内容层负责。", "",
        "## 分段底片提示方向（不是最终提交包）", "",
    ]
    for item in video_segments:
        lines.extend([
            "### %s｜%ss" % (item["id"], item["duration_seconds"]), "",
            item["submission_prompt_zh"], "",
        ])
    lines.extend([
        "## 付费前仍必须完成", "",
        "1. 单独展示并确认身份锚图的人物引用、提示词、模型规格和计划请求指纹。",
        "2. 本地捕获精确 imageUrls 提交包；客户确认模型、画幅、分辨率、提示词与引用图后才允许生成。",
        "3. 生成并展示单人身份锚图，客户明确确认后绑定其文件哈希。",
        "4. 对最终 segments 执行 preflight，再 capture-video 展示主模型、全部 fallback、逐字提示词和 imageUrls。",
        "5. 客户明确确认该最终提示词包后，路由才允许 start_video。", "",
    ])
    atomic_write_bytes(preview_out, ("\n".join(lines) + "\n").encode("utf-8"),
                       label="render_plan_preview")
    plan["customer_preview"] = {
        "path": os.path.abspath(preview_out), "sha256": _sha256(preview_out),
        "canonical_plan_fingerprint": canonical,
    }
    atomic_write_json(out_path, plan)
    return plan


def bind_identity_plate(manifest_path, image_path):
    manifest_path = os.path.abspath(manifest_path)
    for _ in range(3):
        manifest = run_manifest.load_manifest(manifest_path)
        if str((manifest.get("chain_profile") or {}).get("profile") or "") == (
                "document_explainer"):
            raise ValueError(
                "LEGACY_IDENTITY_BIND_DISABLED: use identity_plate_workflow "
                "record-visual-review + confirm-image so result/QC/review/customer "
                "evidence bind the same candidate")
        revision = int(manifest.get("revision") or 0)
        if not run_manifest.approval_is_current(manifest, "render_plan"):
            raise ValueError("IDENTITY_PLATE_RENDER_PLAN_APPROVAL_REQUIRED")
        plan, plan_record = _current_render_plan(manifest)
        evidence = asset_prep.confirmed_asset_record(manifest["client"], image_path)
        document_explainer = str(
            (manifest.get("chain_profile") or {}).get("profile") or "") == (
                "document_explainer")
        if document_explainer:
            state = manifest.get("identity_plate_generation") or {}
            output = state.get("output") or {}
            if (not run_manifest.identity_plate_submission_is_current(manifest) or
                    state.get("status") != "generated_pending_customer_confirmation" or
                    not run_manifest.file_record_is_current(output) or
                    os.path.abspath(str(evidence.get("path") or "")) != output.get("path") or
                    evidence.get("sha256") != output.get("sha256")):
                raise ValueError(
                    "IDENTITY_PLATE_BIND_REQUIRES_CONFIRMED_CAPTURE_GENERATED_OUTPUT")
        gate = {
            "status": "confirmed", "render_plan": plan_record,
            "identity_request_fingerprint": (((plan.get("identity_plate_generation") or {})
                                                .get("request") or {})
                                               .get("source_fingerprint")),
            "asset": evidence, "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
        manifest.setdefault("pre_video_gates", {})["identity_plate"] = gate
        if document_explainer:
            manifest["identity_plate_generation"] = dict(
                state, status="confirmed", output_approval=evidence)
        try:
            run_manifest.save_manifest(
                manifest, manifest_path, expected_revision=revision)
            return gate
        except run_manifest.ManifestConflictError:
            continue
    raise run_manifest.ManifestConflictError("IDENTITY_PLATE_BIND_CONFLICT")


def prepare_identity_submission_request(manifest_path, out_path):
    """Persist the exact request identity before any async create call."""
    manifest = run_manifest.load_manifest(os.path.abspath(manifest_path))
    if not run_manifest.approval_is_current(manifest, "render_plan"):
        raise ValueError("IDENTITY_SUBMISSION_RENDER_PLAN_APPROVAL_REQUIRED")
    plan, plan_record = _current_render_plan(manifest)
    request = ((plan.get("identity_plate_generation") or {}).get("request") or {})
    model, ratio, resolution = asset_prep._validated_image_request_spec(
        request.get("model"), request.get("ratio"), request.get("resolution"))
    if (model != request.get("model") or ratio != request.get("ratio") or
            resolution != request.get("resolution")):
        raise ValueError(
            "IDENTITY_SUBMISSION_SPEC_CHANGED_AFTER_APPROVAL: regenerate render plan")
    spec = _submission_request_spec(request, resolution=resolution)
    payload = {
        "schema_version": 1, "kind": "identity_plate_submission_request",
        "status": "prepared_not_submitted", "client": manifest["client"],
        "run_id": manifest["run_id"], "render_plan": plan_record,
        "request_spec": spec, "request_fingerprint": _json_sha256(spec),
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "invariant": "submission must use these exact model/ratio/resolution/prompt/reference bytes",
    }
    expected = request.get("planned_submission_request_fingerprint")
    if expected != payload["request_fingerprint"]:
        raise ValueError("IDENTITY_SUBMISSION_REQUEST_DIVERGED_FROM_APPROVED_PLAN")
    atomic_write_json(os.path.abspath(out_path), payload)
    return payload


def bind_video_prompt_review(manifest_path, review_path):
    manifest_path = os.path.abspath(manifest_path)
    review_path = os.path.abspath(review_path)
    review = _load(review_path)
    prompt_review.assert_review_current(review, path=review_path)
    if (review.get("stage") != "video" or review.get("status") != "confirmed" or
            review.get("confirmed_by_customer") is not True):
        raise ValueError("VIDEO_PROMPT_REVIEW_CONFIRMED_REQUIRED")
    for _ in range(3):
        manifest = run_manifest.load_manifest(manifest_path)
        revision = int(manifest.get("revision") or 0)
        if not run_manifest.approval_is_current(manifest, "render_plan"):
            raise ValueError("VIDEO_PROMPT_RENDER_PLAN_APPROVAL_REQUIRED")
        _, plan_record = _current_render_plan(manifest)
        gate = {
            "status": "confirmed", "render_plan": plan_record,
            "review": run_manifest.file_record(review_path),
            "review_fingerprint": review.get("review_fingerprint"),
            "confirmed_at": review.get("confirmed_at"),
            "customer_confirmation": review.get("customer_confirmation"),
        }
        manifest.setdefault("pre_video_gates", {})["video_prompt_review"] = gate
        try:
            run_manifest.save_manifest(
                manifest, manifest_path, expected_revision=revision)
            return gate
        except run_manifest.ManifestConflictError:
            continue
    raise run_manifest.ManifestConflictError("VIDEO_PROMPT_BIND_CONFLICT")


def record_identity_plate_failure(manifest_path, out_path, *, task_id, error,
                                  submitted_request_spec, submitted_render_plan,
                                  submitted_request_fingerprint=None, attempt=1):
    manifest_path = os.path.abspath(manifest_path)
    out_path = os.path.abspath(out_path)
    manifest = run_manifest.load_manifest(manifest_path)
    plan, plan_record = _current_render_plan(manifest)
    request = ((plan.get("identity_plate_generation") or {}).get("request") or {})
    if not isinstance(submitted_request_spec, dict):
        raise ValueError("IDENTITY_FAILURE_ACTUAL_REQUEST_SPEC_REQUIRED")
    required = {"model", "ratio", "resolution", "prompt_zh", "image_references"}
    if (not required.issubset(submitted_request_spec) or
            not submitted_request_spec.get("image_references")):
        raise ValueError("IDENTITY_FAILURE_ACTUAL_REQUEST_SPEC_INCOMPLETE")
    submitted_fingerprint = _json_sha256(submitted_request_spec)
    if (submitted_request_fingerprint and
            submitted_request_fingerprint != submitted_fingerprint):
        raise ValueError("IDENTITY_FAILURE_ACTUAL_REQUEST_FINGERPRINT_MISMATCH")
    if (not isinstance(submitted_render_plan, dict) or
            not submitted_render_plan.get("path") or
            not submitted_render_plan.get("sha256")):
        raise ValueError("IDENTITY_FAILURE_SUBMITTED_RENDER_PLAN_REQUIRED")
    try:
        attempt = int(attempt)
    except (TypeError, ValueError) as exc:
        raise ValueError("IDENTITY_FAILURE_ATTEMPT_INVALID") from exc
    if attempt < 1:
        raise ValueError("IDENTITY_FAILURE_ATTEMPT_INVALID")
    payload = {
        "schema_version": 1, "kind": "identity_plate_attempt_failure",
        "client": manifest["client"], "run_id": manifest["run_id"],
        "status": "failed", "task_id": str(task_id), "error": str(error),
        "submitted_request_spec": submitted_request_spec,
        "submitted_request_fingerprint": submitted_fingerprint,
        "submitted_render_plan": submitted_render_plan,
        "planned_request_spec": request.get("planned_submission_request_spec"),
        "planned_request_fingerprint": request.get(
            "planned_submission_request_fingerprint"),
        "current_planned_retry_render_plan": plan_record,
        "returned_media": [], "candidate_images_created": 0,
        "billing_status": "unknown_not_inferred",
    }
    atomic_write_json(out_path, payload)
    for _ in range(3):
        manifest = run_manifest.load_manifest(manifest_path)
        revision = int(manifest.get("revision") or 0)
        task = run_manifest.upsert_task(manifest, {
            "stage": "render_plan", "unit_id": "identity_plate",
            "handoff_fingerprint": submitted_fingerprint, "attempt": attempt,
            "model": submitted_request_spec.get("model"),
            "task_id": str(task_id), "status": "failed",
            "request_spec": submitted_request_spec,
            "request_fingerprint": submitted_fingerprint,
            "planned_retry_request_spec": request.get("planned_submission_request_spec"),
            "failure_evidence": run_manifest.file_record(out_path),
            "returned_media": [], "candidate_images_created": 0,
            "billing_status": "unknown_not_inferred",
        })
        try:
            run_manifest.save_manifest(manifest, manifest_path,
                                       expected_revision=revision)
            return {"failure": payload, "task": task}
        except run_manifest.ManifestConflictError:
            continue
    raise run_manifest.ManifestConflictError("IDENTITY_FAILURE_BIND_CONFLICT")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--storyboard-result")
    parser.add_argument("--execution-preview")
    parser.add_argument("--director-plan")
    parser.add_argument("--out")
    parser.add_argument("--preview-out")
    parser.add_argument("--bind-identity")
    parser.add_argument("--bind-video-prompt-review")
    parser.add_argument("--prepare-identity-request")
    parser.add_argument("--record-identity-failure", action="store_true")
    parser.add_argument("--failure-out")
    parser.add_argument("--failed-task-id")
    parser.add_argument("--failure-message")
    parser.add_argument("--submitted-request",
                        help="失败前已落盘的实际 request spec JSON；不可用当前计划重构历史")
    parser.add_argument("--failed-attempt", type=int, default=1)
    args = parser.parse_args(argv)
    if args.bind_identity:
        result = bind_identity_plate(args.manifest, args.bind_identity)
    elif args.bind_video_prompt_review:
        result = bind_video_prompt_review(
            args.manifest, args.bind_video_prompt_review)
    elif args.prepare_identity_request:
        result = prepare_identity_submission_request(
            args.manifest, args.prepare_identity_request)
    elif args.record_identity_failure:
        if not all((args.failure_out, args.failed_task_id, args.failure_message,
                    args.submitted_request)):
            parser.error("failure recording requires failure/task/message/submitted-request")
        submitted_payload = _load(args.submitted_request)
        if submitted_payload.get("kind") != "identity_plate_submission_request":
            parser.error("--submitted-request 必须是付费调用前落盘的 identity submission contract")
        result = record_identity_plate_failure(
            args.manifest, args.failure_out, task_id=args.failed_task_id,
            error=args.failure_message,
            submitted_request_spec=(submitted_payload.get("request_spec")
                                    or submitted_payload),
            submitted_render_plan=submitted_payload.get("render_plan"),
            submitted_request_fingerprint=submitted_payload.get(
                "request_fingerprint"),
            attempt=args.failed_attempt)
    else:
        required = (args.storyboard_result, args.execution_preview,
                    args.director_plan, args.out, args.preview_out)
        if not all(required):
            parser.error("build requires storyboard/execution/director/out/preview-out")
        manifest = run_manifest.load_manifest(args.manifest)
        revision = int(manifest.get("revision") or 0)
        run_manifest.mark_generation_started(manifest, "render_plan")
        run_manifest.save_manifest(manifest, args.manifest,
                                   expected_revision=revision)
        try:
            plan = build(
                args.manifest, args.storyboard_result, args.execution_preview,
                args.director_plan, os.path.abspath(args.out),
                os.path.abspath(args.preview_out))
            manifest = run_manifest.load_manifest(args.manifest)
            revision = int(manifest.get("revision") or 0)
            run_manifest.mark_generation_finished(
                manifest, "render_plan", [args.out, args.preview_out])
            manifest["generation"]["render_plan"]["primary_artifact"] = (
                run_manifest.file_record(args.out))
            run_manifest.save_manifest(manifest, args.manifest,
                                       expected_revision=revision)
            result = {
                "render_plan": _record(args.out), "preview": _record(args.preview_out),
                "segments": len(plan["segments"]), "external_calls": False,
            }
        except (OSError, ValueError, TypeError, RuntimeError,
                run_manifest.ManifestConflictError) as exc:
            # A failed local builder must never strand the stage at running.
            run_manifest.record_generation_failure_if_running(
                args.manifest, "render_plan", str(exc),
                error_code="RENDER_PLAN_BUILD_FAILED", retryable=True)
            raise
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
