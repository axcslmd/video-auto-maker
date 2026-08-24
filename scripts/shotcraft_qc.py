#!/usr/bin/env python3
"""Fail-closed static and rendered-frame QC for Shotcraft packaging."""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schema_validate
import shotcraft_compile
import media_tools

TEXT_KEYS = {"text", "subtitle", "caption", "lyrics", "dialogue", "slogan", "copy"}
CARD_FIELDS = {
    "renderer", "primary_effect", "motion_speed", "requires_motion_blur",
    "transition_style", "hold_min_frames",
}
TRANSITION_STYLES = {"none", "fade", "slide", "wipe", "iris", "flip"}


def _ffmpeg_bin():
    return media_tools.resolve_ffmpeg_pair(allow_fetch=False)[0]


def _registry_errors(cards):
    errors = []
    if not 25 <= len(cards) <= 30:
        errors.append("CARD_COUNT_OUTSIDE_25_TO_30:%d" % len(cards))
    seen = set()
    renderers = set()
    effects = set()
    for card_id, card in cards.items():
        if card_id in seen:
            errors.append("DUPLICATE_CARD:%s" % card_id)
        seen.add(card_id)
        if not card.get("ratios"):
            errors.append("CARD_RATIOS_REQUIRED:%s" % card_id)
        if not card.get("source_modes"):
            errors.append("CARD_SOURCE_MODES_REQUIRED:%s" % card_id)
        if not isinstance(card.get("qa_offsets"), list) or not card.get("qa_offsets"):
            errors.append("CARD_QA_OFFSETS_REQUIRED:%s" % card_id)
        for field in sorted(CARD_FIELDS):
            if field not in card or card.get(field) in (None, ""):
                errors.append("CARD_EXECUTION_FIELD_REQUIRED:%s:%s" % (card_id, field))
        renderer = card.get("renderer")
        effect = card.get("primary_effect")
        if renderer in renderers:
            errors.append("CARD_RENDERER_NOT_UNIQUE:%s:%s" % (card_id, renderer))
        if effect in effects:
            errors.append("CARD_PRIMARY_EFFECT_NOT_UNIQUE:%s:%s" % (card_id, effect))
        renderers.add(renderer)
        effects.add(effect)
    return errors


