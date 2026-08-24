#!/usr/bin/env python3
"""Inspect sellable module package boundaries."""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES_DIR = os.path.join(ROOT, "packages")
PACKAGE_IDS = ("oral-broadcast", "explainer", "brand-tvc")
PACKAGE_ENTRY_COMMANDS = {
    "oral-broadcast": "/oral-broadcast",
    "explainer": "/explainer-video",
    "brand-tvc": "/brand-tvc",
}
SHARED_COMMANDS = (
    "/video-auto-maker", "/setup", "/asset-prep", "/brand-kit",
    "/digital-human", "/text-anim",
)
# Compatibility prompts may describe an in-package subtype, but must never be
# advertised as a fourth sellable package or own a formal package id.
INTERNAL_SUBTYPE_COMMAND_PACKAGES = {
    "/product-showcase": ("brand-tvc",),
    "/quick-promo": ("brand-tvc",),
    "/product-demo": ("brand-tvc", "oral-broadcast"),
    "/oral-scene-service": ("explainer",),
}
OUT_OF_SCOPE_ADJACENT_COMMANDS = {
    "/oral-interview": "requires explicit upgrade/split-order confirmation",
    "/market-plan": "separate non-video strategy deliverable",
}
REQUIRED_FIELDS = (
    "id", "display_name", "sales_name", "version", "primary_entry",
    "included_commands", "shared_commands", "included_capabilities",
    "excluded_capabilities", "deliverables", "handoff_rules",
)


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _package_path(package_id):
    return os.path.join(PACKAGES_DIR, package_id, "package.json")


def load_package(package_id):
    if package_id not in PACKAGE_IDS:
        raise ValueError("UNKNOWN_MODULE_PACKAGE: %s" % package_id)
    path = _package_path(package_id)
    if not os.path.isfile(path):
        raise ValueError("MODULE_PACKAGE_MISSING: %s" % package_id)
    value = _load_json(path)
    if not isinstance(value, dict):
        raise ValueError("MODULE_PACKAGE_INVALID_JSON: %s" % package_id)
    return value


def load_all_packages():
    return [load_package(package_id) for package_id in PACKAGE_IDS]


def validate_packages():
    errors = []
    seen_entries = {}
    for package_id in PACKAGE_IDS:
        try:
            package = load_package(package_id)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for field in REQUIRED_FIELDS:
            if field not in package:
                errors.append("%s missing %s" % (package_id, field))
        if package.get("id") != package_id:
            errors.append("%s id mismatch: %s" % (package_id, package.get("id")))
        if package.get("primary_entry") != PACKAGE_ENTRY_COMMANDS[package_id]:
            errors.append("%s primary_entry must be %s" % (
                package_id, PACKAGE_ENTRY_COMMANDS[package_id]))
        if package.get("included_commands") != [PACKAGE_ENTRY_COMMANDS[package_id]]:
            errors.append("%s included_commands must contain only its primary entry" % package_id)
        if tuple(package.get("shared_commands") or ()) != SHARED_COMMANDS:
            errors.append("%s shared_commands drifted from shared-core" % package_id)
        entry = package.get("primary_entry")
        if entry in seen_entries:
            errors.append("%s duplicates primary_entry with %s" % (
                package_id, seen_entries[entry]))
        if entry:
            seen_entries[entry] = package_id
        for list_field in (
                "included_commands", "shared_commands", "included_capabilities",
                "excluded_capabilities", "deliverables", "handoff_rules"):
            if not isinstance(package.get(list_field), list) or not package.get(list_field):
                errors.append("%s %s must be a non-empty list" % (package_id, list_field))
        for rule in package.get("handoff_rules") or []:
            if not isinstance(rule, dict):
                errors.append("%s handoff rule must be object" % package_id)
                continue
            target = rule.get("target_package")
            if target not in PACKAGE_IDS:
                errors.append("%s handoff target invalid: %s" % (package_id, target))
    shared = os.path.join(PACKAGES_DIR, "shared-core.json")
    if not os.path.isfile(shared):
        errors.append("shared-core.json missing")
    else:
        shared_payload = _load_json(shared)
        if tuple(shared_payload.get("commands") or ()) != SHARED_COMMANDS:
            errors.append("shared-core commands drifted from canonical shared commands")

    prompt_roots = (
        os.path.join(ROOT, "prompts"),
        os.path.join(ROOT, ".codex", "prompts"),
    )
    classified = set(PACKAGE_ENTRY_COMMANDS.values()) | set(SHARED_COMMANDS)
    classified |= set(INTERNAL_SUBTYPE_COMMAND_PACKAGES)
    classified |= set(OUT_OF_SCOPE_ADJACENT_COMMANDS)
    for prompt_root in prompt_roots:
        if not os.path.isdir(prompt_root):
            errors.append("prompt root missing: %s" % os.path.relpath(prompt_root, ROOT))
            continue
        for filename in sorted(os.listdir(prompt_root)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(prompt_root, filename)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            first_line = text.splitlines()[0] if text.splitlines() else ""
            if not first_line.startswith("# /"):
                continue
            command = first_line[2:].split(None, 1)[0]
            if command not in classified:
                errors.append("unclassified command prompt: %s" %
                              os.path.relpath(path, ROOT))
                continue
            if command not in {"/setup", "/video-auto-maker"}:
                if "key_setup.py gate" not in text:
                    errors.append("%s skips the unified key gate" %
                                  os.path.relpath(path, ROOT))
                if "key_setup.py save --stdin" not in text:
                    errors.append("%s does not document stdin-only key save" %
                                  os.path.relpath(path, ROOT))
            if command in INTERNAL_SUBTYPE_COMMAND_PACKAGES:
                if "售卖边界" not in text:
                    errors.append("%s lacks subtype sales-boundary notice" %
                                  os.path.relpath(path, ROOT))
                for package_id in INTERNAL_SUBTYPE_COMMAND_PACKAGES[command]:
                    if PACKAGE_ENTRY_COMMANDS[package_id] not in text:
                        errors.append("%s does not route subtype to %s" % (
                            os.path.relpath(path, ROOT), package_id))
            if command in OUT_OF_SCOPE_ADJACENT_COMMANDS:
                if "售卖边界" not in text or "/video-auto-maker" not in text:
                    errors.append("%s does not stop and route adjacent scope to boundary triage" %
                                  os.path.relpath(path, ROOT))
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description="查看三个可售卖功能包的边界")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出所有售卖包")
    show = sub.add_parser("show", help="显示某个售卖包")
    show.add_argument("package_id", choices=PACKAGE_IDS)
    sub.add_parser("validate", help="校验 package.json 边界定义")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        print(json.dumps({
            "ok": True,
            "packages": [
                {
                    "id": package["id"],
                    "display_name": package["display_name"],
                    "primary_entry": package["primary_entry"],
                    "sales_name": package["sales_name"],
                }
                for package in load_all_packages()
            ],
            "shared_core": _load_json(os.path.join(PACKAGES_DIR, "shared-core.json")),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "show":
        print(json.dumps(load_package(args.package_id), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "validate":
        errors = validate_packages()
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
