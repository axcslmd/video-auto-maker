#!/usr/bin/env python3
"""Re-render a basecut with subtitle and motion overlays from captions_motion_v2.json."""
import argparse
import json
import os
import re
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import hf_engine  # noqa: E402
import subtitle_overlay  # noqa: E402
import subtitle_asr  # noqa: E402
import key_setup  # noqa: E402
import text_anim  # noqa: E402
import media_tools  # noqa: E402


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _clean_text(text):
    text = str(text or "").strip()
    text = re.sub(r"[。！？!?；;、,.，…]+$", "", text)
    return text.strip()


def _split_for_readability(text, max_chars=22):
    text = _clean_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;])", text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        parts = [text]
    lines = []
    for part in parts:
        if len(part) <= max_chars:
            lines.append(part)
            continue
        buf = ""
        for ch in part:
            buf += ch
            if len(buf) >= max_chars and ch in "，,、——- ":
                lines.append(buf.strip("，,、——- "))
                buf = ""
        if buf.strip():
            lines.append(buf.strip())
    return [ln for ln in lines if ln]


def _split_dialogue_to_caption_units(text, max_chars=18):
    text = _clean_text(text)
    if not text:
        return []
    text = re.sub(r"——", "|", text)
    text = re.sub(r"[。！？!?；;]+", "|", text)
    text = re.sub(r"[，,、]+", "|", text)
    chunks = [part.strip(" \t\r\n。！？!?；;，,、") for part in text.split("|")]
    units = []
    for chunk in chunks:
        if not chunk:
            continue
        if len(chunk) <= max_chars:
            units.append(chunk)
        else:
            units.extend(_split_for_readability(chunk, max_chars=max_chars))
    return [unit for unit in units if unit]


def _distribute_time(start, end, segments):
    dur = max(0.3, float(end) - float(start))
    total = sum(max(len(x), 1) for x in segments) or 1
    cursor = float(start)
    out = []
    for idx, seg in enumerate(segments):
        weight = max(len(seg), 1) / float(total)
        seg_end = float(end) if idx == len(segments) - 1 else cursor + max(0.6, dur * weight)
        out.append({"text": seg, "start": round(cursor, 3), "end": round(seg_end, 3)})
        cursor = seg_end
    if out:
        out[-1]["end"] = round(float(end), 3)
    return out


def _timed_caption_units(motion_items, basecut, api_key=None):
    approx_lines = []
    for item in motion_items:
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start + 1.0))
        units = _split_dialogue_to_caption_units(item.get("dialogue"), max_chars=17)
        if not units:
            continue
        approx_lines.extend(_distribute_time(start, end, units))
    if not approx_lines:
        return []
    aligned = subtitle_asr.align_with_asr(approx_lines, basecut, api_key=api_key)
    return aligned or approx_lines


def _timed_lines_from_source(lines_source, basecut, api_key=None):
    if not lines_source:
        return []
    raw_lines = []
    for item in lines_source:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text") or item.get("dialogue"))
        if not text:
            continue
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start + 1.0))
        raw_lines.append({"text": text, "start": start, "end": end})
    if not raw_lines:
        return []
    aligned = subtitle_asr.align_with_asr(raw_lines, basecut, api_key=api_key)
    return aligned or raw_lines


