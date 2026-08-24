#!/usr/bin/env python3
"""Video prompt building and truncation utilities.

Extracted from video_engine.py (v3 split). Contains:
  - DEFAULT_NEGATIVE constant (suppressed artifacts)
  - STORYBOARD_VIDEO_RULES (storyboard-to-video instruction block)
  - _fit_video_prompt_limit (gateway 2500-char limit compliance)
  - _submission_text (compile segment → model-ready prompt)
  - _require_confirmed_prompt_review (gate: prompts must be user-confirmed)

Dependencies: seedance_prompt, artifact_contract
"""
import os
import sys
import json
import re

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import seedance_prompt
import artifact_contract
import language_contract


# ── Constants ───────────────────────────────────────────────────────────

# Default negative prompt: suppress AI video artifacts (deformities, text, etc.)
DEFAULT_NEGATIVE = ("模糊, 畸形, 多余手指, 手部畸形, 面部扭曲, 文字乱码, 水印, "
                    "字幕, 文字, 悬浮文字, kinetic typography, 逐字动画文字, 数据标签快闪, "
                    "字幕条, 说明文字, slogan文字, 低分辨率, 噪点, 抖动, 画面撕裂, 多余肢体, 变形")

NATIVE_STORYBOARD_VIDEO_RULES = """
【Seedance 原生故事板转视频规则】
Use the uploaded FINAL storyboard/contact sheet as Seedance-native storyboard guidance for this clip.
Follow the specified current segment, panel index, director text, dialogue, timeline, references and continuity contract.
Read the storyboard as a previs guide for composition, shot order, camera movement, action beats and lighting, but never render the grid, panel borders, labels, arrows, notes, text, subtitles or watermark.
Annotation legend for reading only: RED arrows = body / subject movement; BLUE arrows = camera movement; GREEN marks = framing and composition notes; ORANGE marks = lighting direction; PURPLE marks = sound and emotional emphasis; BLACK text = short shot notes and panel labels. Do NOT render any arrows, marks, labels or notes.
The storyboard may be monochrome previs; final output must be photorealistic live-action, full-color commercial footage with real materials, natural lighting and clean camera motion. NEVER render the video as a sketch, pencil drawing, charcoal drawing, storyboard panel, animatic, comic strip or grayscale previs.
Strictly preserve the confirmed product, character when present, props, spatial relationship, wardrobe, voice, BGM mood and lighting. Do not add extra characters or change the plot.
"""

PANEL_REFERENCE_VIDEO_RULES = """
【单镜头展开图转视频规则】
Use the uploaded SINGLE 16:9 reference plate as this clip's composition, motion and lighting guide.
It is one expanded frame prepared from the customer-approved plan, not a multi-panel board and not a grid.
Generate only this clip's described action. Do NOT infer other panels, animate a contact sheet, create split-screen panels, or render any annotation marks.
The reference plate may be monochrome or previs-like; the final video MUST be photorealistic live-action, full-color commercial footage with real materials, natural lighting, believable product scale and clean camera motion. NEVER render the video as a sketch, pencil drawing, charcoal drawing, storyboard panel, animatic, comic strip or grayscale previs.
Strictly preserve the uploaded confirmed product, character when present, props, spatial relationships and scene lighting. Do not add text, subtitles, logos, extra characters, arrows, labels or watermark.
"""

# Backward-compatible export.
STORYBOARD_VIDEO_RULES = NATIVE_STORYBOARD_VIDEO_RULES


# ── Prompt building ────────────────────────────────────────────────────

def _uses_native_storyboard_prompt(segment, model):
    mode = str((segment or {}).get("storyboard_ref_mode") or "").strip().lower()
    if mode in ("expanded_panel", "single_panel", "single_shot_keyframe"):
        return False
    return (not mode or mode in ("native_storyboard", "contact_sheet", "storyboard_contact_sheet")) \
        and seedance_prompt.is_seedance_model(model)


def _is_kling_video_model(model):
    return "kling" in str(model or "").lower()


def _model_prompt_keys(model):
    keys = [str(model)]
    if seedance_prompt.is_seedance_model(model):
        keys.append("seedance")
    if _is_kling_video_model(model):
        keys.append("kling")
    return list(dict.fromkeys(key for key in keys if key))


