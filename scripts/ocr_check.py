#!/usr/bin/env python3
"""OCR 兜底检测 — 用 macOS Vision 框架对成片抽帧检验字幕残留。

出片后自动调用：抽 N 帧 → Vision OCR → 检出画面文字则返回
subtitle_detected=True，让 agent 决定是否重新生成。

依赖（macOS 10.15+）:
  pip install pyobjc-framework-Vision pyobjc-framework-Quartz

CLI:
  python3 ocr_check.py check --video output/demo.mp4
  python3 ocr_check.py check --video output/demo.mp4 --frames 5 --confidence 0.45
  python3 ocr_check.py check --video output/demo.mp4 --json
"""
import os
import sys
import json
import shutil
import argparse
import subprocess
import tempfile
import math
import re
from datetime import datetime
import media_tools


OCR_STATUSES = {"clear", "detected", "unavailable", "error"}
MIN_FRAMES = 12
MAX_FRAMES = 60


# ── ffmpeg 获取 ────────────────────────────────────────────────────────────────

def _ffmpeg_bins():
    """返回用户单独安装或显式配置的 ffmpeg/ffprobe。"""
    return media_tools.resolve_ffmpeg_pair()


# ── 帧提取 ─────────────────────────────────────────────────────────────────────

def _probe_duration(video_path, fp):
    if not fp:
        raise RuntimeError("ffprobe 不可用，无法确认视频时长和抽帧覆盖率")
    result = subprocess.run(
        [fp, "-v", "error", "-print_format", "json", "-show_format", video_path],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("无法读取视频时长: %s" % (result.stderr.strip() or "ffprobe failed"))
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("视频时长信息无效")
    if duration <= 0:
        raise RuntimeError("视频时长必须大于 0")
    return duration


def expected_frame_count(duration, requested=None, min_frames=MIN_FRAMES,
                         max_frames=MAX_FRAMES):
    """At least one frame/second, bounded by the configured review limits."""
    if min_frames < 1 or max_frames < min_frames:
        raise ValueError("抽帧范围无效: min_frames/max_frames")
    coverage = int(math.ceil(float(duration)))
    requested = int(requested) if requested is not None else 0
    return min(max(coverage, requested, int(min_frames)), int(max_frames))


def extract_frames(video_path, n=None, include_endpoints=True,
                   min_frames=MIN_FRAMES, max_frames=MAX_FRAMES,
                   return_metadata=False):
    """均匀抽帧，至少每秒一帧并覆盖首尾；任何缺帧都视为失败。"""
    ff, fp = _ffmpeg_bins()
    if not ff:
        raise RuntimeError(
            "ffmpeg 不可用。请单独安装 ffmpeg/ffprobe 并放入 PATH，"
            "或设置 BASICROUTER_FFMPEG_DIR")

    tmpdir = tempfile.mkdtemp(prefix="ocr_frames_")
    if not os.path.isfile(video_path):
        raise RuntimeError("视频文件不存在: %s" % video_path)
    duration = _probe_duration(video_path, fp)
    expected = expected_frame_count(duration, n, min_frames, max_frames)
    frames = []
    try:
        if include_endpoints:
            # Container duration may include an audio tail beyond the final
            # decodable video frame. Seeking to duration-1ms therefore fails on
            # otherwise valid clips. Keep the last sample inside a small,
            # duration-scaled tail safety window while still covering the end.
            tail_guard = max(0.05, min(0.25, duration / max(expected, 1)))
            end = max(duration - tail_guard, 0.0)
            timestamps = [end * i / max(expected - 1, 1) for i in range(expected)]
        else:
            timestamps = [duration * (i + 1) / (expected + 1) for i in range(expected)]
        for i, timestamp in enumerate(timestamps):
            out = os.path.join(tmpdir, "frame_%02d.jpg" % i)
            result = subprocess.run(
                [ff, "-hide_banner", "-y",
                 "-ss", "%.3f" % timestamp, "-i", video_path,
                 "-vf", "scale=iw:ih", "-vframes", "1", out],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                frames.append(out)
            else:
                raise RuntimeError("抽取第 %d/%d 帧失败: %s" % (
                    i + 1, expected, result.stderr.strip() or "ffmpeg 未生成图片"))
    except (OSError, RuntimeError, subprocess.SubprocessError):
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    metadata = {"duration": duration, "expected": expected,
                "include_endpoints": bool(include_endpoints)}
    return (frames, tmpdir, metadata) if return_metadata else (frames, tmpdir)


# ── macOS Vision OCR ───────────────────────────────────────────────────────────

def _vision_ocr_file(image_path):
    """调 macOS Vision VNRecognizeTextRequest，返回检测到的文字列表（含置信度）。
    每项格式：{"text": "...", "confidence": 0.0~1.0}
    """
    import objc  # noqa — pyobjc-core
    import Quartz
    import Vision

    url = Quartz.NSURL.fileURLWithPath_(image_path)
    found = []
    errors = []

    def handler(request, error):
        if error:
            errors.append(str(error))
            return
        for obs in request.results():
            cands = obs.topCandidates_(1)
            if cands:
                c = cands[0]
                found.append({"text": str(c.string()), "confidence": float(c.confidence())})

    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    # 中英混合识别
    req.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
    req.setUsesLanguageCorrection_(True)

    handler_inst = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        url, {}
    )
    result = handler_inst.performRequests_error_([req], None)
    # PyObjC may expose the Objective-C NSError as a tuple result. The callback
    # can also receive an error for asynchronous Vision failures.
    if isinstance(result, tuple) and len(result) > 1 and result[1] is not None:
        errors.append(str(result[1]))
    if errors:
        raise RuntimeError("Vision OCR 请求失败: %s" % "; ".join(errors))
    return found


# ── 主检测逻辑 ────────────────────────────────────────────────────────────────

def check_video(video_path, n_frames=None, confidence_threshold=0.45,
                include_endpoints=True, min_frames=MIN_FRAMES, max_frames=MAX_FRAMES):
    """对 video_path 抽 n_frames 帧做 OCR，返回检测报告 dict：

    {
      "subtitle_detected": bool,
      "frames_checked": int,
      "detections": [{"frame": "...", "texts": [{"text": "...", "confidence": 0.9}]}],
      "ocr_available": bool,
      "error": str or None
    }
    """
    report = {
        "status": "error",
        "available": True,
        "subtitle_detected": False,
        "frames_checked": 0,
        "expected": 0,
        "detections": [],
        "ocr_available": True,
        "error": None,
    }

    # 检查是否 macOS
    if sys.platform != "darwin":
        report["ocr_available"] = False
        report["available"] = False
        report["status"] = "unavailable"
        report["error"] = "macOS Vision OCR 仅支持 macOS"
        return report

    # 检查 pyobjc-framework-Vision
    try:
        import Vision  # noqa
    except ImportError:
        report["ocr_available"] = False
        report["available"] = False
        report["status"] = "unavailable"
        report["error"] = (
            "缺少 pyobjc-framework-Vision。"
            "运行: pip install pyobjc-framework-Vision pyobjc-framework-Quartz"
        )
        return report

    frames, tmpdir = [], None
    try:
        frames, tmpdir, metadata = extract_frames(
            video_path, n=n_frames, include_endpoints=include_endpoints,
            min_frames=min_frames, max_frames=max_frames, return_metadata=True)
        report["expected"] = metadata["expected"]
        report["duration"] = metadata["duration"]
        report["include_endpoints"] = metadata["include_endpoints"]
        report["frames_checked"] = len(frames)

        for fp in frames:
            try:
                hits = _vision_ocr_file(fp)
                confident = [h for h in hits if h["confidence"] >= confidence_threshold]
                if confident:
                    report["subtitle_detected"] = True
                    report["detections"].append({
                        "frame": os.path.basename(fp),
                        "texts": confident,
                    })
            except (OSError, ValueError, RuntimeError) as e:
                raise RuntimeError("OCR 识别失败 (%s): %s" % (os.path.basename(fp), e))
        if report["frames_checked"] != report["expected"]:
            raise RuntimeError("OCR 抽帧不完整: expected=%d checked=%d" % (
                report["expected"], report["frames_checked"]))
        report["status"] = "detected" if report["subtitle_detected"] else "clear"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        report["status"] = "error"
        report["error"] = str(e)
    finally:
        if tmpdir and os.path.isdir(tmpdir):
            import shutil as _sh
            _sh.rmtree(tmpdir, ignore_errors=True)

    return report


def manual_review(take_fingerprint, reviewer, reason, status, texts=None,
                  frame_sha256s=None):
    """Create non-transferable human OCR evidence for one exact take."""
    if status not in ("clear", "detected"):
        raise ValueError("人工 OCR 复核 status 仅允许 clear/detected")
    if not str(take_fingerprint or "").strip():
        raise ValueError("人工 OCR 复核必须绑定 take fingerprint")
    if not str(reviewer or "").strip() or not str(reason or "").strip():
        raise ValueError("人工 OCR 复核必须填写 reviewer 和 reason")
    normalized = [str(item) for item in (texts or [])]
    frames = [str(item).lower() for item in (frame_sha256s or [])]
    if (len(frames) < MIN_FRAMES or len(set(frames)) != len(frames) or
            any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in frames)):
        raise ValueError("人工 OCR 复核必须绑定至少 12 个唯一的帧 SHA-256（含首尾帧）")
    if status == "detected" and not normalized:
        raise ValueError("detected 人工复核必须记录检出的文字")
    return {"status": status, "available": True, "frames_checked": len(frames),
            "expected": len(frames), "subtitle_detected": status == "detected",
            "texts": normalized, "take_fingerprint": str(take_fingerprint),
            "reviewer": str(reviewer), "reason": str(reason),
            "frame_sha256s": frames, "first_frame_sha256": frames[0],
            "last_frame_sha256": frames[-1], "source": "manual",
            "reviewed_at": datetime.now().isoformat(timespec="seconds")}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv):
    p = argparse.ArgumentParser(description="macOS Vision OCR 字幕兜底检测")
    sub = p.add_subparsers(dest="cmd")

    ck = sub.add_parser("check", help="检测视频是否含画面文字")
    ck.add_argument("--video", required=True, help="输入 mp4 路径")
    ck.add_argument("--frames", type=int,
                    help="最低抽帧数量；实际仍至少每秒 1 帧、最少 12、最多 60")
    ck.add_argument("--min-frames", type=int, default=MIN_FRAMES)
    ck.add_argument("--max-frames", type=int, default=MAX_FRAMES)
    ck.add_argument("--no-endpoints", action="store_true", help="不强制覆盖首尾帧")
    ck.add_argument("--confidence", type=float, default=0.45,
                    help="最低置信度阈值（默认 0.45）")
    ck.add_argument("--json", action="store_true", dest="as_json",
                     help="输出 JSON（供 agent 解析）")
    manual = sub.add_parser("manual-review", help="记录绑定 take 的人工 OCR 复核")
    manual.add_argument("--take-fingerprint", required=True)
    manual.add_argument("--reviewer", required=True)
    manual.add_argument("--reason", required=True)
    manual.add_argument("--status", choices=("clear", "detected"), required=True)
    manual.add_argument("--text", action="append", default=[])
    manual.add_argument("--frame-sha256", action="append", default=[])

    args = p.parse_args(argv)
    if args.cmd == "check":
        report = check_video(args.video, n_frames=args.frames,
                             confidence_threshold=args.confidence,
                             include_endpoints=not args.no_endpoints,
                             min_frames=args.min_frames, max_frames=args.max_frames)
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False))
            return {"clear": 0, "detected": 1}.get(report["status"], 2)

        # human-readable
        if report["status"] == "unavailable":
            print("[OCR] 不可用：%s" % report["error"])
            return 2
        if report["status"] == "error":
            print("[OCR] 检测异常：%s" % report["error"])
            return 2
        if report["status"] == "detected":
            print("[OCR_WARNING] subtitle_detected — 检出画面文字，建议重新生成！")
            for d in report["detections"]:
                for t in d.get("texts", []):
                    print("  帧 %s | %.2f | %s" % (
                        d["frame"], t["confidence"], t["text"]))
            return 1
        else:
            print("[OCR] OK — 检测 %d 帧，未发现画面文字。" % report["frames_checked"])
            return 0

    if args.cmd == "manual-review":
        try:
            report = manual_review(args.take_fingerprint, args.reviewer, args.reason,
                                   args.status, args.text, args.frame_sha256)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
