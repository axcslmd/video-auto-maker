#!/usr/bin/env python3
"""阶段4 · 剧本按时长拆分 → 底片段 segments（喂给 video_engine.render_batch）。

定位（修正后的正确顺序）：
  阶段1 素材补充（六视图在脚本之前就绪） → 阶段2 脚本共创 → 阶段3 分镜图生成
  → **阶段4（本脚本）** 把定稿 storyboard_plan.json 按各镜时长拆成多段，
     每段 = 分镜图(storyboard shot image) + 锚定素材(asset_refs) + 该段台词(dialogue)
  → video_engine.render_batch 并行出「底片段」 → 多段则 ffmpeg+HyperFrames 合成最终底片
  → 阶段5 第一轮客户确认+下载 → 阶段6 逆向工程 → 阶段7 Remotion 剪辑成片

与 guide_scaffold.compile-segments 的区别：
  guide_scaffold 从「引导表」编译；本脚本从「定稿 storyboard_plan.json + 已生成的分镜图」
  编译。剧情/TVC/产品演示等叙事型工作流把每个 shot 的分镜图作为 video_type=4/5
  的锚定参考图，保证成片贴合已确认的剧情分镜；方案文档型口播和 PPT/文档讲解
  工作流的 storyboard 是客户看的设计板/动效板，视频模型只继承人物、服装、
  声音、背景等一致性锁。实物产品型口播仍按产品故事板进视频模型。

storyboard_plan.json（阶段3 定稿，script-cocreation ⑦ 产出）关键字段：
  { "output_ratio":"9:16", "storyboard_aspect_ratio":"16:9", "asset_refs":{...},
    "shots":[ {"id":"s1","duration":3,"dialogue":"...","visual":"...",
               "camera":"...","characters":["host"],"asset_refs":{...}} ] }

分镜图约定：阶段3 storyboard.py 输出 output/storyboard/shot_<id>.jpg（或 .png）。
本脚本按 shot id 自动匹配分镜图目录里的 shot_<id>.* 作为该段首帧锚定图。

CLI:
  split --plan output/storyboard_plan.json --storyboard-dir output/storyboard \
        --out output/segments.json [--fps 30] [--min-seconds 3]
  assemble --segments output/segments.json --results output/batch_results.json \
        --out output/basecut.mp4          # 多段底片 → ffmpeg 拼接为最终版底片
"""
import os
import re
import sys
import json
import argparse
import copy
import shutil
import subprocess
import hashlib
import glob
import tempfile
import urllib.parse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aspect_ratio import output_ratio  # noqa: E402
import run_manifest as rm  # noqa: E402
import take_review  # noqa: E402
from video_segmentation import partition_shots, SEEDANCE_MAX_SECONDS, max_seconds_for_model
from artifact_contract import (build_video_handoff, file_sha256,
                               storyboard_image_input_tags,
                               validate_storyboard_binding)
import schema_validate  # noqa: E402
import br_client  # noqa: E402
import subtitle_asr  # noqa: E402
import storyboard_enhancements  # noqa: E402
import panel_expansion  # noqa: E402
import language_contract  # noqa: E402
import media_qc  # noqa: E402
import media_tools  # noqa: E402
import dialogue_timing  # noqa: E402
import video_recovery  # noqa: E402
import audio_contract as audio_direction_contract  # noqa: E402
from project_utils import atomic_write_json  # noqa: E402

# aspect_ratio → (ratio_str, default resolution)。竖屏优先（短视频主场景）。
_RATIO_MAP = {
    "9:16": ("9:16", "1080p"),
    "16:9": ("16:9", "1080p"),
    "1:1": ("1:1", "1080p"),
}


def _load_storyboard_result(storyboard_dir):
    path = os.path.join(storyboard_dir, "storyboard_result.json")
    try:
        with open(path, encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError, TypeError):
        return path, None
    return path, result if isinstance(result, dict) else None


def _resolve_current_storyboard_dir(storyboard_dir, run_id, plan, storyboard_module,
                                    allow_unconfirmed_product_images=False):
    """Follow storyboard.py's run pointer so a logical run_id cannot read a stale revision."""
    if not storyboard_dir or not run_id:
        return os.path.abspath(storyboard_dir) if storyboard_dir else None
    requested = os.path.abspath(storyboard_dir)
    # Deterministic document-explainer design boards are revision-addressed by
    # their approved IntegratedExplainer build, not by storyboard.py's image
    # generation run pointer.  Redirecting such an explicit directory to the
    # latest video-storyboard revision mixes two different approval roles and
    # makes the already-approved design board appear stale.
    requested_result = os.path.join(requested, "storyboard_result.json")
    try:
        with open(requested_result, encoding="utf-8") as handle:
            requested_payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        requested_payload = {}
    if (requested_payload.get("type") ==
            "deterministic_design_board_storyboard_result"):
        return requested
    base = os.path.dirname(requested)
    current = storyboard_module.resolve_current_storyboard_dir(
        base, run_id, plan,
        allow_unconfirmed_product_images=allow_unconfirmed_product_images)
    if current and os.path.abspath(current) != requested:
        print("[split] storyboard_dir redirected to current revision: %s" %
              os.path.abspath(current), flush=True)
        return os.path.abspath(current)
    return requested


def _storyboard_shot_map(result):
    """Build the only supported shot-image map from explicit result shot IDs.
    Keys are normalized via _normalize_shot_id for flexible matching."""
    mapping = {}
    for item in (result or {}).get("shots") or []:
        raw_sid = str((item.get("shot") or {}).get("id") or "").strip()
        sid = _normalize_shot_id(raw_sid) or raw_sid
        path = item.get("abspath") or item.get("path")
        video_ref = _result_item_video_ref(item)
        if not sid or not path or sid in mapping:
            raise ValueError("INVALID_STORYBOARD_RESULT: shot id/path 缺失或重复")
        mapping[sid] = {
            "path": os.path.abspath(path),
            "url": video_ref,
        }
    return mapping


def _is_remote_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _result_item_video_ref(item, storyboard_dir=None, result_key=None):
    """Return the BasicRouter media URL for video imageUrls, if preserved.

    Local paths are useful for display, hashing and manual QA, but BasicRouter
    /v1/video-generations requires imageUrls to be HTTP(S) media URLs. These
    URLs should come from the original /v1/image-generations/{taskId} retrieve
    response, never from a redraw-as-hosting step.
    """
    if not isinstance(item, dict):
        return None
    for key in ("url", "result_url", "remote_url", "download_url", "imageUrl"):
        value = item.get(key)
        if _is_remote_url(value):
            return value
    values = item.get("imageUrls")
    if isinstance(values, list):
        for value in values:
            if _is_remote_url(value):
                return value
    if storyboard_dir and result_key == "product_board":
        state_path = os.path.join(storyboard_dir, "product_board_state.json")
        try:
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            value = state.get("result_url") or state.get("url")
            if _is_remote_url(value):
                return value
        except (OSError, ValueError, TypeError):
            pass
    return None