def _normalize_timeline(entries, min_gap=0.04, min_duration=0.72, total_duration=None):
    ordered = []
    for idx, item in enumerate(entries or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        ordered.append((float(item.get("start", 0.0)), float(item.get("end", 0.0)), idx, dict(item)))
    ordered.sort(key=lambda x: (x[0], x[1], x[2]))
    if not ordered:
        return []
    normalized = []
    cursor = 0.0
    for i, (_, _, _, item) in enumerate(ordered):
        start = max(float(item.get("start", 0.0)), cursor)
        if normalized:
            start = max(start, float(normalized[-1]["end"]) + min_gap)
        end = max(float(item.get("end", start + min_duration)), start + min_duration)
        if total_duration is not None:
            end = min(end, float(total_duration))
        if i < len(ordered) - 1:
            next_start = max(float(ordered[i + 1][0]), start + min_duration)
            end = min(end, max(start + min_duration, next_start - min_gap))
        if end <= start:
            end = start + min_duration
            if total_duration is not None:
                end = min(end, float(total_duration))
        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        normalized.append(item)
        cursor = end
    return normalized


def _infer_lines_path(captions_motion_path):
    base = os.path.basename(captions_motion_path)
    candidates = []
    if "motion" in base:
        candidates.append(base.replace("motion", "lines"))
    candidates.append("captions_lines_v2.json")
    candidates.append("captions_lines.json")
    root = os.path.dirname(os.path.abspath(captions_motion_path))
    for name in candidates:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _align_units_to_speech(units, speech_segments, total_duration):
    total_chars = sum(max(len(unit), 1) for unit in units) or 1
    total_speech = sum(max(0.0, end - start) for start, end in speech_segments) or max(total_duration, 0.1)
    chars_per_sec = total_chars / total_speech
    aligned = []
    seg_index = 0
    cursor = speech_segments[0][0]
    for unit in units:
        desired = max(0.72, min(3.2, len(unit) / chars_per_sec))
        while seg_index < len(speech_segments) - 1 and cursor >= speech_segments[seg_index][1] - 0.08:
            seg_index += 1
            cursor = max(cursor, speech_segments[seg_index][0])
        start = max(cursor, speech_segments[seg_index][0])
        end = start + desired
        while seg_index < len(speech_segments) - 1 and end > speech_segments[seg_index][1]:
            gap = speech_segments[seg_index + 1][0] - speech_segments[seg_index][1]
            if gap > 0.75:
                end = speech_segments[seg_index][1]
                break
            overflow = end - speech_segments[seg_index][1]
            seg_index += 1
            end = speech_segments[seg_index][0] + overflow
        end = min(end, speech_segments[-1][1], total_duration)
        if end <= start + 0.2:
            end = min(total_duration, start + 0.72)
        aligned.append({"text": unit, "start": round(start, 3), "end": round(end, 3)})
        cursor = end + 0.04
    return aligned


def _wrap_dialogue(text, max_chars=18):
    raw = _clean_text(text)
    if not raw:
        return ""
    parts = re.split(r"([，,。！？!?；;、—-])", raw)
    out = []
    line = ""
    for part in parts:
        if not part:
            continue
        if part in "，,。！？!?；;、—-":
            line += part
            continue
        if len(line) + len(part) > max_chars and line:
            out.append(line.strip("，,。！？!?；;、—-"))
            line = part
        else:
            line += part
    if line.strip():
        out.append(line.strip("，,。！？!?；;、—-"))
    return "\n".join([x for x in out if x])


def _motion_labels(desc):
    text = _clean_text(desc)
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    if "客服/订单/数据" in text:
        return [
            {"text": "客服", "style": "pop", "duration": 1.8},
            {"text": "订单", "style": "slide_up", "duration": 2.0},
            {"text": "数据", "style": "glow", "duration": 2.2},
        ]
    if "ERP/CRM/OA" in text:
        return [
            {"text": "ERP", "style": "pop", "duration": 1.8},
            {"text": "CRM", "style": "slide_left", "duration": 2.0},
            {"text": "OA", "style": "slide_right", "duration": 2.1},
        ]
    if "Demo" in text:
        return [{"text": "Demo 先行", "style": "bounce", "duration": 2.4}]
    if "品牌条" in text or "Logo" in text or "Tagline" in text:
        return [
            {"text": "Basicware", "style": "glow", "duration": 2.4},
            {"text": "AI 员工", "style": "fade_up", "duration": 2.2},
        ]
    if "数据流" in text:
        return [
            {"text": "数据流", "style": "glow", "duration": 2.0},
            {"text": "节点联动", "style": "slide_up", "duration": 2.1},
        ]
    if "关键词卡片" in text:
        tokens = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,4}", text)
        tokens = [t for t in tokens if t not in {"三组", "三大", "快闪", "后期"}][:3]
        return [{"text": t, "style": "pop", "duration": 1.7 + i * 0.2} for i, t in enumerate(tokens)]
    tokens = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,4}", text)
    tokens = [t for t in tokens if t not in {"快闪", "后期", "文字", "浮现"}][:3]
    return [{"text": t, "style": "fade_up", "duration": 1.6 + i * 0.2} for i, t in enumerate(tokens)]


def _motion_positions(layout, width):
    if not isinstance(layout, dict):
        layout = {}
    top = layout.get("top_px")
    left = layout.get("left_px")
    right = layout.get("right_px")
    box_w = layout.get("width_px")
    if top is not None and box_w:
        base = max(40, int(left or 0) + 20)
        mid = int(width * 0.5)
        far = max(40, int(width - int(right or 0) - int(box_w) + 20))
        return [base, mid, far], int(top) + 20
    return [70, 410, 750], 94


