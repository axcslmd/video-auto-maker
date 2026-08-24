#!/usr/bin/env python3
"""Authoritative storyboard, cast-board and design-board prompt builders.

This module replaces the former duplicate prompt implementation. It is loaded
lazily through ``storyboard`` compatibility façades so prompt policy has one
source of truth without creating an import-time cycle.
"""
import storyboard as _storyboard

DEFAULT_DESIGN_BOARD_PANEL_PLAN = _storyboard.DEFAULT_DESIGN_BOARD_PANEL_PLAN
STORYBOARD_RATIO = _storyboard.STORYBOARD_RATIO
_as_feature_dict = _storyboard._as_feature_dict
_assign_asset_tags = _storyboard._assign_asset_tags
_feat = _storyboard._feat
_grid_for_panel_count = _storyboard._grid_for_panel_count
_shot_needs_usage_reference = _storyboard._shot_needs_usage_reference
needs_product_usage_image = _storyboard.needs_product_usage_image
normalize_panel_plan = _storyboard.normalize_panel_plan
product_usage_prompt = _storyboard.product_usage_prompt


def cast_prompt(plan):
    title = plan.get("project_title") or "marketing video"
    style = plan.get("visual_style") or "premium commercial storyboard concept art"
    chars = plan.get("characters") or []
    if not chars:
        return None
    lines = []
    for i, c in enumerate(chars, 1):
        face = _as_feature_dict(c.get("facial_features"))
        body = _as_feature_dict(c.get("body_features"))
        # face_shape 兼容两种键名：features 里可能叫 face_shape 或 face
        face_shape = face.get("face_shape") or face.get("face") or c.get("face_shape", "")
        body_raw = body.get("_raw", "")
        body_desc = " ".join(str(x) for x in [
            body.get("height", ""), body.get("build", ""),
            body.get("proportions", ""), c.get("body_type", ""), body_raw,
        ] if x).strip()
        parts = [
            f"Character {i}: id={c.get('id','')}, name={c.get('name','')}, role={c.get('role','')}",
            f"age/gender/ethnicity={c.get('age','')} {c.get('gender','')} {c.get('ethnicity','')}",
            f"appearance={c.get('appearance','')}",
            f"face={face_shape}; eyebrows={_feat(face,'eyebrows',c)}; eyes={_feat(face,'eyes',c)}; nose={_feat(face,'nose',c)}; lips={_feat(face,'lips',c)}; ears={_feat(face,'ears',c)}; skin={_feat(face,'skin',c)}; special_marks={_feat(face,'special_marks',c)}",
            f"hair={c.get('hair','')}; makeup={c.get('makeup','')}; costume={c.get('costume','')}; shoes={c.get('shoes','')}; accessories={c.get('accessories','')}",
            f"body={body_desc}",
            f"personality={c.get('personality','')}; default_expression={c.get('default_expression','')}; posture/body_language={c.get('body_language','')}; voice={c.get('voice','')}",
        ]
        immutable = c.get("immutable_features") or plan.get("immutable_features") or []
        if immutable:
            parts.append("immutable identity locks=" + (", ".join(immutable) if isinstance(immutable, list) else str(immutable)))
        # 保留有实际内容的 part：只要任一 "key=value" 的 value 非空就留下。
        # （旧逻辑用 not p.endswith('=') 判空，对多字段复合行会误杀——末字段为空
        #  就丢掉整行，导致 face=... 这类多描述行里前面的有效内容一起消失。）
        def _has_content(part):
            for seg in part.split(";"):
                if "=" in seg and seg.split("=", 1)[1].strip():
                    return True
            return "=" not in part and bool(part.strip())
        lines.append("; ".join([p for p in parts if _has_content(p)]))

    # ── 注入客户上传的数字人参考图 ────────────────────────────────────────────
    # asset_refs.digital_human_portraits: 客户真实上传的人物图，是人物板的最高优先级参考。
    # 若存在，强制要求模型严格保持人脸/发型/服装与参考图一致，而不是凭空想象。
    ref_block = ""
    asset_refs = plan.get("asset_refs") or {}
    portrait_refs = asset_refs.get("digital_human_portraits") or []
    if portrait_refs:
        ref_block = (
            "\n\n[CRITICAL — UPLOADED CHARACTER REFERENCE IMAGES]\n"
            "The client has provided REAL uploaded reference photos for the characters below.\n"
            "These photos define the EXACT face, hairstyle, skin tone, and outfit.\n"
            "You MUST reproduce these features faithfully — do NOT invent or alter them.\n"
            "Reference images are provided inline as image_url blocks in the API call.\n"
            "Character portrait references:\n"
            + "\n".join("  - %s" % r for r in portrait_refs)
        )
    # ── 近景人脸 ↔ 全身一致性强锁（seedance 文档 ID 漂移根因对策）────────────────
    # seedance「人物 ID 漂移」根因：人脸参考有效性不足——人脸与全身/姿态混在一起、
    # 人脸占比过小、权重不够。对策：把「人脸特写(大头照)」当成最高权重的身份锚，
    # 明确要求六视图里的正脸特写与全身照里的脸是「同一张脸」，五官逐项对齐；
    # 大头照要占比大、五官清晰、无表情最佳、减少肩颈/背景干扰。
    face_lock_block = (
        "\n\n[CRITICAL — FACE ↔ FULL-BODY IDENTITY LOCK / 近景人脸与全身一致性强锁]\n"
        "The single most important requirement: the SAME face must appear in BOTH the "
        "face close-up views AND the full-body views. Treat the FRONT FACE CLOSE-UP (大头照) "
        "as the primary identity anchor — render it LARGE, high-detail, sharp, front-lit, "
        "ideally neutral expression, with minimal shoulder/neck/background distraction so the "
        "facial features carry maximum weight. Then reproduce EXACTLY that same face on the "
        "full-body panels: identical face shape, eye shape and spacing, eyebrow shape, nose "
        "bridge and tip, lip shape, jawline, ear shape, skin tone, and hairline. "
        "The near-shot face and the full-body face must be unmistakably the SAME PERSON — "
        "no age drift, no face-slimming, no beautify filter that changes bone structure, "
        "no different makeup between the close-up and the full-body panels. "
        "If the close-up face and the full-body face look even slightly like different people, "
        "the sheet is REJECTED. Keep hairstyle, wardrobe, shoes, accessories and body "
        "proportions identical across every panel too."
    )
    return (
        "Create a professional CHARACTER REFERENCE SHEET / 人物六视图参考板 for a marketing video, based on the character design sheet standard. "
        "For EACH appearing character, create ONE clean reference-sheet image that clearly contains SIX REQUIRED VIEWS in a neat 2x3 or 3x2 grid, with visible panel separation and consistent scale. "
        "The six panels MUST be: "
        "(1) full-body front view / 全身正视图, standing straight, full body in frame, feet visible; "
        "(2) full-body back view / 全身后视图, back facing camera, full outfit back, hair back and silhouette visible; "
        "(3) full-body side view / 全身侧视图, exact left or right profile, nose/chin/profile and full outfit side seam visible; "
        "(4) face close-up front view / 正脸正视图, LARGE high-detail 85mm portrait (大头照, the identity anchor), neutral or gentle expression, eyes and all facial features crisp, minimal shoulder/background; "
        "(5) face/head close-up back view / 正脸后视图/后脑勺视图, back of head, hairline/hairstyle/ear/back-neck details visible; "
        "(6) face close-up side view / 正脸侧视图, exact side profile close-up, cheekbone, brow, eye, nose bridge, lips and jawline visible. "
        "Lock the SAME identity across all six panels: face shape, eye shape/color, eyebrow shape, nose bridge, lip shape, ear shape, skin tone, hairstyle, body proportions, height/build, wardrobe, shoes, accessories, makeup, scars/moles/special marks, and color palette must remain consistent. "
        "Use pure white seamless background #FFFFFF, neutral studio backdrop, high-key soft studio lighting, three-point lighting, neutral 5500K color temperature, photorealistic commercial character-design reference sheet, ultra detailed. "
        "Use only tiny panel labels if needed; no subtitles, no long text, no watermark, no logo distortion. "
        "Negative constraints: different face, inconsistent identity, face close-up and full-body looking like different people, changed hairstyle, changed outfit, different body proportions, wrong back view, duplicate panels, missing feet, cropped full body, extra limbs, bad hands, asymmetrical face, blurry, low quality, text blocks, watermark, dark/patterned background. "
        "If multiple characters are listed, include a separate six-view reference block for each character in the same image, without mixing identities; keep each character visually separable.\n"
        f"Project: {title}\nVisual style: {style}\n" + "\n".join(lines)
        + face_lock_block
        + ref_block
    )