def _same_prompt_model(approved_model, model):
    return bool(set(_model_prompt_keys(approved_model)) & set(_model_prompt_keys(model)))


def _approved_submission_for_model(segment, model):
    by_model = segment.get("approved_submission_prompts_by_model") or {}
    if isinstance(by_model, dict):
        for key in _model_prompt_keys(model):
            prompt = by_model.get(key)
            if prompt:
                return prompt
    approved_submission = segment.get("approved_submission_prompt_zh")
    approved_model = segment.get("approved_prompt_model")
    if approved_submission and approved_model and _same_prompt_model(approved_model, model):
        return approved_submission
    return None


def _assert_confirmed_submission_for_model(segment, model):
    if not _approved_submission_for_model(segment, model):
        raise ValueError(
            "PROMPT_REVIEW_REQUIRED_FOR_MODEL: 镜头 %s 缺少模型 %s 的已确认完整视频提交提示词" %
            (segment.get("id"), model))


def _submission_reference_snapshot(segment):
    """Canonicalize approved source media covered by customer confirmation.

    The provider-specific imageUrls subset is selected later from this frozen
    source packet.  Include both native storyboard and approved expanded-panel
    alternatives so adding a Kling fallback plate invalidates an older review.
    """
    refs = []

    def add_special(ref_id, tag, ref_type, label, role, intent, url):
        if not url:
            return
        refs.append({
            "index": len(refs) + 1, "id": ref_id, "tag": tag,
            "type": ref_type, "label": label, "role": role,
            "intent": intent, "url": url,
        })

    add_special(
        "approved_storyboard", "@storyboard", "storyboard_contact_sheet",
        "已确认故事板", "Seedance 原生分镜构图锚",
        "仅模型能力允许时作为 storyboard guidance 提交",
        segment.get("storyboard_url") or segment.get("storyboard_retrieve_url"))
    panel = segment.get("storyboard_panel") or {}
    add_special(
        "approved_expanded_panel", "@panel", "expanded_storyboard_panel",
        "已确认单格展开图", "Kling 单镜头构图锚",
        "仅回落到不支持原生故事板的模型时提交",
        panel.get("url") or panel.get("result_url"))
    for idx, ref in enumerate(segment.get("references") or [], 1):
        if not isinstance(ref, dict) or not ref.get("url"):
            continue
        refs.append({
            "index": ref.get("index") or (len(refs) + 1),
            "id": ref.get("id") or "ref_%02d" % idx,
            "tag": ref.get("tag") or "",
            "type": ref.get("type") or "",
            "label": ref.get("label") or "",
            "role": ref.get("role") or "",
            "intent": ref.get("intent") or "",
            "url": ref.get("url"),
        })
    known_urls = {item["url"] for item in refs}
    for url in segment.get("urls") or []:
        if url in known_urls:
            continue
        refs.append({
            "index": len(refs) + 1,
            "id": "url_%02d" % (len(refs) + 1),
            "tag": "", "type": "image_url", "label": "未标注图片输入",
            "role": "随视频提交的 imageUrls 输入",
            "intent": "未在 references 中登记，需人工确认语义。",
            "url": url,
        })
    return refs


def _submission_source_fingerprint(segment):
    """Bind every authored field that can change a model submission prompt.

    This is intentionally independent of dialogue length.  A four-character
    CTA, a voice-mode change, a camera instruction, or a design-board role
    change must invalidate the old customer confirmation just like a long
    script edit.  Runtime outputs and injected ``approved_*`` fields are not
    part of the source contract.
    """
    keys = (
        "id", "text", "dialogue", "language", "language_code",
        "audio_contract", "seedance_native", "timeline", "action",
        "visual", "camera", "camera_movement", "character_action",
        "scene_prompt", "continuity_in", "style", "negative_prompt",
        "render_plan", "background_identity_lock", "design_board",
        "generation_role", "video_model_contract", "clothing_lock",
        "product_facts", "product_name", "model", "product_type",
        "product_color", "color", "extend_from_previous", "extend_video",
        "storyboard_ref", "storyboard_ref_mode", "use_storyboard_reference",
        "storyboard_panel_index", "duration", "ratio", "resolution", "seed",
        "chain_required", "avatar_plate_chain", "chain_contract",
        "recovery_contract", "privacy_fallback_policy",
        "privacy_fallback_policy_contract", "audio_capability_policy",
        "audio_capability_policy_contract", "audio_canary_proof",
        "output_artifact_contract",
    )
    payload = {key: segment.get(key) for key in keys if key in segment}
    payload["effective_negative_prompt"] = (
        segment.get("negative_prompt")
        if "negative_prompt" in segment else DEFAULT_NEGATIVE)
    payload["submission_references"] = _submission_reference_snapshot(segment)
    return artifact_contract.sha256_json({
        "schema_version": 1,
        "segment": payload,
    })


