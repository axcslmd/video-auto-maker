#!/usr/bin/env python3
"""Compile structured shot data into a Seedance-native video prompt."""
import json
import re

SEEDANCE_MODELS = {"seedance-2.0", "seedance-2.0-fast", "seedance-2.0-white"}
SEEDANCE_MAX_DURATION = 15
MOTION_TERMS = ("static", "locked", "push", "pull", "pan", "tilt", "orbit", "arc",
                "dolly", "tracking", "handheld", "whip", "zoom", "crane", "overhead",
                "环绕", "推进", "拉远", "横摇", "竖摇", "跟拍", "手持", "快速平移", "固定机位", "俯拍")

# 安全区指令模板（与 motion_design.SAFE_ZONES 对齐）
_SAFE_ZONE_HINTS = {
    "lower_third": "Keep the lower 25% of the frame clear of key action, faces, and product details; this space is reserved for subtitle overlay.",
    "upper_third": "Keep the upper 20% of the frame clear of key action and faces; this space is reserved for title overlay.",
    "center": "Keep the center of the frame clear for a title reveal; position key subjects slightly off-center.",
    "corner": "Keep the upper-right corner clear; a small data card will appear there.",
    "left": "Keep the left 30% of the frame clear; text content will appear on the left side.",
    "right": "Keep the right 30% of the frame clear; text content will appear on the right side.",
}


def is_seedance_model(model):
    value = str(model or "").lower()
    return value in SEEDANCE_MODELS or "seedance" in value


def _uses_native_storyboard_prompt(segment, model):
    mode = str((segment or {}).get("storyboard_ref_mode") or "").strip().lower()
    if mode in ("expanded_panel", "single_panel", "single_shot_keyframe"):
        return False
    return (not mode or mode in ("native_storyboard", "contact_sheet", "storyboard_contact_sheet")) \
        and is_seedance_model(model)


def _clean(value):
    return re.sub(r"\s+", " ", str("" if value is None else value)).strip()


def _motion(value):
    text = _clean(value)
    if not text:
        return "固定机位"
    for term in MOTION_TERMS:
        if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(term.lower()), text.lower()):
            return term
    return text


def _reference_clause(ref, index):
    ref_type = ref.get("type") or ref.get("reference_type")
    clauses = {
        "character_identity": "只锁定脸型、五官、发际线、发型、年龄、服装、配饰和身体比例；不要复制参考图背景、姿势、构图或光线",
        "product_identity": "严格保持产品结构、颜色、材质、比例、零件位置和原生标识；不得添加、删除、替换或重新设计产品，不复制背景和构图",
        "scene_environment": "只继承空间布局、布景材质、光线方向和色彩氛围；不要复制图中偶然出现的人物、产品、文字或姿势",
        "storyboard_composition": "读取镜头构图、机位、景别、主体位置、动作顺序和摄像机运动；读取导演标注作为运动指导（红箭头=身体运动方向和力度、蓝箭头=镜头运动方向和速度、绿标记=构图意图、橙标记=灯光方向、紫标记=声音/情绪强调），将这些标注转译为真实运镜和动作，但绝不在成片中渲染标注本身、箭头、素描风格、网格或面板边框",
        "continuation_frame": "只作为本段连续性起点，继承人物位置、动作状态、镜头方向和光线；不得当作整段固定构图或重置场景",
    }
    if ref_type in clauses:
        tag = ref.get("tag") or ("@ref%d" % index)
        line = "%s（素材%d，%s）：%s。" % (tag, index, ref.get("label", "参考图"), clauses[ref_type])
        scope = ref.get("scope")
        if scope == "beat":
            line += " 此素材仅适用于指定镜头时间窗，不得扩大到其他镜头。"
        elif scope == "scene":
            line += " 此约束适用于本场景。"
        elif scope == "clip":
            line += " 此约束仅适用于本次生成片段。"
        return line
    return "素材%d（%s）：%s，%s。" % (
        index, ref.get("label", "参考图"), ref.get("role", "视觉锚定"),
        ref.get("intent", "保持一致"))


