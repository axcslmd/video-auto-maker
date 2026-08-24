#!/usr/bin/env python3
"""Storyboard product-usage geometry review helpers."""
import hashlib
import json
import os

import artifact_contract
import br_client
from project_utils import require_contained_path

USAGE_GEOMETRY_REVIEW_REQUIRED_CHECKS = (
    "product_identity_matches_confirmed_board",
    "active_product_and_receiver_both_visible",
    "product_contact_surface_visible",
    "receiver_contact_surface_visible",
    "flush_contact_line_visible",
    "product_outer_face_visible",
    "no_receiver_edge_screen_or_top_substitution",
    "usage_outcome_matches_script",
    "no_extra_text_labels_or_watermarks",
)

USAGE_GEOMETRY_REVIEW_PANEL_CHECKS = (
    "product_contact_surface_visible",
    "receiver_contact_surface_visible",
    "flush_contact_line_visible",
    "product_outer_face_visible",
    "not_edge_screen_or_top_substitution",
)

USAGE_GEOMETRY_REVIEW_CRITICAL_PANELS = ("3", "4", "7")


def usage_identity_reference_paths(board):
    paths = list(board.get("identity_reference_paths") or [])
    refinement = board.get("refinement") or {}
    paths.extend(refinement.get("identity_reference_paths") or [])
    result = []
    seen = set()
    for path in paths:
        if not path:
            continue
        abs_path = os.path.abspath(path)
        if abs_path not in seen:
            result.append(abs_path)
            seen.add(abs_path)
    return result


def validate_usage_board_identity_references(board):
    refs = usage_identity_reference_paths(board)
    if not refs:
        raise br_client.BRError(
            "USAGE_IDENTITY_REFERENCES_REQUIRED: 产品使用图缺少产品身份锚点记录，"
            "请用最新 storyboard.py --stage next 或 --refine-board usage 重新生成后再确认。")
    missing = [path for path in refs if not os.path.isfile(path)]
    if missing:
        raise br_client.BRError(
            "USAGE_IDENTITY_REFERENCE_MISSING: 产品使用图记录的产品身份锚点文件不存在：%s。"
            "请重新生成产品使用图。" % ", ".join(missing))
    if board.get("generation_mode") == "panelized_local_composite":
        panel_paths = board.get("panel_paths") or []
        panel_urls = board.get("panel_urls") or board.get("imageUrls") or []
        if len(panel_paths) != 9 or len(panel_urls) != 9:
            raise br_client.BRError(
                "USAGE_PANEL_HANDOFF_INCOMPLETE: 逐格产品使用图必须保留 9 个本地素材和 9 个 retrieve URL；"
                "当前为 %d 个本地素材、%d 个远程 URL，请重新生成。" %
                (len(panel_paths), len(panel_urls)))
        missing_panels = [path for path in panel_paths if not os.path.isfile(path)]
        if missing_panels:
            raise br_client.BRError(
                "USAGE_PANEL_LOCAL_ASSET_MISSING: 逐格产品使用图缺少本地素材：%s。"
                "请重新生成。" % ", ".join(missing_panels))
    return refs


def usage_panel_sha256s(board):
    result = {}
    for index, path in enumerate(board.get("panel_paths") or [], 1):
        if path and os.path.isfile(path):
            result[str(index)] = artifact_contract.file_sha256(path)
    return result


def review_bool(value):
    return value is True


