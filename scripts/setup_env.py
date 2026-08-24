#!/usr/bin/env python3
"""Environment initializer for a zero-plugin client machine.

Self-healing: detects Python, checks each dependency, installs what's missing
via pip (no admin needed, --user fallback), then verifies everything imports
and the ffmpeg binary resolves. Prints a clear READY / NOT READY report.

CLI:
  python3 setup_env.py check     # report only, no install
  python3 setup_env.py full-check # report all local engines; nonzero if any is missing
  python3 setup_env.py install   # install missing + verify (default)
"""
import sys
import subprocess
import os
import shutil
import warnings
import glob
import media_tools

HERE = os.path.dirname(os.path.abspath(__file__))

# urllib3 v2 emits a noisy LibreSSL warning on the macOS system Python. The
# project uses BasicRouter through its own retry/HTTPS policy; expose this as a
# capability check rather than polluting the customer setup transcript.
warnings.filterwarnings(
    "ignore", message="urllib3 v2 only supports OpenSSL.*", module="urllib3")

# (import_name, pip_spec, purpose)
DEPS = [
    ("pptx", "python-pptx>=0.6.21", "解析 .pptx"),
    ("docx", "python-docx>=0.8.11", "解析/生成 .docx"),
    ("pypdf", "pypdf>=6.0.0,<7", "解析 .pdf"),
    ("openpyxl", "openpyxl>=3.0.0", "解析 .xlsx"),
]
# macOS-only：OCR 兜底检测依赖（Vision 框架）。非 macOS 平台静默跳过检测/安装。
MACOS_DEPS = [
    ("Vision", "pyobjc-framework-Vision>=10.0", "出片后 OCR 字幕兜底检测(macOS Vision)"),
    ("Quartz", "pyobjc-framework-Quartz>=10.0", "Vision 依赖的 Quartz 绑定"),
]


class KeyStatusUnavailable(RuntimeError):
    """The session key state could not be read; this is not normal MISSING."""


def _basicrouter_key_present():
    """Require authenticated readiness, not merely a cache file's presence."""
    try:
        sys.path.insert(0, HERE)
        import key_setup  # noqa
    except ImportError as exc:
        raise KeyStatusUnavailable(
            "BASICROUTER_KEY_STATUS_UNAVAILABLE:ImportError") from exc
    try:
        key_setup.require_key()
        return True
    except key_setup.SessionKeyRequired:
        return False
    except key_setup.SessionKeyUnverified as exc:
        raise KeyStatusUnavailable(
            "BASICROUTER_KEY_STATUS_UNVERIFIED") from exc
    except (OSError, ValueError) as exc:
        raise KeyStatusUnavailable(
            "BASICROUTER_KEY_STATUS_UNAVAILABLE:%s" % type(exc).__name__) from exc


def bundled_ffmpeg_paths():
    """Compatibility API; community builds never expose bundled FFmpeg."""
    return media_tools.bundled_ffmpeg_paths()


def bundled_node_bin():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "offline-assets", "node-runtime", "bin")
    return path if os.path.isfile(os.path.join(path, "node")) else None
# 注：抠像/换背景/人景融合一律走外部模型（BasicRouter），本地不装 rembg/onnxruntime，保持轻交付。


def _importable(mod):
    try:
        __import__(mod)
        return True
    except (ImportError, OSError, RuntimeError):
        return False


def _pip_install(specs):
    """Try normal install, then --user on failure."""
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    r = subprocess.run(base + specs)
    if r.returncode != 0:
        print("  normal install failed, retrying with --user …")
        r = subprocess.run(base + ["--user"] + specs)
    return r.returncode == 0


