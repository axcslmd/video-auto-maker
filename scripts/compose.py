#!/usr/bin/env python3
"""Video compositing via ffmpeg: concat segments, overlay brand logo watermark.

Requires ffmpeg on PATH. Used by interview (segment concat) and as the
logo-in-motion fallback (fixed watermark overlay) mentioned in the plan.

CLI:
  # concat several clips into one (re-encode for safe joining)
  python3 compose.py concat --inputs a.mp4 b.mp4 c.mp4 --out output/final.mp4
  # overlay a logo PNG (top-right by default)
  python3 compose.py logo --input in.mp4 --logo brand/logo.png --out out.mp4 [--pos tr --scale 0.12]
"""
import os
import sys
import json
import shutil
import argparse
import subprocess
import tempfile
import media_tools


def _ffmpeg_bin():
    return media_tools.resolve_ffmpeg_pair()[0]


def _ffprobe_bin():
    return media_tools.resolve_ffmpeg_pair()[1]


def _need_ffmpeg():
    if not _ffmpeg_bin() or not _ffprobe_bin():
        raise SystemExit(
            "ERROR: 未找到外部 ffmpeg/ffprobe。请单独安装并放入 PATH，"
            "或设置 BASICROUTER_FFMPEG_DIR。")


def _run(cmd):
    if cmd and cmd[0] == "ffmpeg":
        cmd = [_ffmpeg_bin()] + cmd[1:]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise SystemExit("ffmpeg failed:\n" + p.stdout.decode("utf-8", "replace")[-1500:])


