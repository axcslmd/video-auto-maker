#!/usr/bin/env python3
"""Safely migrate legacy video segment field names without guessing semantics."""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import video_use_bridge as bridge  # noqa: E402


def _items(payload):
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("segments", "shots", "items"):
            if isinstance(payload.get(key), list):
                return payload[key], key
    raise ValueError("SEGMENT_MIGRATION_INPUT_INVALID")


def migrate(payload):
    source, container_key = _items(payload)
    migrated = []
    changes = []
    blockers = []
    for index, raw in enumerate(source, 1):
        if not isinstance(raw, dict):
            blockers.append({"code": "SEGMENT_NOT_OBJECT", "index": index})
            continue
        item = dict(raw)
        sid = str(item.get("id") or index)
        for old, new in (("type", "video_type"), ("out", "out_path")):
            if old not in item:
                continue
            if new in item and item[new] != item[old]:
                blockers.append({"code": "LEGACY_FIELD_CONFLICT", "segment_id": sid,
                                 "fields": [old, new]})
                continue
            item[new] = item.pop(old)
            changes.append({"segment_id": sid, "from": old, "to": new})
        video_model_contract = (item.get("video_model_contract")
                                if isinstance(item.get("video_model_contract"), dict) else {})
        is_design_board = bool(
            item.get("design_board") or item.get("storyboard_role") == "design_board" or
            item.get("generation_role") == "avatar_plate_only" or
            video_model_contract.get("role") == "avatar_plate_only")
        has_story_ref = any(item.get(key) for key in (
            "storyboard_ref", "storyboard_ref_path", "storyboardRef", "storyboard_image"))
        if is_design_board and has_story_ref:
            blockers.append({
                "code": "DESIGN_BOARD_REFERENCE_REQUIRES_MANUAL_REMOVAL",
                "segment_id": sid,
                "message": "设计板不是视频模型参考；迁移器不会静默删除有业务语义的输入",
            })
        if is_design_board and int(item.get("video_type") or 0) not in (4, 5):
            blockers.append({
                "code": "AVATAR_PLATE_VIDEO_TYPE_REQUIRES_MANUAL_FIX",
                "segment_id": sid,
                "message": "文档讲解人物底片必须显式选择 type4 或 type5",
            })
        migrated.append(item)
    if container_key is None:
        output = migrated
    else:
        output = dict(payload)
        output[container_key] = migrated
    return {
        "schema_version": 1,
        "ok": not blockers,
        "migrated": output,
        "changes": changes,
        "blockers": blockers,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="迁移旧版 video segments 合同")
    parser.add_argument("--in", dest="input_path", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with open(args.input_path, encoding="utf-8") as handle:
        result = migrate(json.load(handle))
    if result["ok"]:
        bridge._atomic_json(args.out, result["migrated"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
