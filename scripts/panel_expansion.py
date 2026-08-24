#!/usr/bin/env python3
"""Approved storyboard-panel expansion, QA, review, and delivery reports.

This module deliberately sits between storyboard approval and paid video work:
contact sheet -> expanded panel -> automated visual QA -> customer approval ->
video submission.  It is host-agent-neutral and persists only BASICROUTER
artifacts and hashes, never credentials or signed URLs.
"""
import argparse
import html
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import artifact_contract
import br_client
import key_setup
import storyboard
import run_manifest as run_manifest_store
from project_utils import atomic_write_bytes, atomic_write_json

PANEL_QA_CHECKS = (
    "photorealistic", "no_grid_or_text", "identity", "product",
    "interaction", "composition", "no_extra_people",
)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    atomic_write_json(path, value)


def risk_profile(segment):
    """Flag shots that require especially strict panel QA."""
    tags = set(segment.get("ref_tags") or [])
    text = " ".join(str(segment.get(key) or "") for key in (
        "visual", "action", "character_action", "shot_size", "prop_prompts", "text"))
    reasons = []
    if {"@host", "@product"}.issubset(tags):
        reasons.append("human_product_interaction")
    if "@usage" in tags:
        reasons.append("usage_anchor")
    if any(term in text.lower() for term in ("特写", "close-up", "手部", "hand", "接口", "logo", "细节")):
        reasons.append("fine_detail")
    if str(segment.get("panel_index") or segment.get("storyboard_panel_index")) in ("1", "0"):
        reasons.append("opening_key_visual")
    return {"high_risk": bool(reasons), "reasons": reasons,
            "requires_strict_qa": bool(reasons)}


def reference_budget(segment, *, include_tail=False):
    """Make every provider image slot explicit; never truncate confirmed refs."""
    bindings = list(segment.get("reference_bindings") or [])
    if not bindings:
        bindings = [{"tag": tag, "role": "identity_anchor", "must_be_visible": True}
                    for tag in (segment.get("ref_tags") or [])]
    selected = [{"role": "composition_anchor", "tag": "@panel", "must_be_visible": True}]
    selected.extend(bindings)
    if include_tail:
        selected.insert(0, {"role": "continuity_anchor", "tag": "@tail", "must_be_visible": True})
    if len(selected) > 4:
        raise ValueError("REFERENCE_BUDGET_EXCEEDED: 镜头 %s 需要 %d 张强制参考图，超过模型上限 4" %
                         (segment.get("id"), len(selected)))
    return {"max_images": 4, "selected": selected, "count": len(selected)}


def assess_panel(api_key, segment, panel_path):
    """Online visual QA. A malformed response is a blocked result, not approval."""
    tags = " ".join(segment.get("ref_tags") or [])
    question = ("审查这张单格展开图。只返回 JSON："
                '{"pass":true|false,"score":0-100,"issues":[...],"checks":{'
                '"photorealistic":true,"no_grid_or_text":true,"identity":true,'
                '"product":true,"interaction":true,"composition":true,"no_extra_people":true}}。'
                "它必须是干净的真实商业画面，不含故事板格线、素描、箭头、字幕、水印或新增人物；"
                "必须符合镜头 %s 的 %s 绑定和动作：%s。" %
                (segment.get("storyboard_panel_index"), tags, segment.get("text") or ""))
    raw = br_client.analyze_image(api_key, panel_path, question)
    try:
        start, end = raw.find("{"), raw.rfind("}")
        result = json.loads(raw[start:end + 1])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"pass": False, "score": 0, "issues": ["VISION_QA_INVALID_JSON: %s" % exc],
                "raw": raw}
    checks = result.get("checks") or {}
    passed = bool(result.get("pass")) and int(result.get("score") or 0) >= 80 and all(
        checks.get(name) is True for name in PANEL_QA_CHECKS)
    result.update({"pass": passed, "model": br_client.pick_vision_model(), "raw": raw})
    return result


def prepare(segments, *, api_key, out_dir, qa=True):
    prepared = []
    for segment in segments:
        if not segment.get("storyboard_ref"):
            prepared.append(segment)
            continue
        panel = storyboard.expand_storyboard_panel(api_key, segment, out_dir=out_dir)
        recipe = artifact_contract.build_storyboard_panel_recipe(segment)
        recipe_sha = artifact_contract.sha256_json(recipe)
        if panel.get("recipe_sha256") != recipe_sha:
            raise ValueError("STALE_STORYBOARD_PANEL: %s" % segment.get("id"))
        item = dict(segment)
        panel_url = panel.get("url") or panel.get("result_url")
        item["storyboard_panel"] = {
            "path": panel["abspath"], "sha256": artifact_contract.file_sha256(panel["abspath"]),
            "recipe_sha256": recipe_sha, "panel_index": segment.get("storyboard_panel_index"),
            "ref_tags": list(segment.get("ref_tags") or []),
            "url": panel_url,
            "result_url": panel_url,
        }
        item["reference_budget_plan"] = reference_budget(item)
        item["risk_profile"] = risk_profile(item)
        item["panel_quality"] = assess_panel(api_key, item, panel["abspath"]) if qa else {
            "pass": False, "score": None, "method": "qa_skipped_draft",
            "formal_eligible": False, "issues": ["QA_SKIPPED_DRAFT"]}
        item["storyboard_panel_approval"] = {"status": "pending", "recipe_sha256": recipe_sha}
        prepared.append(item)
    return prepared