def _video_ok(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    p = subprocess.run([_ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode == 0 and "video" in p.stdout


def _atomic_target(out_path):
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=".compose-", dir=os.path.dirname(out_path) or ".")
    temporary = os.path.join(temp_dir, os.path.basename(out_path))
    return out_path, temporary


def concat(inputs, out_path, transition=None, transition_duration=0.4,
           durations=None):
    """Concatenate clips. Re-encode each to a common format for reliable joining.

    transition=None (default): hard cut via concat demuxer (fastest).
    transition="xfade": crossfade video + acrossfade audio at each boundary.
    transition_duration: seconds for crossfade overlap (default 0.4s).
    durations: optional per-input timeline durations.  When supplied, each
      clip is padded/trimmed to the exact duration before concatenation.
    """
    _need_ffmpeg()
    if durations is not None:
        if len(durations) != len(inputs):
            raise ValueError("concat durations must match inputs")
        durations = [float(value) for value in durations]
        if any(value <= 0 for value in durations):
            raise ValueError("concat durations must be positive")
    out_path, temporary = _atomic_target(out_path)
    tmpdir = tempfile.mkdtemp(prefix="compose_")
    normed = []
    try:
        for i, src in enumerate(inputs):
            if not os.path.isfile(src):
                raise SystemExit("input not found: %s" % src)
            dst = os.path.join(tmpdir, "n%02d.mp4" % i)
            probe = subprocess.run(
                [_ffprobe_bin(), "-v", "error",
                 "-select_streams", "a:0", "-show_entries", "stream=index",
                 "-of", "csv=p=0", src], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            has_audio = probe.returncode == 0 and bool(probe.stdout.strip())
            cmd = ["ffmpeg", "-y", "-i", src]
            if not has_audio:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0"]
            if durations is not None:
                duration = durations[i]
                cmd += [
                    "-vf", ("tpad=stop_mode=clone:stop_duration=%.3f,"
                            "trim=duration=%.3f,setpts=PTS-STARTPTS") %
                           (duration, duration),
                    "-af", ("apad=pad_dur=%.3f,atrim=duration=%.3f,"
                            "asetpts=PTS-STARTPTS") % (duration, duration),
                    "-t", "%.3f" % duration,
                ]
            cmd += [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
                    "-vsync", "cfr", "-r", "30", "-pix_fmt", "yuv420p", dst]
            _run(cmd)
            normed.append(dst)

        if transition == "xfade" and len(normed) > 1:
            _concat_with_xfade(normed, temporary, transition_duration)
        else:
            listfile = os.path.join(tmpdir, "list.txt")
            with open(listfile, "w") as f:
                for n in normed:
                    f.write("file '%s'\n" % n)
            _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                  "-c", "copy", temporary])
        if not _video_ok(temporary):
            raise SystemExit("ffmpeg concat did not produce valid video")
        os.replace(temporary, out_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if os.path.exists(temporary):
            os.remove(temporary)
        shutil.rmtree(os.path.dirname(temporary), ignore_errors=True)
    return out_path


def _concat_with_xfade(normed, out_path, fade_dur=0.4):
    """Concatenate clips with xfade (video crossfade) + acrossfade (audio).

    Each boundary overlaps by fade_dur seconds. Total output duration =
    sum(durations) - fade_dur * (n-1).
    """
    if len(normed) < 2:
        shutil.copy2(normed[0], out_path)
        return

    # Probe each clip's duration
    durations = []
    for path in normed:
        p = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        durations.append(float(p.stdout.strip()))

    # Build filter_complex for chained xfade + acrossfade
    # For N clips we need N-1 xfade and N-1 acrossfade filters.
    inputs = []
    for path in normed:
        inputs.extend(["-i", path])

    # Build video xfade chain: [0][1]xfade...[v01]; [v01][2]xfade...[v02]; ...
    # Build audio acrossfade chain: [0:a][1:a]acrossfade...[a01]; ...
    vfilters = []
    afilters = []
    for i in range(len(normed) - 1):
        # xfade's offset is relative to the already chained output. Using only
        # durations[i] restarts the timeline at every boundary and truncates
        # long batches (the 5-segment Momax basecut exposed this at 14.67s).
        offset = sum(durations[:i + 1]) - fade_dur * (i + 1)
        if i == 0:
            v_label = "[v%d]" % i
            a_label = "[a%d]" % i
            vfilters.append(
                "[%d:v][%d:v]xfade=transition=fade:duration=%.3f:offset=%.3f%s"
                % (i, i + 1, fade_dur, max(0, offset), v_label))
            afilters.append(
                "[%d:a][%d:a]acrossfade=d=%.3f:c1=tri:c2=tri%s"
                % (i, i + 1, fade_dur, a_label))
        else:
            prev_v = "[v%d]" % (i - 1)
            prev_a = "[a%d]" % (i - 1)
            v_label = "[v%d]" % i
            a_label = "[a%d]" % i
            vfilters.append(
                "%s[%d:v]xfade=transition=fade:duration=%.3f:offset=%.3f%s"
                % (prev_v, i + 1, fade_dur, max(0, offset), v_label))
            afilters.append(
                "%s[%d:a]acrossfade=d=%.3f:c1=tri:c2=tri%s"
                % (prev_a, i + 1, fade_dur, a_label))

    last_v = "[v%d]" % (len(normed) - 2)
    last_a = "[a%d]" % (len(normed) - 2)
    fc = ";".join(vfilters + afilters)

    cmd = (["ffmpeg", "-y"] + inputs +
           ["-filter_complex", fc,
            "-map", last_v, "-map", last_a,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-pix_fmt", "yuv420p", out_path])
    _run(cmd)


_POS = {
    "tr": "W-w-{m}:{m}", "tl": "{m}:{m}",
    "br": "W-w-{m}:H-h-{m}", "bl": "{m}:H-h-{m}",
}


def overlay_logo(input_path, logo_path, out_path, pos="tr", scale=0.12, margin=24):
    """Overlay a logo PNG. Fallback for keeping brand Logo crisp in AI-generated motion."""
    _need_ffmpeg()
    if not os.path.isfile(logo_path):
        raise SystemExit("logo not found: %s" % logo_path)
    if not 0 < scale <= 1:
        raise SystemExit("logo scale must satisfy 0 < scale <= 1")
    out_path, temporary = _atomic_target(out_path)
    xy = _POS.get(pos, _POS["tr"]).format(m=margin)
    canvas_width = _video_width(input_path)
    logo_width = max(1, round(canvas_width * scale))
    filt = ("[1:v]scale=%d:-1[lg];[0:v][lg]overlay=%s:eof_action=pass:shortest=0" %
            (logo_width, xy))
    try:
        _run(["ffmpeg", "-y", "-i", input_path, "-loop", "1", "-i", logo_path,
              "-filter_complex", filt, "-map", "0:a?", "-c:v", "libx264",
              "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", _duration(input_path), temporary])
        if not _video_ok(temporary):
            raise SystemExit("ffmpeg logo overlay did not produce valid video")
        os.replace(temporary, out_path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
        shutil.rmtree(os.path.dirname(temporary), ignore_errors=True)
    return out_path


def _duration(path):
    p = subprocess.run([_ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    try:
        duration = float(p.stdout.strip())
    except ValueError:
        duration = 0
    if p.returncode != 0 or duration <= 0:
        raise SystemExit("cannot probe main video duration: %s" % path)
    return "%.6f" % duration


def _video_width(path):
    p = subprocess.run([_ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width", "-of", "csv=p=0", path],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        width = int(p.stdout.strip())
    except ValueError:
        width = 0
    if p.returncode != 0 or width <= 0:
        raise SystemExit("cannot probe main video width: %s" % path)
    return width


def main(argv):
    p = argparse.ArgumentParser(description="ffmpeg compositing")
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("concat")
    pc.add_argument("--inputs", nargs="+", required=True)
    pc.add_argument("--out", required=True)
    pc.add_argument("--transition", default="cut", choices=["cut", "xfade"],
                    help="镜头间过渡方式：cut=硬切（默认），xfade=交叉淡化")
    pc.add_argument("--transition-duration", type=float, default=0.4,
                    help="交叉淡化时长（秒），默认 0.4")

    pl = sub.add_parser("logo")
    pl.add_argument("--input", required=True)
    pl.add_argument("--logo", required=True)
    pl.add_argument("--out", required=True)
    pl.add_argument("--pos", default="tr", choices=list(_POS.keys()))
    pl.add_argument("--scale", type=float, default=0.12)
    pl.add_argument("--margin", type=int, default=24)

    args = p.parse_args(argv)
    if args.cmd == "concat":
        out = concat(args.inputs, args.out,
                     transition=args.transition if args.transition != "cut" else None,
                     transition_duration=args.transition_duration)
        print(json.dumps({"ok": True, "out": out}))
    elif args.cmd == "logo":
        out = overlay_logo(args.input, args.logo, args.out,
                           pos=args.pos, scale=args.scale, margin=args.margin)
        print(json.dumps({"ok": True, "out": out}))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
