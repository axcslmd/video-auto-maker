#!/usr/bin/env python3
"""视频合成 —— 纯机械叠加/拼接，本地零模型。

定位铁律：本地不跑任何模型。数字人与背景的融合（抠像/换背景/人景合一）一律由
外部模型完成——首选路线 A（将场景图编译进 video_type 4/5 的最终 segment，
再用 video_engine.py --batch 生成，模型直接
生成人已在场景中的视频），或路线 C（matte.py compose 用 img2img 外部合成人景图再
驱动成视频）。**成片里数字人本就在背景中**，无需本地抠像。

本脚本只做 ffmpeg 能干的无模型活：
  1) 画中画角窗叠加（overlay）：把一段【不透明】的解说小窗叠到主画面角落（slot=corner）。
     用于「主画面是产品运镜，右下角放一段单独生成的数字人画外音小窗」这类布局。
     注意：叠的是不透明视频，不做透明抠像；要人景融合请走路线 A/C。
  2) 讲解双栏融合（split）：把 HyperFrames/Remotion 动画视频和数字人讲解视频
     合成左右或上下双栏，适合「文档动画 + 数字人」讲解型视频。
  3) 分段拼接（见 compose.py concat）：多段外部生成的成片按顺序接起来。

字幕/特效层：把成片作为 hf_engine.py 的 background.video 再叠字幕，中文不乱码。

slot 定位（画中画角窗）：
  corner 右下角小窗（默认，画外音/角落解说）
  left   左下贴边   right 右下贴边   full 底部居中放大

依赖：用户单独安装的 ffmpeg/ffprobe，无模型、无 GPU。

CLI:
  python3 fuse.py overlay --bg output/main.mp4 --human output/narrator.mp4 \
      --slot corner --out output/final.mp4 [--scale 0.9]
  python3 fuse.py split --bg output/doc_anim.mp4 --human output/host.mp4 \
      --layout side_by_side --out output/explainer.mp4
  python3 fuse.py doctor
"""
import os
import sys
import shutil
import argparse
import subprocess
import tempfile
import media_tools

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _has(cmd):
    return shutil.which(cmd) is not None


def ensure_ffmpeg_on_path():
    ok, message = media_tools.add_ffmpeg_to_path()
    if not ok:
        raise RuntimeError(message)
    return media_tools.resolve_ffmpeg_pair()[0]


