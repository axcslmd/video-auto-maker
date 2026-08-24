#!/usr/bin/env python3
"""Commercial delivery preflight for deterministic document explainers.

This checker deliberately separates deterministic guarantees (layout, hashes,
timing, alpha and input contracts) from probabilistic video-model quality.  It
does not call a model and is safe to run repeatedly at preview, approval and
final-delivery stages.
"""
import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import video_use_bridge as bridge  # noqa: E402
import content_scaffold  # noqa: E402
import motion_design  # noqa: E402


AUDITOR_VERSION = "document-explainer-qc-final-frame-binding-v2"


def _auditor_source_sha():
    with open(__file__, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


LIMITATIONS = [
    {
        "id": "video_model_identity_and_lipsync",
        "guarantee": "probabilistic",
        "cannot_guarantee": "逐帧人脸、口型和微表情与静态人物板完全一致",
        "mitigation": "已确认身份锚 + type4/5 图生视频 + 正式 take/QC/OCR/客户验片 + 受控重试",
    },
    {
        "id": "video_model_physics",
        "guarantee": "probabilistic",
        "cannot_guarantee": "复杂手部、磁吸、卡扣、佩戴和承重关系在动态中零错误",
        "mitigation": "逐格使用图确认、接触面合同、短镜头、失败镜头单独重生",
    },
    {
        "id": "native_text_and_logo",
        "guarantee": "not_supported_as_generated_overlay",
        "cannot_guarantee": "生成模型内的文字、Logo 和界面内容稳定无变形",
        "mitigation": "干净底片；所有非原生文字和品牌叠层由 HyperFrames/Remotion 确定性完成",
    },
    {
        "id": "vendor_availability_cost_latency",
        "guarantee": "external_dependency",
        "cannot_guarantee": "第三方模型实时可用、固定延迟、固定价格或一次生成即通过",
        "mitigation": "能力探测、模型降级、异步批量、限流重试和费用确认闸门",
    },
]


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _sha(path):
    return bridge._sha256_file(path)


def _check_file(record, label, blockers, *, require_alpha=False):
    if not isinstance(record, dict) or not record.get("path"):
        blockers.append({"code": "%s_MISSING" % label.upper(), "message": "%s 缺失" % label})
        return None
    path = os.path.abspath(record["path"])
    if not os.path.isfile(path):
        blockers.append({"code": "%s_FILE_MISSING" % label.upper(), "message": path})
        return None
    if record.get("sha256") and _sha(path) != record["sha256"]:
        blockers.append({"code": "%s_STALE" % label.upper(), "message": path})
    if require_alpha and not bridge._media_has_alpha(path):
        blockers.append({"code": "%s_ALPHA_REQUIRED" % label.upper(), "message": path})
    return path


def _segments(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("segments", "shots", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _shots_by_id(payload):
    if not isinstance(payload, dict):
        return {}
    source = payload.get("shots") or payload.get("segments") or payload.get("scenes") or []
    return {
        str(item.get("id") or item.get("shot_id") or index): item
        for index, item in enumerate(source, 1) if isinstance(item, dict)
    }


def _subtitle_partial_ascii_word(text, dialogue):
    caption_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(text or ""))
    dialogue_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(dialogue or ""))
    lowered = {word.lower() for word in dialogue_words}
    for word in caption_words:
        candidate = word.lower()
        if candidate in lowered:
            continue
        if any(full.startswith(candidate) and len(full) > len(candidate) for full in lowered):
            return word
    return None


def _check_final_frame_visual_qa(execution, manifest_binding, blockers, checks):
    """Reload the bound QA bytes and compare every approved frame identity."""
    qa_binding = manifest_binding.get("final_frame_visual_qa")
    if isinstance(qa_binding, str):
        qa_binding = {"path": qa_binding}
    if not isinstance(qa_binding, dict) or not qa_binding.get("path"):
        blockers.append({
            "code": "FINAL_FRAME_VISUAL_QA_REQUIRED",
            "message": "执行预演缺少逐帧视觉 QA 绑定",
        })
        return
    qa_path = os.path.abspath(str(qa_binding["path"]))
    if not os.path.isfile(qa_path):
        blockers.append({"code": "FINAL_FRAME_VISUAL_QA_MISSING",
                         "message": qa_path})
        return
    qa_sha = _sha(qa_path)
    if qa_binding.get("sha256") != qa_sha:
        blockers.append({"code": "FINAL_FRAME_VISUAL_QA_STALE",
                         "message": qa_path})
        return
    qa = _load(qa_path)
    checks["final_frame_visual_qa"] = {
        "path": qa_path, "sha256": qa_sha, "passed": qa.get("passed"),
        "reviewer_type": qa.get("reviewer_type"),
        "customer_confirmed": qa.get("customer_confirmed"),
    }
    if (not qa.get("passed") or
            qa.get("reviewer_type") not in {"agent", "human"} or
            qa.get("automated_visual_claim") is not False or
            qa.get("customer_visual_confirmation_required") is not True or
            qa.get("customer_confirmed") is not False):
        blockers.append({"code": "FINAL_FRAME_VISUAL_QA_INVALID",
                         "message": qa_path})
    if (qa.get("reviewer_type") == "agent" and
            qa.get("method") != "manual_frame_by_frame_agent_visual_review"):
        blockers.append({"code": "FINAL_FRAME_VISUAL_QA_AGENT_METHOD_INVALID",
                         "message": qa_path})
    expected = {str(item.get("id")): item
                for item in execution.get("layout_boards") or []}
    actual = {str(item.get("id")): item for item in qa.get("shots") or []}
    if not expected or set(expected) != set(actual):
        blockers.append({"code": "FINAL_FRAME_VISUAL_QA_SHOT_SET_MISMATCH",
                         "message": qa_path})
        return
    for shot_id, board in expected.items():
        checked = actual[shot_id]
        subtitle_valid = (
            not checked.get("subtitle_required") or
            bool(str(checked.get("expected_subtitle_text") or "").strip()))
        if (not checked.get("passed") or not subtitle_valid or
                checked.get("sha256") != board.get("sha256") or
                checked.get("animatic_sha256") != board.get("animatic_sha256")):
            blockers.append({
                "code": "FINAL_FRAME_VISUAL_QA_SHOT_STALE_OR_INCOMPLETE",
                "message": shot_id,
            })


def _audit_header(execution, require_manifest_binding, blockers, checks):
    manifest_binding = execution.get("manifest_binding") or {}
    checks["manifest_binding"] = manifest_binding
    if require_manifest_binding and not (
            manifest_binding.get("status") == "bound" and
            manifest_binding.get("approval_eligible") is True and
            manifest_binding.get("manifest_path")):
        blockers.append({
            "code": "EXECUTION_PREVIEW_MANIFEST_BINDING_REQUIRED",
            "message": "正式可确认执行预演必须由权威 run_manifest.json 登记",
        })
    if require_manifest_binding or manifest_binding.get("final_frame_visual_qa"):
        _check_final_frame_visual_qa(
            execution, manifest_binding, blockers, checks)
    if execution.get("type") != "document_explainer_execution_preview":
        blockers.append({"code": "EXECUTION_PREVIEW_TYPE_INVALID", "message": "不是文档讲解执行预演"})
    if int(execution.get("schema_version") or 0) < 2:
        blockers.append({"code": "COMMERCIAL_CONTRACT_V2_REQUIRED", "message": "需重渲新版执行预演"})
    if execution.get("renderer_source_of_truth") != "remotion.IntegratedExplainer":
        blockers.append({"code": "SAME_RENDERER_REQUIRED", "message": "预演与成片渲染器不一致"})


def _audit_files(execution, blockers):
    integrated_path = _check_file(
        execution.get("integrated_spec") or {}, "integrated_spec", blockers)
    _check_file(execution.get("animatic") or {}, "animatic", blockers)
    _check_file(execution.get("content_layer") or {}, "content_layer", blockers)
    _check_file(execution.get("content_contract_spec") or {}, "content_contract_spec", blockers)
    _check_file(execution.get("content_previs_spec") or {}, "content_previs_spec", blockers)
    hyperframes_path = _check_file(
        execution.get("hyperframes_spec") or {}, "hyperframes_spec", blockers)
    for index, board in enumerate(execution.get("layout_boards") or [], 1):
        _check_file(board, "layout_board_%02d" % index, blockers)
    return integrated_path, hyperframes_path


def _audit_integrated_spec(execution, integrated_path, blockers, checks):
    approved_render = execution.get("approved_render_contract") or {}
    if integrated_path:
        spec = _load(integrated_path)
        frozen = bridge._approved_spec_fingerprint(spec)
        if approved_render.get("frozen_fingerprint") != frozen:
            blockers.append({"code": "FROZEN_SPEC_FINGERPRINT_MISMATCH", "message": integrated_path})
        if approved_render.get("spec_sha256") != _sha(integrated_path):
            blockers.append({"code": "FROZEN_SPEC_SHA_MISMATCH", "message": integrated_path})
        scenes = spec.get("scenes") or []
        cursor = 0
        for scene in scenes:
            if int(scene.get("fromFrame") or 0) != cursor:
                blockers.append({"code": "TIMELINE_GAP_OR_OVERLAP", "message": str(scene.get("id"))})
                break
            cursor += max(1, int(scene.get("durationInFrames") or 1))
        checks["timeline_frames"] = cursor
        checks["output_dimensions"] = [spec.get("width"), spec.get("height")]
        output_width = int(spec.get("width") or 0) or None
        output_height = int(spec.get("height") or 0) or None
        human_image = str(spec.get("humanImagePath") or "")
        human_contract = spec.get("humanImageContract") or execution.get("human_preview_source") or {}
        basename = os.path.basename(human_image).lower()
        if human_image and any(token in basename for token in (
                "cast_board", "character_board", "six_view", "6_view")):
            blockers.append({
                "code": "CAST_BOARD_AS_PRESENTER_PLATE_FORBIDDEN",
                "message": human_image,
            })
        if human_contract.get("kind") == "cast_board_front_cell_derivative":
            if (not human_image or not os.path.isfile(human_image) or
                    human_contract.get("preview_path") != human_image or
                    human_contract.get("preview_sha256") != _sha(human_image)):
                blockers.append({
                    "code": "HUMAN_PREVIEW_DERIVATIVE_STALE",
                    "message": human_image,
                })
            if spec.get("humanFit") == "contain":
                for scene in scenes:
                    if scene.get("humanSlot") == "none":
                        continue
                    scale = scene.get("humanScale")
                    if isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale > 1.05:
                        blockers.append({
                            "code": "PORTRAIT_PREVIEW_OVERZOOM",
                            "message": "%s humanScale=%s" % (scene.get("id"), scale),
                        })

        if any(scene.get("visualMode") == "split_left_right" for scene in scenes):
            split_contract = (spec.get("_composition_contract") or {}).get("split_layout") or {}
            if not (
                    split_contract.get("content_clip_enabled") is True and
                    split_contract.get("presenter_style") == "editorial_card" and
                    isinstance(split_contract.get("content_fraction"), (int, float)) and
                    isinstance(split_contract.get("presenter_fraction"), (int, float)) and
                    float(split_contract.get("content_fraction")) +
                    float(split_contract.get("presenter_fraction")) <= 0.98):
                blockers.append({
                    "code": "SPLIT_LAYOUT_REGION_CONTRACT_REQUIRED",
                    "message": "左右分栏必须使用互斥裁切区和 editorial card 人物区",
                })

        hero_scenes = [scene for scene in scenes if scene.get("visualMode") in {
            "host_full_bleed_text", "cutout_presenter"}]
        if hero_scenes:
            hero_contract = (spec.get("_composition_contract") or {}).get(
                "hero_layout") or {}
            if not (
                    hero_contract.get("content_clip_enabled") is True and
                    hero_contract.get("presenter_style") == "editorial_card" and
                    isinstance(hero_contract.get("content_fraction"), (int, float)) and
                    float(hero_contract.get("content_fraction")) <= 0.48 and
                    isinstance(hero_contract.get("gap_fraction"), (int, float)) and
                    float(hero_contract.get("gap_fraction")) >= 0.03):
                blockers.append({
                    "code": "HERO_LAYOUT_REGION_CONTRACT_REQUIRED",
                    "message": "Hero 内容与人物卡必须使用共享互斥边界",
                })
            for scene in hero_scenes:
                scale = scene.get("contentScale")
                shift = scene.get("contentShiftX")
                if not (
                        isinstance(scale, (int, float)) and not isinstance(scale, bool) and
                        float(scale) <= 0.64 and
                        isinstance(shift, (int, float)) and not isinstance(shift, bool) and
                        (not output_width or float(shift) <= -float(output_width) * 0.21)):
                    blockers.append({
                        "code": "HERO_CONTENT_CROSSES_PRESENTER_REGION",
                        "message": "%s contentScale=%r contentShiftX=%r" % (
                            scene.get("id"), scale, shift),
                    })

        source_plan_path = execution.get("source_plan")
        if source_plan_path and os.path.isfile(source_plan_path):
            authored = _shots_by_id(_load(source_plan_path))
            compiled = _shots_by_id({"shots": scenes})
            for shot_id, raw in authored.items():
                scene = compiled.get(shot_id) or {}
                for source_key, compiled_key in (
                        ("component", "component"),
                        ("slot", "humanSlot"),
                        ("camera", "cameraMovement")):
                    expected = raw.get(source_key)
                    if expected is not None and str(scene.get(compiled_key) or "") != str(expected):
                        blockers.append({
                            "code": "DIRECTOR_FIELD_NOT_COMPILED",
                            "message": "%s.%s=%r -> %s=%r" % (
                                shot_id, source_key, expected, compiled_key, scene.get(compiled_key)),
                        })
    return output_width, output_height


def _audit_hyperframes_spec(execution, hyperframes_path, blockers, output_width):
    renderable_motion = int((execution.get("hyperframes_spec") or {}).get(
        "renderable_scenes") or 0)
    if hyperframes_path:
        hf_spec = _load(hyperframes_path)
        visibility = hf_spec.get("visible_layer_contract") or {}
        if (int(visibility.get("schema_version") or 0) < 1 or
                visibility.get("ok") is not True or visibility.get("errors")):
            blockers.append({
                "code": "VISIBLE_LAYER_CONTRACT_REQUIRED",
                "message": json.dumps(visibility.get("errors") or [], ensure_ascii=False),
            })
        source_plan = {}
        source_plan_path = execution.get("source_plan")
        if source_plan_path and os.path.isfile(source_plan_path):
            source_plan = _shots_by_id(_load(source_plan_path))
        seen_copy = set()
        subtitle_shot_ids = set()
        for scene in hf_spec.get("scenes") or []:
            if (scene.get("source_kind") != "audience_copy" or
                    scene.get("owner") != "hyperframes"):
                blockers.append({
                    "code": "VISIBLE_LAYER_SOURCE_OR_OWNER_INVALID",
                    "message": str(scene.get("semantic_id") or scene.get("slot")),
                })
            key = (
                str(scene.get("source_shot_id") or ""),
                re.sub(r"[\W_]+", "", str(scene.get("text") or "").lower(), flags=re.UNICODE),
            )
            if key[1] and key in seen_copy:
                blockers.append({"code": "MOTION_VISIBLE_COPY_DUPLICATED", "message": str(key)})
            seen_copy.add(key)
            if scene.get("slot") == "subtitle" and "bottom_px" not in scene:
                blockers.append({"code": "SUBTITLE_PRECISE_SAFE_ZONE_REQUIRED",
                                 "message": str(scene.get("source_shot_id"))})
            if scene.get("slot") == "subtitle":
                shot_id = str(scene.get("source_shot_id") or "")
                subtitle_shot_ids.add(shot_id)
                if scene.get("contrast_backplate") != "dark_glass":
                    blockers.append({
                        "code": "SUBTITLE_CONTRAST_BACKPLATE_REQUIRED",
                        "message": shot_id,
                    })
                lines = [re.sub(r"[\W_]+", "", line, flags=re.UNICODE)
                         for line in str(scene.get("text") or "").splitlines()]
                if any(line and len(line) <= 1 for line in lines):
                    blockers.append({
                        "code": "SUBTITLE_ORPHAN_LINE_FORBIDDEN",
                        "message": "%s: %r" % (shot_id, scene.get("text")),
                    })
                if len(re.sub(r"\s+", "", str(scene.get("text") or ""))) > 20 and (
                        scene.get("line_break_strategy") != "balanced_semantic"):
                    blockers.append({
                        "code": "SUBTITLE_BALANCED_LINEBREAK_REQUIRED",
                        "message": shot_id,
                    })
                dialogue = str((source_plan.get(shot_id) or {}).get("dialogue") or "")
                text = str(scene.get("text") or "")
                partial = _subtitle_partial_ascii_word(text, dialogue)
                if partial:
                    blockers.append({
                        "code": "SUBTITLE_ASCII_WORD_TRUNCATED",
                        "message": "%s: %s" % (shot_id, partial),
                    })
                # HyperFrames may replace semantic spaces with balanced line
                # breaks.  For Chinese captions those are layout decisions,
                # not copy changes, so compare the compact contract after
                # removing whitespace on both sides.  Punctuation/ellipsis
                # remain significant and still catch substantive mismatches.
                compact_text = re.sub(r"\s+", "", text)
                compact_dialogue = re.sub(r"\s+", "", dialogue)
                if dialogue and len(compact_text) < len(compact_dialogue):
                    expected = motion_design.compact_subtitle_text(dialogue)
                    if compact_text != re.sub(r"\s+", "", expected):
                        blockers.append({
                            "code": "SUBTITLE_EXCERPT_CONTRACT_MISMATCH",
                            "message": "%s: %r != %r" % (shot_id, text, expected),
                        })
                if not scene.get("width_px") or not scene.get("max_height_lines"):
                    blockers.append({
                        "code": "SUBTITLE_TYPOGRAPHY_CONTRACT_REQUIRED",
                        "message": shot_id,
                    })
                width = scene.get("width_px")
                left = scene.get("left_px")
                right = scene.get("right_px")
                canvas_width = int((hf_spec.get("resolution") or [output_width or 0])[0] or 0)
                if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           for value in (width, left, right)):
                    blockers.append({
                        "code": "SUBTITLE_HORIZONTAL_GEOMETRY_REQUIRED",
                        "message": shot_id,
                    })
                elif canvas_width and (
                        abs((float(left) + float(width) + float(right)) - canvas_width) > 4 or
                        abs(float(left) - float(right)) > 4):
                    blockers.append({
                        "code": "SUBTITLE_HORIZONTAL_GEOMETRY_INVALID",
                        "message": "%s: left=%s width=%s right=%s canvas=%s" % (
                            shot_id, left, width, right, canvas_width),
                    })
            if scene.get("slot") == "title_area":
                shot_id = str(scene.get("source_shot_id") or "")
                width = scene.get("width_px")
                left = scene.get("left_px")
                right = scene.get("right_px")
                canvas_width = int((hf_spec.get("resolution") or [output_width or 0])[0] or 0)
                if not (
                        scene.get("variant") == "title" and
                        "top_px" in scene and
                        all(isinstance(value, (int, float)) and not isinstance(value, bool)
                            for value in (width, left, right)) and
                        canvas_width and float(width) <= canvas_width * 0.52 and
                        abs((float(left) + float(width) + float(right)) - canvas_width) <= 4):
                    blockers.append({
                        "code": "TITLE_AREA_GEOMETRY_INVALID",
                        "message": shot_id,
                    })
        for shot_id, source_shot in source_plan.items():
            if str(source_shot.get("dialogue") or "").strip() and shot_id not in subtitle_shot_ids:
                blockers.append({
                    "code": "SUBTITLE_REQUIRED_SHOT_MISSING",
                    "message": shot_id,
                })
    return renderable_motion


