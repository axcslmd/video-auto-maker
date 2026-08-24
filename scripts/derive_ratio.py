#!/usr/bin/env python3
"""Multi-ratio derive: re-layout an approved final video to different aspect ratios.

After the "final" stage is approved, this module produces additional ratio
versions (e.g. 16:9 and 1:1 from a 9:16 source) using pure ffmpeg crop/scale/overlay —
NO model calls, zero generation cost.

Strategy by source → target:
  9:16 → 16:9:  Letterbox with blurred background fill; or move speaker to right PIP
  9:16 → 1:1:   Center crop + top/bottom padding
  16:9 → 9:16:  Center crop + blurred side fill; or move speaker to bottom PIP
  16:9 → 1:1:   Center crop
  1:1 → 9:16:   Top/bottom extend with blurred fill
  1:1 → 16:9:   Left/right extend with blurred fill

The derive stage sits between "final" and "delivery" in the pipeline STAGES.
Each derived version gets its own sha256 fingerprint and media_qc check.

CLI:
  Formal: python3 scripts/pipeline.py derive --manifest <run_manifest.json> --source <approved-final.mp4> --ratios 16:9 1:1 --out-dir <run-output-dir>
  Diagnostic only: python3 derive_ratio.py derive --draft --source output/final.mp4 --target 16:9 --out output/final_16x9.mp4
"""
import os
import sys
import json
import argparse
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import proc_utils
import aspect_ratio
import media_qc
import media_tools


# ── Ratio dimensions ────────────────────────────────────────────────────
RATIO_DIMS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}


def _detect_resolution(video_path, *, ffprobe_bin=None, required=False):
    """Detect video resolution via ffprobe."""
    try:
        ffprobe_bin = ffprobe_bin or media_tools.resolve_ffmpeg_pair(allow_fetch=False)[1]
        if not ffprobe_bin:
            raise OSError("ffprobe unavailable")
        cmd = [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path]
        result = proc_utils.run_cmd(cmd, timeout=30)
        parts = result.strip().split(",")
        return (int(parts[0]), int(parts[1]))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        if required:
            raise ValueError("DERIVE_SOURCE_RESOLUTION_UNAVAILABLE: %s" % exc)
        return (1080, 1920)


def _detect_duration(video_path, *, ffprobe_bin=None, required=False):
    """Detect video duration via ffprobe."""
    try:
        ffprobe_bin = ffprobe_bin or media_tools.resolve_ffmpeg_pair(allow_fetch=False)[1]
        if not ffprobe_bin:
            raise OSError("ffprobe unavailable")
        cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", video_path]
        result = proc_utils.run_cmd(cmd, timeout=30)
        return float(result.strip())
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        if required:
            raise ValueError("DERIVE_SOURCE_DURATION_UNAVAILABLE: %s" % exc)
        return 10.0


