#!/usr/bin/env python3
"""HyperFrames 字幕/动效主引擎 — HTML/CSS/GSAP → MP4。

为什么用它：浏览器渲染真实字体，中文/粤语字幕不再乱码（旧 libass 静态
二进制无 CJK 字体→豆腐块）；GSAP 缓动做商业级动效，确定性布局不跑版。
本引擎是**字幕/动态文字/motion-graphics 的唯一主引擎**；text_anim.py(libass)
仅在无 Node 环境时兜底。

依赖：包内 Node/HyperFrames CLI + 固定离线 GSAP 运行时 + ffmpeg/ffprobe
（优先包内离线二进制）+ Chrome。渲染过程不从 CDN 加载动画脚本。

输入 = 场景 JSON（通常由引导 skill 从定稿脚本生成）；输出 = 烧好动态文字的 MP4。
场景 JSON（与 text_anim.py 兼容，便于平滑迁移）：
{
  "resolution": [1080,1920], "fps": 30, "duration": 7,
  "background": {"type":"color","color":"#0B1220"}   或
  "background": {"type":"video","path":"output/clip.mp4"},
  "brand": {"primary":"#E60012"},
  "scenes": [
    {"text":"65W 快充","start":0,"end":2.5,"preset":"fade_up","size":120,"pos":"center"},
    {"text":"僅 320g・隨時滿電","start":2.5,"end":5,"preset":"slide_left","size":92,"pos":"lower"}
  ]
}
预设(GSAP缓动): fade_up / slide_left / slide_right / pop / typewriter / fade。

CLI:
  python3 hf_engine.py render --spec scenes.json --out output/promo.mp4
  python3 hf_engine.py build  --spec scenes.json --dir output/_hf   # 只生成HTML工程
  python3 hf_engine.py doctor                                        # 检测依赖
"""
import os
import sys
import json
import shutil
import glob
import argparse
import subprocess
import tempfile
import zipfile
from proc_utils import run_cmd
import re
import base64
import secrets

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GSAP_SOURCE = os.path.join(
    ROOT, "offline-assets", "hyperframes-runtime", "vendor", "gsap", "gsap-3.15.0.min.js")
MINIMAL_GSAP_RUNTIME = r"""
/*! GSAP 3.15.0 minimal offline compatibility runtime */
window.gsap = window.gsap || {
  timeline: function() {
    const steps = [];
    let currentTime = 0;
    let scale = 1;
    let proxy = null;
    const totalDuration = () => {
      let maxT = 0;
      for (const step of steps) {
        const dur = Math.max(0.001, Number((step.vars || {}).duration || 0.6));
        maxT = Math.max(maxT, Number(step.at || 0) + dur);
      }
      return maxT;
    };
    const api = {
      from: function(selector, vars, at) {
        steps.push({selector, vars: vars || {}, at: Number(at || 0)});
        const node = document.querySelector(selector);
        if (node) {
          node.style.opacity = 0;
          node.style.transform = "translate3d(0,0,0) scale(1)";
        }
        return this;
      },
      duration: function(value) {
        if (value === undefined) return totalDuration();
        scale = Number(value) || 1;
        return this;
      },
      totalDuration: function(value) {
        if (value === undefined) return totalDuration();
        scale = Number(value) || 1;
        return this;
      },
      timeScale: function(value) {
        if (value === undefined) return scale;
        scale = Number(value) || 1;
        return this;
      },
      totalTime: function(value) {
        if (value === undefined) return currentTime;
        currentTime = Number(value) || 0;
        return this;
      },
      progress: function(value) {
        if (value === undefined) {
          const dur = Math.max(0.001, totalDuration());
          return Math.max(0, Math.min(1, currentTime / dur));
        }
        currentTime = Math.max(0, Math.min(1, Number(value) || 0)) * Math.max(0.001, totalDuration());
        return this;
      },
      seek: function(t) {
        currentTime = Number(t || 0);
        t = currentTime;
        for (const step of steps) {
          const node = document.querySelector(step.selector);
          if (!node) continue;
          const v = step.vars || {};
          const dur = Math.max(0.001, Number(v.duration || 0.6));
          const p = Math.max(0, Math.min(1, (t - step.at) / dur));
          const eased = 1 - Math.pow(1 - p, 3);
          const x = (Number(v.x || 0) * (1 - eased));
          const y = (Number(v.y || 0) * (1 - eased));
          const scale = 1 + ((Number(v.scale || 1) - 1) * (1 - eased));
          node.style.opacity = String(p <= 0 ? 0 : 1);
          node.style.transform = "translate3d(" + x + "px," + y + "px,0) scale(" + scale + ")";
        }
        return this;
      },
      pause: function(){ return this; },
      play: function(){ return this; },
      reverse: function(){ return this; },
      kill: function(){ return this; },
      clear: function(){ steps.length = 0; currentTime = 0; return this; },
      add: function(){ return this; },
      eventCallback: function(){ return this; },
      getChildren: function(){ return steps.slice(); },
      getLabels: function(){ return {}; },
      getById: function(){ return null; },
      then: function(){ return Promise.resolve(this); }
    };
    proxy = new Proxy(api, {
      get: function(target, prop) {
        if (prop in target) return target[prop];
        if (typeof prop === "string") {
          return function() { return proxy; };
        }
        return undefined;
      }
    });
    return proxy;
  },
  to: function(target, vars) {
    const nodes = target && target.length !== undefined ? target : [target];
    for (const node of nodes) {
      if (!node || !node.style) continue;
      if (vars && vars.textShadow) node.style.textShadow = vars.textShadow;
    }
  }
};
"""

# 跨平台系统中文字体（@font-face local()，无需字体文件）
CJK_LOCALS = ('local("PingFang SC"), local("PingFang HK"), '
              'local("Hiragino Sans GB"), local("Microsoft YaHei"), '
              'local("Noto Sans CJK SC"), local("Source Han Sans SC"), '
              'local("WenQuanYi Zen Hei")')
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")
ALLOWED_PRESETS = {"fade_up", "slide_left", "slide_right", "pop", "typewriter", "fade",
                    "rise", "drop", "zoom_in", "bounce", "glow", "slide_up", "slide_down"}
