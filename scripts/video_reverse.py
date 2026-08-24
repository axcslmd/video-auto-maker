#!/usr/bin/env python3
"""阶段6 · 视频逆向工程：已确认底片 → 逐镜头时间轴 → Remotion 方案命令。

定位（修正后的正确顺序）：
  阶段5 客户第一轮确认并下载「最终版底片」之后，才进入本阶段。
  用「顶级AI视频提示词架构师」提示词把底片逐镜头拆解，先输出完整时间轴，
  再反推出一套「结合动效素材、在 Remotion 中使用的方案命令」，交阶段7 本地剪辑成片。

做法：
  1. 均匀抽底片关键帧（ocr_check.extract_frames 复用）。
  2. 把关键帧 + 逆向工程提示词喂给 BasicRouter 多模态模型（实时从在线列表挑视觉模型，
     默认 kimi-k3；帧先上传拿 https URL，用 input_text/input_image 格式）。
  3. LLM 产出：①完整时间轴（逐镜头拆解）②Remotion 方案命令（JSON，供 final_edit.py 吃）。
  4. 落盘 output/reverse_timeline.md + output/remotion_scheme.json。

注意：LLM 从静帧+已知时长推断运镜/景别/光线，属**辅助**；口型/环境声无法从静帧判断。
      方案命令强制包含「禁止项」（防变脸/服装漂移/肢体错误/背景闪烁/动作断裂/物理失真）。

CLI:
  reverse --basecut output/basecut.mp4 --target-model kling-v3-omni-video \
          --frames 12 --out-dir output [--motion-assets output/content_assets.json]
"""
import os
import re
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import br_client          # noqa: E402 — hoisted from 2 inline imports (v3 cleanup)
import text_provider      # noqa: E402

# 用户指定的「顶级AI视频提示词架构师」逆向工程提示词（逐字保留其意图与禁止项要求）。
REVERSE_PROMPT = """请全面化身为顶级AI视频提示词架构师、电影级镜头语言分析师与多模态视频逆向工程师。\
总结视频内容，请按照时间顺序逐镜头拆解主体身份与外观锚点、动作轨迹表情变化、场景空间、\
前中后景关系、景别、机位、焦段、构图、景深、运镜方式、运动速度、光线方向、综合色温、材质、\
节奏、转场、特效和环境声音。先输出完整的时间轴，再反推出一套适用于[目标视频模型]与动效素材\
相结合、在 Remotion 中使用的方案命令。多镜头必须分别标注起始画面、人物动作、摄影机运动、\
结束画面和镜头衔接，并补充防止人物变脸、服装漂移、肢体错误、背景闪烁、动作断裂和物理关系\
失真的禁止项。"""

