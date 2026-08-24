#!/usr/bin/env python3
"""Build a clean, portable customer delivery bundle.

The source workspace may contain customer assets, rendered media, local Agent
state, and platform-specific caches. This command copies only the reusable
application and its dependency manifests into a sibling delivery directory,
then creates a zip archive and a SHA-256 manifest.

Usage:
  python3 scripts/package_delivery.py --out-dir ../delivery
  python3 scripts/package_delivery.py --out-dir ../delivery --include-client acme
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import time
import zipfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from project_utils import atomic_write_json  # noqa: E402

# Keep this list deliberately conservative. Customer media is opt-in; the
# generic bundle must remain brand- and run-independent.
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "output",
    "node_modules", ".remotion", ".cache", "__pycache__", ".pytest_cache",
    ".workbuddy", ".codex-tmp", "unittest_client_", "offline-assets", ".DS_Store",
}
# Agent prompt/command folders are project source and must ship. Only CI and
# generated distribution output are excluded at the project root.
EXCLUDED_ROOTS = {".github", "dist", "packaged", "reports"}
EXCLUDED_FILE_NAMES = {
    ".DS_Store", ".env", ".env.local", ".env.production",
    "delivery.json", "basicrouter_key.json", ".basicrouter_key",
    "DELIVERY_MANIFEST.json", "RELEASE_VALIDATION.json",
}
EXCLUDED_RELATIVE_FILES = {
    "scripts/basicware_visual_redesign.py",
    # Internal research, incident and remediation notes are source-team
    # evidence, not customer runtime documentation. Keep operational guides
    # under docs/ and every prompts/skills reference.
    "Harness工程视角审视报告.md",
    "OPTIMIZATION_REPORT.md",
    "REAL_TEST_INCIDENTS.md",
    "代码级修复与业务优化建议.md",
    "工作流优化与幻觉风险控制报告.md",
    "监控问题根因汇总.md",
    "监控问题详细报告.md",
    "项目测试体系深度评估与提效方案_2026-08-17.md",
    "项目深度分析报告.md",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".mp4", ".mov", ".webm"}
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
SECRET_TEXT_SUFFIXES = {
    ".cfg", ".conf", ".css", ".env", ".html", ".ini", ".js", ".json",
    ".jsx", ".md", ".mjs", ".ps1", ".py", ".sh", ".toml", ".ts",
    ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
SECRET_TEXT_NAMES = {"dockerfile", "makefile", "procfile"}
SECRET_SCAN_CHUNK_CHARS = 1024 * 1024
ZIP_DEFAULT_EPOCH = 946684800  # 2000-01-01 UTC, inside the ZIP timestamp range.
ZIP_MAX_ENTRIES = 500000
ZIP_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024 * 1024
LAYOUT_CONTRACT = "agent-project-layout.v1"
REQUIRED_ROOT_DIRS = {
    ".codex", ".kilo", ".workbuddy", "adapters", "actors", "assets",
    "brand", "docs", "evals", "packages", "policies", "prompts",
    "references", "remotion_engine", "schemas", "scripts", "skills", "tests",
}
REQUIRED_ROOT_FILES = {
    ".gitattributes", ".gitignore", "AGENTS.md", "AGENT_ENTRY_PROTOCOL.md",
    "README.md", "SECURITY.md",
    "LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md", "TRADEMARKS.md", "CLA.md",
    "CONTRIBUTING.md", "THIRD_PARTY_NOTICES.md",
    "START_HERE_AGENT.md", "START_HERE_AGENT.py", "deploy.ps1", "deploy.sh",
    "pyproject.toml", "requirements.lock.txt", "requirements.txt",
    "runtime-manifest.json", "DELIVERY_MANIFEST.json", "RELEASE_VALIDATION.json",
}
FORBIDDEN_LEGACY_ROOT_FILES = {
    "CLIENT_SETUP.md", "CUSTOMER_GUIDE.md", "DELIVERY_CHECKLIST.md",
    "DELIVERY_PACKAGE.md", "PROJECT_OVERVIEW.md", "START_HERE_HERMES.md",
    "START_HERE_WORKBUDDY.md", "project-capabilities.html",
}


def _is_excluded(path: Path, relative: Path, include_local_runtime=False) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if relative.as_posix() in EXCLUDED_RELATIVE_FILES:
        return True
    if len(parts) == 1 and parts[0] in FORBIDDEN_LEGACY_ROOT_FILES:
        return True
    if parts[0] in EXCLUDED_ROOTS:
        return True
    protected_runtime = include_local_runtime and (
        parts[0] == "offline-assets" or
        len(parts) >= 2 and parts[:2] == ("remotion_engine", "node_modules"))
    # Ship only the project-authored WorkBuddy adapter. Other .workbuddy
    # content is local task/config state and must remain excluded.
    protected_workbuddy_source = (
        len(parts) >= 3 and parts[:3] ==
        (".workbuddy", "skills", "video-auto-maker"))
    if any((part in EXCLUDED_DIRS and
            not (protected_runtime and part in {"node_modules", "offline-assets", "output"}) and
            not (protected_workbuddy_source and part == ".workbuddy")) or
           part.startswith("unittest_client_") for part in parts):
        # Keep the empty public directory available to Remotion, but remove
        # every generated render file under it.
        if relative.as_posix() == "remotion_engine/public" and path.is_dir():
            return False
        return True
    if (len(parts) >= 2 and parts[:2] == ("remotion_engine", "public")
            and path.name == "public" and path.is_dir()):
        return False
    if (len(parts) >= 2 and parts[:2] == ("remotion_engine", "public")
            and path.name != ".gitkeep"):
        return True
    name = path.name
    if (parts[0] == "remotion_engine" and
            re.fullmatch(r"_props(?:_[A-Za-z0-9_]+)?\.json", name)):
        return True
    if name in EXCLUDED_FILE_NAMES or name.endswith(".DS_Store"):
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    return False


def _contains_secret(path: Path) -> bool:
    """Scan packageable text without returning or logging matched content."""
    name = path.name.lower()
    if (path.suffix.lower() not in SECRET_TEXT_SUFFIXES and
            name not in SECRET_TEXT_NAMES and not name.startswith(".env")):
        return False
    tail = ""
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        while True:
            chunk = handle.read(SECRET_SCAN_CHUNK_CHARS)
            if not chunk:
                return False
            candidate = tail + chunk
            if SECRET_RE.search(candidate):
                return True
            # Retain enough text to detect a token crossing a chunk boundary.
            tail = candidate[-256:]


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _validate_symlink(path: Path, root: Path):
    target = os.readlink(path)
    if os.path.isabs(target):
        raise RuntimeError("absolute symlink found in %s; refusing to package" % path)
    resolved_target = (path.parent / target).resolve(strict=False)
    if not _path_within(resolved_target, root):
        raise RuntimeError("escaping symlink found in %s; refusing to package" % path)


def _copy_tree(destination: Path, include_client=None, include_local_runtime=False):
    copied = []
    skipped = []
    for source in ROOT.rglob("*"):
        relative = source.relative_to(ROOT)
        if _is_excluded(source, relative, include_local_runtime=include_local_runtime):
            skipped.append(relative.as_posix())
            continue
        if include_client is None and relative.parts and relative.parts[0] in {"assets", "brand", "actors"}:
            # Templates are useful to the client; demo/customer data is not.
            if len(relative.parts) > 1 and relative.parts[1] != "_template":
                skipped.append(relative.as_posix())
                continue
        if include_client and relative.parts and relative.parts[0] in {"assets", "brand", "actors"}:
            if len(relative.parts) > 1 and relative.parts[1] not in {"_template", include_client}:
                skipped.append(relative.as_posix())
                continue
        target = destination / relative
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            _validate_symlink(source, ROOT)
            link_target = os.readlink(source)
            target.symlink_to(link_target)
            copied.append(relative.as_posix())
            continue
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        try:
            secret_found = _contains_secret(source)
        except OSError as exc:
            raise RuntimeError(
                "secret scan failed for %s; refusing to package" % relative) from exc
        if secret_found:
            raise RuntimeError("secret-like token found in %s; refusing to package" % relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            link_target = os.readlink(source)
            if os.path.isabs(link_target):
                raise RuntimeError("absolute symlink found in %s; refusing to package" % relative)
            target.symlink_to(link_target)
        else:
            shutil.copy2(source, target)
        copied.append(relative.as_posix())
    return copied, skipped


def _sha256(path: Path) -> str:
    if path.is_symlink():
        return hashlib.sha256(("SYMLINK\0" + os.readlink(path)).encode("utf-8")).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _offline_runtime_metadata():
    import offline_setup
    verification = offline_setup.verify_bundle()
    if not verification.get("ok"):
        raise RuntimeError("offline runtime verification failed: %s" %
                           verification.get("error"))
    with (ROOT / "remotion_engine" / "package.json").open(encoding="utf-8") as handle:
        package = json.load(handle)
    return {
        "verified": True,
        "platform": verification.get("platform"),
        "python": verification.get("python"),
        "remotion": (package.get("dependencies") or {}).get("remotion"),
        "artifact_manifest": "offline-assets/artifact-manifest.json",
    }


def _write_delivery_manifest(destination: Path, copied, skipped, include_client,
                             include_local_runtime, runtime_metadata=None,
                             release_validation=None):
    files = []
    for relative in sorted(copied):
        path = destination / relative
        size = len(os.readlink(path).encode("utf-8")) if path.is_symlink() else path.stat().st_size
        files.append({"path": relative, "bytes": size, "sha256": _sha256(path),
                      "symlink": path.is_symlink()})
    payload = {
        "schema": 2 if release_validation else 1,
        "kind": "customer_delivery_bundle",
        "generated_from": ROOT.name,
        "customer_assets_included": include_client,
        "source_media_excluded": True,
        "platform_caches_excluded": True,
        "local_remotion_runtime_included": include_local_runtime,
        "offline_assets_included": include_local_runtime,
        "offline_runtime": runtime_metadata,
        "release_validation": release_validation,
        "dependency_install": {
            "python": "requirements.lock.txt",
            "node": "remotion_engine/package-lock.json",
            "installer_macos_linux": "./deploy.sh",
            "installer_windows": "powershell -ExecutionPolicy Bypass -File .\\deploy.ps1",
        },
        "files": files,
        "excluded_count": len(skipped),
    }
    if release_validation:
        payload["layout_contract"] = LAYOUT_CONTRACT
    target = destination / "DELIVERY_MANIFEST.json"
    atomic_write_json(str(target), payload)
    return target


def _zip_timestamp():
    try:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", ZIP_DEFAULT_EPOCH))
    except ValueError:
        epoch = ZIP_DEFAULT_EPOCH
    epoch = max(315532800, min(epoch, 4354819199))
    value = time.gmtime(epoch)
    return (value.tm_year, value.tm_mon, value.tm_mday,
            value.tm_hour, value.tm_min, value.tm_sec - value.tm_sec % 2)


def _zip_directory(source: Path, archive: Path):
    """Create a deterministic archive with explicit modes and timestamps."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    timestamp = _zip_timestamp()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as handle:
        for path in sorted(source.rglob("*")):
            if path.is_file() or path.is_symlink():
                archive_name = (Path(source.name) / path.relative_to(source)).as_posix()
                info = zipfile.ZipInfo(archive_name, date_time=timestamp)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                if path.is_symlink():
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    data = os.readlink(path).encode("utf-8")
                else:
                    mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    with path.open("rb") as source_handle, \
                            handle.open(info, "w", force_zip64=True) as archive_handle:
                        shutil.copyfileobj(source_handle, archive_handle, 1024 * 1024)
                    continue
                handle.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED,
                                compresslevel=6)