def _storyboard_video_text(text, segment=None, model=None, storyboard_ref=False):
    """Wrap text with storyboard-to-video rules if storyboard_ref is enabled."""
    if storyboard_ref:
        rules = (NATIVE_STORYBOARD_VIDEO_RULES if _uses_native_storyboard_prompt(segment or {}, model)
                 else PANEL_REFERENCE_VIDEO_RULES)
        return rules.strip() + "\n\n【本段台词/剧情】\n" + text
    return text


def _avatar_plate_submission_text(segment, model, extend_url=None):
    """Build the document-explainer video request without composition leakage.

    PPT/layout/motion content belongs to the local render contract.  The paid
    model receives only a clean presenter plate request plus the four allowed
    continuity anchors: person, voice, wardrobe and background.
    """
    audio = segment.get("audio_contract") or {}
    dialogue = str(audio.get("dialogue") or segment.get("dialogue") or "").strip()
    language = audio.get("language") or segment.get("language")
    language_contract.ensure_dialogue_language(
        dialogue, language, context="avatar plate segment %s" % (segment.get("id") or "unknown"))
    refs = []
    for ref in segment.get("references") or []:
        if not isinstance(ref, dict):
            continue
        if ref.get("type") in {"character_identity", "character_board", "actor_board", "scene_environment"}:
            if ref.get("tag"):
                refs.append(ref["tag"])
    clothing = segment.get("clothing_lock") or ""
    background = segment.get("background_identity_lock") or {}
    lines = [
        "【数字人底片模式】只生成干净的数字人讲解底片，不生成PPT页、标题、字幕、卡片、图表、箭头、标注、Logo或任何设计板元素。",
        "人物：保持已确认人物的脸型、五官、发型、年龄、身材比例和身份一致；只使用参考素材%s。" %
        ("（%s）" % "、".join(refs) if refs else ""),
        "服装：%s。不得换装、变色或增加配饰。" % (clothing or "保持已确认服装与配饰不变"),
        "台词：%s" % dialogue if dialogue else "本段无台词，只做自然呼吸和轻微表情",
        "声音：%s；语言：%s；口型同步：%s。" % (
            audio.get("voice_direction") or audio.get("voice") or
            "保持已确认声音人设",
            language or "保持已确认语言",
            "必须" if audio.get("lip_sync", bool(dialogue)) else "不要求"),
        "背景：%s；保持同一背景、机位、灯光方向和色温，不重新设计空间。" %
        (background.get("description") or "使用已确认统一背景"),
        "输出：真实、干净、稳定的 presenter avatar plate，给后续 Remotion/HyperFrames/fuse 合成使用。",
    ]
    if dialogue:
        lines.append("音频输出：必须生成上述中文口播人声并写入成品视频音轨；不得静音，不得只生成画面。")
    bgm = str(audio.get("bgm") or "").strip()
    if bgm.lower() in {"none", "no", "off", "无", "不需要", "不要"}:
        lines.append("背景音乐：不生成；这只表示不要BGM，不得删除或静音口播人声。")
    elif bgm:
        lines.append("BGM氛围：%s；保持跨段一致。" % bgm)
    if extend_url:
        lines.insert(0, "从上一段尾帧自然延续人物位置、动作状态、背景和光线，不重置场景。")
    text = "\n".join(lines)
    if len(text) <= 2400:
        return text
    # Keep the spoken line and identity anchors even when provider limits are tight.
    return "\n".join(lines[:2] + [lines[2], lines[3], lines[4], lines[5], lines[-1]])


