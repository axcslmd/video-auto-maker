#!/usr/bin/env python3
"""Kinetic typography engine: render commercial-grade animated text/subtitles
and burn them into a video — all offline via the bundled ffmpeg (libass).

Input = a JSON scenes spec (usually written by the guided skill from the
confirmed script). Output = a video with animated text baked in.

Scenes spec (JSON):
{
  "resolution": [1080,1920], "fps": 30,
  "base": {"type": "video", "path": "output/clip.mp4"}         # or
  "base": {"type": "color", "color": "0x0B1220", "duration": 6},
  "brand": {"primary": "#E60012", "font": "Arial"},            # optional
  "scenes": [
    {"text": "65W 快充", "start": 0.0, "end": 2.5, "preset": "fade_up", "size": 96, "pos": "center"},
    {"text": "僅 320g 輕巧", "start": 2.5, "end": 5.0, "preset": "slide_left", "size": 72, "pos": "lower"},
    {"text": "隨時滿電", "start": 5.0, "end": 7.0, "preset": "typewriter", "size": 110, "pos": "center"}
  ]
}

Presets (commercial motion): fade_up, slide_left, slide_right, pop, typewriter, fade.

CLI:
  python3 text_anim.py render --spec scenes.json --out output/promo.mp4
  python3 text_anim.py preview-ass --spec scenes.json   # print the generated .ass
"""
import os
import sys
import json
import shutil
import argparse
import subprocess
import tempfile
import locale
import media_tools

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SYSTEM_CJK_FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
SYSTEM_CJK_FONTS = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _ffmpeg():
    exe, _probe = media_tools.resolve_ffmpeg_pair()
    if exe:
        return exe
    raise SystemExit(
        "ffmpeg not found. Install ffmpeg/ffprobe separately or set "
        "BASICROUTER_FFMPEG_DIR.")


def _font_family(spec):
    configured = (spec.get("brand") or {}).get("font")
    if configured and configured.lower() in {"arial", "sans", "sans-serif"}:
        configured = None
    try:
        import cjk_font
        return cjk_font.family()
    except (ImportError, OSError, RuntimeError, ValueError):
        if configured:
            return configured
        return "Hiragino Sans GB" if sys.platform == "darwin" else "Noto Sans CJK SC"


def _hex_to_ass(color):
    """#RRGGBB -> ASS &HBBGGRR (ASS uses BGR)."""
    c = color.lstrip("#")
    if len(c) != 6:
        return "&H00FFFFFF"
    r, g, b = c[0:2], c[2:4], c[4:6]
    return "&H00%s%s%s" % (b.upper(), g.upper(), r.upper())


def _t(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)


# position -> ASS \pos anchor uses alignment 5 (center); we move via \pos/\move
def _center(W, H, pos):
    x = W // 2
    y = {"center": int(H * 0.5), "upper": int(H * 0.28),
         "lower": int(H * 0.72)}.get(pos, int(H * 0.5))
    return x, y