def _caption_entries(subtitle_items, motion_items, basecut, motion_layout=None, width=1080, api_key=None):
    subtitle_scenes = []
    cols, default_y = _motion_positions(motion_layout, width)
    subtitle_source = subtitle_items or []
    if not subtitle_source:
        subtitle_source = motion_items
    if isinstance(motion_layout, dict) and motion_layout.get("subtitle_lines"):
        subtitle_source = motion_layout.get("subtitle_lines")
    for line in _timed_lines_from_source(subtitle_source, basecut, api_key=api_key):
        text = _wrap_dialogue(line["text"], max_chars=18)
        if not text:
            continue
        subtitle_scenes.append({
            "text": text,
            "start": line["start"],
            "end": line["end"],
            "preset": "fade_up",
            "size": 50 if len(text) < 22 else 44,
            "pos": "lower",
            "track_index": 1,
            "variant": "spoken",
        })
    subtitle_scenes = _normalize_timeline(
        subtitle_scenes, min_gap=0.03, min_duration=0.75)
    motion_scenes = []
    for item in motion_items:
        if not isinstance(item, dict):
            continue
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start + 1.0))
        labels = _motion_labels(item.get("motion_elements") or [])
        if not labels:
            continue
        y = default_y if start < 20 else default_y + 14
        for idx, label in enumerate(labels[:3]):
            track = 2 + idx
            delay = 0.55 + idx * 0.42
            layer_offset = idx * 0.16
            motion_scenes.append({
                "text": label["text"],
                "start": start + delay + layer_offset,
                "end": min(end - 0.25, start + delay + layer_offset + label["duration"]),
                "preset": label["style"],
                "size": 40 if len(label["text"]) < 10 else 34,
                "pos": "upper",
                "track_index": track,
                "variant": "feature",
                "left_px": cols[min(idx, len(cols) - 1)],
                "top_px": y + idx * 42,
                "width_px": 240 if len(label["text"]) < 10 else 320,
            })
    motion_scenes = _normalize_timeline(motion_scenes, min_gap=0.08, min_duration=1.0)
    return subtitle_scenes + motion_scenes


def _probe_video_size(path):
    _ffmpeg, probe = media_tools.resolve_ffmpeg_pair()
    if not probe:
        return None
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "json", path],
            capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [{}])[0]
        return int(stream.get("width")), int(stream.get("height"))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render cleaner subtitles + motion overlays")
    ap.add_argument("--basecut", required=True)
    ap.add_argument("--captions-motion", required=True)
    ap.add_argument("--captions-lines")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--motion-layout")
    ap.add_argument("--engine", choices=["hyperframes", "libass"], default="hyperframes")
    ap.add_argument("--keep-intermediate", action="store_true")
    args = ap.parse_args(argv)

    motion_items = _load_json(args.captions_motion)
    if not isinstance(motion_items, list) or not motion_items:
        raise SystemExit("captions_motion must be a non-empty list")
    subtitle_path = args.captions_lines or _infer_lines_path(args.captions_motion)
    subtitle_items = []
    if subtitle_path and os.path.isfile(subtitle_path):
        subtitle_items = _load_json(subtitle_path)
        if not isinstance(subtitle_items, list):
            subtitle_items = []

    size = _probe_video_size(args.basecut)
    width, height = size or (args.width, args.height)
    motion_layout = _load_json(args.motion_layout) if args.motion_layout else {}
    key_setup.ensure_session_id()
    api_key = key_setup.load_key()
    scenes = _caption_entries(
        subtitle_items, motion_items, args.basecut,
        motion_layout=motion_layout, width=width, api_key=api_key)
    if not scenes:
        raise SystemExit("No scenes derived from captions_motion")
    duration = max(float(x.get("end", 0.0)) for x in scenes)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)

    spec = {
        "width": width,
        "height": height,
        "fps": args.fps,
        "base": {"type": "video", "path": args.basecut},
        "durationInSeconds": duration,
        "title": "Basicware subtitles repair",
        "eyebrow": "subtitle motion repair",
        "palette": "blue",
        "captions": [
            {"start": sc["start"], "end": sc["end"], "text": sc["text"], "highlight": []}
            for sc in scenes if int(sc.get("track_index", 1)) == 1
        ],
        "scenes": scenes,
    }
    spec_path = os.path.join(out_dir, "text_anim_render_spec.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    if args.engine == "hyperframes":
        hf_spec = {
            "resolution": [width, height],
            "fps": args.fps,
            "duration": duration,
            "background": {"type": "transparent"},
            "brand": {"primary": "#2E73FF"},
            "scenes": scenes,
        }
        hf_spec_path = os.path.join(out_dir, "hyperframes_motion_subtitle_spec.json")
        with open(hf_spec_path, "w", encoding="utf-8") as f:
            json.dump(hf_spec, f, ensure_ascii=False, indent=2)
        alpha_path = os.path.join(out_dir, "hyperframes_motion_subtitle_alpha.mov")
        work_dir = os.path.join(out_dir, "_hf_motion_subtitles")
        hf_engine.render(hf_spec, alpha_path, work_dir=work_dir, fmt="mov", verbose=True)
        result_doc = subtitle_overlay.compose(
            args.basecut, alpha_path, args.out, width=width, height=height, verbose=True)
        if not result_doc.get("ok"):
            raise SystemExit(result_doc.get("error") or "HyperFrames subtitle compose failed")
        result = result_doc["out"]
        if not args.keep_intermediate:
            try:
                os.remove(alpha_path)
            except OSError:
                pass
        print(json.dumps({"ok": True, "out": os.path.abspath(result),
                          "spec_path": os.path.abspath(hf_spec_path),
                          "text_anim_spec_path": os.path.abspath(spec_path),
                          "engine": "hyperframes"},
                         ensure_ascii=False, indent=2))
    else:
        result = text_anim.render(spec, args.out)
        print(json.dumps({"ok": True, "out": os.path.abspath(result), "spec_path": os.path.abspath(spec_path),
                          "engine": "libass"},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
