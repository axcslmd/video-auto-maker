#!/usr/bin/env python3
"""阶段7 · 本地 Remotion 剪辑：方案命令 + 动效素材 → 最终成片。

定位（修正后的正确顺序，最后一步）：
  阶段6 video_reverse.py 产出 remotion_scheme.json（逐镜头方案命令）。
  本脚本把方案命令编译成 Remotion shotlist：已确认的底片作为背景视频层，
  按方案的 camera_move 套运镜，按 motion_overlay 叠动效素材（字幕/数据/图形，
  对齐项目自有 Remotion 组件风格的内容页），本地渲染出最终成片。

分工：
  - 底片（阶段5 已确认）= 背景视频层（含数字人/口播/画面/环境声）。
  - Remotion = 在底片之上叠运镜微调 + 动效字幕/图形 + 转场（本地渲染，零 Credit）。
  - 方案的 prohibitions（防变脸/服装漂移等）在阶段4 出底片时已通过 negative_prompt 生效；
    本阶段是纯剪辑叠加，不再生成人物，故禁止项此处仅作元信息留档。

映射规则（scheme.shots[i] → Remotion Shot）：
  camera_move（推/拉/摇/移/固定）→ move（push_in/pull_out/pan_*/tilt_*/still/ken_burns）
  start_sec/end_sec → durationInFrames（按 fps）
  transition_to_next → 下一镜的 transition（fade/slide/cut）
  motion_overlay → title/bullets（从叠加建议里提炼）

CLI:
  compile --scheme output/remotion_scheme.json --basecut output/basecut.mp4 \
          --brand <客户确认主色hex> --out-shotlist output/final_shotlist.json
  render  --shotlist output/final_shotlist.json --out output/final.mp4
  run     --scheme ... --basecut ... --brand <客户确认主色hex> --out output/final.mp4
"""
import os
import re
import sys
import json
import argparse
import shutil
import subprocess
import math
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import media_qc  # noqa: E402
import run_manifest as rm  # noqa: E402
import script_splitter  # noqa: E402
import design_token_contract  # noqa: E402

# 逆向方案的 camera_move 自由文本 → Remotion Move 枚举。
# 白名单精确匹配：先做全字符串精确匹配，再做词级匹配（避免"推"误命中"推荐"）。
_MOVE_EXACT = {
    "推": "push_in", "推进": "push_in", "推近": "push_in",
    "push": "push_in", "push_in": "push_in", "push in": "push_in",
    "zoom_in": "push_in", "zoom in": "push_in", "zoomin": "push_in",
    "拉": "pull_out", "拉远": "pull_out", "拉出": "pull_out",
    "pull": "pull_out", "pull_out": "pull_out", "pull out": "pull_out",
    "zoom_out": "pull_out", "zoom out": "pull_out", "zoomout": "pull_out",
    "左摇": "pan_left", "左移": "pan_left", "左移镜头": "pan_left",
    "pan_left": "pan_left", "pan left": "pan_left", "panleft": "pan_left",
    "右摇": "pan_right", "右移": "pan_right", "右移镜头": "pan_right",
    "pan_right": "pan_right", "pan right": "pan_right", "panright": "pan_right",
    "上摇": "tilt_up", "上移": "tilt_up", "仰拍": "tilt_up",
    "tilt_up": "tilt_up", "tilt up": "tilt_up", "tiltup": "tilt_up",
    "下摇": "tilt_down", "下移": "tilt_down", "俯拍": "tilt_down",
    "tilt_down": "tilt_down", "tilt down": "tilt_down", "tiltdown": "tilt_down",
    "固定": "still", "静止": "still", "固定机位": "still", "不动": "still",
    "still": "still", "fixed": "still", "static": "still",
    "缓慢": "ken_burns", "漂移": "ken_burns", "缓慢移动": "ken_burns",
    "ken_burns": "ken_burns", "ken burns": "ken_burns", "kenburns": "ken_burns",
}

# 词级匹配：把文本按空格/标点分词后逐词查表
_MOVE_WORDS = {}
for key, val in _MOVE_EXACT.items():
    for word in key.replace("_", " ").split():
        _MOVE_WORDS[word.lower()] = val

_TRANSITION_EXACT = {
    "淡": "fade", "淡入": "fade", "淡出": "fade", "淡化": "fade",
    "fade": "fade", "fade_in": "fade", "fade in": "fade",
    "fade_out": "fade", "fade out": "fade", "dissolve": "fade",
    "溶": "fade", "溶接": "fade",
    "滑": "slide", "滑动": "slide", "滑入": "slide", "滑出": "slide",
    "slide": "slide", "slide_in": "slide", "slide in": "slide",
    "slide_out": "slide", "slide out": "slide", "wipe": "slide",
    "硬切": "cut", "直切": "cut", "切": "cut", "切换": "cut",
    "cut": "cut", "hard_cut": "cut", "hard cut": "cut", "jump_cut": "cut",
}

