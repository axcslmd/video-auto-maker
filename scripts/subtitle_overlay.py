#!/usr/bin/env python3
"""字幕叠加 + 位置智能推荐 — 视觉模型分析安全区 → HyperFrames alpha 字幕层 → ffmpeg overlay 叠入成片。

解决两件事，合成一个总方案：
  A. 字幕位置由视觉模型（online 图像多模态，偏好 qwen3.6-plus）分析成片帧后推荐**精确像素**安全区，
     绕过 hf_engine 的 upper/center/lower 三档粗定位。
  B. 字幕用 HyperFrames **ProRes 4444 alpha**（透明背景）真正叠入成片，而非烧死或绿底抠像。

四个子命令（也可 run 一步到位）：
  analyze  抽帧 → 视觉模型推荐 safe_zone JSON（bottom_px/left_px/right_px/max_height_px/font_size_*）
  build-scenes  把 safe_zone + 逐句台词 → HyperFrames 场景 JSON（transparent 背景 + 精确定位）
  compose  HyperFrames 渲 alpha mov → ffmpeg overlay 叠回成片 → 验证帧信息量
  run      analyze → build-scenes → compose 全链路

CLI:
  python3 subtitle_overlay.py analyze --video output/video/final.mp4 --out output/safe_zone.json
  python3 subtitle_overlay.py build-scenes --lines output/lines.json --safe-zone output/safe_zone.json \
      --out output/subtitle_scenes_v2.json
  # build-scenes 产出的 scene JSON 先用 hf_engine 渲成 alpha 层，再喂给 compose：
  python3 hf_engine.py render --spec output/subtitle_scenes_v2.json \
      --out output/video/subtitles_alpha.mov --format mov
  # 低层 compose 仅供诊断，不登记正式 caption render：
  python3 subtitle_overlay.py compose --video output/video/final.mp4 \
      --alpha output/video/subtitles_alpha.mov --out output/video/final_subtitled.mp4
  python3 subtitle_overlay.py run --video output/<client>/<run-id>/basecut.mp4 \
      --lines output/<client>/<run-id>/lines.json --client <client> \
      --manifest output/<client>/<run-id>/run_manifest.json \
      --caption-manifest output/<client>/<run-id>/caption_manifest.json \
      --out output/<client>/<run-id>/captioned_basecut.mp4
"""
import os
import sys
import json
import shutil
import argparse
import subprocess
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import br_client  # noqa: E402
import run_manifest as rm  # noqa: E402
import script_splitter  # noqa: E402
import text_provider  # noqa: E402

# 视觉模型分析成片帧、推荐字幕安全区的提示词。
ANALYZE_PROMPT = (
    "你是一个专业的视频字幕与动效排版顾问。这是一段 {W}x{H} 视频的截帧。"
    "请分析画面中人物脸部、主体、背景构图、信息密集区域，给出两套安全区："
    "一套给主字幕，一套给 motion elements / 卡片 / 标签。字幕和动效都不能压住人脸。"
    "只输出一个 JSON 代码块，格式如下（像素单位，基于 {W}x{H} 画布）：\n"
    "```json\n"
    "{{\n"
    '  "subtitle_safe_zone": {{\n'
    '    "bottom_px": <字幕底边距画面底部的像素>,\n'
    '    "left_px": <左边距>,\n'
    '    "right_px": <右边距>,\n'
    '    "max_height_px": <字幕区域最大高度>\n'
    "  }},\n"
    '  "motion_safe_zone": {{\n'
    '    "top_px": <动效顶部距画面顶部的像素，可选>,\n'
    '    "bottom_px": <动效底边距画面底部的像素，可选>,\n'
    '    "left_px": <左边距，可选>,\n'
    '    "right_px": <右边距，可选>,\n'
    '    "width_px": <动效推荐宽度，可选>\n'
    "  }},\n"
    '  "font_size_main": <主字幕推荐字号 px>,\n'
    '  "font_size_sub": <副字幕/英文推荐字号 px>,\n'
    '  "reasoning": "<简短说明：为什么选这个位置，避开了什么>"\n'
    "}}\n"
    "```\n"
    "要求：主字幕不得压住人物面部与主体，优先放在画面下方约 15%-22% 处；动效标签尽量避开人脸，"
    "优先放在对角空区或信息较少的一侧；"
    "严格按上面给的字段名和结构输出（bottom_px/left_px/right_px/max_height_px 必须是像素整数），"
    "不要新增/改名字段，不要用百分比 margin，不要用坐标框(x_min/x_max/y_min/y_max)或其它自定义结构；"
    "只输出 JSON 代码块，不要额外解释。"
)