def confirm(segments, *, customer_confirmation, manual_qa=False,
            manual_qa_report=None):
    confirmation = run_manifest_store.require_explicit_customer_confirmation(
        customer_confirmation, "panel_expansion")
    for item in segments:
        if not item.get("storyboard_ref"):
            continue
        quality = item.get("panel_quality") or {}
        panel = item.get("storyboard_panel") or {}
        if (not quality.get("pass")) and manual_qa and panel.get("path") and os.path.isfile(panel["path"]):
            report = dict(manual_qa_report or {})
            checks = report.get("checks") or {}
            current_sha = artifact_contract.file_sha256(panel["path"])
            if (report.get("status") != "passed" or
                    not str(report.get("reviewer") or "").strip() or
                    not str(report.get("reviewed_at") or "").strip() or
                    not str(report.get("note") or "").strip() or
                    report.get("panel_sha256") != current_sha or
                    any(checks.get(name) is not True for name in PANEL_QA_CHECKS)):
                raise ValueError(
                    "MANUAL_PANEL_QA_REPORT_REQUIRED: 人工 QA 必须绑定当前 panel SHA-256、"
                    "reviewer/reviewed_at/note，并将七项 checks 全部显式记为 true")
            item["automated_panel_quality"] = quality
            item["panel_quality"] = {
                "pass": True,
                "score": None,
                "method": "manual_human_review",
                "issues": [],
                "checks": {name: True for name in PANEL_QA_CHECKS},
                "manual_review": report,
            }
            quality = item["panel_quality"]
        if not quality.get("pass") or not panel.get("path") or not os.path.isfile(panel["path"]):
            raise ValueError("PANEL_QUALITY_APPROVAL_REQUIRED: %s" % item.get("id"))
        if artifact_contract.file_sha256(panel["path"]) != panel.get("sha256"):
            raise ValueError("STALE_STORYBOARD_PANEL: %s" % item.get("id"))
        expected_recipe = artifact_contract.sha256_json(
            artifact_contract.build_storyboard_panel_recipe(item))
        if not panel.get("recipe_sha256") or panel.get("recipe_sha256") != expected_recipe:
            raise ValueError("STALE_STORYBOARD_PANEL: %s recipe 已变化" % item.get("id"))
        item["storyboard_panel_approval"] = {
            "status": "confirmed",
            "at": _now(),
            "confirmed_by_customer": True,
            "customer_confirmation": confirmation,
            "confirmation_source": "customer_dialogue",
            "recipe_sha256": expected_recipe,
            "panel_sha256": panel.get("sha256"),
            "qa_method": quality.get("method") or "automated_vision_qa",
            "qa_fingerprint": artifact_contract.sha256_json(quality),
        }
        if quality.get("manual_review"):
            item["storyboard_panel_approval"]["manual_review"] = quality["manual_review"]
    return segments


def write_report(segments, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for seg in segments:
        panel = seg.get("storyboard_panel") or {}
        if not panel.get("path"):
            continue
        rows.append("<section><h2>镜头 %s · 格 %s</h2><p>绑定：%s<br>QA：%s<br>确认：%s</p>"
                    "<img src=\"file://%s\"><pre>%s</pre></section>" % (
                        html.escape(str(seg.get("id"))), html.escape(str(panel.get("panel_index"))),
                        html.escape(" ".join(seg.get("ref_tags") or [])),
                        html.escape(json.dumps(seg.get("panel_quality") or {}, ensure_ascii=False)),
                        html.escape((seg.get("storyboard_panel_approval") or {}).get("status", "pending")),
                        html.escape(os.path.abspath(panel["path"])),
                        html.escape(json.dumps(seg.get("reference_budget_plan") or {}, ensure_ascii=False, indent=2))))
    path = os.path.join(out_dir, "panel_expansion_preview.html")
    body = "\n".join(rows)
    document = (
        "<html><head><meta charset='utf-8'><style>"
        "body{background:#111;color:#eee;font-family:sans-serif;padding:24px}"
        "section{padding:16px;margin:16px 0;border:1px solid #444}"
        "img{max-width:100%%}"
        "pre{white-space:pre-wrap}"
        "</style></head><body><h1>单格展开图确认</h1>"
        + body +
        "</body></html>"
    )
    atomic_write_bytes(path, document.encode("utf-8"), label="panel_preview")
    return os.path.abspath(path)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--segments", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--skip-qa", action="store_true")
    parser.add_argument("--manual-qa-pass", action="store_true",
                        help="确认时使用 --manual-qa-report 的结构化人工视觉 QA 证据")
    parser.add_argument("--manual-qa-report",
                        help="JSON 报告：必须绑定 panel_sha256、审查人/时间/说明及七项布尔 checks")
    parser.add_argument("--customer-confirmation",
                        help="客户在对话中确认单格展开图的原话；--confirm 时必填")
    args = parser.parse_args(argv)
    with open(args.segments, encoding="utf-8") as handle:
        source = json.load(handle)
    segments = source.get("segments", source) if isinstance(source, dict) else source
    if args.confirm:
        manual_report = None
        if args.manual_qa_report:
            with open(args.manual_qa_report, encoding="utf-8") as handle:
                manual_report = json.load(handle)
        output = confirm(segments, customer_confirmation=args.customer_confirmation,
                         manual_qa=args.manual_qa_pass,
                         manual_qa_report=manual_report)
    else:
        key_setup.ensure_session_id()
        api_key = key_setup.load_key()
        if not api_key:
            parser.error("BASICROUTER_KEY_REQUIRED: 请先完成 key_setup.py gate/save")
        output = prepare(segments, api_key=api_key, out_dir=args.out_dir, qa=not args.skip_qa)
    payload = dict(source) if isinstance(source, dict) else {}
    payload["segments"] = output
    _atomic_json(args.out, payload)
    print(json.dumps({"ok": True, "out": os.path.abspath(args.out),
                      "preview_html": write_report(output, args.out_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
