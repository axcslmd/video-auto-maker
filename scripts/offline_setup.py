#!/usr/bin/env python3
"""Install bundled, platform-specific runtime artifacts without network access."""
import argparse
import glob
import json
import os
import posixpath
import platform
import shutil
import stat
import tarfile
import zipfile
import hashlib
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "remotion_engine")
OFFLINE = os.path.join(ROOT, "offline-assets")
MANIFEST = os.path.join(OFFLINE, "artifact-manifest.json")
INIT_BUNDLE_PATTERNS = (
    os.path.join(ROOT, "dist", "*initready.tar.gz"),
    os.path.join(ROOT, "initready.tar.gz"),
    os.path.join(ROOT, "bootstrap", "*initready.tar.gz"),
)
INIT_BUNDLE_PREFIXES = (
    "offline-assets/",
    "remotion_engine/node_modules/",
    "remotion_engine/public/",
)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_sha256(root, exclude_names=None):
    """Hash a runtime tree, ignoring only explicitly reproducible caches."""
    exclude_names = set(exclude_names or ())
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in exclude_names)
        files = sorted(name for name in files if name not in exclude_names)
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise ValueError("ARTIFACT_SYMLINK_BLOCKED: %s" % path)
                resolved = os.path.realpath(path)
                if os.path.commonpath((os.path.realpath(root), resolved)) != os.path.realpath(root):
                    raise ValueError("ARTIFACT_SYMLINK_ESCAPE: %s" % path)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                digest.update(("L\0%s\0%s\n" % (rel, target)).encode("utf-8"))
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            digest.update(("F\0%s\0%s\n" % (rel, _sha256(path))).encode("utf-8"))
    return digest.hexdigest()