# 首次输出没有严格匹配 schema 时，用同一个模型基于自己刚才的分析做一次纠偏转换。
# 实测 qwen3.6-plus/kimi-k2.6/glm-5v-turbo 都会给出语义正确但结构不同的 JSON
# （百分比 margin、坐标框 x_min/x_max/y_min/y_max、嵌套 safe_zones.action_safe.pixels 等），
# 逐一手写解析器覆盖不完，且新模型/新措辞会不断产生新形状——用模型对自己输出做结构转换更稳健。
REFORMAT_PROMPT = (
    "你刚才的分析如下：\n{prior}\n\n"
    "请把上面的结论换算成基于 {W}x{H} 像素画布的以下精确 JSON 格式（不要重新分析，只做单位/结构换算）：\n"
    "```json\n"
    "{{\n"
    '  "safe_zone": {{"bottom_px": <int>, "left_px": <int>, "right_px": <int>, "max_height_px": <int>}},\n'
    '  "subtitle_safe_zone": {{"bottom_px": <int>, "left_px": <int>, "right_px": <int>, "max_height_px": <int>}},\n'
    '  "motion_safe_zone": {{"top_px": <int>, "bottom_px": <int>, "left_px": <int>, "right_px": <int>, "width_px": <int>}},\n'
    '  "font_size_main": <int>, "font_size_sub": <int>, "reasoning": "<简短说明>"\n'
    "}}\n"
    "```\n"
    "字段定义（务必按此换算，不要把坐标框的 x_min/x_max 直接当成 left_px/right_px）：\n"
    "- safe_zone 与 subtitle_safe_zone 必须一致，都是主字幕安全区\n"
    "- motion_safe_zone 是给后期动效/标签/卡片用的布局区，尽量避开人脸和主体\n"
    "- bottom_px = 字幕底边距**画面底部**的像素距离（不是字幕的 y 坐标）\n"
    "- left_px / right_px = 字幕距**左右边缘**的内缩像素（不是 x_min/x_max 坐标值）\n"
    "- max_height_px = 字幕安全区允许的最大高度（像素），不是画面总高度\n"
    "举例：若画面宽 1080，原分析给的可用宽度坐标是 x_min=108,x_max=972，"
    "则 left_px=108（x_min 本身），right_px=1080-972=108（画面宽度减 x_max），不是把 972 直接填进 right_px。\n"
    "只输出这一个 JSON 代码块，不要用百分比或坐标框，不要额外解释。"
)


def _list_vision_models():
    """Compatibility wrapper around the shared gateway capability owner."""
    return br_client.list_vision_models()


def _pick_vision_model():
    """Compatibility wrapper around the shared gateway capability owner."""
    return br_client.pick_vision_model()