def _anchor(W, H, sc, size):
    if not isinstance(sc, dict):
        return _center(W, H, "center")
    if any(k in sc for k in ("left_px", "right_px", "top_px", "bottom_px")):
        if "width_px" in sc:
            left = int(sc.get("left_px", 0))
            width = int(sc.get("width_px", 0))
            x = left + max(1, width // 2)
        elif "left_px" in sc and "right_px" not in sc:
            x = int(sc.get("left_px", 0)) + max(1, int(sc.get("width_px", W // 2)) // 2)
        elif "right_px" in sc and "left_px" not in sc:
            x = max(1, W - int(sc.get("right_px", 0)) - max(size, 200) // 2)
        else:
            x = W // 2
        if "top_px" in sc:
            y = int(sc.get("top_px", 0)) + max(1, int(size * 0.55))
        elif "bottom_px" in sc:
            y = max(1, H - int(sc.get("bottom_px", 0)) - max(1, int(size * 0.55)))
        else:
            y = _center(W, H, sc.get("pos", "center"))[1]
        return x, y
    return _center(W, H, sc.get("pos", "center"))


def _preset_tags(preset, W, H, x, y, dur_ms):
    """Return ASS override tags implementing the motion preset."""
    if preset == "fade_up":
        return "{\\fad(350,300)\\move(%d,%d,%d,%d)}" % (x, y + 60, x, y)
    if preset == "slide_left":
        return "{\\fad(200,250)\\move(%d,%d,%d,%d)}" % (W + 300, y, x, y)
    if preset == "slide_right":
        return "{\\fad(200,250)\\move(%d,%d,%d,%d)}" % (-300, y, x, y)
    if preset == "pop":
        return "{\\fad(120,200)\\t(0,180,\\fscx120\\fscy120)\\t(180,320,\\fscx100\\fscy100)}"
    if preset == "fade":
        return "{\\fad(350,350)}"
    # typewriter handled separately (per-char), default fade
    return "{\\fad(300,300)}"


def _build_ass(spec):
    W, H = spec.get("resolution", [1080, 1920])
    brand = spec.get("brand", {})
    font = _font_family(spec)
    primary = _hex_to_ass(brand.get("primary", "#FFFFFF"))
    fontname = font if font else "Hiragino Sans GB"
    header = [
        "[Script Info]", "ScriptType: v4.00+",
        "PlayResX: %d" % W, "PlayResY: %d" % H, "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
         "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
         "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        ("Style: Main,%s,76,%s,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,3,0,5,"
         "52,52,54,1" % (fontname, primary)),
        "",
        "[Events]",
        ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
         "Effect, Text"),
    ]
    events = []
    for sc in spec.get("scenes", []):
        text = sc["text"].replace("\n", "\\N")
        start, end = float(sc["start"]), float(sc["end"])
        size = int(sc.get("size", 80))
        pos = sc.get("pos", "center")
        preset = sc.get("preset", "fade")
        x, y = _anchor(W, H, sc, size)
        fs = "\\fs%d" % size
        layer = int(sc.get("track_index", 1))
        if layer <= 0:
            layer = 1
        if preset == "typewriter":
            # reveal char-by-char using \k-like staged \alpha per char is complex;
            # emulate with a simple per-substring reveal across the duration.
            n = max(1, len(text))
            step = (end - start) / n
            for i in range(1, n + 1):
                seg = text[:i]
                s = start + (i - 1) * step
                e = end if i == n else start + i * step + 0.02
                tag = "{\\pos(%d,%d)%s}" % (x, y, fs)
                events.append("Dialogue: %d,%s,%s,Main,,0,0,0,,%s%s"
                              % (layer, _t(s), _t(e), tag, seg))
        else:
            tags = _preset_tags(preset, W, H, x, y, int((end - start) * 1000))
            # inject font size into the tag block
            tags = tags[:-1] + fs + "}" if tags.endswith("}") else tags + "{%s}" % fs
            events.append("Dialogue: %d,%s,%s,Main,,0,0,0,,%s{\\c%s}%s"
                          % (layer, _t(start), _t(end), tags, primary, text))
    return "\n".join(header + events) + "\n"


def render(spec, out_path):
    ff = _ffmpeg()
    W, H = spec.get("resolution", [1080, 1920])
    fps = spec.get("fps", 30)
    base = spec.get("base", {"type": "color", "color": "0x0B1220", "duration": 6})
    tmp = tempfile.mkdtemp(prefix="txtanim_")
    fontsdir = os.path.join(tmp, "fonts")
    os.makedirs(fontsdir, exist_ok=True)
    for font_path in SYSTEM_CJK_FONTS:
        if os.path.isfile(font_path):
            try:
                shutil.copy2(font_path, os.path.join(fontsdir, os.path.basename(font_path)))
            except OSError:
                pass
    ass_path = os.path.join(tmp, "s.ass")
    # UTF-8 BOM makes libass/ffmpeg reliably recognize Chinese on macOS and
    # Windows even when the process locale is not UTF-8.
    with open(ass_path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(_build_ass(spec))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # escape path for the ass filter
    ass_arg = ass_path.replace("\\", "/").replace(":", "\\:")
    fontsdir_arg = fontsdir.replace("\\", "/").replace(":", "\\:")
    try:
        if base.get("type") == "video":
            src = base["path"] if os.path.isabs(base["path"]) else os.path.join(ROOT, base["path"])
            if not os.path.isfile(src):
                raise SystemExit("base video not found: %s" % src)
            cmd = [ff, "-y", "-i", src, "-vf", "ass=%s:fontsdir=%s" % (ass_arg, fontsdir_arg),
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", out_path]
        else:
            dur = base.get("duration", 6)
            color = base.get("color", "0x0B1220")
            cmd = [ff, "-y", "-f", "lavfi",
                   "-i", "color=c=%s:s=%dx%d:d=%s:r=%d" % (color, W, H, dur, fps),
                   "-vf", "ass=%s:fontsdir=%s" % (ass_arg, fontsdir_arg),
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode != 0:
            raise SystemExit("ffmpeg failed:\n" + p.stdout.decode("utf-8", "replace")[-1500:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path


def main(argv):
    p = argparse.ArgumentParser(description="kinetic typography / animated subtitles")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("render")
    pr.add_argument("--spec", required=True)
    pr.add_argument("--out", required=True)
    pa = sub.add_parser("preview-ass")
    pa.add_argument("--spec", required=True)
    args = p.parse_args(argv)

    if args.cmd in ("render", "preview-ass"):
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
    if args.cmd == "render":
        out = render(spec, args.out)
        print(json.dumps({"ok": True, "out": out}))
    elif args.cmd == "preview-ass":
        print(_build_ass(spec))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