def _subject_definition_block(plan, shot, chars):
    """seedance 主体定义句式：把每个出场人物用「将<主体>的核心特征定义为<标签>」锁定，
    后续统一用同一标签指代，降低 ID 漂移/双胞胎。返回一段可拼进 prompt 的文本或 ''。"""
    shot_char_ids = shot.get("characters") or []
    if not shot_char_ids:
        return ""
    lines = ["\n[SUBJECT DEFINITION / 主体定义（seedance 强指代，防 ID 漂移与双胞胎）]"]
    for cid in shot_char_ids:
        c = chars.get(cid, {"id": cid})
        label = c.get("name") or cid
        # 2-3 个稳定静态特征（服饰/发型/外观/类别）唯一可识别
        feats = []
        if c.get("costume"):
            feats.append(str(c["costume"]))
        if c.get("hair"):
            feats.append("发型:" + str(c["hair"]))
        if c.get("appearance"):
            feats.append(str(c["appearance"]))
        feat_str = "，".join([f for f in feats if f][:3]) or "该角色"
        lines.append(
            "  - 将「%s」（%s）定义为主体【%s】，后续全程仅用【%s】指代，保持同一身份，"
            "不得出现第二个外形/着装/配饰完全相同的人物。" % (label, feat_str, label, label)
        )
    return "\n".join(lines)


def _panel_prompt_block(shot):
    """Turn panel_plan into explicit director beats instead of a loose list.

    Image models respond more reliably when each panel has one observable event
    and a camera/composition change.  Keep free-form strings supported for old
    plans, while allowing new plans to provide structured panel dictionaries.

    2026-08-05 revision: panel count is script-driven (no [:12] truncation),
    and each panel line inlines its @tag reference(s) when the plan/shot
    specifies which uploaded asset that beat depends on (panel-level
    ref_tags, falling back to shot-level ref_tags).
    """
    panel_plan = normalize_panel_plan(shot)
    if not panel_plan:
        return ""
    total = len(panel_plan)
    shot_ref_tags = shot.get("ref_tags") or []
    if isinstance(shot_ref_tags, str):
        shot_ref_tags = [shot_ref_tags]
    lines = [
        "\n[PANEL-BY-PANEL DIRECTOR BEATS / 逐格导演执行表 — %d panels total]" % total,
        "Render each numbered panel as one readable instant, not a collage or repeated pose.",
        "For every panel preserve screen direction, subject identity, prop geometry and the previous panel's cause-and-effect.",
    ]
    for number, item in enumerate(panel_plan, 1):
        if isinstance(item, dict):
            panel = item.get("panel") or item.get("index") or number
            values = [
                ("beat", item.get("beat") or item.get("action") or item.get("event")),
                ("shot", item.get("shot_size") or item.get("size")),
                ("camera", item.get("camera_movement") or item.get("camera")),
                ("angle", item.get("angle") or item.get("angle_offset")),
                ("composition", item.get("composition") or item.get("layout")),
                ("lighting", item.get("lighting")),
                ("expression", item.get("micro_expression") or item.get("emotion")),
                ("continuity", item.get("continuity")),
            ]
            detail = "; ".join("%s=%s" % (key, value) for key, value in values if value not in (None, ""))
            panel_tags = item.get("ref_tags") or shot_ref_tags
            if isinstance(panel_tags, str):
                panel_tags = [panel_tags]
            tag_str = (" refs=%s" % " ".join(panel_tags)) if panel_tags else ""
            lines.append("  Panel %s: %s%s" % (panel, detail or "one distinct observable beat", tag_str))
        else:
            tag_str = (" refs=%s" % " ".join(shot_ref_tags)) if shot_ref_tags else ""
            lines.append("  Panel %d: beat=%s%s" % (number, str(item).strip(), tag_str))
    return "\n".join(lines)


