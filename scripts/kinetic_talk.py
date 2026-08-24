#!/usr/bin/env python3
"""Build and validate HorizontalKinetic Remotion props."""
import argparse
import json
import os


ACCENTS = {"blue", "green", "red", "yellow", "purple"}
LAYOUTS = {"intro", "cards", "flow", "cta"}


def _num(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def validate_spec(spec):
    errors, warnings = [], []
    if spec.get("width") not in (None, 1920) or spec.get("height") not in (None, 1080):
        errors.append("横版口播输出必须为 1920x1080")
    duration = _num(spec.get("durationInSeconds"))
    if duration <= 0:
        errors.append("durationInSeconds 必须大于 0")
    captions = spec.get("captions") or []
    previous_end = 0
    for index, cue in enumerate(captions, 1):
        start, end = _num(cue.get("start")), _num(cue.get("end"))
        if end <= start:
            errors.append("字幕 %d 的 end 必须大于 start" % index)
        if start < previous_end - 0.05:
            warnings.append("字幕 %d 与上一条重叠" % index)
        previous_end = max(previous_end, end)
    scenes = spec.get("scenes") or []
    previous_end = 0
    for index, scene in enumerate(scenes, 1):
        start, end = _num(scene.get("start")), _num(scene.get("end"))
        if end <= start:
            errors.append("场景 %d 的 end 必须大于 start" % index)
        if scene.get("accent") not in ACCENTS:
            errors.append("场景 %d 的 accent 无效" % index)
        if scene.get("layout") not in LAYOUTS:
            errors.append("场景 %d 的 layout 无效" % index)
        if start < previous_end - 0.05:
            errors.append("场景 %d 与上一场景重叠" % index)
        previous_end = end
        if scene.get("pip") and not spec.get("pipVideoPath"):
            warnings.append("场景 %d 启用了 PIP 但没有 pipVideoPath，将复用主视频" % index)
    if scenes and previous_end < duration - 0.1:
        warnings.append("场景没有覆盖视频最后 %.2f 秒" % (duration - previous_end))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def build_spec(video_path, duration, captions, *, title="横版口播", eyebrow="KINETIC TALK",
               palette="blue", scenes=None, audio_path=None, pip_video_path=None):
    """Create a minimal but renderable 1920x1080 kinetic talk spec."""
    duration = _num(duration)
    if scenes is None:
        scenes = [{"start": 0, "end": duration, "layout": "intro", "accent": palette,
                   "kicker": eyebrow, "title": title, "subtitle": "",
                   "items": [], "pip": bool(pip_video_path)}]
    spec = {"width": 1920, "height": 1080, "fps": 30,
            "videoPath": video_path, "durationInSeconds": duration,
            "title": title, "eyebrow": eyebrow, "palette": palette,
            "captions": captions, "scenes": scenes}
    if audio_path:
        spec["audioPath"] = audio_path
    if pip_video_path:
        spec["pipVideoPath"] = pip_video_path
    result = validate_spec(spec)
    if not result["ok"]:
        raise ValueError("Kinetic props 校验失败: " + "; ".join(result["errors"]))
    return spec


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build HorizontalKinetic Remotion props")
    parser.add_argument("--video", required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--captions", required=True, help="lines.json: [{text,start,end}]")
    parser.add_argument("--scenes", help="optional scenes JSON")
    parser.add_argument("--out", required=True)
    parser.add_argument("--title", default="横版口播")
    parser.add_argument("--eyebrow", default="KINETIC TALK")
    parser.add_argument("--palette", default="blue", choices=sorted(ACCENTS))
    parser.add_argument("--audio")
    parser.add_argument("--pip-video")
    args = parser.parse_args(argv)
    with open(args.captions, encoding="utf-8") as handle:
        lines = json.load(handle)
    scenes = None
    if args.scenes:
        with open(args.scenes, encoding="utf-8") as handle:
            scenes = json.load(handle)
    captions = [{"start": _num(item.get("start")), "end": _num(item.get("end")),
                 "text": item.get("text", ""), "highlight": item.get("highlight", [])}
                for item in lines]
    spec = build_spec(args.video, args.duration, captions, title=args.title,
                      eyebrow=args.eyebrow, palette=args.palette, scenes=scenes,
                      audio_path=args.audio, pip_video_path=args.pip_video)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                      "validation": validate_spec(spec)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