def _extract_json_block(text):
    """从 LLM 回复里抠出第一个 ```json``` 代码块并解析；无代码块则退回裸 {...}；都失败返回 None。"""
    import re
    if not text:
        return None
    m = re.search(r"```json\s*(.+?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if not raw:
        m2 = re.search(r"\{.*\}", text, re.DOTALL)
        raw = m2.group(0) if m2 else None
    if not raw:
        return None
    try:
        return text_provider.extract_json(raw)
    except ValueError:
        return None


def _default_safe_zone(W, H):
    """视觉模型不可用时的保守安全区兜底。"""
    subtitle_zone = {
        "bottom_px": int(H * 0.14), "left_px": 40, "right_px": 40,
        "max_height_px": int(H * 0.22),
    }
    motion_zone = {
        "top_px": 80, "left_px": 40, "right_px": 40, "width_px": min(520, int(W * 0.42)),
    }
    return {
        "safe_zone": dict(subtitle_zone),
        "subtitle_safe_zone": subtitle_zone,
        "motion_safe_zone": motion_zone,
        "font_size_main": int(W * 0.075), "font_size_sub": int(W * 0.05),
        "reasoning": "视觉模型不可用，主字幕放底部安全区，动效放顶部保守空区。",
        "_fallback": True,
    }


def analyze(video_path, frames=4, width=1080, height=1920, verbose=True):
    """抽帧 → 视觉模型推荐字幕安全区 JSON。返回 dict（含 safe_zone / font_size_* / model）。"""
    import key_setup
    import ocr_check

    def log(m):
        if verbose:
            print(m, flush=True)

    if not os.path.exists(video_path):
        raise FileNotFoundError("视频不存在: %s" % video_path)
    api_key = key_setup.load_key()
    if not api_key:
        raise br_client.BRError("No API key. 先跑密钥准入闸门 key_setup.py gate。")

    frame_paths, tmpdir = ocr_check.extract_frames(video_path, n=frames)
    if not frame_paths:
        raise RuntimeError("抽帧失败，无法分析字幕安全区")
    log("[analyze] 已抽 %d 帧代表画面" % len(frame_paths))
    model = _pick_vision_model()
    log("[analyze] 视觉模型: %s" % model)

    ctx = ANALYZE_PROMPT.format(W=width, H=height)
    content = [{"type": "input_text", "text": ctx}]
    hosted = 0
    for i, fp in enumerate(frame_paths):
        try:
            ref = br_client.to_image_ref(fp, api_key=api_key, prefer_hosted=True)
        except (br_client.BRError, OSError, ValueError, RuntimeError) as he:
            log("[analyze] 帧上传失败跳过: %s" % he)
            continue
        content.append({"type": "input_text", "text": "代表帧 %d：" % (i + 1)})
        content.append({"type": "input_image", "image_url": ref})
        hosted += 1
    content.append({"type": "input_text",
                    "text": "综合以上帧给出**一个**统一的字幕安全区 JSON，只输出 json 代码块。"})
    _cleanup(tmpdir)
    if hosted == 0:
        log("[analyze] 所有帧上传失败，返回兜底安全区")
        r = _default_safe_zone(width, height)
        r["model"] = model
        return r

    sysmsg = {"role": "system",
              "content": "你是专业视频字幕排版顾问，只输出一个 json 代码块的安全区建议，不要多余解释。"}
    msgs = [sysmsg, {"role": "user", "content": content}]
    try:
        resp = br_client.chat(api_key, msgs, model=model, timeout=180)
    except (br_client.BRError, OSError, ValueError, RuntimeError) as e:
        log("[analyze] 视觉分析失败，返回兜底: %s" % e)
        r = _default_safe_zone(width, height)
        r["model"] = model
        return r
    parsed = _extract_json_block(resp)
    if not parsed or "safe_zone" not in parsed:
        # 实测视觉模型（qwen3.6-plus/kimi-k2.6/glm-5v-turbo 等）经常给出语义正确
        # 但结构不同的 JSON（百分比 margin、坐标框、嵌套字段等），不是"分析失败"，
        # 只是没按 schema 输出。先让同一个模型把自己的结论转换成精确 schema 再判定，
        # 不要一次没对上格式就直接判失败丢弃真实分析结果。
        log("[analyze] 首次输出未严格匹配 schema，尝试纠偏转换")
        reformatted = _reformat_to_schema(api_key, model, resp, width, height, log)
        if reformatted:
            reformatted["model"] = model
            reformatted["_reformatted_from"] = (resp or "")[:400]
            return reformatted
        log("[analyze] 纠偏转换仍未解析出安全区 JSON，返回兜底")
        r = _default_safe_zone(width, height)
        r["model"] = model
        r["_raw"] = (resp or "")[:400]
        return r
    # 首次直出就命中 schema 也做一次数值夹紧兜底：模型偶尔会把坐标框数值
    # 直接填进 margin 字段（如 left_px=960 几乎吃满整个宽度），结构对但数值不可用。
    parsed = _clamp_safe_zone(parsed, width, height)
    parsed["model"] = model
    return parsed


def _clamp_safe_zone(parsed, width, height):
    """把 safe_zone 数值夹回合理边界，防御模型产出不可用区域。

    不改变字段结构，只做数值兜底：
      - 四个字段转 int，非法/负数按 0 处理；
      - left_px+right_px 不得超过画面宽度的 80%（否则等比缩到 80% 以内，保留比例）；
      - bottom_px+max_height_px 不得超过画面高度（否则按比例缩小 max_height_px）；
      - max_height_px 至少给 10% 画面高度，避免夹成 0 高度不可用区域。
    """
    parsed = dict(parsed or {})
    subtitle = dict(parsed.get("subtitle_safe_zone") or parsed.get("safe_zone") or {})
    motion = dict(parsed.get("motion_safe_zone") or {})

    def _int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    bottom = max(0, _int(subtitle.get("bottom_px"), int(height * 0.14)))
    left = max(0, _int(subtitle.get("left_px"), 40))
    right = max(0, _int(subtitle.get("right_px"), 40))
    max_h = max(0, _int(subtitle.get("max_height_px"), int(height * 0.22)))

    max_lr = int(width * 0.8)
    if left + right > max_lr > 0 and (left + right) > 0:
        scale = max_lr / float(left + right)
        left = int(left * scale)
        right = int(right * scale)

    min_h = int(height * 0.10)
    # bottom 本身先夹到留出至少 min_h 的空间，避免"bottom 就已经吃掉几乎整个画面"
    # 这种极端输入让后面 max_h 的下限兜底反而把 bottom+max_h 顶回超出画布。
    bottom = min(bottom, max(0, height - min_h))
    if bottom + max_h > height:
        max_h = max(0, height - bottom)
    if max_h < min_h:
        max_h = min(min_h, max(0, height - bottom))

    subtitle["bottom_px"], subtitle["left_px"], subtitle["right_px"], subtitle["max_height_px"] = (
        bottom, left, right, max_h)
    motion_bottom = max(0, _int(motion.get("bottom_px"), 0))
    motion_top = max(0, _int(motion.get("top_px"), 80))
    motion_left = max(0, _int(motion.get("left_px"), 40))
    motion_right = max(0, _int(motion.get("right_px"), 40))
    motion_width = max(0, _int(motion.get("width_px"), min(520, int(width * 0.42))))
    motion["top_px"] = motion_top if motion_top else None
    if motion_bottom:
        motion["bottom_px"] = motion_bottom
    if motion_left:
        motion["left_px"] = motion_left
    if motion_right:
        motion["right_px"] = motion_right
    if motion_width:
        motion["width_px"] = motion_width
    out = dict(parsed)
    out["safe_zone"] = dict(subtitle)
    out["subtitle_safe_zone"] = subtitle
    out["motion_safe_zone"] = motion
    return out


def _reformat_to_schema(api_key, model, prior_resp, width, height, log):
    """把模型首次分析结果（结构不符 schema）喂回去做一次纯换算，不重新分析画面。

    返回解析成功的 dict，或 None（换算仍失败，调用方回退兜底）。
    """
    # 截断太短会把左右边距等关键字段截掉，模型只能看到部分信息去猜/编——
    # 实测截 1200 字符时出现过 left_px/right_px 被错填成坐标框的 x_min/x_max。
    # 放宽到 4000 字符，覆盖模型常见的带 reason 说明的冗长 JSON 结构。
    prompt = REFORMAT_PROMPT.format(prior=(prior_resp or "")[:4000], W=width, H=height)
    msgs = [
        {"role": "system", "content": "你只做 JSON 结构/单位换算，不重新分析画面，只输出一个 json 代码块。"},
        {"role": "user", "content": prompt},
    ]
    try:
        resp2 = br_client.chat(api_key, msgs, model=model, timeout=90)
    except (br_client.BRError, OSError, ValueError, RuntimeError) as e:
        log("[analyze] 纠偏转换请求失败: %s" % e)
        return None
    parsed2 = _extract_json_block(resp2)
    if not parsed2 or ("safe_zone" not in parsed2 and "subtitle_safe_zone" not in parsed2):
        return None
    # 纠偏转换本身也可能算错（如把坐标框直接当 margin），夹回合理边界再放行，
    # 避免"结构对了但数值不可用"（如 left_px=108,right_px=972 几乎吃满整个宽度）。
    return _clamp_safe_zone(parsed2, width, height)


def _cleanup(tmpdir):
    try:
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
    except OSError:
        pass


def build_scenes(lines, safe_zone_doc, width=1080, height=1920, fps=30,
                 preset="fade_up", brand_primary="#6C63FF"):
    """把逐句台词 + 视觉模型安全区 → HyperFrames 场景 JSON（transparent 背景 + 精确定位）。

    lines: [{text, start, end[, size, preset, sub]}...]；[[关键词]] 会高亮成 accent 色。
    safe_zone_doc: analyze() 的返回（含 safe_zone / font_size_main）。
    返回可直接喂 hf_engine.render(fmt='mov') 的 spec dict。
    """
    sz = (safe_zone_doc or {}).get("safe_zone", {})
    bottom_px = int(sz.get("bottom_px", int(height * 0.14)))
    left_px = int(sz.get("left_px", 40))
    right_px = int(sz.get("right_px", 40))
    max_h = int(sz.get("max_height_px", int(height * 0.22)))
    font_main = int(safe_zone_doc.get("font_size_main", int(width * 0.075))) if safe_zone_doc else int(width * 0.075)

    scenes = []
    for idx, ln in enumerate(lines):
        if "text" not in ln or "start" not in ln or "end" not in ln:
            raise ValueError("第 %d 句台词缺 text/start/end 字段: %r" % (idx + 1, ln))
        start, end = float(ln["start"]), float(ln["end"])
        if end <= start:
            raise ValueError("第 %d 句台词时间轴非法（end<=start）: start=%s end=%s"
                             % (idx + 1, start, end))
        sc = {
            "text": ln["text"],
            "start": start,
            "end": end,
            "preset": ln.get("preset", preset),
            "size": int(ln.get("size", font_main)),
            # 精确像素定位（绕过 pos 三档）
            "bottom_px": int(ln.get("bottom_px", bottom_px)),
            "left_px": int(ln.get("left_px", left_px)),
            "right_px": int(ln.get("right_px", right_px)),
            "max_height_px": int(ln.get("max_height_px", max_h)),
        }
        # 字幕设计器可以给出固定内容宽度和层级字号；保留这些字段，
        # 避免渲染器把所有横屏字幕拉成同一条通栏。
        if ln.get("width_px") is not None:
            sc["width_px"] = int(ln["width_px"])
        if ln.get("track_index") is not None:
            sc["track_index"] = int(ln["track_index"])
        if ln.get("variant"):
            sc["variant"] = str(ln["variant"])
        scenes.append(sc)

    duration = max((s["end"] for s in scenes), default=6)
    return {
        "resolution": [width, height], "fps": fps, "duration": duration,
        # transparent → 配合 hf_engine render(fmt='mov') 出 ProRes alpha 字幕层
        "background": {"type": "transparent"},
        "brand": {"primary": brand_primary},
        "scenes": scenes,
    }


def _ffprobe_frame_bytes(ffmpeg, video_path, t=None):
    """抽 t 秒一帧到临时 png，返回文件字节数（验证叠加后帧含真实画面信息量）。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="sub_verify_")
    out = os.path.join(tmp, "probe.png")
    args = [ffmpeg, "-hide_banner", "-y"]
    if t is not None:
        args += ["-ss", "%.3f" % t]
    args += ["-i", video_path, "-vframes", "1", out]
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    size = os.path.getsize(out) if os.path.exists(out) else 0
    shutil.rmtree(tmp, ignore_errors=True)
    return size


def _verify_threshold_kb(width, height, base_kb=200, base_px=1080 * 1920, floor_kb=40):
    """按分辨率等比缩放验证阈值（基准 1080×1920→200KB）。

    横屏/低分辨率成片中段帧 PNG 天然更小，写死 200KB 会误判 ok:false。
    按像素数等比缩放，并设 floor_kb 下限（极小画面也要有基本信息量）。
    """
    px = max(int(width) * int(height), 1)
    scaled = base_kb * (px / float(base_px))
    return max(floor_kb, round(scaled, 1))


def _probe_stream(ffprobe, path):
    """ffprobe 视频流的 (codec_name, pix_fmt)。探测失败返回 (None, None)。"""
    try:
        p = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt,codec_name",
             "-of", "default=nk=1:nw=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = p.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        lines = (output or "").strip().splitlines()
        codec = lines[0] if len(lines) > 0 else None
        pix_fmt = lines[1] if len(lines) > 1 else None
        return codec, pix_fmt
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None, None


def _probe_duration(ffprobe, path):
    try:
        p = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        duration = float(p.stdout.strip())
        return duration if p.returncode == 0 and duration > 0 else None
    except (OSError, TypeError, ValueError):
        return None


def _has_alpha_pix_fmt(pix_fmt):
    return bool(pix_fmt) and any(tag in pix_fmt for tag in
                                  ("yuva", "rgba", "argb", "abgr", "bgra", "gbrap"))


def compose(video_path, alpha_path, out_path, width=1080, height=1920,
            crf=16, verify_min_kb=None, verbose=True, require_alpha=True):
    """ffmpeg 把 alpha 字幕层 overlay 叠回成片。返回 {ok, out, verify_kb}。

    alpha_path: HyperFrames 渲出的 ProRes/VP9 alpha 视频（透明背景字幕层）。
    用 overlay=0:0:format=auto 合成，libx264 crf16 输出，保留原音轨。
    verify_min_kb: 验证阈值；None=按分辨率自动缩放（横屏/低分辨率不误判）。

    require_alpha=True（默认）：合成前先 ffprobe 校验 alpha_path 确实带 alpha 通道
    (pix_fmt 含 yuva/rgba/argb/abgr/bgra/gbrap)。**这是关键防呆**：如果传入的
    "alpha 层"其实是普通不透明视频（例如 hf_engine 渲染时被错误传成 --format mp4，
    没走透明通道），overlay 会把这个不透明帧整块盖在底片上——真实故障表现为
    "最终成片只剩字幕文字和原声音轨，实际底片画面完全看不到"（底片音轨仍被
    `-map 0:a?` 正确保留，所以声音正常，唯独画面被壓住）。命中即直接拒绝合成
    并返回 {ok:False, error:"NO_ALPHA_CHANNEL..."}，不产出看似成功实则遮盖画面的成片。
    """
    import ocr_check
    ff, fp = ocr_check._ffmpeg_bins()
    if not ff:
        raise RuntimeError(
            "ffmpeg 不可用。请单独安装 ffmpeg/ffprobe 并放入 PATH，"
            "或设置 BASICROUTER_FFMPEG_DIR")
    if require_alpha:
        codec, pix_fmt = _probe_stream(fp or shutil.which("ffprobe"), alpha_path)
        if not _has_alpha_pix_fmt(pix_fmt):
            msg = ("NO_ALPHA_CHANNEL: alpha_path (%s) 实测无透明通道 "
                   "(codec=%s, pix_fmt=%s)。overlay 会把这个不透明帧整块盖住底片画面"
                   "（症状：成片只剩字幕+声音，画面消失）。请确认字幕层是用 "
                   "hf_engine.render(fmt='mov'/'webm') 且 spec.background.type="
                   "'transparent' 渲染出来的，而非 mp4。" % (alpha_path, codec, pix_fmt))
            if verbose:
                print("[compose] " + msg, flush=True)
            return {"ok": False, "error": msg}
    if verify_min_kb is None:
        verify_min_kb = _verify_threshold_kb(width, height)
    main_duration = _probe_duration(fp or shutil.which("ffprobe"), video_path)
    if not main_duration:
        return {"ok": False, "error": "MAIN_VIDEO_DURATION_UNAVAILABLE"}
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    import tempfile
    temp_dir = tempfile.mkdtemp(prefix=".subtitle-", dir=os.path.dirname(out_path) or ".")
    temporary = os.path.join(temp_dir, os.path.basename(out_path))
    fc = ("[1:v]scale=%d:%d[sub];[0:v][sub]overlay=0:0:format=auto:"
          "eof_action=pass:shortest=0[v]" % (width, height))
    args = [ff, "-hide_banner", "-y",
            "-i", video_path, "-i", alpha_path,
            "-filter_complex", fc,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
            "-c:a", "aac", "-b:a", "192k", "-t", "%.6f" % main_duration, temporary]
    if verbose:
        print("[compose] ffmpeg overlay 合成中…", flush=True)
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0 or not os.path.exists(temporary) or os.path.getsize(temporary) == 0:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        return {"ok": False, "error": p.stdout.decode("utf-8", "replace")[-1000:]}
    # 验证：抽中段一帧，字节数达阈值说明含真实画面信息（非全黑/空）
    verify_bytes = _ffprobe_frame_bytes(ff, temporary, t=None)
    verify_kb = round(verify_bytes / 1024, 1)
    ok = verify_kb >= verify_min_kb
    if ok:
        os.replace(temporary, out_path)
    else:
        os.remove(temporary)
    shutil.rmtree(temp_dir, ignore_errors=True)
    return {"ok": ok, "out": out_path, "abspath": out_path, "verify_kb": verify_kb,
            "verify_min_kb": verify_min_kb}


def run(video_path, lines, out_path, width=1080, height=1920, fps=30, frames=4,
        work_dir=None, alpha_fmt="mov", keep_intermediate=False, verbose=True,
        safe_zone_doc=None):
    """全链路：analyze → build-scenes → HyperFrames alpha 渲染 → ffmpeg overlay → 验证。

    返回 {ok, out, safe_zone, verify_kb, alpha_path?}。默认渲染后清理中间文件。
    """
    import hf_engine

    def log(m):
        if verbose:
            print(m, flush=True)

    if safe_zone_doc is not None:
        if (not isinstance(safe_zone_doc, dict) or
                not isinstance(safe_zone_doc.get("safe_zone"), dict)):
            raise ValueError("SAFE_ZONE_DOCUMENT_INVALID")
        sz_doc = safe_zone_doc
    else:
        sz_doc = analyze(video_path, frames=frames, width=width, height=height, verbose=verbose)
    log("[run] 安全区: %s" % json.dumps(sz_doc.get("safe_zone", {}), ensure_ascii=False))
    spec = build_scenes(lines, sz_doc, width=width, height=height, fps=fps)

    out_path = os.path.abspath(out_path)
    work_dir = work_dir or os.path.join(ROOT, "output", "_hf_sub")
    alpha_path = os.path.join(os.path.dirname(out_path),
                              "subtitles_alpha." + ("mov" if alpha_fmt == "mov" else alpha_fmt))
    log("[run] 渲染 alpha 字幕层 (%s)…" % alpha_fmt)
    hf_engine.render(spec, alpha_path, work_dir=work_dir, fmt=alpha_fmt, verbose=verbose)

    res = compose(video_path, alpha_path, out_path, width=width, height=height, verbose=verbose)
    res["safe_zone"] = sz_doc.get("safe_zone")
    res["vision_model"] = sz_doc.get("model")
    if not keep_intermediate:
        try:
            os.remove(alpha_path)
            shutil.rmtree(work_dir, ignore_errors=True)
        except OSError:
            pass
    else:
        res["alpha_path"] = alpha_path
    return res


def formal_caption_gate(manifest, caption_artifact, *, client, video_path, lines_path):
    """Validate approved timeline identity and exact basecut/lines bytes."""
    rm.identity_gate(manifest, client=client)
    script_splitter.caption_artifact_is_current(
        manifest, caption_artifact, client=client, require_approved=True)
    files = caption_artifact["files"]
    if os.path.abspath(video_path) != files["basecut"]["path"]:
        raise ValueError("CAPTION_BASECUT_IDENTITY_MISMATCH")
    if os.path.abspath(lines_path) != files["lines"]["path"]:
        raise ValueError("CAPTION_LINES_IDENTITY_MISMATCH")
    if not rm.file_record_is_current(files["basecut"]) or not rm.file_record_is_current(files["lines"]):
        raise ValueError("STALE_CAPTION_INPUT")
    return True


def begin_caption_render(manifest, manifest_path, caption_artifact, *, client,
                         video_path, lines_path, out_path):
    """Persist a content-bound running transaction before local rendering."""
    formal_caption_gate(
        manifest, caption_artifact, client=client,
        video_path=video_path, lines_path=lines_path)
    planned = rm.require_run_output(
        manifest, out_path, label="caption_render_output")
    manifest["caption_render"] = {
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "timeline_approval": "approved",
        "caption_identity": caption_artifact["caption_identity"],
        "basecut": rm.file_record(video_path),
        "lines": rm.file_record(lines_path),
        "planned_output": planned,
    }
    rm.save_manifest(manifest, manifest_path)
    return manifest["caption_render"]


def record_caption_render(manifest, manifest_path, caption_artifact, out_path):
    """Record rendered subtitles separately from approved timeline inputs.

    The timeline remains the `captions` approval. This derived render is allowed
    as a final input while it waits for the final-stage customer approval.
    """
    transaction = manifest.get("caption_render") or {}
    if (transaction.get("status") != "running" or
            transaction.get("caption_identity") != caption_artifact.get("caption_identity") or
            transaction.get("planned_output") != os.path.abspath(out_path) or
            not rm.file_record_is_current(transaction.get("basecut")) or
            not rm.file_record_is_current(transaction.get("lines"))):
        raise ValueError("CAPTION_RENDER_RUNNING_TRANSACTION_REQUIRED")
    output = rm.file_record(out_path)
    if not output or not output.get("exists"):
        raise ValueError("CAPTION_RENDER_OUTPUT_MISSING")
    manifest["caption_render"] = {
        "status": "pending_final_approval",
        "started_at": transaction.get("started_at"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "timeline_approval": "approved",
        "caption_identity": caption_artifact["caption_identity"],
        "basecut_sha256": caption_artifact["files"]["basecut"]["sha256"],
        "output": output,
    }
    rm.add_output(manifest, out_path, kind="caption_render")
    rm.save_manifest(manifest, manifest_path)
    return manifest["caption_render"]


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_text(path, text):
    """写文本前确保父目录存在（--out 指向不存在的子目录时不崩）。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None):
    p = argparse.ArgumentParser(description="字幕叠加 + 位置智能推荐（视觉模型安全区 + ProRes alpha overlay）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("analyze", help="抽帧 → 视觉模型推荐字幕安全区")
    pa.add_argument("--video", required=True)
    pa.add_argument("--frames", type=int, default=4)
    pa.add_argument("--width", type=int, default=1080)
    pa.add_argument("--height", type=int, default=1920)
    pa.add_argument("--out", help="安全区 JSON 输出（默认 stdout）")

    pb = sub.add_parser("build-scenes", help="台词 + 安全区 → HyperFrames 场景 JSON")
    pb.add_argument("--lines", required=True, help="逐句台词 JSON: [{text,start,end}]")
    pb.add_argument("--safe-zone", required=True, help="analyze 输出的安全区 JSON")
    pb.add_argument("--width", type=int, default=1080)
    pb.add_argument("--height", type=int, default=1920)
    pb.add_argument("--fps", type=int, default=30)
    pb.add_argument("--out", required=True)

    pc = sub.add_parser("compose", help="alpha 字幕层 → ffmpeg overlay 叠回成片")
    pc.add_argument("--video", required=True)
    pc.add_argument("--alpha", required=True, help="HyperFrames 渲出的 alpha 字幕层视频")
    pc.add_argument("--width", type=int, default=1080)
    pc.add_argument("--height", type=int, default=1920)
    pc.add_argument("--out", required=True)

    pr = sub.add_parser("run", help="全链路：analyze → build-scenes → alpha 渲染 → overlay → 验证")
    pr.add_argument("--video", required=True)
    pr.add_argument("--lines", required=True)
    pr.add_argument("--width", type=int, default=1080)
    pr.add_argument("--height", type=int, default=1920)
    pr.add_argument("--fps", type=int, default=30)
    pr.add_argument("--frames", type=int, default=4)
    pr.add_argument("--alpha-fmt", default="mov", choices=["mov", "webm"])
    pr.add_argument("--keep-intermediate", action="store_true")
    pr.add_argument("--safe-zone",
                    help="可选：analyze 后人工检查的安全区 JSON；仍通过正式 run gate/登记")
    pr.add_argument("--out", required=True)
    pr.add_argument("--client")
    pr.add_argument("--manifest", help="正式流程 run_manifest.json")
    pr.add_argument("--caption-manifest", help="已确认 caption timeline artifact")
    pr.add_argument("--draft", action="store_true", help="草稿兼容：允许旧的 video+lines 调用")

    a = p.parse_args(argv)
    if a.cmd == "analyze":
        r = analyze(a.video, frames=a.frames, width=a.width, height=a.height)
        text = json.dumps(r, ensure_ascii=False, indent=2)
        if a.out:
            _write_text(a.out, text)
            print(json.dumps({"ok": True, "out": os.path.abspath(a.out),
                              "safe_zone": r.get("safe_zone")}, ensure_ascii=False))
        else:
            print(text)
        return 0
    if a.cmd == "build-scenes":
        spec = build_scenes(_load_json(a.lines), _load_json(a.safe_zone),
                            width=a.width, height=a.height, fps=a.fps)
        _write_text(a.out, json.dumps(spec, ensure_ascii=False, indent=2))
        print(json.dumps({"ok": True, "out": os.path.abspath(a.out),
                          "scenes": len(spec["scenes"])}, ensure_ascii=False))
        return 0
    if a.cmd == "compose":
        r = compose(a.video, a.alpha, a.out, width=a.width, height=a.height)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if a.cmd == "run":
        if not a.draft and not (a.client and a.manifest and a.caption_manifest):
            p.error("run 正式流程必须提供 --client/--manifest/--caption-manifest；旧方式需 --draft")
        manifest = caption_artifact = None
        if not a.draft:
            manifest = _load_json(a.manifest)
            caption_artifact = _load_json(a.caption_manifest)
            begin_caption_render(
                manifest, a.manifest, caption_artifact, client=a.client,
                video_path=a.video, lines_path=a.lines, out_path=a.out)
        safe_zone_doc = _load_json(a.safe_zone) if a.safe_zone else None
        r = run(a.video, _load_json(a.lines), a.out, width=a.width, height=a.height,
                fps=a.fps, frames=a.frames, alpha_fmt=a.alpha_fmt,
                keep_intermediate=a.keep_intermediate,
                safe_zone_doc=safe_zone_doc)
        if r.get("ok") and not a.draft:
            record_caption_render(manifest, a.manifest, caption_artifact, a.out)
            r["caption_identity"] = caption_artifact["caption_identity"]
            r["approval_status"] = "pending_final_approval"
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