ALLOWED_POSITIONS = {"center", "upper", "lower"}
MOTION_GRAMMAR_VERSION = 2
MOTION_LAYERS = {
    "ppt_page": {"order": 10, "owner": "remotion", "role": "content_surface"},
    "corner_pip": {"order": 20, "owner": "remotion/fuse", "role": "avatar_plate"},
    "safe_zone": {"order": 30, "owner": "remotion/fuse", "role": "layout_guard"},
    "title_area": {"order": 40, "owner": "hyperframes", "role": "hierarchy_anchor"},
    "highlight_card": {"order": 50, "owner": "hyperframes", "role": "emphasis"},
    "chart": {"order": 60, "owner": "hyperframes", "role": "chart"},
    "arrow": {"order": 61, "owner": "hyperframes", "role": "pointer_path"},
    "annotation": {"order": 62, "owner": "hyperframes", "role": "annotation"},
    "chart_arrow_annotation": {"order": 63, "owner": "hyperframes", "role": "annotation_group"},
    "subtitle": {"order": 70, "owner": "hyperframes", "role": "spoken_caption"},
    "motion_overlay": {"order": 80, "owner": "hyperframes", "role": "motion_overlay"},
    "bullet": {"order": 81, "owner": "hyperframes", "role": "supporting_point"},
    "metric": {"order": 82, "owner": "hyperframes", "role": "metric"},
    "motion_element": {"order": 90, "owner": "none", "role": "director_instruction"},
}


