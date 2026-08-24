#!/usr/bin/env python3
"""Remotion 运镜/编排引擎包装器 —— 数字人讲解型/产品展示型成片的底层。

分工：Remotion 负责**运镜**（推拉摇移/Ken Burns）+ **PPT 内容页序列** + 数字人
画中画布局槽；HyperFrames 负责最上层字幕/特效；Kling 出会说话的数字人。

本脚本把一份 shotlist JSON 喂给 remotion_engine/ 工程渲成背景+运镜 MP4。

依赖：node/npx + Remotion 工程依赖（首次 npm install）+ 外部 ffmpeg/ffprobe
     + Chrome Headless Shell（Remotion 自带，首次下载；本机自带解压有 bug，doctor/render
       会自动手动 unzip + chmod 兜底）。

shotlist JSON（客户无关，由引导 skill 生成；brandPrimary 必须来自客户确认主色）：
{
  "width":1080,"height":1920,"fps":30,"brandPrimary":"<客户确认主色hex>",
  "shots":[
    {"durationInFrames":90,"move":"ken_burns","image":"assets/x/factory.jpg",
     "title":"智能工厂","bullets":["全自动产线","出货前100%质检"],"humanSlot":"right"},
    {"durationInFrames":60,"move":"push_in","video":"output/clip.mp4","transition":"fade"}
  ]
}
move: ken_burns/push_in/pull_out/pan_left/pan_right/tilt_up/tilt_down/still
humanSlot: none/left/right/full/corner（留给 fuse.py 叠抠像数字人）

CLI:
  python3 remotion_engine.py render --shotlist shots.json --out output/bg.mp4
  python3 remotion_engine.py doctor        # 检测+修复 node/依赖/Chrome
"""
import os
import sys
import json
import zipfile
import shutil
import glob
import argparse
import subprocess
import platform
import hashlib
import tempfile
import copy
import ipaddress
import socket
import urllib.request
import urllib.error
from urllib.parse import urlsplit
import media_tools

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "remotion_engine")
COMPOSITION = "Shots"
CONTENT_COMPOSITION = "Content"  # 文档/PPT 内容动效（项目自有 Remotion 组件）
KINETIC_COMPOSITION = "HorizontalKinetic"  # 横版口播：字幕+章节+卡片+PIP
INTEGRATED_COMPOSITION = "IntegratedExplainer"  # 人物场景底片 + 动画视觉层
STYLEFRAME_COMPOSITION = "Styleframe"  # 与最终 Remotion 设计系统同源的 look frame
SHOTCRAFT_COMPOSITION = "Shotcraft"
REMOTE_MEDIA_ALLOWLIST_ENV = "REMOTION_MEDIA_ALLOWLIST"


def _has(cmd):
    return shutil.which(cmd) is not None


def ensure_ffmpeg_on_path():
    """把用户单独管理的 ffmpeg+ffprobe 放上 PATH。"""
    return media_tools.add_ffmpeg_to_path()


def _use_bundled_node():
    bundled = os.path.join(ROOT, "offline-assets", "node-runtime", "bin")
    node = os.path.join(bundled, "node")
    if os.path.isfile(node):
        os.environ["PATH"] = bundled + os.pathsep + os.environ.get("PATH", "")
        return True
    return False


def normalize_input(input_path, output_path):
    """Normalize a talking-head source for the 1920x1080 kinetic composition."""
    ok, message = ensure_ffmpeg_on_path()
    if not ok:
        raise SystemExit("ERROR: " + message)
    if not os.path.isfile(input_path):
        raise SystemExit("ERROR: 输入口播视频不存在: %s" % input_path)
    ffmpeg = shutil.which("ffmpeg")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    output_path = os.path.abspath(output_path)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % os.path.basename(output_path),
                                     suffix=os.path.splitext(output_path)[1] or ".mp4",
                                     dir=os.path.dirname(output_path) or ".")
    os.close(fd)
    os.unlink(temporary)
    cmd = [ffmpeg, "-y", "-i", input_path,
           "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", temporary]
    result = subprocess.run(cmd)
    if result.returncode != 0 or not _media_output_ok(temporary):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise SystemExit("ERROR: 口播视频标准化失败: %s" % output_path)
    os.replace(temporary, output_path)
    print(json.dumps({"ok": True, "out": os.path.abspath(output_path), "format": "1920x1080 H.264/AAC"}, ensure_ascii=False))
    return output_path


def _npx():
    return shutil.which("npx")


