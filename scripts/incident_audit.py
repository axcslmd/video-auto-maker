#!/usr/bin/env python3
"""Audit REAL_TEST_INCIDENTS.md against current workflow gates.

The incident ledger is useful only if it distinguishes code-level fixes from
workflow states that are still blocked by customer confirmation or paid
generation. This checker keeps that distinction machine-visible.
"""
import argparse
import json
import os
import re
import sys


INCIDENT_RE = re.compile(
    r"^### (?P<id>INC-\d+)：(?P<title>.*?)\n(?P<body>.*?)(?=^### INC-|\n## |\Z)",
    re.M | re.S,
)

DEEP_EVIDENCE_TERMS = (
    "新增", "回归测试", "测试覆盖", "已验证", "验证通过",
    "全绿", "单测", "用例", "OK", "证据", "覆盖",
)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def parse_incidents(text):
    incidents = []
    for match in INCIDENT_RE.finditer(text):
        body = match.group("body").strip()
        status = ""
        for line in body.splitlines():
            if "状态：" in line or "当前状态：" in line:
                status = line.strip()
        incidents.append({
            "id": match.group("id"),
            "title": match.group("title").strip(),
            "status": status,
            "has_temporary_fix": "临时修复" in body,
            "has_deep_evidence": any(term in body for term in DEEP_EVIDENCE_TERMS),
            "mentions_pending": "pending" in body or "待客户确认" in body,
            "mentions_generation_not_ready": "generation_ready=false" in body,
            "body": body,
        })
    return incidents


def _section(text, heading):
    pattern = re.compile(r"^## %s\n(?P<body>.*?)(?=^## |\Z)" %
                         re.escape(heading), re.M | re.S)
    match = pattern.search(text)
    return match.group("body").strip() if match else ""


def _subsection(text, heading):
    pattern = re.compile(r"^### %s\n(?P<body>.*?)(?=^### |\Z)" %
                         re.escape(heading), re.M | re.S)
    match = pattern.search(text)
    return match.group("body").strip() if match else ""


def _load_json(path):
    if not path:
        return None
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def audit(incidents_path, preflight_path=None):
    text = _read(incidents_path)
    incidents = parse_incidents(text)
    review = _section(text, "深度修复审计（2026-08-06）")
    open_section = (_subsection(review, "当前仍未闭环的事项") or
                    _subsection(review, "仍未闭环的事项")) if review else ""
    verification_section = _subsection(review, "本次审计验证") if review else ""

    errors = []
    if len(incidents) < 50:
        errors.append("INCIDENT_COUNT_TOO_LOW")
    if not review:
        errors.append("DEEP_FIX_AUDIT_SECTION_MISSING")
    if review and not open_section:
        errors.append("OPEN_ITEMS_SECTION_MISSING")
    if review and not verification_section:
        errors.append("AUDIT_VERIFICATION_SECTION_MISSING")

    incident_ids = {item["id"] for item in incidents}
    for required in ("INC-003", "INC-005", "INC-050", "INC-052", "INC-053"):
        if required not in incident_ids:
            errors.append("REQUIRED_INCIDENT_MISSING:%s" % required)

    open_incidents = sorted(set(re.findall(r"INC-\d+", open_section)))
    # Incident bodies intentionally retain historical failure evidence. Only
    # the current status line can describe a live workflow blocker; otherwise
    # an old `generation_ready=false` snapshot would reopen a resolved issue.
    workflow_open = sorted(item["id"] for item in incidents
                           if "generation_ready=false" in item["status"])
    temporary_fix_count = sum(1 for item in incidents if item["has_temporary_fix"])
    unresolved_temporary_fixes = sorted(
        item["id"] for item in incidents
        if item["has_temporary_fix"] and
        ("已处理" not in item["status"] or not item["has_deep_evidence"])
    )
    handled_count = sum(1 for item in incidents if "已处理" in item["status"])
    for incident_id in unresolved_temporary_fixes:
        errors.append("TEMP_FIX_WITHOUT_DEEP_EVIDENCE:%s" % incident_id)

    preflight = _load_json(preflight_path)
    preflight_summary = None
    if preflight is not None:
        next_codes = [item.get("code") for item in preflight.get("next_actions") or []]
        preflight_errors = preflight.get("errors") or []
        preflight_summary = {
            "path": os.path.abspath(preflight_path),
            "passed": preflight.get("passed"),
            "generation_ready": preflight.get("generation_ready"),
            "errors": preflight_errors,
            "next_action_codes": next_codes,
        }
        stale_handoff = any(code in preflight_errors for code in (
            "segments_no_declared_missing_images",
            "segments_cover_storyboard_plan",
            "prompt_review_covers_expected_shots",
        ))
        if stale_handoff and "CONFIRM_VIDEO_PROMPT_REVIEW" in next_codes:
            errors.append("STALE_HANDOFF_STILL_SUGGESTS_CONFIRM_VIDEO_PROMPT")
        if stale_handoff and "RECAPTURE_VIDEO_PROMPTS" not in next_codes:
            errors.append("STALE_HANDOFF_MISSING_RECAPTURE_VIDEO_PROMPTS")
        if stale_handoff and "REFRESH_APPROVAL_CHAIN" not in next_codes:
            errors.append("STALE_HANDOFF_MISSING_REFRESH_APPROVAL_CHAIN")

    report = {
        "ok": not errors,
        "incident_count": len(incidents),
        "handled_count": handled_count,
        "temporary_fix_count": temporary_fix_count,
        "unresolved_temporary_fix_incidents": unresolved_temporary_fixes,
        "deep_fix_audit_present": bool(review),
        "open_incidents_from_audit": open_incidents,
        "workflow_open_incidents": workflow_open,
        "preflight": preflight_summary,
        "errors": errors,
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit incident ledger state")
    parser.add_argument("--incidents", default="REAL_TEST_INCIDENTS.md")
    parser.add_argument("--preflight")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = audit(args.incidents, preflight_path=args.preflight)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