def _zip_member_path(name):
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise ValueError("ARCHIVE_MEMBER_NAME_INVALID")
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError("ARCHIVE_MEMBER_PATH_INVALID:%s" % name)
    return candidate


def _zip_member_is_symlink(info):
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def _zip_text_contains_secret(handle, info):
    relative = Path(info.filename)
    name = relative.name.lower()
    if (relative.suffix.lower() not in SECRET_TEXT_SUFFIXES and
            name not in SECRET_TEXT_NAMES and not name.startswith(".env")):
        return False
    tail = ""
    decoder = io.TextIOWrapper(handle.open(info, "r"), encoding="utf-8", errors="ignore")
    try:
        while True:
            chunk = decoder.read(SECRET_SCAN_CHUNK_CHARS)
            if not chunk:
                return False
            candidate = tail + chunk
            if SECRET_RE.search(candidate):
                return True
            tail = candidate[-256:]
    finally:
        decoder.close()


def verify_archive(archive, expected_directory=None):
    """Verify ZIP paths, members, hashes, secrets, and delivery manifest."""
    archive_path = Path(archive).expanduser().resolve()
    errors = []
    if not archive_path.is_file():
        return {"ok": False, "archive": str(archive_path),
                "errors": ["DELIVERY_ARCHIVE_MISSING"]}
    try:
        with zipfile.ZipFile(archive_path, "r") as handle:
            infos = handle.infolist()
            if len(infos) > ZIP_MAX_ENTRIES:
                errors.append("ARCHIVE_ENTRY_LIMIT_EXCEEDED")
            if sum(item.file_size for item in infos) > ZIP_MAX_UNCOMPRESSED_BYTES:
                errors.append("ARCHIVE_SIZE_LIMIT_EXCEEDED")
            names = []
            roots = set()
            for info in infos:
                try:
                    member = _zip_member_path(info.filename)
                    roots.add(member.parts[0])
                    names.append(member.as_posix())
                except ValueError as exc:
                    errors.append(str(exc))
                if info.flag_bits & 0x1:
                    errors.append("ARCHIVE_ENCRYPTED_MEMBER:%s" % info.filename)
            if len(names) != len(set(names)):
                errors.append("ARCHIVE_DUPLICATE_MEMBER")
            if len(roots) != 1:
                errors.append("ARCHIVE_ROOT_INVALID")
            root_name = next(iter(roots), "")
            if expected_directory and root_name != Path(expected_directory).resolve().name:
                errors.append("ARCHIVE_ROOT_MISMATCH")
            manifest_name = "%s/DELIVERY_MANIFEST.json" % root_name
            by_name = {item.filename: item for item in infos}
            manifest_info = by_name.get(manifest_name)
            if not manifest_info:
                errors.append("ARCHIVE_DELIVERY_MANIFEST_MISSING")
                payload = {}
            else:
                try:
                    payload = json.loads(handle.read(manifest_info).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, OSError):
                    payload = {}
                    errors.append("ARCHIVE_DELIVERY_MANIFEST_INVALID")
            expected = {}
            for record in payload.get("files") or []:
                relative = str(record.get("path") or "")
                try:
                    _zip_member_path(relative)
                except ValueError:
                    errors.append("ARCHIVE_MANIFEST_PATH_INVALID:%s" % relative)
                    continue
                if relative in expected:
                    errors.append("ARCHIVE_MANIFEST_PATH_INVALID:%s" % relative)
                    continue
                expected[relative] = record
            actual = set()
            for info in infos:
                if info.is_dir() or info.filename == manifest_name:
                    continue
                try:
                    member = _zip_member_path(info.filename)
                except ValueError:
                    continue
                if not root_name or member.parts[0] != root_name or len(member.parts) < 2:
                    errors.append("ARCHIVE_MEMBER_OUTSIDE_ROOT:%s" % info.filename)
                    continue
                relative = Path(*member.parts[1:]).as_posix()
                actual.add(relative)
                record = expected.get(relative)
                if record is None:
                    continue
                is_symlink = _zip_member_is_symlink(info)
                if bool(record.get("symlink")) != is_symlink:
                    errors.append("ARCHIVE_SYMLINK_MISMATCH:%s" % relative)
                    continue
                if is_symlink:
                    try:
                        target = handle.read(info).decode("utf-8")
                    except (UnicodeDecodeError, OSError):
                        errors.append("ARCHIVE_SYMLINK_TARGET_INVALID:%s" % relative)
                        continue
                    link_target = Path(target)
                    resolved = Path(os.path.normpath(str(Path(relative).parent / link_target)))
                    if link_target.is_absolute() or ".." in resolved.parts:
                        errors.append("ARCHIVE_SYMLINK_ESCAPES_ROOT:%s" % relative)
                    digest = hashlib.sha256(("SYMLINK\0" + target).encode("utf-8")).hexdigest()
                    size = len(target.encode("utf-8"))
                else:
                    digest_object = hashlib.sha256()
                    size = 0
                    with handle.open(info, "r") as member_handle:
                        for block in iter(lambda: member_handle.read(1024 * 1024), b""):
                            size += len(block)
                            digest_object.update(block)
                    digest = digest_object.hexdigest()
                    try:
                        if _zip_text_contains_secret(handle, info):
                            errors.append("ARCHIVE_SECRET_FOUND:%s" % relative)
                    except OSError:
                        errors.append("ARCHIVE_SECRET_SCAN_FAILED:%s" % relative)
                if record.get("bytes") != size:
                    errors.append("ARCHIVE_SIZE_MISMATCH:%s" % relative)
                if record.get("sha256") != digest:
                    errors.append("ARCHIVE_SHA256_MISMATCH:%s" % relative)
            for relative in sorted(set(expected) - actual):
                errors.append("ARCHIVE_FILE_MISSING:%s" % relative)
            for relative in sorted(actual - set(expected)):
                errors.append("ARCHIVE_FILE_UNDECLARED:%s" % relative)
            if payload.get("schema") == 2:
                if payload.get("layout_contract") != LAYOUT_CONTRACT:
                    errors.append("ARCHIVE_LAYOUT_CONTRACT_INVALID")
                for relative in sorted(actual):
                    parts = Path(relative).parts
                    if len(parts) == 1 and relative not in REQUIRED_ROOT_FILES:
                        errors.append("ARCHIVE_LAYOUT_UNEXPECTED_ROOT_FILE:%s" % relative)
                    if (len(parts) == 2 and parts[0] == "remotion_engine" and
                            re.fullmatch(r"_props(?:_[A-Za-z0-9_]+)?\.json", parts[1])):
                        errors.append("ARCHIVE_LAYOUT_GENERATED_RENDER_INPUT:%s" % relative)
                if len([item for item in actual
                        if len(Path(item).parts) == 2 and
                        Path(item).parts[0] == "tests" and
                        Path(item).name.startswith("test_") and item.endswith(".py")]) < 2:
                    errors.append("ARCHIVE_LAYOUT_TEST_SUITE_INCOMPLETE")
                for relative in (
                        "docs/customer/CUSTOMER_GUIDE.md",
                        "docs/operations/DELIVERY_CHECKLIST.md",
                        "docs/architecture/PROJECT_OVERVIEW.md",
                        "adapters/hermes/START_HERE.md",
                        "adapters/workbuddy/START_HERE.md"):
                    if relative not in actual:
                        errors.append("ARCHIVE_LAYOUT_DOCUMENT_MISSING:%s" % relative)
            if expected_directory:
                disk_manifest = Path(expected_directory).resolve() / "DELIVERY_MANIFEST.json"
                if not disk_manifest.is_file():
                    errors.append("ARCHIVE_EXPECTED_MANIFEST_MISSING")
                else:
                    try:
                        disk_payload = json.loads(disk_manifest.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        disk_payload = None
                    if payload != disk_payload:
                        errors.append("ARCHIVE_MANIFEST_MISMATCH")
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        errors.append("DELIVERY_ARCHIVE_INVALID:%s" % type(exc).__name__)
    return {"ok": not errors, "archive": str(archive_path),
            "sha256": _sha256(archive_path), "errors": errors}


def validate_delivery_shape(directory, *, include_local_runtime=False):
    """Enforce the public Agent-project layout independently of file hashes."""
    root = Path(directory).expanduser().resolve()
    errors = []
    actual_root = {path.name for path in root.iterdir()} if root.is_dir() else set()
    required_dirs = set(REQUIRED_ROOT_DIRS)
    if include_local_runtime:
        required_dirs.add("offline-assets")
    required_files = set(REQUIRED_ROOT_FILES)
    for name in sorted(required_dirs):
        if not (root / name).is_dir():
            errors.append("LAYOUT_REQUIRED_DIRECTORY_MISSING:%s" % name)
    for name in sorted(required_files):
        if not (root / name).is_file():
            errors.append("LAYOUT_REQUIRED_FILE_MISSING:%s" % name)
    allowed = required_dirs | required_files
    for name in sorted(actual_root - allowed):
        errors.append("LAYOUT_UNEXPECTED_ROOT_ENTRY:%s" % name)
    for name in sorted(FORBIDDEN_LEGACY_ROOT_FILES & actual_root):
        errors.append("LAYOUT_LEGACY_ROOT_FILE:%s" % name)
    generated_props = sorted(
        path.name for path in (root / "remotion_engine").glob("_props*.json"))
    for name in generated_props:
        errors.append("LAYOUT_GENERATED_RENDER_INPUT:%s" % name)
    test_files = sorted((root / "tests").glob("test_*.py"))
    if len(test_files) < 2:
        errors.append("LAYOUT_TEST_SUITE_INCOMPLETE")
    for relative in (
            "docs/customer/CUSTOMER_GUIDE.md",
            "docs/operations/DELIVERY_CHECKLIST.md",
            "docs/architecture/PROJECT_OVERVIEW.md",
            "adapters/hermes/START_HERE.md",
            "adapters/workbuddy/START_HERE.md"):
        if not (root / relative).is_file():
            errors.append("LAYOUT_DOCUMENT_MISSING:%s" % relative)
    if include_local_runtime and not (root / "remotion_engine" / "node_modules").is_dir():
        errors.append("LAYOUT_LOCAL_NODE_RUNTIME_MISSING")
    return {"ok": not errors, "contract": LAYOUT_CONTRACT,
            "root_entries": len(actual_root), "errors": errors}


def verify_delivery(directory):
    """Verify a built delivery directory against its manifest, fail-closed."""
    root = Path(directory).expanduser().resolve()
    manifest_path = root / "DELIVERY_MANIFEST.json"
    errors = []
    if not manifest_path.is_file():
        return {"ok": False, "directory": str(root),
                "errors": ["DELIVERY_MANIFEST_MISSING"]}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "directory": str(root),
                "errors": ["DELIVERY_MANIFEST_INVALID:%s" % exc]}
    if payload.get("schema") not in (1, 2) or payload.get("kind") != "customer_delivery_bundle":
        errors.append("DELIVERY_MANIFEST_CONTRACT_INVALID")
    layout = None
    if payload.get("schema") == 2:
        if payload.get("layout_contract") != LAYOUT_CONTRACT:
            errors.append("DELIVERY_LAYOUT_CONTRACT_INVALID")
        layout = validate_delivery_shape(
            root, include_local_runtime=bool(payload.get("local_remotion_runtime_included")))
        errors.extend(layout["errors"])
    expected = {}
    for record in payload.get("files") or []:
        relative = str(record.get("path") or "")
        rel_path = Path(relative)
        if (not relative or rel_path.is_absolute() or ".." in rel_path.parts
                or relative in expected):
            errors.append("DELIVERY_MANIFEST_PATH_INVALID:%s" % relative)
            continue
        expected[relative] = record
    release_validation = payload.get("release_validation")
    if release_validation is not None:
        if (not isinstance(release_validation, dict) or
                release_validation.get("path") != "RELEASE_VALIDATION.json" or
                release_validation.get("sha256") !=
                (expected.get("RELEASE_VALIDATION.json") or {}).get("sha256")):
            errors.append("DELIVERY_RELEASE_VALIDATION_BINDING_INVALID")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink()) and path != manifest_path
    }
    for relative in sorted(set(expected) - actual):
        errors.append("DELIVERY_FILE_MISSING:%s" % relative)
    for relative in sorted(actual - set(expected)):
        errors.append("DELIVERY_FILE_UNDECLARED:%s" % relative)
    for relative in sorted(actual & set(expected)):
        path = root / relative
        record = expected[relative]
        if not _path_within(path.parent, root):
            errors.append("DELIVERY_PATH_ESCAPES_ROOT:%s" % relative)
            continue
        if path.is_symlink():
            try:
                _validate_symlink(path, root)
            except RuntimeError:
                errors.append("DELIVERY_SYMLINK_INVALID:%s" % relative)
                continue
        actual_symlink = path.is_symlink()
        if bool(record.get("symlink")) != actual_symlink:
            errors.append("DELIVERY_SYMLINK_MISMATCH:%s" % relative)
        size = len(os.readlink(path).encode("utf-8")) if actual_symlink else path.stat().st_size
        if record.get("bytes") != size:
            errors.append("DELIVERY_SIZE_MISMATCH:%s" % relative)
        if record.get("sha256") != _sha256(path):
            errors.append("DELIVERY_SHA256_MISMATCH:%s" % relative)
        if not actual_symlink:
            try:
                if _contains_secret(path):
                    errors.append("DELIVERY_SECRET_FOUND:%s" % relative)
            except OSError:
                errors.append("DELIVERY_SECRET_SCAN_FAILED:%s" % relative)
    forbidden = sorted(relative for relative in actual
                       if Path(relative).parts and Path(relative).parts[0] in EXCLUDED_ROOTS)
    for relative in forbidden:
        errors.append("DELIVERY_FORBIDDEN_FILE:%s" % relative)
    return {
        "ok": not errors,
        "directory": str(root),
        "declared_files": len(expected),
        "actual_files": len(actual),
        "layout": layout,
        "errors": errors,
    }