_TRANSITION_WORDS = {}
for key, val in _TRANSITION_EXACT.items():
    for word in key.replace("_", " ").split():
        _TRANSITION_WORDS[word.lower()] = val


def _map_move(text):
    if not text:
        return "still"
    s = str(text).strip().lower()
    # 1. 全字符串精确匹配
    if s in _MOVE_EXACT:
        return _MOVE_EXACT[s]
    # 2. 词级匹配
    for word in re.split(r"[\s,，、；;]+", s):
        if word in _MOVE_WORDS:
            return _MOVE_WORDS[word]
    # 3. 中文复合词子串匹配（避免"推"误命中"推荐"，使用更长的不歧义子串）
    _COMPOUND = [
        ("推近", "push_in"), ("推进", "push_in"), ("缓慢推", "push_in"),
        ("拉近", "pull_out"), ("拉远", "pull_out"),
        ("左摇", "pan_left"), ("向左", "pan_left"), ("横摇向左", "pan_left"),
        ("右摇", "pan_right"), ("向右", "pan_right"), ("横摇向右", "pan_right"),
        ("上摇", "tilt_up"), ("向上", "tilt_up"),
        ("下摇", "tilt_down"), ("向下", "tilt_down"),
        ("固定", "still"), ("静止", "still"),
        ("漂移", "ken_burns"), ("缓慢移", "ken_burns"),
    ]
    for substr, mv in _COMPOUND:
        if substr in s:
            return mv
    return "still"


def _map_transition(text):
    if not text:
        return "cut"
    s = str(text).strip().lower()
    if s in _TRANSITION_EXACT:
        return _TRANSITION_EXACT[s]
    for word in re.split(r"[\s,，、；;]+", s):
        if word in _TRANSITION_WORDS:
            return _TRANSITION_WORDS[word]
    # 中文复合词子串匹配
    _COMPOUND = [
        ("淡入", "fade"), ("淡出", "fade"), ("淡化", "fade"),
        ("滑入", "slide"), ("滑出", "slide"),
        ("硬切", "cut"), ("直切", "cut"),
    ]
    for substr, tr in _COMPOUND:
        if substr in s:
            return tr
    return "cut"


def _overlay_to_content(motion_overlay):
    """把 motion_overlay 建议文本拆成 (title, bullets)。
    约定：第一句/冒号前作标题，其余按顿号/分号/换行拆成要点。空则返回 (None, [])。"""
    if not motion_overlay or not str(motion_overlay).strip():
        return None, []
    s = str(motion_overlay).strip()
    # 去掉「叠加/字幕/建议」等前缀噪声
    s = re.sub(r"^(叠加|建议|字幕|overlay|动效)[:：]?\s*", "", s, flags=re.IGNORECASE)
    parts = re.split(r"[；;\n]|、", s)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return None, []
    if len(parts) == 1:
        # 单句：冒号前作标题
        m = re.split(r"[：:]", parts[0], 1)
        if len(m) == 2 and m[0].strip():
            return m[0].strip(), [m[1].strip()]
        return parts[0][:24], []
    return parts[0][:24], parts[1:5]


# 动效建议 style（自由文本/枚举）→ Remotion MotionOverlay 组件的规范 style。
# 逆向 SCHEME_SPEC 建议枚举：title_reveal/bullet_list/metric_pop/lower_third/keyword_flash/data_card
_MOTION_STYLE_MAP = [
    (r"metric|data.?card|数据|指标|图表|卡片", "data_card"),
    (r"lower.?third|下三分|条幅|字条|署名条", "lower_third"),
    (r"keyword|flash|快闪|关键词|标签", "keyword_flash"),
    (r"bullet|list|要点|清单|列表", "bullet_list"),
    (r"title|reveal|标题|大字", "title_reveal"),
]
_MOTION_POS_MAP = [
    (r"lower.?third|下三分", "lower_third"),
    (r"top|顶部|上方", "top"),
    (r"bottom|底部|下方", "bottom"),
    (r"left|左", "left"),
    (r"right|右", "right"),
    (r"corner|角", "corner"),
    (r"center|中间|居中", "center"),
]


def _map_motion_style(text, has_metric=False, n_bullets=0):
    """从 motion_suggestion.style 自由文本推断规范动效 style。
    无法命中时按内容形态兜底：有 metric→data_card，多要点→bullet_list，否则 title_reveal。"""
    if text:
        for pat, st in _MOTION_STYLE_MAP:
            if re.search(pat, str(text), re.IGNORECASE):
                return st
    if has_metric:
        return "data_card"
    if n_bullets >= 1:
        return "bullet_list"
    return "title_reveal"