def _load_manifest():
    with open(MANIFEST, encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema") != 1 or manifest.get("algorithm") != "sha256":
        raise ValueError("ARTIFACT_MANIFEST_INVALID")
    return manifest


def _supported_wheel_tags():
    try:
        from packaging.tags import sys_tags
    except ImportError:
        from pip._vendor.packaging.tags import sys_tags
    return {str(tag) for tag in sys_tags()}


def _wheel_tags(filename):
    try:
        from packaging.utils import parse_wheel_filename
    except ImportError:
        from pip._vendor.packaging.utils import parse_wheel_filename
    return {str(tag) for tag in parse_wheel_filename(filename)[3]}


def _bundle_candidates():
    candidates = []
    for pattern in INIT_BUNDLE_PATTERNS:
        candidates.extend(glob.glob(pattern))
    unique = []
    seen = set()
    for path in candidates:
        path = os.path.abspath(path)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        unique.append(path)
    unique.sort(key=lambda path: (os.path.getmtime(path), path), reverse=True)
    return unique


def _bundle_platform(bundle_path):
    try:
        with tarfile.open(bundle_path, "r:gz") as bundle:
            try:
                member = bundle.getmember("offline-assets/artifact-manifest.json")
            except KeyError:
                return None
            payload = bundle.extractfile(member)
            if payload is None:
                return None
            manifest = json.loads(payload.read().decode("utf-8"))
            return manifest.get("platform")
    except (OSError, tarfile.TarError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return None


def _init_runtime_ready():
    node = os.path.join(OFFLINE, "node-runtime", "bin",
                        "node.exe" if os.name == "nt" else "node")
    chrome = os.path.join(OFFLINE, "chrome", "chrome-headless-shell-%s.zip" % platform_tag())
    remotion_bin = os.path.join(ENGINE, "node_modules", ".bin", "remotion")
    return (os.path.isdir(os.path.join(OFFLINE, "python-wheels")) and
            os.path.isfile(node) and
            os.path.isfile(chrome) and
            os.path.isdir(os.path.join(OFFLINE, "hyperframes-runtime")) and
            os.path.isdir(os.path.join(ENGINE, "public")) and
            os.path.exists(remotion_bin))


def _tar_member_selected(name):
    normalized = posixpath.normpath(name)
    if normalized in ("", "."):
        return False, normalized
    if normalized.startswith("../") or normalized == ".." or os.path.isabs(normalized):
        raise ValueError("INIT_BUNDLE_PATH_ESCAPE: %s" % name)
    for prefix in INIT_BUNDLE_PREFIXES:
        if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
            return True, normalized
    return False, normalized


def _safe_member_target(normalized):
    target = os.path.abspath(os.path.join(ROOT, normalized))
    root_real = os.path.realpath(ROOT)
    target_real = os.path.realpath(target)
    if os.path.commonpath((root_real, target_real)) != root_real:
        raise ValueError("INIT_BUNDLE_PATH_ESCAPE: %s" % normalized)
    return target


def _extract_init_bundle(bundle_path):
    extracted = []
    with tarfile.open(bundle_path, "r:gz") as bundle:
        for member in bundle.getmembers():
            selected, normalized = _tar_member_selected(member.name)
            if not selected:
                continue
            target = _safe_member_target(normalized)
            if member.issym() or member.islnk():
                link_target = os.path.realpath(os.path.join(os.path.dirname(target), member.linkname))
                if os.path.commonpath((os.path.realpath(ROOT), link_target)) != os.path.realpath(ROOT):
                    raise ValueError("INIT_BUNDLE_SYMLINK_ESCAPE: %s -> %s" %
                                     (member.name, member.linkname))
            bundle.extract(member, ROOT)
            extracted.append(normalized)
    return extracted


def hydrate_init_bundle():
    """Hydrate heavyweight runtime assets from a project-local init bundle."""
    if _init_runtime_ready():
        return {"ok": True, "skipped": True, "reason": "already-ready"}
    candidates = _bundle_candidates()
    if not candidates:
        return {"ok": True, "skipped": True, "reason": "bundle-missing"}
    bundle_path = candidates[0]
    bundle_platform = _bundle_platform(bundle_path)
    if bundle_platform and bundle_platform != platform_tag():
        return {"ok": True, "skipped": True, "reason": "platform-mismatch",
                "bundle": bundle_path, "bundle_platform": bundle_platform,
                "current_platform": platform_tag()}
    extracted = _extract_init_bundle(bundle_path)
    return {"ok": True, "bundle": bundle_path, "extracted": len(extracted),
            "paths": extracted[:20], "truncated": len(extracted) > 20}


def verify_bundle():
    """Verify platform, Python ABI/tags, and every bundled artifact digest."""
    try:
        manifest = _load_manifest()
        if manifest.get("platform") != platform_tag():
            raise ValueError("OFFLINE_PLATFORM_MISMATCH")
        python_spec = manifest.get("python") or {}
        current_version = "%d.%d" % sys.version_info[:2]
        if (python_spec.get("implementation") != "cp" or
                python_spec.get("version") != current_version or
                platform.python_implementation() != "CPython"):
            raise ValueError("OFFLINE_PYTHON_ABI_MISMATCH")
        wheel_root = os.path.join(OFFLINE, python_spec["wheel_tree"])
        if _tree_sha256(wheel_root) != python_spec.get("wheel_tree_sha256"):
            raise ValueError("ARTIFACT_DIGEST_MISMATCH: python-wheels")
        supported = _supported_wheel_tags()
        incompatible = [name for name in sorted(os.listdir(wheel_root))
                        if name.endswith(".whl") and not (_wheel_tags(name) & supported)]
        if incompatible:
            raise ValueError("OFFLINE_WHEEL_TAG_MISMATCH: %s" % ", ".join(incompatible))
        for artifact in manifest.get("artifacts", []):
            path = os.path.abspath(os.path.join(OFFLINE, artifact["path"]))
            if os.path.commonpath((ROOT, path)) != ROOT:
                raise ValueError("ARTIFACT_PATH_ESCAPE")
            if "sha256" in artifact:
                actual = _sha256(path)
                expected = artifact["sha256"]
            else:
                actual = _tree_sha256(
                    path, exclude_names=artifact.get("tree_exclude_names") or ())
                expected = artifact.get("tree_sha256")
            if actual != expected:
                raise ValueError("ARTIFACT_DIGEST_MISMATCH: %s" % artifact["path"])
        return {"ok": True, "platform": platform_tag(), "python": current_version}
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "platform": platform_tag(), "error": str(exc)}


def platform_tag():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine in ("arm64", "aarch64"):
        return "darwin-arm64"
    if system == "darwin":
        return "darwin-x64"
    if system == "windows":
        return "win-arm64" if machine in ("arm64", "aarch64") else "win-x64"
    return "linux-arm64" if machine in ("arm64", "aarch64") else "linux-x64"


def _install_python_wheels():
    wheel_dir = os.path.join(OFFLINE, "python-wheels")
    lock = os.path.join(ROOT, "requirements.lock.txt")
    if not os.path.isdir(wheel_dir):
        return {"ok": False, "error": "offline-assets/python-wheels 不存在"}
    import subprocess
    result = subprocess.run([
        os.environ.get("PYTHON", "python3"), "-m", "pip", "install",
        "--no-index", "--only-binary=:all:", "--find-links", wheel_dir, "-r", lock,
    ])
    return {"ok": result.returncode == 0, "step": "python-wheels", "returncode": result.returncode}


def _install_chrome():
    tag = platform_tag()
    archive = os.path.join(OFFLINE, "chrome", "chrome-headless-shell-%s.zip" % tag)
    if not os.path.isfile(archive):
        return {"ok": False, "error": "缺少平台专属 Chrome 离线包: %s" % archive}
    target = os.path.join(ENGINE, "node_modules", ".remotion", "chrome-headless-shell")
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        target_real = os.path.realpath(target)
        for member in bundle.infolist():
            destination = os.path.realpath(os.path.join(target, member.filename))
            if os.path.commonpath((target_real, destination)) != target_real:
                return {"ok": False, "error": "CHROME_ZIP_PATH_ESCAPE"}
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                return {"ok": False, "error": "CHROME_ZIP_SYMLINK_BLOCKED"}
        bundle.extractall(target)
    executable = None
    for current, _dirs, files in os.walk(target):
        for name in ("chrome-headless-shell", "chrome-headless-shell.exe"):
            if name in files:
                executable = os.path.join(current, name)
                if os.name != "nt":
                    os.chmod(executable, os.stat(executable).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                break
        if executable:
            break
    return {"ok": bool(executable), "step": "chrome", "executable": executable}


def install_chrome():
    """Install the platform Chrome Headless Shell zip if it is bundled."""
    try:
        return _install_chrome()
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        return {"ok": False, "step": "chrome", "error": str(exc)}


def _install_hyperframes_runtime():
    """Expose the bundled HyperFrames npm tree through the project runtime."""
    source = os.path.join(OFFLINE, "hyperframes-runtime", "node_modules")
    if not os.path.isdir(source):
        return {"ok": False, "step": "hyperframes", "error": "缺少 offline-assets/hyperframes-runtime"}
    target = os.path.join(OFFLINE, "node-runtime", "lib", "node_modules", "hyperframes")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.isdir(target) and not os.path.islink(target):
        return {"ok": True, "step": "hyperframes", "runtime": target,
                "source": "verified-bundle"}
    if os.path.lexists(target):
        return {"ok": False, "step": "hyperframes", "error": "RUNTIME_TARGET_UNSAFE"}
    package = os.path.join(source, "hyperframes")
    if os.path.isdir(package):
        shutil.copytree(package, target)
    else:
        return {"ok": False, "step": "hyperframes", "error": "离线 HyperFrames 包目录不存在"}
    return {"ok": True, "step": "hyperframes", "runtime": target}


def install():
    try:
        hydrate = hydrate_init_bundle()
    except (OSError, tarfile.TarError, ValueError) as exc:
        hydrate = {"ok": False, "error": str(exc)}
    verification = verify_bundle()
    tag = platform_tag()
    if not verification["ok"]:
        if not hydrate.get("ok"):
            return verification
    results = [_install_python_wheels(), _install_hyperframes_runtime(),
               _install_chrome()]
    ok = all(item.get("ok") for item in results)
    return {"ok": ok, "platform": tag, "verified": True, "hydrate": hydrate,
            "results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install bundled offline runtime")
    parser.add_argument("command", nargs="?", choices=["check", "hydrate", "install", "install-chrome"], default="install")
    args = parser.parse_args(argv)
    if args.command == "check":
        result = verify_bundle()
    elif args.command == "hydrate":
        try:
            result = hydrate_init_bundle()
        except (OSError, tarfile.TarError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
    elif args.command == "install-chrome":
        result = install_chrome()
    else:
        result = install()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