def _compile_seedance_text(segment, model):
    """Compile structured segments while preserving legacy plain-text calls."""
    if not segment.get("seedance_native", True):
        return segment.get("text", "")
    if not (seedance_prompt.is_seedance_model(model) or _is_kling_video_model(model)):
        return segment.get("text", "")
    if not any(segment.get(k) for k in ("timeline", "action", "visual", "camera", "camera_movement")):
        return segment.get("text", "")
    prompt_model = ("kling-v3-omni-video" if _is_kling_video_model(model)
                    else model)
    prompt_segment = dict(segment)
    prompt_segment["storyboard_ref"] = False
    prompt_segment.pop("storyboard_ref_mode", None)
    return seedance_prompt.compile_prompt(
        prompt_segment, segment.get("references") or segment.get("reference_roles") or [],
        style=segment.get("style"), negative=segment.get("negative_prompt"),
        target_model=prompt_model)


def _product_identity_lock(segment):
    """Return a client-neutral product lock for compressed prompts.

    Product-specific facts belong in the approved segment text and references.
    This fallback clause must never inject demo-client details into another
    customer's video.
    """
    facts = segment.get("product_facts") or {}
    if not isinstance(facts, dict):
        facts = {}
    named = []
    for key in ("product_name", "model", "product_type", "product_color", "color"):
        value = facts.get(key) or segment.get(key)
        if value:
            named.append("%s=%s" % (key, value))
    fact_note = (" Known confirmed facts: %s." % "; ".join(named)) if named else ""
    return (
        "Product: preserve the exact confirmed product identity, geometry, proportions, "
        "color/material, controls, ports, native markings and all visible functional "
        "surfaces from the approved references; do not recolor, redesign, simplify, "
        "flatten or replace the product.%s" % fact_note
    )


def _fit_video_prompt_limit(text, segment, model, limit=2400):
    """Keep provider prompt below the gateway's 2500-character hard limit.

    Preserve business-critical dialogue, action, product identity and
    continuity contract while dropping duplicated explanatory prose first.
    """
    text = str(text or "")
    if len(text) <= limit:
        return text
    audio = segment.get("audio_contract") or {}
    audio_method = " ".join(item for item in (
        "Voice direction: %s." % audio.get("voice_direction") if audio.get("voice_direction") else "",
        "Voice method: %s." % audio.get("voice_continuity_method") if audio.get("voice_continuity_method") else "",
        "BGM method: %s." % audio.get("bgm_continuity_method") if audio.get("bgm_continuity_method") else "",
        "SFX method: %s." % audio.get("sfx_continuity_method") if audio.get("sfx_continuity_method") else "",
        "Media reference method: %s." % audio.get("media_reference_method") if audio.get("media_reference_method") else "",
    ) if item)
    director_brief = segment.get("director_brief") or {}
    director_summary = ""
    if isinstance(director_brief, dict) and director_brief:
        director_summary = "; ".join(item for item in (
            "narrative=%s" % director_brief.get("narrative_function") if director_brief.get("narrative_function") else "",
            "beats=%s" % " / ".join(map(str, director_brief.get("timeline_beats") or [])) if director_brief.get("timeline_beats") else "",
            "start=%s" % director_brief.get("start_state") if director_brief.get("start_state") else "",
            "end=%s" % director_brief.get("end_state") if director_brief.get("end_state") else "",
            "camera=%s" % director_brief.get("camera_motion") if director_brief.get("camera_motion") else "",
            "dialogue=%s" % director_brief.get("dialogue_delivery") if director_brief.get("dialogue_delivery") else "",
            "edit=%s" % director_brief.get("edit_continuity") if director_brief.get("edit_continuity") else "",
            "refs=%s" % director_brief.get("reference_priority") if director_brief.get("reference_priority") else "",
        ) if item)
    fields = [
        ("Commercial video. Use the Seedance-native storyboard/contact sheet for the specified current segment; annotation colors/arrows/marks are reading-only. Do NOT render any arrows, marks, labels, notes, grid, storyboard border, text, subtitles, logos or watermark. NEVER render the video as a sketch, pencil drawing, charcoal drawing, storyboard panel, animatic or grayscale previs; final output is photorealistic live-action."
         if _uses_native_storyboard_prompt(segment, model)
         else "Commercial video. Use only the current shot's expanded SINGLE 16:9 reference plate; annotation colors/arrows/marks are reading-only. Do NOT render any arrows, marks, labels, notes, grid, storyboard border, split screens, text, subtitles, logos or watermark. NEVER render the video as a sketch, pencil drawing, charcoal drawing, storyboard panel, animatic or grayscale previs; final output is photorealistic live-action."),
        "Dialogue: %s" % (audio.get("dialogue") or segment.get("dialogue") or ""),
        ("Language lock: %s" % language_contract.language_instruction(
            audio.get("language") or segment.get("language"))
         if (audio.get("language") or segment.get("language")) else ""),
        "Director text: %s" % (segment.get("text") or ""),
        "Director brief: %s" % director_summary,
        "Visual: %s" % (segment.get("visual") or ""),
        "Action: %s" % (segment.get("character_action") or segment.get("action") or ""),
        "Camera: %s" % (segment.get("camera_movement") or segment.get("camera") or ""),
        "Scene: %s" % (segment.get("scene_prompt") or ""),
        "Continuity: %s" % (segment.get("continuity_in") or "same confirmed product, character when present, background and lighting"),
        "Audio continuity: %s" % ((audio.get("voice_continuity") or "") + " " +
                                  (audio.get("bgm_continuity") or "") + " " +
                                  (audio.get("sfx_continuity") or "") + " " +
                                  audio_method),
        _product_identity_lock(segment),
    ]
    background = segment.get("background_identity_lock") or {}
    if background:
        fields.insert(9, "Background lock: %s; reference_tags=%s; mode=%s; do not change the confirmed background, camera position, lighting direction or color temperature." % (
            background.get("description") or "same confirmed background",
            ",".join(background.get("reference_tags") or []) or "none",
            background.get("mode") or "text_only_weak"))
    compact = "\n".join(item for item in fields if item.split(": ", 1)[-1].strip())
    if len(compact) <= limit:
        return compact
    required = fields[:4] + [fields[8], fields[9], fields[10]]
    optional = fields[4:8]
    chosen = list(required)
    for item in optional:
        candidate = "\n".join(chosen + [item])
        if len(candidate) <= limit:
            chosen.append(item)
    return "\n".join(chosen)


