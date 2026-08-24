#!/usr/bin/env python3
"""Apply an auditable HyperFrames alpha AI-disclosure overlay to a final video."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hf_engine
import subtitle_overlay

DISCLOSURE_TEXT = {
    "zh-CN": "AI 生成内容", "zh-TW": "AI 生成內容", "en": "AI-generated content",
    "ja": "AI生成コンテンツ", "ko": "AI 생성 콘텐츠",
}


def _probe(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        hf_engine.ensure_ffmpeg_on_path()
        ffprobe = shutil.which("ffprobe")
    import subprocess
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=width,height:format=duration",
                             "-of", "json", path], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    return int(stream["width"]), int(stream["height"]), float((data.get("format") or {})["duration"])


def _spec(text, width, height, start, duration):
    return {"resolution": [width, height], "fps": 30, "duration": duration,
            "background": {"type": "transparent"}, "brand": {"primary": "#999999"},
            "scenes": [{"text": text, "start": start, "end": duration, "preset": "fade",
                        "size": min(32, max(20, int(width * .022))), "pos": "lower", "color": "#999999",
                        "bottom_px": 30, "right_px": 30, "left_px": int(width * .55),
                        "max_height_px": max(60, int(height * .08))}]}


def apply_disclosure(video_path, out_path, *, lang="zh-CN", enabled=True, keep_alpha=False):
    """Render a full-duration transparent alpha overlay and compose it safely."""
    if not enabled:
        shutil.copy2(video_path, out_path)
        return {"applied": False, "style": "none", "duration_ms": 0, "lang": lang, "text": "", "out_path": os.path.abspath(out_path)}
    width, height, duration = _probe(video_path)
    text = DISCLOSURE_TEXT.get(lang, DISCLOSURE_TEXT["en"])
    start = max(0.0, duration - 1.0)
    root = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(root, exist_ok=True)
    alpha_path = os.path.join(root, ".%s.disclosure.mov" % os.path.basename(out_path))
    hf_engine.render(_spec(text, width, height, start, duration), alpha_path, fmt="mov", verbose=False)
    result = subtitle_overlay.compose(video_path, alpha_path, out_path, width=width, height=height, verbose=False)
    if not result.get("ok"):
        raise ValueError("AI_DISCLOSURE_COMPOSE_FAILED: %s" % result.get("error"))
    digest = hashlib.sha256(open(alpha_path, "rb").read()).hexdigest()
    response = {"applied": True, "style": "corner-badge", "duration_ms": 1000, "lang": lang,
                "text": text, "out_path": os.path.abspath(out_path), "alpha_path": os.path.abspath(alpha_path),
                "alpha_sha256": digest}
    if not keep_alpha:
        os.remove(alpha_path)
        response.pop("alpha_path")
    return response


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply HyperFrames alpha AI disclosure")
    parser.add_argument("--video", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--lang", default="zh-CN", choices=sorted(DISCLOSURE_TEXT))
    parser.add_argument("--no-disclosure", action="store_true")
    parser.add_argument("--keep-alpha", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(apply_disclosure(args.video, args.out, lang=args.lang,
                                      enabled=not args.no_disclosure, keep_alpha=args.keep_alpha), ensure_ascii=False))


if __name__ == "__main__":
    main()
