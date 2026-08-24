#!/usr/bin/env python3
"""Compile source-bound, customer-readable approval previews.

Machine-readable workflow contracts must remain approval inputs, but they are
not suitable customer previews.  This module creates a deterministic view of
the decision fields without treating raw JSON as a display artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os

import run_manifest
import audio_contract
from project_utils import atomic_write_bytes, require_contained_path, sha256_file


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PREVIEW_PREFIX = "brief_preview."


def _plain(value):
    """Return one safe Markdown text line without executable HTML."""
    if value is None:
        return ""
    text = " ".join(str(value).replace("\x00", "").split())
    return html.escape(text, quote=False)


def _list(values):
    return [_plain(value) for value in (values or []) if _plain(value)]


def _label(value, labels):
    value = str(value or "").strip()
    return labels.get(value, value or "未指定")


def _source_summary(brief):
    documents = [os.path.basename(str(path)) for path in brief.get("ppt_files") or []]
    slides = {
        item.get("slide") for item in brief.get("texts") or []
        if isinstance(item, dict) and item.get("slide") is not None
    }
    facts = _list(brief.get("fact_refs"))
    lines = []
    if documents:
        lines.append("- 来源文档：%s" % "、".join(_plain(item) for item in documents))
    if slides:
        lines.append("- 已提取内容：%d 页；用于事实核对，不在本页逐字展开" % len(slides))
    if facts:
        lines.append("- 事实引用：%s" % "、".join("`%s`" % item for item in facts))
    return lines or ["- 未登记外部事实来源；后续脚本不得自行补充未经确认的事实。"]


def compile_brief_preview(brief, *, source_sha256, client, run_id):
    """Return deterministic Markdown for the customer-decision part of a brief."""
    if not isinstance(brief, dict):
        raise ValueError("BRIEF_PREVIEW_SOURCE_OBJECT_REQUIRED")
    if str(brief.get("client") or client) != str(client):
        raise ValueError("BRIEF_PREVIEW_CLIENT_MISMATCH")
    if len(str(source_sha256 or "")) != 64:
        raise ValueError("BRIEF_PREVIEW_SOURCE_HASH_INVALID")

    secondary = _list(brief.get("secondary_points"))
    if not secondary:
        secondary = _list(brief.get("usps"))
    digital = brief.get("digital_human") or {}
    digital_enabled = digital.get("enabled") is not False and bool(digital)
    profile = _label(brief.get("profile"), {
        "oral_broadcast": "数字人口播",
        "document_explainer": "文档 / 方案讲解",
        "product_showcase": "产品展示",
        "product_with_human": "数字人产品讲解",
        "venue_service": "场地 / 服务介绍",
        "brand_tvc": "品牌 TVC",
        "generic_video": "通用营销视频",
    })
    role = _label(brief.get("storyboard_role"), {
        "design_board": "设计预演板（确认版式、层级、节奏与合成关系）",
        "video_storyboard": "视频故事板（确认镜头、表演与产品关系）",
    })
    binding = json.dumps({
        "schema_version": 1,
        "stage": "brief",
        "source_sha256": source_sha256,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lines = [
        "<!-- approval-preview-binding:%s -->" % binding,
        "# %s 创意 Brief 审批预览" % _plain(client),
        "",
        "> 本次只确认创作方向与制作边界；不代表脚本、分镜、形象板或付费生成已获批准。",
        "",
        "## 核心决策",
        "",
        "- 成片目标：%s" % (_plain(brief.get("objective")) or "未指定"),
        "- 目标受众：%s" % (_plain(brief.get("audience")) or "未指定"),
        "- 核心卖点：%s" % (_plain(brief.get("primary_usp")) or "未指定"),
    ]
    if secondary:
        lines.extend(["- 辅助信息："] + ["  %d. %s" % (index, item)
                     for index, item in enumerate(secondary, 1)])
    else:
        lines.append("- 辅助信息：未指定")
    lines.extend([
        "- 行动引导：%s" % (_plain(brief.get("cta")) or "未指定"),
        "",
        "## 制作规格",
        "",
        "- 内容类型：%s" % _plain(profile),
        "- 发布渠道：%s" % (_plain(brief.get("channel")) or "未指定"),
        "- 输出画幅：%s" % (_plain(brief.get("output_ratio")) or "未指定"),
        "- 表达语气：%s" % (_plain(brief.get("tone")) or "未指定"),
        "- 预演类型：%s" % _plain(role),
        "",
        "## 数字人与声音",
        "",
    ])
    if digital_enabled:
        lines.extend([
            "- 是否出镜：是",
            "- 角色：%s" % (_plain(digital.get("role")) or "未指定"),
            "- 人设：%s" % (_plain(digital.get("persona")) or "未指定"),
            "- 外观边界：%s" % (_plain(digital.get("appearance")) or "未指定"),
            "- 声音：%s" % (_plain(digital.get("voice_type")) or "未指定"),
            "- 表演：%s" % (_plain(digital.get("expression")) or "未指定"),
            "- 出镜方式：%s" % (_plain(digital.get("presentation_mode")) or "未指定"),
        ])
    else:
        lines.append("- 是否出镜：否")
    lines.extend([
        "",
        "## 事实来源与边界",
        "",
        *_source_summary(brief),
        "- 约束：脚本可以提炼和重组，但不得新增来源未支持的参数、效果数字、认证、排名或承诺。",
        "",
        "## 版本绑定",
        "",
        "- 客户：`%s`" % _plain(client),
        "- Run：`%s`" % _plain(run_id),
        "- 当前 Brief SHA-256：`%s`" % source_sha256,
        "- 任何 Brief 字节变化都会使本预览和展示回执失效，必须重新生成并展示。",
        "",
    ])
    return "\n".join(lines)


def render_brief_preview(source_path, out_path, *, client, run_id):
    source_path = require_contained_path(
        ROOT, source_path, label="brief_preview_source", must_exist=True)
    out_path = require_contained_path(
        ROOT, out_path, label="brief_preview_output", must_exist=False)
    with open(source_path, "rb") as handle:
        source_bytes = handle.read()
    try:
        brief = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("BRIEF_PREVIEW_SOURCE_JSON_INVALID") from exc
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    preview = compile_brief_preview(
        brief, source_sha256=source_sha256, client=client, run_id=run_id)
    atomic_write_bytes(
        out_path, preview.encode("utf-8"), mode=0o600, label="approval_preview")
    if sha256_file(source_path) != source_sha256:
        raise ValueError("APPROVAL_PREVIEW_SOURCE_CHANGED: 请重新生成审批预览")
    return {
        "stage": "brief",
        "source_path": source_path,
        "source_sha256": source_sha256,
        "preview_path": out_path,
        "preview_sha256": sha256_file(out_path),
    }


def _immutable_brief_preview(source_path, output_root, *, client, run_id):
    """Write a content-addressed preview without replacing an approved artifact."""
    source_path = require_contained_path(
        ROOT, source_path, label="brief_preview_source", must_exist=True)
    with open(source_path, "rb") as handle:
        source_bytes = handle.read()
    try:
        brief = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("BRIEF_PREVIEW_SOURCE_JSON_INVALID") from exc
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    preview_bytes = compile_brief_preview(
        brief, source_sha256=source_sha256, client=client,
        run_id=run_id).encode("utf-8")
    preview_sha256 = hashlib.sha256(preview_bytes).hexdigest()
    filename = "%s%s.%s.md" % (
        PREVIEW_PREFIX, source_sha256[:12], preview_sha256[:12])
    preview_path = require_contained_path(
        ROOT, os.path.join(output_root, filename),
        label="brief_preview_output", must_exist=False)
    if os.path.exists(preview_path):
        with open(preview_path, "rb") as handle:
            if handle.read() != preview_bytes:
                raise ValueError("APPROVAL_PREVIEW_IMMUTABLE_PATH_CONFLICT")
    else:
        atomic_write_bytes(
            preview_path, preview_bytes, mode=0o600, label="approval_preview")
    if sha256_file(source_path) != source_sha256:
        raise ValueError("APPROVAL_PREVIEW_SOURCE_CHANGED: 请重新生成审批预览")
    return {
        "stage": "brief",
        "source_path": source_path,
        "source_sha256": source_sha256,
        "preview_path": preview_path,
        "preview_sha256": preview_sha256,
    }


def _managed_brief_preview(path):
    name = os.path.basename(str(path)).lower()
    return name == "brief_preview.md" or (
        name.startswith(PREVIEW_PREFIX) and name.endswith(".md"))


def _brief_source(manifest):
    generation = (manifest.get("generation") or {}).get("brief") or {}
    candidates = [
        str(path) for path in generation.get("outputs") or []
        if str(path).lower().endswith(".json")
    ]
    preferred = [path for path in candidates
                 if os.path.basename(path).lower() == "brief.json"]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError("BRIEF_PREVIEW_SOURCE_AMBIGUOUS: 必须登记唯一的 brief.json")


def _load_json_object(path, error):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(value, dict):
        raise ValueError(error)
    return value


def _audio_sources(manifest):
    generation = (manifest.get("generation") or {}).get("audio") or {}
    items = []
    for path in generation.get("outputs") or []:
        if not str(path).lower().endswith(".json"):
            continue
        value = _load_json_object(path, "AUDIO_PREVIEW_SOURCE_JSON_INVALID")
        items.append((os.path.abspath(path), value))
    contracts = [(path, value) for path, value in items
                 if value.get("kind") == audio_contract.CONTRACT_KIND or
                 value.get("audio_mode") in audio_contract.AUDIO_MODES]
    choices = [(path, value) for path, value in items
               if value.get("kind") == audio_contract.CHOICE_KIND or
               value.get("mode") in audio_contract.AUDIO_MODES]
    briefs = [(path, value) for path, value in items
              if value.get("kind") == audio_contract.VOICE_BRIEF_KIND or
              (value.get("voice_type") and not value.get("audio_mode") and
               not value.get("mode"))]
    if not (len(contracts) == len(choices) == len(briefs) == 1):
        raise ValueError(
            "AUDIO_PREVIEW_SOURCE_AMBIGUOUS: 必须各登记一份声音选择、voice brief 和 audio contract")
    return choices[0], briefs[0], contracts[0]


def _managed_audio_preview(path):
    name = os.path.basename(str(path)).lower()
    return name == "audio_preview.md" or (
        name.startswith(audio_contract.PREVIEW_PREFIX) and name.endswith(".md"))


def _immutable_audio_preview(manifest, output_root):
    (choice_path, choice), (brief_path, _), (contract_path, contract) = \
        _audio_sources(manifest)
    choice_sha = sha256_file(choice_path)
    brief_sha = sha256_file(brief_path)
    contract_sha = sha256_file(contract_path)
    preview_bytes = audio_contract.compile_preview(
        choice, contract, choice_sha256=choice_sha,
        voice_brief_sha256=brief_sha, contract_sha256=contract_sha,
        client=manifest["client"], run_id=manifest["run_id"]).encode("utf-8")
    preview_sha = hashlib.sha256(preview_bytes).hexdigest()
    filename = "%s%s.%s.md" % (
        audio_contract.PREVIEW_PREFIX, contract_sha[:12], preview_sha[:12])
    preview_path = require_contained_path(
        ROOT, os.path.join(output_root, filename),
        label="audio_preview_output", must_exist=False)
    if os.path.exists(preview_path):
        with open(preview_path, "rb") as handle:
            if handle.read() != preview_bytes:
                raise ValueError("APPROVAL_PREVIEW_IMMUTABLE_PATH_CONFLICT")
    else:
        atomic_write_bytes(
            preview_path, preview_bytes, mode=0o600, label="approval_preview")
    for path, expected in ((choice_path, choice_sha), (brief_path, brief_sha),
                           (contract_path, contract_sha), (preview_path, preview_sha)):
        if sha256_file(path) != expected:
            raise ValueError("APPROVAL_PREVIEW_SOURCE_CHANGED: 请重新生成声音方案预览")
    return {
        "stage": "audio", "choice_path": choice_path,
        "voice_brief_path": brief_path, "contract_path": contract_path,
        "preview_path": preview_path, "choice_sha256": choice_sha,
        "voice_brief_sha256": brief_sha, "contract_sha256": contract_sha,
        "preview_sha256": preview_sha,
    }


def ensure_stage_preview(manifest, stage):
    """Refresh a deterministic preview and bind it to all pending stage inputs."""
    if stage not in {"brief", "audio"}:
        return None
    generation = (manifest.get("generation") or {}).get(stage) or {}
    if (generation.get("status") != "pending_approval" or
            (manifest.get("approvals") or {}).get(stage)):
        raise ValueError("APPROVAL_PREVIEW_STAGE_NOT_PENDING: %s" % stage)
    run_manifest.identity_gate(manifest)
    output_root = run_manifest.run_output_root(manifest)
    os.makedirs(output_root, exist_ok=True)
    if stage == "brief":
        source_path = _brief_source(manifest)
        result = _immutable_brief_preview(
            source_path, output_root, client=manifest["client"],
            run_id=manifest["run_id"])
        required_paths = [source_path]
        managed_preview = _managed_brief_preview
    else:
        result = _immutable_audio_preview(manifest, output_root)
        required_paths = [
            result["choice_path"], result["voice_brief_path"],
            result["contract_path"],
        ]
        managed_preview = _managed_audio_preview
    preview_path = result["preview_path"]

    outputs = []
    for path in list(generation.get("outputs") or []) + required_paths + [preview_path]:
        absolute = os.path.abspath(path)
        if managed_preview(absolute) and absolute != preview_path:
            continue
        if absolute not in outputs:
            outputs.append(absolute)
    current_records = generation.get("artifacts") or []
    desired_records = [run_manifest.file_record(path) for path in outputs]
    if (generation.get("outputs") != outputs or current_records != desired_records):
        run_manifest.replace_pending_stage_artifacts(manifest, stage, outputs)
    result["stage_input_fingerprint"] = run_manifest.stage_input_fingerprint(
        manifest, stage, refresh=True)
    if sha256_file(preview_path) != result["preview_sha256"]:
        raise ValueError("APPROVAL_PREVIEW_CHANGED_DURING_BINDING")
    if stage == "brief" and sha256_file(source_path) != result["source_sha256"]:
        raise ValueError("APPROVAL_PREVIEW_CHANGED_DURING_BINDING")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    result = render_brief_preview(
        args.source, args.out, client=args.client, run_id=args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