def _map_motion_position(text, style):
    """从 motion_suggestion.position 推断屏幕位置。缺省按 style 给合理默认。"""
    if text:
        for pat, pos in _MOTION_POS_MAP:
            if re.search(pat, str(text), re.IGNORECASE):
                return pos
    # 按 style 兜底默认位
    return {"data_card": "corner", "lower_third": "lower_third",
            "keyword_flash": "center", "bullet_list": "center",
            "title_reveal": "center"}.get(style, "center")


def _build_motion_overlay(shot):
    """把 shot 的动效设计编译成 Remotion MotionOverlay 组件 props。

    优先级：
      1. motion_design（motion_design.py 导演级预规划）→ 直接使用
      2. motion_suggestion + motion_content（video_reverse 逆向工程三分离）
      3. motion_overlay 自由文本兜底
    """
    # 1. 导演级预规划动效设计（最高优先级）
    md = shot.get("motion_design")
    if isinstance(md, dict):
        overlay_spec = md.get("motion_overlay") or {}
        if overlay_spec.get("style") and overlay_spec["style"] != "none":
            result = {
                "style": overlay_spec["style"],
                "position": overlay_spec.get("position") or "center",
                "timing": overlay_spec.get("timing") or "",
            }
            if overlay_spec.get("preset"):
                result["preset"] = overlay_spec["preset"]
            if overlay_spec.get("size_px") is not None:
                result["size"] = int(overlay_spec["size_px"])
            if overlay_spec.get("width_px") is not None:
                result["width_px"] = int(overlay_spec["width_px"])
            if overlay_spec.get("title"):
                result["title"] = overlay_spec["title"]
            if overlay_spec.get("bullets"):
                result["bullets"] = overlay_spec["bullets"][:5]
            if overlay_spec.get("metric"):
                m = overlay_spec["metric"]
                result["metric"] = {"value": str(m.get("value", "")),
                                    "label": str(m.get("label", ""))}
            return result

    # 2. 逆向工程三分离字段
    sug = shot.get("motion_suggestion") if isinstance(shot.get("motion_suggestion"), dict) else {}
    content = shot.get("motion_content") if isinstance(shot.get("motion_content"), dict) else {}

    title = (content.get("title") or "").strip() if content else ""
    bullets = [str(b).strip() for b in (content.get("bullets") or []) if str(b).strip()] if content else []
    metric = content.get("metric") if isinstance(content.get("metric"), dict) else None
    has_metric = bool(metric and (metric.get("value") or metric.get("label")))

    # 无结构化内容 → 从自由文本 motion_overlay 兜底拆
    if not (title or bullets or has_metric):
        t, b = _overlay_to_content(shot.get("motion_overlay"))
        if not (t or b):
            return None
        title, bullets = t or "", b

    style = _map_motion_style(sug.get("style") if sug else None,
                              has_metric=has_metric, n_bullets=len(bullets))
    position = _map_motion_position(sug.get("position") if sug else None, style)
    overlay = {
        "style": style,
        "position": position,
        "timing": (sug.get("timing") if sug else "") or "",
    }
    if title:
        overlay["title"] = title
    if bullets:
        overlay["bullets"] = bullets[:5]
    if has_metric:
        overlay["metric"] = {"value": str(metric.get("value", "")).strip(),
                             "label": str(metric.get("label", "")).strip()}
    return overlay


def _load_subtitles(scheme):
    """从 scheme.subtitles（规范化后 [{start_sec,end_sec,text}]）读取字幕。
    兼容：若 scheme 无 subtitles 但有 _srt 路径，则解析 srt 文件。返回 [] 表示无字幕。"""
    subs = scheme.get("subtitles")
    if isinstance(subs, list) and subs:
        out = []
        for it in subs:
            if not isinstance(it, dict):
                continue
            txt = (it.get("text") or "").strip()
            if not txt:
                continue
            out.append({"start_sec": it.get("start_sec"), "end_sec": it.get("end_sec"), "text": txt})
        if out:
            return out
    srt_path = scheme.get("_srt")
    if srt_path and os.path.exists(srt_path):
        return _parse_srt(srt_path)
    return []


def _srt_ts_to_sec(ts):
    """SRT 时间码 HH:MM:SS,mmm → 秒。"""
    ts = ts.strip().replace(".", ",")
    hms, _, ms = ts.partition(",")
    parts = hms.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec + (float(ms) / 1000.0 if ms else 0.0)


def _parse_srt(srt_path):
    """解析 .srt → [{start_sec,end_sec,text}]。"""
    with open(srt_path, "r", encoding="utf-8") as f:
        raw = f.read()
    out = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # 第一行可能是序号；找含 --> 的行
        ts_line = None
        ti = 0
        for i, ln in enumerate(lines):
            if "-->" in ln:
                ts_line = ln
                ti = i
                break
        if not ts_line:
            continue
        a, _, b = ts_line.partition("-->")
        text = " ".join(lines[ti + 1:]).strip()
        if not text:
            continue
        out.append({"start_sec": _srt_ts_to_sec(a), "end_sec": _srt_ts_to_sec(b), "text": text})
    return out