def clean_stale_locks(verbose=True, stale_after=300.0, roots=("assets", "output")):
    """清理残留的 stale FileLock（进程崩溃后遗留的 *.lock，mtime 超过 stale_after）。

    FileLock 自身有 stale 检测，但没人定期扫描——assets/ 下已实测出现 3 个
    残留锁文件。只删除 mtime 超过阈值的 .lock，绝不碰其他文件。
    """
    import time
    removed = []
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    now = time.time()
    for root in roots:
        base = os.path.join(project_root, root)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if not name.endswith(".lock"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    age = now - os.path.getmtime(path)
                except OSError:
                    continue
                if age <= stale_after:
                    continue
                try:
                    os.remove(path)
                    removed.append(path)
                except OSError:
                    pass
    if verbose and removed:
        print("  [清理] 移除 %d 个 stale lock：%s"
              % (len(removed), ", ".join(os.path.relpath(p, project_root) for p in removed)))
    return removed


def check(verbose=True, include_optional=True):
    """检测核心依赖。返回 missing（仅核心项，影响 READY 退出码）。

    OCR 依赖（MACOS_DEPS）是**可选**的——ocr_check.py 缺 Vision 时静默降级，
    不阻塞出片。所以即使装不上（部分 macOS 编译 pyobjc 失败）也不算 NOT READY，
    只提示可选功能不可用，避免总入口反复触发自举死循环。
    """
    missing = []
    for mod, spec, purpose in DEPS:
        ok = _importable(mod)
        if verbose:
            print("  [%s] %-16s %s" % ("OK" if ok else "--", mod, purpose))
        if not ok:
            missing.append((mod, spec, purpose))
    # 可选：OCR 兜底（仅 macOS，缺失不影响 READY）
    if include_optional and sys.platform == "darwin":
        for mod, spec, purpose in MACOS_DEPS:
            ok = _importable(mod)
            if verbose:
                print("  [%s] %-16s %s%s" % ("OK" if ok else "~~", mod, purpose,
                                             "" if ok else "  (可选,缺失则OCR跳过)"))
    clean_stale_locks(verbose=verbose)
    return missing


def missing_optional():
    """返回缺失的可选依赖（OCR）。仅用于提示，不影响 READY。"""
    if sys.platform != "darwin":
        return []
    return [(m, s, p) for m, s, p in MACOS_DEPS if not _importable(m)]


def verify_ffmpeg():
    pair = media_tools.resolve_ffmpeg_pair(allow_fetch=False)
    return all(pair)


def verify_hyperframes_engine(verbose=True):
    """HyperFrames 主引擎就绪度：可执行 CLI + ffmpeg/ffprobe。

    返回 (runtime_ok, ff_ok)。任一不满足时打印如何补齐；引擎缺失不阻塞基础出片，
    但动态文字会回退 libass 兜底，且必须向交付流程标注体验降级。
    """
    try:
        import hf_engine
        runtime_ok, runtime_msg = hf_engine.hyperframes_runtime_status()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        runtime_ok, runtime_msg = False, "CLI 探测失败: %s" % exc
    ff_ok = False
    try:
        ff, fp = media_tools.resolve_ffmpeg_pair(allow_fetch=False)
        ff_ok = all((ff, fp)) and os.path.isfile(ff) and os.path.isfile(fp)
    except (ImportError, OSError, RuntimeError):
        ff_ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    if verbose:
        print("  [%s] HyperFrames CLI    (%s)" % (
            "OK" if runtime_ok else "--", runtime_msg))
        print("  [%s] ffmpeg+ffprobe     (HyperFrames 渲染硬依赖)" % ("OK" if ff_ok else "--"))
        if not runtime_ok:
            print("     → HyperFrames CLI 不可用：请重跑 setup/deploy；动态文字只能显式降级到 libass。")
    return runtime_ok, ff_ok


def _remotion_engine_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "remotion_engine")


def _remotion_bin(name):
    suffix = ".cmd" if sys.platform == "win32" else ""
    return os.path.join(_remotion_engine_dir(), "node_modules", ".bin", name + suffix)


def verify_remotion_project_deps(verbose=True):
    """Remotion project dependencies required for render and typecheck."""
    remotion_cli = os.path.exists(_remotion_bin("remotion"))
    tsc_cli = os.path.exists(_remotion_bin("tsc"))
    package_lock = os.path.exists(os.path.join(_remotion_engine_dir(), "package-lock.json"))
    ready = bool(shutil.which("node") and shutil.which("npm") and package_lock and remotion_cli and tsc_cli)
    if verbose:
        print("  [%s] Remotion CLI        (本地运镜/包装渲染)" % ("OK" if remotion_cli else "--"))
        print("  [%s] TypeScript/tsc      (Remotion 类型检查，项目必备)" % ("OK" if tsc_cli else "--"))
        if not ready:
            print("     → Remotion npm 依赖未完整安装：运行 `python3 scripts/setup_env.py install` 或 deploy 预热。")
    return ready