def compile_prompt(segment, refs=None, *, style=None, negative=None, target_model="seedance-2.0"):
    """Build a director-grade video prompt optimized for the target model.

    Structure follows 2026 best practices (researched from Seedance/Kling/Veo/Wan):
    1. Lead with shot type + camera movement (Kling) or subject + action (Seedance)
    2. One clear moment per generation — not a scene's worth of choreography
    3. Specific cinematography terms (Kling responds to these)
    4. Character identity re-injected per segment
    5. Style anchors (1-2 strong, not 10 adjectives)
    6. Constraints at the end (negative instructions last)
    """
    timeline = segment.get("timeline") or []
    if not timeline:
        duration = segment.get("duration") or 5
        timeline = [{"start": 0, "end": duration,
                     "action": segment.get("action") or segment.get("visual") or segment.get("text"),
                     "camera": segment.get("camera_movement") or segment.get("camera"),
                     "sound": segment.get("sound") or segment.get("sfx")}]
    duration = segment.get("duration") or 5
    is_kling = not is_seedance_model(target_model)

    # ── 核心指令行：模型专属结构 ──
    # Kling: 镜头语言优先（"Slow push-in from wide shot..."）
    # Seedance: 主体行为优先（"A luxury perfume bottle rotates..."）
    core = _build_core_instruction(segment, timeline, duration, is_kling)

    # ── 故事板规则（storyboard_ref 时必须，之前已修复短路）──
    storyboard_ref = bool(segment.get("storyboard_ref") or segment.get("storyboard_ref_mode"))
    lines = []
    if storyboard_ref:
        lines.append(_storyboard_rules_block(
            native=_uses_native_storyboard_prompt(segment, target_model)))

    # ── 核心指令（含模型标识用于调试区分）──
    model_label = "Kling" if is_kling else "Seedance 2.0"
    lines.append("%s 视频提示词：%s" % (model_label, core))

    # ── 连续性锁定（断层/服装/音画）──
    continuity_lines = _build_continuity_lock(segment)
    if continuity_lines:
        lines.extend(continuity_lines)

    # ── 音频契约（完整音频指令）──
    audio = segment.get("audio_contract") or {}
    if audio and audio.get("track") != "none":
        audio_parts = []
        if audio.get("dialogue"):
            audio_parts.append("台词：%s" % _clean(audio["dialogue"]))
        if audio.get("language"):
            audio_parts.append("语言/口音：%s" % _clean(audio["language"]))
        if audio.get("voice_direction") or audio.get("voice"):
            audio_parts.append("声音人设：%s" % _clean(
                audio.get("voice_direction") or audio.get("voice")))
        if audio.get("bgm"):
            audio_parts.append("背景音乐：%s" % _clean(audio["bgm"]))
        if audio.get("sfx"):
            audio_parts.append("音效：%s" % _clean(audio["sfx"]))
        if audio.get("voice_continuity"):
            audio_parts.append("声音连续性：%s" % _clean(audio["voice_continuity"]))
        if audio.get("bgm_continuity"):
            audio_parts.append("BGM连续性：%s" % _clean(audio["bgm_continuity"]))
        if audio.get("sfx_continuity"):
            audio_parts.append("音效连续性：%s" % _clean(audio["sfx_continuity"]))
        if audio.get("voice_continuity_method"):
            audio_parts.append("声音连续性方法：%s" % _clean(audio["voice_continuity_method"]))
        if audio.get("bgm_continuity_method"):
            audio_parts.append("BGM连续性方法：%s" % _clean(audio["bgm_continuity_method"]))
        if audio.get("media_reference_method"):
            audio_parts.append("音频参考方式：%s" % _clean(audio["media_reference_method"]))
        if audio.get("lip_sync"):
            audio_parts.append("口型同步：必须")
        if audio_parts:
            lines.append("音频契约：%s。" % "；".join(audio_parts))

    # ── 已确认渲染方案 ──
    render_plan = (segment.get("render_plan") or {}).get("content") or {}
    if render_plan:
        lines.append("已确认渲染方案：%s。必须实际执行。" % json.dumps(
            render_plan, ensure_ascii=False, sort_keys=True))

    # ── 参考图标注（告诉模型每张图是什么）──
    refs = refs or segment.get("references") or segment.get("reference_roles") or []
    for i, ref in enumerate(refs, 1):
        if isinstance(ref, dict):
            lines.append(_reference_clause(ref, i))

    # ── 导演意图（叙事功能 + 观众感受）──
    director = _build_director_intent(segment)
    if director:
        lines.append(director)

    # ── 身份锁定 ──
    if segment.get("product_identity_lock"):
        lines.append("产品一致性锁：只允许展示参考产品本身，不添加、替换或重设计产品零件、材质、颜色和比例。")
    if segment.get("character_identity_lock"):
        lines.append("人物一致性锁：只允许使用参考人物本身，不改变脸型、发型、服装、年龄和身体比例。")

    # ── 负向约束（最后）──
    if negative:
        lines.append("避免：%s。" % _clean(negative))
    return "\n".join(lines)


