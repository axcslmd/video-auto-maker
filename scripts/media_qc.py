#!/usr/bin/env python3
"""Structured, content-bound media quality control using ffprobe/ffmpeg."""
import argparse
import hashlib
import json
import os
import re
import shutil
import glob
import subprocess
import tempfile
from datetime import datetime, timezone
import media_tools
from project_utils import atomic_write_json


PROFILES = {
    "formal": {
        "min_short_edge": 720,
        "min_fps": 23.0,
        "max_fps": 61.0,
        "duration_tolerance_seconds": 0.75,
        "duration_tolerance_ratio": 0.15,
        "max_av_duration_delta": 0.35,
        "min_audio_sample_rate": 44100,
        "max_black_seconds": 0.75,
        "max_black_ratio": 0.20,
        "max_freeze_seconds": 1.50,
        "max_freeze_ratio": 0.40,
        "max_silence_seconds": 1.50,
        "max_silence_ratio": 0.45,
        "aspect_ratio_tolerance": 0.035,
    },
    "draft": {
        "min_short_edge": 240,
        "min_fps": 12.0,
        "max_fps": 121.0,
        "duration_tolerance_seconds": 1.50,
        "duration_tolerance_ratio": 0.30,
        "max_av_duration_delta": 0.80,
        "min_audio_sample_rate": 22050,
        "max_black_seconds": 2.00,
        "max_black_ratio": 0.50,
        "max_freeze_seconds": 3.00,
        "max_freeze_ratio": 0.70,
        "max_silence_seconds": 3.00,
        "max_silence_ratio": 0.75,
        "aspect_ratio_tolerance": 0.06,
    },
}


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bins():
    return media_tools.resolve_ffmpeg_pair(allow_fetch=False)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(value):
    if not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (AttributeError, ValueError, ZeroDivisionError):
        return _float(value)


def _ratio(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)\s*", str(value))
    return float(match.group(1)) / float(match.group(2)) if match and float(match.group(2)) else None


def _run(command):
    return subprocess.run(command, capture_output=True, text=True, errors="replace")


def _intervals(text, prefix):
    starts = [float(v) for v in re.findall(prefix + r"_start:\s*([0-9.]+)", text)]
    ends = [float(v) for v in re.findall(prefix + r"_end:\s*([0-9.]+)", text)]
    durations = [float(v) for v in re.findall(prefix + r"_duration:\s*([0-9.]+)", text)]
    return starts, ends, durations


def _metric(ffmpeg, path, filter_name, prefix, duration, has_audio=False):
    selector = "0:a:0" if has_audio else "0:v:0"
    proc = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", path, "-map", selector,
                 "-af" if has_audio else "-vf", filter_name, "-f", "null", "-"])
    starts, ends, durations = _intervals(proc.stderr, prefix)
    warnings = []
    if starts and len(ends) < len(starts):
        ends.extend([duration] * (len(starts) - len(ends)))
        warnings.append("%s_UNTERMINATED_INTERVAL" % prefix.upper())
    if not durations and starts:
        durations = [max(0.0, end - start) for start, end in zip(starts, ends)]
    total = sum(durations)
    return {"total_seconds": round(total, 4), "max_seconds": round(max(durations or [0.0]), 4),
            "ratio": round(total / duration, 6) if duration else 0.0,
            "interval_count": max(len(starts), len(durations)), "command_ok": proc.returncode == 0,
            "warnings": warnings}


def _write_json(path, value):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    atomic_write_json(path, value)