def _probe_duration(video_path, *, required=False):
    """Return media duration in seconds, failing closed when required."""
    try:
        import media_tools
        ffprobe = media_tools.resolve_ffmpeg_pair(allow_fetch=False)[1]
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        if required:
            raise ValueError("BASECUT_DURATION_PROBE_REQUIRED")
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        if duration <= 0:
            raise ValueError
        return duration
    except (OSError, subprocess.CalledProcessError, ValueError):
        if required:
            raise ValueError("BASECUT_DURATION_PROBE_REQUIRED")
        return None


def _validate_timeline(shots, basecut_duration=None):
    """Validate source ranges before converting seconds to frames.

    Checks:
      1. Each shot has valid numeric start_sec < end_sec.
      2. No shot exceeds basecut duration.
      3. EDL continuity: adjacent shots must not overlap; gaps > 0.5s
         trigger a warning (not an error, to allow intentional black frames).
    """
    prev_end = None
    for i, shot in enumerate(shots):
        try:
            start = float(shot.get("start_sec"))
            end = float(shot.get("end_sec"))
        except (TypeError, ValueError):
            raise ValueError("INVALID_SHOT_RANGE: shot %d requires numeric start_sec/end_sec" % (i + 1))
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("INVALID_SHOT_RANGE: shot %d must satisfy 0 <= start_sec < end_sec" % (i + 1))
        if basecut_duration is not None and end > basecut_duration + 0.001:
            raise ValueError(
                "SHOT_EXCEEDS_BASECUT: shot %d ends at %.3fs, basecut is %.3fs" %
                (i + 1, end, basecut_duration))
        # EDL continuity check
        if prev_end is not None:
            if start < prev_end - 0.001:
                raise ValueError(
                    "EDL_OVERLAP: shot %d starts at %.3fs but shot %d ends at %.3fs" %
                    (i + 1, start, i, prev_end))
            gap = start - prev_end
            if gap > 0.5:
                print("[edl-check] WARNING: shot %d 与 shot %d 之间有 %.3fs 空隙"
                      % (i, i + 1, gap), flush=True)
        prev_end = end


def _normalize_brand_primary(value):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("BRAND_PRIMARY_REQUIRED: final_edit 需要 scheme.brandPrimary（例如 #E60012）")
    hex_value = raw[1:] if raw.startswith("#") else raw
    if re.fullmatch(r"[0-9a-fA-F]{3}", hex_value):
        return "#" + "".join(ch * 2 for ch in hex_value).upper()
    if re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
        return "#" + hex_value.upper()
    raise ValueError("BRAND_PRIMARY_INVALID: 请提供 hex 颜色，例如 #E60012")