def install_remotion_project_deps(verbose=True):
    """Install Remotion npm deps, including devDependencies needed by typecheck."""
    engine = _remotion_engine_dir()
    if not os.path.isdir(engine):
        if verbose:
            print("  [--] remotion_engine/ 不存在，跳过 Remotion npm 依赖安装")
        return False
    if not shutil.which("npm"):
        if verbose:
            print("  [--] 未找到 npm，无法安装 Remotion npm 依赖")
        return False
    env = os.environ.copy()
    env.pop("NODE_ENV", None)
    env["npm_config_production"] = "false"
    if verbose:
        print("  准备 Remotion npm 依赖（包含 TypeScript/typecheck，可能需要几分钟）...")
    result = subprocess.run(
        [shutil.which("npm"), "ci", "--include=dev", "--no-audit", "--no-fund"],
        cwd=engine, env=env)
    if result.returncode != 0:
        return False
    try:
        import offline_setup
        offline = offline_setup.install_chrome()
        if verbose and offline.get("ok"):
            print("  [OK] Remotion Chrome 已从离线包安装: %s" % offline.get("executable"))
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass
    return verify_remotion_project_deps(verbose=False)


def verify_video_engines(verbose=True):
    """视频编排就绪度：Remotion 运镜（本地无模型）+ 外部融合能力（BasicRouter）。

    返回 (remotion_ok, fuse_ok)。抠像/换背景/人景融合全部走外部模型——本地不跑，
    所以这里只确认：Remotion 运镜引擎在，以及外部融合的 API Key 已配（就绪即可用
    matte.py compose 路线C / 在已确认 segment 内设 video_type 4/5 的路线A）。
    """
    # Remotion：node + 工程依赖已装（纯排版渲染，无模型）
    remotion_ok = verify_remotion_project_deps(verbose=False)
    # 外部人景融合：只需 API Key（能力在 BasicRouter 云端，本地零依赖）
    fuse_ok = _basicrouter_key_present()
    if verbose:
        print("  [%s] Remotion 运镜引擎   (数字人+PPT内容页/推拉摇移，本地无模型)" % ("OK" if remotion_ok else "--"))
        verify_remotion_project_deps(verbose=True)
        print("  [%s] 外部人景融合能力   (抠像/换背景走 BasicRouter，需 API Key)" % ("OK" if fuse_ok else "--"))
        if not fuse_ok:
            print("     → 未配 API Key：配好后即可用路线A(参考图)/路线C(img2img)做人景融合，本地不跑模型。")
    return remotion_ok, fuse_ok