def fix_chrome_headless_shell():
    """兜底修复：Remotion 自带解压在部分机器坏了（zip 在但没解出可执行）。
    手动 unzip + chmod +x。返回 (fixed_or_ok, msg)。"""
    base = os.path.join(ENGINE, "node_modules", ".remotion", "chrome-headless-shell")
    if not os.path.isdir(base):
        return True, "Chrome 尚未下载（首次 render 时下载）"
    # 找 zip 与目标平台目录
    fixed = []
    for root, dirs, files in os.walk(base):
        for f in files:
            if f.endswith(".zip"):
                zpath = os.path.join(root, f)
                # 解压到 zip 同目录
                try:
                    with zipfile.ZipFile(zpath) as z:
                        z.extractall(root)
                    fixed.append(f)
                except (OSError, ValueError):
                    pass
    # 给所有 chrome-headless-shell 可执行文件加执行权限
    made = 0
    for root, dirs, files in os.walk(base):
        for f in files:
            if f in ("chrome-headless-shell", "chrome-headless-shell.exe"):
                p = os.path.join(root, f)
                try:
                    os.chmod(p, 0o755)
                    made += 1
                except OSError:
                    pass
    if made:
        return True, "Chrome 就位（手动解压%d个zip, chmod %d个可执行）" % (len(fixed), made)
    return False, "Chrome 解压后未找到可执行文件"


def find_chrome_executable():
    """定位已解压的 chrome-headless-shell 可执行文件（用于 --browser-executable，
    避免每次 render 都重新下载/校验 Chrome，网络不稳时尤其关键）。找不到返回 None。"""
    base = os.path.join(ENGINE, "node_modules", ".remotion", "chrome-headless-shell")
    if not os.path.isdir(base):
        return None
    for root, _dirs, files in os.walk(base):
        for f in files:
            if f in ("chrome-headless-shell", "chrome-headless-shell.exe"):
                p = os.path.join(root, f)
                if os.access(p, os.X_OK):
                    return p
    return None


def _host_allowed(host, allowlist):
    host = (host or "").lower().rstrip(".")
    for item in allowlist:
        item = item.lower().strip().rstrip(".")
        if item.startswith("*.") and host.endswith(item[1:]) and host != item[2:]:
            return True
        if host == item:
            return True
    return False