def _number(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("HF_SPEC_INVALID: %s 必须是数字" % label)
    value = float(value)
    if not minimum <= value <= maximum:
        raise ValueError("HF_SPEC_INVALID: %s 超出允许范围" % label)
    return value


def _hex_color(value, label):
    if not isinstance(value, str) or not HEX_COLOR_RE.fullmatch(value):
        raise ValueError("HF_SPEC_INVALID: %s 必须是 #RRGGBB 或 #RRGGBBAA" % label)
    return value


def validate_spec(spec):
    """Fail closed before untrusted values are interpolated into HTML or CSS."""
    if not isinstance(spec, dict):
        raise ValueError("HF_SPEC_INVALID: spec 必须是对象")
    resolution = spec.get("resolution", [1080, 1920])
    if not isinstance(resolution, list) or len(resolution) != 2:
        raise ValueError("HF_SPEC_INVALID: resolution 必须是 [width,height]")
    _number(resolution[0], "resolution.width", 16, 8192)
    _number(resolution[1], "resolution.height", 16, 8192)
    _number(spec.get("fps", 30), "fps", 1, 240)
    if spec.get("duration") is not None:
        _number(spec["duration"], "duration", 0.01, 86400)
    brand = spec.get("brand") or {}
    if not isinstance(brand, dict):
        raise ValueError("HF_SPEC_INVALID: brand 必须是对象")
    for key, value in brand.items():
        if "color" in key.lower() or key in {"primary", "secondary", "accent"}:
            _hex_color(value, "brand.%s" % key)
    background = spec.get("background") or {"type": "color", "color": "#0B1220"}
    if not isinstance(background, dict) or background.get("type", "color") not in {
            "color", "transparent", "video"}:
        raise ValueError("HF_SPEC_INVALID: background.type 不受支持")
    if background.get("type", "color") == "color":
        _hex_color(background.get("color", "#0B1220"), "background.color")
    scenes = spec.get("scenes", [])
    if not isinstance(scenes, list) or len(scenes) > 10000:
        raise ValueError("HF_SPEC_INVALID: scenes 必须是有限数组")
    for index, scene in enumerate(scenes):
        label = "scenes[%d]" % index
        if not isinstance(scene, dict) or not isinstance(scene.get("text"), str):
            raise ValueError("HF_SPEC_INVALID: %s.text 必须是字符串" % label)
        if len(scene["text"]) > 20000 or "\x00" in scene["text"]:
            raise ValueError("HF_SPEC_INVALID: %s.text 无效" % label)
        start = _number(scene.get("start"), label + ".start", 0, 86400)
        end = _number(scene.get("end"), label + ".end", 0, 86400)
        if end <= start:
            raise ValueError("HF_SPEC_INVALID: %s.end 必须晚于 start" % label)
        _number(scene.get("size", 84), label + ".size", 1, 2048)
        _number(scene.get("track_index", 1), label + ".track_index", 0, 1024)
        if scene.get("preset", "fade") not in ALLOWED_PRESETS:
            raise ValueError("HF_SPEC_INVALID: %s.preset 不受支持" % label)
        if scene.get("pos", "center") not in ALLOWED_POSITIONS:
            raise ValueError("HF_SPEC_INVALID: %s.pos 不受支持" % label)
        for key in ("bottom_px", "top_px", "left_px", "right_px", "width_px", "max_height_px"):
            if key in scene:
                _number(scene[key], "%s.%s" % (label, key), 0, 8192)
        if all(key in scene for key in ("left_px", "right_px", "width_px")):
            horizontal_total = (
                float(scene["left_px"]) + float(scene["width_px"]) +
                float(scene["right_px"]))
            if abs(horizontal_total - float(resolution[0])) > 4:
                raise ValueError(
                    "HF_SPEC_INVALID: %s 水平几何未闭合(left+width+right != canvas)" % label)
        for key, value in scene.items():
            if "color" in key.lower():
                _hex_color(value, "%s.%s" % (label, key))
    return spec


def _cjk_font_css():
    """Prefer a platform font known to contain Simplified Chinese glyphs."""
    try:
        import cjk_font
        chosen = cjk_font.family()
        return 'local("%s"), %s' % (chosen, CJK_LOCALS)
    except (ImportError, OSError, RuntimeError, ValueError):
        return CJK_LOCALS


def ensure_ffmpeg_on_path():
    """把用户单独管理的 ffmpeg+ffprobe 加进 PATH。"""
    import media_tools
    return media_tools.add_ffmpeg_to_path()


def _node_executable():
    bundled = os.path.join(ROOT, "offline-assets", "node-runtime", "bin")
    bundled_node = os.path.join(bundled, "node.exe" if os.name == "nt" else "node")
    if os.path.isfile(bundled_node):
        os.environ["PATH"] = bundled + os.pathsep + os.environ.get("PATH", "")
        return bundled_node
    return shutil.which("node")


def _hyperframes_cli_candidates():
    package_tail = os.path.join("hyperframes", "bin", "hyperframes.mjs")
    return [
        os.path.join(ROOT, "offline-assets", "hyperframes-runtime", "node_modules",
                     package_tail),
        os.path.join(ROOT, "offline-assets", "node-runtime", "lib", "node_modules",
                     package_tail),
    ]


def _local_hyperframes_cli():
    return next((path for path in _hyperframes_cli_candidates()
                 if os.path.isfile(path)), None)


def _has_node():
    if not _node_executable():
        return False
    return bool(_local_hyperframes_cli() or shutil.which("hyperframes") or
                shutil.which("npx"))


def ensure_browser_path():
    """Resolve a usable headless Chrome and export it for HyperFrames.

    A separately audited private runtime may provide a local browser archive;
    the community source package does not distribute it. HyperFrames needs an
    executable path during its browser probe, so system browsers remain the
    normal source-build path.
    """
    env_keys = ("HYPERFRAMES_BROWSER_PATH", "PRODUCER_HEADLESS_SHELL_PATH")
    for key in env_keys:
        value = os.environ.get(key, "").strip()
        if value and os.path.isfile(value) and os.access(value, os.X_OK):
            return True, value
    archive = os.path.join(ROOT, "offline-assets", "chrome",
                           "chrome-headless-shell-darwin-arm64.zip")
    bundle_error = None
    if os.path.isfile(archive):
        cache_root = os.path.join(ROOT, "output", ".cache", "hyperframes-chrome")
        binary = os.path.join(cache_root, "chrome-headless-shell-mac-arm64",
                              "chrome-headless-shell")
        try:
            if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
                os.makedirs(cache_root, mode=0o700, exist_ok=True)
                with zipfile.ZipFile(archive) as bundle:
                    names = bundle.namelist()
                    if any(name.startswith("/") or ".." in name.split("/") for name in names):
                        return False, "内置 Chrome 压缩包路径不安全"
                    bundle.extractall(cache_root)
                os.chmod(binary, 0o700)
            os.environ["HYPERFRAMES_BROWSER_PATH"] = binary
            return True, binary
        except (OSError, zipfile.BadZipFile) as exc:
            bundle_error = "内置 Chrome 准备失败: %s" % exc

    # Generic/online distributions may omit the pinned browser archive. In
    # that case a locally installed browser remains a valid fallback; the
    # offline distribution always takes the bundled deterministic binary.
    system_candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
    for candidate in system_candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            os.environ["HYPERFRAMES_BROWSER_PATH"] = candidate
            return True, candidate
    return False, bundle_error or "项目内置 headless Chrome 压缩包不存在"


def _npx_command(args):
    """Use an optional local HyperFrames runtime before consulting npm/npx."""
    if args and args[0] == "hyperframes":
        cli = _local_hyperframes_cli()
        node = _node_executable()
        if node and cli:
            return [node, cli] + args[1:]
        installed = shutil.which("hyperframes")
        if installed:
            return [installed] + args[1:]
        npx = shutil.which("npx")
        if npx:
            return [npx, "--yes"] + args
        missing_cli = os.path.join(HERE, "hyperframes_missing_runtime.mjs")
        if node and os.path.isfile(missing_cli):
            return [node, missing_cli] + args[1:]
    npx = shutil.which("npx") or "npx"
    return [npx, "--yes"] + args


def hyperframes_runtime_status():
    """Probe the exact CLI command rendering will use, avoiding false READY states."""
    if not _has_node():
        return False, "缺少 Node.js 或 HyperFrames CLI/npx"
    command = _npx_command(["hyperframes", "--version"])
    try:
        result = run_cmd(command, cwd=ROOT, timeout=120, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "CLI 探测失败: %s" % exc
    output = result.stdout
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    output = str(output or "").strip()
    if result.returncode != 0:
        return False, output[-400:] or "CLI 返回非零状态"
    source = "local-runtime" if _local_hyperframes_cli() else (
        "installed" if shutil.which("hyperframes") else "npx")
    version = output.splitlines()[-1] if output else "version unknown"
    return True, "%s, %s" % (source, version)


def _pos_top(H, pos, size):
    """把 center/upper/lower 语义位置换成 top 像素（竖排文字大致居中）。"""
    frac = {"center": 0.5, "upper": 0.28, "lower": 0.72}.get(pos, 0.5)
    return int(H * frac - size * 0.6)


def _scene_position_css(sc, W, H, size):
    """把单条字幕的定位换成绝对定位 CSS 字符串。

    优先用视觉模型推荐的**精确像素**（bottom_px/left_px/right_px/max_height_px）——
    绕过 pos 三档(upper/center/lower)太粗的限制，字幕落在画面安全区。
    没给精确像素时回退旧的 pos 语义档位（top 百分比定位）。
    """
    has_precise = any(k in sc for k in ("bottom_px", "top_px", "left_px", "right_px"))
    if has_precise:
        css = ["position:absolute", "text-align:center"]
        if "bottom_px" in sc:
            css.append("bottom:%dpx" % int(sc["bottom_px"]))
        if "top_px" in sc:
            css.append("top:%dpx" % int(sc["top_px"]))
        # 左右边距默认给个安全内缩；模型给了就用模型的
        if "width_px" in sc:
            width_px = int(sc["width_px"])
            if "left_px" in sc:
                left_px = int(sc["left_px"])
            elif "right_px" in sc:
                left_px = max(0, W - int(sc["right_px"]) - width_px)
            else:
                left_px = max(0, int(round((W - width_px) / 2.0)))
            css.append("left:%dpx" % left_px)
            css.append("width:%dpx" % width_px)
        else:
            css.append("left:%dpx" % int(sc.get("left_px", 40)))
            css.append("right:%dpx" % int(sc.get("right_px", 40)))
            # 未显式给 left/right 时，width 交给 left/right 约束；不再钉死整宽
            css.append("width:auto")
        if "max_height_px" in sc:
            css.append("max-height:%dpx" % int(sc["max_height_px"]))
            css.append("overflow:hidden")
        return ";".join(css) + ";"
    # 回退：旧 pos 语义档位（整宽 + top 百分比）
    top = _pos_top(H, sc.get("pos", "center"), size)
    return "left:0;width:%dpx;text-align:center;top:%dpx;" % (W, top)


def _subtitle_visual_units(value):
    units = 0.0
    for char in str(value or ""):
        if char.isspace():
            units += 0.42
        elif char.isascii() and (char.isalnum() or char in "_.-"):
            units += 0.56
        else:
            units += 1.0
    return units


def _balance_subtitle_lines(value, width_px, size_px, max_lines=2):
    """Insert a deterministic semantic break instead of browser orphan wrap."""
    # Treat author line breaks as advisory.  A copied line break before Chinese
    # punctuation is illegal typography and must not survive into the renderer.
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace(" ", "") if not re.search(r"[A-Za-z0-9] [A-Za-z0-9]", text) else text
    if max_lines < 2 or not text:
        return text
    capacity = max(8.0, float(width_px or 0) / max(1.0, float(size_px or 1) * 1.03))
    if _subtitle_visual_units(text) <= capacity:
        return text
    tokens = text.split(" ")
    if len(tokens) < 2:
        forbidden_line_head = set("、，。；：！？）】》〉」』〕］｝…")
        forbidden_line_tail = set("（【《〈「『〔［｛")
        # Production vocabulary that must remain a semantic unit.  This is a
        # deterministic renderer glossary, not an LLM guess, and can be safely
        # extended without changing approved copy.
        unbreakable_phrases = (
            "分镜", "脚本", "关键素材", "下一步", "数字人", "故事板",
            "产品图", "使用图", "客户确认", "渲染质量", "BasicRouter",
        )

        def splits_unbreakable_phrase(index):
            return any(
                start >= 0 and start < index < start + len(phrase)
                for phrase in unbreakable_phrases
                for start in (text.find(phrase),)
            )

        candidates = []
        for index in range(2, len(text) - 1):
            if (
                text[index] in forbidden_line_head
                or text[index - 1] in forbidden_line_tail
                or splits_unbreakable_phrase(index)
            ):
                continue
            left, right = text[:index].rstrip(), text[index:].lstrip()
            left_units = _subtitle_visual_units(left)
            right_units = _subtitle_visual_units(right)
            orphan_penalty = 100.0 if min(left_units, right_units) < 2.5 else 0.0
            overflow_penalty = max(0.0, left_units - capacity) + max(0.0, right_units - capacity)
            score = abs(left_units - right_units) + overflow_penalty * 5.0 + orphan_penalty
            candidates.append((score, left, right))
        if not candidates:
            return text
        _, left, right = min(candidates, key=lambda item: item[0])
        return left + "\n" + right
    candidates = []
    for index in range(1, len(tokens)):
        left = " ".join(tokens[:index])
        right = " ".join(tokens[index:])
        left_units = _subtitle_visual_units(left)
        right_units = _subtitle_visual_units(right)
        # Prefer balanced lines and strongly penalize a visually tiny orphan.
        orphan_penalty = 100.0 if min(left_units, right_units) < 2.5 else 0.0
        overflow_penalty = max(0.0, left_units - capacity) + max(0.0, right_units - capacity)
        score = abs(left_units - right_units) + overflow_penalty * 5.0 + orphan_penalty
        candidates.append((score, left, right))
    _, left, right = min(candidates, key=lambda item: item[0])
    return left + "\n" + right


def _gsap_from(preset):
    """预设 → GSAP from() 参数（入场动效，缓动是商业级手感的关键）。"""
    table = {
        "fade_up":     '{ opacity:0, y:70, duration:0.7, ease:"power3.out" }',
        "slide_left":  '{ opacity:0, x:-160, duration:0.7, ease:"power3.out" }',
        "slide_right": '{ opacity:0, x:160, duration:0.7, ease:"power3.out" }',
        "pop":         '{ opacity:0, scale:0.5, duration:0.6, ease:"back.out(2.0)" }',
        "fade":        '{ opacity:0, duration:0.6, ease:"power2.out" }',
        "typewriter":  '{ opacity:0, duration:0.3, ease:"none" }',
        # 新增预设
        "rise":        '{ opacity:0, y:100, scale:0.9, duration:0.8, ease:"power4.out" }',
        "drop":        '{ opacity:0, y:-80, scale:0.95, duration:0.7, ease:"power3.out" }',
        "zoom_in":     '{ opacity:0, scale:0.3, duration:0.6, ease:"back.out(1.5)" }',
        "bounce":      '{ opacity:0, scale:0.4, y:60, duration:0.8, ease:"elastic.out(1, 0.5)" }',
        "glow":        '{ opacity:0, scale:0.8, duration:0.8, ease:"power2.out", '
                       'onComplete: function() { gsap.to(this.targets()[0], '
                       '{ textShadow: "0 0 20px rgba(255,255,255,0.5), 0 0 40px rgba(255,255,255,0.2)", '
                       'duration: 0.4, yoyo: true, repeat: 1 }); } }',
        "slide_up":    '{ opacity:0, y:50, duration:0.6, ease:"power2.out" }',
        "slide_down":  '{ opacity:0, y:-50, duration:0.6, ease:"power2.out" }',
    }
    return table.get(preset, table["fade"])


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))