def _cinematic_grammar_block(shot):
    """Add model-agnostic visual grammar and a practical acceptance checklist."""
    panel_count = len(normalize_panel_plan(shot))
    return (
        "\n[CINEMATIC PROMPT GRAMMAR / 电影提示词语法]\n"
        "Priority order: 1) subject identity and count, 2) spatial relationships and screen direction, "
        "3) one physical action with a visible result, 4) shot size and camera angle, 5) camera movement, "
        "6) composition and negative space, 7) lighting/material response, 8) emotional state. "
        "Use concrete visible nouns and verbs; do not describe invisible marketing claims as imagery. "
        "Each panel is a single decisive frame from a continuous take, with motivated camera movement and "
        "a clear foreground/midground/background layer. Keep product scale, hand contact, eyelines and "
        "left-to-right screen direction physically plausible.\n"
        "[DIRECTOR QA / 导演验收约束]\n"
        "The result is acceptable only if all %d panels are countable and readable, each panel has a distinct "
        "story beat, adjacent panels provide a 30-50 degree angle change or a clear shot-size/composition change, "
        "hands actually contact the intended prop, faces remain the same person, and no panel contradicts the "
        "location, wardrobe, lighting, product geometry or timeline. Prefer simple achievable actions over "
        "simultaneous complex actions." % panel_count
    )


def _storyboard_contract_value(plan):
    contract = (plan or {}).get("storyboard_contract") or {}
    if not isinstance(contract, dict):
        contract = {}
    return str(
        (plan or {}).get("storyboard_role") or contract.get("kind") or
        contract.get("role") or contract.get("usage") or ""
    ).strip().lower()


def _plan_has_physical_product_subject(plan):
    if not isinstance(plan, dict):
        return False
    asset_keys = {
        "product_images", "product_boards", "product_usage_images",
        "usage_reference_images", "product_reference_images",
    }
    asset_refs = plan.get("asset_refs") or {}
    if any(asset_refs.get(key) for key in asset_keys):
        return True
    if any(plan.get(key) for key in (
            "product_sku", "product_refs", "product_assets", "physical_product",
            "product_image", "product_board", "usage_geometry_review")):
        return True
    if str(plan.get("product_kind") or "").strip().lower() in (
            "physical", "physical_product", "实体产品", "实物产品"):
        return True
    for item in plan.get("references") or []:
        if isinstance(item, dict) and item.get("type") in (
                "product_identity", "product_board", "product_usage_identity"):
            return True
    for shot in plan.get("shots") or []:
        if any(shot.get(key) for key in (
                "product_sku", "product_refs",
                "force_usage_reference", "physical_product")):
            return True
        shot_refs = shot.get("asset_refs") or {}
        if any(shot_refs.get(key) for key in asset_keys):
            return True
        for item in shot.get("references") or []:
            if isinstance(item, dict) and item.get("type") in (
                    "product_identity", "product_board", "product_usage_identity"):
                return True
    return False


def _looks_like_design_board(plan):
    if _plan_has_physical_product_subject(plan):
        return False
    value = _storyboard_contract_value(plan).replace("_", "-")
    if value in {
            "design-board", "motion-design-board", "layout-board",
            "customer-preview"}:
        return True
    contract = (plan or {}).get("storyboard_contract") or {}
    if isinstance(contract, dict) and contract.get("video_model_usage") == "do_not_submit_as_storyboard_ref":
        return True
    return False


def _design_board_grammar_block(shot):
    return (
        "\n[DESIGN BOARD PROMPT GRAMMAR / 设计板提示词语法]\n"
        "This is a customer-facing motion/layout design board for a document, PPT, or solution explainer. "
        "Contract: storyboard_role=design_board; video_model_usage=do_not_submit_as_storyboard_ref. "
        "It previews one clean composition frame only; it is not a video-model storyboard reference. "
        "Priority order: 1) PPT/content surface placement, 2) presenter position and scale, "
        "3) reserved safe zones for later HyperFrames text/cards/highlights, 4) enterprise visual hierarchy, "
        "5) stable presenter identity and wardrobe. Use clean enterprise presentation-frame clarity; "
        "reserve blank shapes for future graphics, but never draw readable text, labels, arrows, notes, or camera marks.\n"
        "[DESIGN BOARD QA / 设计板验收约束]\n"
        "The result is acceptable only if it is exactly one single full-frame 16:9 image, with no grid, no collage, "
        "no contact sheet, no multi-panel layout, no readable text, no labels, no shot numbers, no arrows, and no "
        "director/camera notes. The presenter must not cover important content zones, and later text/card areas "
        "must be blank reserved regions rather than rendered typography."
    )