def compile_shotlist(scheme, basecut_path, *, require_basecut_duration=False):
    """remotion_scheme.json → Remotion shotlist（底片作背景视频 + 运镜 + 动效字幕）。

    返回 shotlist dict（与 remotion_engine Shots composition / types.ts 对齐）。
    """
    fps = scheme.get("fps", 30)
    width = scheme.get("width", 1080)
    height = scheme.get("height", 1920)
    shots_in = scheme.get("shots") or []
    if not shots_in:
        raise ValueError("scheme 无 shots，无法编译 shotlist")

    basecut_abs = os.path.abspath(basecut_path)
    if not os.path.exists(basecut_abs):
        raise FileNotFoundError("底片不存在: %s" % basecut_abs)

    brand_primary = _normalize_brand_primary(scheme.get("brandPrimary") or scheme.get("brand_primary"))
    basecut_duration = _probe_duration(basecut_abs, required=require_basecut_duration)
    _validate_timeline(shots_in, basecut_duration)

    out_shots = []
    for i, sh in enumerate(shots_in):
        start = float(sh["start_sec"])
        end = float(sh["end_sec"])
        source_start_frame = int(round(start * fps))
        source_end_frame = int(round(end * fps))
        dur_frames = source_end_frame - source_start_frame
        if dur_frames < 1:
            raise ValueError("INVALID_SHOT_RANGE: shot %d is shorter than one frame" % (i + 1))
        move = _map_move(sh.get("camera_move"))
        # 差异化动效：从三分离字段编译 MotionOverlay props（含 style/position/内容）
        overlay = _build_motion_overlay(sh)
        # 转场取「上一镜的 transition_to_next」放到当前镜头入场
        prev_trans = "cut"
        if i > 0:
            prev_trans = _map_transition(shots_in[i - 1].get("transition_to_next"))
        shot = {
            "durationInFrames": dur_frames,
            "duration": round(dur_frames / float(fps), 6),
            "timeline_fps": fps,
            "sourceStartFrame": source_start_frame,
            "move": move,
            "transition": prev_trans,
            "video": basecut_abs,   # 底片作背景视频层
            "humanSlot": "none",
        }
        if overlay:
            shot["motionOverlay"] = overlay
            # 向后兼容：仍填 title/bullets（旧 ContentPage 路径 / 无 MotionOverlay 组件时兜底）
            if overlay.get("title"):
                shot["title"] = overlay["title"]
            if overlay.get("bullets"):
                shot["bullets"] = overlay["bullets"]
        out_shots.append(shot)

    # 字幕轨（配音逐句时间轴）：全局时间码 → 绝对帧，作 shotlist 顶层轨，
    # 覆盖整条底片（不随镜头切换重置），由 Remotion SubtitleTrack 渲染。
    subs = _load_subtitles(scheme)
    subtitle_track = []
    for s in subs:
        st = s.get("start_sec")
        en = s.get("end_sec")
        if st is None:
            continue
        from_frame = max(0, int(round(float(st) * fps)))
        to_frame = int(round(float(en) * fps)) if en is not None else from_frame + int(2 * fps)
        if to_frame <= from_frame:
            to_frame = from_frame + int(1.5 * fps)
        subtitle_track.append({
            "fromFrame": from_frame,
            "durationInFrames": max(1, to_frame - from_frame),
            "text": s["text"],
        })

    result = {
        "width": width, "height": height, "fps": fps,
        "brandPrimary": brand_primary,
        "designTokens": design_token_contract.resolve(scheme),
        "_basecut": basecut_abs,
        "_target_model": scheme.get("target_model"),
        "_prohibitions": scheme.get("prohibitions", []),
        "shots": out_shots,
    }
    if subtitle_track:
        result["subtitles"] = subtitle_track
    if isinstance(scheme.get("disclosure"), dict) and scheme["disclosure"].get("text"):
        result["disclosure"] = {
            "text": str(scheme["disclosure"]["text"]),
            "durationInFrames": int(scheme["disclosure"].get("durationInFrames") or fps),
        }
    return result


def reject_duplicate_subtitles(scheme):
    """A formally captioned basecut must not receive scheme subtitles again."""
    if _load_subtitles(scheme):
        raise ValueError("DUPLICATE_SUBTITLES: captioned basecut already contains subtitles")


def _stage_media_to_public(shotlist):
    """把 shot.video/image 的本地绝对路径拷进 remotion_engine/public/ 并改写为相对文件名。

    Remotion 对 <Video> 的 file:// 有安全拦截（MEDIA_ELEMENT_ERROR code 4），
    必须通过 public/ + staticFile 加载本地视频。图片同理处理，保持一致。
    就地修改并返回 shotlist。"""
    import remotion_engine
    return remotion_engine._stage_local_media(shotlist)


def render(shotlist_path, out_path, quality="high"):
    """调 remotion_engine 渲染最终成片（Shots composition，底片叠动效）。

    渲染前把本地媒体拷进 public/ 并改写 shotlist 为相对路径（绕过 file:// 拦截）。"""
    import remotion_engine
    out_abs = os.path.abspath(out_path)
    profile = "direct_delivery" if quality == "high" else "preview"
    remotion_engine.render(
        shotlist_path, out_abs, quality=quality,
        composition=remotion_engine.COMPOSITION, profile=profile)
    return out_abs


