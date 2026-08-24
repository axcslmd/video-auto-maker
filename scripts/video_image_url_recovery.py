#!/usr/bin/env python3
"""Recover and audit BasicRouter image URLs before video generation.

BasicRouter /v1/video-generations accepts imageUrls as HTTP(S) image material
URLs. This utility only restores URLs that were already returned by image
generation retrieve and preserved in local metadata. It never uploads, redraws,
or converts local files to base64.
"""
import argparse
import hashlib
import json
import os
import sys
from project_utils import atomic_write_json


EMPTY_SOURCE_FINGERPRINT = hashlib.sha256(b"").hexdigest()


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _write_json(path, value):
    atomic_write_json(path, value)


def _is_remote(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _norm_path(value, base_dir=None):
    if not value or _is_remote(value):
        return None
    path = str(value)
    if os.path.isabs(path):
        return os.path.abspath(path)
    candidates = [os.path.abspath(path)]
    if base_dir:
        candidates.append(os.path.abspath(os.path.join(base_dir, path)))
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[-1]


def _remote_from_item(item):
    if not isinstance(item, dict):
        return None
    for key in ("url", "result_url", "remote_url", "download_url", "imageUrl"):
        value = item.get(key)
        if _is_remote(value):
            return value
    values = item.get("imageUrls")
    if isinstance(values, list):
        for value in values:
            if _is_remote(value):
                return value
    return None


def _register_url(mapping, local_path, remote_url, source):
    local_path = _norm_path(local_path)
    if local_path and _is_remote(remote_url):
        mapping.setdefault(local_path, {"url": remote_url, "sources": []})
        if source not in mapping[local_path]["sources"]:
            mapping[local_path]["sources"].append(source)


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _customer_evidence_current(record):
    return (
        isinstance(record, dict)
        and record.get("confirmed_by_customer") is True
        and bool(str(record.get("customer_confirmation") or "").strip())
        and record.get("confirmation_source") == "customer_dialogue"
        and bool(record.get("confirmed_at"))
    )


def _contained_file(path, root):
    local = _norm_path(path, root)
    if not local or not os.path.isfile(local) or os.path.islink(local):
        return None
    try:
        if os.path.commonpath((os.path.abspath(root), local)) != os.path.abspath(root):
            return None
    except ValueError:
        return None
    return local


def _validated_board_marker(storyboard_dir, kind):
    record = _load_json(
        os.path.join(storyboard_dir, ".%s_confirmed.json" % kind), {})
    source_fingerprint = str(record.get("source_fingerprint") or "")
    path = _contained_file(record.get("path"), storyboard_dir)
    if (
        record.get("status") != "confirmed"
        or record.get("kind") != kind
        or not _customer_evidence_current(record)
        or not source_fingerprint
        or source_fingerprint == EMPTY_SOURCE_FINGERPRINT
        or not path
        or record.get("board_sha256") != _file_sha256(path)
    ):
        return None
    return dict(record, path=path)


def _validated_storyboard_shots(storyboard_dir):
    record = _load_json(
        os.path.join(storyboard_dir, ".storyboard_confirmed.json"), {})
    if (record.get("status") != "confirmed"
            or record.get("kind") not in ("storyboard", "design_board")
            or not _customer_evidence_current(record)
            or not record.get("plan_fingerprint")):
        return []
    shots = []
    seen = set()
    for item in record.get("shots") or []:
        shot_id = str(item.get("id") or "").strip()
        path = _contained_file(item.get("path"), storyboard_dir)
        if (not shot_id or shot_id in seen or not path
                or item.get("sha256") != _file_sha256(path)):
            return []
        seen.add(shot_id)
        shots.append(dict(item, path=path))
    return shots


def build_url_map(storyboard_dir):
    storyboard_dir = os.path.abspath(storyboard_dir)
    mapping = {}
    for kind in ("cast", "product", "usage"):
        record = _validated_board_marker(storyboard_dir, kind)
        if not record:
            continue
        _register_url(
            mapping, record.get("path"),
            _remote_from_item(record), ".%s_confirmed" % kind)
    for item in _validated_storyboard_shots(storyboard_dir):
        _register_url(mapping, item.get("path"), _remote_from_item(item),
                      ".storyboard_confirmed.shots.%s" % item.get("id"))
    return mapping


def recover_segments(segments_doc, storyboard_dir):
    mapping = build_url_map(storyboard_dir)
    changed = []
    missing = []

    def recover_value(value, context):
        if not value or _is_remote(value):
            return value
        local = _norm_path(value, storyboard_dir)
        hit = mapping.get(local)
        if hit:
            changed.append(dict(context, value=value, recovered_url=hit["url"],
                                sources=hit["sources"]))
            return hit["url"]
        missing.append(dict(context, value=value, exists=bool(local and os.path.isfile(local))))
        return value

    segments = segments_doc.get("segments") if isinstance(segments_doc, dict) else segments_doc
    for segment in segments or []:
        segment_id = segment.get("id")
        segment["urls"] = [
            recover_value(value, {"segment_id": segment_id, "field": "urls",
                                  "index": index})
            for index, value in enumerate(segment.get("urls") or [], 1)
        ]
        for ref in segment.get("references") or []:
            if not isinstance(ref, dict) or not ref.get("url"):
                continue
            ref["url"] = recover_value(
                ref["url"], {"segment_id": segment_id, "field": "references.url",
                             "tag": ref.get("tag"), "type": ref.get("type")})
        storyboard_path = segment.get("storyboard_path")
        if segment.get("storyboard_ref") and not segment.get("storyboard_url"):
            local = _norm_path(storyboard_path, storyboard_dir)
            hit = mapping.get(local)
            if hit:
                segment["storyboard_url"] = hit["url"]
                changed.append({"segment_id": segment_id, "field": "storyboard_url",
                                "value": storyboard_path, "recovered_url": hit["url"],
                                "sources": hit["sources"]})
            elif storyboard_path:
                missing.append({"segment_id": segment_id, "field": "storyboard_url",
                                "value": storyboard_path,
                                "exists": bool(local and os.path.isfile(local))})
    # De-duplicate missing entries that appear both in urls and references.
    unique = {}
    for item in missing:
        key = (item.get("segment_id"), item.get("value"), item.get("tag"),
               item.get("type"), item.get("field"))
        unique[key] = item
    return {"segments_doc": segments_doc, "recovered": changed,
            "missing": list(unique.values()), "url_map": mapping}


def _asset_kind(item):
    value = str(item.get("value") or "")
    name = os.path.basename(value)
    asset_type = item.get("type")
    if asset_type == "character_board" or name == "cast_board.jpg":
        return "cast_board"
    if asset_type == "product_usage_identity" or name == "product_usage_board.jpg":
        return "product_usage"
    if asset_type == "storyboard_composition" or name.startswith("shot_"):
        return "storyboard_shot"
    return "unknown"


def build_missing_asset_plan(result, *, client=None, run_id=None):
    assets = {}
    for item in result.get("missing") or []:
        value = item.get("value")
        if not value:
            continue
        key = os.path.abspath(value) if os.path.isabs(str(value)) else str(value)
        asset = assets.setdefault(key, {
            "value": key,
            "asset_kind": _asset_kind(item),
            "segments": [],
            "fields": [],
            "tags": [],
            "types": [],
            "exists": bool(item.get("exists")),
            "requires_paid_regeneration": True,
            "reason": (
                "local file has no preserved BasicRouter retrieve URL; "
                "cannot be used in /v1/video-generations imageUrls"),
        })
        for field in ("segment_id", "field", "tag", "type"):
            target = {
                "segment_id": "segments",
                "field": "fields",
                "tag": "tags",
                "type": "types",
            }[field]
            value_to_add = item.get(field)
            if value_to_add and value_to_add not in asset[target]:
                asset[target].append(value_to_add)
        asset["exists"] = asset["exists"] or bool(item.get("exists"))
        if asset["asset_kind"] == "unknown":
            asset["asset_kind"] = _asset_kind(item)

    already_remote = []
    segments_doc = result.get("segments_doc") or {}
    segments = segments_doc.get("segments") if isinstance(segments_doc, dict) else segments_doc
    seen_remote = set()
    for segment in segments or []:
        segment_id = segment.get("id")
        for ref in segment.get("references") or []:
            if not isinstance(ref, dict) or not _is_remote(ref.get("url")):
                continue
            key = (segment_id, ref.get("tag"), ref.get("type"), ref.get("url"))
            if key in seen_remote:
                continue
            seen_remote.add(key)
            already_remote.append({
                "segment_id": segment_id,
                "tag": ref.get("tag"),
                "type": ref.get("type"),
                "url": ref.get("url"),
            })

    missing_assets = sorted(
        assets.values(), key=lambda item: (item.get("asset_kind") or "", item.get("value") or ""))
    for asset in missing_assets:
        for key in ("segments", "fields", "tags", "types"):
            asset[key] = sorted(asset[key])

    actions = []
    storyboard_shots = sorted({
        segment for asset in missing_assets
        if asset["asset_kind"] == "storyboard_shot"
        for segment in asset.get("segments") or []
    })
    if any(asset["asset_kind"] == "cast_board" for asset in missing_assets):
        actions.append({
            "code": "REGENERATE_CAST_BOARD_FOR_URL",
            "requires_paid_generation": True,
            "reason": "confirmed cast board has no preserved retrieve URL",
        })
    if any(asset["asset_kind"] == "product_usage" for asset in missing_assets):
        actions.append({
            "code": "REGENERATE_PRODUCT_USAGE_FOR_URL",
            "requires_paid_generation": True,
            "reason": "confirmed product usage board has no preserved retrieve URL",
        })
    if storyboard_shots:
        actions.append({
            "code": "REGENERATE_STORYBOARD_SHOTS_FOR_URL",
            "shot_ids": storyboard_shots,
            "requires_paid_generation": True,
            "reason": "confirmed storyboard shots have no preserved retrieve URL",
        })

    status = "ready" if not missing_assets else "blocked_until_remote_urls_restored"
    return {
        "status": status,
        "client": client,
        "run_id": run_id,
        "missing_assets": missing_assets,
        "already_remote_references": already_remote,
        "recommended_actions": actions,
        "next_steps": [
            "Do not run video_engine while this plan has missing_assets.",
            "Recover original BasicRouter image retrieve URLs from result/state/approval files if available.",
            "If unavailable, regenerate the listed images with gpt-image-2, show them to the user, confirm, refresh manifest, split, prompt review, and preflight again.",
        ] if missing_assets else [
            "Run video_effect_qc preflight on the recovered segments before video generation.",
        ],
    }


def write_plan(path, plan):
    _write_json(path, plan)


def write_report(path, result):
    lines = ["# Video imageUrls recovery report", ""]
    lines.append("## Recovered")
    if not result["recovered"]:
        lines.append("- None")
    for item in result["recovered"]:
        lines.append("- {segment_id} {field} {tag} -> {recovered_url}".format(
            segment_id=item.get("segment_id"), field=item.get("field"),
            tag=item.get("tag") or item.get("index") or "",
            recovered_url=item.get("recovered_url")))
    lines.extend(["", "## Missing"])
    if not result["missing"]:
        lines.append("- None")
    for item in result["missing"]:
        lines.append("- {segment_id} {field} {tag}: `{value}`".format(
            segment_id=item.get("segment_id"), field=item.get("field"),
            tag=item.get("tag") or item.get("index") or "",
            value=item.get("value")))
    plan = build_missing_asset_plan(result)
    lines.extend(["", "## Unique Missing Assets"])
    if not plan["missing_assets"]:
        lines.append("- None")
    for asset in plan["missing_assets"]:
        lines.append(
            "- {kind}: `{value}`; segments={segments}; tags={tags}".format(
                kind=asset.get("asset_kind"),
                value=asset.get("value"),
                segments=",".join(asset.get("segments") or []),
                tags=",".join(asset.get("tags") or [])))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Recover video imageUrls from preserved BasicRouter retrieve URLs")
    parser.add_argument("--segments", required=True)
    parser.add_argument("--storyboard-dir", required=True)
    parser.add_argument("--out", help="write recovered segments JSON here")
    parser.add_argument("--report", help="write markdown recovery report")
    parser.add_argument("--plan-out", help="write unique missing-asset recovery plan JSON here")
    parser.add_argument("--client")
    parser.add_argument("--run-id")
    parser.add_argument("--fail-on-missing", action="store_true")
    args = parser.parse_args(argv)

    segments_doc = _load_json(args.segments)
    result = recover_segments(segments_doc, args.storyboard_dir)
    if args.out:
        _write_json(args.out, result["segments_doc"])
    if args.report:
        write_report(args.report, result)
    plan = build_missing_asset_plan(result, client=args.client, run_id=args.run_id)
    if args.plan_out:
        write_plan(args.plan_out, plan)
    summary = {
        "ok": not result["missing"],
        "recovered_count": len(result["recovered"]),
        "missing_count": len(result["missing"]),
        "missing_asset_count": len(plan["missing_assets"]),
        "out": os.path.abspath(args.out) if args.out else None,
        "report": os.path.abspath(args.report) if args.report else None,
        "plan_out": os.path.abspath(args.plan_out) if args.plan_out else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.fail_on_missing and result["missing"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
