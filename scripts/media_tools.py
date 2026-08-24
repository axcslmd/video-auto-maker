#!/usr/bin/env python3
"""Resolve an externally managed ffmpeg/ffprobe pair.

The community source distribution never downloads or bundles FFmpeg.  Users
install FFmpeg separately and expose it through PATH or
``BASICROUTER_FFMPEG_DIR``.  This keeps FFmpeg's LGPL/GPL obligations attached
to the external tool instead of misrepresenting it as project-licensed code.
"""
import os
import shutil


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _valid_pair(ffmpeg, ffprobe):
    return bool(ffmpeg and ffprobe and os.path.isfile(ffmpeg) and os.path.isfile(ffprobe))


def configured_ffmpeg_paths():
    directory = os.environ.get("BASICROUTER_FFMPEG_DIR", "").strip()
    if not directory:
        return None, None
    suffix = ".exe" if os.name == "nt" else ""
    return _pair_in_directory(directory, suffix=suffix)


def _pair_in_directory(directory, *, suffix=""):
    ffmpeg = os.path.join(directory, "ffmpeg" + suffix)
    ffprobe = os.path.join(directory, "ffprobe" + suffix)
    return (ffmpeg, ffprobe) if _valid_pair(ffmpeg, ffprobe) else (None, None)


def system_ffmpeg_paths():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    return (ffmpeg, ffprobe) if _valid_pair(ffmpeg, ffprobe) else (None, None)


def bundled_ffmpeg_paths():
    """Bundled binaries are intentionally unavailable to community builds."""
    return None, None


def resolve_ffmpeg_pair(*, allow_fetch=False):
    # ``allow_fetch`` remains in the API for compatibility, but is deliberately
    # ignored: production code must never download an undeclared binary.
    for resolver in (configured_ffmpeg_paths, system_ffmpeg_paths):
        pair = resolver()
        if _valid_pair(*pair):
            return pair
    return None, None


def add_ffmpeg_to_path():
    ffmpeg, ffprobe = resolve_ffmpeg_pair()
    if not _valid_pair(ffmpeg, ffprobe):
        return False, (
            "未找到外部 ffmpeg/ffprobe。请单独安装并放入 PATH，"
            "或设置 BASICROUTER_FFMPEG_DIR；社区源码包不自动下载 GPL 二进制。")
    directory = os.path.dirname(ffmpeg)
    os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    return True, "已使用外部 ffmpeg/ffprobe: %s" % directory