def _motion_safe_zone_position(safe_zone):
    return {
        "lower_third": "lower",
        "upper_third": "upper",
        "center": "center",
        "corner": "upper",
        "left": "center",
        "right": "center",
    }.get(str(safe_zone or ""), "center")


def _motion_preset(plan, layer="primary"):
    if not isinstance(plan, dict):
        return "fade_up"
    density = str(plan.get("density") or "medium").lower()
    personality = str(plan.get("personality") or "balanced").lower()
    if layer == "ambient":
        return "fade"
    if personality == "energetic" or density == "high":
        return "pop" if layer == "primary" else "slide_up"
    if personality == "calm" or density == "low":
        return "fade_up" if layer == "primary" else "fade"
    return "fade_up" if layer == "primary" else "slide_left"


def _timing_window(slot, start, end, index=0):
    duration = max(0.2, float(end) - float(start))
    raw = str((slot or {}).get("timing") or "").lower()
    if "spoken" in raw or "全程" in raw:
        return float(start), float(end)
    if "0.5" in raw or "进场" in raw:
        st = float(start) + min(0.5, duration * 0.25)
        return st, min(float(end), st + max(0.8, duration * 0.58))
    if "结尾" in raw or "end" in raw:
        st = float(end) - max(0.8, duration * 0.35)
        return max(float(start), st), float(end)
    offset = min(duration * 0.18, index * 0.18)
    return float(start) + offset, float(end)


def _slot_kind(slot):
    slot_name = str((slot or {}).get("slot") or "").strip().lower()
    if slot_name.startswith("motion_overlay_bullet_"):
        return "bullet"
    if slot_name.startswith("motion_element_"):
        return "motion_element"
    if slot_name in MOTION_LAYERS:
        return slot_name
    return str((slot or {}).get("kind") or slot_name or "motion_element").strip().lower()


def _slot_source_kind(slot, kind):
    explicit = str((slot or {}).get("source_kind") or "").strip().lower()
    if explicit in {"audience_copy", "director_instruction", "layout_reservation"}:
        return explicit
    if kind == "motion_element":
        return "director_instruction"
    if kind in {"subtitle", "motion_overlay", "bullet", "metric", "title_area",
                "highlight_card", "chart", "arrow", "annotation"}:
        return "audience_copy"
    return "layout_reservation"


def _slot_text(slot):
    if not isinstance(slot, dict):
        return ""
    return str(slot.get("text") or slot.get("description") or slot.get("label") or "").strip()


def _slot_preset(slot, motion_plan, layer):
    if isinstance(slot, dict) and slot.get("preset") in ALLOWED_PRESETS:
        return slot["preset"]
    kind = _slot_kind(slot)
    defaults = {
        "title_area": "fade_up", "highlight_card": "pop", "chart": "slide_left",
        "arrow": "slide_right", "annotation": "fade_up", "subtitle": "fade_up",
        "motion_overlay": "pop", "bullet": "slide_left", "metric": "pop",
    }
    return defaults.get(kind) or motion_plan.get("preset") or _motion_preset(motion_plan, layer)