def build(segment, model="seedance-2.0", refs=None, style=None, negative=None,
          target_model=None):
    """Backward-compatible alias for older call sites."""
    return compile_prompt(
        segment, refs, style=style, negative=negative,
        target_model=target_model or model)


def _build_core_instruction(segment, timeline, duration, is_kling):
    """Build the core instruction using model-specific structure.

    Kling: camera-first (responds to specific cinematography terms)
    Seedance: subject-first (responds to subject behavior descriptions)
    Both: one clear moment, specific lighting, style anchor, structured details.
    """
    ratio = _clean(segment.get("ratio") or "9:16")
    style = segment.get("style") or "photorealistic commercial"

    # Extract the primary action from the first timeline beat
    first_beat = timeline[0] if timeline else {}
    action = _clean(first_beat.get("action") or first_beat.get("visual")
                    or segment.get("visual") or segment.get("text") or "主体自然运动")
    camera = _motion(first_beat.get("camera") or first_beat.get("movement")
                     or segment.get("camera_movement") or segment.get("camera"))
    lighting = _clean(first_beat.get("lighting") or segment.get("lighting") or "")
    scene = _clean(first_beat.get("scene_prompt") or segment.get("scene_prompt") or "")

    # Bind the action to concrete input images in the same sentence. This is
    # deliberately not a detached "reference list" that the model must map.
    ref_tags = segment.get("ref_tags") or first_beat.get("ref_tags") or []
    if isinstance(ref_tags, str):
        ref_tags = [ref_tags]
    inline_refs = " ".join(ref_tags)
    if inline_refs:
        action = "镜头%d，%s %s" % (int(segment.get("storyboard_panel_index") or 1), inline_refs, action)

    # Build the core instruction
    parts = []
    if is_kling:
        # Kling: camera direction first
        if camera and camera != "固定机位":
            parts.append("%s，%s秒" % (camera, duration))
        else:
            parts.append("固定机位，%s秒" % duration)
        parts.append(action)
    else:
        # Seedance: subject behavior first
        parts.append(action)
        if camera and camera != "固定机位":
            parts.append("镜头%s" % camera)
        parts.append("时长%s秒" % duration)

    # Structured details from first beat (景别/构图/人物动作/微表情等)
    details = []
    for key, label in (("shot_size", "景别"), ("angle_offset", "角度"),
                       ("composition", "构图"), ("lighting", "灯光"),
                       ("character_action", "人物动作"),
                       ("micro_expression", "微表情"),
                       ("scene_prompt", "场景"), ("prop_prompts", "道具/产品")):
        value = first_beat.get(key) or segment.get(key)
        if isinstance(value, list):
            value = "；".join(_clean(v) for v in value)
        if value:
            details.append("%s：%s" % (label, _clean(value)))
    if details:
        parts.append("；".join(details))
    bindings = segment.get("reference_bindings") or []
    if bindings:
        parts.append("参考角色：" + "；".join(
            "%s=%s，%s" % (item.get("tag"), item.get("role"),
                           "必须可见" if item.get("must_be_visible") else "仅作环境锚定")
            for item in bindings if item.get("tag")))

    # Environment and lighting (if not already in details)
    if scene and not any("场景" in d for d in details):
        parts.append("场景：%s" % scene)
    if lighting and not any("灯光" in d for d in details):
        parts.append("灯光：%s" % lighting)

    # Style anchor (1-2 strong, not 10 adjectives)
    parts.append("风格：%s" % style)
    parts.append("画幅：%s" % ratio)

    # Additional timeline beats (if multi-beat)
    if len(timeline) > 1:
        beat_lines = []
        for item in timeline[1:]:
            beat_action = _clean(item.get("action") or item.get("visual") or "主体自然运动")
            beat_camera = _motion(item.get("camera") or item.get("movement"))
            beat_start = _clean(item.get("start", 0))
            beat_end = _clean(item.get("end", duration))
            beat_line = "%s-%s秒：%s" % (beat_start, beat_end, beat_action)
            if beat_camera and beat_camera != "固定机位":
                beat_line += "，镜头%s" % beat_camera
            if item.get("sound") or item.get("sfx"):
                beat_line += "，声音/情绪：%s" % _clean(item.get("sound") or item.get("sfx"))
            beat_lines.append(beat_line)
        if beat_lines:
            parts.append("后续节奏：" + "；".join(beat_lines))

    # First beat time range (always include for temporal structure)
    first_start = _clean(first_beat.get("start", 0))
    first_end = _clean(first_beat.get("end", duration))
    time_range = "%s-%s秒" % (first_start, first_end)
    if parts:
        parts[0] = time_range + "：" + parts[0]

    # Sound/SFX from first beat
    if first_beat.get("sound") or first_beat.get("sfx"):
        parts.append("声音/情绪：%s" % _clean(first_beat.get("sound") or first_beat.get("sfx")))

    # Continuity endpoint
    if segment.get("continuity_out"):
        parts.append("衔接点：%s" % _clean(segment["continuity_out"]))

    return "。".join(p for p in parts if p) + "。"