def formal_final_gate(manifest, caption_artifact, *, client, basecut_path):
    """Fail closed on run identity, approved inputs, take OCR, and caption render."""
    rm.identity_gate(manifest, client=client)
    if not rm.approval_is_current(manifest, "video"):
        raise ValueError("FINAL_VIDEO_APPROVAL_REQUIRED")
    if manifest.get("requires_shotcraft_packaging"):
        if not rm.approval_is_current(manifest, "shotcraft_packaging"):
            raise ValueError("FINAL_SHOTCRAFT_PACKAGING_APPROVAL_REQUIRED")
        packaging = (manifest.get("generation") or {}).get("shotcraft_packaging") or {}
        outputs = packaging.get("artifacts") or []
        packaged = next((item for item in outputs if str(item.get("path", "")).lower().endswith((".mp4", ".mov", ".webm"))), None)
        if not packaged or not rm.file_record_is_current(packaged):
            raise ValueError("FINAL_SHOTCRAFT_PACKAGING_STALE")
    script_splitter.caption_artifact_is_current(
        manifest, caption_artifact, client=client, require_approved=True)
    caption_render = manifest.get("caption_render") or {}
    if caption_render.get("caption_identity") != caption_artifact.get("caption_identity"):
        raise ValueError("FINAL_CAPTION_RENDER_IDENTITY_MISMATCH")
    if caption_render.get("status") != "pending_final_approval":
        raise ValueError("FINAL_CAPTION_RENDER_REQUIRED")
    render_output = caption_render.get("output") or {}
    if os.path.abspath(basecut_path) != render_output.get("path"):
        raise ValueError("FINAL_BASECUT_IDENTITY_MISMATCH")
    if not rm.file_record_is_current(render_output):
        raise ValueError("STALE_FINAL_BASECUT")
    if manifest.get("requires_shotcraft_packaging") and render_output.get("sha256") == packaged.get("sha256"):
        # Caption rendering should compose onto the packaging result, never
        # silently bypass it by treating the unchanged source as captioned.
        raise ValueError("FINAL_SHOTCRAFT_CAPTION_COMPOSITE_REQUIRED")

    with open(caption_artifact["files"]["segments"]["path"], encoding="utf-8") as handle:
        segments_spec = json.load(handle)
    for segment in segments_spec.get("segments") or []:
        sid = str(segment.get("id"))
        accepted = (manifest.get("accepted_takes") or {}).get(sid) or {}
        take_fp = accepted.get("take_fingerprint")
        if not take_fp:
            raise ValueError("FINAL_ACCEPTED_TAKE_REQUIRED: %s" % sid)
        if accepted.get("video_handoff_fingerprint") != segment.get("video_handoff_fingerprint"):
            raise ValueError("FINAL_TAKE_HANDOFF_MISMATCH: %s" % sid)
        if not rm.ocr_take_is_clear_or_waived(manifest, sid, take_fp):
            raise ValueError("FINAL_OCR_CLEAN_OR_EXACT_WAIVER_REQUIRED: %s" % sid)
    return True


def record_final_generation(manifest, manifest_path, *, scheme_path, basecut_path,
                             caption_artifact, out_path, media_qc_report=None,
                             disclosure=None, render_quality="high", render_spec_path=None):
    """Persist exact final inputs and leave final output pending approval."""
    if render_quality != "high":
        raise ValueError("FORMAL_FINAL_HIGH_QUALITY_REQUIRED")
    transaction = (manifest.get("generation") or {}).get("final") or {}
    scheme_record = rm.file_record(scheme_path)
    basecut_record = rm.file_record(basecut_path)
    if (transaction.get("status") != "running" or
            transaction.get("render_quality") != "high" or
            transaction.get("planned_output") != os.path.abspath(out_path) or
            transaction.get("planned_qc") != os.path.abspath(out_path) + ".qc.json" or
            transaction.get("caption_identity") != caption_artifact.get("caption_identity") or
            transaction.get("scheme") != scheme_record or
            transaction.get("captioned_basecut") != basecut_record or
            not rm.file_record_is_current(transaction.get("scheme")) or
            not rm.file_record_is_current(transaction.get("captioned_basecut"))):
        raise ValueError("FINAL_RUNNING_TRANSACTION_REQUIRED")
    inputs = {
        "scheme": scheme_record,
        "captioned_basecut": basecut_record,
        "caption_identity": caption_artifact.get("caption_identity"),
        "timeline_basecut_sha256": caption_artifact["files"]["basecut"]["sha256"],
    }
    if render_spec_path:
        render_spec_record = rm.file_record(render_spec_path)
        if not render_spec_record or not rm.file_record_is_current(render_spec_record):
            raise ValueError("FINAL_RENDER_SPEC_STALE")
        inputs["render_quality_spec"] = render_spec_record
    if not inputs["scheme"].get("exists") or not inputs["captioned_basecut"].get("exists"):
        raise ValueError("FINAL_INPUT_MISSING")
    if media_qc_report is None:
        report_path = os.path.abspath(out_path) + ".qc.json"
        media_qc_report = media_qc.check(
            out_path, profile="formal", audio_required=True, report_path=report_path)
    media_qc.require_pass(media_qc_report)
    report_path = os.path.abspath(out_path) + ".qc.json"
    try:
        with open(report_path, encoding="utf-8") as handle:
            persisted_qc = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("FINAL_MEDIA_QC_IDENTITY_MISMATCH") from exc
    output_record = rm.file_record(out_path)
    qc_file = media_qc_report.get("file") or {}
    if (media_qc_report.get("profile") != "formal" or
            not (media_qc_report.get("expectations") or {}).get("audio_required") or
            persisted_qc != media_qc_report or
            qc_file.get("path") != output_record.get("path") or
            qc_file.get("sha256") != output_record.get("sha256")):
        raise ValueError("FINAL_MEDIA_QC_IDENTITY_MISMATCH")
    started_at = transaction.get("started_at")
    artifacts = [out_path, report_path]
    if render_spec_path:
        artifacts.append(render_spec_path)
    rm.mark_generation_finished(manifest, "final", artifacts)
    manifest["generation"]["final"]["started_at"] = started_at
    manifest["generation"]["final"]["media_qc"] = media_qc_report
    manifest["generation"]["final"]["actual_duration"] = (
        media_qc_report.get("media") or {}).get("actual_duration")
    manifest["generation"]["final"]["render_quality"] = render_quality
    manifest["generation"]["final"]["planned_output"] = os.path.abspath(out_path)
    manifest["generation"]["final"]["planned_qc"] = report_path
    manifest["generation"]["final"]["scheme"] = transaction["scheme"]
    manifest["generation"]["final"]["captioned_basecut"] = transaction["captioned_basecut"]
    manifest["generation"]["final"]["caption_identity"] = transaction["caption_identity"]
    if render_spec_path:
        manifest["generation"]["final"]["render_quality_spec"] = inputs["render_quality_spec"]
    manifest["delivery_qc"] = media_qc_report
    manifest["final_inputs"] = inputs
    if disclosure is not None:
        manifest["disclosure"] = disclosure
    rm.save_manifest(manifest, manifest_path)
    return manifest["generation"]["final"]