def design_board_prompt(plan, shot, idx, bw=True, strict_bw=False,
                        reference_registry=None):
    """Prompt for one clean customer-facing design-board frame.

    Unlike video_storyboard prompts, this must not ask the model to draw a
    storyboard table/contact sheet. Remotion and HyperFrames own labels, cards,
    arrows, subtitles and PPT text later; this image is only the clean visual
    plate used for customer composition confirmation.
    """
    title = plan.get("project_title") or "document explainer"
    continuity = shot.get("continuity") or plan.get("continuity") or {}
    audio = shot.get("audio") or {}
    chars = {c.get("id"): c for c in (plan.get("characters") or [])}
    shot_chars = []
    for cid in shot.get("characters") or []:
        c = chars.get(cid, {"id": cid})
        shot_chars.append(
            "%s: %s %s %s" % (
                c.get("name") or cid,
                c.get("appearance", ""),
                c.get("costume", ""),
                c.get("personality", ""),
            )
        )
    if not shot_chars and (plan.get("characters") or []):
        for c in (plan.get("characters") or [])[:1]:
            shot_chars.append(
                "%s: %s %s %s" % (
                    c.get("name") or c.get("id") or "presenter",
                    c.get("appearance", ""),
                    c.get("costume", ""),
                    c.get("personality", ""),
                )
            )
    asset_refs = plan.get("asset_refs") or {}
    shot_asset_refs = shot.get("asset_refs") or {}
    doc_refs = (
        shot_asset_refs.get("document_images") or shot_asset_refs.get("ppt_pages") or
        asset_refs.get("document_images") or asset_refs.get("ppt_pages") or []
    )
    portrait_refs = (
        shot_asset_refs.get("digital_human_portraits") or
        asset_refs.get("digital_human_portraits") or []
    )
    registry = reference_registry or []
    ref_lines = []
    if registry:
        ref_lines.append("\n[REFERENCE CONTRACT / 参考图合同]")
        ref_lines.append("Use attached references only as identity/layout anchors; do not copy or invent readable text from them.")
        for item in registry:
            ref_lines.append(
                "  %s = %s; role=%s" % (
                    item.get("tag", ""),
                    item.get("type") or "generic_visual",
                    item.get("role") or item.get("label") or "",
                )
            )
    elif doc_refs or portrait_refs:
        ref_lines.append("\n[REFERENCE CONTRACT / 参考图合同]")
        if doc_refs:
            ref_lines.append("PPT/document references define deck style and content-surface placement only; render unreadable abstract blocks, not slide text.")
        if portrait_refs:
            ref_lines.append("Presenter portrait references lock face, hair, wardrobe and business presence.")
    color_mode = ("bw" if strict_bw else
                  (shot.get("color_mode") or plan.get("color_mode") or ("bw" if bw else "color")))
    if str(color_mode).lower() in ("bw", "black_white", "grayscale", "mono", "黑白"):
        color_block = (
            "Render the image in clean black/white/grayscale only, using tonal contrast and shading. "
            "Do not add color accents."
        )
        color_negative = "color accents, saturated colors, "
    else:
        color_block = "Use the approved enterprise visual style with restrained color and clear hierarchy."
        color_negative = ""
    motion_elements = shot.get("motion_elements") or []
    if isinstance(motion_elements, str):
        motion_elements = [motion_elements]
    reserved_motion = (
        "Reserve calm blank regions for later motion graphics, captions, cards, logo or CTA, but do not render those elements now."
        if motion_elements else
        "Reserve calm blank regions for later captions or brand overlays, but do not render those elements now."
    )
    continuity_lines = []
    for key, label in (
            ("background", "background"),
            ("character_identity", "presenter identity"),
            ("wardrobe", "wardrobe"),
            ("lighting", "lighting")):
        value = continuity.get(key)
        if value:
            continuity_lines.append("%s: %s" % (label, value))
    prompt_lines = [
        "Create one single clean 16:9 client-facing preview image for a PPT/document/service explainer.",
        "This is NOT a storyboard sheet and NOT a contact sheet.",
        "Hard format: one full-frame composition only, no grid, no collage, no split screen, no multi-panel layout, no borders between panels.",
        "Hard text rule: absolutely no readable text, no English labels, no Chinese labels, no captions, no shot numbers, no camera notes, no arrows, no UI words, no logo, no watermark.",
        "Show a polished enterprise explainer composition: modern office or presentation environment, a stable digital-human presenter, and a clean PPT/content surface represented by abstract unreadable blocks.",
        "The final readable PPT text, subtitles, highlight cards, arrows, logo and CTA will be added later by Remotion/HyperFrames/fuse; do not draw them in this base image.",
        color_block,
        reserved_motion,
        _design_board_grammar_block(shot),
        "Project: %s" % title,
        "Shot number: %s" % idx,
        "Duration: %s seconds" % (shot.get("duration", "")),
        "Customer-facing visual intent: %s" % (shot.get("visual", "")),
        "Composition/layout: %s" % (shot.get("composition") or shot.get("layout") or ""),
        "Camera/lens/movement feel: %s" % (shot.get("camera") or shot.get("camera_movement", "")),
        "Shot size: %s" % shot.get("shot_size", ""),
        "Angle offset: %s" % shot.get("angle_offset", ""),
        "Presenter action/performance: %s" % shot.get("character_action", ""),
        "Presenter identity lock: %s" % (shot.get("character_prompt") or "; ".join(shot_chars)),
        "Micro-expression: %s" % shot.get("micro_expression", ""),
        "Scene prompt: %s" % (shot.get("scene_prompt") or shot.get("scene", "")),
        "Motion layer handoff summary: %s" % "; ".join(map(str, motion_elements)),
        "Continuity lock: %s" % ("; ".join(continuity_lines) if continuity_lines else "same presenter, same wardrobe, same office style, same lighting"),
        "Negative constraints: %sgrid, collage, contact sheet, storyboard sheet, multiple panels, panel labels, director notes, camera labels, arrows, readable text, subtitles, captions, speech bubbles, logo, watermark, kinetic typography, data labels, UI text, duplicated presenter, distorted face, extra fingers, low quality" % color_negative,
    ]
    if ref_lines:
        prompt_lines.extend(ref_lines)
    return "\n".join(line for line in prompt_lines if line is not None)