def usage_geometry_review_fingerprint(review):
    review = dict(review)
    review.pop("_path", None)
    review.pop("review_fingerprint", None)
    return hashlib.sha256(json.dumps(
        review, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def load_usage_geometry_review(path, out_dir):
    if not path:
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_FILE_REQUIRED: 产品使用图带有物理关系合同，"
            "不能只用 --geometry-reviewed 或 Agent 自行判断确认。请先展示图片，"
            "由客户逐项确认后在本 run 目录写入 usage_geometry_review.json，"
            "再用 --geometry-review 指向该文件。")
    try:
        review_path = require_contained_path(
            out_dir, path, label="usage_geometry_review", must_exist=True)
    except ValueError as exc:
        raise br_client.BRError(str(exc)) from exc
    try:
        with open(review_path, encoding="utf-8") as handle:
            review = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_INVALID_JSON: %s" % exc) from exc
    if not isinstance(review, dict):
        raise br_client.BRError("USAGE_GEOMETRY_REVIEW_INVALID_JSON: 根节点必须是 object")
    review["_path"] = review_path
    return review


def validate_usage_geometry_review(board, review, *, board_sha256, source_fingerprint):
    status = str(review.get("status") or "").strip().lower()
    if status not in {"confirmed", "approved", "customer_confirmed", "customer_approved"}:
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_NOT_CONFIRMED: usage_geometry_review.json "
            "必须记录 status=customer_confirmed/customer_approved。")
    if review.get("agent_self_review") is True or review.get("reviewer_role") == "agent_only":
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_AGENT_ONLY_BLOCKED: 产品使用图不能由 Agent 自行确认。")
    if not review_bool(review.get("confirmed_by_customer")):
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_CUSTOMER_CONFIRMATION_REQUIRED: "
            "usage_geometry_review.json 必须包含 confirmed_by_customer=true。")
    confirmation = str(
        review.get("customer_confirmation") or
        review.get("customer_confirmation_text") or
        review.get("confirmation_text") or "").strip()
    if len(confirmation) < 8:
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_CONFIRMATION_TEXT_REQUIRED: "
            "必须记录客户对产品使用图的明确确认原话，不能只有 Agent 摘要。")
    if review.get("board_sha256") != board_sha256:
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_BOARD_MISMATCH: review 绑定的 board_sha256 与当前产品使用图不一致。")
    if review.get("source_fingerprint") != source_fingerprint:
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_SOURCE_MISMATCH: review 绑定的 source_fingerprint 与当前使用图来源不一致。")
    checks = review.get("checks") or {}
    failed = [key for key in USAGE_GEOMETRY_REVIEW_REQUIRED_CHECKS
              if not review_bool(checks.get(key))]
    if failed:
        raise br_client.BRError(
            "USAGE_GEOMETRY_REVIEW_CHECKS_INCOMPLETE: 以下产品使用图复核项未通过：%s。"
            "请先用 --refine-board usage 修正，再重新展示给客户确认。"
            % ", ".join(failed))
    panel_hashes = usage_panel_sha256s(board)
    if board.get("generation_mode") == "panelized_local_composite":
        panel_checks = review.get("panel_checks") or {}
        for panel_index in USAGE_GEOMETRY_REVIEW_CRITICAL_PANELS:
            item = panel_checks.get(panel_index) or panel_checks.get(int(panel_index))
            if not isinstance(item, dict):
                raise br_client.BRError(
                    "USAGE_GEOMETRY_REVIEW_PANEL_CHECK_REQUIRED: "
                    "关键接触格 %s 缺少逐格复核。" % panel_index)
            if panel_hashes and item.get("panel_sha256") != panel_hashes.get(panel_index):
                raise br_client.BRError(
                    "USAGE_GEOMETRY_REVIEW_PANEL_HASH_MISMATCH: "
                    "关键接触格 %s 的 panel_sha256 与当前素材不一致。" % panel_index)
            failed_panel = [key for key in USAGE_GEOMETRY_REVIEW_PANEL_CHECKS
                            if not review_bool(item.get(key))]
            if failed_panel:
                raise br_client.BRError(
                    "USAGE_GEOMETRY_REVIEW_PANEL_CHECKS_INCOMPLETE: "
                    "关键接触格 %s 未通过：%s。请先精修使用图。"
                    % (panel_index, ", ".join(failed_panel)))
    clean_review = dict(review)
    clean_review.pop("_path", None)
    clean_review.pop("review_fingerprint", None)
    clean_review["review_fingerprint"] = usage_geometry_review_fingerprint(clean_review)
    return clean_review


def usage_geometry_review_record_current(record, out_dir):
    if not record.get("geometry_reviewed"):
        return False
    review_path = record.get("geometry_review_path")
    if not review_path:
        return False
    try:
        review_path = require_contained_path(
            out_dir, review_path, label="usage_geometry_review",
            must_exist=True)
        with open(review_path, encoding="utf-8") as handle:
            review = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(review, dict):
        return False
    panel_paths = record.get("panel_paths") or []
    if record.get("generation_mode") == "panelized_local_composite":
        if len(panel_paths) != 9:
            return False
        if not all(os.path.isfile(path) for path in panel_paths):
            return False
    review["_path"] = review_path
    board = {
        "generation_mode": record.get("generation_mode"),
        "panel_paths": panel_paths,
    }
    try:
        clean_review = validate_usage_geometry_review(
            board, review, board_sha256=record.get("board_sha256"),
            source_fingerprint=record.get("source_fingerprint"))
    except br_client.BRError:
        return False
    expected = clean_review.get("review_fingerprint")
    if not expected or record.get("geometry_review_fingerprint") != expected:
        return False
    stored = record.get("geometry_review")
    if not isinstance(stored, dict):
        return False
    stored_body = dict(stored)
    stored_fingerprint = stored_body.pop("review_fingerprint", None)
    return stored_fingerprint == expected and stored_body == {
        key: value for key, value in clean_review.items()
        if key != "review_fingerprint"
    }