def _audit_motion_layer(execution, renderable_motion, blockers, checks, probe_alpha):
    motion = execution.get("motion_layer") or {}
    if renderable_motion:
        motion_path = _check_file(
            motion, "motion_layer", blockers,
            require_alpha=bool(probe_alpha))
        if motion_path and motion.get("has_alpha") is not True:
            blockers.append({"code": "MOTION_ALPHA_CONTRACT_FALSE", "message": motion_path})
        if motion.get("master_path"):
            master_record = {
                "path": motion.get("master_path"),
                "sha256": motion.get("master_sha256"),
            }
            _check_file(master_record, "motion_master", blockers,
                        require_alpha=bool(probe_alpha))
    checks["renderable_motion_scenes"] = renderable_motion


def _audit_content_keyframe_audio(execution, blockers, warnings, checks):
    content_contract_path = (execution.get("content_contract_spec") or {}).get("path")
    if content_contract_path and os.path.isfile(content_contract_path):
        content_validation = content_scaffold.validate(_load(content_contract_path))
        checks["content_spec_validation"] = content_validation
        if not content_validation.get("ok"):
            blockers.append({
                "code": "CONTENT_SPEC_SEMANTIC_INVALID",
                "message": " | ".join(content_validation.get("errors") or []),
            })

    keyframe = execution.get("generation_keyframe_contract") or {}
    if not (keyframe.get("role") == "avatar_plate_only" and
            keyframe.get("clean_plate") is True and
            keyframe.get("storyboard_ref") is False):
        blockers.append({"code": "AVATAR_PLATE_CONTRACT_INVALID", "message": "视频模型输入合同不安全"})

    audio = execution.get("audio_timing_contract") or {}
    if audio.get("source") == "approved_edl":
        warnings.append({
            "code": "MODEL_AUDIO_REQUIRES_POST_GENERATION_TIMING_CHECK",
            "message": "音画一体底片生成后，时长超出 max(0.6s, 2%) 会使本审批失效",
        })
    elif audio.get("matched") is not True:
        blockers.append({"code": "PREVIEW_AUDIO_TIMING_INVALID", "message": "预听轨未对齐 EDL"})


