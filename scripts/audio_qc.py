#!/usr/bin/env python3
"""Objective audio checks for master narration and final mixes."""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import media_qc


def check(path, *, report_path=None):
    ffmpeg, ffprobe = media_qc._bins()
    report = {"schema_version": 1, "file": os.path.abspath(path), "passed": False,
              "errors": [], "checks": {}}
    if not os.path.isfile(path):
        report["errors"].append("AUDIO_FILE_MISSING")
    elif not ffmpeg or not ffprobe:
        report["errors"].append("FFMPEG_UNAVAILABLE")
    else:
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-of", "json", path],
                capture_output=True, text=True)
            payload = json.loads(probe.stdout) if probe.returncode == 0 else {}
            streams = payload.get("streams") or []
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            streams = []
            report["errors"].append("AUDIO_PROBE_FAILED")
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        report["checks"]["audio_stream_count"] = len(audio)
        if len(audio) != 1:
            report["errors"].append("EXACTLY_ONE_AUDIO_TRACK_REQUIRED")
        else:
            stream = audio[0]
            try:
                sample_rate = int(stream.get("sample_rate") or 0)
            except (TypeError, ValueError):
                sample_rate = 0
            report["checks"]["sample_rate"] = sample_rate
            if sample_rate < 44100:
                report["errors"].append("AUDIO_SAMPLE_RATE_TOO_LOW")
            try:
                measure = subprocess.run(
                    [ffmpeg, "-hide_banner", "-nostdin", "-i", path, "-af", "volumedetect",
                     "-f", "null", "-"], capture_output=True, text=True)
                text = measure.stderr if measure.returncode == 0 else ""
            except OSError:
                text = ""
            import re
            max_match = re.search(r"max_volume:\s*([-0-9.]+) dB", text)
            maximum = float(max_match.group(1)) if max_match else None
            report["checks"]["max_volume_db"] = maximum
            if maximum is None:
                report["errors"].append("AUDIO_LEVEL_UNAVAILABLE")
            elif maximum > -0.1:
                report["errors"].append("AUDIO_CLIPPING_RISK")
    report["passed"] = not report["errors"]
    if report_path:
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run objective audio QC")
    parser.add_argument("--file", required=True)
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    report = check(args.file, report_path=args.report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