def verify_remotion_chrome(verbose=True):
    """Check whether a browser usable by Remotion/HyperFrames is executable."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, "remotion_engine", "node_modules", ".remotion",
                        "chrome-headless-shell")
    found = None
    if os.path.isdir(base):
        for current, _dirs, files in os.walk(base):
            for filename in ("chrome-headless-shell", "chrome-headless-shell.exe"):
                if filename in files:
                    candidate = os.path.join(current, filename)
                    if sys.platform == "win32" or os.access(candidate, os.X_OK):
                        found = candidate
                        break
            if found:
                break
    if not found:
        for key in ("REMOTION_BROWSER_EXECUTABLE", "HYPERFRAMES_BROWSER_PATH",
                    "PRODUCER_HEADLESS_SHELL_PATH"):
            candidate = os.environ.get(key, "").strip()
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    if not found and sys.platform == "darwin":
        for candidate in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    if not found:
        cache_root = os.path.join(root, "output", ".cache", "hyperframes-chrome")
        if os.path.isdir(cache_root):
            for current, _dirs, files in os.walk(cache_root):
                for filename in ("chrome-headless-shell", "chrome-headless-shell.exe"):
                    if filename in files:
                        candidate = os.path.join(current, filename)
                        if sys.platform == "win32" or os.access(candidate, os.X_OK):
                            found = candidate
                            break
                if found:
                    break
    ready = bool(found)
    if verbose:
        print("  [%s] Remotion Chrome Headless Shell" % ("OK" if ready else "--"))
        if not ready:
            print("     → 首次渲染前会由 deploy/remotion doctor 下载并修复 Chrome。")
    return ready


def capability_status():
    """Return machine-readable capability state for routing decisions."""
    missing = check(verbose=False, include_optional=False)
    node_ok, hf_ff = verify_hyperframes_engine(verbose=False)
    remotion_ok, key_present = verify_video_engines(verbose=False)
    chrome_ok = verify_remotion_chrome(verbose=False)
    engine_imports = True
    for module in ("video_engine", "hf_engine", "remotion_engine"):
        try:
            __import__(module)
        except (ImportError, OSError, RuntimeError):
            engine_imports = False
    ocr_ok = (sys.platform == "darwin" and
              all(_importable(mod) for mod, _, _ in MACOS_DEPS))
    status = {
        "python_core": not missing,
        "ffmpeg": verify_ffmpeg(),
        "hyperframes": bool(node_ok and hf_ff),
        "remotion": bool(remotion_ok),
        "engine_imports": engine_imports,
        "remotion_chrome": bool(chrome_ok),
        # OCR is optional off macOS because Vision is macOS-only and ocr_check
        # intentionally degrades to a no-op on other platforms.
        "ocr": bool(ocr_ok) if sys.platform == "darwin" else True,
        "basicrouter_key_present": bool(key_present),
    }
    status["dependencies_ready"] = all(status[name] for name in
                                        ("python_core", "ffmpeg", "hyperframes", "remotion", "engine_imports", "remotion_chrome", "ocr"))
    status["formal_generation_ready"] = bool(status["dependencies_ready"] and key_present)
    return status


def _print_capabilities(caps):
    print("\n能力状态:")
    for name, ready in caps.items():
        print("  [%s] %s" % ("OK" if ready else "--", name))


def main(argv):
    mode = argv[0] if argv else "install"
    print("Python: %s" % sys.version.split()[0])
    if sys.version_info < (3, 7):
        print("WARNING: Python 3.7+ recommended.")

    print("依赖检测:")
    missing = check()

    if mode in ("check", "full-check"):
        print("HyperFrames 引擎:")
        verify_hyperframes_engine()
        print("数字人视频引擎:")
        try:
            verify_video_engines()
            caps = capability_status()
        except KeyStatusUnavailable as exc:
            print("  [ERROR] BasicRouter 密钥状态无法安全读取（不等同于 MISSING）。")
            print("     → 请检查会话 ID、缓存文件权限后重试；诊断码: %s" % exc)
            print("\n结果: NOT READY ❌")
            return 1
        _print_capabilities(caps)
        print("\n结果: %s" % ("核心环境 READY" if not missing else
                               "缺 %d 个核心依赖，运行 `python3 setup_env.py install` 安装" % len(missing)))
        if mode == "full-check":
            return 0 if caps["dependencies_ready"] else 1
        return 0 if not missing else 1

    # install mode
    if missing:
        print("\n安装缺失依赖: %s" % ", ".join(m[1] for m in missing))
        if not _pip_install([m[1] for m in missing]):
            print("\nNOT READY: pip 安装失败。请检查网络或手动 `pip3 install -r requirements.txt`。")
            return 1
        print("安装完成，重新校验…")
        missing = check(verbose=False)

    # 可选依赖（OCR）best-effort 安装：失败不阻塞 READY
    opt = missing_optional()
    if opt:
        print("\n安装可选依赖(OCR字幕检测): %s" % ", ".join(m[1] for m in opt))
        if not _pip_install([m[1] for m in opt]):
            print("  可选依赖安装失败(不影响出片)：OCR 字幕检测将自动跳过。")

    ff = verify_ffmpeg()
    print("  [%s] ffmpeg 二进制可用" % ("OK" if ff else "--"))

    print("HyperFrames 引擎:")
    node_ok, hf_ff = verify_hyperframes_engine()

    print("Remotion npm 依赖:")
    remotion_deps_ok = install_remotion_project_deps()
    if not remotion_deps_ok:
        print("  Remotion npm 依赖安装失败或不完整。请检查网络/npm registry，修复后重跑 setup。")

    print("数字人视频引擎:")
    try:
        remotion_ok, matte_ok = verify_video_engines()
        caps = capability_status()
    except KeyStatusUnavailable as exc:
        print("  [ERROR] BasicRouter 密钥状态无法安全读取（不等同于 MISSING）。")
        print("     → 请检查会话 ID、缓存文件权限后重试；诊断码: %s" % exc)
        print("\n结果: NOT READY ❌")
        return 1
    ready = caps["dependencies_ready"]
    print("\n结果: %s" % ("环境就绪 READY ✅" if ready else "NOT READY ❌"))
    if not ready:
        print("提示: Remotion/HyperFrames/Chrome/TypeScript 都属于正式本地后期依赖，缺任一项都不应进入正式交付。")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