def compile_motion_grammar(motion_plan, *, start=0.0, end=None):
    """Compile a design-board MotionPlan into a semantic HyperFrames grammar.

    This is intentionally separate from the legacy ``scenes`` list.  The
    latter remains the stable text+preset renderer backend; ``tracks`` and
    ``layers`` preserve ownership, hierarchy, timing and accessibility so a
    richer HyperFrames/GSAP implementation can consume the same contract.
    """
    if not isinstance(motion_plan, dict):
        raise ValueError("HF_MOTION_PLAN_INVALID: motion_plan 必须是对象")
    start = float(start)
    end = float(end if end is not None else start + float(motion_plan.get("duration") or 6))
    if end <= start:
        raise ValueError("HF_MOTION_PLAN_INVALID: end 必须晚于 start")
    personality = str(motion_plan.get("personality") or "balanced")
    density = str(motion_plan.get("density") or "medium")
    a11y = dict(motion_plan.get("a11y") or {})
    identity = dict(motion_plan.get("motion_identity") or {})
    choreography = dict(motion_plan.get("choreography") or {})
    execution_plan = dict(motion_plan.get("execution_plan") or {})
    execution_tracks = {
        str(track.get("target")): track for track in (execution_plan.get("tracks") or [])
        if isinstance(track, dict) and track.get("target")
    }
    slots = motion_plan.get("asset_slots") or []
    elements = []
    for index, raw in enumerate(slots):
        if not isinstance(raw, dict):
            continue
        kind = _slot_kind(raw)
        layer_def = MOTION_LAYERS.get(kind, MOTION_LAYERS["motion_element"])
        source_kind = _slot_source_kind(raw, kind)
        enabled = raw.get("enabled", True)
        if not enabled:
            continue
        layer = str(raw.get("layer") or layer_def["owner"])
        track = str(raw.get("track") or raw.get("track_name") or layer_def["role"])
        st, en = _timing_window(raw, start, end, index=index)
        intensity = raw.get("intensity") or (
            motion_plan.get("primary") if kind in ("title_area", "highlight_card", "metric")
            else motion_plan.get("secondary") if kind in ("chart", "arrow", "annotation", "bullet")
            else motion_plan.get("ambient"))
        elements.append({
            "id": str(raw.get("id") or raw.get("slot") or "element_%02d" % (index + 1)),
            "slot": str(raw.get("slot") or kind),
            "kind": kind,
            "role": raw.get("role") or layer_def["role"],
            "owner": raw.get("owner") or layer_def["owner"],
            "source_kind": source_kind,
            "semantic_id": str(raw.get("semantic_id") or raw.get("id") or
                               raw.get("slot") or "element_%02d" % (index + 1)),
            "collision_policy": str(raw.get("collision_policy") or "reserved_regions"),
            "exclusive_owner": bool(raw.get("exclusive_owner", False)),
            "layer": layer,
            "track": track,
            "order": int(raw.get("order") or layer_def["order"]),
            "text": _slot_text(raw),
            "safe_zone": raw.get("safe_zone") or motion_plan.get("safe_zone"),
            "timing": {
                "start": round(st, 3), "end": round(en, 3),
                "enter": raw.get("enter") or "at_start",
                "hold": raw.get("hold") or "until_exit",
                "exit": raw.get("exit") or "fade_out",
            },
            "motion": {
                "personality": personality,
                "intensity": intensity or "medium",
                "preset": _slot_preset(raw, motion_plan, "primary" if index == 0 else "secondary"),
                "easing": raw.get("easing") or (
                    "power3.out" if personality != "energetic" else "back.out(1.6)"),
            },
            "a11y": {
                "contrast": raw.get("contrast") or a11y.get("contrast") or "high",
                "max_lines": raw.get("max_lines") or a11y.get("max_lines") or 2,
                "subtitle_required": bool(raw.get("subtitle_required", a11y.get("subtitle_required", False))),
            },
            "source": raw.get("source") or raw.get("ref_tags") or None,
            "asset_binding": raw.get("asset_binding") or {},
            "typography": dict(raw.get("typography") or {}),
            "size_px": raw.get("size_px"),
            "max_width_px": raw.get("max_width_px") or raw.get("width_px"),
            "max_lines": raw.get("max_lines"),
            "execution": raw.get("execution") or {},
            "renderable": bool(
                raw.get("renderable", kind not in ("ppt_page", "corner_pip", "safe_zone")) and
                source_kind == "audience_copy" and
                str(raw.get("owner") or layer_def["owner"]) == "hyperframes"),
        })
        if elements[-1]["execution"] == {}:
            target = {
                "ppt_page": "content_stage", "corner_pip": "presenter_plate",
                "title_area": "foreground_graphics", "highlight_card": "foreground_graphics",
                "chart": "foreground_graphics", "arrow": "foreground_graphics",
                "annotation": "foreground_graphics", "subtitle": "foreground_graphics",
                "motion_overlay": "foreground_graphics",
            }.get(kind, "foreground_graphics")
            elements[-1]["execution"] = execution_tracks.get(target) or {}
    elements.sort(key=lambda item: (item["order"], item["timing"]["start"], item["id"]))
    return {
        "schema_version": MOTION_GRAMMAR_VERSION,
        "source_schema_version": motion_plan.get("schema_version"),
        "motion_contract_version": motion_plan.get("motion_contract_version"),
        "compiler": "hf_engine.semantic_motion_compiler",
        "personality": personality,
        "motion_identity": identity,
        "choreography": choreography,
        "execution_plan": execution_plan,
        "intensity": {
            "primary": motion_plan.get("primary") or "medium",
            "secondary": motion_plan.get("secondary") or "medium",
            "ambient": motion_plan.get("ambient") or "low",
        },
        "density": density,
        "timing": motion_plan.get("timing") or "steady",
        "safe_zone": motion_plan.get("safe_zone") or "center",
        "safe_zones": motion_plan.get("safe_zones") or [motion_plan.get("safe_zone") or "center"],
        "a11y": a11y,
        "layer_order": [item["kind"] for item in sorted(
            ({"kind": key, **value} for key, value in MOTION_LAYERS.items()),
            key=lambda item: item["order"])],
        "elements": elements,
    }