def _validated_release_report(path):
    if not path:
        return None, None
    import release_audit
    report_path = Path(path).expanduser().resolve()
    result = release_audit.validate_report(report_path, require_current=True)
    if not result["ok"]:
        raise RuntimeError("release validation failed: %s" % ", ".join(result["errors"]))
    return report_path, {
        "path": "RELEASE_VALIDATION.json",
        "sha256": _sha256(report_path),
        "project_fingerprint": result["project_fingerprint"],
    }


def build(out_dir, include_client=None, include_local_runtime=False,
          validation_report=None):
    if include_local_runtime:
        raise RuntimeError(
            "LOCAL_RUNTIME_REDISTRIBUTION_DISABLED: community releases must not "
            "bundle offline-assets, node_modules, FFmpeg, Chromium, or other "
            "unaudited binaries; build the source-only package instead")
    import license_compliance
    compliance = license_compliance.audit(ROOT)
    if not compliance["ok"]:
        raise RuntimeError("license compliance failed: %s" %
                           ", ".join(compliance["errors"]))
    destination = Path(out_dir).expanduser().resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise RuntimeError("delivery output must be outside the source project")
    archive = destination.parent / (destination.name + ".zip")
    if os.path.lexists(str(destination)):
        raise RuntimeError(
            "delivery destination already exists; choose a new unique --out-dir: %s" %
            destination)
    if os.path.lexists(str(archive)):
        raise RuntimeError(
            "delivery archive already exists; choose a new unique --out-dir: %s" %
            archive)
    destination.mkdir(parents=True)
    report_path, report_record = _validated_release_report(validation_report)
    runtime_metadata = _offline_runtime_metadata() if include_local_runtime else None
    copied, skipped = _copy_tree(
        destination, include_client=include_client,
        include_local_runtime=include_local_runtime)
    if report_path:
        shutil.copy2(report_path, destination / "RELEASE_VALIDATION.json")
        copied.append("RELEASE_VALIDATION.json")
    manifest = _write_delivery_manifest(
        destination, copied, skipped, include_client, include_local_runtime,
        runtime_metadata=runtime_metadata, release_validation=report_record)
    verification = verify_delivery(destination)
    if not verification["ok"]:
        raise RuntimeError("delivery verification failed: %s" % ", ".join(verification["errors"]))
    _zip_directory(destination, archive)
    archive_verification = verify_archive(archive, expected_directory=destination)
    if not archive_verification["ok"]:
        raise RuntimeError("archive verification failed: %s" %
                           ", ".join(archive_verification["errors"]))
    return {
        "directory": str(destination),
        "archive": str(archive),
        "files": len(copied),
        "excluded": len(skipped),
        "manifest": str(manifest),
        "bytes": archive.stat().st_size,
        "sha256": _sha256(archive),
        "verification": verification,
        "archive_verification": archive_verification,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out-dir", help="sibling directory for the delivery bundle")
    action.add_argument("--verify", help="verify an existing delivery directory and its manifest")
    action.add_argument("--verify-archive", help="verify a delivery ZIP without extracting it")
    parser.add_argument("--include-client", help="optional client slug to include as customer seed data")
    parser.add_argument(
        "--include-local-runtime", action="store_true",
        help="disabled for community releases; third-party binaries require a separately audited distribution")
    parser.add_argument("--validation-report",
                        help="current RELEASE_VALIDATION.json produced by release_audit.py")
    args = parser.parse_args()
    if args.verify:
        result = verify_delivery(args.verify)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["ok"] else 1)
    if args.verify_archive:
        result = verify_archive(args.verify_archive)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["ok"] else 1)
    print(json.dumps(build(
        args.out_dir, include_client=args.include_client,
        include_local_runtime=args.include_local_runtime,
        validation_report=args.validation_report),
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