def check(path, *, profile="formal", expected_duration=None, expected_ratio=None,
          audio_required=False, report_path=None, ffmpeg_bin=None, ffprobe_bin=None):
    """Inspect one media file and return a JSON-serializable, SHA-bound report."""
    if profile not in PROFILES:
        raise ValueError("unknown media QC profile: %s" % profile)
    absolute = os.path.abspath(path)
    thresholds = dict(PROFILES[profile])
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "file": {"path": absolute, "exists": os.path.isfile(absolute)},
        "expectations": {"duration": expected_duration, "aspect_ratio": expected_ratio,
                         "audio_required": bool(audio_required)},
        "thresholds": thresholds,
        "checks": {}, "errors": [], "warnings": [], "passed": False,
    }
    if report["file"]["exists"]:
        report["file"].update({"size_bytes": os.path.getsize(absolute),
                               "sha256": file_sha256(absolute)})
    else:
        report["errors"].append("FILE_MISSING")
        if report_path:
            _write_json(report_path, report)
        return report

    ffmpeg = ffmpeg_bin
    ffprobe = ffprobe_bin
    if not ffmpeg or not ffprobe:
        detected_ffmpeg, detected_ffprobe = _bins()
        ffmpeg, ffprobe = ffmpeg or detected_ffmpeg, ffprobe or detected_ffprobe
    tools_ok = bool(ffmpeg and ffprobe)
    report["checks"]["tooling"] = {"passed": tools_ok, "ffmpeg": ffmpeg, "ffprobe": ffprobe}
    if not tools_ok:
        issue = "FFMPEG_UNAVAILABLE"
        report["errors"].append(issue)
        report["passed"] = False
        if report_path:
            _write_json(report_path, report)
        return report

    probe = _run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", absolute])
    try:
        metadata = json.loads(probe.stdout) if probe.returncode == 0 else {}
    except ValueError:
        metadata = {}
    streams = metadata.get("streams") or []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    duration = (_float((metadata.get("format") or {}).get("duration")) or
                _float(video.get("duration")) or _float(audio.get("duration")))
    width, height = video.get("width"), video.get("height")
    fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    report["media"] = {
        "duration": duration, "actual_duration": duration,
        "video": {"stream_count": len(videos), "width": width, "height": height,
                  "aspect_ratio": (float(width) / height if width and height else None), "fps": fps,
                  "codec": video.get("codec_name")},
        "audio": {"stream_count": len(audios), "sample_rate": int(audio["sample_rate"])
                  if str(audio.get("sample_rate", "")).isdigit() else None,
                  "duration": _float(audio.get("duration")), "codec": audio.get("codec_name")},
    }

    def add(name, passed, **details):
        report["checks"][name] = {"passed": bool(passed), **details}
        if not passed:
            report["errors"].append(name.upper() + "_FAILED")

    add("video_stream", bool(videos), count=len(videos))
    decode = _run([ffmpeg, "-v", "error", "-xerror", "-nostdin", "-i", absolute,
                   "-map", "0", "-f", "null", "-"]) if videos else None
    add("complete_decode", bool(decode and decode.returncode == 0),
        returncode=decode.returncode if decode else None,
        stderr=(decode.stderr[-2000:] if decode and decode.stderr else ""))
    dimensions_ok = bool(width and height and min(width, height) >= thresholds["min_short_edge"])
    add("dimensions", dimensions_ok, width=width, height=height,
        min_short_edge=thresholds["min_short_edge"])
    wanted_ratio, actual_ratio = _ratio(expected_ratio), (float(width) / height if width and height else None)
    ratio_delta = abs(actual_ratio - wanted_ratio) / wanted_ratio if actual_ratio and wanted_ratio else None
    add("aspect_ratio", wanted_ratio is None or (ratio_delta is not None and ratio_delta <= thresholds["aspect_ratio_tolerance"]),
        expected=wanted_ratio, actual=actual_ratio, relative_delta=ratio_delta)
    add("fps", bool(fps and thresholds["min_fps"] <= fps <= thresholds["max_fps"]), fps=fps)
    expected = _float(expected_duration)
    duration_delta = abs(duration - expected) if duration is not None and expected is not None else None
    tolerance = max(thresholds["duration_tolerance_seconds"],
                    expected * thresholds["duration_tolerance_ratio"]) if expected is not None else None
    add("duration", bool(duration and duration > 0 and
                         (expected is None or duration_delta <= tolerance)),
        actual=duration, expected=expected, delta=duration_delta, tolerance=tolerance)
    add("audio_track", bool(audios) or not audio_required, required=bool(audio_required), count=len(audios))
    video_duration = _float(video.get("duration")) or duration
    audio_duration = _float(audio.get("duration"))
    av_delta = abs(video_duration - audio_duration) if video_duration is not None and audio_duration is not None else None
    add("av_duration", not audios or av_delta is None or av_delta <= thresholds["max_av_duration_delta"],
        video_duration=video_duration, audio_duration=audio_duration, delta=av_delta)
    sample_rate = report["media"]["audio"]["sample_rate"]
    add("sample_rate", not audios or bool(sample_rate and sample_rate >= thresholds["min_audio_sample_rate"]),
        actual=sample_rate, minimum=thresholds["min_audio_sample_rate"])

    if videos and duration:
        black = _metric(ffmpeg, absolute, "blackdetect=d=0.10:pix_th=0.10", "black", duration)
        freeze = _metric(ffmpeg, absolute, "freezedetect=n=-60dB:d=0.50", "freeze", duration)
        add("black_frames", black["command_ok"] and
            black["max_seconds"] <= thresholds["max_black_seconds"] and
            black["ratio"] <= thresholds["max_black_ratio"], **black)
        add("freeze", freeze["command_ok"] and
            freeze["max_seconds"] <= thresholds["max_freeze_seconds"] and
            freeze["ratio"] <= thresholds["max_freeze_ratio"], **freeze)
    else:
        add("black_frames", False, reason="no decodable video duration")
        add("freeze", False, reason="no decodable video duration")
    if audios and duration:
        silence = _metric(ffmpeg, absolute, "silencedetect=n=-50dB:d=0.30", "silence", duration,
                          has_audio=True)
        add("silence", silence["command_ok"] and ((not audio_required) or
            (silence["max_seconds"] <= thresholds["max_silence_seconds"] and
             silence["ratio"] <= thresholds["max_silence_ratio"])), **silence)
    else:
        add("silence", not audio_required, reason="audio not present")

    report["passed"] = not report["errors"]
    if report_path:
        _write_json(report_path, report)
    return report


def require_pass(report):
    if not report.get("passed"):
        raise ValueError("MEDIA_QC_FAILED: %s" % ", ".join(report.get("errors") or ["unknown"]))
    record = report.get("file") or {}
    path, expected_sha = record.get("path"), record.get("sha256")
    if not path or not expected_sha or not os.path.isfile(path) or file_sha256(path) != expected_sha:
        raise ValueError("MEDIA_QC_STALE: report 与当前媒体文件 SHA-256 不一致")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="ffprobe/ffmpeg structured media QC")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("check", help="check one media file")
    command.add_argument("--file", "--video", dest="path", required=True)
    command.add_argument("--profile", choices=sorted(PROFILES), default="formal")
    command.add_argument("--expected-duration", type=float)
    command.add_argument("--expected-ratio")
    command.add_argument("--audio-required", action="store_true")
    command.add_argument("--report")
    args = parser.parse_args(argv)
    report_path = os.path.abspath(args.report) if args.report else None
    report = check(args.path, profile=args.profile, expected_duration=args.expected_duration,
                   expected_ratio=args.expected_ratio, audio_required=args.audio_required,
                   report_path=report_path)
    report["report_path"] = report_path
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