def compile_motion_plan_to_spec(motion_plan, *, resolution=None, fps=30, brand=None,
                                background=None, start=0.0, end=None):
    """Compile structured MotionPlan into the legacy HyperFrames scene spec.

    MotionPlan is the semantic layer produced by the design_board/render
    contract. This compiler keeps hf_engine's proven text+preset renderer as
    the low-level backend while giving upstream code a richer motion grammar.
    """
    if not isinstance(motion_plan, dict):
        raise ValueError("HF_MOTION_PLAN_INVALID: motion_plan 必须是对象")
    resolution = resolution or [1080, 1920]
    duration = float(end if end is not None else motion_plan.get("duration") or 6)
    start = float(start)
    end = float(end if end is not None else start + duration)
    grammar = compile_motion_grammar(motion_plan, start=start, end=end)
    execution_plan = motion_plan.get("execution_plan") or {}
    graphics_track = next((track for track in execution_plan.get("tracks") or []
                           if isinstance(track, dict) and track.get("target") == "foreground_graphics"), {})
    scenes = []
    for index, element in enumerate(grammar["elements"]):
        text = element["text"]
        if (not text or not element.get("renderable") or
                element.get("source_kind") != "audience_copy" or
                element.get("owner") != "hyperframes"):
            continue
        kind = element["kind"]
        layer = "primary" if index == 0 else "secondary"
        st = element["timing"]["start"]
        en = element["timing"]["end"]
        safe_zone = element.get("safe_zone") or motion_plan.get("safe_zone")
        scene = {
            "text": text,
            "start": round(st, 3),
            "end": round(en, 3),
            "preset": element["motion"]["preset"],
            "pos": _motion_safe_zone_position(safe_zone),
            "safe_zone": safe_zone,
            # A design board may intentionally choreograph several graphics
            # at once.  Give each authored element its own deterministic lane
            # so HyperFrames can represent overlap without lint conflicts.
            "track_index": 1 if kind == "subtitle" else 2 + index,
            "variant": (
                "spoken" if kind == "subtitle" else
                "title" if element.get("slot") == "title_area" else "feature"),
            "contrast_backplate": "dark_glass" if kind == "subtitle" else None,
            "size": int(
                (element.get("typography") or {}).get("size_px") or
                element.get("size_px") or (44 if kind == "subtitle" else 52)),
            "semantic_role": element["role"],
            "semantic_id": element["semantic_id"],
            "source_kind": element["source_kind"],
            "owner": element["owner"],
            "collision_policy": element["collision_policy"],
            "exclusive_owner": element["exclusive_owner"],
            "slot": element["slot"],
            "layer": element["layer"],
            "easing": element["motion"]["easing"],
            "execution": graphics_track,
            "base_plate_contract": execution_plan.get("base_plate_contract") or {},
        }
        typography = element.get("typography") or {}
        width_px = typography.get("max_width_px") or element.get("max_width_px")
        max_lines = typography.get("max_lines") or element.get("max_lines") or element.get("a11y", {}).get("max_lines")
        if width_px:
            scene["width_px"] = int(width_px)
        if max_lines:
            scene["max_height_lines"] = max(1, int(max_lines))
            scene["max_height_px"] = int(
                scene["size"] * 1.32 * scene["max_height_lines"] +
                (22 if kind == "subtitle" else 0))
        if kind == "subtitle" and scene.get("width_px"):
            scene["text"] = _balance_subtitle_lines(
                scene["text"], scene["width_px"], scene["size"],
                scene.get("max_height_lines") or 2)
            scene["line_break_strategy"] = "balanced_semantic"
        if safe_zone == "corner":
            scene.update({"top_px": 92, "right_px": 74, "width_px": 460})
        elif safe_zone == "left":
            scene.update({"left_px": 86, "top_px": 360, "width_px": 560})
        elif safe_zone == "right":
            scene.update({"left_px": max(80, int(resolution[0] * 0.54)), "top_px": 360, "width_px": 560})
        scenes.append(scene)
    return {
        "resolution": resolution,
        "fps": fps,
        "duration": round(end, 3),
        "background": background or {"type": "transparent"},
        "brand": brand or {},
        "motion_plan_schema_version": motion_plan.get("schema_version"),
        "motion_contract_version": motion_plan.get("motion_contract_version"),
        "motion_grammar_version": MOTION_GRAMMAR_VERSION,
        "semantic_compiler": "hf_engine.semantic_motion_compiler",
        "motion_grammar": grammar,
        "scenes": scenes,
    }