def _shot_prompt_layout_context(plan, shot, *, bw, strict_bw, design_board,
                                tagged_assets, panel_plan):
    color_mode = ("bw" if strict_bw else
                  (shot.get("color_mode") or plan.get("color_mode") or ("bw" if bw else "color")))
    if str(color_mode).lower() in ("bw", "black_white", "grayscale", "mono", "黑白"):
        bw_block = (
             "STRICT BLACK-AND-WHITE DRAWING / 严格黑白绘画: render the ENTIRE %d-panel sheet in monochrome "
            "grayscale only — pure black, white and grays, NO color hue anywhere, like a classic "
            "pencil/charcoal film previsualization storyboard. Use tonal contrast and shading "
            "(not color) to convey depth, material and lighting. This is a black-and-white "
            "storyboard, not a colored render. " % len(panel_plan)
        )
        bw_negative = "任何彩色, 色相, 饱和度, 上色, colored, color tint, saturation, "
    else:
        bw_block = ""
        bw_negative = ""
    # ── N 格电影预演：每格都必须包含身体动量和明确摄影运动（N 由剧本分镜数决定）。
    seq_block = (
        "Treat the %d panels as an ORDERED SHOT SEQUENCE 镜头1→镜头%d in strict event order "
        "(先主后次). Every panel MUST show visible movement and body momentum; avoid static standing poses. "
        "Each panel = subject + location + physical action + camera movement + emotional pressure. "
        % (len(panel_plan), len(panel_plan))
    )
    if design_board:
        seq_block = (
            "Treat the %d panels as an ORDERED DESIGN SEQUENCE 设计步骤1→设计步骤%d in strict review order. "
            "Every panel MUST show a clear composition or motion-design change: PPT/content area, presenter "
            "window placement, highlight/focus region, transition rhythm, and reserved safe zones for later "
            "post-production graphics. "
            % (len(panel_plan), len(panel_plan))
        )
    # ── 双胞胎/字幕/Logo/水印全局约束（seedance 常见问题对策 + AGENTS.md 铁律#15）───
    twin_block = (
        "GLOBAL ANTI-TWIN CONSTRAINT 双胞胎全局约束: the whole sheet must NEVER show two people "
        "with identical face/outfit/accessories; NO clone/twin/duplicate person of any defined "
        "subject; each panel keeps only the single correct subject. "
        "ABSOLUTELY NO TEXT IN FRAME / 画面背景严禁出现任何文字: NO subtitles, NO on-screen "
        "captions, NO caption bar, NO speech bubbles, NO watermark, NO platform Logo, NO kinetic "
        "typography, NO floating slogan text, NO data/metric label callouts, NO UI text overlays "
        "that the model has to invent. All of these belong to a SEPARATE post-production motion-"
        "graphics layer (HyperFrames) added AFTER this storyboard/video is generated — this frame "
        "must render as a clean, text-free cinematic plate (人物/场景/产品实拍画面), even if the "
        "shot's narrative mentions slogans, stats, or captions appearing on screen. The ONLY "
        "exception is text that is a physical, pre-existing part of a real prop (e.g. text already "
        "printed on product packaging or visible in an uploaded screenshot reference image) — never "
        "invent new floating/animated text. "
    )
    # ── motion_elements：只作为后期交接数据，不把具体文字/动效设计发给图像模型。
    motion_elements = shot.get("motion_elements") or []
    if isinstance(motion_elements, str):
        motion_elements = [motion_elements]
    motion_note = (
        "\nA separate post-production motion layer may be added after the base video; "
        "do not render any such graphics in this storyboard image."
        if motion_elements else ""
    )
    # Grid layout is script-driven: pick cols x rows from the actual panel
    # count (2026-08-05 revision). 12 remains a valid value (falls through to
    # 4x3/3x4 as before) but is no longer forced.
    aspect = STORYBOARD_RATIO
    portrait = False
    panel_count = len(panel_plan)
    grid_cols, grid_rows = _grid_for_panel_count(panel_count, portrait)
    grid_desc = "%dx%d" % (grid_cols, grid_rows)
    canvas_desc = STORYBOARD_RATIO

    # ── @tag inline legend so the model maps each panel beat to a concrete
    # uploaded reference image instead of only seeing a bare URL list below.
    # (tagged_assets computed earlier alongside product_imgs/... for asset_block)
    tag_legend = ""
    if tagged_assets:
        _tag_role_label = {
            "product_images": "product reference",
            "product_usage_images": "confirmed product-in-use reference",
            "scene_images": "scene/background reference",
            "digital_human_portraits": "character/digital-human portrait reference",
        }
        legend_lines = ["\n[REFERENCE TAG LEGEND / 参考图标签]",
                        "Use these @tags when a panel beat below says refs=@xxx; each @tag points to one uploaded image."]
        for tag, _url, category in tagged_assets:
            legend_lines.append("  %s = %s" % (tag, _tag_role_label.get(category, category)))
        tag_legend = "\n".join(legend_lines)
    return {
        "bw_block": bw_block, "bw_negative": bw_negative,
        "seq_block": seq_block, "twin_block": twin_block,
        "motion_note": motion_note, "panel_count": panel_count,
        "grid_desc": grid_desc, "canvas_desc": canvas_desc,
        "tag_legend": tag_legend,
    }