def _submission_text(segment, model, storyboard_ref=False, extend_url=None,
                     *, allow_unconfirmed=False):
    """Compile from original segment for the target model.

    Formal/paid generation returns the exact customer-approved model-specific
    submission text.  Only prompt-review capture may compile a new candidate
    by passing ``allow_unconfirmed=True``.
    """
    audio = segment.get("audio_contract") or {}
    language = audio.get("language") or segment.get("language")
    dialogue = audio.get("dialogue") or segment.get("dialogue")
    language_contract.ensure_dialogue_language(
        dialogue, language, context="video segment %s" % (segment.get("id") or "unknown"))
    approved_submission = _approved_submission_for_model(segment, model)
    if approved_submission:
        return str(approved_submission)
    if not allow_unconfirmed:
        _assert_confirmed_submission_for_model(segment, model)
    if (segment.get("design_board") or
            str(segment.get("generation_role") or "") == "avatar_plate_only" or
            str((segment.get("video_model_contract") or {}).get("role") or "") == "avatar_plate_only"):
        if storyboard_ref:
            raise ValueError(
                "DESIGN_BOARD_STORYBOARD_REF_FORBIDDEN: avatar_plate_only 不允许使用故事板参考模式")
        return _avatar_plate_submission_text(segment, model, extend_url=extend_url)
    base_text = segment.get("approved_prompt_zh") or _compile_seedance_text(segment, model)
    text = _storyboard_video_text(
        base_text, segment=segment, model=model, storyboard_ref=storyboard_ref)
    if not segment.get("seedance_native", True):
        render_plan = (segment.get("render_plan") or {}).get("content") or {}
        if audio:
            text += ("\n【音频执行契约】台词=%s；语言/口音=%s；声音人设=%s；背景音乐=%s；"
                     "音效=%s；口型同步=%s。必须实际生成并遵守，不得当作备注忽略。" % (
                         audio.get("dialogue") or "无", audio.get("language") or "无指定",
                         audio.get("voice_direction") or audio.get("voice") or
                         "无指定", audio.get("bgm") or "无",
                         audio.get("sfx") or "无", "必须" if audio.get("lip_sync") else "不要求"))
        if render_plan:
            text += "\n【已确认渲染方案】%s。必须实际执行。" % json.dumps(
                render_plan, ensure_ascii=False, sort_keys=True)
    if extend_url:
        text = ("将上一段视频自然延长，不重新剪辑前段；保持人物、产品、场景、服装、光线和运镜逻辑完全连续。新增部分：\n"
                  + text)
    audio_mode = (segment.get("audio_contract") or {}).get("audio_mode")
    if audio_mode == "voiceover":
        text += ("\n【画外音模式】人物必须保持闭嘴，仅做自然展示、指向、点头或微笑；"
                 "禁止说话口型、讲话式下颌运动或生成对白。")
    elif audio_mode == "talking_presenter":
        text += "\n【口播模式】人物必须按确认台词自然说话并保持原生口型同步。"
    elif audio_mode == "music_only":
        text += "\n【纯音乐模式】禁止对白、旁白、说话口型、字幕和画面文字。"
    if language:
        text += "\n【语言硬锁】%s" % language_contract.language_instruction(language)
    background = segment.get("background_identity_lock") or {}
    if background:
        text += "\n【跨段背景硬锁】%s；参考%s。所有分段复用同一背景、机位、灯光方向和色温，不得改背景。" % (
            background.get("description") or "同一确认背景",
            "、".join(background.get("reference_tags") or []) or "文本锁（缺少图像锚点）")
    return _fit_video_prompt_limit(text, segment, model)