def build_html(spec):
    """场景 JSON → HyperFrames composition HTML 字符串（含 CJK 字体 + GSAP 时间线）。"""
    validate_spec(spec)
    W, H = spec.get("resolution", [1080, 1920])
    fps = spec.get("fps", 30)
    scenes = spec.get("scenes", [])
    duration = spec.get("duration") or (max((float(s["end"]) for s in scenes), default=6))
    brand = spec.get("brand", {})
    # Missing brand data must not inject a demo customer's red into production.
    # Neutral white is the renderer fallback; formal plans bind an approved
    # brand.primary value through the motion/style contracts.
    primary = brand.get("primary", "#FFFFFF")
    bg = spec.get("background", {"type": "color", "color": "#0B1220"})
    bg_type = bg.get("type")
    # transparent：字幕层专用，配合 --format mov/webm 出 alpha 通道，overlay 叠回成片
    if bg_type == "transparent":
        bg_css = "background:transparent;"
    elif bg_type == "color":
        bg_css = "background:%s;" % bg.get("color", "#0B1220")
    else:
        bg_css = "background:#000;"

    nonce = base64.b64encode(secrets.token_bytes(18)).decode("ascii")
    # HyperFrames injects a local frame-capture bootstrap script at render time.
    # A nonce on the authored scripts cannot authorize that injected script, so
    # this offline page explicitly allows inline scripts while keeping network,
    # object, frame, and external media sources disabled.
    csp = ("default-src &#39;none&#39;; script-src &#39;self&#39; &#39;unsafe-inline&#39;; "
           "worker-src &#39;self&#39; blob:; "
           "style-src &#39;unsafe-inline&#39;; connect-src &#39;none&#39;; "
           "img-src &#39;none&#39;; media-src &#39;none&#39;; font-src &#39;self&#39;; "
           "object-src &#39;none&#39;; frame-src &#39;none&#39;; base-uri &#39;none&#39;; "
           "form-action &#39;none&#39;")
    L = [
        '<!doctype html>', '<html lang="zh-Hant">', '<head>',
        '<meta charset="UTF-8" />',
         '<meta http-equiv="Content-Security-Policy" content="%s" />' % csp,
        '<meta name="viewport" content="width=%d, height=%d" />' % (W, H),
         '<script nonce="%s" src="gsap.min.js"></script>' % nonce,
        '<style>',
        '@font-face{font-family:"CJK";src:%s;}' % _cjk_font_css(),
        '*{margin:0;padding:0;box-sizing:border-box;}',
        'html,body{width:%dpx;height:%dpx;overflow:hidden;%sfont-family:"CJK",sans-serif;}' % (W, H, bg_css),
        '.cap{position:absolute;color:#fff;'
        'font-weight:700;letter-spacing:0;line-height:1.32;'
        'text-shadow:0 0 1px rgba(0,0,0,0.9),0 2px 4px rgba(0,0,0,0.6),0 4px 16px rgba(0,0,0,0.4);'
        'padding:8px 0;border-radius:0;'
        'background:transparent;border:0;'
        'box-shadow:none;'
        'white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;'
        'display:block;}',
        '.spoken{background:linear-gradient(135deg,rgba(3,8,18,.88),rgba(15,23,42,.80));'
        'border:1px solid rgba(148,163,184,.32);box-shadow:0 12px 32px rgba(0,0,0,.36),inset 0 1px 0 rgba(255,255,255,.08);'
        'padding:9px 18px;border-radius:16px;backdrop-filter:blur(8px);'
        'text-shadow:0 2px 4px rgba(0,0,0,.86),0 4px 18px rgba(0,0,0,.62);}',
        '.feature{background:linear-gradient(135deg,rgba(255,255,255,0.95),rgba(223,235,255,0.92));'
        'color:#111827;font-weight:800;padding:12px 22px;border:1px solid rgba(70,122,210,0.58);'
        'box-shadow:0 10px 24px rgba(25,54,100,0.22);border-radius:8px;'
        'text-shadow:0 1px 0 rgba(255,255,255,0.55);}',
        '.title{background:linear-gradient(135deg,rgba(7,13,25,.88),rgba(17,28,46,.80));'
        'color:#f8fafc;font-weight:850;padding:14px 22px;border:1px solid rgba(148,163,184,.42);'
        'border-left:4px solid %s;box-shadow:0 14px 34px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.10);'
        'border-radius:14px;text-shadow:0 3px 16px rgba(0,0,0,.72);}' % primary,
        '.brand-lockup{background:rgba(8,12,24,0.82);font-weight:800;'
        'padding:14px 26px;border:1px solid rgba(244,181,68,0.8);}',
        '.feature::before{content:"";position:absolute;left:10px;top:8px;bottom:8px;width:4px;'
        'border-radius:999px;background:%s;opacity:.9;}' % primary,
        '.accent{color:%s;font-weight:800;}' % primary,
        '</style>', '</head>', '<body>',
        '<div id="root" data-composition-id="main" data-start="0" data-duration="%s" '
        'data-width="%d" data-height="%d" data-fps="%d">' % (duration, W, H, fps),
    ]
    tl = []  # GSAP timeline lines
    for i, sc in enumerate(scenes):
        sid = "s%d" % i
        start, end = float(sc["start"]), float(sc["end"])
        size = int(sc.get("size", 84))
        pos_css = _scene_position_css(sc, W, H, size)
        preset = sc.get("preset", "fade")
        text = _esc(sc["text"]).replace("[[", '<span class="accent">').replace("]]", "</span>")
        track_index = int(sc.get("track_index", 1))
        default_variant = "feature" if track_index == 2 else "spoken"
        if "Momax" in sc.get("text", ""):
            default_variant = "brand-lockup"
        variant = str(sc.get("variant", default_variant))
        if variant not in {"spoken", "feature", "title", "brand-lockup"}:
            variant = "spoken"
        z_index = 30 if variant == "spoken" else 25 if variant in {"brand-lockup", "title"} else 20
        L.append('<div id="%s" class="clip cap %s" data-start="%s" data-duration="%s" '
                  'data-track-index="%d" style="%sz-index:%d;font-size:%dpx;">%s</div>'
                  % (sid, variant, start, end - start, track_index, pos_css, z_index, size, text))
        tl.append('tl.from("#%s", %s, %s);' % (sid, _gsap_from(preset), start))
    L += [
        '</div>', '<script nonce="%s">' % nonce,
        'window.__timelines = window.__timelines || {};',
        'const tl = gsap.timeline({ paused: true });',
    ] + tl + [
        'window.__timelines["main"] = tl;',
        '</script>', '</body>', '</html>',
    ]
    return "\n".join(L) + "\n"