def begin_final_generation(manifest, manifest_path, *, caption_artifact, client,
                           scheme_path, basecut_path, out_path,
                           render_quality="high"):
    """Persist an observable formal final transaction before Remotion starts."""
    if render_quality != "high":
        raise ValueError("FORMAL_FINAL_HIGH_QUALITY_REQUIRED")
    formal_final_gate(
        manifest, caption_artifact, client=client, basecut_path=basecut_path)
    scheme = rm.file_record(scheme_path)
    basecut = rm.file_record(basecut_path)
    if (not scheme or not scheme.get("exists") or
            not basecut or not basecut.get("exists")):
        raise ValueError("FINAL_INPUT_MISSING")
    planned = rm.require_run_output(manifest, out_path, label="final_output")
    planned_qc = rm.require_run_output(
        manifest, planned + ".qc.json", label="final_qc")
    rm.mark_generation_started(manifest, "final")
    manifest["generation"]["final"].update({
        "scheme": scheme,
        "captioned_basecut": basecut,
        "caption_identity": caption_artifact.get("caption_identity"),
        "planned_output": planned,
        "planned_qc": planned_qc,
        "render_quality": render_quality,
        "transaction_started_at": datetime.now().isoformat(timespec="seconds"),
    })
    rm.save_manifest(manifest, manifest_path)
    return manifest["generation"]["final"]