def _validate_remote_media(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SystemExit("ERROR: REMOTE_MEDIA_REJECTED: 仅允许白名单 HTTPS 媒体")
    allowlist = [x for x in os.environ.get(REMOTE_MEDIA_ALLOWLIST_ENV, "").split(",") if x.strip()]
    if not _host_allowed(parsed.hostname, allowlist):
        raise SystemExit("ERROR: REMOTE_MEDIA_NOT_ALLOWLISTED: %s" % parsed.hostname)
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                                                               type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise SystemExit("ERROR: REMOTE_MEDIA_DNS_FAILED: %s" % exc)
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise SystemExit("ERROR: REMOTE_MEDIA_PRIVATE_ADDRESS: %s" % address)
    return url


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_remote_media(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _stage_remote_media(url, public_dir, max_bytes=2 * 1024 * 1024 * 1024):
    """Download allowlisted remote media so Chromium never accesses the network."""
    _validate_remote_media(url)
    opener = urllib.request.build_opener(_ValidatedRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "BasicRouter-Remotion/1"})
    try:
        response = opener.open(request, timeout=600)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise SystemExit("ERROR: REMOTE_MEDIA_DOWNLOAD_FAILED: %s" % exc)
    with response:
        final_url = response.geturl()
        _validate_remote_media(final_url)
        try:
            peer = response.fp.raw._sock.getpeername()[0]
            if not ipaddress.ip_address(peer.split("%", 1)[0]).is_global:
                raise SystemExit("ERROR: REMOTE_MEDIA_PRIVATE_PEER: %s" % peer)
        except AttributeError:
            raise SystemExit("ERROR: REMOTE_MEDIA_PEER_UNVERIFIED")
        expected = response.headers.get("Content-Length")
        if expected and int(expected) > max_bytes:
            raise SystemExit("ERROR: REMOTE_MEDIA_TOO_LARGE")
        suffix = os.path.splitext(urlsplit(final_url).path)[1].lower() or ".bin"
        fd, temporary = tempfile.mkstemp(prefix=".remote-", suffix=suffix, dir=public_dir)
        digest = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise SystemExit("ERROR: REMOTE_MEDIA_TOO_LARGE")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if expected and received != int(expected):
                raise SystemExit("ERROR: REMOTE_MEDIA_TRUNCATED")
            target = os.path.join(public_dir, digest.hexdigest()[:24] + suffix)
            os.replace(temporary, target)
            return target
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


def _stage_local_media(shotlist, stage_dir=None):
    """Copy local shot media into Remotion's public directory.

    Remotion's staticFile() only serves files below ``remotion_engine/public``.
    Shotlists are normally authored from the project root, so passing a valid
    project-relative path directly produces a late browser 404 during render.
    Stage files once per content hash and rewrite only local media references;
    URLs, data URLs and CSS backgrounds remain untouched.
    """
    public_root = os.path.join(ENGINE, "public")
    os.makedirs(public_root, exist_ok=True)
    stage_dir = stage_dir or tempfile.mkdtemp(prefix="render-", dir=public_root)
    public_dir = os.path.abspath(stage_dir)
    if os.path.commonpath([public_root, public_dir]) != os.path.abspath(public_root):
        raise SystemExit("ERROR: REMOTION_STAGE_ESCAPE")
    public_rel = os.path.relpath(public_dir, public_root).replace(os.sep, "/")
    staged = {}

    def stage_nested_media(value):
        """Stage media nested inside ContentSpec scene props."""
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key in ("image", "imagePath", "video", "videoPath", "source") and isinstance(child, str):
                    if child.startswith(("http://", "https://", "data:", "@")):
                        continue
                    local_value = child[len("file://"):] if child.startswith("file://") else child
                    candidates = [local_value, os.path.join(ROOT, local_value),
                                  os.path.join(ENGINE, "public", local_value)]
                    source = next((p for p in candidates if os.path.isfile(p)), None)
                    if source:
                        digest = hashlib.sha256()
                        with open(source, "rb") as handle:
                            for block in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(block)
                        name = digest.hexdigest()[:24] + os.path.splitext(source)[1].lower()
                        target = os.path.join(public_dir, name)
                        if not os.path.isfile(target):
                            shutil.copy2(source, target)
                        value[key] = public_rel + "/" + name
                    continue
                value[key] = stage_nested_media(child)
            return value
        if isinstance(value, list):
            return [stage_nested_media(item) for item in value]
        return value

    for scene in shotlist.get("scenes") or []:
        stage_nested_media(scene)
    media_objects = list(shotlist.get("shots") or [])
    media_objects.append(shotlist)
    for shot in media_objects:
        for field in (
                "image", "video", "videoPath", "pipVideoPath", "audioPath", "source",
                "humanVideoPath", "humanImagePath", "contentVideoPath",
                "motionVideoPath", "previewAudioPath"):
            value = shot.get(field)
            if not isinstance(value, str) or value.startswith("data:"):
                continue
            if value.startswith(("http://", "https://")):
                source = _stage_remote_media(value, public_dir)
                shot[field] = public_rel + "/" + os.path.basename(source)
                continue
            local_value = value[len("file://"):] if value.startswith("file://") else value
            candidates = [local_value, os.path.join(ROOT, local_value),
                          os.path.join(ENGINE, "public", local_value)]
            source = next((p for p in candidates if os.path.isfile(p)), None)
            if not source:
                raise SystemExit("ERROR: Remotion 素材不存在: %s" % value)
            digest = hashlib.sha256()
            with open(source, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            name = digest.hexdigest()[:24] + os.path.splitext(source)[1].lower()
            target = os.path.join(public_dir, name)
            if not os.path.isfile(target):
                os.makedirs(public_dir, exist_ok=True)
                shutil.copy2(source, target)
            staged[value] = public_rel + "/" + name
            shot[field] = staged[value]
        assets = shot.get("assets")
        if isinstance(assets, list):
            staged_assets = []
            for value in assets:
                if not isinstance(value, str) or value.startswith("@"):
                    staged_assets.append(value)
                    continue
                local_value = value[len("file://"):] if value.startswith("file://") else value
                source = next((p for p in (local_value, os.path.join(ROOT, local_value),
                                           os.path.join(ENGINE, "public", local_value)) if os.path.isfile(p)), None)
                if not source:
                    raise SystemExit("ERROR: Remotion Shotcraft 素材不存在: %s" % value)
                digest = hashlib.sha256(open(source, "rb").read()).hexdigest()
                name = digest[:24] + os.path.splitext(source)[1].lower()
                target = os.path.join(public_dir, name)
                if not os.path.isfile(target):
                    shutil.copy2(source, target)
                staged_assets.append(public_rel + "/" + name)
            shot["assets"] = staged_assets
    return shotlist


def _media_output_ok(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type",
         "-of", "default=nw=1:nk=1", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    return result.returncode == 0 and "video" in result.stdout.splitlines()


def _probe_video_format(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not os.path.isfile(path):
        return None, None, None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt:stream_tags=alpha_mode",
         "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        return None, None, None
    try:
        stream = (json.loads(result.stdout).get("streams") or [{}])[0]
    except (ValueError, TypeError, IndexError):
        return None, None, None
    return (
        stream.get("codec_name"),
        stream.get("pix_fmt"),
        (stream.get("tags") or {}).get("alpha_mode"),
    )


def _has_alpha_pixel_format(pix_fmt, alpha_mode=None):
    value = str(pix_fmt or "").lower()
    return str(alpha_mode or "") == "1" or any(marker in value for marker in (
        "yuva", "rgba", "argb", "abgr", "bgra", "gbrap"))


def ensure_deps(verbose=True):
    """确保 Remotion 工程依赖已装（node_modules 存在）。首次会 npm install。"""
    _use_bundled_node()
    if not _npx():
        raise SystemExit("ERROR: 未找到 node/npx。请先运行 deploy 脚本自动安装 Node。")
    nm = os.path.join(ENGINE, "node_modules", ".bin", "remotion")
    # A packaged node_modules tree is platform-specific. Never try to execute
    # the bundled macOS ARM binaries on Windows/Linux; npm ci will replace it.
    bundled_ok = (platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"))
    if os.path.exists(nm) and bundled_ok:
        # Prefer a verified local archive/executable. Calling `remotion browser
        # ensure` unconditionally causes a network download even when the
        # offline package is already installed.
        fixed, fix_msg = fix_chrome_headless_shell()
        local_chrome = find_chrome_executable()
        if local_chrome:
            if verbose:
                print("[chrome] 使用本地 Headless Shell: %s" % local_chrome, flush=True)
            return
        try:
            import offline_setup
            offline = offline_setup.install_chrome()
            if offline.get("ok"):
                if verbose:
                    print("[chrome] 已从离线包安装 Headless Shell: %s" %
                          offline.get("executable"), flush=True)
                return
        except (OSError, ValueError, RuntimeError):
            pass
        if verbose:
            print("检查 Remotion Chrome Headless Shell（本地包缺失，准备联网兜底）...", flush=True)
        subprocess.run([_remotion_bin(), "browser", "ensure"], cwd=ENGINE, check=True)
        return
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("ERROR: 未找到 npm。")
    if verbose and os.path.exists(nm) and not bundled_ok:
        print("包内 Node 依赖属于 macOS ARM，当前平台为 %s/%s，改用 npm ci 安装平台依赖..." %
              (platform.system(), platform.machine()), flush=True)
    if verbose:
        print("首次准备 Remotion 依赖（npm install，可能几分钟）...", flush=True)
    env = os.environ.copy()
    env.pop("NODE_ENV", None)
    env["npm_config_production"] = "false"
    subprocess.run([npm, "ci", "--include=dev", "--no-audit", "--no-fund"], cwd=ENGINE, env=env, check=True)
    fixed, fix_msg = fix_chrome_headless_shell()
    if not find_chrome_executable():
        try:
            import offline_setup
            offline = offline_setup.install_chrome()
            if verbose and offline.get("ok"):
                print("[chrome] 已从离线包安装 Headless Shell: %s" %
                      offline.get("executable"), flush=True)
        except (OSError, ValueError, RuntimeError):
            pass
    if not find_chrome_executable():
        subprocess.run([_remotion_bin(), "browser", "ensure"], cwd=ENGINE, check=True)


def _remotion_bin():
    return os.path.join(ENGINE, "node_modules", ".bin", "remotion")


def _node_bin(name):
    suffix = ".cmd" if sys.platform == "win32" else ""
    return os.path.join(ENGINE, "node_modules", ".bin", name + suffix)


QUALITY_SCALES = {
    # draft is explicitly non-approvable and optimized for rapid diagnostics;
    # standard remains a readable half-size client-internal preview.
    "draft": 0.25,
    "standard": 0.5,
    "high": 1.0,
}

RENDER_PROFILES = ("preview", "direct_delivery", "commercial_master")


def resolve_render_profile(quality, out_path, *, alpha=False, profile=None):
    """Choose an encoder contract without allowing extension/codec mismatch."""
    if profile and profile not in RENDER_PROFILES:
        raise ValueError("REMOTION_RENDER_PROFILE_INVALID: %s" % profile)
    if alpha:
        return "alpha_intermediate"
    extension = os.path.splitext(str(out_path))[1].lower()
    if profile:
        selected = profile
    elif quality != "high" or extension == ".webm":
        selected = "preview"
    elif extension == ".mov":
        selected = "commercial_master"
    else:
        selected = "direct_delivery"
    if selected == "commercial_master" and extension != ".mov":
        raise ValueError("REMOTION_COMMERCIAL_MASTER_REQUIRES_MOV")
    if selected == "direct_delivery" and extension not in (".mp4", ".mov"):
        raise ValueError("REMOTION_DIRECT_DELIVERY_REQUIRES_MP4_OR_MOV")
    return selected


def render_profile_args(profile, out_path, *, alpha=False):
    """Compile a deterministic Remotion/FFmpeg mastering argument set.

    Approval frames use PNG, delivery uses one H.264 encode, and the working
    master remains 10-bit 4:2:2 ProRes. Alpha intermediates retain their own
    codec because they are compositing assets rather than delivery masters.
    """
    extension = os.path.splitext(str(out_path))[1].lower()
    if alpha or profile == "alpha_intermediate":
        if extension == ".mov":
            return [
                "--codec", "prores", "--pixel-format", "yuva444p10le",
                "--prores-profile", "4444", "--image-format", "png",
                "--color-space", "bt709", "--audio-sample-rate", "48000",
            ]
        if extension == ".webm":
            return [
                "--codec", "vp9", "--pixel-format", "yuva420p",
                "--image-format", "png", "--color-space", "bt709",
                "--audio-sample-rate", "48000",
            ]
        raise ValueError("REMOTION_ALPHA_REQUIRES_MOV_OR_WEBM")
    if profile == "commercial_master":
        if extension != ".mov":
            raise ValueError("REMOTION_COMMERCIAL_MASTER_REQUIRES_MOV")
        return [
            "--codec", "prores", "--prores-profile", "hq",
            "--pixel-format", "yuv422p10le", "--image-format", "png",
            "--color-space", "bt709", "--audio-codec", "pcm-s16le",
            "--audio-sample-rate", "48000",
        ]
    if profile == "direct_delivery":
        if extension not in (".mp4", ".mov"):
            raise ValueError("REMOTION_DIRECT_DELIVERY_REQUIRES_MP4_OR_MOV")
        return [
            "--codec", "h264", "--pixel-format", "yuv420p",
            "--image-format", "png", "--color-space", "bt709",
            "--crf", "16", "--x264-preset", "slow",
            "--audio-codec", "aac", "--audio-bitrate", "256k",
            "--audio-sample-rate", "48000",
        ]
    if profile == "preview":
        if extension in (".mp4", ".mov"):
            return [
                "--codec", "h264", "--pixel-format", "yuv420p",
                "--image-format", "jpeg", "--jpeg-quality", "88",
                "--color-space", "bt709", "--crf", "22",
                "--x264-preset", "fast", "--audio-sample-rate", "48000",
            ]
        return ["--image-format", "jpeg", "--jpeg-quality", "88"]
    raise ValueError("REMOTION_RENDER_PROFILE_INVALID: %s" % profile)


def quality_scale(quality):
    """Map the public quality contract to a real render-size reduction.

    Historically ``draft`` only skipped the high JPEG-quality flag, so a
    45-second diagnostic still rendered all 1350 frames at 1920x1080.  Keep
    the composition geometry and timing identical while asking Remotion to
    scale the encoded frames.  Formal approval paths already require ``high``
    and therefore remain pixel-identical to the authored dimensions.
    """
    try:
        return QUALITY_SCALES[str(quality)]
    except KeyError:
        raise ValueError(
            "REMOTION_QUALITY_INVALID: %s (expected draft/standard/high)" % quality)


def render(shotlist_path, out_path, quality="high", composition=COMPOSITION, alpha=False,
           profile=None):
    ensure_deps()
    okff, msgff = ensure_ffmpeg_on_path()
    if not okff:
        raise SystemExit("ERROR: " + msgff)
    print("[ffmpeg] " + msgff, flush=True)

    with open(shotlist_path, "r", encoding="utf-8") as f:
        shotlist = json.load(f)

    public_root = os.path.join(ENGINE, "public")
    os.makedirs(public_root, exist_ok=True)
    stage_dir = tempfile.mkdtemp(prefix="render-", dir=public_root)
    shotlist = _stage_local_media(copy.deepcopy(shotlist), stage_dir=stage_dir)

    # ── 兼容 guide_scaffold compile_shots 输出的 resolution:[w,h] 格式 ──
    # Root.tsx 的 calculateMetadata 读顶层 width/height，而非 resolution 数组。
    # 若只有 resolution 数组则拆开，保证横屏/自定义尺寸能正确渲染，不回落默认 1080×1920。
    if "resolution" in shotlist and ("width" not in shotlist or "height" not in shotlist):
        res = shotlist["resolution"]
        if isinstance(res, (list, tuple)) and len(res) == 2:
            shotlist.setdefault("width", int(res[0]))
            shotlist.setdefault("height", int(res[1]))

    # props 必须是文件（Remotion --props 接受 json 文件路径）
    props_fd, props_path = tempfile.mkstemp(
        prefix="_props_", suffix=".json", dir=ENGINE)
    with os.fdopen(props_fd, "w", encoding="utf-8") as f:
        json.dump(shotlist, f, ensure_ascii=False)

    out_path = os.path.abspath(out_path)
    alpha_extension = os.path.splitext(out_path)[1].lower()
    if alpha and alpha_extension not in (".mov", ".webm"):
        raise SystemExit("ERROR: REMOTION_ALPHA_REQUIRES_MOV_OR_WEBM")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fd, render_path = tempfile.mkstemp(prefix=".%s." % os.path.basename(out_path),
                                       suffix=os.path.splitext(out_path)[1] or ".mp4",
                                       dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    os.unlink(render_path)

    selected_profile = resolve_render_profile(
        quality, out_path, alpha=alpha, profile=profile)

    def _run(browser_exe=None):
        cmd = [_remotion_bin(), "render", "src/index.ts", composition, render_path,
               "--props", props_path, "--log=error"]
        scale = quality_scale(quality)
        if scale != 1.0:
            cmd += ["--scale", str(scale)]
        cmd += render_profile_args(selected_profile, out_path, alpha=alpha)
        if browser_exe:
            cmd += ["--browser-executable", browser_exe]
        return subprocess.run(cmd, cwd=ENGINE)

    # 优先复用已解压的 Chrome，跳过每次 render 的重复下载/校验（网络不稳时关键）。
    chrome = find_chrome_executable()
    if chrome:
        print("[chrome] 复用已解压 Chrome: %s" % chrome, flush=True)
    print("[remotion] quality=%s profile=%s scale=%sx composition=%s" % (
        quality, selected_profile, quality_scale(quality), composition), flush=True)
    def _produced_ok():
        # 出片校验：文件必须真实存在且非空。Remotion 偶发 returncode=0 却没落盘
        # （Chrome 解压不完整/浏览器崩溃等），只看返回码会误报成功，必须查文件。
        return _media_output_ok(render_path)

    try:
        p = _run(browser_exe=chrome)
        if p.returncode != 0 or not _produced_ok():
            why = "返回码 %s" % p.returncode if p.returncode != 0 else "未产出成片文件"
            print("首次渲染失败（%s），尝试修复 Chrome Headless Shell 后重试..." % why, flush=True)
            fixed, msg = fix_chrome_headless_shell()
            print("[chrome] " + msg, flush=True)
            p = _run(browser_exe=find_chrome_executable())
        if p.returncode != 0:
            raise SystemExit("ERROR: Remotion 渲染失败（见上方日志）")
        if not _produced_ok():
            raise SystemExit(
                "ERROR: Remotion 返回成功但未生成可探测成片：%s（可能 Chrome 未就绪或渲染中断）" % out_path)
        if alpha:
            codec, pix_fmt, alpha_mode = _probe_video_format(render_path)
            if not _has_alpha_pixel_format(pix_fmt, alpha_mode):
                raise SystemExit(
                    "ERROR: REMOTION_ALPHA_VERIFY_FAILED: codec=%s pix_fmt=%s alpha_mode=%s" %
                    (codec, pix_fmt, alpha_mode))
        os.replace(render_path, out_path)
    finally:
        try:
            os.remove(props_path)
        except OSError:
            pass
        try:
            os.unlink(render_path)
        except OSError:
            pass
        try:
            shutil.rmtree(stage_dir, ignore_errors=True)
        except OSError:
            pass
    print(json.dumps({"ok": True, "out": out_path,
                      "render_profile": selected_profile}, ensure_ascii=False))
    return out_path


def render_still(props_path, out_path, *, composition=INTEGRATED_COMPOSITION,
                 frame=0, quality="high"):
    """Render one approval still through the exact Remotion composition used later.

    This is intentionally not a screenshot of a separately maintained mockup.
    Media staging, design tokens and component code are shared with ``render``.
    """
    ensure_deps()
    with open(props_path, "r", encoding="utf-8") as handle:
        props = json.load(handle)
    public_root = os.path.join(ENGINE, "public")
    os.makedirs(public_root, exist_ok=True)
    stage_dir = tempfile.mkdtemp(prefix="still-", dir=public_root)
    props = _stage_local_media(copy.deepcopy(props), stage_dir=stage_dir)
    if "resolution" in props and ("width" not in props or "height" not in props):
        resolution = props.get("resolution")
        if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
            props.setdefault("width", int(resolution[0]))
            props.setdefault("height", int(resolution[1]))
    props_fd, staged_props_path = tempfile.mkstemp(prefix="_still_props_", suffix=".json", dir=ENGINE)
    with os.fdopen(props_fd, "w", encoding="utf-8") as handle:
        json.dump(props, handle, ensure_ascii=False)
    out_path = os.path.abspath(out_path)
    if os.path.splitext(out_path)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        raise SystemExit("ERROR: REMOTION_STILL_REQUIRES_IMAGE_OUTPUT")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    suffix = os.path.splitext(out_path)[1] or ".png"
    fd, render_path = tempfile.mkstemp(prefix=".%s." % os.path.basename(out_path), suffix=suffix,
                                       dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    os.unlink(render_path)

    def run(browser_exe=None):
        cmd = [_remotion_bin(), "still", "src/index.ts", composition, render_path,
               "--props", staged_props_path, "--frame", str(max(0, int(frame))), "--log=error"]
        scale = quality_scale(quality)
        if scale != 1.0:
            cmd += ["--scale", str(scale)]
        if suffix.lower() in (".jpg", ".jpeg"):
            cmd += ["--image-format", "jpeg", "--jpeg-quality", "100"]
        else:
            cmd += ["--image-format", "png"]
        if browser_exe:
            cmd += ["--browser-executable", browser_exe]
        return subprocess.run(cmd, cwd=ENGINE)

    try:
        completed = run(find_chrome_executable())
        if completed.returncode != 0 or not os.path.isfile(render_path) or os.path.getsize(render_path) == 0:
            fixed, message = fix_chrome_headless_shell()
            print("[chrome] " + message, flush=True)
            completed = run(find_chrome_executable())
        if completed.returncode != 0 or not os.path.isfile(render_path) or os.path.getsize(render_path) == 0:
            raise SystemExit("ERROR: Remotion 静帧渲染失败（见上方日志）")
        os.replace(render_path, out_path)
    finally:
        for path in (staged_props_path, render_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        shutil.rmtree(stage_dir, ignore_errors=True)
    print(json.dumps({"ok": True, "out": out_path, "frame": int(frame),
                      "composition": composition}, ensure_ascii=False))
    return out_path


def render_quality_spec(spec_path, out_path):
    """Render only a complete, untampered formal quality specification."""
    import artifact_contract
    import design_token_contract
    import schema_validate

    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    schema_validate.enforce(
        spec, "render-quality-spec", context="remotion_engine.render_quality_spec")
    expected = spec.get("contract_sha256")
    payload = dict(spec)
    payload.pop("contract_sha256", None)
    actual = artifact_contract.sha256_json(payload)
    if expected != actual:
        raise SystemExit("ERROR: RENDER_SPEC_STALE: expected %s actual %s" % (expected, actual))
    if spec.get("mode") != "formal":
        raise SystemExit("ERROR: RENDER_SPEC_FORMAL_REQUIRED")
    if spec.get("unresolved"):
        raise SystemExit("ERROR: RENDER_SPEC_UNRESOLVED: %s" % "; ".join(spec["unresolved"]))
    design_token_contract.validate(spec.get("design_tokens") or {})
    props = spec.get("engine_props")
    if not isinstance(props, dict):
        raise SystemExit("ERROR: RENDER_SPEC_ENGINE_PROPS_REQUIRED")
    target = spec.get("target") or {}
    for key in ("width", "height", "fps"):
        if int(props.get(key) or 0) != int(target.get(key) or 0):
            raise SystemExit("ERROR: RENDER_SPEC_TARGET_MISMATCH: %s" % key)
    renderer = spec.get("renderer") or {}
    fd, props_path = tempfile.mkstemp(prefix=".formal-render-props-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(props, handle, ensure_ascii=False)
        return render(
            props_path, out_path, quality="high",
            composition=renderer.get("composition") or CONTENT_COMPOSITION,
            profile=renderer.get("render_profile") or "direct_delivery")
    finally:
        try:
            os.unlink(props_path)
        except OSError:
            pass


def render_shotcraft(spec_path, out_path, quality="high", profile=None):
    """Render a validated Shotcraft spec through the existing deterministic Shots composition.

    Shotcraft cards are deliberately compiled to clean media-only shots here;
    exact typography remains owned by HyperFrames so generated and deterministic
    layers never compete for text ownership.
    """
    sys.path.insert(0, HERE)
    import shotcraft_qc
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    report = shotcraft_qc.check(spec)
    if not report["passed"]:
        raise SystemExit("ERROR: SHOTCRAFT_QC_FAILED: %s" % ", ".join(report["errors"]))
    shotlist = copy.deepcopy(spec)
    fd, temporary = tempfile.mkstemp(prefix=".shotcraft-", suffix=".json", dir=os.path.dirname(os.path.abspath(out_path)) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(shotlist, handle, ensure_ascii=False)
        return render(temporary, out_path, quality, composition=SHOTCRAFT_COMPOSITION,
                      profile=profile)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def doctor():
    print("=== Remotion 引擎体检 ===")
    print("node:", shutil.which("node") or "缺失")
    print("npx :", _npx() or "缺失")
    okff, msgff = ensure_ffmpeg_on_path()
    print("ffmpeg+ffprobe:", ("OK — " + msgff) if okff else ("缺失 — " + msgff))
    try:
        ensure_deps(verbose=False)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Remotion 依赖预热失败:", exc)
    nm = os.path.exists(_remotion_bin())
    tsc = os.path.exists(_node_bin("tsc"))
    print("Remotion 依赖:", "已装" if nm else "未装")
    print("TypeScript/tsc:", "已装" if tsc else "未装（typecheck 必备）")
    if nm:
        fixed, msg = fix_chrome_headless_shell()
        local_chrome = find_chrome_executable()
        if local_chrome:
            print("Chrome Headless Shell: 使用本地包 —", local_chrome)
        else:
            print("Chrome Headless Shell:", msg)
            try:
                subprocess.run([_remotion_bin(), "browser", "ensure"], cwd=ENGINE, check=True)
            except subprocess.CalledProcessError as exc:
                print("Chrome 预热失败，退出码:", exc.returncode)
    ok = bool(shutil.which("node")) and okff and nm and tsc and bool(find_chrome_executable())
    print("结论:", "READY" if ok else "需补依赖（跑 deploy）")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Remotion 运镜/编排引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="按 shotlist 渲染运镜背景 MP4")
    r.add_argument("--shotlist", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    r.add_argument("--profile", choices=RENDER_PROFILES)
    rc = sub.add_parser("render-content",
                        help="按内容动效 spec 渲染文档/PPT 型动效 MP4（项目自有组件）")
    rc.add_argument("--spec", required=True, help="内容动效 scene spec JSON（content_scaffold.py 生成）")
    rc.add_argument("--out", required=True)
    rc.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    rc.add_argument("--profile", choices=RENDER_PROFILES)
    rc.add_argument("--alpha", action="store_true",
                    help="输出透明 MOV/WebM；spec.transparentBackground 应为 true")
    rk = sub.add_parser("render-kinetic", help="渲染横版口播增强模板（字幕/章节/卡片/PIP）")
    rk.add_argument("--spec", required=True, help="HorizontalKinetic props JSON")
    rk.add_argument("--out", required=True)
    rk.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    rk.add_argument("--profile", choices=RENDER_PROFILES)
    ri = sub.add_parser(
        "render-integrated",
        help="渲染人物嵌入动画场景的讲解成片（同一时间轴，柔和蒙版融合）")
    ri.add_argument("--spec", required=True, help="IntegratedExplainer props JSON")
    ri.add_argument("--out", required=True)
    ri.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    ri.add_argument("--profile", choices=RENDER_PROFILES)
    rs = sub.add_parser("render-shotcraft", help="渲染已校验的 Shotcraft 确定性包装 spec")
    rs.add_argument("--spec", required=True)
    rs.add_argument("--out", required=True)
    rs.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    rs.add_argument("--profile", choices=RENDER_PROFILES)
    rst = sub.add_parser("render-still", help="用正式 Remotion composition 渲染客户审批静帧")
    rst.add_argument("--spec", required=True)
    rst.add_argument("--out", required=True)
    rst.add_argument("--composition", default=INTEGRATED_COMPOSITION)
    rst.add_argument("--frame", type=int, default=0)
    rqs = sub.add_parser(
        "render-spec", help="按不可变 formal RenderSpec 渲染；任何未解析默认值都会阻断")
    rqs.add_argument("--spec", required=True)
    rqs.add_argument("--out", required=True)
    ni = sub.add_parser("normalize-input", help="将口播源统一为1920x1080 H.264/AAC")
    ni.add_argument("--input", required=True)
    ni.add_argument("--out", required=True)
    sub.add_parser("doctor", help="检测并修复依赖/Chrome")
    a = ap.parse_args()
    if a.cmd == "render":
        render(a.shotlist, a.out, a.quality, composition=COMPOSITION, profile=a.profile)
    elif a.cmd == "render-content":
        render(a.spec, a.out, a.quality, composition=CONTENT_COMPOSITION, alpha=a.alpha,
               profile=a.profile)
    elif a.cmd == "render-kinetic":
        render(a.spec, a.out, a.quality, composition=KINETIC_COMPOSITION,
               profile=a.profile)
    elif a.cmd == "render-integrated":
        render(a.spec, a.out, a.quality, composition=INTEGRATED_COMPOSITION,
               profile=a.profile)
    elif a.cmd == "render-shotcraft":
        render_shotcraft(a.spec, a.out, a.quality, profile=a.profile)
    elif a.cmd == "render-still":
        render_still(a.spec, a.out, composition=a.composition, frame=a.frame)
    elif a.cmd == "render-spec":
        render_quality_spec(a.spec, a.out)
    elif a.cmd == "normalize-input":
        normalize_input(a.input, a.out)
    elif a.cmd == "doctor":
        sys.exit(doctor())


if __name__ == "__main__":
    main()