# 要求 LLM 在时间轴之后追加一段结构化 JSON（供 final_edit.py 直接消费）。
# 三分离契约（本版新增）：
#   ① 动效建议 motion_suggestion —— 哪种动效放在哪个位置/时机（如「左下角数据卡片从下滑入」）
#   ② 动效内容 motion_content    —— 具体要显示的文字/数据（如 title/bullets/metric 结构）
#   ③ 字幕 subtitles            —— 配音逐句时间轴（供烧制 srt + Remotion 字幕轨）
# motion_overlay 保留为向后兼容的自由文本摘要（final_edit 仍能吃）。
SCHEME_SPEC = """
【输出格式（严格遵守）】
第一部分：完整时间轴（Markdown），按时间顺序逐镜头，每镜含上述所有维度。
第二部分：在时间轴之后，输出一个 ```json 代码块，schema 如下（这是给 Remotion 剪辑器吃的方案命令）：
{
  "target_model": "<目标视频模型>",
  "fps": 30,
  "width": 1080,
  "height": 1920,
  "shots": [
    {
      "id": "s1",
      "start_sec": 0.0,
      "end_sec": 3.0,
      "start_frame_desc": "起始画面描述",
      "subject_anchor": "主体身份与外观锚点",
      "action": "人物动作/动作轨迹",
      "camera_move": "摄影机运动（推/拉/摇/移/固定）",
      "shot_size": "景别（远/全/中/近/特）",
      "angle": "机位角度",
      "end_frame_desc": "结束画面描述",
      "transition_to_next": "镜头衔接方式",
      "motion_suggestion": {
        "style": "动效类型（title_reveal/bullet_list/metric_pop/lower_third/keyword_flash/data_card 之一，或口语描述）",
        "position": "屏幕位置（center/top/bottom/lower_third/left/right/corner）",
        "timing": "何时出现（如 进场0.5s后/全程/结尾强调）",
        "note": "为什么这样叠（一句话理由，可空）"
      },
      "motion_content": {
        "title": "叠加主标题（无则空串）",
        "bullets": ["要点1", "要点2"],
        "metric": {"value": "数值如 3倍", "label": "指标名如 效率提升"}
      },
      "motion_overlay": "上面动效内容的一句话自由文本摘要（向后兼容字段，可由 title+bullets 拼成）",
      "lighting": "光线方向+色温",
      "pace": "节奏"
    }
  ],
  "subtitles": [
    {"index": 1, "start_sec": 0.0, "end_sec": 2.4, "text": "这一句配音台词"},
    {"index": 2, "start_sec": 2.4, "end_sec": 5.0, "text": "下一句配音台词"}
  ],
  "prohibitions": ["人物变脸","服装漂移","肢体错误","背景闪烁","动作断裂","物理关系失真"]
}
【三分离硬性要求】每个镜头必须区分：① motion_suggestion（用哪种动效、放哪、何时出现）；
② motion_content（具体要显示的文字/数据）；③ 顶层 subtitles（配音逐句时间轴，用于生成 srt 字幕）。
subtitles 的时间轴要覆盖整条底片、按配音节奏逐句断句（每句 1-2 行、约 2-5 秒），
且 subtitles 与 shots 是两套并行轨（字幕跟配音走，不必与镜头切换对齐）。
【硬性约束】JSON 顶层必须有 "shots" 数组与 "subtitles" 数组。
禁止改用 "scenes"、"elements"、"timeline"、"remotion" 等其它顶层结构；禁止输出 Remotion 工程配置或绝对坐标绘图指令。
这是"逐镜头方案命令"而非"Remotion 项目文件"。只输出时间轴 + 一个含顶层 shots/subtitles 的 json 代码块，不要其它解释。"""

def _list_vision_models():
    """Compatibility wrapper around the shared gateway capability owner."""
    return br_client.list_vision_models()


def _pick_vl_model():
    """Compatibility wrapper around the shared gateway capability owner."""
    return br_client.pick_vision_model()


def _probe_duration(video_path):
    try:
        import ocr_check
        ff, fp = ocr_check._ffmpeg_bins()
        if fp:
            import subprocess
            r = subprocess.run([fp, "-v", "quiet", "-print_format", "json",
                                "-show_format", video_path],
                               capture_output=True, text=True)
            return float(json.loads(r.stdout)["format"]["duration"])
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return None