def _main(argv=None):
    ap = argparse.ArgumentParser(description="阶段7 · 本地 Remotion 剪辑（方案命令+动效素材→成片）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    cp = sub.add_parser("compile", help="方案命令 → Remotion shotlist")
    cp.add_argument("--scheme", required=True)
    cp.add_argument("--basecut", required=True)
    cp.add_argument("--brand", help="客户确认主色 hex；若 scheme 已有 brandPrimary 可省略")
    cp.add_argument("--out-shotlist", required=True)

    rd = sub.add_parser("render", help="shotlist → 最终成片 mp4")
    rd.add_argument("--shotlist", required=True)
    rd.add_argument("--out", required=True)
    rd.add_argument("--quality", default="high", choices=["draft", "standard", "high"])

    rn = sub.add_parser("run", help="一步到位：方案命令+底片 → 最终成片")
    rn.add_argument("--scheme", required=True)
    rn.add_argument("--basecut", required=True)
    rn.add_argument("--brand", help="客户确认主色 hex；若 scheme 已有 brandPrimary 可省略")
    rn.add_argument("--out", required=True)
    rn.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    rn.add_argument("--client")
    rn.add_argument("--manifest", help="正式流程 run_manifest.json")
    rn.add_argument("--caption-manifest", help="已确认 caption timeline artifact")
    rn.add_argument("--no-disclosure", action="store_true", help="客户已确认不添加 AI 生成内容披露")
    rn.add_argument("--disclosure-lang", default="zh-CN")
    rn.add_argument("--draft", action="store_true", help="草稿兼容：允许旧的 scheme+basecut 调用")

    a = ap.parse_args(argv)
    if a.cmd == "compile":
        with open(a.scheme, "r", encoding="utf-8") as f:
            scheme = json.load(f)
        if a.brand:
            scheme["brandPrimary"] = a.brand
        sl = compile_shotlist(scheme, a.basecut, require_basecut_duration=True)
        with open(a.out_shotlist, "w", encoding="utf-8") as f:
            json.dump(sl, f, ensure_ascii=False, indent=2)
        print("已编译 shotlist: %s（%d 镜）" % (a.out_shotlist, len(sl["shots"])))
    elif a.cmd == "render":
        render(a.shotlist, a.out, quality=a.quality)
    elif a.cmd == "run":
        if not a.draft and not (a.client and a.manifest and a.caption_manifest):
            ap.error("run 正式流程必须提供 --client/--manifest/--caption-manifest；旧方式需 --draft")
        if not a.draft and a.quality != "high":
            ap.error("正式 final 只接受 --quality high；draft/standard 仅限显式 --draft 内部预览")
        manifest = caption_artifact = None
        if not a.draft:
            with open(a.manifest, encoding="utf-8") as f:
                manifest = json.load(f)
            with open(a.caption_manifest, encoding="utf-8") as f:
                caption_artifact = json.load(f)
            begin_final_generation(
                manifest, a.manifest, caption_artifact=caption_artifact,
                client=a.client, scheme_path=a.scheme, basecut_path=a.basecut,
                out_path=a.out, render_quality=a.quality)
        with open(a.scheme, "r", encoding="utf-8") as f:
            scheme = json.load(f)
        if a.brand:
            scheme["brandPrimary"] = a.brand
        if not a.draft:
            reject_duplicate_subtitles(scheme)
            if not a.no_disclosure:
                import ai_disclosure
                scheme["disclosure"] = {
                    "text": ai_disclosure.DISCLOSURE_TEXT.get(
                        a.disclosure_lang, ai_disclosure.DISCLOSURE_TEXT["en"]),
                    "durationInFrames": int(scheme.get("fps") or 30),
                }
        sl = compile_shotlist(
            scheme, a.basecut, require_basecut_duration=not a.draft)
        import tempfile
        fd, tmp_sl = tempfile.mkstemp(prefix=".final_shotlist-", suffix=".json",
                                      dir=os.path.dirname(os.path.abspath(a.out)) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(sl, f, ensure_ascii=False, indent=2)
            print("已编译 shotlist: %s（%d 镜），开始渲染..." % (tmp_sl, len(sl["shots"])))
            render_spec_path = None
            if a.draft:
                render(tmp_sl, a.out, quality=a.quality)
            else:
                import render_spec_compiler
                from project_utils import atomic_write_json
                tokens = design_token_contract.resolve(scheme, required=True)
                quality_spec = render_spec_compiler.compile_spec(
                    sl, tokens, engine_props=sl, composition="Shots",
                    render_profile="direct_delivery", formal=True)
                render_spec_path = os.path.abspath(a.out) + ".render-spec.json"
                rm.require_run_output(
                    manifest, render_spec_path, label="final_render_quality_spec")
                atomic_write_json(render_spec_path, quality_spec)
                manifest["generation"]["final"]["render_quality_spec"] = rm.file_record(
                    render_spec_path)
                rm.save_manifest(manifest, a.manifest)
                import remotion_engine
                remotion_engine.render_quality_spec(render_spec_path, a.out)
        finally:
            if os.path.exists(tmp_sl):
                os.remove(tmp_sl)
        if not a.draft:
            if not a.no_disclosure:
                disclosure = {
                    "applied": True, "style": "same_timeline_corner_badge",
                    "duration_ms": 1000, "lang": a.disclosure_lang,
                    "text": scheme["disclosure"]["text"],
                    "out_path": os.path.abspath(a.out),
                    "single_lossy_encode": True,
                }
            else:
                disclosure = {"applied": False, "style": "none", "opt_out": True}
            record_final_generation(manifest, a.manifest, scheme_path=a.scheme,
                                     basecut_path=a.basecut,
                                     caption_artifact=caption_artifact, out_path=a.out,
                                     disclosure=disclosure,
                                     render_quality=a.quality,
                                     render_spec_path=render_spec_path)
            print(json.dumps({"ok": True, "out": os.path.abspath(a.out),
                              "status": "pending_approval"}, ensure_ascii=False))


def _formal_run_manifest_arg(argv):
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens or tokens[0] != "run" or "--draft" in tokens:
        return None
    try:
        return os.path.abspath(tokens[tokens.index("--manifest") + 1])
    except (ValueError, IndexError):
        return None


def main(argv=None):
    """CLI boundary that never leaves a failed formal render as ``running``."""
    manifest_path = _formal_run_manifest_arg(argv)
    try:
        return _main(argv)
    except (OSError, ValueError, RuntimeError, TypeError,
            subprocess.SubprocessError) as exc:
        if manifest_path and os.path.isfile(manifest_path):
            try:
                rm.record_generation_failure_if_running(
                    manifest_path, "final", exc,
                    error_code="FINAL_RENDER_FAILED", retryable=True)
            except (OSError, ValueError, RuntimeError, TypeError) as closeout_exc:
                if hasattr(exc, "add_note"):
                    exc.add_note("generation closeout failed: %s" % closeout_exc)
        raise


if __name__ == "__main__":
    main()