def _contains_forbidden_text(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in TEXT_KEYS and item:
                return True
            if _contains_forbidden_text(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_text(item) for item in value)
    return False


def check(spec, *, registry_path=shotcraft_compile.REGISTRY):
    """Validate the machine-verifiable motion quality contract before render."""
    schema_validate.enforce(spec, "shotcraft-spec", context="shotcraft_qc.check")
    cards = shotcraft_compile._cards(registry_path)
    errors = _registry_errors(cards)
    token_sha = spec.get("brand_token_sha256")
    theme_sha = shotcraft_compile._canonical_sha256(spec.get("theme") or {})
    if token_sha != theme_sha:
        errors.append("BRAND_TOKEN_DIGEST_INVALID")
    shots = spec.get("shots") or []
    for index, shot in enumerate(shots):
        sid = shot.get("id")
        card = cards.get(shot.get("card_id"))
        if not card:
            errors.append("UNKNOWN_CARD:%s" % shot.get("card_id"))
            continue
        if shot.get("shotcraft_card_id") != shot.get("card_id"):
            errors.append("SHOTCRAFT_CARD_ID_MISMATCH:%s" % sid)
        if shot.get("renderer") != card.get("renderer"):
            errors.append("CARD_RENDERER_MISMATCH:%s" % sid)
        primary = shot.get("primary_effect")
        if not isinstance(primary, str) or not primary.strip():
            errors.append("ONE_PRIMARY_EFFECT_REQUIRED:%s" % sid)
        elif primary != card.get("primary_effect"):
            errors.append("PRIMARY_EFFECT_MISMATCH:%s" % sid)
        required_assets = card.get("required_assets") or []
        assets = shot.get("assets") or []
        if len(assets) < len(required_assets):
            errors.append("REQUIRED_ASSETS_MISSING:%s:%s" % (
                sid, ",".join(required_assets[len(assets):])))
        if not shot.get("qa_frames"):
            errors.append("QA_FRAMES_REQUIRED:%s" % sid)
        if any(not isinstance(value, int) or value < 0 or value >= shot["durationInFrames"]
               for value in shot.get("qa_frames") or []):
            errors.append("QA_FRAME_OUT_OF_RANGE:%s" % sid)
        hold_min = max(int(card.get("hold_min_frames") or 0),
                       int(((spec.get("theme") or {}).get("motion") or {}).get(
                           "hold_min_frames") or 15))
        hold = shot.get("hold_frames")
        if not isinstance(hold, int) or hold < hold_min or hold >= shot["durationInFrames"]:
            errors.append("SETTLED_HOLD_INVALID:%s:min=%d" % (sid, hold_min))
        transition = shot.get("transition") or {}
        style = transition.get("style")
        duration = transition.get("duration_frames")
        beat = transition.get("beat_frame")
        if style not in TRANSITION_STYLES:
            errors.append("TRANSITION_STYLE_INVALID:%s" % sid)
        if index < len(shots) - 1:
            if style == "none" or not isinstance(duration, int) or duration <= 0:
                errors.append("TRANSITION_REQUIRED:%s" % sid)
            if not isinstance(beat, int) or beat <= 0 or beat >= shot["durationInFrames"]:
                errors.append("TRANSITION_BEAT_INVALID:%s" % sid)
            elif isinstance(duration, int) and beat != shot["durationInFrames"] - duration:
                errors.append("TRANSITION_NOT_ON_BEAT:%s" % sid)
            if not transition.get("beat_source"):
                errors.append("TRANSITION_BEAT_SOURCE_REQUIRED:%s" % sid)
        elif style != "none" or duration != 0 or beat is not None:
            errors.append("FINAL_SHOT_TRANSITION_MUST_BE_NONE:%s" % sid)
        blur = shot.get("motion_blur") or {}
        if card.get("motion_speed") == "fast" and not blur.get("enabled"):
            errors.append("FAST_MOTION_BLUR_REQUIRED:%s" % sid)
        if blur.get("enabled") and (not isinstance(blur.get("samples"), int) or
                                    blur.get("samples") < 2):
            errors.append("MOTION_BLUR_SAMPLES_INVALID:%s" % sid)
        if shot.get("brand_token_sha256") != token_sha:
            errors.append("BRAND_TOKEN_MISMATCH:%s" % sid)
        if shot.get("text_ownership") != "hyperframes":
            errors.append("TEXT_OWNERSHIP_INVALID:%s" % sid)
        safe_zones = shot.get("safe_zones") or []
        provenance = shot.get("safe_zone_provenance") or {}
        if safe_zones and (provenance.get("source") != "visual_analysis" or
                           provenance.get("status") != "verified" or
                           not provenance.get("analysis_ref")):
            errors.append("SAFE_ZONE_VISUAL_ANALYSIS_REQUIRED:%s" % sid)
        if _contains_forbidden_text(shot.get("postproduction") or {}):
            errors.append("TEXT_MUST_USE_HYPERFRAMES:%s" % sid)
    return {
        "schema_version": 2,
        "kind": "shotcraft_static_qc",
        "passed": not errors,
        "errors": errors,
        "shot_count": len(shots),
        "checks": {
            "one_primary_effect": True,
            "settled_hold": True,
            "transition_on_beat": True,
            "text_owned_by_hyperframes": True,
            "brand_tokens_unified": True,
            "fast_motion_blur": True,
            "safe_zone_visual_analysis": True,
        },
    }


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "shot"))[:80]