def _format_shot_prompt(*, shot, idx, title, style, shot_chars, prop_prompts,
                        panel_plan, panel_source, design_board, director_block,
                        subject_def_block, audio, continuity_bits, asset_block,
                        product_fact_block, usage_block, no_character_block,
                        layout):
    bw_block = layout["bw_block"]
    bw_negative = layout["bw_negative"]
    seq_block = layout["seq_block"]
    twin_block = layout["twin_block"]
    motion_note = layout["motion_note"]
    panel_count = layout["panel_count"]
    grid_desc = layout["grid_desc"]
    canvas_desc = layout["canvas_desc"]
    tag_legend = layout["tag_legend"]
    return (
        "Create a %s cinematic storyboard TABLE with %d movie-style panels arranged as a clean %s grid / %s电影风格%d格故事板表格。"
        % (canvas_desc, panel_count, grid_desc, canvas_desc, panel_count)
        + bw_block
        + "The single image must contain exactly %d panels arranged in a clean %s grid inside one %s canvas, each panel showing a distinct beat of the SAME segment. "
        % (panel_count, grid_desc, canvas_desc)
        + (
            "The drawing itself is ONLY rough black-and-white layout/design sketches: clean PPT surface blocks, "
            "presenter picture-in-picture frames, safe-zone rectangles, motion arrows, highlight regions, and "
            "simple camera/composition notes. Keep the visual language enterprise, precise and readable, not "
            "theatrical or music-video-like. "
            if design_board else
            "The drawing itself is ONLY rough black-and-white pencil lines, minimal detail, quick loose construction, simple anatomy, strong silhouette readability, lightweight dynamic unfinished early choreography previsualization. "
        )
        + (
            "Add design annotation marks as a separate overlay layer: BLUE arrows for slide/camera movement, "
            "GREEN boxes for framing and safe zones, ORANGE marks for emphasis timing, PURPLE marks for audio/"
            "speaker rhythm, and BLACK short panel notes. Annotation colors are allowed only for these marks; "
            "all people, PPT surfaces, backgrounds and drawn imagery remain black/white/gray. "
            if design_board else
            "Add director annotation marks as a separate overlay layer: RED arrows for body motion, BLUE arrows for camera motion, GREEN marks for framing/composition notes, ORANGE marks for lighting direction, PURPLE marks for sound/emotional emphasis, and BLACK short shot notes/panel labels. Annotation colors are allowed only for these marks; all characters, environment, smoke, fabric, reflections and drawn imagery remain black/white/gray. "
        )
        + (
            "Use restrained presentation motion language: slide pan/zoom, corner presenter-window placement, "
            "callout-safe areas, card/highlight timing, and clear information hierarchy. "
            if design_board else
            "Use handheld energy, whip pans, orbiting camera, overhead view, side profile silhouette, aggressive close-up, and long-lens compression across the sequence. Keep the environment minimal: open space, smoke, fabric motion, reflected light. Make performers feel trapped between ritual and emotional release. "
        )
        + seq_block
        + (
            "Use presentation-design previsualization, readable layout hierarchy, stable safe zones and review-friendly composition. "
            "The panel-by-panel design beats below define the exact %d-step motion/layout progression for this segment; follow them in order while preserving the same explainer layout contract. " % panel_count
            if design_board else
            "Use film-style previsualization, readable composition, strong scene continuity, and edit-friendly coverage. "
            "The panel-by-panel director beats below define the exact %d-beat progression for this segment; follow them in order while preserving the same segment continuity. " % panel_count
        )
        + (
            "Use professional information-design language: slide content surface, presenter window, focus region, safe zones, visual hierarchy, and intentional negative space for later overlays. "
            "Keep adjacent panels visually different by changing highlight area, zoom crop, presenter-window emphasis, or composition focus. "
            if design_board else
            "Use professional camera language: rule-of-thirds or symmetry or foreground framing, clear eyeline/product placement, lens-like depth, directional lighting, and intentional negative space for later overlays. "
            "Keep adjacent panels visually different: 30-50 degree camera/subject angle offsets, or wide/medium/close/detail shot-size variation, or composition center shift. "
        )
        + (
            "Keep the SAME presenter identity, SAME wardrobe, SAME background style, SAME slide deck visual language, SAME lighting, and SAME art direction across the %s grid. " % grid_desc
            if design_board else
            "Keep the SAME background, SAME character identity, SAME wardrobe, SAME product appearance, SAME prop relationships, SAME lighting, and SAME art direction across the %s grid. " % grid_desc
        )
        + (
            "Across the whole explainer, keep presenter appearance, PIP position logic and motion-design language consistent; only vary layout focus and transition beats. "
            if design_board else
            "If this segment belongs to a multi-part stitched sequence, keep background, character appearance, voice tone, and BGM mood consistent across ALL segments; only vary camera angle, framing, and beat progression. "
        )
         + twin_block
         + director_block
         + (_design_board_grammar_block(panel_source) if design_board else _cinematic_grammar_block(shot))
         + subject_def_block + "\n"
        f"Project: {title}\n"
        f"Shot number: {idx}\n"
        f"Duration: {shot.get('duration','')} seconds\n"
         f"Visual action: {shot.get('visual','')}\n"
        f"Camera/lens/movement: {shot.get('camera') or shot.get('camera_movement','')}\n"
        f"Shot size: {shot.get('shot_size','')}\n"
        f"Angle offset: {shot.get('angle_offset','')}\n"
        f"Composition/layout: {shot.get('composition') or shot.get('layout','')}\n"
        f"Lighting: {shot.get('lighting','')}\n"
        f"Character action/performance: {shot.get('character_action','')}\n"
        f"Character prompt / identity lock: {shot.get('character_prompt') or shot.get('actor_prompt','')}\n"
        f"Micro-expression: {shot.get('micro_expression','')}\n"
        f"Characters in shot: {'; '.join(shot_chars) if shot_chars else 'none / product only'}\n"
        f"Scene prompt: {shot.get('scene_prompt') or shot.get('scene','')}\n"
        f"Scene relationship / continuity: {shot.get('scene_relationship','')}\n"
        f"Props/product/scene: {shot.get('props','')} {shot.get('scene','')}\n"
        f"Important prop/asset prompts: {'; '.join(prop_prompts)}\n"
         f"Custom {panel_count}-panel plan: {'; '.join(str(p) if not isinstance(p, dict) else json.dumps(p, ensure_ascii=False, sort_keys=True) for p in panel_plan)}\n"
        + ("" if design_board else
           f"Audio continuity notes: voice={audio.get('voice','')}; bgm={audio.get('bgm','')}; sfx={audio.get('sfx','')}\n")
        + f"Video prompt notes: {shot.get('video_prompt_notes','')}\n"
        f"Overall visual style: {style}\n"
        f"Negative constraints: {bw_negative}字幕, 文字, 水印, logo, kinetic typography, 悬浮文字, 数据标签, 双胞胎, 分身, 同款重复人物, 畸形, 多手指, 面部扭曲, 低质量\n"
        + (("\n".join(continuity_bits)) + "\n" if continuity_bits else "")
          + asset_block
          + tag_legend
          + product_fact_block
          + usage_block
          + _panel_prompt_block(panel_source)
          + motion_note
          + (("\n【用户确认的中文导演提示词】\n" + str(shot.get("approved_prompt_zh")))
             if shot.get("approved_prompt_zh") else "")
          + no_character_block
    )


