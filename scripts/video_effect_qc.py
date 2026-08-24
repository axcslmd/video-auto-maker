#!/usr/bin/env python3
"""Preflight and postflight checks for storyboard-to-video design fidelity.

This module is intentionally conservative. Technical media checks can prove
container/audio/OCR facts, but product geometry and product-to-phone contact
must be reviewed by a human or a vision reviewer and recorded explicitly.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_manifest  # noqa: E402
import storyboard  # noqa: E402
import prompt_review_contract  # noqa: E402


REQUIRED_MANUAL_CHECKS = (
    "product_shape_fidelity",
    "magnetic_bottom_to_phone_back",
    "storyboard_action_fidelity",
    "voice_continuity",
    "bgm_continuity_or_post_mix",
    "lip_sync",
    "no_extra_text_or_logos",
)


def _load_json(path, default=None):
    if not path:
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _segments(value):
    if isinstance(value, dict):
        return value.get("segments") or []
    return value or []


def _text_blob(value):
    parts = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.startswith("_"):
                continue
            parts.append(_text_blob(item))
    elif isinstance(value, list):
        parts.extend(_text_blob(item) for item in value)
    elif value is not None:
        parts.append(str(value))
    return " ".join(part for part in parts if part)


def _prompt_items(review):
    return {str(item.get("shot_id")): item
            for item in (review or {}).get("prompts") or []}


def _all_prompt_texts(item):
    texts = []
    for key in ("prompt_zh", "submission_prompt_zh"):
        if item.get(key):
            texts.append(str(item[key]))
    for value in (item.get("model_submission_prompts") or {}).values():
        if value:
            texts.append(str(value))
    for fallback in item.get("fallback_submission_prompts") or []:
        if isinstance(fallback, dict) and fallback.get("submission_prompt_zh"):
            texts.append(str(fallback["submission_prompt_zh"]))
    return texts


def _submission_prompt_texts(item):
    texts = []
    if item.get("submission_prompt_zh"):
        texts.append(str(item["submission_prompt_zh"]))
    for value in (item.get("model_submission_prompts") or {}).values():
        if value:
            texts.append(str(value))
    for fallback in item.get("fallback_submission_prompts") or []:
        if isinstance(fallback, dict) and fallback.get("submission_prompt_zh"):
            texts.append(str(fallback["submission_prompt_zh"]))
    return texts


def _add(report, check_id, passed, message="", critical=True, evidence=None):
    item = {
        "id": check_id,
        "passed": bool(passed),
        "critical": bool(critical),
        "message": message,
        "evidence": evidence or {},
    }
    report["checks"].append(item)
    if not passed and critical:
        report["errors"].append(check_id)
    elif not passed:
        report["warnings"].append(check_id)
    return item


def _check_manifest(report, manifest_path, client):
    if not manifest_path:
        _add(report, "manifest_supplied", False,
             "未提供 manifest；只能做合同静态检查，不能证明正式生成闸门可通过。",
             critical=False)
        return
    try:
        manifest = _load_json(manifest_path)
        run_manifest.generation_gate(manifest, "video", client=client or manifest.get("client"))
        _add(report, "manifest_video_gate", True, "video 依赖阶段均已确认且 artifact 当前。")
    except (OSError, ValueError, TypeError, RuntimeError, KeyError) as error:
        _add(report, "manifest_video_gate", False, str(error),
             evidence={"manifest": os.path.abspath(manifest_path)})


def _check_plan_coverage(report, segments, plan):
    if not plan:
        _add(report, "storyboard_plan_supplied", False,
             "未提供 storyboard_plan；无法证明视频段覆盖全部已确认分镜。",
             critical=False)
        return
    shot_ids = [str(shot.get("id")) for shot in plan.get("shots") or [] if shot.get("id")]
    segment_ids = {str(segment.get("id")) for segment in segments}
    missing = [shot_id for shot_id in shot_ids if shot_id not in segment_ids]
    extra = sorted(segment_ids - set(shot_ids))
    _add(report, "segments_cover_storyboard_plan", not missing and not extra,
         "视频 segments 必须逐 shot 覆盖 storyboard_plan，不得少段或多段。",
         evidence={"missing": missing, "extra": extra,
                   "plan_shot_count": len(shot_ids), "segment_count": len(segments)})


def _check_segments_declared_complete(report, segments_doc):
    if not isinstance(segments_doc, dict):
        return
    missing_images = segments_doc.get("missing_images") or []
    needs_image = segments_doc.get("needs_image") or []
    _add(report, "segments_no_declared_missing_images",
         not missing_images and not needs_image,
         "segments 文件不能声明 missing_images/needs_image；正式视频 handoff 必须完整。",
         evidence={"missing_images": missing_images, "needs_image": needs_image})


def _check_reference_handoff(report, segments):
    incomplete = []
    for segment in segments:
        required = set(segment.get("required_reference_types") or [])
        if not required:
            continue
        actual = {ref.get("type") for ref in segment.get("references") or []
                  if isinstance(ref, dict) and ref.get("type")}
        missing = sorted(required - actual)
        dropped = sorted({item.get("type") for item in segment.get("dropped_references") or []
                          if isinstance(item, dict) and item.get("type") in required})
        if missing or dropped:
            incomplete.append({
                "segment_id": segment.get("id"),
                "required": sorted(required),
                "actual": sorted(actual),
                "missing": missing,
                "dropped": dropped,
            })
    _add(report, "reference_handoff_complete", not incomplete,
         "视频 handoff 必须实际包含所有 required_reference_types，不能只在合同里声明。",
         evidence={"incomplete": incomplete})


def _is_http_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _check_video_reference_urls(report, segments):
    bad = []
    for segment in segments:
        segment_id = segment.get("id")
        for index, value in enumerate(segment.get("urls") or [], 1):
            if not _is_http_url(value):
                bad.append({"segment_id": segment_id, "field": "urls",
                            "index": index, "value": str(value)[:180]})
        for ref in segment.get("references") or []:
            if not isinstance(ref, dict) or not ref.get("url"):
                continue
            value = ref.get("url")
            if not _is_http_url(value):
                bad.append({"segment_id": segment_id, "field": "references.url",
                            "tag": ref.get("tag"), "type": ref.get("type"),
                            "value": str(value)[:180]})
    _add(report, "video_reference_urls_are_remote", not bad,
         "BasicRouter /v1/video-generations 的 imageUrls 必须是图片素材 URL；"
         "不能提交本地路径或 base64。缺 URL 时应回到图片 retrieve 产物恢复，而不是重新画图托管。",
         evidence={"bad": bad})


def _check_render_plan_storyboard_binding(report, segments, storyboard_result_status):
    expected = (storyboard_result_status or {}).get("path")
    if not expected:
        return
    expected_abs = os.path.abspath(expected)
    mismatches = []
    for segment in segments:
        render_plan = ((segment.get("render_plan") or {}).get("content") or {})
        declared = render_plan.get("storyboard_result")
        if not declared:
            continue
        declared_abs = os.path.abspath(declared)
        if declared_abs != expected_abs:
            mismatches.append({
                "segment_id": segment.get("id"),
                "declared": declared_abs,
                "expected": expected_abs,
            })
    _add(report, "render_plan_storyboard_result_current", not mismatches,
         "segments 内嵌 render_plan 的 storyboard_result 必须指向当前 storyboard revision；"
         "不能把旧逻辑目录或旧 revision 路径带入视频提示词。",
         evidence={"mismatches": mismatches})


def _plan_shot_ids(plan):
    return [str(shot.get("id") or index)
            for index, shot in enumerate((plan or {}).get("shots") or [], 1)]


def _storyboard_item_id(item):
    return str((item.get("shot") or {}).get("id") or item.get("id") or "").strip()


def _storyboard_result_status(storyboard_dir, plan):
    status = {
        "path": os.path.join(storyboard_dir, "storyboard_result.json"),
        "exists": False,
        "shot_ids": [],
        "missing": _plan_shot_ids(plan),
        "missing_files": [],
        "covers_plan": False,
        "needs_confirmation": None,
    }
    if not plan:
        return status
    if not os.path.isfile(status["path"]):
        return status
    try:
        result = _load_json(status["path"], {})
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        status["error"] = str(error)
        return status
    status["exists"] = True
    status["needs_confirmation"] = bool(result.get("needs_confirmation"))
    by_id = {}
    missing_files = []
    for item in result.get("shots") or []:
        shot_id = _storyboard_item_id(item)
        if not shot_id:
            continue
        by_id[shot_id] = item
        path = item.get("abspath") or item.get("path")
        if path and not os.path.isabs(path):
            path = os.path.abspath(path)
        if not path or not os.path.isfile(path):
            missing_files.append(shot_id)
    expected = _plan_shot_ids(plan)
    missing = [shot_id for shot_id in expected if shot_id not in by_id]
    status.update({
        "shot_ids": sorted(by_id),
        "missing": missing,
        "missing_files": sorted(set(missing_files)),
        "covers_plan": not missing and not missing_files and bool(expected),
    })
    return status


def _expected_prompt_ids(segments, plan):
    if plan:
        plan_ids = [str(shot.get("id")) for shot in plan.get("shots") or [] if shot.get("id")]
        if plan_ids:
            return set(plan_ids), "storyboard_plan"
    return {str(segment.get("id")) for segment in segments}, "segments"


def _check_prompt_review(report, segments, review, plan=None):
    ids = {str(segment.get("id")) for segment in segments}
    expected_ids, expected_source = _expected_prompt_ids(segments, plan)
    items = _prompt_items(review)
    integrity_error = ""
    try:
        prompt_review_contract.assert_current(review)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        integrity_error = str(error)
    _add(report, "prompt_review_integrity_current", not integrity_error,
         "已确认提示词审核文件必须与确认时的 JSON 内容指纹一致；确认后被修改必须重新捕获并确认。",
         evidence={
             "error": integrity_error,
             "stored": (review or {}).get("review_fingerprint"),
             "current": (prompt_review_contract.review_fingerprint(review)
                         if isinstance(review, dict) else None),
         })
    _add(report, "prompt_review_confirmed",
         review.get("status") == "confirmed" and review.get("stage") == "video",
         "视频提示词审核必须是 confirmed/video。",
         evidence={"status": review.get("status"), "stage": review.get("stage")})
    missing = sorted(ids - set(items))
    _add(report, "prompt_review_covers_segments", not missing,
         "提示词审核必须覆盖所有视频段。", evidence={"missing": missing})
    missing_expected = sorted(expected_ids - set(items))
    _add(report, "prompt_review_covers_expected_shots", not missing_expected,
         "视频提示词审核必须覆盖完整 storyboard_plan，而不能只覆盖旧 handoff 中已有片段。",
         evidence={"missing": missing_expected, "expected_source": expected_source})

    missing_seedance, missing_kling, weak_rules = [], [], []
    for segment in segments:
        segment_id = str(segment.get("id"))
        item = items.get(segment_id) or {}
        model_prompts = item.get("model_submission_prompts") or {}
        seedance_text = model_prompts.get("seedance-2.0") or model_prompts.get("seedance")
        kling_text = model_prompts.get("kling-v3-omni-video") or model_prompts.get("kling")
        if not seedance_text:
            missing_seedance.append(segment_id)
        if not kling_text:
            missing_kling.append(segment_id)
        for text in _submission_prompt_texts(item):
            lowered = text.lower()
            if ("never render" not in lowered or "photorealistic" not in lowered or
                    "audio continuity:" not in lowered):
                weak_rules.append(segment_id)
                break
    _add(report, "seedance_native_prompts_present", not missing_seedance,
         "Seedance 主路径必须有客户确认过的完整提交提示词。",
         evidence={"missing": missing_seedance})
    _add(report, "kling_fallback_prompts_present", not missing_kling,
         "Kling fallback 必须有客户确认过的完整提交提示词。",
         evidence={"missing": missing_kling})
    _add(report, "submission_prompts_keep_video_rules", not weak_rules,
         "完整提交提示词必须保留禁线稿/禁故事板成片/音频连续性规则。",
         evidence={"weak_segment_ids": sorted(set(weak_rules))})


def _check_audio_contract(report, segments):
    bad = []
    for segment in segments:
        audio = segment.get("audio_contract") or {}
        if audio.get("track") != "required":
            continue
        if audio.get("voice_continuity_method") != "text_contract_and_human_qc":
            bad.append([segment.get("id"), "voice_continuity_method"])
        if audio.get("bgm") and audio.get("bgm_continuity_method") != "post_mix_preferred":
            bad.append([segment.get("id"), "bgm_continuity_method"])
        if (audio.get("media_reference_method") !=
                "basicrouter_video_v1_has_no_public_audio_reference_field"):
            bad.append([segment.get("id"), "media_reference_method"])
    _add(report, "audio_continuity_methods_explicit", not bad,
         "声音/BGM 连续性必须显式声明方法，避免误称已上传音频参考。",
         evidence={"bad": bad})


def _needs_magnetic_phone_contract(text):
    lowered = text.lower()
    has_magnetic = "磁吸" in text or "magnetic" in lowered or "magsafe" in lowered
    has_phone_back = ("手机背面" in text or "手机背" in text or
                      "phone back" in lowered or "smartphone back" in lowered)
    has_attach_action = any(term in text for term in ("贴", "贴合", "吸附", "连接", "靠近")) or any(
        term in lowered for term in ("attach", "snap", "mount", "connect", "stick"))
    return has_magnetic and has_phone_back and has_attach_action


def _check_magnetic_contract(report, segments, review):
    offenders = []
    segment_ids = {str(segment.get("id")) for segment in segments}
    segment_blobs = {str(segment.get("id")): _text_blob(segment) for segment in segments}
    magnetic_action_ids = {
        segment_id for segment_id, blob in segment_blobs.items()
        if _needs_magnetic_phone_contract(blob)
    }
    for segment in segments:
        segment_id = str(segment.get("id"))
        if segment_id not in magnetic_action_ids:
            continue
        blob = segment_blobs[segment_id]
        has_bottom = "底部" in blob or "bottom" in blob.lower()
        forbidden = ("产品背部贴合手机" in blob or "产品背部贴合" in blob or
                     "back of the product attaches" in blob.lower())
        if not has_bottom or forbidden:
            offenders.append({"segment_id": segment.get("id"),
                              "has_bottom": has_bottom,
                              "forbidden_back_attachment": forbidden})
    for segment_id, item in _prompt_items(review).items():
        if segment_id not in segment_ids:
            continue
        if segment_id not in magnetic_action_ids:
            continue
        text = _text_blob(_all_prompt_texts(item))
        if "底部" not in text and "bottom" not in text.lower():
            offenders.append({"segment_id": segment_id,
                              "prompt_missing_bottom": True})
        if "产品背部贴合手机" in text or "产品背部贴合" in text:
            offenders.append({"segment_id": segment_id,
                              "prompt_forbidden_back_attachment": True})
    _add(report, "magnetic_bottom_to_phone_back_contract", not offenders,
         "磁吸手机镜头必须写成音响底部磁吸面贴合手机背面，不能写成产品背部贴手机。",
         evidence={"offenders": offenders})


def _check_results(report, segments, results):
    if results is None:
        _add(report, "video_results_supplied", False,
             "post 模式必须提供 video_engine --results-out 结果。")
        return
    by_id = {str(item.get("segment_id") or item.get("id")): item for item in results}
    missing = [str(segment.get("id")) for segment in segments
               if str(segment.get("id")) not in by_id]
    _add(report, "video_results_cover_segments", not missing,
         "生成结果必须覆盖所有视频段。", evidence={"missing": missing})
    bad_ok, missing_files, ocr_bad, media_bad = [], [], [], []
    for segment in segments:
        result = by_id.get(str(segment.get("id"))) or {}
        if result.get("ok") is not True:
            bad_ok.append(segment.get("id"))
        path = result.get("localPath") or result.get("absPath")
        if not path or not os.path.isfile(path):
            missing_files.append(segment.get("id"))
        if result.get("ocr_warning"):
            ocr_bad.append({"segment_id": segment.get("id"),
                            "texts": result.get("ocr_texts") or []})
        media_qc = result.get("media_qc") or {}
        if media_qc and media_qc.get("passed") is not True:
            media_bad.append(segment.get("id"))
    _add(report, "all_results_ok", not bad_ok, "每段生成结果必须 ok=true。",
         evidence={"bad": bad_ok})
    _add(report, "local_video_files_exist", not missing_files,
         "每段必须有已下载的本地视频文件。", evidence={"missing": missing_files})
    _add(report, "ocr_clear", not ocr_bad,
         "OCR 检出画面文字时必须阻断交付。", evidence={"detected": ocr_bad})
    _add(report, "media_qc_passed_or_pending", not media_bad,
         "如结果包含 media_qc，则必须 passed=true。", evidence={"bad": media_bad})


def _check_manual_review(report, manual_review):
    if not manual_review:
        _add(report, "manual_design_review_supplied", False,
             "产品造型、磁吸接触关系、声线/BGM 连续性必须人工或视觉模型复核。")
        return
    checks = manual_review.get("checks") or {}
    missing = [key for key in REQUIRED_MANUAL_CHECKS if key not in checks]
    failed = [key for key in REQUIRED_MANUAL_CHECKS if checks.get(key) is not True]
    _add(report, "manual_design_review_complete", not missing,
         "人工/视觉复核项必须完整。", evidence={"missing": missing})
    _add(report, "manual_design_review_passed", not failed,
         "所有人工/视觉复核项必须通过。", evidence={"failed": failed})


def build_report(segments_path, prompt_review_path, *, mode="preflight",
                 results_path=None, manual_review_path=None, manifest_path=None,
                 client=None, plan_path=None):
    segments_doc = _load_json(segments_path)
    segments = _segments(segments_doc)
    review = _load_json(prompt_review_path, {})
    plan = _load_json(plan_path, None) if plan_path else None
    results = _load_json(results_path, None) if results_path else None
    manual_review = _load_json(manual_review_path, None) if manual_review_path else None
    run_dir = os.path.dirname(os.path.abspath(segments_path))
    run_id = os.path.basename(run_dir)
    storyboard_base_dir = os.path.abspath(os.path.join(run_dir, "..", "storyboard"))
    inferred_storyboard_dir = (
        storyboard.resolve_current_storyboard_dir(storyboard_base_dir, run_id, plan) or
        os.path.join(storyboard_base_dir, run_id)
    )
    inferred_storyboard_dir = os.path.abspath(inferred_storyboard_dir)
    report = {
        "schema_version": 1,
        "mode": mode,
        "client": client,
        "run_id": run_id,
        "segments_path": os.path.abspath(segments_path),
        "prompt_review_path": os.path.abspath(prompt_review_path),
        "results_path": os.path.abspath(results_path) if results_path else None,
        "manual_review_path": os.path.abspath(manual_review_path) if manual_review_path else None,
        "plan_path": os.path.abspath(plan_path) if plan_path else None,
        "manifest_path": os.path.abspath(manifest_path) if manifest_path else None,
        "storyboard_dir": inferred_storyboard_dir,
        "storyboard_result_status": _storyboard_result_status(inferred_storyboard_dir, plan),
        "segment_count": len(segments),
        "required_manual_checks": list(REQUIRED_MANUAL_CHECKS),
        "checks": [],
        "errors": [],
        "warnings": [],
        "next_actions": [],
        "generation_ready": False,
        "passed": False,
    }
    _add(report, "segments_nonempty", bool(segments), "必须至少有一个视频段。")
    _check_segments_declared_complete(report, segments_doc)
    _check_reference_handoff(report, segments)
    _check_video_reference_urls(report, segments)
    _check_render_plan_storyboard_binding(
        report, segments, report.get("storyboard_result_status"))
    _check_plan_coverage(report, segments, plan)
    _check_manifest(report, manifest_path, client)
    _check_prompt_review(report, segments, review, plan=plan)
    _check_audio_contract(report, segments)
    _check_magnetic_contract(report, segments, review)
    report["generation_ready"] = not report["errors"]
    if mode == "post":
        _check_results(report, segments, results)
        _check_manual_review(report, manual_review)
    report["passed"] = not report["errors"]
    report["next_actions"] = next_actions(report)
    return report


def _video_preflight_path(report):
    return os.path.join(
        os.path.dirname(report.get("segments_path") or "."),
        "video_preflight.json")


def _video_prompt_recapture_commands(report):
    segments = report.get("segments_path") or "<segments.json>"
    preflight = _video_preflight_path(report)
    review = report.get("prompt_review_path") or "<prompt_review_video.json>"
    preview = os.path.join(
        os.path.dirname(report.get("prompt_review_path") or "."),
        "video_submission_prompt_preview.md")
    return [
        "python3 scripts/video_engine.py --batch %s --preflight --preflight-out %s" %
        (segments, preflight),
        "python3 scripts/prompt_review.py capture-video --plan %s --segments %s "
        "--preflight-json %s --out %s --preview-out %s" %
        (report.get("plan_path") or "<storyboard_plan.json>", segments,
         preflight, review, preview),
    ]


def _action_storyboard_coverage(report, errors, checks, stale_handoff, actions):
    if errors & {"segments_cover_storyboard_plan", "segments_no_declared_missing_images"}:
        missing = ((checks.get("segments_cover_storyboard_plan") or {})
                   .get("evidence") or {}).get("missing") or []
        declared = ((checks.get("segments_no_declared_missing_images") or {})
                    .get("evidence") or {})
        missing = sorted(set(
            list(missing) +
            list(declared.get("missing_images") or []) +
            list(declared.get("needs_image") or [])))
        storyboard_status = report.get("storyboard_result_status") or {}
        reusable_storyboard = (
            storyboard_status.get("exists") and
            storyboard_status.get("covers_plan") and
            not storyboard_status.get("missing_files") and
            not any(shot_id not in set(storyboard_status.get("shot_ids") or [])
                    for shot_id in missing)
        )
        if reusable_storyboard:
            actions.append({
                "code": "CONFIRM_REGENERATED_STORYBOARD",
                "message": "缺失 shot 的故事板图已存在；展示给用户确认后刷新 handoff，不要重复付费生成。",
                "shot_ids": missing,
                "requires_paid_generation": False,
                "commands": [
                    "# 先用 Markdown/预览页展示已生成的故事板图，用户确认后运行:",
                    "python3 scripts/storyboard.py --confirm --result-json %s --customer-confirmation '<客户确认原话>'" %
                    storyboard_status.get("path"),
                    "# 然后执行 REFRESH_APPROVAL_CHAIN 里的 manifest 刷新和重新 split。",
                ],
            })
        else:
            storyboard_review = os.path.join(
                os.path.dirname(report.get("segments_path") or "."),
                "storyboard_prompt_review_refresh.json")
            storyboard_preview = os.path.join(
                os.path.dirname(report.get("segments_path") or "."),
                "storyboard_prompt_review_refresh.md")
            actions.append({
                "code": "REGENERATE_STALE_STORYBOARD_SHOTS",
                "message": "重新生成并确认缺失或过期的故事板 shot，再重新 split。",
                "shot_ids": missing,
                "requires_paid_generation": True,
                "commands": [
                    "# 非付费：捕获 storyboard.py 将提交给 gpt-image-2 的完整提示词预览:",
                    "python3 scripts/prompt_review.py capture-storyboard --plan %s --out %s --preview-out %s" %
                    (report.get("plan_path") or "<storyboard_plan.json>",
                     storyboard_review, storyboard_preview),
                    "# 用户确认 storyboard 提示词后运行:",
                    "python3 scripts/prompt_review.py confirm --review %s --customer-confirmation '<客户确认原话>'" % storyboard_review,
                    "# 会调用 gpt-image-2；确认后再运行:",
                    "python3 scripts/storyboard.py --plan %s --out-dir %s --run-id %s --stage storyboard --model gpt-image-2 --prompt-review %s %s--json" %
                    (report.get("plan_path") or "<storyboard_plan.json>",
                     os.path.dirname(report.get("storyboard_dir") or "<storyboard_out_dir>"),
                     report.get("run_id") or "<run_id>", storyboard_review,
                     (" ".join("--force-shot %s" % shot_id for shot_id in missing) + " ")
                     if missing else ""),
                ],
            })


def _action_reference_handoff(report, errors, checks, stale_handoff, actions):
    if "reference_handoff_complete" in errors:
        actions.append({
            "code": "RECOMPILE_VIDEO_REFERENCES",
            "message": "重新 split，让已确认人物板、产品板、产品使用图和故事板真实进入每段 references。",
            "requires_paid_generation": False,
            "commands": [
                "python3 scripts/script_splitter.py split --plan %s --storyboard-dir %s --out %s --client %s --run-id %s --manifest %s" %
                (report.get("plan_path") or "<storyboard_plan.json>",
                 report.get("storyboard_dir") or "<storyboard_dir>",
                 report.get("segments_path") or "<segments.json>",
                 report.get("client") or "<client>",
                 report.get("run_id") or "<run_id>",
                 report.get("manifest_path") or "<run_manifest.json>"),
            ] + _video_prompt_recapture_commands(report),
        })


def _action_prompt_integrity(report, errors, checks, stale_handoff, actions):
    if "prompt_review_integrity_current" in errors and not stale_handoff:
        actions.append({
            "code": "RECAPTURE_VIDEO_PROMPTS",
            "message": "视频提示词审核文件已确认后被修改或缺少内容指纹；重新捕获、展示并让用户确认完整 Seedance/Kling 提交提示词。",
            "requires_paid_generation": False,
            "commands": _video_prompt_recapture_commands(report) + [
                "python3 scripts/prompt_review.py confirm --review %s --customer-confirmation '<客户确认原话>'" %
                (report.get("prompt_review_path") or "<prompt_review_video.json>"),
            ],
        })


def _action_manifest_gate(report, errors, checks, stale_handoff, actions):
    if "manifest_video_gate" in errors:
        manifest_path = report.get("manifest_path") or "<run_manifest.json>"
        plan_path = report.get("plan_path") or "<storyboard_plan.json>"
        storyboard_dir = report.get("storyboard_dir") or "<storyboard_dir>"
        run_dir = os.path.dirname(report.get("segments_path") or ".")
        storyboard_result = os.path.join(storyboard_dir, "storyboard_result.json")
        cast_board = os.path.join(storyboard_dir, "cast_board.jpg")
        product_usage = os.path.join(storyboard_dir, "product_usage_board.jpg")
        render_plan = os.path.join(run_dir, "render_plan.json")
        actions.append({
            "code": "REFRESH_APPROVAL_CHAIN",
            "message": "脚本/素材/故事板/render plan 的 artifact 指纹已变化；重新生成或重新确认相关阶段后再登记 video handoff。",
            "requires_paid_generation": False,
            "commands": [
                "# 非付费：重新登记并确认已修改的 storyboard_plan/script artifact:",
                "python3 scripts/run_manifest.py finish-stage --manifest %s --stage script --path %s" %
                (manifest_path, plan_path),
                "python3 scripts/pipeline.py approve --manifest %s --stage script --customer-confirmation '<客户确认原话>'" %
                manifest_path,
                "# 非付费：重新展示人物板与产品使用图；用户确认后只走正式原子确认入口:",
                "python3 scripts/storyboard.py --confirm-board cast --result-json %s --customer-confirmation '<客户确认原话>'" %
                storyboard_result,
                "python3 scripts/storyboard.py --confirm-board usage --result-json %s --geometry-review %s --customer-confirmation '<客户确认原话>'" %
                (storyboard_result, os.path.join(storyboard_dir, "usage_geometry_review.json")),
                "# 故事板图片展示给用户确认后运行:",
                "python3 scripts/storyboard.py --confirm --result-json %s --customer-confirmation '<客户确认原话>'" %
                storyboard_result,
                "# 非付费：render_plan 未改内容但受 storyboard 上游指纹影响，需要重新登记确认:",
                "python3 scripts/run_manifest.py finish-stage --manifest %s --stage render_plan --path %s" %
                (manifest_path, render_plan),
                "python3 scripts/pipeline.py approve --manifest %s --stage render_plan --customer-confirmation '<客户确认完整提示词原话>'" %
                manifest_path,
                "python3 scripts/script_splitter.py split --plan %s --storyboard-dir %s --out %s --client %s --run-id %s --manifest %s" %
                (plan_path,
                 storyboard_dir,
                 report.get("segments_path") or "<segments.json>",
                 report.get("client") or "<client>",
                 report.get("run_id") or "<run_id>",
                 manifest_path),
            ],
        })


def _action_magnetic_contract(report, errors, checks, stale_handoff, actions):
    if "magnetic_bottom_to_phone_back_contract" in errors or stale_handoff:
        actions.append({
            "code": "RECAPTURE_VIDEO_PROMPTS",
            "message": (
                "重新捕获视频提示词，确保覆盖完整最新 segments；磁吸关系必须写成音响底部磁吸面贴合手机背面。"
                if stale_handoff else
                "重新捕获视频提示词，确保磁吸关系写成音响底部磁吸面贴合手机背面。"),
            "depends_on": ["REFRESH_APPROVAL_CHAIN"] if stale_handoff else [],
            "requires_paid_generation": False,
            "commands": _video_prompt_recapture_commands(report),
        })


def _action_remote_urls(report, errors, checks, stale_handoff, actions):
    if "video_reference_urls_are_remote" in errors:
        segments_path = report.get("segments_path") or "<segments.json>"
        run_dir = os.path.dirname(segments_path)
        stem = os.path.splitext(os.path.basename(segments_path))[0]
        recovered_segments = os.path.join(run_dir, "%s.url_recovered.json" % stem)
        recovery_report = os.path.join(run_dir, "video_image_url_recovery_report.md")
        actions.append({
            "code": "RESTORE_VIDEO_IMAGE_URLS",
            "message": "从 BasicRouter 图片生成 retrieve 结果恢复每张确认图的 URL，并重新 split；恢复不了的图必须重新生成并重新确认，不能用本地路径/base64/图片重绘伪托管。",
            "requires_paid_generation": False,
            "commands": [
                "# 非付费：先从 storyboard_result.json、各 *_state.json、确认文件里的 result_url/url 恢复视频 imageUrls。",
                "python3 scripts/video_image_url_recovery.py --segments %s --storyboard-dir %s --out %s --report %s --plan-out %s --client %s --run-id %s --fail-on-missing" %
                (segments_path,
                 report.get("storyboard_dir") or "<storyboard_dir>",
                 recovered_segments,
                 recovery_report,
                 os.path.join(run_dir, "video_image_url_recovery_plan.json"),
                 report.get("client") or "<client>",
                 report.get("run_id") or "<run_id>"),
                "# 若只有本地路径而无 BasicRouter 图片 URL：停止视频生成，重新生成缺失素材并让用户重新确认。",
                "# 恢复成功后，用恢复后的 segments 再跑一次 preflight；不要再用旧 segments 直接生成视频。",
                "python3 scripts/video_effect_qc.py --plan %s --segments %s --prompt-review %s --manifest %s --client %s --mode preflight" %
                (report.get("plan_path") or "<storyboard_plan.json>",
                 recovered_segments,
                 report.get("prompt_review_path") or "<prompt_review_video.json>",
                 report.get("manifest_path") or "<run_manifest.json>",
                 report.get("client") or "<client>"),
                "python3 scripts/script_splitter.py split --plan %s --storyboard-dir %s --out %s --client %s --run-id %s --manifest %s" %
                (report.get("plan_path") or "<storyboard_plan.json>",
                 report.get("storyboard_dir") or "<storyboard_dir>",
                 report.get("segments_path") or "<segments.json>",
                 report.get("client") or "<client>",
                 report.get("run_id") or "<run_id>",
                 report.get("manifest_path") or "<run_manifest.json>"),
            ],
        })


def _action_prompt_confirmation(report, errors, checks, stale_handoff, actions):
    if "prompt_review_confirmed" in errors and not stale_handoff:
        actions.append({
            "code": "CONFIRM_VIDEO_PROMPT_REVIEW",
            "message": "展示新的完整 Seedance/Kling 提示词，用户确认后再生成视频。",
            "requires_paid_generation": False,
            "commands": [
                "python3 scripts/prompt_review.py confirm --review %s --customer-confirmation '<客户确认原话>'" %
                (report.get("prompt_review_path") or "<prompt_review_video.json>"),
            ],
        })


def _action_video_generation(report, errors, checks, stale_handoff, actions):
    if "video_results_supplied" in errors:
        actions.append({
            "code": "RUN_VIDEO_GENERATION",
            "message": "完成 preflight 后运行 video_engine，并传入 --results-out。",
            "requires_paid_generation": True,
        })


def _action_manual_review(report, errors, checks, stale_handoff, actions):
    if "manual_design_review_supplied" in errors:
        actions.append({
            "code": "REVIEW_GENERATED_TAKES",
            "message": "对每段成片记录产品造型、底部磁吸关系、分镜执行、声线/BGM、口型和无文字复核。",
            "required_checks": report.get("required_manual_checks") or [],
            "requires_paid_generation": False,
        })


_NEXT_ACTION_RULES = (
    _action_storyboard_coverage,
    _action_reference_handoff,
    _action_prompt_integrity,
    _action_manifest_gate,
    _action_magnetic_contract,
    _action_remote_urls,
    _action_prompt_confirmation,
    _action_video_generation,
    _action_manual_review,
)


def next_actions(report):
    """Translate failing checks into deterministic recovery actions."""
    errors = set(report.get("errors") or [])
    actions = []
    checks = {item.get("id"): item for item in report.get("checks") or []}
    stale_handoff = bool(errors & {
        "segments_cover_storyboard_plan",
        "segments_no_declared_missing_images",
        "prompt_review_covers_expected_shots",
        "render_plan_storyboard_result_current",
    })
    for rule in _NEXT_ACTION_RULES:
        rule(report, errors, checks, stale_handoff, actions)
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate video design-effect handoff and final takes")
    parser.add_argument("--segments", required=True)
    parser.add_argument("--prompt-review", required=True)
    parser.add_argument("--mode", choices=["preflight", "post"], default="preflight")
    parser.add_argument("--results")
    parser.add_argument("--manual-review")
    parser.add_argument("--manifest")
    parser.add_argument("--plan")
    parser.add_argument("--client")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    report = build_report(
        args.segments, args.prompt_review, mode=args.mode,
        results_path=args.results, manual_review_path=args.manual_review,
        manifest_path=args.manifest, client=args.client, plan_path=args.plan)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    print(text)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