def _audit_approval(stage, approval, blockers):
    if stage in ("approved", "final"):
        if not isinstance(approval, dict) or approval.get("status") != "confirmed":
            blockers.append({"code": "CUSTOMER_APPROVAL_REQUIRED", "message": "缺少客户确认记录"})
        else:
            if (approval.get("confirmed_by_customer") is not True or
                    not str(approval.get("customer_confirmation") or "").strip() or
                    approval.get("confirmation_source") != "customer_dialogue" or
                    not approval.get("confirmed_at") or
                    not approval.get("_approval_file_sha256")):
                blockers.append({
                    "code": "CUSTOMER_APPROVAL_EVIDENCE_INVALID",
                    "message": "客户原话、确认来源、时间或审批文件哈希缺失",
                })
            artifacts = approval.get("execution_artifacts") or {}
            commercial = artifacts.get("commercial_render_contract") or {}
            if commercial.get("formal_render_must_bind_approved_spec") is not True:
                blockers.append({"code": "COMMERCIAL_APPROVAL_CONTRACT_REQUIRED", "message": "确认记录是旧版"})


def _audit_final(stage, final_manifest, segments, renderable_motion, blockers):
    if stage == "final":
        if not isinstance(final_manifest, dict):
            blockers.append({"code": "FINAL_INTEGRATED_MANIFEST_REQUIRED", "message": "缺少最终合成清单"})
        else:
            binding = final_manifest.get("approval_binding") or {}
            if final_manifest.get("formal") is not True:
                blockers.append({"code": "FINAL_RENDER_NOT_FORMAL", "message": "最终合成未启用 formal"})
            if (binding.get("approved_frozen_fingerprint") !=
                    binding.get("bound_frozen_fingerprint")):
                blockers.append({"code": "FINAL_SPEC_DIVERGED", "message": "最终布局偏离审批规格"})
            if (binding.get("customer_approved") is not True or
                    binding.get("confirmed_by_customer") is not True or
                    not str(binding.get("customer_confirmation") or "").strip() or
                    binding.get("confirmation_source") != "customer_dialogue" or
                    not binding.get("confirmed_at") or
                    not binding.get("approval_record_sha256")):
                blockers.append({"code": "FINAL_CUSTOMER_APPROVAL_UNBOUND",
                                 "message": "最终合成没有绑定当前客户确认文件"})
            if not (binding.get("approved_content_contract_spec") or {}).get("sha256"):
                blockers.append({"code": "FINAL_CONTENT_CONTRACT_UNBOUND",
                                 "message": "最终内容层没有绑定审批 content_spec"})
            if renderable_motion and final_manifest.get("motion_has_alpha") is not True:
                blockers.append({"code": "FINAL_MOTION_ALPHA_REQUIRED", "message": "最终动效层无 alpha"})
            out_path = final_manifest.get("out")
            if not out_path or not os.path.isfile(out_path):
                blockers.append({"code": "FINAL_VIDEO_MISSING", "message": str(out_path)})

        for index, segment in enumerate(_segments(segments), 1):
            sid = str(segment.get("id") or index)
            if "type" in segment or "out" in segment:
                blockers.append({"code": "LEGACY_SEGMENT_FIELDS", "message": sid})
            if int(segment.get("video_type") or 0) not in (4, 5):
                blockers.append({"code": "AVATAR_PLATE_VIDEO_TYPE_REQUIRED", "message": sid})
            if (segment.get("storyboard_ref") or segment.get("storyboard_ref_path") or
                    segment.get("storyboardRef")):
                blockers.append({"code": "DESIGN_BOARD_MODEL_REFERENCE_FORBIDDEN", "message": sid})