def shot_prompt(plan, shot, idx, bw=True, strict_bw=False):
    title = plan.get("project_title") or "marketing video"
    style = plan.get("visual_style") or "premium commercial storyboard, director's frame"
    chars = {c.get("id"): c for c in (plan.get("characters") or [])}
    shot_chars = []
    for cid in shot.get("characters") or []:
        c = chars.get(cid, {"id": cid})
        shot_chars.append(
            f"{c.get('name') or cid}: {c.get('appearance','')} {c.get('costume','')} {c.get('personality','')}"
        )
    no_character_block = ""
    if not shot_chars:
        no_character_block = (
            "\n[NO-CHARACTER SHOT HARD LOCK / 无人物镜头硬约束]\n"
            "This shot is explicitly characters: none. Do NOT draw any human face, head, body, torso, seated person, presenter, model, or full person in any panel. "
            "If the action explicitly requires interaction, show only cropped hands/fingers needed for the physical action; otherwise show product/props only. "
            "A lifestyle desktop may imply a user, but the user must remain off camera. This rule overrides any ambiguous wording such as 'young lifestyle scene' or old approved notes."
        )
    subject_def_block = _subject_definition_block(plan, shot, chars)
    continuity = shot.get("continuity") or plan.get("continuity") or {}
    continuity_bits = []
    bg = continuity.get("background") or shot.get("background") or shot.get("scene") or shot.get("props")
    if bg:
        continuity_bits.append(f"Background continuity: {bg}")
    if continuity.get("character_identity"):
        continuity_bits.append(f"Character identity continuity: {continuity['character_identity']}")
    if continuity.get("voice"):
        continuity_bits.append(f"Voice continuity: {continuity['voice']}")
    if continuity.get("bgm"):
        continuity_bits.append(f"BGM continuity: {continuity['bgm']}")
    if continuity.get("wardrobe"):
        continuity_bits.append(f"Wardrobe continuity: {continuity['wardrobe']}")
    if continuity.get("lighting"):
        continuity_bits.append(f"Lighting continuity: {continuity['lighting']}")
    prop_prompts = shot.get("prop_prompts") or shot.get("asset_prompts") or []
    if isinstance(prop_prompts, str):
        prop_prompts = [prop_prompts]
    design_board = _looks_like_design_board(plan)
    panel_source = shot
    if design_board and not shot.get("panel_plan"):
        panel_source = dict(shot, panel_plan=DEFAULT_DESIGN_BOARD_PANEL_PLAN)
    panel_plan = normalize_panel_plan(panel_source)
    audio = shot.get("audio") or {}
    director_bits = []
    for key, label in (("narrative_function", "Narrative function"),
                       ("felt_intent", "Audience feeling target"),
                       ("director_voice", "Directorial voice"),
                       ("arc_position", "Story arc position")):
        value = shot.get(key) or plan.get(key)
        if value:
            director_bits.append("%s: %s" % (label, value))
    director_block = (
        "\nDIRECTOR'S READ — derive camera, light, blocking, performance and sound from one intention; "
        "do not stack generic cinematic adjectives:\n  " + "\n  ".join(director_bits)
        if director_bits else ""
    )

    # ── 注入客户上传的素材图和数字人参考图 ──────────────────────────────────────
    # asset_refs 挂在 plan 级别（所有镜位共享），也可在 shot 级别覆盖。
    # 三类注入：
    #   1) product_images    — 产品图，每个镜位的产品外观必须严格对照
    #   2) scene_images      — 场景图，背景/陈列要与上传图一致
    #   3) digital_human_portraits — 数字人肖像，人脸/服装锁定
    # 这些 URL/路径会在 render_storyboard 里通过 API image_urls 参数实际传给模型。
    # 这里仅在文本 prompt 里明确声明，确保模型知道参考图的存在和用途。
    asset_refs = plan.get("asset_refs") or {}
    shot_asset_refs = shot.get("asset_refs") or {}  # shot 级覆盖

    product_imgs = shot_asset_refs.get("product_images") or asset_refs.get("product_images") or []
    product_usage_imgs = (shot_asset_refs.get("product_usage_images") or
                          asset_refs.get("product_usage_images") or [])
    if not _shot_needs_usage_reference(shot):
        product_usage_imgs = []
    scene_imgs = shot_asset_refs.get("scene_images") or asset_refs.get("scene_images") or []
    portrait_imgs = shot_asset_refs.get("digital_human_portraits") or asset_refs.get("digital_human_portraits") or []
    # @tag assignment: computed once here, reused both by asset_block (URL ->
    # tag legend) and by the tag_legend block further below (tag -> role).
    tagged_assets = _assign_asset_tags(product_imgs, product_usage_imgs, scene_imgs, portrait_imgs)
    _url_tag = {url: tag for tag, url, _category in tagged_assets}

    product_facts = shot.get("product_facts") or plan.get("product_facts") or {}
    product_fact_block = ""
    if product_facts and (product_imgs or shot.get("product") or shot.get("props")):
        facts = []
        for key in ("product_name", "product_type", "color", "price"):
            if product_facts.get(key):
                facts.append("%s=%s" % (key, product_facts[key]))
        if product_facts.get("usps"):
            facts.append("verified USPs=" + "; ".join(map(str, product_facts["usps"])))
        if product_facts.get("specs"):
            facts.append("verified specs=" + "; ".join(
                "%s:%s" % (k, v) for k, v in product_facts["specs"].items()))
        product_fact_block = (
            "\nPRODUCT FACTS FROM THE CLIENT BRIEF — MUST BE CONSISTENT WITH THE "
            "UPLOADED PRODUCT IMAGES; do not invent or contradict these visible facts:\n"
            "  " + "\n  ".join(facts)
        )
    usage_plan = dict(plan)
    usage_plan["asset_refs"] = dict(asset_refs)
    if product_imgs:
        usage_plan["asset_refs"]["product_images"] = product_imgs
    usage_plan["shots"] = [shot]
    usage_block = (
        "\n" + product_usage_prompt(usage_plan, shot)
        if needs_product_usage_image(usage_plan) and
        _shot_needs_usage_reference(shot)
        else ""
    )

    asset_block = ""
    if product_imgs or product_usage_imgs or scene_imgs or portrait_imgs:
        lines_a = [
            "\n\n[CRITICAL — CLIENT UPLOADED REFERENCE ASSETS]",
            "The following images were uploaded by the client and MUST be strictly referenced.",
            "Do NOT invent product shapes, colors, logos, or character faces from imagination.",
            "Every panel in this storyboard must be consistent with these references.",
        ]
        if product_imgs:
            lines_a.append("PRODUCT reference images (exact shape, color, logo, material must match):")
            lines_a.extend("  - %s %s" % (_url_tag.get(u, ""), u) for u in product_imgs)
        if product_usage_imgs:
            lines_a.append("CONFIRMED PRODUCT-IN-USE reference (match the person's real operation, hand contact, product orientation and visible functional details):")
            lines_a.extend("  - %s %s" % (_url_tag.get(u, ""), u) for u in product_usage_imgs)
        if scene_imgs:
            lines_a.append("SCENE / BACKGROUND reference images (layout, props, environment must match):")
            lines_a.extend("  - %s %s" % (_url_tag.get(u, ""), u) for u in scene_imgs)
        if portrait_imgs:
            lines_a.append("CHARACTER / DIGITAL HUMAN portrait references (face, hair, outfit IDENTITY LOCKED):")
            lines_a.extend("  - %s %s" % (_url_tag.get(u, ""), u) for u in portrait_imgs)
        asset_block = "\n".join(lines_a)
    layout = _shot_prompt_layout_context(
        plan, shot, bw=bw, strict_bw=strict_bw, design_board=design_board,
        tagged_assets=tagged_assets, panel_plan=panel_plan)
    return _format_shot_prompt(
        shot=shot, idx=idx, title=title, style=style, shot_chars=shot_chars,
        prop_prompts=prop_prompts, panel_plan=panel_plan,
        panel_source=panel_source, design_board=design_board,
        director_block=director_block, subject_def_block=subject_def_block,
        audio=audio, continuity_bits=continuity_bits, asset_block=asset_block,
        product_fact_block=product_fact_block, usage_block=usage_block,
        no_character_block=no_character_block, layout=layout)