def _file_sha256(path):
    """Compute sha256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_ratio(source_path, target_ratio, out_path, *, strategy="auto"):
    """Re-layout a final video to a different aspect ratio.

    Args:
        source_path: path to the approved final video
        target_ratio: target ratio string ("16:9", "9:16", "1:1", etc.)
        out_path: output file path
        strategy: "auto" (default), "letterbox", "crop", "pip"

    Returns:
        {"ratio": target_ratio, "path": out_path, "sha256": "...",
         "strategy": str, "source_ratio": str}
    """
    if target_ratio not in RATIO_DIMS:
        raise ValueError("UNSUPPORTED_RATIO: %s (supported: %s)" %
                         (target_ratio, ", ".join(RATIO_DIMS.keys())))
    source_path = os.path.abspath(source_path)
    out_path = os.path.abspath(out_path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError("DERIVE_SOURCE_MISSING: %s" % source_path)
    ffmpeg, ffprobe = media_tools.resolve_ffmpeg_pair(allow_fetch=False)
    if not ffmpeg or not ffprobe:
        raise RuntimeError("DERIVE_FFMPEG_UNAVAILABLE")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    src_w, src_h = _detect_resolution(source_path, ffprobe_bin=ffprobe, required=True)
    src_ratio = _detect_ratio(src_w, src_h)
    tgt_w, tgt_h = RATIO_DIMS[target_ratio]

    # Choose strategy
    if strategy == "auto":
        strategy = _choose_strategy(src_ratio, target_ratio)

    # Build ffmpeg filter
    filter_complex = _build_filter(src_w, src_h, tgt_w, tgt_h, strategy)

    cmd = [
        ffmpeg, "-y",
        "-i", source_path,
        "-filter_complex", filter_complex,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "copy",
        "-movflags", "+faststart",
        out_path,
    ]

    proc_utils.run_cmd(cmd, timeout=300)

    sha = _file_sha256(out_path)
    qc_path = os.path.abspath(out_path) + ".qc.json"
    qc = media_qc.check(out_path, profile="formal", expected_ratio=target_ratio,
                        audio_required=False, report_path=qc_path)
    media_qc.require_pass(qc)

    return {
        "ratio": target_ratio,
        "path": out_path,
        "sha256": sha,
        "strategy": strategy,
        "source_ratio": src_ratio,
        "source_resolution": [src_w, src_h],
        "target_resolution": [tgt_w, tgt_h],
        "media_qc": qc,
        "media_qc_path": qc_path,
    }


def _detect_ratio(w, h):
    """Detect ratio string from width/height."""
    if h > w * 1.5:
        return "9:16"
    elif w > h * 1.5:
        return "16:9"
    elif abs(w - h) < max(w, h) * 0.1:
        return "1:1"
    elif w > h:
        return "4:3"
    else:
        return "3:4"


def _choose_strategy(src_ratio, tgt_ratio):
    """Choose the best re-layout strategy."""
    if src_ratio == tgt_ratio:
        return "copy"

    # Portrait → Landscape: blurred fill is best for talking head
    if src_ratio == "9:16" and tgt_ratio == "16:9":
        return "blurred_fill"

    # Portrait → Square: center crop
    if src_ratio == "9:16" and tgt_ratio == "1:1":
        return "crop"

    # Landscape → Portrait: blurred fill
    if src_ratio == "16:9" and tgt_ratio == "9:16":
        return "blurred_fill"

    # Landscape → Square: center crop
    if src_ratio == "16:9" and tgt_ratio == "1:1":
        return "crop"

    # Default: blurred fill (preserves content)
    return "blurred_fill"


def _build_filter(src_w, src_h, tgt_w, tgt_h, strategy):
    """Build ffmpeg filter_complex for the re-layout."""
    if strategy == "copy":
        return "null"

    if strategy == "crop":
        # Center crop to target aspect ratio, then scale
        return (
            "crop='min(iw,ih*{tw}/{th})':'min(ih,iw*{th}/{tw})',"
            "scale={tw}:{th},setsar=1"
        ).format(tw=tgt_w, th=tgt_h)

    if strategy == "blurred_fill":
        # Scale source to fill target, crop overflow, blur as background
        return (
            # Background: scale to fill target, blur
            "[0:v]split=2[bg][fg];"
            "[bg]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            "crop={tw}:{th},boxblur=20:5[bgblur];"
            # Foreground: scale to fit inside target
            "[fg]scale={tw}:{th}:force_original_aspect_ratio=decrease[fgfit];"
            # Overlay foreground on blurred background
            "[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2"
        ).format(tw=tgt_w, th=tgt_h)

    if strategy == "letterbox":
        # Scale to fit, pad with black bars
        return (
            "scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            "pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        ).format(tw=tgt_w, th=tgt_h)

    # Fallback: blurred fill
    return _build_filter(src_w, src_h, tgt_w, tgt_h, "blurred_fill")


def derive_batch(source_path, target_ratios, out_dir, *, strategy="auto"):
    """Derive multiple ratios from a single source.

    Args:
        source_path: path to the approved final video
        target_ratios: list of ratio strings ("16:9", "1:1", etc.)
        out_dir: output directory
        strategy: override strategy for all, or "auto"

    Returns:
        {"source": path, "source_ratio": str, "derived": [result_dict, ...]}
    """
    source_path = os.path.abspath(source_path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError("DERIVE_SOURCE_MISSING: %s" % source_path)
    ffmpeg, ffprobe = media_tools.resolve_ffmpeg_pair(allow_fetch=False)
    if not ffmpeg or not ffprobe:
        raise RuntimeError("DERIVE_FFMPEG_UNAVAILABLE")
    os.makedirs(out_dir, exist_ok=True)
    src_ratio = _detect_ratio(*_detect_resolution(
        source_path, ffprobe_bin=ffprobe, required=True))
    base_name = os.path.splitext(os.path.basename(source_path))[0]

    results = []
    for ratio in target_ratios:
        if ratio == src_ratio:
            continue  # Skip same-ratio derivation

        ratio_slug = ratio.replace(":", "x")
        out_path = os.path.join(out_dir, "%s_%s.mp4" % (base_name, ratio_slug))
        result = derive_ratio(source_path, ratio, out_path, strategy=strategy)
        results.append(result)

    return {
        "source": source_path,
        "source_ratio": src_ratio,
        "derived": results,
    }


def main():
    parser = argparse.ArgumentParser(description="Multi-ratio video derivation")
    sub = parser.add_subparsers(dest="cmd")

    der = sub.add_parser("derive", help="Derive a single ratio version")
    der.add_argument("--source", required=True, help="Source final video path")
    der.add_argument("--target", required=True, choices=list(RATIO_DIMS.keys()),
                     help="Target aspect ratio")
    der.add_argument("--out", required=True, help="Output path")
    der.add_argument("--strategy", default="auto",
                     choices=["auto", "crop", "blurred_fill", "letterbox"])
    der.add_argument("--draft", action="store_true",
                     help="仅本地诊断；正式派生必须走 pipeline.py derive --manifest")

    batch = sub.add_parser("derive-batch", help="Derive multiple ratios")
    batch.add_argument("--source", required=True, help="Source final video path")
    batch.add_argument("--ratios", nargs="+", required=True,
                       choices=list(RATIO_DIMS.keys()))
    batch.add_argument("--out-dir", required=True)
    batch.add_argument("--draft", action="store_true",
                       help="仅本地诊断；正式派生必须走 pipeline.py derive --manifest")

    args = parser.parse_args()

    if args.cmd in ("derive", "derive-batch") and not args.draft:
        parser.error("直接 derive_ratio CLI 仅限显式 --draft；正式派生请使用 pipeline.py derive --manifest")

    if args.cmd == "derive":
        result = derive_ratio(args.source, args.target, args.out, strategy=args.strategy)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.cmd == "derive-batch":
        result = derive_batch(args.source, args.ratios, args.out_dir)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