def _extract_json_block(text):
    """从 LLM 回复里抠出第一个 ```json ...``` 代码块并解析。失败返回 None。"""
    if not text:
        return None
    m = re.search(r"```json\s*(.+?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if not raw:
        # 兜底：找第一个 {...} 顶层对象
        m2 = re.search(r"\{.*\}", text, re.DOTALL)
        raw = m2.group(0) if m2 else None
    if not raw:
        return None
    try:
        return text_provider.extract_json(raw)
    except ValueError:
        return None


def _sec_to_srt_ts(sec):
    """秒 → SRT 时间码 HH:MM:SS,mmm。"""
    try:
        sec = max(0.0, float(sec))
    except (TypeError, ValueError):
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:  # 进位保护
        s += 1
        ms = 0
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _normalize_subtitles(subs):
    """把模型返回的 subtitles 规范成 [{index,start_sec,end_sec,text}]（按 start 排序）。

    容错：字段名可能是 start/end/from/to；缺 index 自动补；空 text 丢弃。
    识别不出返回 []。
    """
    if not isinstance(subs, list):
        return []
    out = []
    for it in subs:
        if not isinstance(it, dict):
            continue
        text = (it.get("text") or it.get("content") or "").strip()
        if not text:
            continue
        st = it.get("start_sec", it.get("start", it.get("from")))
        en = it.get("end_sec", it.get("end", it.get("to")))
        try:
            st = float(st) if st is not None else None
            en = float(en) if en is not None else None
        except (TypeError, ValueError):
            st = en = None
        out.append({"start_sec": st, "end_sec": en, "text": text})
    # 排序 + 补时间轴空洞（缺 start/end 的按顺序均分兜底交给上层，这里先按已知 start 排）
    out.sort(key=lambda x: (x["start_sec"] is None, x["start_sec"] or 0.0))
    for i, it in enumerate(out):
        it["index"] = i + 1
    return out


def write_srt(subtitles, srt_path, total_duration=None):
    """把规范化后的 subtitles 写成 .srt 文件。返回写入的字幕条数。

    缺 start/end 的条目：按总时长在缺失区间内均分兜底，保证每条都有合法时间码。
    """
    subs = _normalize_subtitles(subtitles)
    if not subs:
        return 0
    n = len(subs)
    # 兜底填充缺失时间：整体在 [0, total] 上按条均分
    total = total_duration if (total_duration and total_duration > 0) else (n * 3.0)
    for i, it in enumerate(subs):
        if it.get("start_sec") is None:
            it["start_sec"] = round(total * i / n, 3)
        if it.get("end_sec") is None:
            nxt = subs[i + 1]["start_sec"] if i + 1 < n and subs[i + 1].get("start_sec") is not None else None
            it["end_sec"] = round(nxt if nxt else total * (i + 1) / n, 3)
        # 防越界：end 必须 > start
        if it["end_sec"] <= it["start_sec"]:
            it["end_sec"] = it["start_sec"] + 1.5
    lines = []
    for it in subs:
        lines.append(str(it["index"]))
        lines.append("%s --> %s" % (_sec_to_srt_ts(it["start_sec"]), _sec_to_srt_ts(it["end_sec"])))
        lines.append(it["text"])
        lines.append("")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return n


def _synthesize_overlay(shot):
    """motion_overlay 缺失时，从 motion_content(title/bullets/metric) 合成一句摘要。

    保证向后兼容：老的 final_edit._overlay_to_content 仍能吃到自由文本。
    """
    mc = shot.get("motion_content")
    if not isinstance(mc, dict):
        return shot.get("motion_overlay") or ""
    parts = []
    if mc.get("title"):
        parts.append(str(mc["title"]).strip())
    for b in (mc.get("bullets") or []):
        if str(b).strip():
            parts.append(str(b).strip())
    metric = mc.get("metric")
    if isinstance(metric, dict) and (metric.get("value") or metric.get("label")):
        parts.append(("%s %s" % (metric.get("value", ""), metric.get("label", ""))).strip())
    return "；".join([p for p in parts if p][:5])


def _normalize_scheme(scheme, fps=30):
    """把模型可能返回的多种结构统一成含顶层 shots[] 的规范 scheme。

    尽管 prompt 已强约束顶层 shots，个别模型仍会返回 Remotion 工程式结构
    （remotion.scenes[] / scenes[] / timeline[]）。这里做兜底归一，保证阶段7
    final_edit.compile_shotlist 一定能吃到 shots[]，不因结构漂移而断链。
    识别不出镜头时返回原 scheme（交由上层「无 shots」告警）。
    """
    if not isinstance(scheme, dict):
        return scheme
    if isinstance(scheme.get("shots"), list) and scheme["shots"]:
        return scheme  # 已规范

    # 找候选场景数组：remotion.{scenes,timeline,shots} / 顶层 scenes/timeline/shotList/cuts
    scenes = None
    rem = scheme.get("remotion")
    # 场景数组候选键（涵盖各模型偏好命名）。composition 里也常带 fps/尺寸。
    _SCENE_KEYS = ("scenes", "sequences", "timeline", "shots", "shotList", "shot_list", "cuts")
    comp = scheme.get("composition") if isinstance(scheme.get("composition"), dict) else None
    if isinstance(rem, dict):
        comp = comp or (rem.get("composition") if isinstance(rem.get("composition"), dict) else None)
    if comp:
        scheme.setdefault("fps", comp.get("fps", fps))
        scheme.setdefault("width", comp.get("width", 1080))
        scheme.setdefault("height", comp.get("height", 1920))
    if isinstance(rem, dict):
        for rk in _SCENE_KEYS:
            if isinstance(rem.get(rk), list) and rem[rk]:
                scenes = rem[rk]
                break
    for key in _SCENE_KEYS:
        if scenes is None and isinstance(scheme.get(key), list) and scheme[key]:
            scenes = scheme[key]
    if not scenes:
        return scheme  # 认不出，交上层告警

    eff_fps = scheme.get("fps", fps) or fps

    def _tc_to_sec(v):
        """'HH:MM:SS' / 'MM:SS' / 秒数 / 帧号 → 秒。帧号(>120)按 fps 折算。"""
        if v is None:
            return None
        if isinstance(v, str) and ":" in v:
            parts = v.split(":")
            try:
                parts = [float(p) for p in parts]
            except (TypeError, ValueError):
                return None
            sec = 0.0
            for p in parts:
                sec = sec * 60 + p
            return sec
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v / eff_fps if v > 120 else v

    shots = []
    cursor = 0.0
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        def _frames_to_sec(v):
            try:
                return float(v) / eff_fps
            except (TypeError, ValueError):
                return None

        # 起点：秒制字段用 _tc_to_sec；帧制字段（startFrame/from）一律按帧÷fps。
        s_sec = None
        if sc.get("start_sec") is not None:
            s_sec = _tc_to_sec(sc.get("start_sec"))
        elif sc.get("start") is not None:
            s_sec = _tc_to_sec(sc.get("start"))
        elif sc.get("time") is not None:
            s_sec = _tc_to_sec(sc.get("time"))
        elif sc.get("startFrame") is not None:
            s_sec = _frames_to_sec(sc.get("startFrame"))
        elif sc.get("from") is not None:
            s_sec = _frames_to_sec(sc.get("from"))
        if s_sec is None:
            s_sec = cursor
        # 终点：优先显式 end；否则用 duration(秒) / durationInFrames(帧) 推。
        e_sec = None
        if sc.get("end_sec") is not None:
            e_sec = _tc_to_sec(sc.get("end_sec"))
        elif sc.get("end") is not None:
            e_sec = _tc_to_sec(sc.get("end"))
        elif sc.get("endFrame") is not None:
            e_sec = _frames_to_sec(sc.get("endFrame"))
        if e_sec is None:
            dur = _tc_to_sec(sc.get("duration")) if sc.get("duration") is not None else None
            if dur is None and sc.get("durationInFrames") is not None:
                dur = _frames_to_sec(sc.get("durationInFrames"))
            e_sec = s_sec + (dur if dur else 3.0)
        cursor = e_sec
        # 从 elements / layers 里捞文字作 motion_overlay 建议
        overlay = sc.get("motion_overlay") or ""
        if not overlay:
            texts = []
            for el in (sc.get("elements") or []) + (sc.get("layers") or []):
                if not isinstance(el, dict):
                    continue
                # 文本可能在 el.content / el.text / el.style.content
                txt = el.get("content") or el.get("text")
                if not txt and isinstance(el.get("style"), dict):
                    txt = el["style"].get("content") or el["style"].get("text")
                if not txt:
                    continue
                etype = str(el.get("type", "")).lower()
                # 只收文本类图层（type 为空/含 text/title），跳过 rect/circle 等纯图形
                if etype in ("", "text", "title") or "text" in etype:
                    texts.append(str(txt).replace("\n", " ").strip())
            overlay = "；".join([t for t in texts if t][:3])
        shots.append({
            "id": sc.get("id") or ("s%d" % (i + 1)),
            "start_sec": round(s_sec, 3),
            "end_sec": round(e_sec, 3),
            "camera_move": sc.get("camera_move", sc.get("camera", "")),
            "motion_suggestion": sc.get("motion_suggestion") if isinstance(sc.get("motion_suggestion"), dict) else {},
            "motion_content": sc.get("motion_content") if isinstance(sc.get("motion_content"), dict) else {},
            "motion_overlay": overlay,
            "transition_to_next": sc.get("transition_to_next", sc.get("transition", "")),
            "subject_anchor": sc.get("subject_anchor", sc.get("name", "")),
        })
    if shots:
        scheme["shots"] = shots
        scheme["_normalized_from"] = "scenes"  # 留痕，便于排查
    return scheme


def _postprocess_scheme(scheme):
    """规范化后统一收尾：① 每个 shot 补 motion_overlay(从 motion_content 合成)；
    ② 顶层 subtitles 规范化。就地修改并返回。"""
    if not isinstance(scheme, dict):
        return scheme
    for sh in (scheme.get("shots") or []):
        if not isinstance(sh, dict):
            continue
        if not (sh.get("motion_overlay") or "").strip():
            syn = _synthesize_overlay(sh)
            if syn:
                sh["motion_overlay"] = syn
    scheme["subtitles"] = _normalize_subtitles(scheme.get("subtitles"))
    return scheme


def reverse(basecut_path, target_model="kling-v3-omni-video", frames=12,
            out_dir="output", motion_assets=None, fps=30, width=1080, height=1920,
            verbose=True):
    """底片 → 逆向工程时间轴 + Remotion 方案命令。

    返回 {ok, timeline_path, scheme_path, scheme, model, frames_used, error?}。
    """
    import ocr_check
    import key_setup

    def log(m):
        if verbose:
            print(m, flush=True)

    if not os.path.exists(basecut_path):
        raise FileNotFoundError("底片不存在: %s" % basecut_path)
    api_key = key_setup.load_key()
    if not api_key:
        raise br_client.BRError("No API key. 先跑密钥准入闸门。")

    os.makedirs(out_dir, exist_ok=True)
    duration = _probe_duration(basecut_path)
    log("[reverse] 底片时长: %s 秒，抽 %d 帧" % (duration, frames))

    frame_paths, tmpdir = ocr_check.extract_frames(basecut_path, n=frames)
    if not frame_paths:
        raise RuntimeError("抽帧失败，无法逆向分析")
    log("[reverse] 已抽 %d 帧" % len(frame_paths))

    model = _pick_vl_model()
    log("[reverse] 视觉模型: %s" % model)

    # 组多模态 content：提示词 + 时长上下文 + 所有关键帧
    ctx = REVERSE_PROMPT.replace("[目标视频模型]", target_model) + SCHEME_SPEC
    if duration:
        ctx += "\n\n【底片总时长】%.2f 秒；【帧数】均匀抽取 %d 帧；【目标 fps】%d。" % (
            duration, len(frame_paths), fps)
    if motion_assets:
        ctx += "\n【可用动效素材清单】%s" % json.dumps(motion_assets, ensure_ascii=False)[:800]

    # BasicRouter 多模态格式（见 basicrouter.ai/docs）：content 数组用
    #   {"type":"input_image","image_url":"<url字符串>"} + {"type":"input_text","text":"..."}
    # 注意：image_url 是**扁平字符串**（非 OpenAI 的嵌套 {"url":...}）；图片需为可访问 URL，
    # 故本地帧先经 to_image_ref(prefer_hosted=True) 上传拿 https URL。
    # 指令块放在**图片之前**（instruction-first），模型更会把它当主任务执行，
    # 而不是把图片当主体做泛化描述。末尾再追加一句强约束，确保产出 json 方案命令。
    content = [{"type": "input_text", "text": ctx}]
    hosted = 0
    for i, fp in enumerate(frame_paths):
        try:
            ref = br_client.to_image_ref(fp, api_key=api_key, prefer_hosted=True)
        except (br_client.BRError, OSError, ValueError, RuntimeError) as he:
            log("[reverse] 帧上传失败，跳过: %s" % he)
            continue
        content.append({"type": "input_text", "text": "第 %d 帧（按时间顺序）：" % (i + 1)})
        content.append({"type": "input_image", "image_url": ref})
        hosted += 1
    content.append({"type": "input_text", "text":
                    "以上为底片按时间顺序抽取的关键帧。现在严格按【输出格式】执行："
                    "先输出完整时间轴（逐镜头），再输出一个 ```json 代码块（方案命令）。"
                    "不要只做泛化的图片内容描述。"})
    if hosted == 0:
        _cleanup(tmpdir)
        return {"ok": False, "error": "所有关键帧上传失败，无法逆向分析",
                "model": model, "frames_used": 0}

    system_msg = {
        "role": "system",
        "content": (
            "你是顶级AI视频提示词架构师、电影级镜头语言分析师与多模态视频逆向工程师。"
            "你的唯一任务是对给定关键帧做逐镜头逆向工程，**严格**按用户指定的【输出格式】产出："
            "第一部分是逐镜头完整时间轴，第二部分是一个 ```json 代码块（Remotion 方案命令）。"
            "禁止把它当成普通图片描述或营销文案分析，禁止输出「需要我帮你起草…」之类的收尾问句，"
            "禁止省略 json 代码块。只输出时间轴 + 一个 json 代码块。"
        ),
    }
    msgs = [system_msg, {"role": "user", "content": content}]
    # 重推理视觉模型（如 kimi-k3）非流式会被网关长连接断开，**优先流式**保活；
    # 流式失败再降级到非流式（给足 600s）。两条路都过 chat/_extract 归一。
    resp = None
    try:
        resp = br_client.chat_stream(api_key, msgs, model=model, timeout=600)
    except (br_client.BRError, OSError, ValueError, RuntimeError) as se:
        log("[reverse] 流式失败，降级非流式重试: %s" % se)
        try:
            resp = br_client.chat(api_key, msgs, model=model, timeout=600)
        except (br_client.BRError, OSError, ValueError, RuntimeError) as e:
            _cleanup(tmpdir)
            return {"ok": False, "error": "多模态分析失败（模型不支持视觉/网关错误）: %s" % e,
                    "model": model, "frames_used": hosted}
    _cleanup(tmpdir)

    timeline_path = os.path.join(out_dir, "reverse_timeline.md")
    with open(timeline_path, "w", encoding="utf-8") as f:
        f.write(resp or "")

    scheme = _extract_json_block(resp)
    if scheme:
        scheme = _normalize_scheme(scheme, fps)
        scheme = _postprocess_scheme(scheme)  # 补 overlay 摘要 + 规范化 subtitles
    scheme_path = os.path.join(out_dir, "remotion_scheme.json")
    srt_path = None
    if scheme:
        # 补默认字段
        scheme.setdefault("target_model", target_model)
        scheme.setdefault("fps", fps)
        scheme.setdefault("width", width)
        scheme.setdefault("height", height)
        scheme.setdefault("prohibitions",
                          ["人物变脸", "服装漂移", "肢体错误", "背景闪烁", "动作断裂", "物理关系失真"])
        scheme["_basecut"] = os.path.abspath(basecut_path)
        with open(scheme_path, "w", encoding="utf-8") as f:
            json.dump(scheme, f, ensure_ascii=False, indent=2)
        # 落盘 srt 字幕轨（配音逐句时间轴），供 final_edit 烧字幕
        subs = scheme.get("subtitles") or []
        if subs:
            srt_candidate = os.path.join(out_dir, "reverse.srt")
            n = write_srt(subs, srt_candidate, total_duration=duration)
            if n:
                srt_path = srt_candidate
                scheme["_srt"] = os.path.abspath(srt_path)
                # srt 路径回写进 scheme，方便 final_edit 直接读
                with open(scheme_path, "w", encoding="utf-8") as f:
                    json.dump(scheme, f, ensure_ascii=False, indent=2)
                log("[reverse] 已写字幕轨 %s（%d 句）" % (srt_path, n))
        else:
            log("[reverse] 警告：方案里没有 subtitles，未生成 srt 字幕轨。")
    else:
        log("[reverse] 警告：未能从回复中解析出 JSON 方案命令，仅保存时间轴。")

    return {"ok": True, "timeline_path": timeline_path,
            "scheme_path": scheme_path if scheme else None,
            "srt_path": srt_path,
            "scheme": scheme, "model": model, "frames_used": len(frame_paths)}


def _cleanup(tmpdir):
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="阶段6 · 视频逆向工程（底片→时间轴→Remotion 方案命令）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rv = sub.add_parser("reverse", help="底片逆向 → 时间轴 + 方案命令")
    rv.add_argument("--basecut", required=True, help="阶段5 已确认的最终版底片 mp4")
    rv.add_argument("--target-model", default="kling-v3-omni-video", help="目标视频模型名")
    rv.add_argument("--frames", type=int, default=12, help="抽帧数（默认12）")
    rv.add_argument("--out-dir", default="output")
    rv.add_argument("--motion-assets", help="可用动效素材清单 JSON 路径（content_scaffold 产物等）")
    rv.add_argument("--fps", type=int, default=30)
    rv.add_argument("--width", type=int, default=1080)
    rv.add_argument("--height", type=int, default=1920)

    a = ap.parse_args(argv)
    if a.cmd == "reverse":
        motion = None
        if a.motion_assets and os.path.exists(a.motion_assets):
            with open(a.motion_assets, "r", encoding="utf-8") as f:
                motion = json.load(f)
        r = reverse(a.basecut, target_model=a.target_model, frames=a.frames,
                    out_dir=a.out_dir, motion_assets=motion,
                    fps=a.fps, width=a.width, height=a.height)
        print(json.dumps({k: v for k, v in r.items() if k != "scheme"},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