def audit(execution, *, stage="preview", approval=None, final_manifest=None,
          segments=None, probe_alpha=True, require_manifest_binding=False):
    blockers, warnings, checks = [], [], {}
    _audit_header(execution, require_manifest_binding, blockers, checks)
    integrated_path, hyperframes_path = _audit_files(execution, blockers)
    output_width, _ = _audit_integrated_spec(
        execution, integrated_path, blockers, checks)
    renderable_motion = _audit_hyperframes_spec(
        execution, hyperframes_path, blockers, output_width)
    _audit_motion_layer(
        execution, renderable_motion, blockers, checks, probe_alpha)
    _audit_content_keyframe_audio(execution, blockers, warnings, checks)
    _audit_approval(stage, approval, blockers)
    _audit_final(stage, final_manifest, segments, renderable_motion, blockers)
    return {
        "schema_version": 2,
        "type": "document_explainer_commercial_qc",
        "auditor_version": AUDITOR_VERSION,
        "auditor_source_sha256": _auditor_source_sha(),
        "stage": stage,
        "passed": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "commercial_limitations": LIMITATIONS,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="文档讲解商业级交付前质检")
    parser.add_argument("--execution-preview", required=True)
    parser.add_argument("--stage", choices=["preview", "approved", "final"], default="preview")
    parser.add_argument("--approval")
    parser.add_argument("--final-manifest")
    parser.add_argument("--segments")
    parser.add_argument("--out")
    parser.add_argument("--skip-alpha-probe", action="store_true", help="仅用于无 ffprobe 的合同单测")
    parser.add_argument("--require-manifest-binding", action="store_true",
                        help="正式可确认预演必须绑定并登记到 run_manifest.json")
    args = parser.parse_args(argv)
    approval = _load(args.approval) if args.approval else None
    if isinstance(approval, dict) and args.approval and os.path.isfile(args.approval):
        approval["_approval_file_sha256"] = bridge._sha256_file(args.approval)
    result = audit(
        _load(args.execution_preview), stage=args.stage,
        approval=approval,
        final_manifest=_load(args.final_manifest) if args.final_manifest else None,
        segments=_load(args.segments) if args.segments else None,
        probe_alpha=not args.skip_alpha_probe,
        require_manifest_binding=args.require_manifest_binding)
    if args.out:
        bridge._atomic_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