def _frame_luma(path):
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as image:
            gray = image.convert("L")
            stats = ImageStat.Stat(gray)
            return round(float(stats.mean[0]), 4), round(float(stats.stddev[0]), 4)
    except (ImportError, OSError, ValueError):
        return None, None


def check_rendered_frames(video_path, spec, *, report_path=None, frames_dir=None):
    """Extract authored QA frames and reject missing, blank, or frozen renders."""
    static_report = check(spec)
    errors = list(static_report["errors"])
    video_path = os.path.abspath(video_path)
    if not os.path.isfile(video_path) or os.path.getsize(video_path) < 1024:
        errors.append("RENDERED_VIDEO_MISSING_OR_EMPTY")
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        errors.append("FFMPEG_REQUIRED_FOR_FRAME_QC")
    temporary = frames_dir is None
    target_dir = frames_dir or tempfile.mkdtemp(prefix="shotcraft-qa-")
    os.makedirs(target_dir, exist_ok=True)
    frame_records = []
    global_start = 0
    try:
        if not errors:
            fps = int(spec.get("fps") or 30)
            for index, shot in enumerate(spec.get("shots") or []):
                sid = _safe_id(shot.get("id"))
                for local_frame in shot.get("qa_frames") or []:
                    global_frame = global_start + int(local_frame)
                    target = os.path.join(target_dir, "%02d_%s_f%06d.png" %
                                          (index + 1, sid, global_frame))
                    command = [
                        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", "%.6f" % (global_frame / float(fps)),
                        "-i", video_path, "-frames:v", "1", target,
                    ]
                    process = subprocess.run(command, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True)
                    if process.returncode != 0 or not os.path.isfile(target):
                        errors.append("QA_FRAME_EXTRACTION_FAILED:%s:%d" %
                                      (shot.get("id"), local_frame))
                        continue
                    mean, stddev = _frame_luma(target)
                    size = os.path.getsize(target)
                    blank = bool(mean is not None and stddev is not None and
                                 (stddev < 1.0 and (mean < 2.0 or mean > 253.0)))
                    if size < 256 or blank:
                        errors.append("QA_FRAME_BLANK:%s:%d" %
                                      (shot.get("id"), local_frame))
                    frame_records.append({
                        "shot_id": shot.get("id"),
                        "local_frame": int(local_frame),
                        "global_frame": global_frame,
                        "path": os.path.abspath(target),
                        "bytes": size,
                        "sha256": _sha256(target),
                        "luma_mean": mean,
                        "luma_stddev": stddev,
                    })
                if index < len(spec.get("shots") or []) - 1:
                    transition = int((shot.get("transition") or {}).get("duration_frames") or 0)
                    global_start += int(shot["durationInFrames"]) - transition
            hashes = {item["sha256"] for item in frame_records}
            if len(frame_records) > 1 and len(hashes) < 2:
                errors.append("QA_FRAMES_ALL_IDENTICAL")
        report = {
            "schema_version": 2,
            "kind": "shotcraft_rendered_frame_qc",
            "passed": not errors,
            "errors": errors,
            "video": video_path,
            "video_sha256": _sha256(video_path) if os.path.isfile(video_path) else None,
            "frames_dir": None if temporary else os.path.abspath(target_dir),
            "frame_count": len(frame_records),
            "unique_frame_count": len({item["sha256"] for item in frame_records}),
            "frames": frame_records,
        }
        if report_path:
            os.makedirs(os.path.dirname(os.path.abspath(report_path)) or ".", exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        return report
    finally:
        if temporary:
            shutil.rmtree(target_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a Shotcraft spec and optional render")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--video")
    parser.add_argument("--frames-dir")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    with open(args.spec, encoding="utf-8") as handle:
        spec = json.load(handle)
    report = check_rendered_frames(
        args.video, spec, report_path=args.out, frames_dir=args.frames_dir) if args.video else check(spec)
    if args.out and not args.video:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