def build_project(spec, work_dir):
    """把场景写成一个 HyperFrames 工程目录，返回 index.html 路径。"""
    os.makedirs(work_dir, exist_ok=True)
    if os.path.isfile(GSAP_SOURCE):
        shutil.copyfile(GSAP_SOURCE, os.path.join(work_dir, "gsap.min.js"))
    else:
        with open(os.path.join(work_dir, "gsap.min.js"), "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_GSAP_RUNTIME)
    html_path = os.path.join(work_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(spec))
    # 最小 hyperframes.json（render 命令可无项目文件运行，但保留占位便于 preview）
    with open(os.path.join(work_dir, "hyperframes.json"), "w", encoding="utf-8") as f:
        json.dump({"entry": "index.html"}, f)
    return html_path


def _run_hf(args, cwd, verbose=True):
    cmd = _npx_command(["hyperframes"] + args)
    p = run_cmd(cmd, cwd=cwd, timeout=600, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
    out = p.stdout
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    else:
        out = str(out or "")
    if verbose:
        print(out[-1200:], flush=True)
    return p.returncode, out


def _fmt_from_out(out_path, fmt=None):
    """按显式 fmt 或输出扩展名推断 hyperframes --format。
    mov→ProRes 4444(alpha,Apple Silicon 硬件加速);webm→VP9(alpha,体积小);其余 mp4。"""
    if fmt:
        return fmt.lower()
    ext = os.path.splitext(out_path)[1].lower().lstrip(".")
    return ext if ext in ("mov", "webm", "mp4") else "mp4"


def _probe_pix_fmt(path):
    """ffprobe 视频流的 pix_fmt/codec_name。探测失败返回 (None, None)。"""
    try:
        ok, _msg = ensure_ffmpeg_on_path()
        if not ok:
            return None, None
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None, None
        p = run_cmd(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt,codec_name",
             "-of", "default=nk=1:nw=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        output = p.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        lines = (output or "").strip().splitlines()
        codec = lines[0] if len(lines) > 0 else None
        pix_fmt = lines[1] if len(lines) > 1 else None
        return codec, pix_fmt
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None, None


def render(spec, out_path, work_dir=None, verbose=True, fmt=None, quality="high"):
    """场景 JSON → 视频。返回 out_path。缺 node 时抛错（由调用方决定是否走 libass 兜底）。

    fmt: None=按 out_path 扩展名推断；'mov'=ProRes 4444 alpha（字幕透明层，推荐，
      Apple Silicon 硬件加速无损）；'webm'=VP9 alpha（体积小但慢）；'mp4'=普通不透明。
    透明字幕层：spec.background.type='transparent' + fmt='mov'，再用 ffmpeg overlay 叠回成片。

    **透明层防呆（重要）**：当 spec.background.type=='transparent' 时，本函数强制要求
    render_fmt in ('mov','webm')——绝不允许静默把透明字幕层渲成 'mp4'。这是因为 mp4/h264
    不支持 alpha 通道：浏览器画布的 `background:transparent` CSS 在无 alpha 编码格式下会被
    合成成不透明黑底，字幕层退化成"铺满全屏的黑色矩形+白字"。后续 subtitle_overlay.compose()
    的 ffmpeg overlay 会把这块黑色矩形整块盖在底片上——症状正是"最终成片只剩字幕和声音，
    实际画面被完全遮盖"（真实复现过：.mp4 输出的字幕层 ffprobe 显示 codec=h264/pix_fmt=yuv420p，
    无 alpha；.mov 正确输出显示 codec=prores/pix_fmt=yuva444p12le，带 alpha）。
    渲染完成后额外用 ffprobe 校验实际产物确有 alpha 通道（yuva*/rgba*/argb 等），
    校验失败同样报错阻断，不静默交付一个"看起来渲染成功但没有透明度"的文件。
    """
    if not _has_node():
        raise SystemExit("NO_NODE: 未检测到 node/npx，HyperFrames 不可用。"
                         "请先装 Node.js，或改用 text_anim.py(libass) 兜底。")
    browser_ok, browser_msg = ensure_browser_path()
    if not browser_ok:
        raise SystemExit("NO_BROWSER: %s" % browser_msg)
    ok, msg = ensure_ffmpeg_on_path()
    if verbose:
        print("[ffmpeg] " + msg, flush=True)
    if not ok:
        raise SystemExit("NO_FFMPEG: " + msg)
    owned_work_dir = work_dir is None
    output_root = os.path.join(ROOT, "output")
    os.makedirs(output_root, exist_ok=True)
    work_dir = work_dir or tempfile.mkdtemp(prefix="hf-", dir=output_root)
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    render_fmt = _fmt_from_out(out_path, fmt)
    fd, render_path = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(out_path), suffix="." + render_fmt,
        dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    os.unlink(render_path)
    try:
        build_project(spec, work_dir)

        bg_type = (spec.get("background") or {}).get("type")
        needs_alpha = bg_type == "transparent"
        if needs_alpha and render_fmt not in ("mov", "webm"):
            raise SystemExit(
            "ALPHA_FORMAT_MISMATCH: spec.background.type='transparent' 要求透明字幕层，"
            "但推断/指定的输出格式是 '%s'（不支持 alpha 通道）。mp4/h264 无法保留透明度，"
            "渲染出的字幕层会变成不透明黑底，overlay 时整块盖住底片画面（真实故障模式，"
            "非理论假设）。请把 --out 后缀改成 .mov/.webm，或显式传 --format mov。" % render_fmt)

        rc, out = _run_hf(["lint"], work_dir, verbose=verbose)
        if rc != 0:
            raise SystemExit("HyperFrames lint 失败:\n" + out[-1000:])
        render_args = ["render", "--output", render_path, "--quality", quality,
                       "--format", render_fmt]
        rc, out = _run_hf(render_args, work_dir, verbose=verbose)
        if rc != 0 or not os.path.isfile(render_path) or os.path.getsize(render_path) == 0:
            raise SystemExit("HyperFrames render 失败:\n" + out[-1200:])

        codec, pix_fmt = _probe_pix_fmt(render_path)
        if not codec or not pix_fmt:
            raise SystemExit("MEDIA_VERIFY_FAILED: HyperFrames 产物无法由 ffprobe 识别")
        if needs_alpha:
            has_alpha = bool(pix_fmt) and ("yuva" in pix_fmt or "rgba" in pix_fmt
                                        or "argb" in pix_fmt or "abgr" in pix_fmt
                                        or "bgra" in pix_fmt or "gbrap" in pix_fmt)
            if not has_alpha:
                raise SystemExit(
                "ALPHA_VERIFY_FAILED: 渲染完成但实测产物无 alpha 通道 "
                "(codec=%s, pix_fmt=%s)。这个字幕层如果拿去 overlay 会整块盖住底片画面。"
                "不予交付——请检查 HyperFrames/ffmpeg 版本，或改用 --format mov 重渲。"
                    % (codec, pix_fmt))
            if verbose:
                print("[ffmpeg] alpha 通道校验通过 (codec=%s, pix_fmt=%s)" % (codec, pix_fmt),
                      flush=True)
        os.replace(render_path, out_path)
        return out_path
    finally:
        if os.path.exists(render_path):
            os.unlink(render_path)
        if owned_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def doctor():
    runtime_ok, runtime_msg = hyperframes_runtime_status()
    print("HyperFrames CLI:", ("OK — " + runtime_msg) if runtime_ok else
          ("缺 — " + runtime_msg))
    browser_ok, browser_msg = ensure_browser_path()
    print("headless Chrome:", ("OK — " + browser_msg) if browser_ok else ("缺 — " + browser_msg))
    ok, msg = ensure_ffmpeg_on_path()
    print("ffmpeg+ffprobe:", ("OK — " + msg) if ok else ("缺 — " + msg))
    return 0 if (runtime_ok and browser_ok and ok) else 1


def main(argv):
    p = argparse.ArgumentParser(description="HyperFrames 字幕/动效引擎")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("render"); pr.add_argument("--spec", required=True)
    pr.add_argument("--out", required=True); pr.add_argument("--dir", default=None)
    pr.add_argument("--format", dest="fmt", default=None,
                    choices=["mov", "webm", "mp4"],
                    help="mov=ProRes4444 alpha(透明字幕层,推荐);webm=VP9 alpha(体积小);"
                         "mp4=不透明。缺省按 --out 扩展名推断。")
    pr.add_argument("--quality", default="high")
    pb = sub.add_parser("build"); pb.add_argument("--spec", required=True)
    pb.add_argument("--dir", required=True)
    sub.add_parser("doctor")
    args = p.parse_args(argv)

    if args.cmd == "doctor":
        return doctor()
    if args.cmd in ("render", "build"):
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
    if args.cmd == "render":
        out = render(spec, args.out, work_dir=args.dir, fmt=args.fmt, quality=args.quality)
        print(json.dumps({"ok": True, "out": out}, ensure_ascii=False))
        return 0
    if args.cmd == "build":
        html = build_project(spec, args.dir)
        print(json.dumps({"ok": True, "html": html}, ensure_ascii=False))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