def _normalize_shot_id(raw_id):
    """Normalize shot ID to canonical form for storyboard image matching.

    Handles common LLM-generated variants:
      "s1" / "s01" / "shot_1" / "shot1" / "镜头1" / "镜头一" / "1" → "s1"
    """
    if not raw_id:
        return ""
    s = str(raw_id).strip().lower()
    # Remove common prefixes
    for prefix in ("shot_", "shot", "scene_", "scene"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Chinese shot number
    cn_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
              "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    if s.startswith("镜头"):
        suffix = s[2:]
        s = cn_map.get(suffix, suffix)
    # Strip leading zeros from numeric part
    if s.startswith("s") and len(s) > 1:
        num = s[1:]
        if num.isdigit():
            return "s" + str(int(num))
        return s
    if s.isdigit():
        return "s" + str(int(s))
    return s


def _find_shot_image(shot_map, shot_id, source_ids=None):
    """Resolve shot image by normalized ID; filenames and substrings are never identities."""
    candidates = [_normalize_shot_id(shot_id)]
    candidates.extend(_normalize_shot_id(v) for v in (source_ids or []))
    for candidate in dict.fromkeys(candidates):
        if candidate in (shot_map or {}):
            return shot_map[candidate]
    return None


def _storyboard_result_fingerprint(storyboard_dir):
    """Read the generated storyboard revision identity, when available."""
    if not storyboard_dir:
        return None
    path = os.path.join(storyboard_dir, "storyboard_result.json")
    try:
        with open(path, encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return (result or {}).get("plan_fingerprint")

def _stale_storyboard_shot_ids(result, shots, storyboard):
    """Return only storyboard panels whose approved visual contract is stale.

    A pre-panel-fingerprint result remains fail-closed on a plan revision.  New
    results can safely retain approvals for unchanged panels because both the
    panel fingerprint and the approved image bytes must still match.
    """
    result_by_id = {
        str((item.get("shot") or {}).get("id")): item
        for item in (result or {}).get("shots") or []
    }
    stale = set()
    for shot in shots or []:
        sid = str(shot.get("id") or "")
        item = result_by_id.get(sid)
        result_shot = (item or {}).get("shot") or {}
        path = (item or {}).get("abspath") or (item or {}).get("path")
        if (not item or not item.get("shot_fingerprint") or
                not _storyboard_result_shot_matches_source(result_shot, shot) or
                not path or not os.path.isfile(path)):
            stale.add(sid)
    return stale


_AUTHOR_VISUAL_SHOT_KEYS = (
    "id", "panel_index", "visual", "characters",
    "scene", "props", "ref_tags", "requires_usage", "force_usage_reference",
    "shot_size", "camera_movement",
    "angle_offset", "composition", "lighting", "character_action",
    "micro_expression", "scene_prompt", "prop_prompts", "asset_refs",
)


def _storyboard_result_shot_matches_source(result_shot, source_shot):
    """Compare only authored visual fields.

    storyboard.py injects runtime-only fields such as approved_prompt_zh,
    panel_plan, references, and generated reference metadata into the result
    shot. Those fields are part of the rendered proof, but they are not present
    in the original storyboard_plan.json used by the video splitter. Treating
    them as source edits makes a confirmed storyboard look stale immediately.
    """
    if not isinstance(result_shot, dict) or not isinstance(source_shot, dict):
        return False
    # canonical_storyboard_plan() materializes these optional switches as
    # ``False``.  Older deterministic design-board adapters omitted them when
    # false, so compare their effective boolean value instead of treating the
    # serialization-only difference as a visual edit.
    optional_false_flags = {"requires_usage", "force_usage_reference"}
    for key in _AUTHOR_VISUAL_SHOT_KEYS:
        result_value = result_shot.get(key)
        source_value = source_shot.get(key)
        if key in optional_false_flags:
            result_value = bool(result_value)
            source_value = bool(source_value)
        if result_value != source_value:
            return False
    return True


def _collect_anchor_urls(shot, plan_refs, shot_image, bw_storyboard=True):
    """收集该段锚定素材 URL 列表。锚定顺序决定权重（越靠前权重越高）。

    bw_storyboard=True（默认，故事板是黑白）：**彩色素材优先**（人物板/产品图/场景图作
      主参考锚定颜色），黑白分镜图降为「构图确认」用，放最后作弱构图提示；避免黑白图当
      首帧锚导致出片补色不确定、掉色。
    bw_storyboard=False（故事板本身是彩色）：沿用旧语义，分镜图优先作首帧锚。
    """
    color_refs = []
    shot_refs = shot.get("asset_refs") or {}
    refs = dict(plan_refs or {})
    refs.update(shot_refs)  # shot 级覆盖 plan 级
    for key in ("digital_human_portraits", "background_images", "product_images", "scene_images"):
        for u in (refs.get(key) or []):
            if u and u not in color_refs:
                color_refs.append(u)

    if bw_storyboard:
        # 彩色素材锚定颜色在前；黑白分镜图作构图提示垫后
        urls = list(color_refs)
        if shot_image and shot_image not in urls:
            urls.append(shot_image)
    else:
        # 彩色故事板：分镜图作首帧锚在前
        urls = []
        if shot_image:
            urls.append(shot_image)
        for u in color_refs:
            if u not in urls:
                urls.append(u)
    return urls[:4]  # kling 参考图上限 4 张


_REFERENCE_META = {
    "product_usage_images": (
        "product_usage_identity", "已确认产品使用细节图", "锁定人物与产品操作关系",
        "继承人物身份、产品外观、手部接触点和真实操作关系，不继承背景文字", "@usage"),
    "cast_boards": (
        "character_board", "已确认人物六视图", "锁定人物身份与全身一致性",
        "继承脸部、发型、服装、配饰和身体比例，不继承板式布局", "@host"),
    "product_boards": (
        "product_board", "已确认产品九宫格板", "锁定产品多角度外观",
        "继承产品结构、材质、颜色、比例和原生标识，不继承板式布局", "@product"),
    "digital_human_portraits": (
        "character_identity", "人物身份参考", "锁定人物身份",
        "只继承脸部、发型、服装、配饰和身体比例，不继承背景、姿势或构图", "@host"),
    "background_images": (
        "scene_environment", "已确认统一背景参考", "锁定跨段空间与灯光",
        "所有数字人底片只继承同一空间布局、材质、机位和灯光，不复制图中文字", "@background"),
    "product_images": (
        "product_identity", "产品外观参考", "锁定产品身份",
        "只继承结构、材质、颜色、比例和原生标识，不继承背景或构图", "@product"),
    "scene_images": (
        "scene_environment", "场景环境参考", "锁定空间与灯光",
        "只继承环境布局、材质和光线，不复制图中偶然出现的人物或文字", "@scene"),
}


def _next_tag(base_tag, tag_counts):
    """Allocate a stable, unique @tag within one shot's reference set.

    第一张用基础 tag（如 @host），同类型第二张起加数字后缀（@host2、@host3），
    保证同一 run 内每张参考图都有唯一、可在 prompt 里内联引用的标识符。
    """
    count = tag_counts.get(base_tag, 0) + 1
    tag_counts[base_tag] = count
    return base_tag if count == 1 else "%s%d" % (base_tag, count)


def _required_reference_types(_refs=None, has_human=False, has_product=False,
                              usage_required=False, storyboard_required=True,
                              background_required=False):
    """Return semantic reference types that cannot be silently dropped."""
    required = {"storyboard_composition"} if storyboard_required else set()
    if has_human:
        # A six-view contact sheet is an approval artifact, not a single-person
        # video input. Formal video handoff must bind a remote identity plate or
        # portrait that contains exactly one person/view.
        required.add("character_identity")
    if has_product:
        required.add("product_board")
    if usage_required:
        required.add("product_usage_identity")
    if background_required:
        required.add("scene_environment")
    return required


def _collect_typed_references(shot, plan_refs, shot_image, bw_storyboard=True,
                              include_storyboard_ref=True):
    """Return role-bound references while preserving the legacy URL order.

    每条 reference 分配唯一 @tag（如 @host、@product、@usage、@scene），供故事板 prompt 和
    视频提交 prompt 逐格/逐镜头内联引用，取代过去"整段动作描述 + 参考图清单"两段分离的写法。
    """
    shot_refs = shot.get("asset_refs") or {}
    refs = dict(plan_refs or {})
    refs.update(shot_refs)
    by_url = {}
    color = []
    approval_only = []
    tag_counts = {}
    wanted_tags = list(shot.get("ref_tags") or [])
    if isinstance(wanted_tags, str):
        wanted_tags = [wanted_tags]
    registry_by_tag = {
        item.get("tag"): item for item in (refs.get("reference_registry") or [])
        if isinstance(item, dict) and item.get("tag") and item.get("url")
    }
    usage_contract = shot.get("requires_usage") if isinstance(shot, dict) else None
    if isinstance(usage_contract, bool):
        usage_action = usage_contract
    else:
        try:
            import storyboard as _storyboard
            usage_action = _storyboard._shot_needs_usage_reference(shot)
        except (ImportError, OSError, ValueError, TypeError, RuntimeError):
            usage_action = False
    explicit_usage = "@usage" in wanted_tags
    if "@usage" in registry_by_tag and not explicit_usage and usage_action:
        wanted_tags.insert(0, "@usage")
    if registry_by_tag:
        tag_refs = [{"tag": tag, "url": registry_by_tag[tag].get("url"),
                     "type": registry_by_tag[tag].get("type")}
                    for tag in wanted_tags if tag in registry_by_tag]
        budget = storyboard_image_input_tags({
            "ref_tags": wanted_tags,
            "references": tag_refs,
        })
        wanted_tags = [tag for tag in wanted_tags if tag in set(budget["selected"])]
    for tag in wanted_tags:
        item = registry_by_tag.get(tag)
        if not item or item.get("url") in by_url:
            continue
        ref_type = item.get("type") or "generic_visual"
        if ref_type in ("character_board", "actor_board"):
            approval_only.append({
                "url": item.get("url"), "type": ref_type, "tag": tag,
                "label": item.get("label") or "人物六视图审批板",
                "reason": "approval_contact_sheet_not_video_input",
            })
            continue
        if ref_type == "product_usage_identity":
            label, role, intent = _REFERENCE_META["product_usage_images"][1:4]
        elif ref_type in ("character_identity", "character_board"):
            label, role, intent = _REFERENCE_META["digital_human_portraits"][1:4]
        elif ref_type == "product_board":
            label, role, intent = _REFERENCE_META["product_boards"][1:4]
        elif ref_type == "product_identity":
            label, role, intent = _REFERENCE_META["product_images"][1:4]
        elif ref_type == "scene_environment":
            label, role, intent = _REFERENCE_META["scene_images"][1:4]
        else:
            label, role, intent = "已确认参考素材", "锁定画面语义", "继承已确认素材的可见身份与关系"
        ref = {"id": "ref_%02d" % (len(by_url) + 1), "url": item["url"],
               "type": ref_type, "scope": "scene", "label": item.get("label") or label,
               "role": item.get("role") or role, "intent": item.get("intent") or intent,
               "source": item.get("source") or "storyboard.reference_registry",
               "tag": tag}
        if item.get("background_anchor") is True:
            ref["background_anchor"] = True
        by_url[ref["url"]] = ref
        color.append(ref)
    # A clean background is a cross-segment anchor, so it is never treated as
    # optional just because a portrait was found first.
    for key in ("background_images",):
        ref_type, label, role, intent, base_tag = _REFERENCE_META[key]
        for url in refs.get(key) or []:
            if not url or url in by_url:
                continue
            item = {"id": "ref_%02d" % (len(by_url) + 1), "url": url,
                    "type": ref_type, "scope": "global", "label": label,
                    "role": role, "intent": intent, "source": "asset_refs.%s" % key,
                    "tag": _next_tag(base_tag, tag_counts)}
            by_url[url] = item
            color.append(item)
    # Background is an additional cross-segment anchor, not a replacement for
    # the identity/product references needed by this shot.
    for key in ("product_usage_images", "cast_boards", "product_boards",
                "digital_human_portraits", "product_images", "scene_images"):
        if key == "product_usage_images" and not (usage_action or explicit_usage):
            continue
        ref_type, label, role, intent, base_tag = _REFERENCE_META[key]
        for url in refs.get(key) or []:
            if not url or url in by_url:
                continue
            if key == "cast_boards":
                approval_only.append({
                    "url": url, "type": ref_type, "label": label,
                    "reason": "approval_contact_sheet_not_video_input",
                })
                continue
            item = {"id": "ref_%02d" % (len(by_url) + 1), "url": url,
                    "type": ref_type, "scope": "scene", "label": label,
                    "role": role, "intent": intent, "source": "asset_refs.%s" % key,
                    "tag": _next_tag(base_tag, tag_counts)}
            by_url[url] = item
            color.append(item)
    storyboard = None
    if shot_image and include_storyboard_ref:
        storyboard = {"id": "ref_%02d" % (len(by_url) + 1), "url": shot_image,
                      "type": "storyboard_composition", "scope": "beat",
                      "label": "分镜锚定图", "role": "构图与动作顺序参考",
                      "intent": "只继承构图、机位和动作顺序，不继承黑白素描媒介",
                      "source": "storyboard", "tag": _next_tag("@storyboard", tag_counts)}
    # Keep the shared environment inside the gateway's four-image budget. It
    # is the continuity anchor for an avatar plate and therefore outranks
    # optional scene/product derivatives when the budget is tight.
    color = ([item for item in color if item.get("type") == "scene_environment"] +
             [item for item in color if item.get("type") != "scene_environment"])
    ordered = color + ([storyboard] if storyboard else []) if bw_storyboard else \
        ([storyboard] if storyboard else []) + color
    kept = ordered[:4]
    for index, item in enumerate(kept, 1):
        item["id"] = "ref_%02d" % index
        item["index"] = index
    dropped = approval_only + [dict(
        item, reason="gateway_reference_limit") for item in ordered[4:]]
    # 契约强制：每条 typed reference 必须通过 reference-contract（fail-closed）
    for item in kept:
        schema_validate.enforce(item, "reference-contract",
                                context="script_splitter._collect_typed_references")
    return kept, dropped


def _remap_reference_registry_to_confirmed_boards(reference_registry, confirmed_stage_paths):
    """Point storyboard tags at confirmed boards before video handoff.

    The storyboard registry is authored early from raw product/character inputs.
    Once customers confirm generated boards, video must inherit those confirmed
    boards rather than silently falling back to raw identity images under the
    same @tag.
    """
    registry = []
    for item in reference_registry or []:
        if not isinstance(item, dict):
            continue
        ref = dict(item)
        ref_type = ref.get("type")
        # Never replace an authored single-person identity reference with the
        # confirmed six-view contact sheet. The board proves consistency but is
        # not a valid video-model frame.
        if (ref_type in ("product_identity", "product_board") and
                confirmed_stage_paths.get("product_boards")):
            _ref_type, label, role, intent, _tag = _REFERENCE_META["product_boards"]
            ref.update({"url": confirmed_stage_paths["product_boards"],
                        "type": "product_board",
                        "source": "asset_refs.product_boards",
                        "label": label, "role": role, "intent": intent})
        if (ref_type == "product_usage_identity" and
                confirmed_stage_paths.get("product_usage_images")):
            _ref_type, label, role, intent, _tag = _REFERENCE_META["product_usage_images"]
            ref.update({"url": confirmed_stage_paths["product_usage_images"],
                        "type": "product_usage_identity",
                        "source": "asset_refs.product_usage_images",
                        "label": label, "role": role, "intent": intent})
        registry.append(ref)
    return registry


def _confirmed_generated_board_current(storyboard_dir, approval_kind, item,
                                       source_fingerprint, storyboard_module):
    if not isinstance(item, dict) or item.get("status") != "confirmed":
        return False
    if not source_fingerprint:
        return False
    # Hashes and retrieve URLs prove artifact identity, not customer consent.
    # The sole formal authorization is the current customer-evidenced marker.
    return bool(storyboard_module._approval_current(
        storyboard_dir, approval_kind, source_fingerprint))


def _legacy_reference_roles(references):
    return [{"label": ref["label"], "role": ref["role"], "intent": ref["intent"],
             "type": ref["type"], "scope": ref["scope"], "ref_id": ref["id"],
             "tag": ref.get("tag"),
             "ref_index": index}

            for index, ref in enumerate(references or [], 1)]


def _build_clip_contract(plan, shot, references, ratio, style, duration):
    authored = shot.get("clip_contract") or {}
    scope = dict(authored.get("scope") or {})
    for key in ("already_happened", "this_clip_only", "reserved_for_later", "endpoint"):
        if key in shot and key not in scope:
            scope[key] = shot[key]
    # A compiled segment is one authored storyboard panel, not a request for a
    # model to infer several beats from a contact sheet.
    ref_tags = shot.get("ref_tags") or [ref.get("tag") for ref in references
                                         if ref.get("tag") and ref.get("type") != "storyboard_composition"]
    if isinstance(ref_tags, str):
        ref_tags = [ref_tags]
    beats = [{
        "panel_index": int(shot.get("panel_index") or 1), "ref_tags": ref_tags,
        "start": 0, "end": duration,
        "action": shot.get("character_action") or shot.get("action") or shot.get("visual"),
        "camera": shot.get("camera_movement") or shot.get("camera"),
    }]
    contract = {
        "version": 1,
        "scope": scope,
        "scopes": {
            "global": {"ratio": ratio, "medium": "photorealistic live-action",
                       "style": style or ""},
            "scene": {"scene_id": shot.get("scene_id"),
                      "reference_ids": [r["id"] for r in references if r.get("scope") in ("global", "scene")]},
            "clip": {"clip_id": str(shot.get("id") or ""), "duration": duration,
                     "reference_ids": [r["id"] for r in references],
                     "continuation_mode": "fresh_scene",
                     "narrative_function": shot.get("narrative_function") or plan.get("narrative_function"),
                     "felt_intent": shot.get("felt_intent") or plan.get("felt_intent"),
                     "director_voice": shot.get("director_voice") or plan.get("director_voice"),
                     "arc_position": shot.get("arc_position") or plan.get("arc_position"),
                     "is_pattern_break": bool(shot.get("is_pattern_break"))},
            "beats": beats,
        },
    }
    # 契约强制：clip-contract 运行时校验（fail-closed）
    schema_validate.enforce(contract, "clip-contract",
                            context="script_splitter._build_clip_contract")
    return contract


def _reference_bindings(shot, references, has_human, has_product):
    """Add shot-local semantic roles without changing stable identity tags."""
    bindings = []
    for ref in references:
        tag = ref.get("tag")
        if not tag or ref.get("type") == "storyboard_composition":
            continue
        ref_type = ref.get("type")
        if ref_type == "product_usage_identity":
            role = "interaction_anchor"
        elif ref_type in ("character_board", "character_identity"):
            role = "active_operator" if has_human and has_product else "primary_subject"
        elif ref_type in ("product_board", "product_identity"):
            role = "product_focus"
        elif ref_type == "scene_environment":
            role = "environment_anchor"
        else:
            role = "identity_anchor"
        bindings.append({"tag": tag, "role": role, "must_be_visible": role != "environment_anchor",
                         "visibility_priority": "high" if role in ("primary_subject", "product_focus", "interaction_anchor") else "normal",
                         "allowed_transformations": "identity_only" if role != "environment_anchor" else "layout_lighting_only"})
    return bindings


def _voice_direction_text(voice_brief):
    voice_brief = voice_brief if isinstance(voice_brief, dict) else {}
    delivery = voice_brief.get("delivery") or {}
    parts = [voice_brief.get("voice_type")]
    for key in ("tone", "pace", "articulation", "emphasis", "ending"):
        if delivery.get(key):
            parts.append(delivery[key])
    return "；".join(str(item).strip() for item in parts if str(item or "").strip())


def _build_audio_contract(plan, shot, dialogue, approved_direction=None):
    """Normalize authored audio intent without asking downstream QC to infer it."""
    audio = dict(plan.get("audio") or {})
    audio.update(shot.get("audio") or {})
    direction = ((approved_direction or {}).get("contract") or {})
    if direction:
        audio_direction_contract.validate(direction)
    voice_brief = direction.get("voice_brief") or {}
    delivery = voice_brief.get("delivery") or {}
    dialogue = (dialogue or "").strip()
    mode = direction.get("audio_mode") or audio.get("audio_mode")
    if mode == "music_only" and dialogue:
        raise ValueError(
            "AUDIO_DIRECTION_DIALOGUE_CONFLICT: 已确认纯音乐，但分镜仍包含台词")
    speech = bool(dialogue)
    bgm = voice_brief.get("bgm") if "bgm" in voice_brief else audio.get("bgm")
    sfx = audio.get("sfx")
    bgm_enabled = str(bgm or "").strip().lower() not in {
        "", "none", "off", "no", "无", "不需要", "不要"}
    track = "required" if speech or bgm_enabled or sfx else "none"
    language = (voice_brief.get("language") or audio.get("language") or
                shot.get("language") or plan.get("language"))
    language_contract.ensure_dialogue_language(
        dialogue, language, context="shot %s" % (shot.get("id") or "unknown"))
    contract = {
        "track": track,
        "speech": speech,
        "dialogue": dialogue,
        "language": language,
        "language_code": language_contract.normalize_language(language),
        "audio_mode": mode,
        "voice": (voice_brief.get("voice_type") or audio.get("voice") or
                  audio.get("voice_type") or shot.get("voice_type") or
                  plan.get("voice_type")),
        "voice_direction": _voice_direction_text(voice_brief),
        "voice_tone": delivery.get("tone"),
        "voice_brief": voice_brief,
        "bgm": bgm,
        "sfx": sfx,
        "lip_sync": (False if mode in {"voiceover", "music_only"} else
                     bool(audio.get("lip_sync", speech and bool(shot.get("characters"))))),
        "voice_continuity_method": audio.get("voice_continuity_method") or "text_contract_and_human_qc",
        "bgm_continuity_method": audio.get("bgm_continuity_method") or
        ("post_mix_preferred" if bgm else "none"),
        "sfx_continuity_method": audio.get("sfx_continuity_method") or "text_contract_and_human_qc",
        "media_reference_method": audio.get("media_reference_method") or
        "basicrouter_video_v1_has_no_public_audio_reference_field",
    }
    if approved_direction:
        contract["approved_direction"] = {
            "artifact": approved_direction.get("artifact"),
            "stage_input_fingerprint": approved_direction.get(
                "stage_input_fingerprint"),
            "voice_owner": audio_direction_contract.VOICE_OWNER,
            "first_generated_audition_stage":
                audio_direction_contract.FIRST_GENERATED_AUDITION_STAGE,
        }
    return contract


def _segment_product_facts(plan, shot):
    """Carry confirmed product facts into downstream video prompt compression."""
    facts = dict((plan or {}).get("product_facts") or {})
    for source, target in (
            ("product_name", "product_name"),
            ("product", "product_name"),
            ("model", "model"),
            ("product_type", "product_type"),
            ("product_color", "product_color"),
            ("color", "color")):
        value = (shot or {}).get(source) or (plan or {}).get(source)
        if value and not facts.get(target):
            facts[target] = value
    features = (shot or {}).get("features") or (plan or {}).get("features")
    if features and not facts.get("features"):
        facts["features"] = features
    return facts


_ORAL_SCENE_TYPES = {
    "oral-broadcast", "oralbroadcast", "broadcast", "口播", "普通口播"
}
_EXPLAINER_SCENE_TYPES = {
    "explainer", "explainer-video", "document-explainer", "ppt-explainer",
    "presentation-explainer", "讲解", "ppt讲解", "文档讲解"
}
_DESIGN_BOARD_KINDS = {
    "design-board", "design_board", "motion-design-board", "motion_design_board",
    "layout-board", "layout_board", "customer-preview", "customer_preview",
    "动效设计板", "设计板"
}
_VIDEO_STORYBOARD_KINDS = {
    "video-storyboard", "video_storyboard", "storyboard-ref", "storyboard_ref",
    "narrative-storyboard", "narrative_storyboard", "剧情分镜"
}


def _normalize_scene_type(plan):
    return str(
        (plan or {}).get("scene_type") or (plan or {}).get("sceneType") or
        (plan or {}).get("scenario") or (plan or {}).get("workflow") or
        (plan or {}).get("skill") or (plan or {}).get("command") or ""
    ).strip().lower().replace("_", "-")


def _storyboard_contract_value(plan):
    contract = (plan or {}).get("storyboard_contract") or {}
    if not isinstance(contract, dict):
        contract = {}
    return str(
        (plan or {}).get("storyboard_role") or contract.get("kind") or
        contract.get("role") or contract.get("usage") or ""
    ).strip().lower()


def _plan_looks_like_document_explainer(plan):
    if not isinstance(plan, dict):
        return False
    text = " ".join(str(plan.get(key) or "") for key in (
        "source_ppt", "source_pdf", "source_doc", "source_document",
        "slide_images_dir", "content_strategy", "document_type"))
    lowered = text.lower()
    if any(token in lowered for token in ("ppt", "powerpoint", "pdf", "word", "docx",
                                          "slides", "slide", "文档", "讲解页")):
        return True
    if plan.get("content_spec") or plan.get("content_shotlist"):
        return True
    for shot in plan.get("shots") or []:
        if shot.get("source_slides") or shot.get("content_component"):
            return True
    return False


def _plan_has_physical_product_subject(plan):
    """Detect product oral/showcase jobs that must keep storyboard-to-video refs."""
    if not isinstance(plan, dict):
        return False
    asset_keys = {
        "product_images", "product_boards", "product_usage_images",
        "usage_reference_images", "product_reference_images",
    }
    asset_refs = plan.get("asset_refs") or {}
    if any(asset_refs.get(key) for key in asset_keys):
        return True
    if any(plan.get(key) for key in (
            "product_sku", "product_refs", "product_assets", "physical_product",
            "product_image", "product_board", "usage_geometry_review")):
        return True
    if str(plan.get("product_kind") or "").strip().lower() in (
            "physical", "physical_product", "实体产品", "实物产品"):
        return True
    for item in plan.get("references") or []:
        if isinstance(item, dict) and item.get("type") in (
                "product_identity", "product_board", "product_usage_identity"):
            return True
    for shot in plan.get("shots") or []:
        if any(shot.get(key) for key in (
                "product_sku", "product_refs",
                "force_usage_reference", "physical_product")):
            return True
        shot_refs = shot.get("asset_refs") or {}
        if any(shot_refs.get(key) for key in asset_keys):
            return True
        for item in shot.get("references") or []:
            if isinstance(item, dict) and item.get("type") in (
                    "product_identity", "product_board", "product_usage_identity"):
                return True
    return False


def _plan_looks_like_document_or_solution(plan):
    if _plan_looks_like_document_explainer(plan):
        return True
    text = " ".join(str((plan or {}).get(key) or "") for key in (
        "source_outline", "brief_type", "content_strategy", "topic_type",
        "document_type", "material_type"
    )).lower()
    if any(token in text for token in (
            "ppt", "powerpoint", "pdf", "word", "docx", "document", "deck",
            "proposal", "solution", "service", "slides", "文档", "方案", "服务")):
        return True
    for shot in (plan or {}).get("shots") or []:
        tags = _tags(shot.get("ref_tags")) + _tags(shot.get("design_ref_tags"))
        if any(str(tag).startswith("@ppt_slide_") for tag in tags):
            return True
        visual_text = "%s %s" % (shot.get("visual") or "", shot.get("camera") or "")
        if any(token in visual_text.lower() for token in (
                "ppt", "slide", "document", "deck", "方案", "文档", "页面")):
            return True
    return False


def storyboard_is_design_board(plan):
    """Whether storyboard images are customer design previews, not video refs."""
    value = _storyboard_contract_value(plan)
    normalized = value.replace("_", "-")
    has_physical_product = _plan_has_physical_product_subject(plan)
    looks_like_document = _plan_looks_like_document_or_solution(plan)
    if value in _VIDEO_STORYBOARD_KINDS or normalized in _VIDEO_STORYBOARD_KINDS:
        return False
    if value in _DESIGN_BOARD_KINDS or normalized in _DESIGN_BOARD_KINDS:
        return not has_physical_product
    scene_type = _normalize_scene_type(plan)
    if scene_type in _ORAL_SCENE_TYPES:
        return looks_like_document and not has_physical_product
    if scene_type in _EXPLAINER_SCENE_TYPES and _plan_looks_like_document_explainer(plan):
        return True
    return False


def _tags(value):
    if isinstance(value, str):
        return [value]
    return [str(item) for item in (value or []) if item]


def _design_board_video_ref_tags(shot, plan_refs):
    explicit = (
        _tags((shot or {}).get("video_ref_tags")) or
        _tags((shot or {}).get("video_model_ref_tags")) or
        _tags((shot or {}).get("model_ref_tags"))
    )
    if explicit:
        background_urls = set((plan_refs or {}).get("background_images") or [])
        background_tags = [item.get("tag") for item in (plan_refs or {}).get("reference_registry") or []
                           if isinstance(item, dict) and item.get("type") == "scene_environment"
                           and item.get("url") in background_urls and item.get("tag")]
        return list(dict.fromkeys(explicit + background_tags))
    registry_by_tag = {
        item.get("tag"): item for item in ((plan_refs or {}).get("reference_registry") or [])
        if isinstance(item, dict) and item.get("tag")
    }
    keep_types = {
        "character_identity",
        "product_identity", "product_board", "product_usage_identity",
        "scene_environment",
    }
    kept = []
    for tag in _tags((shot or {}).get("ref_tags")):
        ref_type = (registry_by_tag.get(tag) or {}).get("type")
        if ref_type in keep_types:
            kept.append(tag)
    background_urls = set((plan_refs or {}).get("background_images") or [])
    for item in (plan_refs or {}).get("reference_registry") or []:
        if (isinstance(item, dict) and item.get("type") == "scene_environment"
                and item.get("url") in background_urls and item.get("tag")):
            kept.append(item["tag"])
    return list(dict.fromkeys(kept))


def _video_reference_shot_for_design_board(shot, plan_refs):
    video_shot = dict(shot or {})
    video_shot["ref_tags"] = _design_board_video_ref_tags(shot, plan_refs)
    return video_shot


def _video_plan_refs_for_design_board(plan_refs, video_shot):
    plan_refs = dict(plan_refs or {})
    wanted = set(_tags((video_shot or {}).get("ref_tags")))
    if plan_refs.get("reference_registry"):
        plan_refs["reference_registry"] = [
            item for item in plan_refs.get("reference_registry") or []
            if not wanted or item.get("tag") in wanted
        ]
    # In PPT/document explainers, scene_images are usually rendered slides for
    # deterministic Remotion/HyperFrames composition, not video-model anchors.
    if not any(str(tag).startswith("@scene") for tag in wanted):
        plan_refs.pop("scene_images", None)
    plan_refs.pop("layout_previews", None)
    return plan_refs


def _normalized_plan_refs(plan):
    """Separate PPT pages from the clean background used by the video model."""
    refs = dict((plan or {}).get("asset_refs") or {})
    backgrounds = (
        refs.get("background_images") or refs.get("video_model_background_refs") or
        (plan or {}).get("background_images") or (plan or {}).get("video_model_background_refs") or [])
    if isinstance(backgrounds, str):
        backgrounds = [backgrounds]
    backgrounds = list(dict.fromkeys(str(item) for item in backgrounds if item))
    refs["background_images"] = backgrounds
    registry = [dict(item) for item in refs.get("reference_registry") or []
                if isinstance(item, dict)]
    registered_urls = {item.get("url") for item in registry}
    for index, url in enumerate(backgrounds, 1):
        if url in registered_urls:
            continue
        registry.append({
            "tag": "@background" if index == 1 else "@background%d" % index,
            "type": "scene_environment", "url": url,
            "source": "asset_refs.background_images",
        })
    refs["reference_registry"] = registry
    return refs


def _background_description(plan):
    continuity = (plan or {}).get("continuity")
    if isinstance(continuity, dict):
        continuity = continuity.get("background") or continuity.get("scene")
    elif isinstance(continuity, list):
        continuity = "；".join(str(item) for item in continuity
                              if "背景" in str(item) or "场景" in str(item))
    return str(
        (plan or {}).get("background_identity") or (plan or {}).get("background_style") or
        (plan or {}).get("background") or continuity or
        "同一固定讲解空间、同一机位、同一景深、同一灯光方向与色温"
    ).strip()


def _build_background_identity_lock(plan, references, *, requires_reference=False):
    anchors = [item for item in (references or [])
               if ((item.get("type") == "scene_environment" and
                    (str(item.get("tag") or "").startswith("@background") or
                     item.get("source") == "asset_refs.background_images")) or
                   item.get("source") in {
                       "approved_identity_plate",
                       "identity_plate.approved_candidate"})]
    return {
        "version": 1,
        "scope": "run",
        "description": _background_description(plan),
        "reference_urls": [item.get("url") for item in anchors if item.get("url")],
        "reference_tags": [item.get("tag") for item in anchors if item.get("tag")],
        "mode": "reference_plus_text" if anchors else "text_only_weak",
        "requires_reference": bool(requires_reference),
        "forbid_relayout": True,
        "forbid_relighting": True,
    }


def _pick_video_type(n_urls, has_human, has_environment=False):
    """按锚定图数量与是否含数字人自动选 videoType。

    videoType 语义（与网关 allowVideoType 对齐）：
      1=文生 2=单图首帧 3=首尾帧 4=单张参考图(仅 kling) 5=多图/多主体(含 seedance)
    选型：
      - 无图 → 1
      - 含数字人 + 多图（人物板 + 场景图/产品图，人景同框/多主体）→ 5
        （多图多主体 seedance-2.0 自身支持，_pick_video_model 会保持 seedance，更快更省）
       - 含产品/场景环境素材 → 5（即使当前只有一张环境图，也预留多主体/人景一致性能力）
       - 含数字人 + 单图（仅一张人物锚定图）→ 4（单张身份锚定，仅 kling，会自动回落 kling）
      - 无数字人 + 多图 → 5；无数字人 + 单图 → 2
    """
    if n_urls == 0:
        return 1
    if has_environment:
        return 5
    if n_urls >= 2:
        return 5   # 多图：人景同框/多主体/多方位——seedance 也支持，不强制 kling
    if has_human:
        return 4   # 单张数字人参考图身份锚定：仅 kling
    return 2       # 单图生视频首帧


# voice_type → 中文口播语速（字/秒）。基于实际 AIGC 视频生成模型的语音合成实测数据。
# 慢速：情感旁白/纪录片；标准：一般口播；快速：促销/快节奏带货。
_VOICE_SPEECH_RATES = {
    "slow": 3.2,       # 慢速旁白、情感类
    "calm": 3.5,       # 沉稳、纪录片
    "narrator": 3.8,   # 旁白解说
    "standard": 4.2,   # 标准口播（默认）
    "professional": 4.5,  # 专业播报
    "energetic": 5.0,  # 活力带货
    "fast": 5.5,       # 快节奏促销
    "promo": 5.8,      # 极速促销
}
_DEFAULT_SPEECH_RATE = 4.2  # 无 voice_type 时的默认语速


def _speech_rate_for_shot(shot, plan_voice_type=None):
    """Determine speech rate (chars/sec) for a shot based on voice_type context."""
    return dialogue_timing.speech_rate(
        shot, plan_voice_type=plan_voice_type)


def _shot_duration_seconds(shot, min_seconds, speech_rate=None):
    """取该镜时长（秒）。优先 shot.duration；缺失则按台词字数估（按 voice_type 调速）。"""
    d = shot.get("duration") or shot.get("seconds")
    if isinstance(d, (int, float)) and d > 0:
        return max(min_seconds, int(round(d)))
    dialogue = shot.get("dialogue") or shot.get("voiceover") or ""
    rate = speech_rate or _speech_rate_for_shot(shot)
    est = len(re.sub(r"\s", "", dialogue)) / rate if dialogue else min_seconds
    return max(min_seconds, int(round(est)))


def _validate_dialogue_fit(shot, duration, speech_rate=None):
    """Check if dialogue fits within the allocated duration.

    Returns (fits, expected_seconds, message). When dialogue is too long,
    the model will either rush the speech (causing lip-sync issues) or
    truncate the dialogue (causing missing content).
    """
    dialogue = shot.get("dialogue") or shot.get("voiceover") or ""
    if not dialogue:
        return True, 0, None
    rate = speech_rate or _speech_rate_for_shot(shot)
    timing = dialogue_timing.analyze(
        dialogue, duration, item=dict(shot, voice_type=(
            shot.get("voice_type") if speech_rate is None else "standard")))
    if speech_rate is not None:
        units = dialogue_timing.speech_units(dialogue)
        expected = units / rate
        fits = expected <= float(duration) * 1.10
    else:
        expected = timing["estimated_speech_seconds"]
        fits = timing["fits"]
    if not fits:
        return False, round(expected, 1), (
            "台词时长(%.1fs)超出镜头分配时长(%ds)的10%%容差。"
            "预计需要%.1fs但只分配了%ds，模型会被迫加速念词导致嘴形配音不同步。"
            "建议：①缩减台词至%d字以内 ②增加该镜头时长至%ds ③拆分为两个镜头"
            % (expected, duration, expected, duration,
               int(duration * rate * 0.95), int(expected) + 1))
    return True, round(expected, 1), None


def _dialogue_speech_units(dialogue):
    """Compatibility wrapper around the shared mixed-language estimator."""
    return dialogue_timing.speech_units(dialogue)


def _check_scene_consistency(shots):
    """Warn when shots sharing a scene_id have divergent scene_prompt descriptions.

    This is a soft check (print warning, not raise) to avoid blocking valid
    creative choices while surfacing likely authoring mistakes.
    """
    from difflib import SequenceMatcher
    scenes = {}
    for shot in shots:
        sid = str(shot.get("scene_id") or "").strip()
        prompt = str(shot.get("scene_prompt") or "").strip()
        if not sid or not prompt:
            continue
        scenes.setdefault(sid, []).append((shot.get("id"), prompt))
    for sid, entries in scenes.items():
        if len(entries) < 2:
            continue
        base_id, base_prompt = entries[0]
        for other_id, other_prompt in entries[1:]:
            ratio = SequenceMatcher(None, base_prompt, other_prompt).ratio()
            if ratio < 0.3:
                print("[scene-check] WARNING: scene_id=%s 的 shots %s 和 %s "
                      "scene_prompt 差异很大（相似度 %.0f%%），请确认是否为同一场景"
                      % (sid, base_id, other_id, ratio * 100), flush=True)


def _extract_clothing_lock(plan, shot):
    """Extract clothing description from character metadata for identity lock.

    Sources (in priority order):
      1. shot.character.clothing / shot.clothing
      2. plan.characters[].clothing (matched by shot.characters[0])
      3. plan.cast_description / plan.character_description
    Returns a clothing description string, or None if not available.
    """
    # Shot-level clothing
    char = shot.get("character") or {}
    if isinstance(char, dict) and char.get("clothing"):
        return str(char["clothing"]).strip()
    if shot.get("clothing"):
        return str(shot["clothing"]).strip()

    # Plan-level characters
    shot_chars = shot.get("characters") or []
    plan_chars = plan.get("characters") or []
    if shot_chars and plan_chars:
        # Match by character name/id
        target = str(shot_chars[0]).strip().lower()
        for pc in plan_chars:
            if not isinstance(pc, dict):
                continue
            pc_name = str(pc.get("name") or pc.get("id") or "").strip().lower()
            if pc_name == target or pc_name in target or target in pc_name:
                clothing = pc.get("clothing") or pc.get("outfit") or pc.get("costume")
                if clothing:
                    return str(clothing).strip()

    # Fallback: plan-level description
    for key in ("cast_description", "character_description", "actor_description"):
        desc = plan.get(key)
        if desc and isinstance(desc, str):
            # Try to extract clothing from description
            import re as _re
            m = _re.search(r"穿[着戴]?([^，。；;]+)", desc)
            if m:
                return m.group(1).strip()
    return None


def _inject_scene_transitions(shots):
    """Add transition hints at scene boundaries so the model generates a
    smooth visual transition instead of a hard cut between scenes."""
    prev_scene = None
    for shot in shots:
        scene_id = str(shot.get("scene_id") or "").strip() or None
        if prev_scene is not None and scene_id != prev_scene:
            # Scene boundary detected — inject transition hint
            hint = "场景过渡：从上一场自然转场到当前场景"
            existing = shot.get("continuity_in") or ""
            if hint not in existing:
                shot["continuity_in"] = (existing + "；" + hint).strip("；") if existing else hint
        prev_scene = scene_id


def _is_empty_continuity(value):
    if value is None:
        return True
    if isinstance(value, dict):
        return not value
    text = str(value).strip()
    return text in ("", "{}", "null", "None")


def _segment_state_summary(segment):
    def _short(value, limit=72):
        text = str(value or "").strip()
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    timeline = segment.get("timeline") or []
    beat = timeline[0] if timeline and isinstance(timeline[0], dict) else {}
    parts = []
    for key, label in (
            ("shot_size", "景别"), ("camera", "镜头"), ("composition", "构图"),
            ("character_action", "动作")):
        value = beat.get(key) or segment.get(key)
        if value:
            parts.append("%s=%s" % (label, _short(value)))
    refs = segment.get("ref_tags") or []
    if refs:
        parts.append("参考=%s" % " ".join(refs))
    if not parts:
        parts.append(_short(segment.get("text") or segment.get("id") or "当前镜头状态"))
    return "；".join(str(item) for item in parts)


def _inject_segment_continuity_fallbacks(segments):
    output = []
    total = len(segments or [])
    for index, segment in enumerate(segments or []):
        item = dict(segment)
        state = _segment_state_summary(item)
        previous_state = _segment_state_summary(output[index - 1]) if index > 0 else None
        next_segment = segments[index + 1] if index + 1 < total else None
        next_state = _segment_state_summary(next_segment) if next_segment else None
        if _is_empty_continuity(item.get("continuity_in")):
            if previous_state:
                item["continuity_in"] = (
                    "承接上一段结尾状态：%s。本段从当前故事板镜头参考的主体位置、光线和镜头方向开始：%s。"
                    % (previous_state, state))
            else:
                item["continuity_in"] = (
                    "从本段故事板镜头参考建立的主体位置、产品比例、场景光线和镜头方向开始：%s。"
                    % state)
        if _is_empty_continuity(item.get("continuity_out")):
            if next_state:
                item["continuity_out"] = (
                    "本段结束时保持：%s。为下一段预留自然剪辑关系：%s。"
                    % (state, next_state))
            else:
                item["continuity_out"] = "本段以稳定清晰的产品/人物状态收尾：%s。" % state
        output.append(item)
    return output


def _audio_state_summary(segment):
    audio = segment.get("audio_contract") or {}
    parts = []
    for key, label in (("language", "语言"), ("voice", "声音"),
                       ("bgm", "BGM"), ("sfx", "音效")):
        if audio.get(key):
            parts.append("%s=%s" % (label, audio[key]))
    return "；".join(str(item) for item in parts)


def _inject_audio_continuity_fallbacks(segments):
    output = []
    total = len(segments or [])
    for index, segment in enumerate(segments or []):
        item = dict(segment)
        audio = dict(item.get("audio_contract") or {})
        if audio.get("track") == "required":
            previous_audio = _audio_state_summary(output[index - 1]) if index > 0 else None
            next_audio = _audio_state_summary(segments[index + 1]) if index + 1 < total else None
            audio.setdefault(
                "voice_continuity",
                ("承接上一段普通话女声的人设、音色、响度和语速，不突然换声线；当前段执行：%s。" %
                 (audio.get("voice") or "保持同一中文普通话年轻女声"))
                if previous_audio else
                ("建立全片统一普通话女声基准，后续段保持同一音色、响度和口播距离；当前段执行：%s。" %
                 (audio.get("voice") or "中文普通话年轻女声")))
            audio.setdefault(
                "bgm_continuity",
                ("BGM 从上一段节拍和情绪自然延续，不重置、不突兀切换；当前段：%s；下一段预期：%s。" %
                 (audio.get("bgm") or "保持同一音乐底色", next_audio or "收束"))
                if previous_audio else
                ("建立全片统一潮流电子节拍底色；当前段：%s；后续段只做强弱和收束变化。" %
                 (audio.get("bgm") or "轻快潮流电子节拍")))
            audio.setdefault(
                "sfx_continuity",
                "音效只做动作点缀，贴合画面动作，不盖过人声；当前段：%s。" %
                (audio.get("sfx") or "无额外音效"))
            audio.setdefault("voice_continuity_method", "text_contract_and_human_qc")
            audio.setdefault("bgm_continuity_method",
                             "post_mix_preferred" if audio.get("bgm") else "none")
            audio.setdefault("sfx_continuity_method", "text_contract_and_human_qc")
            audio.setdefault(
                "media_reference_method",
                "basicrouter_video_v1_has_no_public_audio_reference_field")
            item["audio_contract"] = audio
        output.append(item)
    return output


def _compile_segment_references(context, shot, index):
    i = index
    stale_storyboard_shots = context["stale_storyboard_shots"]
    shot_map = context["shot_map"]
    storyboard_dir = context["storyboard_dir"]
    design_board = context["design_board"]
    plan_refs = context["plan_refs"]
    bw_storyboard = context["bw_storyboard"]
    allow_text2video = context["allow_text2video"]
    asset_prep = context["asset_prep"]
    client = context["client"]
    allow_unconfirmed = context["allow_unconfirmed"]
    missing = context["missing"]
    needs_image = context["needs_image"]
    unconfirmed_refs = context["unconfirmed_refs"]
    dropped_references = context["dropped_references"]
    sid = str(shot.get("id") or (i + 1))
    source_ids = shot.get("source_shot_ids") or [shot.get("source_shot_id") or sid]
    if any(str(source_id) in stale_storyboard_shots for source_id in source_ids):
        missing.append(sid)
        return None
    shot_image_record = _find_shot_image(shot_map, sid, source_ids)
    shot_image_path = ((shot_image_record or {}).get("path")
                       if isinstance(shot_image_record, dict) else shot_image_record)
    shot_image_url = ((shot_image_record or {}).get("url")
                      if isinstance(shot_image_record, dict) else None)
    if storyboard_dir and not shot_image_path and not design_board:
        missing.append(sid)
    video_ref_shot = (_video_reference_shot_for_design_board(shot, plan_refs)
                      if design_board else shot)
    video_plan_refs = (_video_plan_refs_for_design_board(plan_refs, video_ref_shot)
                       if design_board else plan_refs)
    # shot 级 color_mode 可覆盖 plan 级
    shot_bw = bw_storyboard
    if shot.get("color_mode"):
        shot_bw = str(shot["color_mode"]).lower() in ("bw", "black_white", "grayscale", "mono", "黑白")
    references, dropped = _collect_typed_references(
        video_ref_shot, video_plan_refs,
        None if design_board else (shot_image_url or shot_image_path),
        bw_storyboard=shot_bw,
        include_storyboard_ref=not design_board)
    urls = [ref["url"] for ref in references]
    dropped_references.extend(dict(item, shot_id=sid) for item in dropped)
    if not urls and not allow_text2video:
        # 铁律#11：零锚定素材 → 不静默降级为纯文本生视频，记入 needs_image 跳过
        needs_image.append(sid)
        return None

    if asset_prep is not None:
        # Storyboard approval is checked above as one content-bound unit;
        # asset_prep only owns source product/character/scene candidates.
        generated_sources = {
            "asset_refs.product_usage_images", "asset_refs.cast_boards",
            "asset_refs.product_boards", "approved_identity_plate",
            "identity_plate.approved_candidate",
        }
        asset_urls = [ref["url"] for ref in references
                      if ref.get("source") != "storyboard" and
                      ref.get("source") not in generated_sources]
        shot_unconfirmed = [u for u in asset_urls if not asset_prep.is_confirmed(client, u)]
        if shot_unconfirmed:
            unconfirmed_refs.extend({"shot_id": sid, "url": u} for u in shot_unconfirmed)
            if not allow_unconfirmed:
                raise ValueError(
                    "UNCONFIRMED_ASSET: shot %s 引用了未经客户确认(status=pending)的"
                    "候选素材图 %r。这类图可能是两遍清洗里客户还没选定/可能被拒绝的"
                    "版本，不能静默当成最终锚定素材出片。请先让客户走 asset_prep.py "
                    "confirm-image 确认，或显式传 allow_unconfirmed=True 明确接受用"
                    "未确认素材（如仅做草稿预览）。" % (sid, shot_unconfirmed[0]))
    return {
        "sid": sid, "source_ids": source_ids,
        "shot_image_path": shot_image_path, "shot_image_url": shot_image_url,
        "video_ref_shot": video_ref_shot, "video_plan_refs": video_plan_refs,
        "shot_bw": shot_bw, "references": references, "dropped": dropped,
        "urls": urls,
    }


def _compile_segment_subject(context, shot, resolved):
    shots = context["shots"]
    design_board = context["design_board"]
    plan = context["plan"]
    draft_allow_unapproved_storyboard = context["draft_allow_unapproved_storyboard"]
    min_seconds = context["min_seconds"]
    video_plan_refs = resolved["video_plan_refs"]
    video_ref_shot = resolved["video_ref_shot"]
    references = resolved["references"]
    dropped = resolved["dropped"]
    urls = resolved["urls"]
    sid = resolved["sid"]
    merged_refs = dict(video_plan_refs)
    merged_refs.update(video_ref_shot.get("asset_refs") or {})
    has_human = bool(shot.get("characters")) or bool(merged_refs.get("digital_human_portraits"))
    has_product = bool(merged_refs.get("product_images") or
                       merged_refs.get("product_boards") or
                       merged_refs.get("product_usage_images") or
                       shot.get("product_sku") or shot.get("product_refs"))
    scene_prompt_value = shot.get("scene_prompt")
    shot_size_only = {
        "中景", "近景", "远景", "全景", "特写", "大特写", "半身", "全身",
        "medium shot", "close-up", "close up", "wide shot", "long shot",
    }
    normalized_scene_prompt = str(scene_prompt_value or "").strip().lower()
    # Legacy normalization sometimes copied `visual: 中景` into
    # scene_prompt. A shot size is not an environment reference and must
    # not turn a single portrait identity anchor (type4) into a multi-image
    # scene request (type5).
    authored_scene_prompt = bool(
        normalized_scene_prompt and
        normalized_scene_prompt != "preserve the authored scene" and
        normalized_scene_prompt not in shot_size_only and
        normalized_scene_prompt != str(shot.get("visual") or "").strip().lower())
    has_scene = bool(
        merged_refs.get("background_images") or merged_refs.get("scene_images") or
        shot.get("scene") or
        authored_scene_prompt)
    # has_environment 精细化：只有"多主体同框"或"人+环境"才算多主体场景。
    # 单产品静物（无人物无场景）不应触发多主体模式。
    # - 有人物 + 有环境 → 多主体（type 5）
    # - 有产品 + 有场景 → 多主体（type 5）
    # - 仅产品（无场景） → 单主体（type 2）
    # - 仅场景（无产品） → 单主体（type 2）
    env_element_count = sum([has_human, has_product, has_scene])
    has_environment = env_element_count >= 2
    usage_required = any(ref.get("type") == "product_usage_identity"
                         for ref in references)
    background_required = bool(
        context.get("formal_avatar_plate") or
        (has_human and len(shots) > 1 and
         (design_board or plan.get("background_reference_required"))))
    if context.get("formal_avatar_plate"):
        # The independently approved identity plate is both the clean person
        # anchor and the frozen background anchor.  Requiring a legacy cast
        # contact sheet or inventing a second scene image here would either
        # leak an approval-only board into the video model or make a valid
        # one-image provider request impossible.
        required_types = {"character_identity"}
    else:
        required_types = _required_reference_types(
            has_human=has_human, has_product=has_product,
            usage_required=usage_required,
            storyboard_required=not design_board,
            background_required=background_required)
    dropped_required = [item for item in dropped if item.get("type") in required_types]
    actual_required = {item.get("type") for item in references if item.get("type")}
    missing_required = sorted(required_types - actual_required)
    if missing_required and not draft_allow_unapproved_storyboard:
        raise ValueError(
            "REFERENCE_HANDOFF_INCOMPLETE: shot %s 缺少单视图参考类型 [%s]。"
            "人物六视图只能用于审批；请先确认单人 identity plate/portrait，不能把六宫格直接交给视频模型。" %
            (sid, ", ".join(missing_required)))
    if dropped_required and not draft_allow_unapproved_storyboard:
        raise ValueError(
            "REFERENCE_HANDOFF_INCOMPLETE: shot %s 的已确认参考图被网关上限丢弃：%s。"
            "请选择支持足够参考图数量的模型，或先生成一个包含全部必需身份/操作锚点的"
            "单一确认参考图；不得静默降级。" %
            (sid, ", ".join(item.get("label") or item.get("type")
                            for item in dropped_required)))
    if context.get("formal_avatar_plate"):
        if len(urls) != 1 or not has_human:
            raise ValueError(
                "FORMAL_AVATAR_SINGLE_IDENTITY_REFERENCE_REQUIRED: shot %s" % sid)
        # The approved identity plate already contains the frozen person and
        # clean background in one image.  A textual scene_prompt describes
        # that same plate; it is not a second environment input and must not
        # promote this one-image request to multi-image videoType 5.
        vtype = 4
        has_environment = False
    else:
        vtype = _pick_video_type(
            len(urls), has_human, has_environment=has_environment)
    secs = _shot_duration_seconds(shot, min_seconds)
    return {
        "has_human": has_human, "has_product": has_product,
        "has_scene": has_scene, "has_environment": has_environment,
        "background_required": background_required,
        "required_types": required_types, "vtype": vtype, "secs": secs,
    }


def _compile_segment_prompt(context, shot, resolved, subject):
    plan = context["plan"]
    ratio = context["ratio"]
    sid = resolved["sid"]
    video_ref_shot = resolved["video_ref_shot"]
    references = resolved["references"]
    secs = subject["secs"]
    has_human = subject["has_human"]
    has_product = subject["has_product"]
    dlg_fits, dlg_expected, dlg_msg = _validate_dialogue_fit(shot, secs)
    if not dlg_fits:
        if context.get("strict_dialogue_fit"):
            raise ValueError(
                "DIALOGUE_DURATION_OVERFLOW: shot %s — %s" %
                (sid, dlg_msg))
        print("[dialogue-fit] WARNING: shot %s — %s" % (sid, dlg_msg), flush=True)

    approved_avatar_prompt = str(
        shot.get("approved_avatar_plate_prompt") or "").strip()
    if approved_avatar_prompt:
        text = approved_avatar_prompt
    else:
        # 台词 + 表演指导 + 镜头语言，拼成音画一体出片 text
        parts = []
        if shot.get("dialogue") or shot.get("voiceover"):
            parts.append("【台词】" + (shot.get("dialogue") or shot.get("voiceover")))
        if shot.get("visual"):
            parts.append("【画面】" + shot["visual"])
        if shot.get("performance") or shot.get("action"):
            parts.append("【表演】" + (shot.get("performance") or shot.get("action")))
        if shot.get("camera"):
            parts.append("【镜头】" + shot["camera"])
        for key, label in (("shot_size", "景别"), ("angle_offset", "角度偏移"),
                           ("composition", "构图"), ("lighting", "灯光"),
                           ("character_action", "人物动作"),
                           ("micro_expression", "微表情"),
                           ("scene_prompt", "场景"), ("prop_prompts", "道具/产品")):
            value = shot.get(key)
            if isinstance(value, list):
                value = "；".join(str(v) for v in value)
            if value:
                parts.append("【%s】%s" % (label, value))
        text = "\n".join(parts) or (shot.get("visual") or "产品展示镜头")

    style = plan.get("visual_style") or (plan.get("render_profile") or {}).get("video_style_prompt", "")
    ref_tags = video_ref_shot.get("ref_tags") or [
        ref.get("tag") for ref in references
        if ref.get("tag") and ref.get("type") != "storyboard_composition"]
    if isinstance(ref_tags, str):
        ref_tags = [ref_tags]
    available_ref_tags = [ref.get("tag") for ref in references
                          if ref.get("tag") and ref.get("type") != "storyboard_composition"]
    ref_tags = [tag for tag in ref_tags if tag in set(available_ref_tags)]
    # A confirmed usage board is mandatory only when the storyboard
    # contract says this shot has a physical use relation. Plain
    # presenter/product CTA shots must not inherit magnetic-use geometry.
    if bool(shot.get("requires_usage")):
        usage_tags = [ref.get("tag") for ref in references
                      if ref.get("type") == "product_usage_identity" and ref.get("tag")]
        ref_tags = list(dict.fromkeys(list(ref_tags) + usage_tags))
    contract = _build_clip_contract(
        plan, dict(video_ref_shot, ref_tags=ref_tags), references, ratio, style, secs)
    reference_bindings = _reference_bindings(shot, references, has_human, has_product)
    clothing_lock = _extract_clothing_lock(plan, shot)
    return {
        "text": text, "style": style, "ref_tags": ref_tags,
        "contract": contract, "reference_bindings": reference_bindings,
        "clothing_lock": clothing_lock,
    }


def _compile_segment_contracts(context, shot, resolved, subject):
    plan = context["plan"]
    design_board = context["design_board"]
    references = resolved["references"]
    background_required = subject["background_required"]
    clothing_lock = _extract_clothing_lock(plan, shot)

    dialogue = (shot.get("dialogue") or shot.get("voiceover") or "").strip()
    audio_contract = _build_audio_contract(
        plan, shot, dialogue, context.get("approved_audio_direction"))
    background_lock = _build_background_identity_lock(
        plan, references,
        requires_reference=bool(
            background_required))
    avatar_plate_only = bool(design_board)
    composition_contract = {
        "design_board": "composite_plan",
        "video_model": "avatar_plate_only" if avatar_plate_only else "video_storyboard",
        "content_owner": "remotion" if avatar_plate_only else None,
        "motion_owner": "hyperframes" if avatar_plate_only else None,
        "assembly_owner": "fuse" if avatar_plate_only else "ffmpeg",
        "storyboard_ref_for_video_model": not avatar_plate_only,
    }
    video_model_contract = {
        "role": "avatar_plate_only" if avatar_plate_only else "story_continuity_plate",
        "allowed_locks": ["person", "voice", "wardrobe", "background"]
        if avatar_plate_only else ["person", "voice", "wardrobe", "background", "story_continuity"],
        "forbidden_inputs": [
            "ppt_page", "design_board", "title_area", "safe_zone", "highlight_card",
            "chart", "arrow", "annotation", "motion_grammar",
        ] if avatar_plate_only else [],
        # Keep this field schema-valid and machine-routable.  The individual
        # deterministic owners belong in ``composition_components``; packing
        # several implementation names into one enum made older plans fail
        # downstream validation and leaked internal engine names into previews.
        "composition_owner": "remotion" if avatar_plate_only else "storyboard",
        "composition_components": (
            ["remotion", "hyperframes", "ffmpeg"] if avatar_plate_only else
            ["video_model", "storyboard_ref"]),
        "video_model_usage": (
            "do_not_submit_as_storyboard_ref" if avatar_plate_only else
            "submit_per_shot"),
        "storyboard_ref": not avatar_plate_only,
    }
    return {
        "dialogue": dialogue, "audio_contract": audio_contract,
        "background_lock": background_lock,
        "avatar_plate_only": avatar_plate_only,
        "composition_contract": composition_contract,
        "video_model_contract": video_model_contract,
    }


def _assemble_compiled_segment(context, shot, index, resolved, subject,
                               prompt, contracts):
    i = index
    shots = context["shots"]
    client = context["client"]
    run_id = context["run_id"]
    approval_identity = context["approval_identity"]
    render_plan_identity = context["render_plan_identity"]
    identity_plate_artifact = context.get("identity_plate_artifact")
    ratio = context["ratio"]
    resolution = context["resolution"]
    design_board = context["design_board"]
    storyboard_dir = context["storyboard_dir"]
    plan_fingerprint = context["plan_fingerprint"]
    storyboard_fingerprint = context["storyboard_fingerprint"]
    storyboard = context["storyboard"]
    oral_broadcast = context["oral_broadcast"]
    scene_aware = context["scene_aware"]
    plan = context["plan"]
    sid = resolved["sid"]
    shot_image_path = resolved["shot_image_path"]
    shot_image_url = resolved["shot_image_url"]
    urls = resolved["urls"]
    references = resolved["references"]
    dropped = resolved["dropped"]
    required_types = subject["required_types"]
    secs = subject["secs"]
    vtype = subject["vtype"]
    has_human = subject["has_human"]
    text = prompt["text"]
    style = prompt["style"]
    ref_tags = prompt["ref_tags"]
    reference_bindings = prompt["reference_bindings"]
    clothing_lock = prompt["clothing_lock"]
    contract = prompt["contract"]
    dialogue = contracts["dialogue"]
    audio_contract = contracts["audio_contract"]
    background_lock = contracts["background_lock"]
    avatar_plate_only = contracts["avatar_plate_only"]
    composition_contract = contracts["composition_contract"]
    video_model_contract = contracts["video_model_contract"]
    segment = {
        "id": sid,
        "client": client,
        "run_id": run_id,
        "storyboard_approval": approval_identity,
        "scene_id": shot.get("scene_id"),
        "source_shot_ids": shot.get("source_shot_ids") or [shot.get("source_shot_id") or sid],
        "text": text,
        "dialogue": dialogue,
        "product_facts": _segment_product_facts(plan, shot),
        "audio_contract": audio_contract,
        "language": audio_contract.get("language"),
        "language_code": audio_contract.get("language_code"),
        "background_identity_lock": background_lock,
        "render_plan": render_plan_identity,
        "render_plan_fingerprint": _sha256_json(render_plan_identity),
        "identity_plate_artifact": identity_plate_artifact,
        "duration": secs,
        "video_type": vtype,
        "urls": urls,
        "references": references,
        "required_reference_types": sorted(required_types),
        "dropped_references": dropped,
        "ratio": ratio,
        "resolution": resolution,
        "storyboard_ref": not design_board,
        # Seedance can use native storyboard guidance. If generation falls
        # back to Kling, video_engine will prepare a single-shot keyframe.
        "storyboard_ref_mode": "native_storyboard" if not design_board else None,
        "storyboard_path": shot_image_path if not design_board else None,
        "storyboard_dir": storyboard_dir if not design_board else None,
        "storyboard_url": shot_image_url if not design_board else None,
        "storyboard_retrieve_url": shot_image_url if not design_board else None,
        "storyboard_usage": "design_board" if design_board else "video_storyboard",
        "design_board": bool(design_board),
        "generation_role": "avatar_plate_only" if avatar_plate_only else "story_continuity_plate",
        "composition_contract": composition_contract,
        "video_model_contract": video_model_contract,
        # Document explainers produce an avatar plate, not a storyboard
        # video. Generate the plate serially so each segment receives the
        # previous tail frame as an additional continuity anchor.
        "chain_required": bool(
            plan.get("chain_required") or
            (design_board and has_human and len(shots) > 1)),
        "design_board_path": shot_image_path if design_board else None,
        "design_board_url": shot_image_url if design_board else None,
        "storyboard_panel_mode": "shot_representative_keyframe",
        # panel_index identifies the current shot inside the approved
        # contact sheet. Seedance uses it natively; Kling fallback asks
        # gpt-image-2 for a clean img2img keyframe rather than a pixel crop.
        "storyboard_panel_index": int(shot.get("panel_index") or (i + 1)),
        "ref_tags": ref_tags,
        "reference_bindings": reference_bindings,
        "risk_profile": panel_expansion.risk_profile({
            "id": sid, "ref_tags": ref_tags, "visual": shot.get("visual"),
            "action": shot.get("action"), "character_action": shot.get("character_action"),
            "shot_size": shot.get("shot_size"), "prop_prompts": shot.get("prop_prompts"),
        }),
        "storyboard_dir": storyboard_dir if not design_board else None,
        "storyboard_plan_fingerprint": plan_fingerprint,
        "storyboard_result_fingerprint": storyboard_fingerprint,
        "storyboard_shot_fingerprint": storyboard.shot_fingerprint(shot),
        "out_path": os.path.join("output", client, run_id, "seg_%s.mp4" % sid),
        "model": "kling-v3-omni-video" if oral_broadcast else None,
        "seedance_native": not design_board,
        "extend_from_previous": bool(oral_broadcast and
                                      (shot.get("extend_from_previous") or shot.get("extend"))),
        "clothing_lock": clothing_lock,
        "character_identity_lock": bool(has_human),
        # 动效设计规范（motion_design.py 预规划）：安全区 + 字幕/动效 spec
        "video_safe_zones": shot.get("video_safe_zones") or [],
        "motion_design": shot.get("motion_design"),
        "motion_plan": shot.get("motion_plan") or (
            shot.get("motion_design", {}).get("motion_plan")
            if isinstance(shot.get("motion_design"), dict) else None),
        "asset_slots": list(shot.get("asset_slots") or (
            shot.get("motion_design", {}).get("asset_slots")
            if isinstance(shot.get("motion_design"), dict) else []) or []),
        # Keep authored graphics out of the video prompt, but carry them
        # forward for the post-production HyperFrames stage.
        "motion_elements": list(shot.get("motion_elements") or [])
            if not isinstance(shot.get("motion_elements"), str)
            else [shot["motion_elements"]],
        "timeline": (shot.get("timeline") or [{
            "start": 0,
            "end": secs,
            "action": shot.get("visual") or shot.get("scene_prompt") or "主体自然运动",
            "camera": shot.get("camera_movement") or shot.get("camera"),
            "sound": (shot.get("audio") or {}).get("sfx"),
            "shot_size": shot.get("shot_size"),
            "angle_offset": shot.get("angle_offset"),
            "composition": shot.get("composition") or shot.get("layout"),
            "lighting": shot.get("lighting"),
            "character_action": shot.get("character_action") or shot.get("action"),
            "micro_expression": shot.get("micro_expression"),
            "scene_prompt": shot.get("scene_prompt") or shot.get("scene"),
            "prop_prompts": shot.get("prop_prompts") or shot.get("asset_prompts"),
        }]),
        "style": style,
        "reference_roles": _legacy_reference_roles(references),
        "clip_contract": contract,
        "take_review_required": bool(
            scene_aware or shot.get("clip_contract") or
            any(shot.get(field) for field in
                ("narrative_function", "felt_intent", "director_voice", "arc_position"))),
        "sequence_state": shot.get("sequence_state") or {
            "version": 1, "sequence_id": shot.get("sequence_id") or shot.get("scene_id") or "default",
            "scene_id": shot.get("scene_id"), "entry": shot.get("planned_start_state") or {},
            "exit": shot.get("planned_end_state") or {}, "reset": False},
        "continuity_in": shot.get("continuity_in"),
        "continuity_out": shot.get("continuity_out"),
        "extend_video": bool(oral_broadcast and
                              (shot.get("extend_video") or shot.get("extend_from_previous") or
                               shot.get("extend"))),
        "oral_broadcast": oral_broadcast,
        "chain_contract": ({"predecessor_segment_id": str(shots[i - 1].get("id"))}
                           if i > 0 else {}),
    }
    return segment


def _enrich_compiled_segment(context, shot, segment, resolved):
    motion_design_bundle = context["motion_design_bundle"]
    plan = context["plan"]
    design_board = context["design_board"]
    segments = context["segments"]
    sid = resolved["sid"]
    if not segment.get("motion_design"):
        shot_design = None
        if isinstance(motion_design_bundle, dict):
            shot_design = next(
                (item for item in (motion_design_bundle.get("shots") or [])
                 if isinstance(item, dict) and str(item.get("shot_id")) == sid),
                None,
            )
        if shot_design:
            segment["motion_design"] = shot_design
            segment["motion_plan"] = shot_design.get("motion_plan") or segment["motion_plan"] or {}
            segment["asset_slots"] = shot_design.get("asset_slots") or segment["asset_slots"]
            segment["motion_elements"] = shot_design.get("motion_elements") or segment["motion_elements"]
            segment["video_safe_zones"] = shot_design.get("video_safe_zones") or segment.get("video_safe_zones") or []
        else:
            try:
                import motion_design
                motion_plan = motion_design.ensure_motion_plan_contract(
                    dict(shot, storyboard_role="design_board" if design_board else shot.get("storyboard_role")),
                    {"storyboard_role": "design_board" if design_board else shot.get("storyboard_role"),
                     "source_ppt": plan.get("source_ppt") or plan.get("source_doc"),
                     "scene_type": plan.get("scene_type") or plan.get("type")})
                segment["motion_plan"] = motion_plan
                segment["asset_slots"] = motion_plan.get("asset_slots") or segment["asset_slots"]
                segment["motion_elements"] = motion_plan.get("motion_elements") or segment["motion_elements"]
            except (OSError, ValueError, TypeError, KeyError):
                pass
    # Rebuild contract beats from the normalized segment timeline.
    segment["clip_contract"]["scopes"]["beats"] = segment["timeline"]
    segments.append(segment)


def _compile_split_segments(
        *, shots, stale_storyboard_shots, shot_map, storyboard_dir,
        design_board, plan_refs, bw_storyboard, allow_text2video,
        asset_prep, client, allow_unconfirmed,
        draft_allow_unapproved_storyboard, plan, ratio, resolution,
        run_id, approval_identity, render_plan_identity,
        storyboard_fingerprint, plan_fingerprint, oral_broadcast,
        scene_aware, motion_design_bundle, storyboard, min_seconds,
        identity_plate_artifact, formal_avatar_plate, strict_dialogue_fit,
        approved_audio_direction):
    """Compile panel-bound shots into provider-ready segment contracts."""
    context = dict(locals())
    context.update({
        "segments": [], "missing": [], "needs_image": [],
        "unconfirmed_refs": [], "dropped_references": [],
    })
    total_seconds = 0
    for index, shot in enumerate(shots):
        resolved = _compile_segment_references(context, shot, index)
        if resolved is None:
            continue
        subject = _compile_segment_subject(context, shot, resolved)
        prompt = _compile_segment_prompt(context, shot, resolved, subject)
        contracts = _compile_segment_contracts(context, shot, resolved, subject)
        segment = _assemble_compiled_segment(
            context, shot, index, resolved, subject, prompt, contracts)
        _enrich_compiled_segment(context, shot, segment, resolved)
        total_seconds += subject["secs"]
    return (
        context["segments"], total_seconds, context["missing"],
        context["needs_image"], context["unconfirmed_refs"],
        context["dropped_references"])



def _finalize_split_segments(
        segments, shots, *, long_video, oral_broadcast, scene_aware,
        storyboard_dir, draft_allow_unapproved_storyboard):
    """Apply cross-segment continuity and freeze each video handoff."""
    if long_video and oral_broadcast:
        for index, segment in enumerate(segments[1:], 1):
            previous = segments[index - 1]
            same_scene = (
                not scene_aware or
                segment.get("scene_id") == previous.get("scene_id"))
            if same_scene:
                segment["extend_video"] = True
                segment["extend_from_previous"] = True
                segment["continuity_in"] = "上一段视频的最后画面和动作状态"
                segment["continuity_out"] = (
                    "保持当前人物、产品、场景、光线和运动方向，交给下一段延长")
                segment["clip_contract"]["scopes"]["clip"][
                    "continuation_mode"] = "extend_previous"
            else:
                segment["extend_video"] = False
                segment["extend_from_previous"] = False
                segment["continuity_in"] = None
                segment["clip_contract"]["scopes"]["clip"][
                    "continuation_mode"] = "fresh_scene"

    continuity_graph = storyboard_enhancements.build_continuity_graph(shots)
    # Existing authored plans may omit explicit coverage metadata. Preserve the
    # historical permissive split behavior but surface this as a report; plans
    # that declare contradictory screen direction remain blocked upstream.
    segments = storyboard_enhancements.inject_continuity(
        segments, dict(continuity_graph, ok=True, errors=[]))
    segments = _inject_segment_continuity_fallbacks(segments)
    segments = _inject_audio_continuity_fallbacks(segments)
    for segment in segments:
        validate_storyboard_binding(
            segment,
            strict=bool(
                storyboard_dir and segment.get("storyboard_path") and
                not draft_allow_unapproved_storyboard))
        handoff = build_video_handoff(segment)
        segment["video_handoff_fingerprint"] = handoff["fingerprint"]
    return segments, continuity_graph


def _bind_formal_avatar_plate_plan(plan, render_plan, identity_plate_artifact):
    """Compile the approved render-plan dialogue onto a clean execution copy.

    The authored/storyboard plan remains immutable.  Formal document explainers
    may use only the independently approved identity plate as the video-model
    image anchor; the cast contact sheet and design-board/PPT surfaces stay in
    deterministic Remotion/HyperFrames composition.
    """
    if (render_plan.get("kind") != "document_explainer_render_plan" or
            render_plan.get("client") != plan.get("client") or
            str(render_plan.get("run_id")) != str(plan.get("run_id"))):
        raise ValueError("FORMAL_AVATAR_RENDER_PLAN_IDENTITY_MISMATCH")
    render_segments = list(render_plan.get("segments") or [])
    authored_shots = list(plan.get("shots") or [])
    render_ids = [str(item.get("id") or "") for item in render_segments]
    authored_ids = [str(item.get("id") or "") for item in authored_shots]
    if (not render_ids or any(not item for item in render_ids) or
            render_ids != authored_ids or len(render_ids) != len(set(render_ids))):
        raise ValueError("RENDER_PLAN_DIALOGUE_SEGMENT_ORDER_BINDING_REQUIRED")

    timing_shots = [{
        "id": item.get("id"), "dialogue": item.get("dialogue"),
        "duration": item.get("duration_seconds"),
        "audio_contract": item.get("audio_contract") or {},
        "voice_type": item.get("voice_type"),
    } for item in render_segments]
    timing = dialogue_timing.validate_contract(
        render_plan.get("dialogue_timing_contract"), timing_shots,
        plan_voice_type=render_plan.get("dialogue_timing_voice_type"))
    dialogue_timing.assert_fits(
        timing, error_code="FORMAL_AVATAR_DIALOGUE_DURATION_OVERFLOW")
    timing_by_id = {item["shot_id"]: item for item in timing["items"]}

    identity = identity_plate_artifact or {}
    local_identity = identity.get("output") or identity
    identity_path = os.path.abspath(str(local_identity.get("path") or ""))
    current_identity = rm.file_record(identity_path)
    if (not current_identity or not current_identity.get("exists") or
            current_identity.get("sha256") != local_identity.get("sha256")):
        raise ValueError("IDENTITY_PLATE_ARTIFACT_REQUIRED")

    remote_url = str(identity.get("remote_url") or "").strip()
    parsed_remote = urllib.parse.urlsplit(remote_url)
    if (parsed_remote.scheme.lower() != "https" or
            not parsed_remote.hostname or parsed_remote.username or
            parsed_remote.password):
        raise ValueError("IDENTITY_PLATE_HTTPS_RETRIEVE_URL_REQUIRED")
    result_record = identity.get("generation_result") or {}
    current_result_record = rm.file_record(result_record.get("path"))
    if (not current_result_record or not current_result_record.get("exists") or
            current_result_record.get("sha256") != result_record.get("sha256")):
        raise ValueError("IDENTITY_PLATE_GENERATION_RESULT_REQUIRED")
    try:
        with open(current_result_record["path"], encoding="utf-8") as handle:
            generation_result = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("IDENTITY_PLATE_GENERATION_RESULT_INVALID") from exc
    if (generation_result.get("kind") != "identity_plate_generation_result" or
            generation_result.get("status") != "pending_visual_review" or
            generation_result.get("client") != plan.get("client") or
            str(generation_result.get("run_id")) != str(plan.get("run_id")) or
            generation_result.get("candidate") != current_identity or
            generation_result.get("remote_url") != remote_url):
        raise ValueError("IDENTITY_PLATE_GENERATION_RESULT_MISMATCH")

    formal_identity = {
        "kind": "approved_identity_plate_remote_reference",
        "approved_local_artifact": current_identity,
        "remote_url": remote_url,
        "generation_result": current_result_record,
    }

    by_id = {str(item["id"]): item for item in render_segments}
    for shot in authored_shots:
        shot_id = str(shot["id"])
        compiled = by_id[shot_id]
        dialogue = str(compiled.get("dialogue") or "").strip()
        prompt = str(compiled.get("submission_prompt_zh") or "").strip()
        if not dialogue or not prompt:
            raise ValueError("RENDER_PLAN_AVATAR_PROMPT_REQUIRED: %s" % shot_id)
        authored_dialogue = str(
            shot.get("dialogue") or shot.get("voiceover") or "").strip()
        if authored_dialogue and authored_dialogue != dialogue:
            raise ValueError("RENDER_PLAN_DIALOGUE_MISMATCH: %s" % shot_id)
        compiled_duration = float(compiled.get("duration_seconds") or 0)
        authored_duration = float(
            shot.get("duration") or shot.get("seconds") or 0)
        if compiled_duration <= 0 or abs(compiled_duration - authored_duration) > 0.001:
            raise ValueError("RENDER_PLAN_DURATION_MISMATCH: %s" % shot_id)
        if compiled.get("dialogue_timing") != timing_by_id[shot_id]:
            raise ValueError(
                "RENDER_PLAN_DIALOGUE_TIMING_MISMATCH: %s" % shot_id)
        shot["dialogue"] = dialogue
        shot["duration"] = compiled_duration
        shot["visual_mode"] = compiled.get("visual_mode")
        shot["approved_avatar_plate_prompt"] = prompt
        shot["dialogue_timing"] = timing_by_id[shot_id]
        shot["characters"] = shot.get("characters") or ["presenter"]
        shot["asset_refs"] = {}
        shot["ref_tags"] = ["@identity_plate"]
        shot["video_ref_tags"] = ["@identity_plate"]
        audio = dict(shot.get("audio") or {})
        audio.update({
            "language": ((compiled.get("audio_contract") or {}).get(
                "language") or "普通话"),
            "voice": ((render_plan.get("voice_continuity_contract") or {}).get(
                "voice_profile") or "已确认同一讲师声线"),
            "lip_sync": True,
        })
        shot["audio"] = audio

    plan["asset_refs"] = {
        "digital_human_portraits": [remote_url],
        "background_images": [],
        "reference_registry": [{
            "tag": "@identity_plate", "type": "character_identity",
            "url": remote_url, "source": "identity_plate.approved_candidate",
            "label": "客户确认的单人身份与干净背景锚图",
            "background_anchor": True,
            "approved_local_artifact": current_identity,
            "generation_result": current_result_record,
        }],
    }
    plan["background_reference_required"] = True
    plan["dialogue_timing_contract"] = timing
    plan["formal_avatar_plate_contract"] = {
        "kind": "approved_identity_plate_only",
        "identity_plate": formal_identity,
        "forbidden_video_inputs": [
            "cast_contact_sheet", "design_board", "ppt_page",
            "subtitle", "graphic_card", "logo_overlay"],
    }
    return plan, formal_identity



def _prepare_split_plan(context):
    plan = context["plan"]
    client = context["client"]
    allow_unconfirmed = context["allow_unconfirmed"]
    min_seconds = context["min_seconds"]
    render_plan_artifact = context["render_plan_artifact"]
    identity_plate_artifact = context["identity_plate_artifact"]
    ratio_override = context["ratio_override"]
    bw_storyboard = context["bw_storyboard"]
    if not client:
        raise ValueError("CLIENT_REQUIRED: split 正式流程必须显式指定 client")
    import storyboard
    plan = json.loads(json.dumps(plan, ensure_ascii=False))
    if plan.get("client") and str(plan["client"]) != str(client):
        raise ValueError("CLIENT_MISMATCH: plan.client 与 split client 不一致")
    plan["client"] = client
    draft_product_sources = False
    try:
        plan = storyboard.canonical_storyboard_plan(plan)
    except br_client.BRError as exc:
        if not (allow_unconfirmed and
                "UNCONFIRMED_PRODUCT_IMAGE" in str(exc)):
            raise ValueError(str(exc)) from exc
        plan = storyboard.canonical_storyboard_plan(
            plan, allow_unconfirmed_product_images=True)
        draft_product_sources = True
    plan = storyboard.expand_product_sku_refs(plan)
    # Resolve target model's duration limit early — used by oral broadcast
    # validation and shot partitioning below.
    target_model = plan.get("model") or plan.get("video_model")
    max_secs = max_seconds_for_model(target_model)
    scene_type = _normalize_scene_type(plan)
    oral_broadcast = scene_type in _ORAL_SCENE_TYPES
    design_board = storyboard_is_design_board(plan)
    if oral_broadcast:
        authored_shots = plan.get("shots") or []
        if len(authored_shots) < 2:
            raise ValueError("ORAL_BROADCAST_SHOTS_REQUIRED: 口播延长链至少需要两个独立镜头")
        total_authored = sum(_shot_duration_seconds(shot, min_seconds)
                             for shot in authored_shots)
        if total_authored > max_secs * len(authored_shots):
            raise ValueError("ORAL_BROADCAST_DURATION_INVALID: 每个口播镜头必须不超过%ds（当前模型上限）"
                             % max_secs)
    # Video extension is an explicit business policy, not a generic same-scene
    # continuity shortcut. Only the single-speaker oral-broadcast workflow may
    # submit a provider extension request; every other workflow must use fresh
    # segment generation and local assembly.
    render_plan_artifact = render_plan_artifact or plan.get("render_plan_artifact")
    if render_plan_artifact:
        path = render_plan_artifact.get("path")
        if not path or not os.path.isfile(path):
            raise ValueError("RENDER_PLAN_ARTIFACT_REQUIRED")
        with open(path, "rb") as handle:
            actual_sha = hashlib.sha256(handle.read()).hexdigest()
        if actual_sha != render_plan_artifact.get("sha256"):
            raise ValueError("STALE_RENDER_PLAN_ARTIFACT")
        with open(path, encoding="utf-8") as handle:
            render_plan = json.load(handle)
        render_plan_identity = {"path": os.path.abspath(path), "sha256": actual_sha,
                                "content": render_plan}
    else:
        render_plan = plan.get("render_plan") or {}
        render_plan_identity = {"path": None, "sha256": _sha256_json(render_plan),
                                "content": render_plan}

    formal_identity_plate = None
    formal_avatar_plate = bool(
        design_board and
        (render_plan.get("identity_plate_generation") or {}).get(
            "required_before_video") is True)
    # Keep the customer-approved visual plan separate from the provider-facing
    # execution copy.  Binding the approved identity plate intentionally
    # rewrites ref_tags/asset_refs so only the clean avatar plate reaches the
    # video model; those safety rewrites must not invalidate the deterministic
    # design-board approval they are derived from.
    storyboard_approval_plan = json.loads(json.dumps(plan, ensure_ascii=False))
    if formal_avatar_plate:
        plan, formal_identity_plate = _bind_formal_avatar_plate_plan(
            plan, render_plan, identity_plate_artifact)

    # Storyboard contact sheets are commonly 16:9 even when the final video is
    # vertical. Once a render plan has been confirmed, its ratio is the formal
    # handoff contract and takes precedence over stale draft aliases.
    render_ratio = render_plan.get("ratio") if isinstance(render_plan, dict) else None
    aspect = ratio_override or (render_ratio if render_ratio in _RATIO_MAP else None) or output_ratio(plan)
    ratio, resolution = _RATIO_MAP.get(aspect, ("9:16", "1080p"))
    plan_refs = _normalized_plan_refs(plan)
    # max_secs already resolved above (line ~500) from the target model.
    source_shots = plan.get("shots") or []
    scene_aware = any(str(shot.get("scene_id") or "").strip() for shot in source_shots)

    # 场景一致性校验：同 scene_id 的 shots 应有相近的 scene_prompt
    if scene_aware:
        _check_scene_consistency(source_shots)

    # Storyboard-to-video is panel-bound: every approved storyboard shot must
    # become its own video task so the model receives the exact panel image and
    # ref_tags for that shot. Generic duration packing can split words (e.g.
    # "TWS" -> "TW/S") and merges unrelated visual contracts. Only the explicit
    # oral-broadcast extension workflow keeps its special chained behavior.
    shots = partition_shots(source_shots, max_seconds=max_secs,
                            scene_aware="auto", preserve_shots=True)
    if not shots:
        raise ValueError("storyboard_plan 无 shots，无法拆分")

    # 跨场景过渡指令：scene_id 变化时在第一个 shot 注入过渡提示
    if scene_aware:
        _inject_scene_transitions(shots)

    if bw_storyboard is None:
        cm = str(plan.get("color_mode") or "bw").lower()
        bw_storyboard = cm in ("bw", "black_white", "grayscale", "mono", "黑白")

    # All generated clips after the first continue from the previous clip.
    # The renderer uses this marker to submit Seedance's video-extension call.
    total_plan_seconds = sum(
        float(shot.get("duration") or shot.get("seconds") or min_seconds)
        for shot in (plan.get("shots") or [])
    )
    long_video = total_plan_seconds > max_secs
    return {
        "storyboard": storyboard, "plan": plan,
        "draft_product_sources": draft_product_sources,
        "max_secs": max_secs, "oral_broadcast": oral_broadcast,
        "design_board": design_board,
        "formal_avatar_plate": formal_avatar_plate,
        "storyboard_approval_plan": storyboard_approval_plan,
        "identity_plate_artifact": formal_identity_plate,
        "strict_dialogue_fit": bool(render_plan_artifact),
        "render_plan_identity": render_plan_identity,
        "ratio": ratio, "resolution": resolution, "plan_refs": plan_refs,
        "source_shots": source_shots, "scene_aware": scene_aware,
        "shots": shots, "bw_storyboard": bw_storyboard,
        "long_video": long_video,
    }


def _bind_split_storyboard(context, prepared):
    storyboard_dir = context["storyboard_dir"]
    run_id = context["run_id"]
    client = context["client"]
    draft_allow_unapproved_storyboard = context["draft_allow_unapproved_storyboard"]
    plan = prepared["plan"]
    approval_plan = prepared.get("storyboard_approval_plan") or plan
    storyboard = prepared["storyboard"]
    draft_product_sources = prepared["draft_product_sources"]
    source_shots = prepared["source_shots"]
    approval_source_shots = approval_plan.get("shots") or []
    plan_refs = prepared["plan_refs"]
    import asset_prep as asset_prep

    storyboard_dir = _resolve_current_storyboard_dir(
        storyboard_dir, run_id, approval_plan, storyboard,
        allow_unconfirmed_product_images=draft_product_sources) if storyboard_dir else None
    plan_fingerprint = storyboard.plan_fingerprint(
        approval_plan, allow_unconfirmed_product_images=draft_product_sources)
    result_path, storyboard_result, shot_map = None, None, {}
    storyboard_approval_record = None
    storyboard_approval_path = None
    stale_storyboard_shots = set()
    if storyboard_dir:
        if not os.path.isdir(storyboard_dir):
            raise ValueError("STORYBOARD_DIR_NOT_FOUND: %s" % storyboard_dir)
        result_path, storyboard_result = _load_storyboard_result(storyboard_dir)
        if not storyboard_result and not draft_allow_unapproved_storyboard:
            raise ValueError("STORYBOARD_APPROVAL_REQUIRED: storyboard_result.json 缺失或无效")
        if storyboard_result:
            if storyboard_result.get("client") != client:
                raise ValueError("CLIENT_MISMATCH: storyboard 不属于当前 client")
            result_run_id = storyboard_result.get("run_id")
            if run_id is not None and str(run_id) != str(result_run_id):
                raise ValueError("RUN_ID_MISMATCH: storyboard 不属于当前 run")
            run_id = result_run_id
            plan_changed = storyboard_result.get("plan_fingerprint") != plan_fingerprint
            stale_storyboard_shots = _stale_storyboard_shot_ids(
                storyboard_result, approval_source_shots,
                storyboard) if plan_changed else set()
            if (plan_changed and approval_source_shots and
                    len(stale_storyboard_shots) == len(approval_source_shots)):
                raise ValueError("STALE_STORYBOARD: storyboard plan fingerprint 已过期")
            approval_current = storyboard.storyboard_approval_is_current(
                result_path, client=client, run_id=run_id, out_dir=storyboard_dir,
                plan_fingerprint_value=plan_fingerprint)
            # A dialogue-only revision can keep existing panel-scoped approvals
            # valid, but only when an approval marker exists and still matches
            # the current image bytes. Never treat "no stale visual shots" as an
            # approval substitute.
            if not draft_allow_unapproved_storyboard and not approval_current:
                raise ValueError("STORYBOARD_APPROVAL_REQUIRED: 故事板未确认或确认已失效")
            if approval_current:
                storyboard_approval_path = os.path.join(
                    storyboard_dir, ".storyboard_confirmed.json")
                try:
                    with open(storyboard_approval_path, encoding="utf-8") as handle:
                        storyboard_approval_record = json.load(handle)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("STORYBOARD_APPROVAL_REQUIRED: 确认 marker 无效") from exc
            shot_map = _storyboard_shot_map(storyboard_result)
            generated_refs = dict(plan_refs)
            confirmed_stage_paths = {}
            # Replace raw source references with the confirmed generated board
            # for each stage. Keeping both duplicates the same semantic anchor
            # and can evict the usage/storyboard refs.
            for result_key, ref_key, approval_kind in (
                    ("product_usage_image", "product_usage_images", "usage"),
                    ("cast_board", "cast_boards", "cast"),
                    ("product_board", "product_boards", "product")):
                item = storyboard_result.get(result_key) or {}
                path = item.get("abspath") or item.get("path")
                video_ref = _result_item_video_ref(
                    item, storyboard_dir=storyboard_dir, result_key=result_key)
                source_fp = item.get("source_fingerprint")
                if (path and _confirmed_generated_board_current(
                        storyboard_dir, approval_kind, item, source_fp, storyboard)):
                    if ref_key == "product_usage_images":
                        generated_refs.pop("usage_reference_images", None)
                    if ref_key == "cast_boards":
                        # Keep any separately confirmed single-person portrait;
                        # the cast contact sheet remains in storyboard_result as
                        # approval evidence and must not enter video imageUrls.
                        generated_refs.pop("cast_boards", None)
                        continue
                    if ref_key == "product_boards":
                        generated_refs.pop("product_images", None)
                    generated_refs[ref_key] = [video_ref or os.path.abspath(path)]
                    confirmed_stage_paths[ref_key] = video_ref or os.path.abspath(path)
            if storyboard_result.get("reference_registry"):
                generated_refs["reference_registry"] = (
                    _remap_reference_registry_to_confirmed_boards(
                        storyboard_result["reference_registry"], confirmed_stage_paths))
            plan_refs = _normalized_plan_refs(dict(plan, asset_refs=generated_refs))
            if prepared.get("formal_avatar_plate"):
                # A design board is customer evidence for local composition,
                # never an identity source for the video model.
                plan_refs = prepared["plan_refs"]
    run_id = str(run_id or plan.get("run_id") or plan_fingerprint)
    approval_identity = {
        "client": client, "run_id": run_id,
        "plan_fingerprint": plan_fingerprint,
        "result_json": os.path.abspath(result_path) if result_path else None,
        "out_dir": storyboard_dir,
        "status": ("draft" if draft_allow_unapproved_storyboard else
                   "confirmed" if storyboard_result else "not_applicable"),
    }
    if storyboard_approval_record and not draft_allow_unapproved_storyboard:
        for field in (
                "kind", "confirmed_by_customer", "customer_confirmation",
                "confirmation_source", "confirmed_at", "approval_scope",
                "storyboard_usage"):
            approval_identity[field] = storyboard_approval_record.get(field)
        approval_identity["approval_path"] = os.path.abspath(storyboard_approval_path)
        approval_identity["approval_sha256"] = file_sha256(storyboard_approval_path)
    storyboard_fingerprint = (storyboard_result or {}).get("plan_fingerprint")
    motion_design_bundle = None
    motion_design_path = None
    if storyboard_dir:
        motion_design_path = (storyboard_result or {}).get("motion_design_path")
        if not motion_design_path:
            motion_design_path = os.path.join(storyboard_dir, "motion_design.json")
        if motion_design_path and os.path.isfile(motion_design_path):
            try:
                with open(motion_design_path, encoding="utf-8") as handle:
                    motion_design_bundle = json.load(handle)
            except (OSError, ValueError, TypeError):
                motion_design_bundle = None
    return {
        "asset_prep": asset_prep, "storyboard_dir": storyboard_dir,
        "run_id": run_id, "plan_fingerprint": plan_fingerprint,
        "shot_map": shot_map,
        "stale_storyboard_shots": stale_storyboard_shots,
        "plan_refs": plan_refs, "approval_identity": approval_identity,
        "storyboard_fingerprint": storyboard_fingerprint,
        "motion_design_bundle": motion_design_bundle,
    }


def split(plan, storyboard_dir=None, fps=30, min_seconds=3, bw_storyboard=None,
          allow_text2video=False, client=None, allow_unconfirmed=False,
          ratio_override=None, draft_allow_unapproved_storyboard=False,
          run_id=None, render_plan_artifact=None,
          identity_plate_artifact=None, approved_audio_direction=None):
    """把定稿 storyboard_plan.json 按各镜时长拆成 render_batch segments。

    每段 segment：
      {id, text(台词+表演指导), duration(秒), video_type, urls(分镜图+锚定素材),
       ratio, resolution, out_path, storyboard_ref:True}
    返回 {segments:[...], total_seconds, missing_images:[shot_id...],
         needs_image:[shot_id...], unconfirmed_refs:[{shot_id,url}...],
         ratio, resolution}。

    bw_storyboard：故事板是否黑白（决定锚定优先级）。None=自动读 plan.color_mode
      （默认黑白）；黑白时彩色素材优先锚定颜色，黑白分镜图垫后作构图提示。

    allow_text2video（铁律#11，与 guide_scaffold.compile_segments 的政策保持一致）：
      默认 False——若某镜锚定素材为 0（既无分镜图也无 asset_refs），该镜**不生成
      segment**，其 id 记入返回值 needs_image，交由调用方回填素材后重新 split，
      避免"缺图→静默退化为纯文本生视频"（数字人/产品一致性会失控）。
      仅当明确是纯数字人口播、无需产品图/场景图时，才应传 True 显式放行，
      此时缺图镜头会正常走 video_type=1（文生视频）。

    client + allow_unconfirmed（confirmed/pending 状态机跨模块执行，asset_prep.py
      is_confirmed()）：asset_prep.gen_image() 产出的候选图默认 status='pending'，
      只有客户 confirm_image() 之后才是 status='confirmed'。这个状态机原本只是
      brief.json 里的约定，没有任何代码路径校验——本函数若传入 client，会对每段
      urls 里来自 asset_prep 的本地路径素材逐一跑 is_confirmed() 检查；命中未确认
      (pending) 素材时，默认（allow_unconfirmed=False）直接抛 ValueError 列出问题
      shot/url，交由调用方先让客户走确认闸门，不静默把未过客户确认的候选图当成
      最终锚定素材出片。client=None（未传）时跳过这项检查（无法判断 brief 归属）。
      仅当明确要用未确认素材做快速预览/草稿时，才应传 allow_unconfirmed=True。
    """
    context = dict(locals())
    prepared = _prepare_split_plan(context)
    binding = _bind_split_storyboard(context, prepared)
    plan = prepared["plan"]
    storyboard = prepared["storyboard"]
    oral_broadcast = prepared["oral_broadcast"]
    design_board = prepared["design_board"]
    identity_plate_artifact = prepared["identity_plate_artifact"]
    strict_dialogue_fit = prepared["strict_dialogue_fit"]
    render_plan_identity = prepared["render_plan_identity"]
    ratio = prepared["ratio"]
    resolution = prepared["resolution"]
    source_shots = prepared["source_shots"]
    scene_aware = prepared["scene_aware"]
    shots = prepared["shots"]
    bw_storyboard = prepared["bw_storyboard"]
    long_video = prepared["long_video"]
    asset_prep = binding["asset_prep"]
    storyboard_dir = binding["storyboard_dir"]
    run_id = binding["run_id"]
    plan_fingerprint = binding["plan_fingerprint"]
    shot_map = binding["shot_map"]
    stale_storyboard_shots = binding["stale_storyboard_shots"]
    plan_refs = binding["plan_refs"]
    approval_identity = binding["approval_identity"]
    storyboard_fingerprint = binding["storyboard_fingerprint"]
    motion_design_bundle = binding["motion_design_bundle"]
    (segments, total_seconds, missing, needs_image,
     unconfirmed_refs, dropped_references) = _compile_split_segments(
        shots=shots,
        stale_storyboard_shots=stale_storyboard_shots,
        shot_map=shot_map,
        storyboard_dir=storyboard_dir,
        design_board=design_board,
        plan_refs=plan_refs,
        bw_storyboard=bw_storyboard,
        allow_text2video=allow_text2video,
        asset_prep=asset_prep,
        client=client,
        allow_unconfirmed=allow_unconfirmed,
        draft_allow_unapproved_storyboard=draft_allow_unapproved_storyboard,
        plan=plan,
        ratio=ratio,
        resolution=resolution,
        run_id=run_id,
        approval_identity=approval_identity,
        render_plan_identity=render_plan_identity,
        storyboard_fingerprint=storyboard_fingerprint,
        plan_fingerprint=plan_fingerprint,
        oral_broadcast=oral_broadcast,
        scene_aware=scene_aware,
        motion_design_bundle=motion_design_bundle,
        storyboard=storyboard,
        min_seconds=min_seconds,
        identity_plate_artifact=identity_plate_artifact,
        formal_avatar_plate=prepared["formal_avatar_plate"],
        strict_dialogue_fit=strict_dialogue_fit,
        approved_audio_direction=approved_audio_direction,
    )

    segments, continuity_graph = _finalize_split_segments(
        segments,
        shots,
        long_video=long_video,
        oral_broadcast=oral_broadcast,
        scene_aware=scene_aware,
        storyboard_dir=storyboard_dir,
        draft_allow_unapproved_storyboard=draft_allow_unapproved_storyboard,
    )

    return {
        "client": client,
        "run_id": run_id,
        "storyboard_approval": approval_identity,
        "segments": segments,
        "continuity_graph": continuity_graph,
        "total_seconds": total_seconds,
        "total_frames": total_seconds * fps,
        "missing_images": missing,
        "needs_image": needs_image,
        "unconfirmed_refs": unconfirmed_refs,
        "dropped_references": dropped_references,
        "scene_aware": scene_aware,
        "oral_broadcast": oral_broadcast,
        "generation_strategy": "extend" if oral_broadcast else "segmented",
        "storyboard_usage": "design_board" if design_board else "video_storyboard",
        "scene_count": len({s.get("scene_id") for s in segments if s.get("scene_id")}),
        "contract_version": 1,
        "schema_version": 2,
        "video_handoff_fingerprints": {
            segment["id"]: segment["video_handoff_fingerprint"] for segment in segments},
        "ratio": ratio,
        "resolution": resolution,
        "shot_count": len(shots),
        "storyboard_dir": storyboard_dir,
        "storyboard_plan_fingerprint": plan_fingerprint,
        "storyboard_result_fingerprint": storyboard_fingerprint,
        "render_plan": render_plan_identity,
        "render_plan_fingerprint": _sha256_json(render_plan_identity),
        "identity_plate_artifact": identity_plate_artifact,
        "approved_audio_direction": approved_audio_direction,
    }


def assemble(segments_spec, results, out_path, ff=None, allow_ocr_warning=False,
             legacy_unsafe=False):
    """多段底片 → ffmpeg 拼接为最终版底片（阶段4 尾）。

    segments_spec: split() 的输出（或含 segments 列表的 dict）。
    results: video_engine.render_batch 的结果列表（每项 {ok, localPath, ocr_warning, ...}），或已下载的
             本地 mp4 路径列表。按 segments 顺序拼接。
    单段直接复制/返回该段；多段调 compose.concat 拼。

    铁律#9：若某段 results[i].ocr_warning=True（OCR 检出画面文字疑似字幕残留）且
    allow_ocr_warning 未显式置 True，则拒绝拼接，抛 RuntimeError 列出问题段号，
    交由 agent/人工决定重新生成或显式放行——绝不静默把带字幕残留的段拼进成片。
    （若 results 是纯字符串路径列表，说明调用方未走 render_batch 的 OCR 检测，
    此时无法判断，直接放行。）

    返回 {ok, out, segment_count, used_paths}。
    """
    segs = segments_spec.get("segments") if isinstance(segments_spec, dict) else segments_spec
    if not isinstance(segs, list) or not all(isinstance(seg, dict) for seg in segs):
        raise TypeError("INVALID_SEGMENTS: 必须提供结构化 segments 列表")
    if isinstance(results, dict):
        results = results.get("results")
    if not legacy_unsafe and (not isinstance(results, list) or
                              not all(isinstance(item, dict) for item in results)):
        raise TypeError("STRUCTURED_RESULTS_REQUIRED: results 必须是结构化对象列表")
    # 解析每段的本地路径（result.localPath 优先；否则按 out_path）
    paths = []
    ocr_flagged = []
    strict_results = not legacy_unsafe or results is not None
    if strict_results and len(results) != len(segs):
        raise RuntimeError("合成结果数量与分段数量不一致：%d != %d" %
                           (len(results), len(segs)))
    for i, seg in enumerate(segs):
        p = None
        if results and i < len(results):
            r = results[i]
            if isinstance(r, dict):
                if not r.get("ok"):
                    raise RuntimeError("段 %s 出片失败，拒绝合成：%s" %
                                       (seg.get("id"), r.get("error") or "未知错误"))
                p = r.get("localPath") or r.get("out_path")
                if not legacy_unsafe and "segment_id" not in r:
                    raise RuntimeError("RESULT_SEGMENT_ID_REQUIRED: 段 %s" % seg.get("id"))
                if r.get("segment_id") != seg.get("id"):
                    raise RuntimeError("段结果与分段 ID 不一致：%s != %s" %
                                       (r.get("segment_id"), seg.get("id")))
                expected_handoff = seg.get("video_handoff_fingerprint")
                if not legacy_unsafe and not expected_handoff:
                    raise RuntimeError("SEGMENT_HANDOFF_REQUIRED: 段 %s" % seg.get("id"))
                if not legacy_unsafe and "video_handoff_fingerprint" not in r:
                    raise RuntimeError("RESULT_HANDOFF_REQUIRED: 段 %s" % seg.get("id"))
                if expected_handoff and r.get("video_handoff_fingerprint") != expected_handoff:
                    raise RuntimeError("STALE_VIDEO_HANDOFF: 段 %s 的成片不属于当前生成契约" % seg.get("id"))
                if not legacy_unsafe:
                    current_fp = take_review.take_fingerprint(r)
                    if not r.get("take_fingerprint") or current_fp != r.get("take_fingerprint"):
                        raise RuntimeError(
                            "STALE_TAKE_ARTIFACT: 段 %s 的当前文件与验片 take 不一致" % seg.get("id"))
                if (seg.get("take_review_required") and r.get("review_status") != "accepted"):
                    raise RuntimeError("TAKE_REVIEW_REQUIRED: 段 %s 尚未通过客户验片" % seg.get("id"))
                if not legacy_unsafe and "ocr_warning" not in r:
                    raise RuntimeError("RESULT_OCR_STATUS_REQUIRED: 段 %s" % seg.get("id"))
                if not legacy_unsafe and not isinstance(r.get("ocr_warning"), bool):
                    raise RuntimeError("INVALID_RESULT_OCR_STATUS: 段 %s" % seg.get("id"))
                if r.get("ocr_warning"):
                    if not legacy_unsafe and not isinstance(r.get("ocr_texts"), list):
                        raise RuntimeError("RESULT_OCR_TEXTS_REQUIRED: 段 %s" % seg.get("id"))
                    ocr_flagged.append({"index": i, "id": seg.get("id"),
                                        "texts": r.get("ocr_texts") or []})
            elif isinstance(r, str):
                if not legacy_unsafe:
                    raise TypeError("STRUCTURED_RESULTS_REQUIRED: 不接受字符串结果")
                p = r
        if not p and strict_results:
            raise RuntimeError("段 %s 没有本次运行的本地成片结果，拒绝回退到旧文件" %
                               seg.get("id"))
        if not p:
            p = seg.get("out_path")
        if not p or not os.path.exists(p):
            raise FileNotFoundError("段 %s 的底片缺失: %r" % (seg.get("id"), p))
        paths.append(os.path.abspath(p))

    if ocr_flagged and not allow_ocr_warning:
        raise RuntimeError(
            "[OCR_WARNING] %d 段检出疑似字幕残留，拒绝静默拼接（铁律#9）："
            "%s。请重新生成对应段，或确认可接受后传 allow_ocr_warning=True 显式放行。"
            % (len(ocr_flagged), ocr_flagged))

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    if len(paths) == 1:
        import shutil
        shutil.copyfile(paths[0], out_path)
        return {"ok": True, "out": out_path, "segment_count": 1, "used_paths": paths}

    import compose
    # Document explainers bind every segment to an approved EDL.  Crossfades
    # shorten the assembled presenter track and can move later dialogue out of
    # its frozen Remotion/HyperFrames scene, so preserve the exact timeline
    # with hard cuts for design-board work.  Narrative video storyboards keep
    # the existing crossfade behavior.
    exact_timeline = bool(
        isinstance(segments_spec, dict) and
        segments_spec.get("storyboard_usage") == "design_board")
    use_xfade = len(paths) > 2 and not exact_timeline
    timeline_durations = (
        [float(segment.get("duration") or 0) for segment in segs]
        if exact_timeline else None)
    compose.concat(
        paths, out_path, transition="xfade" if use_xfade else None,
        durations=timeline_durations)
    return {"ok": True, "out": out_path, "segment_count": len(paths), "used_paths": paths}


def persist_and_gate_assemble_ocr(manifest, manifest_path, segments_spec, results, *, client):
    """Persist result OCR evidence and allow only clean or exact waived takes."""
    rm.identity_gate(manifest, client=client)
    segs = segments_spec.get("segments") if isinstance(segments_spec, dict) else segments_spec
    raw_results = results.get("results") if isinstance(results, dict) else results
    recorded_handoff = (manifest.get("handoffs") or {}).get("video") or {}
    expected = {seg.get("id"): seg.get("video_handoff_fingerprint") for seg in segs or []}
    if recorded_handoff.get("segments") != expected:
        raise ValueError("VIDEO_HANDOFF_MISMATCH")
    if not isinstance(raw_results, list) or len(raw_results) != len(segs or []):
        raise ValueError("STRUCTURED_RESULTS_REQUIRED")
    blocked = []
    for seg, result in zip(segs, raw_results):
        sid = str(seg.get("id"))
        take_fp = result.get("take_fingerprint")
        if result.get("segment_id") != seg.get("id") or not take_fp:
            raise ValueError("ASSEMBLE_TAKE_IDENTITY_REQUIRED: %s" % sid)
        existing = (manifest.get("ocr_checks") or {}).get(sid) or {}
        if result.get("ocr_warning"):
            rm.record_ocr_result(manifest, sid, take_fp, "detected",
                                 result.get("ocr_texts") or [], available=True,
                                 frames_checked=result.get("ocr_frames_checked", 0),
                                 expected=result.get("ocr_expected", 0),
                                 error=result.get("ocr_error"))
        elif (result.get("ocr_status") == "clear" and result.get("ocr_available") and
              int(result.get("ocr_expected") or 0) > 0 and
              int(result.get("ocr_frames_checked") or 0) == int(result.get("ocr_expected") or 0)):
            rm.record_ocr_result(manifest, sid, take_fp, "clear", [], available=True,
                                 frames_checked=result["ocr_frames_checked"],
                                 expected=result["ocr_expected"])
        elif not (existing.get("take_fingerprint") == take_fp and
                  rm.ocr_take_is_clear_or_waived(manifest, sid, take_fp)):
            # Legacy results only expose warning/no-warning and cannot prove
            # OCR availability or frame coverage. Persist fail-closed evidence
            # without overwriting an exact manual/full-coverage clear record.
            rm.record_ocr_result(manifest, sid, take_fp, "unavailable", [],
                                 available=False)
        if not rm.ocr_take_is_clear_or_waived(manifest, sid, take_fp):
            blocked.append(sid)
    rm.save_manifest(manifest, manifest_path)
    if blocked:
        raise ValueError("OCR_EXACT_WAIVER_REQUIRED: %s" % ", ".join(blocked))
    return True


def _validate_video_assembly_inputs(manifest, *, client, segments_path,
                                    results_path, reviews_path):
    rm.identity_gate(manifest, client=client)
    with open(segments_path, encoding="utf-8") as handle:
        segments_spec = json.load(handle)
    with open(results_path, encoding="utf-8") as handle:
        results_value = json.load(handle)
    with open(reviews_path, encoding="utf-8") as handle:
        reviews_value = json.load(handle)
    results = results_value.get("results") if isinstance(results_value, dict) else results_value
    segments = segments_spec.get("segments") or []
    if (segments_spec.get("client") != client or
            str(segments_spec.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("VIDEO_STAGE_RUN_IDENTITY_MISMATCH")
    if len(results or []) != len(segments):
        raise ValueError("VIDEO_STAGE_RESULT_COUNT_MISMATCH")
    if not isinstance(reviews_value, dict):
        raise ValueError("VIDEO_STAGE_REVIEWS_MAP_REQUIRED")
    expected_review_ids = {str(segment.get("id")) for segment in segments}
    if set(map(str, reviews_value)) != expected_review_ids:
        raise ValueError("VIDEO_STAGE_REVIEW_IDS_MISMATCH")
    source_takes = []
    for segment_id, review in reviews_value.items():
        if not isinstance(review, dict) or str(review.get("segment_id")) != str(segment_id):
            raise ValueError("VIDEO_STAGE_REVIEW_IDENTITY_MISMATCH: %s" % segment_id)
    for segment, result in zip(segments, results):
        sid = str(segment.get("id"))
        accepted = (manifest.get("accepted_takes") or {}).get(sid) or {}
        review = reviews_value.get(sid) or reviews_value.get(segment.get("id")) or {}
        if not accepted.get("take_fingerprint"):
            raise ValueError("VIDEO_ACCEPTED_TAKE_REQUIRED: %s" % sid)
        if accepted.get("take_fingerprint") != result.get("take_fingerprint"):
            raise ValueError("VIDEO_ACCEPTED_TAKE_MISMATCH: %s" % sid)
        if take_review.take_fingerprint(result) != accepted.get("take_fingerprint"):
            raise ValueError("VIDEO_ACCEPTED_TAKE_ARTIFACT_MISMATCH: %s" % sid)
        if (accepted.get("video_handoff_fingerprint") !=
                segment.get("video_handoff_fingerprint")):
            raise ValueError("VIDEO_ACCEPTED_TAKE_HANDOFF_MISMATCH: %s" % sid)
        if (result.get("video_handoff_fingerprint") !=
                segment.get("video_handoff_fingerprint") or
                not result.get("ok") or not (result.get("media_qc") or {}).get("passed") or
                not take_review.is_accepted(review, accepted.get("take_fingerprint")) or
                not rm.ocr_take_is_clear_or_waived(
                    manifest, sid, accepted.get("take_fingerprint"))):
            raise ValueError("VIDEO_STAGE_TAKE_CLOSURE_FAILED: %s" % sid)
        local_path = (result.get("localPath") or result.get("local_path") or
                      result.get("absPath") or result.get("path"))
        record = rm.file_record(local_path)
        if not record or not record.get("exists"):
            raise ValueError("VIDEO_STAGE_TAKE_FILE_MISSING: %s" % sid)
        source_takes.append(record)
    records = {
        "segments": rm.file_record(segments_path),
        "results": rm.file_record(results_path),
        "reviews": rm.file_record(reviews_path),
    }
    if any(not record or not record.get("exists") for record in records.values()):
        raise ValueError("VIDEO_STAGE_INPUT_MISSING")
    return records, source_takes


def begin_video_assembly(manifest, manifest_path, *, client, segments_path,
                         results_path, basecut_path, reviews_path):
    records, source_takes = _validate_video_assembly_inputs(
        manifest, client=client, segments_path=segments_path,
        results_path=results_path, reviews_path=reviews_path)
    planned = rm.require_run_output(
        manifest, basecut_path, label="video_assembly_output")
    planned_qc = rm.require_run_output(
        manifest, planned + ".qc.json", label="video_assembly_qc")
    rm.invalidate_from(manifest, "video")
    manifest.setdefault("generation", {}).setdefault("video", {}).update({
        "status": "running",
        "operation": "assembly",
        "planned_output": planned,
    })
    manifest["status"] = "generating_video"
    manifest["video_assembly"] = {
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        **records,
        "source_takes": source_takes,
        "handoff_sha256": ((manifest.get("handoffs") or {}).get("video") or {}).get("sha256"),
        "planned_output": planned,
        "planned_qc": planned_qc,
    }
    rm.save_manifest(manifest, manifest_path)
    return manifest["video_assembly"]


def record_video_stage_pending(manifest, manifest_path, *, client, segments_path,
                               results_path, basecut_path, reviews_path,
                               media_qc_report=None):
    """Bind the formal basecut to every review/render input and await approval."""
    records, source_takes = _validate_video_assembly_inputs(
        manifest, client=client, segments_path=segments_path,
        results_path=results_path, reviews_path=reviews_path)
    transaction = manifest.get("video_assembly") or {}
    if (transaction.get("status") != "running" or
            transaction.get("planned_output") != os.path.abspath(basecut_path) or
            transaction.get("segments") != records["segments"] or
            transaction.get("results") != records["results"] or
            transaction.get("reviews") != records["reviews"] or
            transaction.get("source_takes") != source_takes or
            transaction.get("handoff_sha256") !=
            ((manifest.get("handoffs") or {}).get("video") or {}).get("sha256") or
            any(not rm.file_record_is_current(record) for record in source_takes)):
        raise ValueError("VIDEO_ASSEMBLY_RUNNING_TRANSACTION_REQUIRED")
    qc_path = os.path.abspath(basecut_path) + ".qc.json"
    if transaction.get("planned_qc") != qc_path:
        raise ValueError("VIDEO_ASSEMBLY_RUNNING_TRANSACTION_REQUIRED")
    if media_qc_report is None:
        media_qc_report = media_qc.check(
            basecut_path, profile="formal", audio_required=True,
            report_path=qc_path)
    media_qc.require_pass(media_qc_report)
    output_record = rm.file_record(basecut_path)
    qc_file = media_qc_report.get("file") or {}
    try:
        with open(qc_path, encoding="utf-8") as handle:
            persisted_qc = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("VIDEO_ASSEMBLY_QC_IDENTITY_MISMATCH") from exc
    if (media_qc_report.get("profile") != "formal" or
            not (media_qc_report.get("expectations") or {}).get("audio_required") or
            persisted_qc != media_qc_report or
            qc_file.get("path") != output_record.get("path") or
            qc_file.get("sha256") != output_record.get("sha256") or
            not os.path.isfile(qc_path)):
        raise ValueError("VIDEO_ASSEMBLY_QC_IDENTITY_MISMATCH")
    artifacts = [segments_path, results_path, basecut_path, reviews_path, qc_path]
    rm.mark_video_generation_finished(manifest, artifacts)
    manifest["video_artifact"] = {
        "status": "pending_approval",
        "segments": rm.file_record(segments_path),
        "results": rm.file_record(results_path),
        "basecut": rm.file_record(basecut_path),
        "reviews": rm.file_record(reviews_path),
        "assembly_qc": media_qc_report,
        "assembly_qc_file": rm.file_record(qc_path),
        "handoff_sha256": ((manifest.get("handoffs") or {}).get("video") or {}).get("sha256"),
    }
    manifest["video_assembly"] = dict(
        transaction, status="pending_video_approval",
        finished_at=datetime.now().isoformat(timespec="seconds"),
        output=output_record, media_qc=media_qc_report,
        media_qc_file=rm.file_record(qc_path))
    rm.save_manifest(manifest, manifest_path)
    return manifest["video_artifact"]


def _fmt_srt_ts(seconds):
    """秒 → SRT 时间码 HH:MM:SS,mmm。"""
    if seconds < 0:
        seconds = 0
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "%02d:%02d:%02d,%03d" % (h, m, sec, ms)


_MAX_CHARS_PER_LINE = 20  # 每行字幕最大字符数（含标点）


def _split_long_sentence(text, max_chars=_MAX_CHARS_PER_LINE):
    """Split a long sentence at natural breakpoints (commas, pauses) to fit
    max_chars_per_line. Falls back to hard split if no natural breakpoint."""
    if len(text) <= max_chars:
        return [text]
    # Try splitting at Chinese comma/pause marks
    parts = re.split(r"(?<=[，、；])", text)
    if len(parts) > 1:
        result, current = [], ""
        for p in parts:
            if current and len(current) + len(p) > max_chars:
                result.append(current)
                current = p
            else:
                current += p
        if current:
            result.append(current)
        if all(len(r) <= max_chars for r in result):
            return result
    # Hard split at max_chars boundary
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _split_dialogue_sentences(text):
    """把一段台词按中英文断句符切成若干短句，供逐句字幕换行（避免整段字幕过长）。
    超过 _MAX_CHARS_PER_LINE 的长句会在逗号/顿号处二次拆分。"""
    if not text:
        return []
    # 在中英句末标点后切分，保留可读短句；无标点则整段一句。
    parts = re.split(r"(?<=[。！？!?；;\n])|(?<=[.])\s+", text)
    raw = [p.strip() for p in parts if p and p.strip()]
    if not raw:
        raw = [text.strip()]
    # 二次拆分超长句
    out = []
    for sentence in raw:
        out.extend(_split_long_sentence(sentence))
    return out


def derive_captions(segments_spec, fps=30, per_sentence=True, results=None):
    """连续性反推：定稿剧本各段台词 + 各段实际时长 → 字幕/动效脚本（阶段4尾→阶段5前）。

    解决的流程断点：底片(basecut.mp4)拼完后，此前没有任何一步把「剧本台词 + 各段时长」
    连续性反推成字幕时间轴和动效脚本，导致 subtitle_overlay 需要的 lines.json 只能人工手写。
    本函数按 segments 顺序累加时间轴（段i 的起点 = 前 i-1 段时长之和），产出：
      - srt：标准 SRT 字幕文本（可外挂/压制）
      - lines：[{text,start,end}]，可直接喂 subtitle_overlay.run/build_scenes
      - motion_plan：每段动效脚本骨架（段落时间窗 + 台词 + 可标关键词/动效意图占位），
        供后续 HyperFrames 编排（kinetic typography / 关键词快闪）填充。

    per_sentence=True：把每段台词按句末标点再切成逐句字幕（时间在段内按字数比例分配），
      字幕更贴合语音节奏、单条不过长；False 则整段台词一条字幕占满该段时间窗。

    **不自动往下走**：返回 needs_confirmation=True。时间轴/断句/动效意图需用户确认后，
      才继续 subtitle_overlay 渲染字幕层 + 合成。台词无声音轨精确对齐（音画一体模型未回传
      逐字时间戳），时间轴是按时长/字数估算的近似值，务必让用户核对再压制。
    """
    segs = segments_spec.get("segments") if isinstance(segments_spec, dict) else segments_spec
    if not segs:
        raise ValueError("derive_captions: 空 segments，无法反推字幕/动效")

    raw_results = results.get("results") if isinstance(results, dict) else results
    if raw_results is not None and (not isinstance(raw_results, list) or
                                    not all(isinstance(r, dict) for r in raw_results)):
        raise TypeError("derive_captions results 必须是结构化对象列表")
    result_by_id = {
        str(result.get("segment_id")): result for result in (raw_results or [])
        if result.get("segment_id")
    }

    srt_blocks = []
    lines = []
    motion_plan = []
    cursor = 0.0
    def _motion_elements_without_dialogue_duplicates(elements, dialogue):
        """Keep post effects from rendering a second copy of spoken CTA text."""
        spoken = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", dialogue or "").lower()
        if not spoken:
            return list(elements or [])
        kept = []
        for item in elements or []:
            text = str(item)
            compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text).lower()
            is_text_cta = re.search(r"cta|片尾|品牌", text, re.IGNORECASE)
            clauses = re.split(r"[：:，,；;。.!！?？]", text)
            duplicates_spoken = any(
                len(re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", clause)) >= 4
                and re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", clause).lower() in spoken
                for clause in clauses
            )
            if is_text_cta and duplicates_spoken:
                continue
            kept.append(item)
        return kept

    idx = 1
    for seg in segs:
        sid = seg.get("id")
        rendered = result_by_id.get(str(sid))
        actual = None
        if rendered:
            actual = rendered.get("actual_duration") or rendered.get("duration")
            if not actual and (rendered.get("localPath") or rendered.get("out_path")):
                actual = _probe_duration(rendered.get("localPath") or rendered.get("out_path"))
        secs = float(actual or seg.get("duration") or 0)
        if secs <= 0:
            secs = 3.0
        seg_start, seg_end = cursor, cursor + secs
        dialogue = (seg.get("dialogue") or "").strip()

        # 段级动效脚本骨架：时间窗 + 台词 + 待填的关键词/动效意图（供 HyperFrames 编排）
        motion_plan.append({
            "seg_id": sid,
            "start": round(seg_start, 3),
            "end": round(seg_end, 3),
            "dialogue": dialogue,
            "emphasis_keywords": [],          # 待用户/LLM 标：需快闪强调的关键词
            "motion": "",                      # 待填：kinetic/pop-in/underline/none 等动效意图
            "motion_elements": _motion_elements_without_dialogue_duplicates(
                list(seg.get("motion_elements") or [])
                if not isinstance(seg.get("motion_elements"), str)
                else [seg["motion_elements"]], dialogue),
            "note": "",
        })

        if not dialogue:
            # 无台词段（纯画面/音乐）：不生成字幕，仅占位时间窗给动效脚本
            cursor = seg_end
            continue

        if per_sentence:
            sentences = _split_dialogue_sentences(dialogue)
            total_chars = sum(len(re.sub(r"\s", "", s)) for s in sentences) or 1
            sub_cursor = seg_start
            for si, sent in enumerate(sentences):
                w = len(re.sub(r"\s", "", sent)) or 1
                dur = secs * (w / total_chars)
                st = sub_cursor
                en = seg_end if si == len(sentences) - 1 else sub_cursor + dur
                lines.append({"text": sent, "start": round(st, 3), "end": round(en, 3),
                              "seg_id": sid})
                srt_blocks.append("%d\n%s --> %s\n%s\n" % (
                    idx, _fmt_srt_ts(st), _fmt_srt_ts(en), sent))
                idx += 1
                sub_cursor = en
        else:
            lines.append({"text": dialogue, "start": round(seg_start, 3),
                          "end": round(seg_end, 3), "seg_id": sid})
            srt_blocks.append("%d\n%s --> %s\n%s\n" % (
                idx, _fmt_srt_ts(seg_start), _fmt_srt_ts(seg_end), dialogue))
            idx += 1

        cursor = seg_end

    return {
        "ok": True,
        "needs_confirmation": True,
        "total_seconds": round(cursor, 3),
        "srt": "\n".join(srt_blocks).strip() + "\n" if srt_blocks else "",
        "lines": lines,
        "motion_plan": motion_plan,
        "note": ("时间轴按各段时长/字数估算（音画一体模型未回传逐字时间戳），"
                 "为近似值。请用户确认时间轴/断句/动效关键词后，再继续字幕渲染与合成。"),
    }


def _sha256_json(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_json(path, value):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write_json(path, value)
    return path


def _commit_video_handoff(manifest, manifest_path, segments_path, segments_spec,
                          manifest_mutator=None):
    """Commit a formal segments handoff without exposing an unregistered file.

    The staged file is fully validated first.  The manifest is then committed
    with a record for the final path, and only afterwards is the staged file
    atomically renamed into place.  A manifest CAS failure therefore leaves an
    existing segments file untouched.  If the final rename fails, best-effort
    manifest rollback restores the previous logical state; either way readers
    fail closed instead of accepting a half-registered handoff.
    """
    final_path = os.path.abspath(segments_path)
    directory = os.path.dirname(final_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(
        prefix=".%s.handoff-" % os.path.basename(final_path),
        suffix=".json", dir=directory)
    os.close(fd)
    previous_manifest = copy.deepcopy(manifest)
    candidate = copy.deepcopy(manifest)
    expected_revision = int(manifest.get("revision") or 0)
    manifest_committed = False
    try:
        _atomic_json(staged_path, segments_spec)
        rm.record_video_handoff(candidate, segments_spec, staged_path)
        handoff = candidate["handoffs"]["video"]
        handoff["path"] = final_path
        handoff["file"]["path"] = final_path
        if manifest_mutator is not None:
            manifest_mutator(candidate, previous_manifest)
        rm.save_manifest(
            candidate, manifest_path, expected_revision=expected_revision)
        manifest_committed = True
        try:
            os.replace(staged_path, final_path)
        except OSError:
            try:
                rm.save_manifest(
                    previous_manifest, manifest_path,
                    expected_revision=int(candidate.get("revision") or
                                          (expected_revision + 1)))
            except (OSError, ValueError, RuntimeError):
                pass
            raise
        manifest.clear()
        manifest.update(candidate)
        return final_path
    finally:
        if os.path.exists(staged_path):
            os.remove(staged_path)
        if manifest_committed and not os.path.isfile(final_path):
            # The persisted record is intentionally stale/fail-closed if both
            # the final rename and rollback failed; never fabricate the file.
            pass


def apply_video_recovery_contract(segments_spec, contract_path, manifest):
    return video_recovery.apply_contract(
        segments_spec, contract_path, manifest)


def bind_recovery_privacy_policy(manifest, manifest_path, segments_path,
                                 policy_contract_path):
    return video_recovery.bind_privacy_policy(
        manifest, manifest_path, segments_path, policy_contract_path,
        commit_handoff=_commit_video_handoff)


def finalize_recovery_privacy_policy_contract(
        manifest, segments_path, template_path, out_path):
    return video_recovery.finalize_privacy_policy_contract(
        manifest, segments_path, template_path, out_path)


def promote_recovery_audio_canary(manifest, manifest_path, segments_path):
    return video_recovery.promote_audio_canary_to_batch(
        manifest, manifest_path, segments_path,
        commit_handoff=_commit_video_handoff)


def bind_recovery_canary_output(manifest, manifest_path, segments_path):
    return video_recovery.bind_canary_output(
        manifest, manifest_path, segments_path,
        commit_handoff=_commit_video_handoff)


def bind_recovery_attempt_output(manifest, manifest_path, segments_path,
                                 segment_id, rejected_review_path=None,
                                 failure_results_path=None):
    return video_recovery.bind_attempt_output(
        manifest, manifest_path, segments_path,
        commit_handoff=_commit_video_handoff,
        segment_id=segment_id,
        rejected_review_path=rejected_review_path,
        failure_results_path=failure_results_path)


def preflight_caption_inputs(manifest, *, client, segments_path, basecut_path,
                             results_path=None, output_paths=()):
    """Fail before writing captions unless they derive from the approved cut."""
    rm.identity_gate(manifest, client=client)
    if not rm.approval_is_current(manifest, "video"):
        raise ValueError("CAPTION_VIDEO_APPROVAL_REQUIRED")
    rm.validate_video_closure(manifest)
    video_artifact = manifest.get("video_artifact") or {}
    if (video_artifact.get("segments") != rm.file_record(segments_path) or
            video_artifact.get("basecut") != rm.file_record(basecut_path)):
        raise ValueError("CAPTION_INPUT_MUST_MATCH_APPROVED_VIDEO")
    if (results_path is not None and
            video_artifact.get("results") != rm.file_record(results_path)):
        raise ValueError("CAPTION_RESULTS_MUST_MATCH_APPROVED_VIDEO")
    for label, path in output_paths:
        rm.require_run_output(manifest, path, label=label, must_exist=False)
    return True


def persist_caption_artifact(manifest, manifest_path, *, client, segments_path,
                             basecut_path, lines_path, srt_path, motion_path,
                             caption_manifest_path):
    """Bind estimated caption files to this run, handoff, and basecut bytes."""
    preflight_caption_inputs(
        manifest, client=client, segments_path=segments_path,
        basecut_path=basecut_path, output_paths=(
            ("caption_lines", lines_path), ("caption_srt", srt_path),
            ("caption_motion", motion_path),
            ("caption_manifest", caption_manifest_path)))
    with open(segments_path, encoding="utf-8") as handle:
        segments_spec = json.load(handle)
    if segments_spec.get("client") != client or str(segments_spec.get("run_id")) != str(manifest.get("run_id")):
        raise ValueError("CAPTION_RUN_IDENTITY_MISMATCH")
    expected = {str(seg.get("id")): seg.get("video_handoff_fingerprint")
                for seg in segments_spec.get("segments") or []}
    recorded = (manifest.get("handoffs") or {}).get("video") or {}
    if not expected or recorded.get("segments") != expected:
        raise ValueError("CAPTION_VIDEO_HANDOFF_MISMATCH")
    records = {name: rm.file_record(path) for name, path in (
        ("segments", segments_path), ("basecut", basecut_path), ("lines", lines_path),
        ("srt", srt_path), ("motion", motion_path))}
    if any(not record or not record.get("exists") for record in records.values()):
        raise ValueError("CAPTION_ARTIFACT_MISSING")
    identity_payload = {
        "client": client, "run_id": manifest.get("run_id"),
        "video_handoff_sha256": recorded.get("sha256"),
        "files": {name: record.get("sha256") for name, record in records.items()},
    }
    artifact = {
        "schema_version": 1, "artifact_type": "caption_timeline",
        "client": client, "run_id": manifest.get("run_id"),
        "status": "pending_approval", "created_at": datetime.now().isoformat(timespec="seconds"),
        "video_handoff_sha256": recorded.get("sha256"), "files": records,
        "caption_identity": _sha256_json(identity_payload),
    }
    _atomic_json(caption_manifest_path, artifact)
    rm.mark_generation_finished(manifest, "captions", [lines_path, srt_path, motion_path])
    manifest["caption_artifact"] = {
        "path": os.path.abspath(caption_manifest_path),
        "caption_identity": artifact["caption_identity"], "status": "pending_approval",
    }
    rm.save_manifest(manifest, manifest_path)
    return artifact


def caption_artifact_is_current(manifest, caption_artifact, *, client=None,
                                require_approved=True):
    """Re-read every bound file and validate the caption timeline identity."""
    rm.identity_gate(manifest, client=client)
    if (caption_artifact.get("client") != manifest.get("client") or
            str(caption_artifact.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("CAPTION_RUN_IDENTITY_MISMATCH")
    for name in ("segments", "basecut", "lines", "srt", "motion"):
        if not rm.file_record_is_current((caption_artifact.get("files") or {}).get(name)):
            raise ValueError("STALE_CAPTION_ARTIFACT: %s hash 已变化" % name)
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    if caption_artifact.get("video_handoff_sha256") != handoff.get("sha256"):
        raise ValueError("CAPTION_VIDEO_HANDOFF_MISMATCH")
    payload = {
        "client": caption_artifact.get("client"), "run_id": caption_artifact.get("run_id"),
        "video_handoff_sha256": caption_artifact.get("video_handoff_sha256"),
        "files": {name: caption_artifact["files"][name].get("sha256")
                  for name in ("segments", "basecut", "lines", "srt", "motion")},
    }
    if caption_artifact.get("caption_identity") != _sha256_json(payload):
        raise ValueError("CAPTION_IDENTITY_MISMATCH")
    if require_approved:
        recorded = manifest.get("caption_artifact") or {}
        if (caption_artifact.get("status") != "approved" or
                recorded.get("status") != "approved" or
                recorded.get("caption_identity") != caption_artifact.get("caption_identity") or
                caption_artifact.get("confirmed_by_customer") is not True or
                not str(caption_artifact.get("customer_confirmation") or "").strip() or
                caption_artifact.get("confirmation_source") != "customer_dialogue" or
                recorded.get("customer_confirmation") !=
                caption_artifact.get("customer_confirmation") or
                not rm.customer_approval_evidence_is_current(manifest, "captions") or
                not rm.approval_is_current(manifest, "captions")):
            raise ValueError("CAPTION_APPROVAL_REQUIRED")
    return True


def confirm_captions(manifest, manifest_path, caption_manifest_path, *, client,
                     customer_confirmation):
    """Approve the exact timeline files without making rendered subtitles circular."""
    confirmation = str(customer_confirmation or "").strip()
    if not confirmation:
        raise ValueError("CUSTOMER_CONFIRMATION_EVIDENCE_REQUIRED: captions")
    original = None
    if os.path.isfile(caption_manifest_path):
        with open(caption_manifest_path, "rb") as handle:
            original = handle.read()
    with open(caption_manifest_path, encoding="utf-8") as handle:
        artifact = json.load(handle)
    caption_artifact_is_current(manifest, artifact, client=client, require_approved=False)
    expected_revision = int(manifest.get("revision") or 0)
    evidence = rm.record_customer_approval_evidence(
        manifest, "captions", confirmation)
    rm.approve(manifest, "captions", strict=True)
    artifact["status"] = "approved"
    artifact["approved_at"] = evidence["confirmed_at"]
    artifact["confirmed_by_customer"] = True
    artifact["customer_confirmation"] = confirmation
    artifact["confirmation_source"] = "customer_dialogue"
    artifact["approval_content_fingerprint"] = evidence["content_fingerprint"]
    _atomic_json(caption_manifest_path, artifact)
    manifest["caption_artifact"] = {
        "path": os.path.abspath(caption_manifest_path),
        "caption_identity": artifact["caption_identity"], "status": "approved",
        "confirmed_by_customer": True,
        "customer_confirmation": confirmation,
        "confirmation_source": "customer_dialogue",
        "approval_content_fingerprint": evidence["content_fingerprint"],
    }
    manifest_saved = False
    try:
        rm.save_manifest(
            manifest, manifest_path, expected_revision=expected_revision)
        manifest_saved = True
    finally:
        if not manifest_saved:
            if original is None:
                if os.path.exists(caption_manifest_path):
                    os.remove(caption_manifest_path)
            else:
                restore = caption_manifest_path + ".rollback"
                with open(restore, "wb") as handle:
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(restore, caption_manifest_path)
    return artifact


def _probe_duration(video_path):
    """Read the actual rendered duration; returns None when ffprobe is unavailable."""
    ffprobe = media_tools.resolve_ffmpeg_pair()[1]
    if not ffprobe or not os.path.isfile(video_path):
        return None
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            stderr=subprocess.STDOUT, text=True)
        value = float(out.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def align_captions_to_audio(derived, video_path, api_key=None):
    """Align the estimated timeline to the actual rendered audio.

    Three-tier strategy:
      1. ASR-based alignment (subtitle_asr.align_with_asr) — silence
         detection or cloud ASR for sentence-level timestamps.
      2. Duration scaling — ratio-scale estimates to match actual duration.
      3. Keep original estimates if both fail.
    """
    actual = _probe_duration(video_path)
    if actual is None or not derived.get("total_seconds"):
        derived["timeline_source"] = "estimated"
        return derived

    # Tier 1: ASR-based sentence alignment
    asr_aligned = subtitle_asr.align_with_asr(
        derived.get("lines", []), video_path, api_key=api_key)
    if asr_aligned:
        derived["lines"] = asr_aligned
        derived["total_seconds"] = round(actual, 3)
        derived["timeline_source"] = "asr_aligned"
        derived["needs_confirmation"] = True
        derived["note"] = ("已通过音频分析(ASR/静音检测)对齐逐句时间戳，"
                            "准确率高于字数比例估算；请抽查确认后再渲染字幕。")
        derived["srt"] = subtitle_asr.lines_to_srt(asr_aligned)
        # Re-scale motion_plan blocks proportionally
        old_total = float(derived.get("total_seconds") or 1)
        ratio = actual / old_total if old_total > 0 else 1.0
        if abs(ratio - 1.0) > 0.01:
            for block in derived.get("motion_plan", []):
                block["start"] = round(block["start"] * ratio, 3)
                block["end"] = round(block["end"] * ratio, 3)
        return derived

    # Tier 2: Duration scaling (original fallback)
    old = float(derived["total_seconds"])
    ratio = actual / old
    for line in derived.get("lines", []):
        line["start"] = round(line["start"] * ratio, 3)
        line["end"] = round(line["end"] * ratio, 3)
    for block in derived.get("motion_plan", []):
        block["start"] = round(block["start"] * ratio, 3)
        block["end"] = round(block["end"] * ratio, 3)
    derived["total_seconds"] = round(actual, 3)
    derived["timeline_source"] = "audio_duration_aligned_estimated_sentences"
    derived["needs_confirmation"] = True
    derived["note"] = ("已按成片实际时长校准时间轴，但未取得逐字 ASR 时间戳；"
                        "断句仍为按台词/字数估算，请确认后再渲染字幕。")
    # Rebuild SRT from the adjusted lines.
    derived["srt"] = "".join(
        "%d\n%s --> %s\n%s\n\n" % (i, _fmt_srt_ts(line["start"]),
                                      _fmt_srt_ts(line["end"]), line["text"])
        for i, line in enumerate(derived.get("lines", []), 1)
    )
    return derived


def main(argv=None):
    import script_splitter_cli
    return script_splitter_cli.main(sys.modules[__name__], argv)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        code = str(exc).split(":", 1)[0] or "WORKFLOW_BLOCKED"
        if code == "RENDER_PLAN_ARTIFACT_REQUIRED":
            next_step = ("下一步：先在当前 run 的 manifest 中登记并确认 render plan，"
                         "再重新执行 split；可用 `python3 START_HERE_AGENT.py status --manifest <manifest>` 查看阶段。")
        elif code in ("STORYBOARD_DIR_NOT_FOUND", "STORYBOARD_APPROVAL_REQUIRED"):
            next_step = ("下一步：先使用 storyboard.py --stage next 生成并确认当前 run 的故事板，"
                         "再重新执行 split。")
        elif code in ("UNCONFIRMED_ASSET", "MISSING_REFERENCE"):
            next_step = ("下一步：上传并确认缺失素材，或为内部预览显式使用 --draft/--allow-unconfirmed；"
                         "正式客户流程不能跳过确认。")
        else:
            next_step = "下一步：请查看当前 run 状态并完成该阶段要求后重试。"
        print("ERROR:%s\n%s\n详情：%s" % (code, next_step, exc), file=sys.stderr)
        raise SystemExit(2)