def _require_confirmed_prompt_review(path, stage, segments):
    """Gate: ensure user has confirmed the Chinese prompt for each shot."""
    with open(path, encoding="utf-8") as handle:
        review = json.load(handle)
    import prompt_review_contract as _pr_contract
    _pr_contract.assert_current(review, path=path)
    expected = {str(seg.get("id")) for seg in segments}
    actual = {str(item.get("shot_id")) for item in review.get("prompts") or []}
    expected_fingerprints = {seg.get("storyboard_plan_fingerprint") for seg in segments
                             if seg.get("storyboard_plan_fingerprint")}
    if (review.get("status") != "confirmed" or review.get("stage") != stage or
            not expected.issubset(actual) or
            (expected_fingerprints and review.get("plan_fingerprint") not in expected_fingerprints)):
        raise ValueError(
            "PROMPT_REVIEW_REQUIRED: %s 阶段的中文提示词必须先由用户确认；缺少=%s" %
            (stage, ",".join(sorted(expected - actual))))
    prompts = {str(item.get("shot_id")): item
               for item in review.get("prompts") or []}
    for segment in segments:
        item = prompts.get(str(segment.get("id"))) or {}
        base_prompt = item.get("prompt_zh")
        submission_prompt = item.get("submission_prompt_zh")
        if not submission_prompt or not item.get("model"):
            raise ValueError(
                "PROMPT_REVIEW_EXACT_SUBMISSION_REQUIRED: 镜头 %s 必须通过 capture-video "
                "确认主模型及其完整提交提示词；仅确认基础 prompt_zh 无效" % segment.get("id"))
        reviewed_extend = bool(item.get("extend_from_previous"))
        current_extend = bool(segment.get("extend_from_previous") or segment.get("extend_video"))
        if reviewed_extend != current_extend:
            raise ValueError(
                "PROMPT_REVIEW_VARIANT_MISMATCH: 镜头 %s 的续接模式在确认后发生变化；"
                "请重新 capture-video 并确认" % segment.get("id"))
        current_chain_required = bool(
            segment.get("chain_required") or
            segment.get("avatar_plate_chain"))
        if (bool(item.get("chain_required")) != current_chain_required or
                (item.get("chain_contract") or {}) !=
                (segment.get("chain_contract") or {})):
            raise ValueError(
                "PROMPT_REVIEW_CHAIN_MISMATCH: 镜头 %s 的尾帧串联合同在确认后"
                "发生变化；请重新 capture-video 并确认" % segment.get("id"))
        if (item.get("audio_capability_policy") !=
                segment.get("audio_capability_policy") or
                (item.get("audio_capability_policy_contract") or {}) !=
                (segment.get("audio_capability_policy_contract") or {})):
            raise ValueError(
                "PROMPT_REVIEW_AUDIO_CAPABILITY_POLICY_MISMATCH: 镜头 %s 的"
                "输出音轨实证策略在确认后发生变化；请重新 capture-video 并确认" %
                segment.get("id"))
        reviewed_storyboard_mode = str(item.get("storyboard_ref_mode") or "")
        current_storyboard_mode = str(segment.get("storyboard_ref_mode") or "")
        if reviewed_storyboard_mode != current_storyboard_mode:
            raise ValueError(
                "PROMPT_REVIEW_VARIANT_MISMATCH: 镜头 %s 的故事板参考模式在确认后发生变化；"
                "请重新 capture-video 并确认" % segment.get("id"))
        if item.get("storyboard_panel_index") != segment.get("storyboard_panel_index"):
            raise ValueError(
                "PROMPT_REVIEW_VARIANT_MISMATCH: 镜头 %s 的故事板格号在确认后发生变化；"
                "请重新 capture-video 并确认" % segment.get("id"))
        if "submission_references" not in item or (
                item.get("submission_references") or []) != _submission_reference_snapshot(segment):
            raise ValueError(
                "PROMPT_REVIEW_REFERENCE_MISMATCH: 镜头 %s 的 imageUrls 输入在确认后发生变化；"
                "请重新 capture-video、展示完整提交包并确认" % segment.get("id"))
        if item.get("submission_source_fingerprint") != _submission_source_fingerprint(segment):
            raise ValueError(
                "PROMPT_REVIEW_SOURCE_MISMATCH: 镜头 %s 的台词、声音、画面、"
                "运镜或生成角色在确认后发生变化；请重新 capture-video 并确认" %
                segment.get("id"))
        # The reviewed prompt is the text actually sent to the paid model. A
        # stale review must not replace a newly authored Chinese dialogue with
        # an English line (or another shot's dialogue).
        audio = segment.get("audio_contract") or {}
        language = audio.get("language") or segment.get("language")
        dialogue = audio.get("dialogue") or segment.get("dialogue") or ""
        language_contract.ensure_dialogue_language(
            dialogue, language, context="prompt review segment %s" % (segment.get("id") or "unknown"))
        if language_contract.is_chinese_language(language):
            dialogue_chars = re.sub(r"[^\u3400-\u9fff]", "", str(dialogue))
        if base_prompt:
            segment["approved_prompt_zh"] = base_prompt
        model_prompts = {}
        raw_model_prompts = item.get("model_submission_prompts") or {}
        if isinstance(raw_model_prompts, dict):
            model_prompts.update({str(k): v for k, v in raw_model_prompts.items() if v})
        for fallback in item.get("fallback_submission_prompts") or []:
            if isinstance(fallback, dict) and fallback.get("model") and fallback.get("submission_prompt_zh"):
                model_prompts[str(fallback["model"])] = fallback["submission_prompt_zh"]
        if submission_prompt:
            segment["approved_submission_prompt_zh"] = submission_prompt
            segment["approved_prompt_model"] = item.get("model")
            model_prompts.setdefault(str(item.get("model")), submission_prompt)
        if language_contract.is_chinese_language(language) and dialogue_chars:
            for reviewed_model, reviewed_prompt in model_prompts.items():
                reviewed_chars = re.sub(r"[^\u3400-\u9fff]", "", str(reviewed_prompt))
                if dialogue_chars not in reviewed_chars:
                    raise ValueError(
                        "PROMPT_DIALOGUE_MISMATCH: 镜头 %s 的模型 %s 确认提示词未包含当前中文台词；"
                        "请重新 capture-video 并展示确认，禁止沿用旧提示词" %
                        (segment.get("id") or "unknown", reviewed_model))
        if model_prompts:
            segment["approved_submission_prompts_by_model"] = model_prompts
        if item.get("director_brief"):
            segment["director_brief"] = item["director_brief"]