def _probe_wh(ff, video):
    import json as _json
    fp = shutil.which("ffprobe")
    out = subprocess.check_output([fp, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", video]).decode()
    s = _json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def _has_audio(video):
    fp = shutil.which("ffprobe")
    result = subprocess.run(
        [fp, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=index", "-of", "csv=p=0", video],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def _probe_duration(video):
    fp = shutil.which("ffprobe")
    result = subprocess.run(
        [fp, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", video], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        duration = 0
    if result.returncode != 0 or duration <= 0:
        raise SystemExit("无法读取主视频时长: %s" % video)
    return duration


def _valid_video(path):
    fp = shutil.which("ffprobe")
    result = subprocess.run(
        [fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", path], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    return (result.returncode == 0 and "video" in result.stdout and
            os.path.isfile(path) and os.path.getsize(path) > 0)


def _slot_geometry(slot, W, H, scale):
    """返回 (human_target_w, human_target_h, overlay_x, overlay_y)。
    数字人按高度铺满 slot 区域，横向居中/靠边。"""
    if slot == "full":
        hw, hh = int(W * scale), int(H * scale)
        return hw, hh, "(W-w)/2", "H-h"          # 底部居中
    if slot == "corner":
        hh = int(H * 0.42 * scale)
        return -1, hh, "W-w-40", "H-h-40"         # 右下角
    if slot == "left":
        hh = int(H * 0.92 * scale)
        return -1, hh, "40", "H-h"                # 左下贴边
    # right（默认）
    hh = int(H * 0.92 * scale)
    return -1, hh, "W-w-40", "H-h"                # 右下贴边


def _normalize_split_layout(layout):
    value = str(layout or "side_by_side").strip().lower().replace("-", "_")
    if value in ("side_by_side", "horizontal", "left_right", "split"):
        return "side_by_side"
    if value in ("stacked", "vertical", "top_bottom"):
        return "stacked"
    raise ValueError("SPLIT_LAYOUT_UNSUPPORTED: %s" % layout)


def _split_geometry(layout, W, H, ratio=0.58, gap=24, swap=False):
    """Return target boxes for the first input (bg) and second input (human)."""
    layout = _normalize_split_layout(layout)
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        raise ValueError("SPLIT_RATIO_INVALID")
    if not 0.2 <= ratio <= 0.8:
        raise ValueError("SPLIT_RATIO_INVALID: ratio must be between 0.2 and 0.8")
    gap = max(0, int(gap or 0))
    if layout == "side_by_side":
        if gap >= W:
            raise ValueError("SPLIT_GAP_TOO_LARGE")
        first_w = max(1, int(round((W - gap) * ratio)))
        second_w = max(1, W - gap - first_w)
        first = {"w": first_w, "h": H, "x": 0, "y": 0}
        second = {"w": second_w, "h": H, "x": first_w + gap, "y": 0}
    else:
        if gap >= H:
            raise ValueError("SPLIT_GAP_TOO_LARGE")
        first_h = max(1, int(round((H - gap) * ratio)))
        second_h = max(1, H - gap - first_h)
        first = {"w": W, "h": first_h, "x": 0, "y": 0}
        second = {"w": W, "h": second_h, "x": 0, "y": first_h + gap}
    if swap:
        first, second = second, first
    return {"layout": layout, "bg": first, "human": second, "gap": gap, "ratio": ratio}


def _fit_box_filter(w, h):
    """Scale into a fixed box without cropping the source frame."""
    return (
        "scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    ) % (int(w), int(h), int(w), int(h))


def _contain_box_filter(w, h):
    """Scale into a box without cropping or padding; useful for card overlays."""
    return (
        "scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "setsar=1"
    ) % (int(w), int(h))


def _cover_box_filter(w, h):
    """Fill a box by cropping the excess after scaling."""
    return (
        "scale=%d:%d:force_original_aspect_ratio=increase:force_divisible_by=2,"
        "crop=%d:%d,setsar=1"
    ) % (int(w), int(h), int(w), int(h))


def _backplate_filter(w, h):
    """Generate a soft stage-like backplate from the first branch."""
    return (
        "scale=%d:%d:force_original_aspect_ratio=increase,"
        "crop=%d:%d,eq=brightness=-0.20:saturation=0.84,"
        "gblur=sigma=18:steps=2,format=rgba"
    ) % (int(w), int(h), int(w), int(h))


def overlay(bg, human, out_path, slot="right", scale=0.9, mix_audio=True):
    """把不透明解说小窗叠到主画面角落。

    mix_audio=True（默认）：amerge 混合主画面音轨(BGM/旁白)和数字人音轨，
      两路都保留——解决「叠数字人后 BGM 丢失」问题。
    mix_audio=False：仅保留数字人音轨（旧行为，适合主画面本无声音的情况）。
    """
    ff = ensure_ffmpeg_on_path()
    W, H = _probe_wh(ff, bg)
    tw, th, ox, oy = _slot_geometry(slot, W, H, scale)
    bg_duration = _probe_duration(bg)
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(out_path), suffix=os.path.splitext(out_path)[1],
        dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    os.unlink(temp_path)
    # Full-frame narration should fill the composition without distorting a
    # portrait source. Other slots preserve their existing height-based fit.
    scale_expr = (
        "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1"
        % (tw, th, tw, th)
    ) if slot == "full" else (
        "scale=-1:%d:force_divisible_by=2,setsar=1,drawbox=x=0:y=0:w=iw:h=ih:"
        "color=white@0.10:t=2" % th
    )
    bg_audio, human_audio = _has_audio(bg), _has_audio(human)
    if mix_audio and bg_audio and human_audio:
        fc = (
            "[1:v]%s[hum];"
            "[0:v][hum]overlay=%s:%s:format=auto:eof_action=pass:shortest=0[v];"
            "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[a]"
            % (scale_expr, ox, oy)
        )
        cmd = [ff, "-hide_banner", "-y", "-i", bg, "-i", human,
               "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-t", "%.6f" % bg_duration, temp_path]
    else:
        fc = ("[1:v]%s[hum];[0:v][hum]overlay=%s:%s:format=auto:eof_action=pass:shortest=0[v]"
              % (scale_expr, ox, oy))
        audio_map = "1:a" if human_audio else "0:a" if bg_audio else None
        cmd = [ff, "-hide_banner", "-y", "-i", bg, "-i", human,
               "-filter_complex", fc, "-map", "[v]"]
        if audio_map:
            cmd.extend(["-map", audio_map])
        cmd.extend([
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-t", "%.6f" % bg_duration, temp_path])
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise SystemExit("ffmpeg overlay 失败:\n" + p.stdout.decode("utf-8", "replace")[-1500:])
    if not _valid_video(temp_path):
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise SystemExit("ffmpeg overlay 未生成有效文件")
    os.replace(temp_path, out_path)
    import json as _json
    print(_json.dumps({"ok": True, "out": out_path, "canvas": [W, H], "slot": slot,
                       "mix_audio": mix_audio}, ensure_ascii=False))
    return out_path


def split(bg, human, out_path, layout="side_by_side", ratio=0.58, gap=24,
          swap=False, mix_audio=True):
    """Fuse an animation branch and a presenter branch into one explainer frame.

    The first input (bg) is usually the document/product animation. The second
    input (human) is usually the digital-human presenter. The two branches are
    expected to be cut to the same director-approved duration; the output uses
    the shorter duration to avoid hanging on a stale branch.
    """
    ff = ensure_ffmpeg_on_path()
    W, H = _probe_wh(ff, bg)
    bg_duration = _probe_duration(bg)
    human_duration = _probe_duration(human)
    duration = min(bg_duration, human_duration)
    if abs(bg_duration - human_duration) > 0.5:
        print("[split] WARNING: 双分支时长不一致 animation=%.3fs human=%.3fs，输出按较短时长 %.3fs 截止"
              % (bg_duration, human_duration, duration), flush=True)
    geometry = _split_geometry(layout, W, H, ratio=ratio, gap=gap, swap=swap)
    bg_box, human_box = geometry["bg"], geometry["human"]
    slot_margin = max(24, int(round(min(W, H) * 0.022)))
    bg_panel = {
        "x": max(0, bg_box["x"] + slot_margin),
        "y": max(0, bg_box["y"] + slot_margin),
        "w": max(1, bg_box["w"] - slot_margin * 2),
        "h": max(1, bg_box["h"] - slot_margin * 2),
    }
    human_panel = {
        "x": max(0, human_box["x"] + slot_margin),
        "y": max(0, human_box["y"] + slot_margin),
        "w": max(1, human_box["w"] - slot_margin * 2),
        "h": max(1, human_box["h"] - slot_margin * 2),
    }
    human_fit = (
        _cover_box_filter(human_panel["w"], human_panel["h"])
        if geometry["layout"] == "side_by_side"
        else _contain_box_filter(human_panel["w"], human_panel["h"])
    )
    bg_fit = _contain_box_filter(bg_panel["w"], bg_panel["h"])

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(out_path), suffix=os.path.splitext(out_path)[1],
        dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    os.unlink(temp_path)

    fc_video = (
        "[0:v]split=2[bgsrc][bgcopy];"
        "[bgsrc]%s[stage0];"
        "[stage0]drawbox=x=%d:y=%d:w=%d:h=%d:color=0x07101B@0.58:t=fill[stage1];"
        "[stage1]drawbox=x=%d:y=%d:w=%d:h=%d:color=0x07101B@0.52:t=fill[stage2];"
        "[bgcopy]%s[bgv];"
        "[1:v]%s[humv];"
        "[stage2][bgv]overlay=%d+(%d-overlay_w)/2:%d+(%d-overlay_h)/2:format=auto:eof_action=pass:shortest=0[tmp];"
        "[tmp][humv]overlay=%d+(%d-overlay_w)/2:%d+(%d-overlay_h)/2:format=auto:eof_action=pass:shortest=0[v];"
        "[v]drawbox=x=%d:y=%d:w=%d:h=%d:color=white@0.12:t=2[v]"
        % (
            _backplate_filter(W, H),
            bg_panel["x"], bg_panel["y"], bg_panel["w"], bg_panel["h"],
            human_panel["x"], human_panel["y"], human_panel["w"], human_panel["h"],
            bg_fit,
            human_fit,
            bg_panel["x"], bg_panel["w"], bg_panel["y"], bg_panel["h"],
            human_panel["x"], human_panel["w"], human_panel["y"], human_panel["h"],
            bg_panel["x"], bg_panel["y"], bg_panel["w"], bg_panel["h"],
        )
    )
    bg_audio, human_audio = _has_audio(bg), _has_audio(human)
    cmd = [ff, "-hide_banner", "-y", "-i", bg, "-i", human]
    if mix_audio and bg_audio and human_audio:
        fc = fc_video + ";[0:a][1:a]amix=inputs=2:duration=shortest:dropout_transition=2[a]"
        cmd.extend(["-filter_complex", fc, "-map", "[v]", "-map", "[a]"])
    else:
        cmd.extend(["-filter_complex", fc_video, "-map", "[v]"])
        audio_map = "0:a" if bg_audio else "1:a" if human_audio else None
        if audio_map:
            cmd.extend(["-map", audio_map])
    cmd.extend([
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-t", "%.6f" % duration, temp_path,
    ])
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise SystemExit("ffmpeg split 失败:\n" + p.stdout.decode("utf-8", "replace")[-1500:])
    if not _valid_video(temp_path):
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise SystemExit("ffmpeg split 未生成有效文件")
    os.replace(temp_path, out_path)
    import json as _json
    print(_json.dumps({"ok": True, "out": out_path, "canvas": [W, H],
                       "layout": geometry["layout"], "ratio": geometry["ratio"],
                       "gap": geometry["gap"], "swap": bool(swap),
                       "mix_audio": mix_audio, "duration": duration}, ensure_ascii=False))
    return out_path


def doctor():
    print("=== 合成引擎体检 ===")
    try:
        ff = ensure_ffmpeg_on_path()
        print("ffmpeg+ffprobe: OK")
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as e:
        print("ffmpeg+ffprobe: 缺失 —", e); return 1
    print("结论: READY")
    return 0


def main():
    ap = argparse.ArgumentParser(description="视频合成（画中画/讲解双栏，纯ffmpeg无模型）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("overlay", help="把不透明解说小窗叠到主画面角落（无抠像）")
    o.add_argument("--bg", required=True, help="主画面视频（Remotion 运镜/产品展示）")
    o.add_argument("--human", required=True, help="要叠的解说小窗视频（不透明，外部生成）")
    o.add_argument("--out", required=True)
    o.add_argument("--slot", default="corner", choices=["left", "right", "corner", "full"])
    o.add_argument("--scale", type=float, default=0.9)
    o.add_argument("--no-mix-audio", dest="no_mix_audio", action="store_true",
                   help="仅保留数字人音轨，丢弃主画面音轨（默认：amix 混合两路音轨）")
    s = sub.add_parser("split", help="讲解型双视频融合：动画分支 + 数字人分支")
    s.add_argument("--bg", required=True, help="动画/主内容视频（通常由 HyperFrames/Remotion 生成）")
    s.add_argument("--human", required=True, help="数字人讲解视频（不透明，外部生成）")
    s.add_argument("--out", required=True)
    s.add_argument("--layout", default="side_by_side",
                   choices=["split", "side_by_side", "side-by-side", "horizontal", "left_right",
                            "stacked", "vertical", "top_bottom"],
                   help="双栏布局：横向左右或纵向上下")
    s.add_argument("--ratio", type=float, default=0.58,
                   help="第一输入视频占主轴比例，0.2-0.8，默认 0.58")
    s.add_argument("--gap", type=int, default=24, help="两栏之间的黑色间隔像素")
    s.add_argument("--swap", action="store_true", help="交换两路视频的布局位置")
    s.add_argument("--no-mix-audio", dest="no_mix_audio", action="store_true",
                   help="不混合两路音频，仅保留第一路可用音轨")
    sub.add_parser("doctor", help="检测 ffmpeg")
    a = ap.parse_args()
    if a.cmd == "overlay":
        overlay(a.bg, a.human, a.out, a.slot, a.scale, mix_audio=not a.no_mix_audio)
    elif a.cmd == "split":
        split(a.bg, a.human, a.out, layout=a.layout, ratio=a.ratio, gap=a.gap,
              swap=a.swap, mix_audio=not a.no_mix_audio)
    elif a.cmd == "doctor":
        sys.exit(doctor())


if __name__ == "__main__":
    main()
