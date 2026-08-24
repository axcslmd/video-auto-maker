#!/usr/bin/env python3
"""Fail-closed checks for the community source distribution.

This is a release engineering check, not legal advice. It prevents known
license regressions and accidental bundling of unaudited local runtimes.
"""
import argparse
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

LEGAL_FILES = {
    "LICENSE",
    "NOTICE",
    "COMMERCIAL_LICENSE.md",
    "TRADEMARKS.md",
    "CLA.md",
    "CONTRIBUTING.md",
    "THIRD_PARTY_NOTICES.md",
}
PROHIBITED_REQUIREMENTS = {
    "pymupdf",
    "imageio-ffmpeg",
    "static-ffmpeg",
}


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _requirement_name(line):
    value = line.split("#", 1)[0].strip().lower()
    for marker in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
        value = value.split(marker, 1)[0]
    return value.strip().replace("_", "-")


def audit(root=ROOT):
    root = Path(root).expanduser().resolve()
    errors = []
    warnings = []

    for name in sorted(LEGAL_FILES):
        if not (root / name).is_file():
            errors.append("LEGAL_FILE_MISSING:%s" % name)

    license_text = _read(root / "LICENSE")
    if "PolyForm Noncommercial License 1.0.0" not in license_text:
        errors.append("COMMUNITY_LICENSE_INVALID")
    notice_text = _read(root / "NOTICE")
    required_notice = (
        "Required Notice: Copyright 2026 AI 营销数字员工项目作者（个人开发者）. "
        "Commercial licensing: 359971043@qq.com"
    )
    if required_notice not in notice_text:
        errors.append("REQUIRED_NOTICE_INVALID")

    for filename in ("requirements.txt", "requirements.lock.txt"):
        path = root / filename
        for line_number, line in enumerate(_read(path).splitlines(), 1):
            name = _requirement_name(line)
            if name in PROHIBITED_REQUIREMENTS:
                errors.append("PROHIBITED_REQUIREMENT:%s:%s:%s" % (
                    filename, line_number, name))

    runtime_patterns = (
        "import " + "fitz",
        "from " + "fitz" + " import",
        "import " + "imageio" + "_ffmpeg",
        "from " + "imageio" + "_ffmpeg",
        "import " + "static" + "_ffmpeg",
        "from " + "static" + "_ffmpeg",
    )
    scan_roots = (root / "scripts", root / "remotion_engine" / "src")
    for scan_root in scan_roots:
        if not scan_root.is_dir():
            continue
        for path in scan_root.rglob("*"):
            if path.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".mjs"}:
                continue
            text = _read(path)
            for pattern in runtime_patterns:
                if pattern in text:
                    errors.append("PROHIBITED_RUNTIME_IMPORT:%s:%s" % (
                        path.relative_to(root).as_posix(), pattern))

    vendor_root = root / "remotion_engine" / "src" / "vendor"
    if vendor_root.is_dir():
        files = [path for path in vendor_root.rglob("*") if path.is_file()]
        if files:
            errors.append("UNATTRIBUTED_VENDOR_CODE_PRESENT:%s" % len(files))

    delivery_text = _read(root / "scripts" / "package_delivery.py")
    if "LOCAL_RUNTIME_REDISTRIBUTION_DISABLED" not in delivery_text:
        errors.append("LOCAL_RUNTIME_REDISTRIBUTION_NOT_BLOCKED")
    if '"offline-assets"' not in delivery_text or '"node_modules"' not in delivery_text:
        errors.append("COMMUNITY_BINARY_EXCLUSIONS_MISSING")

    third_party = _read(root / "THIRD_PARTY_NOTICES.md")
    for marker in ("pypdf", "Remotion", "MPL", "FFmpeg", "Chromium"):
        if marker not in third_party:
            errors.append("THIRD_PARTY_NOTICE_INCOMPLETE:%s" % marker)

    if (root / "offline-assets").exists():
        warnings.append(
            "LOCAL_RUNTIME_PRESENT_BUT_EXCLUDED: do not publish offline-assets/ "
            "without a separate binary-license audit"
        )

    return {
        "ok": not errors,
        "root": str(root),
        "license": "PolyForm-Noncommercial-1.0.0",
        "commercial_contact": "359971043@qq.com",
        "errors": errors,
        "warnings": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check"])
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    result = audit(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