def contact_sheet_prompt(plan, bw=True, reference_registry=None):
    """Prompt for a single-shot contact sheet with explicit reference mapping."""
    shots = plan.get("shots") or []
    shot = shots[0] if shots else {}
    if _looks_like_design_board(plan):
        return design_board_prompt(
            plan, shot, 1, bw=bw, strict_bw=False,
            reference_registry=reference_registry)
    if shot.get("approved_submission_prompt_zh"):
        return str(shot["approved_submission_prompt_zh"])
    base = shot_prompt(plan, shot, 1, bw=bw, strict_bw=False)
    registry = reference_registry or []
    if not registry:
        return base
    tags = {str(item.get("tag") or "") for item in registry}
    priority_parts = []
    if "@usage" in tags:
        priority_parts.append("@usage product-in-use relationship first")
    if any("product" in str(item.get("type") or "") for item in registry):
        priority_parts.append("product-board/product refs for product shape")
    if any("character" in str(item.get("type") or "") for item in registry):
        priority_parts.append("character refs for face/body/wardrobe")
    if any("scene" in str(item.get("type") or "") for item in registry):
        priority_parts.append("scene refs for environment only")
    priority_text = "; ".join(priority_parts) or "only the attached refs listed below"
    lines = [
        "\n[CONTACT SHEET REFERENCE REGISTRY / 故事板联系表参考图注册表]",
        "These uploaded images are attached to this generation request. Treat each @tag as an identity-locked visual anchor, not as optional inspiration.",
        "When the shot or panel plan says refs=@tag, the matching uploaded image must control the visible subject identity, product geometry, operation relationship, and continuity.",
        "Do not average conflicting references. Priority for this request: %s." % priority_text,
    ]
    for item in registry:
        lines.append(
            "  %s = %s; role=%s; source=%s" % (
                item.get("tag", ""),
                item.get("source") or item.get("url") or "",
                item.get("type") or "generic_visual",
                item.get("url") or "",
            )
        )
    return base + "\n" + "\n".join(lines)