def _storyboard_rules_block(native=False):
    """Compact storyboard-to-video rules (always included when storyboard_ref=True)."""
    if native:
        return ("【Seedance 原生故事板转视频规则】输入的是客户确认过的最终多格故事板/contact sheet；"
                "只执行当前 segment/panel index 对应镜头，但可以读取整张故事板里的前后镜头关系、构图递进、"
                "动作节奏、机位变化、光线和情绪弧线，保证该片段展开后能与上下片段连贯剪接。"
                "故事板是黑白素描预演，仅用于分镜、构图和动作顺序参考；成片必须是真实摄影质感的彩色商业视频，"
                "绝不输出素描、铅笔画、炭笔画、故事板格子、分屏或面板边框。"
                "标注颜色只用于读取导演意图：RED arrows = body / subject movement；BLUE arrows = camera movement；"
                "GREEN marks = framing and composition notes；ORANGE marks = lighting direction；"
                "PURPLE marks = sound and emotional emphasis；BLACK text = short shot notes and panel labels；"
                "Do NOT render any arrows, marks, labels or notes. NEVER render the video as a sketch. "
                "导演标注（红箭头=身体运动、蓝箭头=镜头运动、绿标记=构图、橙标记=灯光、紫标记=情绪）"
                "必须读取并转译为真实运镜、动作和声音情绪，但绝不渲染标注本身。")
    return ("【单镜头展开图转视频规则】输入的故事板参考图是当前镜头专属的单格展开图，不是整张多格故事板；"
            "只生成该镜头一个清晰时刻，绝不推断或复现其他格。故事板是黑白素描预演，仅用于构图和动作顺序参考；"
            "成片必须是真实摄影质感的彩色商业视频，绝不输出素描、铅笔画、炭笔画或故事板格子画面。"
            "NEVER render the video as a sketch, pencil drawing, charcoal drawing, storyboard panel, animatic, comic strip or grayscale previs."
            "导演标注（红箭头=身体运动、蓝箭头=镜头运动、绿标记=构图、橙标记=灯光、紫标记=情绪）"
            "必须读取并转译为真实运镜和动作，但绝不渲染标注本身。")


def _build_director_intent(segment):
    """Build director intent line from clip contract."""
    contract = segment.get("clip_contract") or {}
    clip_scope = (contract.get("scopes") or {}).get("clip") or {}
    parts = []
    for key, label in (("narrative_function", "叙事功能"), ("felt_intent", "观众感受目标"),
                       ("director_voice", "导演语气"), ("arc_position", "叙事弧位置")):
        if clip_scope.get(key):
            parts.append("%s：%s" % (label, _clean(clip_scope[key])))
    if not parts:
        return None
    return "导演意图：%s。所有运镜、灯光、表演和声音必须服务同一个意图。" % "；".join(parts)


def _build_continuity_lock(segment):
    """Build enhanced continuity lock lines based on segment context.

    Addresses three production quality issues:
      1. Segment discontinuity (断层) — tail-frame chaining + visual state lock
      2. Clothing inconsistency (服装漂移) — explicit clothing description lock
      3. Voice/lip-sync drift (音画不同步) — voice pacing + duration constraint
    """
    lines = []

    # ── 服装锁定（修复人物服装不一致）──
    clothing = segment.get("clothing_lock") or segment.get("clothing_description")
    if clothing:
        lines.append(
            "服装严格锁定（最高优先级）：%s。不允许更换、添加、删除或改变任何服装元素；"
            "不允许改变颜色、材质、纹理、图案或配饰。每一帧的服装都必须与参考图完全一致。"
            % _clean(clothing))
    elif segment.get("character_identity_lock"):
        lines.append(
            "服装严格锁定：人物必须穿着与参考图完全相同的服装、配饰和鞋子；"
            "不允许更换颜色、款式、材质或添加/删除任何服装元素。")

    # ── 尾帧串联连续性（修复片段衔接断层）──
    if segment.get("_chain_tail_frame") or segment.get("extend_video"):
        lines.append(
            "段间连续性锁定（关键）：本段从上一段的最后一帧无缝继续。第一帧必须与参考图"
            "（上一段尾帧）在人物位置、姿势、表情、服装、场景、光线、色调、镜头角度上完全一致。"
            "不允许跳变、重置场景、改变人物位置或改变光线方向。")
        if segment.get("continuity_in"):
            lines.append(
                "上一段结束状态：%s。本段必须从这个精确状态自然延续。" % _clean(segment["continuity_in"]))

    # ── 口播音画同步锁定（修复音画不同步 + 长口播嘴形配音不一致）──
    audio = segment.get("audio_contract") or {}
    dialogue = audio.get("dialogue") or segment.get("dialogue") or ""
    duration = segment.get("duration") or 5
    if dialogue and segment.get("oral_broadcast"):
        lines.append(
            "口播音画同步锁定：台词必须在 %s 秒内以自然语速念完；不允许加速或减速。"
            "嘴唇动作必须与发音精确同步，从第一个字到最后一个字。不允许提前闭嘴或延迟开口。"
            "配音音色、音调、语速、情感必须与上一段完全一致，如同同一个人连续说话。"
            % _clean(duration))
        if segment.get("extend_video"):
            lines.append(
                "音频连续性：这是上一段口播的自然延续。声音特质（音色、音调、语速、口音、情感）"
                "必须与上一段完全一致，听众不应察觉到段落切换。第一句话紧接上一段最后一句的语气。")
    elif dialogue:
        lines.append(
            "音画同步：台词必须在 %s 秒内以自然语速念完，嘴唇动作与发音精确同步。"
            % _clean(duration))

    # ── 画面安全区（为后期字幕/动效预留空间）──
    safe_zones = segment.get("video_safe_zones") or []
    motion_design = segment.get("motion_design") or {}
    if not safe_zones and motion_design:
        safe_zones = motion_design.get("video_safe_zones") or []
    if safe_zones:
        hints = [_SAFE_ZONE_HINTS[z] for z in safe_zones if z in _SAFE_ZONE_HINTS]
        if hints:
            lines.append(
                "画面构图安全区（重要）：%s 这些区域不要放置关键动作、人脸或产品细节，"
                "后期会在此区域叠加文字/动效。" % " ".join(hints))

    return lines


def audit_segments(segments):
    issues = []
    previous = None
    for index, segment in enumerate(segments or [], 1):
        if not (segment.get("urls") or segment.get("refs") or segment.get("allow_text2video")):
            issues.append({"index": index, "severity": "error", "code": "MISSING_REFERENCE"})
        if not (segment.get("camera") or segment.get("camera_movement") or segment.get("timeline")):
            issues.append({"index": index, "severity": "warning", "code": "MISSING_MOTION"})
        if len(segment.get("timeline") or []) > 4:
            issues.append({"index": index, "severity": "warning", "code": "TOO_MANY_BEATS"})
        references = segment.get("references")
        if references is not None and len(segment.get("urls") or []) != len(references):
            issues.append({"index": index, "severity": "error", "code": "REFERENCE_COUNT_MISMATCH"})
        if (previous and segment.get("extend_video") and previous.get("scene_id")
                and segment.get("scene_id") and previous.get("scene_id") != segment.get("scene_id")):
            issues.append({"index": index, "severity": "error", "code": "CROSS_SCENE_EXTENSION"})
        for beat in segment.get("timeline") or []:
            if float(beat.get("start", 0)) < 0 or float(beat.get("end", 0)) > float(segment.get("duration") or 0):
                issues.append({"index": index, "severity": "error", "code": "BEAT_OUT_OF_RANGE"})
        previous = segment
    return {"ok": not any(i["severity"] == "error" for i in issues), "issues": issues}
