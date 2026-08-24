#!/usr/bin/env python3
"""Video engine: submit createVideo, poll, download to output/.

音画一体（NOT two steps）: kling-v3-omni-video 一次调用即产出「带配音+对口型」的
成片——`--text` 就是要念的台词脚本，模型自动配音、对口型、生成画面。没有"先出
视频再单独配音"的第二步，也不要再叫 TTS 后期。

异步=并行工作流: createVideo 返回 taskId 后可继续提交下一段，多段应「先全部提交，
再统一轮询」（见 render_batch / --batch），墙钟时间≈单段，不是 N× 串行。

Wraps br_client so scene skills just call one command. Reads the API key via
key_setup (agent session cache selected by BASICROUTER_SESSION_ID).

CLI example（单段也写成仅含一项的 batch；生成前先做 preflight 并确认提示词）:
  python3 video_engine.py --batch segments.json --preflight \
      --preflight-out video_preflight.json
  python3 prompt_review.py capture-video --plan storyboard_plan.json \
      --segments segments.json --preflight-json video_preflight.json \
      --out video_prompt_review.json --preview-out video_prompt_review.md
  python3 video_engine.py --batch segments.json --client <client> \
      --manifest <run_manifest.json> --results-out <batch_results.json> \
      --prompt-review <confirmed_video_prompt_review.json>
   # segments.json = [{"text":"...","video_type":4,"urls":["..."],"duration":8,"ratio":"9:16","out_path":"output/seg1.mp4"}, ...]
   # script_splitter.split() 的完整输出也可直接传入（自动提取其中的 segments 数组）
"""
import os
import sys
import json
import argparse
import tempfile
import hashlib
import uuid
import threading
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import br_client            # noqa: E402
import key_setup            # noqa: E402
import seedance_prompt      # noqa: E402
import ux                    # noqa: E402
import artifact_contract     # noqa: E402
import generation_ledger     # noqa: E402
import take_review           # noqa: E402
import media_qc              # noqa: E402
import video_models as _video_models  # noqa: E402
import run_manifest as _rm   # noqa: E402 — hoisted from 12 inline imports (v3 cleanup)
import cost_ledger          # noqa: E402 — v3 cost tracking in _persist_task
import schema_validate      # noqa: E402 — v5 契约运行时校验（reference-contract）
import policy_check         # noqa: E402 — v5 集中化闸门策略（policies/gates.json）
import obs_log              # noqa: E402 — v5 结构化事件日志（run_id 全链 trace）
import language_contract    # noqa: E402
import panel_expansion      # noqa: E402
from project_utils import atomic_write_json  # noqa: E402
from video_models import (   # noqa: E402 — v4 shim: identical functions extracted
    DEFAULT_MODEL, IMAGE_MODEL_FALLBACK, VIDEO_MODEL_CAPS, VIDEO_MODEL_FALLBACK,
    VIDEO_MODEL_INTEGRATED_AUDIO, VIDEO_MODEL_INTEGRATED_AUDIO_EVIDENCE,
    _available_models_set, _catalog_bool, _catalog_list,
    _catalog_record_route_names, _catalog_submission_names,
    _integrated_audio_value, _is_kling_video_model, _model_catalog,
    _resolve_catalog_model,
)
from video_tasks import (     # noqa: E402 — v4 shim: identical functions extracted
    _completed_task, _current_attempt, _manifest_handoff_matches,
    _provider_error_evidence,
    _persist_submission_intent,
    _persist_submission_prompt_snapshot, _persist_task, _record_task_resume,
    _submission_prompt_snapshot_fields, _submission_request_id,
    _task_video_url, _write_video_submission_report,
)
from video_guard import (     # noqa: E402 — v4 shim: extracted QC/validation
    _media_qc_guard, _ocr_guard, _reference_is_trusted,
    _trusted_remote_segment_reference, _validate_model_reference_capacity as _guard_validate_model_reference_capacity,
    _validate_references as _guard_validate_references,
)
from video_prompts import (   # noqa: E402 — v4 shim: extracted prompt builders
    DEFAULT_NEGATIVE, NATIVE_STORYBOARD_VIDEO_RULES,
    PANEL_REFERENCE_VIDEO_RULES, STORYBOARD_VIDEO_RULES,
    _assert_confirmed_submission_for_model, _fit_video_prompt_limit,
    _require_confirmed_prompt_review, _submission_text,
    _submission_source_fingerprint, _uses_native_storyboard_prompt,
)


_PROCESS_VIDEO_OWNER = "video-%s-%s" % (os.getpid(), uuid.uuid4().hex)
VIDEO_PREFLIGHT_CONTRACT_VERSION = 8
PRIVACY_FALLBACK_STOP_POLICY = "stop_without_paid_fallback"
EMPIRICAL_AUDIO_CANARY_POLICY = "empirical_canary_unknown_output_audio"
EMPIRICAL_AUDIO_RETEST_POLICY = (
    "empirical_retest_disproved_output_audio_after_prompt_change")
EMPIRICAL_AUDIO_POLICIES = {
    EMPIRICAL_AUDIO_CANARY_POLICY, EMPIRICAL_AUDIO_RETEST_POLICY}


def _video_generation_owner_token():
    return "%s-thread-%s" % (_PROCESS_VIDEO_OWNER, threading.get_ident())


def _segment_integrated_audio_requirement(segment):
    """Return a truthy selector value when one segment needs embedded audio."""
    audio = segment.get("audio_contract") or {}
    dialogue = audio.get("dialogue") or segment.get("dialogue")
    if str(dialogue or "").strip():
        return str(dialogue)
    if (audio.get("track") == "required" or
            any(audio.get(key) not in (None, "", False)
                for key in ("voice", "bgm", "sfx", "lip_sync"))):
        return "__embedded_audio_required__"
    return None


def _validate_references(segments, client, manifest, draft=False):
    return _guard_validate_references(
        segments, client, manifest, draft=draft,
        reference_is_trusted=_reference_is_trusted,
        trusted_remote_segment_reference=_trusted_remote_segment_reference)


def _validate_model_reference_capacity(model, reference_count, *, formal):
    return _guard_validate_model_reference_capacity(
        model, reference_count, formal=formal, model_catalog=_model_catalog)


def _validate_reference_handoff(segments):
    """Reject segments whose confirmed reference contract was reduced."""
    for seg in segments:
        for ref in seg.get("references") or []:
            if (isinstance(ref, dict) and
                    ref.get("type") in {"character_board", "actor_board"}):
                raise ValueError(
                    "CAST_CONTACT_SHEET_VIDEO_INPUT_FORBIDDEN: segment %s 仍把人物六视图整板"
                    "作为视频输入。六视图只用于客户审核；请改用已确认的单人 identity "
                    "plate/portrait，避免模型把六个视图理解为六个人。" %
                    (seg.get("id") or "unknown"))
        required = set(seg.get("required_reference_types") or [])
        if not required:
            continue
        actual = {ref.get("type") for ref in seg.get("references") or []
                  if isinstance(ref, dict) and ref.get("type")}
        missing = sorted(required - actual)
        dropped = [item for item in seg.get("dropped_references") or []
                   if item.get("type") in required]
        if missing or dropped:
            detail = ", ".join(missing or [item.get("type", "unknown") for item in dropped])
            raise ValueError(
                "REFERENCE_HANDOFF_INCOMPLETE: segment %s 缺少已确认参考图类型 [%s]。"
                "视频请求已阻断；不能把产品使用板、人物板、产品板或故事板静默丢弃。"
                % (seg.get("id") or "unknown", detail))
        for ref in seg.get("references") or []:
            if isinstance(ref, dict) and (ref.get("url") or ref.get("id")):
                schema_validate.enforce(
                    ref, "reference-contract",
                    context="video_engine._validate_reference_handoff:%s"
                            % (seg.get("id") or "unknown"))


def _validate_generation_contracts(segments, *, formal=False):
    """Fail before a paid request when language/background/role contracts drift."""
    locks = []
    for segment in segments or []:
        segment_id = segment.get("id") or "unknown"
        if formal and "type" in segment and "video_type" not in segment:
            raise ValueError(
                "LEGACY_VIDEO_TYPE_FIELD: segment %s 仍使用旧字段 type；正式出片必须重新编译 "
                "segments.json 并显式写 video_type，禁止静默回落为文生视频。" % segment_id)
        if formal and "out" in segment and "out_path" not in segment:
            raise ValueError(
                "LEGACY_VIDEO_OUT_FIELD: segment %s 仍使用旧字段 out；正式出片必须重新编译并写 "
                "out_path，避免结果写入与审批清单脱节。" % segment_id)
        artifact_contract.validate_storyboard_binding(
            segment,
            strict=bool(formal or segment.get("video_handoff_fingerprint")))
        audio = segment.get("audio_contract") or {}
        language_contract.ensure_dialogue_language(
            audio.get("dialogue") or segment.get("dialogue"),
            audio.get("language") or segment.get("language"),
            context="video segment %s" % (segment.get("id") or "unknown"))
        lock = segment.get("background_identity_lock") or {}
        role = str(segment.get("generation_role") or
                   (segment.get("video_model_contract") or {}).get("role") or "")
        if segment.get("design_board") or role == "avatar_plate_only":
            contract = segment.get("video_model_contract") or {}
            if contract.get("role") != "avatar_plate_only":
                raise ValueError(
                    "AVATAR_PLATE_CONTRACT_INVALID: segment %s 必须声明 video_model_contract.role=avatar_plate_only" %
                    (segment.get("id") or "unknown"))
            forbidden = set(contract.get("forbidden_inputs") or [])
            if segment.get("storyboard_ref") or segment.get("storyboard_ref_mode"):
                raise ValueError(
                    "DESIGN_BOARD_STORYBOARD_REF_FORBIDDEN: segment %s 的设计板只能用于合成，不得作为视频模型分镜参考" %
                    (segment.get("id") or "unknown"))
            leaked = []
            for item in segment.get("references") or []:
                if not isinstance(item, dict):
                    continue
                ref_type = str(item.get("type") or "")
                tag = str(item.get("tag") or "").lower()
                source = str(item.get("source") or item.get("label") or "").lower()
                if (ref_type in {"storyboard_composition", "design_board", "ppt_page"} or
                        tag.startswith(("@ppt", "@slide", "@doc", "@design_board")) or
                        any(token in source for token in ("ppt", "powerpoint", "slide", "design board"))):
                    leaked.append(item.get("tag") or ref_type or item.get("url") or "unknown")
            leaked = sorted(set(map(str, leaked)))
            if leaked:
                raise ValueError(
                    "AVATAR_PLATE_GRAPHICS_LEAK: segment %s 将 %s 设计层参考图带入视频模型合同；"
                    "请只保留人物、声音、服装和背景锚点" %
                    (segment.get("id") or "unknown", ", ".join(leaked)))
            if formal and segment.get("video_type") not in (4, 5):
                raise ValueError(
                    "AVATAR_PLATE_VIDEO_TYPE_REQUIRED: segment %s 的文档讲解底片必须显式使用 "
                    "video_type=4/5 和已确认人物/场景锚点；禁止缺省为 type1。" % segment_id)
        if not lock:
            continue
        locks.append((lock.get("description"), tuple(lock.get("reference_urls") or [])))
        if formal and lock.get("requires_reference") and not lock.get("reference_urls"):
            raise ValueError(
                "BACKGROUND_REFERENCE_REQUIRED: segment %s 需要一张已确认的干净统一背景参考图；"
                "PPT页/设计板不能充当数字人视频背景，请补充 asset_refs.background_images 后重新 split。"
                % (segment.get("id") or "unknown"))
    if formal and locks and len(set(locks)) > 1:
        raise ValueError("BACKGROUND_LOCK_MISMATCH: 多段数字人底片没有使用同一背景合同")


def _result_dependency_fingerprint(result):
    if not isinstance(result, dict):
        return None
    for key in ("dependency_fingerprint", "generation_dependency_fingerprint"):
        value = result.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    dependency = result.get("generation_dependency") or {}
    if isinstance(dependency, dict):
        value = dependency.get("fingerprint")
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _expand_product_refs(segment, client):
    sku = segment.get("product_sku")
    if not sku or segment.get("urls"):
        return segment
    try:
        import product_library as _pl
        resolved = _pl.resolve(segment.get("client") or client or "", sku)
    except (Exception, SystemExit) as error:
        raise ValueError("PRODUCT_SKU_RESOLVE_FAILED: %s (%s)" % (sku, error))
    expanded = ([resolved.get("hero")] if resolved.get("hero") else [])
    expanded.extend(resolved.get("refs") or [])
    if resolved.get("product_board_confirmed") and resolved.get("product_board"):
        expanded.append(resolved["product_board"])
    expanded = list(dict.fromkeys(item for item in expanded if item))[:5]
    if not expanded:
        raise ValueError("PRODUCT_SKU_NO_REFERENCES: %s" % sku)
    copy = dict(segment, urls=expanded, video_type=5)
    return copy


def _atomic_json_write(path, value):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    atomic_write_json(path, value)

# OCR/QC guards and prompt builders are split into helper modules.  Keep these
# model helpers as patchable compatibility shims for older tests and callers.


def _normalize_model_catalog(models):
    return _video_models._normalize_model_catalog(models)


def _model_allow_types(category="video"):
    return _video_models._model_allow_types(
        category, model_catalog_fn=_model_catalog)


def _model_supports_type(model, video_type):
    return _video_models._model_supports_type(
        model, video_type,
        model_allow_types_fn=_model_allow_types, caps=VIDEO_MODEL_CAPS)


def _model_integrated_audio(model_id, record=None):
    return _video_models._model_integrated_audio(
        model_id, record, integrated_audio_table=VIDEO_MODEL_INTEGRATED_AUDIO)


def _video_image_ref(path_or_url, api_key=None, allow_local_draft=False):
    """Prepare createVideo imageUrls without regenerating the reference image.

    BasicRouter video docs define imageUrls as image material URLs. Local files
    and data URLs are not accepted here: confirmed images must carry forward the
    URL returned by /v1/image-generations/{taskId}. Do not call image generation
    as a pseudo-hosting/upload service because that can redraw confirmed assets.

    Draft/test mode may still adapt local paths through br_client.to_image_ref so
    old offline tests can exercise routing without hitting the paid video API.
    That escape hatch must never be used by formal generation.
    """
    value = str(path_or_url or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if allow_local_draft:
        return br_client.to_image_ref(value, api_key=api_key)
    raise ValueError(
        "VIDEO_IMAGE_URL_REQUIRED: createVideo imageUrls must be HTTP(S) URLs "
        "returned by BasicRouter image retrieve, got %s" % (value[:120] or "<empty>"))


def _pick_video_model(preferred=None, video_type=None, dialogue=None, formal=False,
                      allow_fallback=True, reference_count=None,
                      exclude_models=None, catalog=None,
                      allow_unproven_audio=False, proven_audio_models=()):
    catalog_fn = (_model_catalog if catalog is None else
                  (lambda _category: catalog))
    available_fn = (_available_models_set if catalog is None else
                    (lambda _category: set()))
    # When a caller supplies a catalog snapshot, every capability decision must
    # stay inside that snapshot/offline table.  Reusing the compatibility shim
    # here would query the gateway again through ``_model_allow_types`` whenever
    # the explicit snapshot is empty, defeating deterministic preflight tests.
    supports_type_fn = (
        _model_supports_type if catalog is None else
        (lambda model_id, requested_type:
         _video_models._model_supports_type(
             model_id, requested_type,
             model_allow_types_fn=lambda _category: {},
             caps=VIDEO_MODEL_CAPS)))
    return _video_models._pick_video_model(
        preferred, video_type, dialogue, formal, allow_fallback,
        reference_count, exclude_models, allow_unproven_audio,
        proven_audio_models,
        model_catalog_fn=catalog_fn,
        available_models_fn=available_fn,
        resolve_catalog_model_fn=_resolve_catalog_model,
        model_supports_type_fn=supports_type_fn,
        model_integrated_audio_fn=_model_integrated_audio,
        fallback_chain=VIDEO_MODEL_FALLBACK,
        default_model=DEFAULT_MODEL)


def _pick_image_model(preferred=None):
    return _video_models._pick_image_model(
        preferred, available_models_fn=_available_models_set,
        fallback_chain=IMAGE_MODEL_FALLBACK)


def _pick_privacy_fallback_model(video_type, dialogue=None, *, formal=True,
                                 reference_count=None, catalog=None):
    offline_catalog = {"records": {}, "aliases": {}}

    def offline_picker(preferred=None, video_type=None, dialogue=None,
                       formal=False, allow_fallback=True,
                       reference_count=None, exclude_models=None):
        return _pick_video_model(
            preferred, video_type=video_type, dialogue=dialogue,
            formal=formal, allow_fallback=allow_fallback,
            reference_count=reference_count, exclude_models=exclude_models,
            catalog=offline_catalog)

    return _video_models._pick_privacy_fallback_model(
        video_type, dialogue, formal=formal,
        reference_count=reference_count, catalog=catalog,
        pick_video_model_fn=offline_picker)




# 注：视频出片不做两遍清洗（首帧图生只会重生成、不保证一致，已移除）。
# 视频质量靠正式 take/QC/OCR/客户验片与受控新 attempt；两遍清洗只用于文生图阶段。
# （见 asset_prep.py refine_image），因为静态图的二次精修可控且可让客户确认。


def _privacy_rejection_stops(segment):
    return str((segment or {}).get("privacy_fallback_policy") or "").strip() == \
        PRIVACY_FALLBACK_STOP_POLICY


def _empirical_audio_canary_requested(segment):
    return str((segment or {}).get("audio_capability_policy") or "").strip() in \
        EMPIRICAL_AUDIO_POLICIES


def _validate_empirical_audio_route(
        segment, *, model_catalog=None, allow_disproved=False):
    """Validate one exact live route without claiming it embeds audio.

    This gate proves only that the route is online and accepts the exact
    canary request.  It intentionally does *not* turn an ``audio`` input
    modality into an output-audio capability; downloaded media must establish
    that fact through ffprobe and non-silence QC.
    """
    requested = str((segment or {}).get("model") or "").strip()
    if not requested:
        raise ValueError("EMPIRICAL_AUDIO_CANARY_MODEL_REQUIRED")
    catalog = (model_catalog if model_catalog is not None else
               _model_catalog("video"))
    route_key = _resolve_catalog_model(requested, catalog)
    if not route_key:
        raise ValueError(
            "EMPIRICAL_AUDIO_CANARY_ROUTE_NOT_IN_LIVE_CATALOG: %s" %
            requested)
    record = (catalog.get("records") or {}).get(route_key) or {}
    if (not record.get("active") or record.get("conflict") or
            record.get("route_identity_conflict") or
            str(record.get("submission_name") or "") != requested):
        raise ValueError(
            "EMPIRICAL_AUDIO_CANARY_EXACT_ROUTE_REQUIRED: %s -> %s/%s" %
            (requested, route_key, record.get("submission_name")))
    video_type = int((segment or {}).get("video_type") or 1)
    allow_types = record.get("allow_types")
    if allow_types is None or video_type not in allow_types:
        raise ValueError(
            "EMPIRICAL_AUDIO_CANARY_VIDEO_TYPE_UNSUPPORTED: %s type=%s" %
            (requested, video_type))
    reference_count = len((segment or {}).get("urls") or [])
    if (reference_count and record.get("image_count") is None) or (
            record.get("image_count") is not None and
            reference_count > int(record.get("image_count"))):
        raise ValueError(
            "EMPIRICAL_AUDIO_CANARY_REFERENCE_BUDGET_UNPROVEN: %s refs=%s max=%s" %
            (requested, reference_count, record.get("image_count")))
    resolution = str((segment or {}).get("resolution") or "").strip().lower()
    resolutions = {
        str(item).strip().lower()
        for item in (record.get("resolutions") or ()) if str(item).strip()}
    if not resolution or not resolutions or resolution not in resolutions:
        raise ValueError(
            "EMPIRICAL_AUDIO_CANARY_RESOLUTION_UNSUPPORTED: %s resolution=%s allowed=%s" %
            (requested, resolution or "<missing>",
             ",".join(sorted(resolutions)) or "<unproven>"))
    modalities = {
        str(item).strip().lower()
        for item in (record.get("multimodel_types") or ())}
    outputs = {
        str(item).strip().lower()
        for item in (record.get("output_multi_type") or ())}
    # Some live BasicRouter video routes publish an empty output modality list.
    # That is incomplete metadata, not proof that a video endpoint cannot
    # return video.  An explicit non-empty list that excludes video remains a
    # blocker; an empty list is allowed only for this empirical one-shot gate.
    if ("audio" not in modalities or
            (reference_count and "image" not in modalities) or
            (outputs and "video" not in outputs)):
        raise ValueError(
            "EMPIRICAL_AUDIO_CANARY_ROUTE_MODALITY_INELIGIBLE: %s inputs=%s outputs=%s" %
            (requested, ",".join(sorted(modalities)),
             ",".join(sorted(outputs))))
    # Both live evidence and audited offline aliases must be genuinely
    # unknown.  Explicit true belongs to the normal path; explicit false has
    # already disproven the route.
    catalog_audio = record.get("integrated_audio")
    audited_audio = _model_integrated_audio(route_key, record)
    if (catalog_audio is True or audited_audio is True or
            ((catalog_audio is False or audited_audio is False) and
             not allow_disproved)):
        raise ValueError(
            "EMPIRICAL_AUDIO_CANARY_REQUIRES_UNKNOWN_OUTPUT_AUDIO: %s" %
            requested)
    dialogue = (((segment or {}).get("audio_contract") or {}).get("dialogue")
                or (segment or {}).get("dialogue"))
    if not str(dialogue or "").strip():
        raise ValueError("EMPIRICAL_AUDIO_CANARY_DIALOGUE_REQUIRED")
    return requested


def _validate_empirical_audio_canary_policy(
        segment, *, manifest=None, formal_stage="video", model_catalog=None,
        validate_route=True):
    """Authorize unknown output audio only for the bound recovery canary."""
    policy = str((segment or {}).get("audio_capability_policy") or "").strip()
    if not policy:
        return False
    if policy not in EMPIRICAL_AUDIO_POLICIES:
        raise ValueError("AUDIO_CAPABILITY_POLICY_UNSUPPORTED: %s" % policy)
    if formal_stage != "test_segment" or manifest is None:
        raise ValueError(
            "AUDIO_CAPABILITY_POLICY_NOT_AUTHORIZED: 仅允许正式 recovery test_segment")
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    sid = str((segment or {}).get("id") or "")
    recovery = (segment or {}).get("recovery_contract") or {}
    policy_contract = (segment or {}).get("audio_capability_policy_contract") or {}
    contract_record = requirement.get("contract") or {}
    handoff = artifact_contract.verify_video_handoff(segment)
    manifest_handoff = (((manifest.get("handoffs") or {}).get("video") or {})
                        .get("segments") or {}).get(sid)
    if (not _rm.recovery_test_segment_requirement_is_current(manifest) or
            manifest.get("requires_test_segment") is not True or
            sid != str(requirement.get("segment_id") or "") or
            requirement.get("video_handoff_fingerprint") !=
            segment.get("video_handoff_fingerprint") or
            manifest_handoff != segment.get("video_handoff_fingerprint") or
            not handoff.get("strict") or not handoff.get("ok") or
            recovery.get("recovery_id") != requirement.get("recovery_id") or
            recovery.get("path") != contract_record.get("path") or
            recovery.get("sha256") != contract_record.get("sha256") or
            policy_contract.get("policy") != policy or
            policy_contract.get("scope") != "formal_test_segment_only" or
            policy_contract.get("segment_id") != sid or
            policy_contract.get("recovery_id") != requirement.get("recovery_id") or
            policy_contract.get("file") != contract_record or
            not _rm.file_record_is_current(contract_record)):
        raise ValueError(
            "AUDIO_CAPABILITY_POLICY_NOT_AUTHORIZED: canary、handoff 或恢复合同不匹配")
    try:
        with open(contract_record["path"], encoding="utf-8") as handle:
            contract = json.load(handle)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "AUDIO_CAPABILITY_POLICY_CONTRACT_INVALID: %s" % exc) from exc
    strategy = contract.get("strategy") or {}
    if (contract.get("kind") != "video_recovery_contract" or
            int(contract.get("schema_version") or 0) != 1 or
            str(contract.get("client")) != str(manifest.get("client")) or
            str(contract.get("run_id")) != str(manifest.get("run_id")) or
            contract.get("recovery_id") != requirement.get("recovery_id") or
            contract.get("confirmed_by_customer") is not True or
            contract.get("confirmation_source") != "customer_dialogue" or
            not str(contract.get("customer_confirmation") or "").strip() or
            strategy.get("audio_capability_policy") != policy or
            strategy.get("require_canary_before_batch") is not True or
            str(strategy.get("canary_segment_id") or "") != sid or
            str(strategy.get("target_model") or "") !=
            str(segment.get("model") or "") or
            str(strategy.get("resolution") or "").lower() !=
            str(segment.get("resolution") or "").lower()):
        raise ValueError("AUDIO_CAPABILITY_POLICY_CONTRACT_INVALID")
    if policy == EMPIRICAL_AUDIO_RETEST_POLICY:
        evidence = strategy.get("disproved_audio_evidence") or {}
        qc_record = evidence.get("media_qc_report") or {}
        if (str(evidence.get("model") or "") !=
                str(segment.get("model") or "") or
                not str(evidence.get("take_fingerprint") or "").strip() or
                not str(evidence.get("prior_submission_prompt_sha256") or "").strip() or
                not str(strategy.get("prompt_change_summary") or "").strip() or
                not _rm.file_record_is_current(qc_record)):
            raise ValueError("AUDIO_RETEST_DISPROVED_ROUTE_EVIDENCE_INVALID")
        try:
            with open(qc_record["path"], encoding="utf-8") as handle:
                prior_qc = json.load(handle)
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "AUDIO_RETEST_DISPROVED_ROUTE_EVIDENCE_INVALID") from exc
        if (prior_qc.get("passed") is not False or
                int((((prior_qc.get("media") or {}).get("audio") or {})
                    .get("stream_count") or 0)) != 0):
            raise ValueError("AUDIO_RETEST_DISPROVED_ROUTE_EVIDENCE_INVALID")
    if not validate_route:
        return str(segment.get("model") or "")
    return _validate_empirical_audio_route(
        segment, model_catalog=model_catalog,
        allow_disproved=policy == EMPIRICAL_AUDIO_RETEST_POLICY)


def _validate_privacy_stop_policy(segment, *, manifest=None,
                                  formal_stage="video"):
    """Authorize stop-only handling for one exact recovery canary."""
    policy = str((segment or {}).get("privacy_fallback_policy") or "").strip()
    if not policy:
        return False
    if policy != PRIVACY_FALLBACK_STOP_POLICY:
        raise ValueError("PRIVACY_FALLBACK_POLICY_UNSUPPORTED: %s" % policy)
    policy_record = segment.get("privacy_fallback_policy_contract") or {}
    if policy_record.get("scope") == "formal_batch_after_approved_canary":
        if (formal_stage != "video" or manifest is None or
                policy_record.get("kind") !=
                "promoted_canary_privacy_stop_policy" or
                policy_record.get("policy") !=
                PRIVACY_FALLBACK_STOP_POLICY):
            raise ValueError(
                "PRIVACY_FALLBACK_POLICY_NOT_AUTHORIZED: 正式批次 Canary 晋级合同无效")
        _rm.validate_audio_canary_promotion_segment(manifest, segment)
        return True
    if formal_stage != "test_segment" or manifest is None:
        raise ValueError(
            "PRIVACY_FALLBACK_POLICY_NOT_AUTHORIZED: 仅允许正式 recovery test_segment")
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    sid = str(segment.get("id") or "")
    recovery = segment.get("recovery_contract") or {}
    handoff = artifact_contract.verify_video_handoff(segment)
    manifest_handoff = (((manifest.get("handoffs") or {}).get("video") or {})
                        .get("segments") or {}).get(sid)
    recovery_file = requirement.get("contract") or {}
    if (not _rm.recovery_test_segment_requirement_is_current(manifest) or
            manifest.get("requires_test_segment") is not True or
            sid != str(requirement.get("segment_id") or "") or
            requirement.get("video_handoff_fingerprint") !=
            segment.get("video_handoff_fingerprint") or
            manifest_handoff != segment.get("video_handoff_fingerprint") or
            not handoff.get("strict") or not handoff.get("ok") or
            recovery.get("recovery_id") != requirement.get("recovery_id") or
            recovery.get("path") != recovery_file.get("path") or
            recovery.get("sha256") != recovery_file.get("sha256") or
            policy_record.get("recovery_id") != requirement.get("recovery_id") or
            policy_record.get("segment_id") != sid or
            policy_record.get("policy") != PRIVACY_FALLBACK_STOP_POLICY or
            policy_record.get("scope") != "formal_test_segment_only" or
            not _rm.file_record_is_current(policy_record.get("file") or {})):
        raise ValueError(
            "PRIVACY_FALLBACK_POLICY_NOT_AUTHORIZED: canary、handoff 或恢复合同不匹配")
    contract_file = policy_record.get("file") or {}
    try:
        with open(contract_file["path"], encoding="utf-8") as handle:
            contract = json.load(handle)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "PRIVACY_FALLBACK_POLICY_CONTRACT_INVALID: %s" % exc) from exc
    if (contract.get("kind") != "privacy_fallback_policy_contract" or
            contract.get("policy") != PRIVACY_FALLBACK_STOP_POLICY or
            contract.get("scope") != "formal_test_segment_only" or
            str(contract.get("client")) != str(manifest.get("client")) or
            str(contract.get("run_id")) != str(manifest.get("run_id")) or
            str(contract.get("segment_id")) != sid or
            contract.get("recovery_id") != requirement.get("recovery_id") or
            contract.get("source_video_handoff_fingerprint") !=
            policy_record.get("source_video_handoff_fingerprint") or
            contract.get("confirmed_by_customer") is not True or
            contract.get("confirmation_source") != "customer_dialogue" or
            not str(contract.get("customer_confirmation") or "").strip()):
        raise ValueError("PRIVACY_FALLBACK_POLICY_CONTRACT_INVALID")
    return True


def _privacy_fallback_allowed(model, video_type, ref_urls, allow_fallback=True,
                              segment=None):
    if (_privacy_rejection_stops(segment) or
            _empirical_audio_canary_requested(segment)):
        return False
    return (allow_fallback and seedance_prompt.is_seedance_model(model)
            and int(video_type or 1) in (2, 3, 5) and bool(ref_urls)
            )


def _privacy_stop_result(task, segment, provider_failure, *,
                         observation_source="provider_terminal_status"):
    provider_error = _provider_error_evidence(
        provider_failure, model=task.get("model"),
        task_id=task.get("task_id"),
        observation_source=observation_source)
    return {
        "ok": False, "videoUrl": None, "localPath": None, "absPath": None,
        "error": (
            "PRIVACY_REJECTED_STOPPED_NO_PAID_FALLBACK: provider 拒绝真人参考图；"
            "已按确认的 recovery canary 策略停止，未提交回退任务"),
        "taskId": task.get("task_id"), "segment_id": segment.get("id"),
        "video_handoff_fingerprint": task.get("handoff_fingerprint"),
        "provider_error": provider_error,
        "privacy_fallback_policy": PRIVACY_FALLBACK_STOP_POLICY,
        "privacy_fallback_attempted": False,
        "resume_available": False, "regeneration_required": True,
        "needs_confirmation": False,
        **_fallback_metadata(
            task.get("initial_model") or task.get("model"),
            task.get("model"), task.get("fallback_reason")),
    }


def _test_segment_failure_contract(result):
    """Classify a terminal canary failure without inventing a retry path."""
    privacy_stopped = (
        (result or {}).get("privacy_fallback_policy") ==
        PRIVACY_FALLBACK_STOP_POLICY)
    return {
        "error_code": ("TEST_SEGMENT_PRIVACY_STOPPED" if privacy_stopped
                       else "TEST_SEGMENT_GENERATION_FAILED"),
        "retryable": not privacy_stopped,
    }


def _validate_recovery_output_contract(
        segment, *, manifest=None, formal_stage="video",
        before_new_submission=False, allow_registered_task=False):
    """Verify that a recovery canary cannot overwrite rejected take bytes."""
    contract = (segment or {}).get("output_artifact_contract") or {}
    if not contract:
        return False
    if manifest is None or formal_stage not in {"test_segment", "video"}:
        raise ValueError("RECOVERY_CANARY_OUTPUT_NOT_AUTHORIZED")
    sid = str(segment.get("id") or "")
    if contract.get("mode") == "approved_canary_exact_media_reuse":
        proof = segment.get("audio_canary_proof") or {}
        output = os.path.abspath(str(contract.get("path") or ""))
        matching = [
            task for task in (manifest.get("tasks") or [])
            if task.get("stage") == "video" and
            str(task.get("unit_id") or "") == sid and
            task.get("task_id") == contract.get("task_id") and
            task.get("handoff_fingerprint") ==
            segment.get("video_handoff_fingerprint") and
            int(task.get("attempt") or 1) == int(
                contract.get("attempt") or 1) and
            task.get("status") == "succeeded" and
            task.get("media_qc_failed") is not True and
            task.get("promotion_proof_fingerprint") ==
            proof.get("fingerprint")]
        if (formal_stage != "video" or
                contract.get("audio_canary_proof_fingerprint") !=
                proof.get("fingerprint") or
                contract.get("take_fingerprint") !=
                proof.get("take_fingerprint") or
                contract.get("source_video_handoff_fingerprint") !=
                proof.get("source_video_handoff_fingerprint") or
                not contract.get("source_output_artifact_contract") or
                not output or output != os.path.abspath(str(
                    segment.get("out_path") or "")) or
                artifact_contract.file_sha256(output) !=
                contract.get("sha256") or len(matching) != 1):
            raise ValueError("APPROVED_CANARY_OUTPUT_REUSE_STALE")
        _rm.validate_audio_canary_promotion_segment(manifest, segment)
        _rm.require_run_output(
            manifest, output, label="approved_canary_output",
            must_exist=True)
        return True
    requirement = manifest.get("recovery_test_segment_requirement") or {}
    recovery = segment.get("recovery_contract") or {}
    manifest_handoff = (((manifest.get("handoffs") or {}).get("video") or {})
                        .get("segments") or {}).get(sid)
    raw_out_path = str(segment.get("out_path") or "").strip()
    raw_prior_path = str(contract.get("prior_take_path") or "").strip()
    out_path = os.path.abspath(raw_out_path) if raw_out_path else ""
    prior_path = os.path.abspath(raw_prior_path) if raw_prior_path else ""
    base_segment = dict(segment)
    source_output_contract = contract.get("source_output_artifact_contract") or {}
    if source_output_contract:
        base_segment["output_artifact_contract"] = source_output_contract
    else:
        base_segment.pop("output_artifact_contract", None)
    base_segment["out_path"] = contract.get("source_out_path")
    base_segment.pop("video_handoff_fingerprint", None)
    source_handoff = artifact_contract.build_video_handoff(
        base_segment)["fingerprint"]
    authorized = (manifest.get("video_attempts") or {}).get(sid) or {}
    evidence_type = contract.get("evidence_type") or "rejected_take"
    if evidence_type == "rejected_take":
        evidence_valid = bool(
            authorized.get("previous_take_fingerprint") and
            contract.get("previous_take_fingerprint") ==
            authorized.get("previous_take_fingerprint") and
            prior_path and contract.get("prior_take_sha256") ==
            artifact_contract.file_sha256(prior_path))
    elif evidence_type == "settled_provider_failure":
        evidence_valid = bool(
            not authorized.get("previous_take_fingerprint") and
            contract.get("previous_take_fingerprint") is None and
            contract.get("previous_task_id") ==
            authorized.get("previous_task_id") and
            contract.get("failure_stage") == authorized.get("failure_stage") and
            contract.get("failure_results") ==
            authorized.get("failure_results") and
            contract.get("failure_video_handoff_fingerprint") ==
            authorized.get("failure_video_handoff_fingerprint") and
            prior_path and contract.get("prior_take_sha256") ==
            artifact_contract.file_sha256(prior_path) and
            _rm.video_attempt_failure_evidence_is_current(
                manifest, sid, authorized))
    else:
        evidence_valid = False
    canary_scope_valid = bool(
        formal_stage != "test_segment" or
        (sid == str(requirement.get("segment_id") or "") and
         _rm.recovery_test_segment_requirement_is_current(manifest)))
    if (contract.get("mode") != "attempt_specific_no_overwrite" or
            contract.get("must_not_exist_before_generation") is not True or
            not canary_scope_valid or not manifest_handoff or
            manifest_handoff != segment.get("video_handoff_fingerprint") or
            not _rm.file_record_is_current(
                ((manifest.get("handoffs") or {}).get("video") or {}).get(
                    "file") or {}) or
            not _rm.file_record_is_current({
                "path": recovery.get("path"),
                "sha256": recovery.get("sha256"),
                "exists": True,
            }) or
            int(contract.get("attempt") or 0) !=
            int(authorized.get("attempt") or 0) or
            not evidence_valid or
            contract.get("recovery_id") != recovery.get("recovery_id") or
            not out_path or contract.get("path") != out_path or
            out_path == prior_path or
            contract.get("source_video_handoff_fingerprint") !=
            source_handoff or
            contract.get("target_model") != segment.get("model") or
            not artifact_contract.verify_video_handoff(segment).get("ok")):
        raise ValueError("RECOVERY_CANARY_OUTPUT_CONTRACT_INVALID")
    _rm.require_run_output(
        manifest, out_path, label="recovery_canary_output", must_exist=False)
    if before_new_submission and os.path.exists(out_path):
        registered = False
        if allow_registered_task:
            registered = any(
                isinstance(task, dict) and task.get("stage") == "video" and
                str(task.get("unit_id") or "") == sid and
                task.get("handoff_fingerprint") ==
                segment.get("video_handoff_fingerprint") and
                int(task.get("attempt") or 0) ==
                int(contract.get("attempt") or 0) and
                task.get("status") in {
                    "submitted", "running", "succeeded",
                    "fallback_approval_required"}
                for task in (manifest.get("tasks") or []))
        if not registered:
            raise ValueError(
                "RECOVERY_CANARY_OUTPUT_ALREADY_EXISTS: %s" % out_path)
    return True


def _validate_recovery_attempt_authorization(segment, manifest):
    """Require a new authorization/output identity after any failed paid take."""
    recovery = (segment or {}).get("recovery_contract") or {}
    if not recovery or manifest is None:
        return True
    sid = str(segment.get("id") or "")
    handoff = segment.get("video_handoff_fingerprint")
    tasks = [
        task for task in (manifest.get("tasks") or [])
        if isinstance(task, dict) and task.get("stage") == "video" and
        str(task.get("unit_id") or "") == sid and task.get("task_id")]
    current_usable = any(
        task.get("handoff_fingerprint") == handoff and
        task.get("status") in {"submitted", "running", "succeeded"} and
        task.get("media_qc_failed") is not True
        for task in tasks)
    if current_usable:
        return True
    failed_paid = [
        task for task in tasks
        if task.get("status") in {
            "failed", "fallback_approval_required"} or
        (task.get("status") == "succeeded" and
         task.get("media_qc_failed") is True)]
    if not failed_paid:
        return True
    maximum = max(int(task.get("attempt") or 1) for task in failed_paid)
    authorized = (manifest.get("video_attempts") or {}).get(sid) or {}
    output = segment.get("output_artifact_contract") or {}
    take_authorized = bool(authorized.get("previous_take_fingerprint"))
    failure_authorized = bool(
        not take_authorized and
        _rm.video_attempt_failure_evidence_is_current(
            manifest, sid, authorized))
    output_matches_evidence = bool(
        (take_authorized and
         (output.get("evidence_type") or "rejected_take") == "rejected_take" and
         output.get("previous_take_fingerprint") ==
         authorized.get("previous_take_fingerprint")) or
        (failure_authorized and
         output.get("evidence_type") == "settled_provider_failure" and
         output.get("previous_task_id") == authorized.get("previous_task_id") and
         output.get("failure_results") == authorized.get("failure_results") and
         output.get("failure_video_handoff_fingerprint") ==
         authorized.get("failure_video_handoff_fingerprint")))
    if (int(authorized.get("attempt") or 0) <= maximum or
            not (take_authorized or failure_authorized) or
            int(output.get("attempt") or 0) !=
            int(authorized.get("attempt") or 0) or
            not output_matches_evidence):
        raise ValueError(
            "RECOVERY_ATTEMPT_AUTHORIZATION_REQUIRED: %s" % sid)
    return True


def _privacy_fallback_recovery_action(segment, target_model=None, *, task_exists=True):
    target = target_model or "实时目录中满足容量的音画一体视频路由"
    storyboard_video = bool(
        segment.get("storyboard_ref") and
        segment.get("storyboard_usage") != "design_board")
    if storyboard_video:
        preparation = (
            "先完成当前故事板单格展开图 QA/客户确认，再选择 %s，重新 capture-video "
            "并由客户确认完整提示词与引用包" % target)
    else:
        preparation = (
            "本段是 identity plate/design-board 底片，不需要单格展开图；请重新选择能"
            "承载身份图与尾帧容量的 %s，重编译链合同，并重新 capture-video/客户确认" %
            target)
    return preparation + (
        "；原 task 与未知计费证据已保留，不会自动重提。" if task_exists else
        "；确认新的付费 attempt 后才可提交。")


def _prepare_privacy_fallback_submission(api_key, task, segment, *, draft):
    """Prepare every Kling fallback input before superseding the paid task.

    This deliberately performs panel-approval and exact-prompt checks first.
    A missing customer approval therefore pauses the run without changing the
    old task into a fictitious superseded state or submitting another charge.
    """
    dialogue = _segment_integrated_audio_requirement(segment)
    target_model = _pick_privacy_fallback_model(
        task["video_type"], dialogue, formal=not draft,
        reference_count=len(task.get("ref_urls") or []))
    prepared_segment = segment
    source_segment = task.get("source_segment") or segment
    prepared_urls = list(task.get("ref_urls") or [])
    panel = task.get("storyboard_panel")
    if task.get("storyboard_ref"):
        prepared_segment, raw_urls, panel = _prepare_storyboard_submission_for_model(
            api_key, source_segment, target_model, formal=not draft)
        prepared_urls = [_video_image_ref(
            url, api_key=api_key, allow_local_draft=draft)
            for url in (raw_urls or [])]
    submission_text = _submission_text(
        prepared_segment, target_model, task.get("storyboard_ref"))
    return {
        "model": target_model,
        "segment": prepared_segment,
        "source_segment": source_segment,
        "ref_urls": prepared_urls,
        "storyboard_panel": panel,
        "submission_text": submission_text,
    }


def _compile_seedance_text(segment, model):
    """Backward-compatible prompt compiler used by prompt_review."""
    if not segment.get("seedance_native", True):
        return segment.get("text", "")
    if not (seedance_prompt.is_seedance_model(model) or _is_kling_video_model(model)):
        return segment.get("text", "")
    if not any(segment.get(k) for k in ("timeline", "action", "visual", "camera", "camera_movement")):
        return segment.get("text", "")
    prompt_model = ("kling-v3-omni-video" if _is_kling_video_model(model)
                    else model)
    prompt_segment = dict(segment)
    prompt_segment["storyboard_ref"] = False
    prompt_segment.pop("storyboard_ref_mode", None)
    return seedance_prompt.compile_prompt(
        prompt_segment, segment.get("references") or segment.get("reference_roles") or [],
        style=segment.get("style"), negative=segment.get("negative_prompt"),
        target_model=prompt_model)


def _expanded_storyboard_reference(api_key, segment):
    """Create the high-resolution panel plate for one storyboard video unit."""
    import storyboard
    out_dir = segment.get("storyboard_dir") or os.path.dirname(
        segment.get("storyboard_path") or "")
    if not out_dir:
        raise ValueError("STORYBOARD_PANEL_OUTPUT_DIR_MISSING: 镜头 %s 缺少故事板目录" %
                         segment.get("id"))
    expanded = storyboard.expand_storyboard_panel(api_key, segment, out_dir=out_dir)
    path = expanded.get("abspath") or expanded.get("path")
    if not path or not os.path.isfile(path):
        raise ValueError("STORYBOARD_PANEL_EXPANSION_FAILED: 镜头 %s 未生成单格展开图" %
                         segment.get("id"))
    return expanded


def _prepare_storyboard_panel_submission(api_key, segment, *, formal=False):
    """Return a request segment plus its panel image and explicitly bound refs.

    This is the sole adapter between an approved contact sheet and every video
    entry point.  A contact sheet must never reach createVideo directly.
    """
    enabled = bool(segment.get("storyboard_ref") or segment.get("storyboard_ref_mode") or
                   segment.get("use_storyboard_reference"))
    if not enabled:
        return dict(segment), list(segment.get("urls") or [])
    required = ("storyboard_path", "storyboard_dir", "storyboard_panel_index")
    missing = [key for key in required if not segment.get(key)]
    if missing:
        raise ValueError("STORYBOARD_PANEL_BINDING_REQUIRED: 镜头 %s 缺少 %s" %
                         (segment.get("id") or "unknown", ", ".join(missing)))
    tags = segment.get("ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tagged = {ref.get("tag"): ref for ref in (segment.get("references") or [])
              if isinstance(ref, dict) and ref.get("tag")}
    unknown = [tag for tag in tags if tag not in tagged]
    if unknown:
        raise ValueError("STORYBOARD_PANEL_REFS_MISSING: 镜头 %s 的 ref_tags 无对应素材: %s" %
                         (segment.get("id") or "unknown", ", ".join(unknown)))
    if not tags:
        raise ValueError("STORYBOARD_PANEL_REFS_MISSING: 镜头 %s 未声明 ref_tags" %
                         (segment.get("id") or "unknown"))
    declared_panel = segment.get("storyboard_panel") or {}
    if formal:
        approval = segment.get("storyboard_panel_approval") or {}
        quality = segment.get("panel_quality") or {}
        quality_checks = quality.get("checks") or {}
        panel_path = declared_panel.get("path")
        if not (approval.get("status") == "confirmed" and quality.get("pass") and
                panel_path and os.path.isfile(panel_path) and
                all(quality_checks.get(name) is True
                    for name in panel_expansion.PANEL_QA_CHECKS)):
            raise ValueError("PANEL_EXPANSION_APPROVAL_REQUIRED: 镜头 %s 的 Kling 单格展开图必须先完成 QA 与客户确认" %
                             (segment.get("id") or "unknown"))
        if artifact_contract.file_sha256(panel_path) != declared_panel.get("sha256"):
            raise ValueError("STALE_STORYBOARD_PANEL: 镜头 %s 的展开图字节已变化" %
                             segment.get("id"))
        expected_recipe_sha = artifact_contract.sha256_json(
            artifact_contract.build_storyboard_panel_recipe(segment))
        expected_qa_sha = artifact_contract.sha256_json(quality)
        expected_qa_method = quality.get("method") or "automated_vision_qa"
        if (approval.get("confirmed_by_customer") is not True or
                not str(approval.get("customer_confirmation") or "").strip() or
                approval.get("confirmation_source") != "customer_dialogue" or
                not approval.get("at") or
                approval.get("panel_sha256") != declared_panel.get("sha256") or
                approval.get("recipe_sha256") != expected_recipe_sha or
                declared_panel.get("recipe_sha256") != expected_recipe_sha or
                approval.get("qa_method") != expected_qa_method or
                approval.get("qa_fingerprint") != expected_qa_sha):
            raise ValueError(
                "PANEL_EXPANSION_APPROVAL_REQUIRED: 镜头 %s 的客户确认、recipe 或 QA 证据无效" %
                (segment.get("id") or "unknown"))
    if declared_panel.get("path") and os.path.isfile(declared_panel["path"]):
        expanded = {"abspath": declared_panel["path"],
                    "url": declared_panel.get("url") or declared_panel.get("result_url"),
                    "recipe_sha256": declared_panel.get("recipe_sha256")}
    else:
        expanded = _expanded_storyboard_reference(api_key, segment)
    input_tags, omitted = _storyboard_budgeted_tags(segment)
    expanded_url = expanded.get("url") or expanded.get("result_url")
    urls = [expanded_url or expanded["abspath"]] + [tagged[tag]["url"] for tag in input_tags]
    urls = list(dict.fromkeys(urls))
    if len(urls) > 4:
        # Do not silently drop a confirmed identity/product binding merely to
        # satisfy a provider limit. The author must simplify the shot first.
        raise ValueError("REFERENCE_COUNT_UNSUPPORTED: 镜头 %s 的单格展开图加 ref_tags 共 %d 张，超过网关上限 4。"
                         % (segment.get("id") or "unknown", len(urls)))
    panel_record = {
        "path": expanded["abspath"], "sha256": artifact_contract.file_sha256(expanded["abspath"]),
        "url": expanded_url,
        "recipe_sha256": expanded.get("recipe_sha256"),
        "panel_index": int(segment["storyboard_panel_index"]), "ref_tags": list(tags),
    }
    expected_recipe = artifact_contract.build_storyboard_panel_recipe(segment)
    expected_recipe_sha = artifact_contract.sha256_json(expected_recipe)
    if panel_record["recipe_sha256"] != expected_recipe_sha:
        raise ValueError("STALE_STORYBOARD_PANEL: 镜头 %s 的展开图 recipe 与正式交接不一致" %
                         segment.get("id"))
    prepared = dict(segment, storyboard_ref_mode="expanded_panel",
                    storyboard_panel=panel_record,
                    storyboard_panel_path=expanded["abspath"], ref_tags=list(tags),
                    image_input_ref_tags=list(input_tags),
                    omitted_ref_tags=list(omitted))
    return prepared, urls


def _storyboard_tagged_references(segment):
    tags = segment.get("ref_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tagged = {ref.get("tag"): ref for ref in (segment.get("references") or [])
              if isinstance(ref, dict) and ref.get("tag")}
    unknown = [tag for tag in tags if tag not in tagged]
    if unknown:
        raise ValueError("STORYBOARD_PANEL_REFS_MISSING: 镜头 %s 的 ref_tags 无对应素材: %s" %
                         (segment.get("id") or "unknown", ", ".join(unknown)))
    if not tags:
        raise ValueError("STORYBOARD_PANEL_REFS_MISSING: 镜头 %s 未声明 ref_tags" %
                         (segment.get("id") or "unknown"))
    return tags, tagged


def _storyboard_budgeted_tags(segment):
    budget = artifact_contract.storyboard_image_input_tags(segment)
    return budget["selected"], budget["omitted"]


def _prepare_native_storyboard_submission(segment):
    """Seedance native path: pass the approved storyboard plus exact refs."""
    required = ("storyboard_path", "storyboard_dir", "storyboard_panel_index")
    missing = [key for key in required if not segment.get(key)]
    if missing:
        raise ValueError("STORYBOARD_NATIVE_BINDING_REQUIRED: 镜头 %s 缺少 %s" %
                         (segment.get("id") or "unknown", ", ".join(missing)))
    sheet = segment.get("storyboard_path")
    if not sheet or not os.path.isfile(sheet):
        raise ValueError("STORYBOARD_NATIVE_SOURCE_MISSING: %s" % sheet)
    sheet_url = segment.get("storyboard_url")
    if not sheet_url:
        raise ValueError("STORYBOARD_NATIVE_URL_MISSING: 镜头 %s 缺少 BasicRouter 图片 retrieve URL" %
                         (segment.get("id") or "unknown"))
    tags, tagged = _storyboard_tagged_references(segment)
    input_tags, omitted = _storyboard_budgeted_tags(segment)
    urls = [sheet_url] + [tagged[tag]["url"] for tag in input_tags]
    urls = list(dict.fromkeys(urls))
    if len(urls) > 4:
        raise ValueError("REFERENCE_COUNT_UNSUPPORTED: 镜头 %s 的原生故事板加 ref_tags 共 %d 张，超过网关上限 4。"
                         % (segment.get("id") or "unknown", len(urls)))
    prepared = dict(segment, storyboard_ref_mode="native_storyboard",
                    storyboard_panel=None, storyboard_panel_path=None,
                    ref_tags=list(tags), image_input_ref_tags=list(input_tags),
                    omitted_ref_tags=list(omitted))
    return prepared, urls


def _prepare_storyboard_submission_for_model(api_key, segment, model, *, formal=False):
    enabled = bool(segment.get("storyboard_ref") or segment.get("storyboard_ref_mode") or
                   segment.get("use_storyboard_reference"))
    if not enabled:
        return dict(segment), list(segment.get("urls") or []), None
    if _uses_native_storyboard_prompt(segment, model):
        prepared, urls = _prepare_native_storyboard_submission(segment)
        return prepared, urls, None
    prepared, urls = _prepare_storyboard_panel_submission(api_key, segment, formal=formal)
    return prepared, urls, prepared.get("storyboard_panel")


def _preflight_request_fingerprint(rows, preferred_model, formal):
    """Bind the capability decision, capacity budget and exact source packet."""
    return artifact_contract.sha256_json({
        "contract_version": VIDEO_PREFLIGHT_CONTRACT_VERSION,
        "preferred_model": preferred_model or DEFAULT_MODEL,
        "formal": bool(formal),
        "segments": list(rows or []),
    })


def _resolved_model_audio_evidence(model, catalog, proven_audio_models=()):
    selected_id = _resolve_catalog_model(model, catalog)
    selected_record = ((catalog.get("records") or {}).get(selected_id)
                       if selected_id else None)
    integrated_audio = (
        selected_record.get("integrated_audio") if selected_record else
        _model_integrated_audio(model))
    if integrated_audio is None and selected_record:
        integrated_audio = _model_integrated_audio(selected_id, selected_record)
    proven_names = {str(item).strip().lower()
                    for item in (proven_audio_models or ())}
    selected_names = (
        _catalog_record_route_names(selected_id, selected_record, catalog)
        if selected_id and selected_record else
        {str(model or "").strip().lower()})
    canary_proven = bool(selected_names.intersection(proven_names))
    canary_promoted = bool(integrated_audio is None and canary_proven)
    if canary_promoted:
        integrated_audio = True
    catalog_evidence = (
        dict(selected_record.get("audio_evidence") or {})
        if selected_record else {})
    exact_route_evidence = None
    if selected_record:
        for route_name in [selected_id] + list(
                selected_record.get("aliases") or []) + list(
                selected_record.get("submission_names") or []):
            if str(route_name) in VIDEO_MODEL_INTEGRATED_AUDIO_EVIDENCE:
                exact_route_evidence = dict(
                    VIDEO_MODEL_INTEGRATED_AUDIO_EVIDENCE[str(route_name)])
                exact_route_evidence["route_name"] = str(route_name)
                break
    if (integrated_audio is not None and exact_route_evidence and
            catalog_evidence.get("value") is None):
        evidence = {
            "value": integrated_audio,
            "source": "trusted_offline_exact_route_table",
            "exact_route_evidence": exact_route_evidence,
            "catalog_evidence": catalog_evidence,
            "conflict": False,
        }
    else:
        evidence = catalog_evidence if selected_record else {
            "value": integrated_audio,
            "source": "trusted_offline_capability_table",
            "conflict": False,
        }
    evidence["resolved_value"] = integrated_audio
    if canary_promoted:
        evidence = {
            "value": True, "resolved_value": True,
            "source": "approved_test_segment_media_and_take_review",
            "catalog_evidence": evidence, "conflict": False,
        }
    return integrated_audio, evidence


def _resolved_model_route_evidence(model, catalog):
    """Bind one selected submission name to its unique provider route."""
    selected_id = _resolve_catalog_model(model, catalog)
    record = ((catalog.get("records") or {}).get(selected_id)
              if selected_id else None)
    if record:
        return {
            "provider_route_key": selected_id,
            "provider_model_id": record.get("provider_model_id"),
            "provider_submission_name": record.get("submission_name"),
            "route_identity_source": record.get("route_identity_source"),
            "submission_identity_source": record.get(
                "submission_identity_source"),
            "route_identity_conflict": bool(
                record.get("route_identity_conflict")),
            "resolution_source": "live_catalog_unique_alias",
        }
    return {
        "provider_route_key": str(model or ""),
        "provider_model_id": str(model or ""),
        "provider_submission_name": str(model or ""),
        "route_identity_source": "trusted_offline_route",
        "submission_identity_source": "trusted_offline_route",
        "route_identity_conflict": False,
        "resolution_source": "trusted_offline_capability_table",
    }


def _model_request_capability_snapshot(model, segment, catalog):
    """Return normalized request/route evidence without discarding failures."""
    selected_id = _resolve_catalog_model(model, catalog)
    record = ((catalog.get("records") or {}).get(selected_id)
              if selected_id else None)
    requested_resolution = str(
        (segment or {}).get("resolution") or "1080p").strip().lower()
    resolutions = sorted({
        str(item).strip().lower()
        for item in ((record or {}).get("resolutions") or ())
        if str(item).strip()
    })
    inputs = sorted({
        str(item).strip().lower()
        for item in ((record or {}).get("multimodel_types") or ())
        if str(item).strip()
    })
    outputs = sorted({
        str(item).strip().lower()
        for item in ((record or {}).get("output_multi_type") or ())
        if str(item).strip()
    })
    return {
        "requested_resolution": requested_resolution,
        "advertised_resolutions": resolutions,
        "model_input_modalities": inputs,
        "model_output_modalities": outputs,
    }


def _model_request_capability_evidence(model, segment, catalog):
    """Validate one exact paid request against its selected live route.

    Empty advertised sets mean the gateway did not declare that dimension; in
    that case existing type/audio/reference gates remain authoritative.  A
    non-empty declaration is a hard capability contract and must fail closed
    before ``createVideo``.
    """
    evidence = _model_request_capability_snapshot(model, segment, catalog)
    requested_resolution = evidence["requested_resolution"]
    resolutions = evidence["advertised_resolutions"]
    inputs = evidence["model_input_modalities"]
    outputs = evidence["model_output_modalities"]
    if resolutions and requested_resolution not in resolutions:
        raise ValueError(
            "MODEL_RESOLUTION_UNSUPPORTED: %s requests %s; route advertises %s" %
            (model, requested_resolution, ",".join(resolutions)))
    video_type = int((segment or {}).get("video_type") or 1)
    if video_type in (2, 3, 4, 5) and inputs and not any(
            "image" in modality for modality in inputs):
        raise ValueError(
            "MODEL_INPUT_MODALITY_UNSUPPORTED: %s type=%s requires image input; "
            "route advertises %s" % (model, video_type, ",".join(inputs)))
    if outputs and not any("video" in modality for modality in outputs):
        raise ValueError(
            "MODEL_OUTPUT_MODALITY_UNSUPPORTED: %s route advertises %s" %
            (model, ",".join(outputs)))
    return evidence


def _inspect_model_contract(segment, sid, model, catalog,
                            proven_audio_models, blockers, *, enforce=True):
    integrated_audio, audio_evidence = _resolved_model_audio_evidence(
        model, catalog, proven_audio_models)
    route_evidence = _resolved_model_route_evidence(model, catalog)
    evidence = _model_request_capability_snapshot(model, segment, catalog)
    current = True
    try:
        evidence = _model_request_capability_evidence(model, segment, catalog)
    except ValueError as exc:
        current = False
        evidence["capability_drift_error"] = str(exc)
        if enforce:
            blockers.append({
                "segment_id": sid, "code": str(exc).split(":", 1)[0],
                "message": str(exc),
                "action": (
                    "选择声明支持当前分辨率与输入/输出模态的唯一在线路由，"
                    "更新 segments 后重新 capture-video 并由客户确认。"),
            })
    return (integrated_audio, audio_evidence, route_evidence, evidence,
            current, current or not enforce)


def _inspect_recovery_contracts(segment, sid, manifest, formal_stage, blockers,
                                *, catalog=None, existing_paid_task=None):
    recovery_ready = True
    try:
        _validate_recovery_attempt_authorization(segment, manifest)
    except ValueError as exc:
        recovery_ready = False
        blockers.append({
            "segment_id": sid, "code": str(exc).split(":", 1)[0],
            "message": str(exc),
            "action": ("为该段登记 rejected review、授权下一 attempt，并原子绑定"
                       "独立输出路径后重新 capture-video。"),
        })
    output_ready = True
    if segment.get("output_artifact_contract"):
        try:
            _validate_recovery_output_contract(
                segment, manifest=manifest, formal_stage=formal_stage,
                before_new_submission=(formal_stage == "test_segment"),
                allow_registered_task=True)
        except (ValueError, OSError) as exc:
            output_ready = False
            blockers.append({
                "segment_id": sid, "code": str(exc).split(":", 1)[0],
                "message": str(exc),
                "action": ("重新原子绑定 recovery canary 的 attempt 专用输出路径；"
                           "不得覆盖 rejected take 字节。"),
            })
    privacy_policy = segment.get("privacy_fallback_policy")
    privacy_stop, privacy_ready = False, True
    if privacy_policy:
        try:
            privacy_stop = _validate_privacy_stop_policy(
                segment, manifest=manifest, formal_stage=formal_stage)
        except (ValueError, br_client.BRError) as exc:
            privacy_ready = False
            blockers.append({
                "segment_id": sid, "code": str(exc).split(":", 1)[0],
                "message": str(exc),
                "action": ("移除未授权策略，或重新绑定当前 recovery canary、handoff "
                           "与客户确认的策略合同后再 capture-video。"),
            })
    audio_policy = segment.get("audio_capability_policy")
    audio_policy_ready = True
    if audio_policy:
        try:
            _validate_empirical_audio_canary_policy(
                segment, manifest=manifest, formal_stage=formal_stage,
                model_catalog=catalog,
                validate_route=not bool(existing_paid_task))
        except (ValueError, br_client.BRError) as exc:
            audio_policy_ready = False
            blockers.append({
                "segment_id": sid, "code": str(exc).split(":", 1)[0],
                "message": str(exc),
                "action": ("只允许当前 recovery canary 使用客户确认的精确未知"
                           "输出音轨路由；修复合同后重新 capture-video。"),
            })
    return (privacy_policy, recovery_ready, output_ready, privacy_stop,
            privacy_ready, audio_policy, audio_policy_ready)


def _eligible_model_fallbacks(segment, video_type, audio_requirement,
                              reference_count, formal, catalog):
    eligible = []
    candidate_segment = dict(segment, video_type=video_type)
    for requested in VIDEO_MODEL_FALLBACK:
        try:
            candidate = _pick_video_model(
                requested, video_type=video_type, dialogue=audio_requirement,
                formal=formal, allow_fallback=False,
                reference_count=reference_count, catalog=catalog)
            _model_request_capability_evidence(
                candidate, candidate_segment, catalog)
        except (ValueError, br_client.BRError):
            continue
        if candidate not in eligible:
            eligible.append(candidate)
    return eligible


def _existing_paid_task_for_segment(manifest, segment, model):
    if manifest is None:
        return None
    handoff = (segment.get("video_handoff_fingerprint") or
               artifact_contract.build_video_handoff(segment)["fingerprint"])
    if model is None:
        attempt = _current_attempt(manifest, segment.get("id"))
        return next((task for task in reversed(manifest.get("tasks") or [])
                     if task.get("stage") == "video"
                     and task.get("unit_id") == segment.get("id")
                     and task.get("handoff_fingerprint") == handoff
                     and int(task.get("attempt") or 1) == attempt
                     and task.get("task_id")
                     and (task.get("status") in (
                         "submitted", "running", "timed_out",
                         "fallback_approval_required") or
                         (task.get("status") == "succeeded" and
                          task.get("media_qc_failed") is not True))), None)
    return (
        _completed_task(manifest, segment.get("id"), handoff, model=model) or
        _rm.find_resumable_task(
            manifest, "video", segment.get("id"), handoff, model=model))


def _select_preflight_model(segment, preferred_model, video_type,
                            audio_requirement, reference_count, *, formal,
                            allow_model_fallback, catalog,
                            allow_unproven_audio_canary, proven_audio_models,
                            manifest, formal_stage):
    existing = _existing_paid_task_for_segment(manifest, segment, None)
    if existing:
        return (existing.get("model") or segment.get("model") or
                preferred_model or DEFAULT_MODEL), existing
    if segment.get("audio_capability_policy"):
        return (_validate_empirical_audio_canary_policy(
            segment, manifest=manifest, formal_stage=formal_stage,
            model_catalog=catalog), None)
    selected = _pick_video_model(
        segment.get("model") or preferred_model or DEFAULT_MODEL,
        video_type=video_type, dialogue=audio_requirement, formal=formal,
        allow_fallback=allow_model_fallback, reference_count=reference_count,
        # Unknown embedded audio is never admitted by a generic flag.  The
        # only formal exception is the contract-bound policy branch above.
        catalog=catalog, allow_unproven_audio=False,
        proven_audio_models=proven_audio_models)
    return selected, None


def _finalize_submission_preflight(rows, blockers, preferred_model, formal):
    contract = {
        "contract_version": VIDEO_PREFLIGHT_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preferred_model": preferred_model or DEFAULT_MODEL,
        "formal": bool(formal),
        "segments": rows,
        "blockers": blockers,
    }
    contract["request_fingerprint"] = _preflight_request_fingerprint(
        rows, contract["preferred_model"], contract["formal"])
    contract["ok"] = not blockers
    contract["ready"] = not blockers
    return contract


def inspect_submission_requirements(segments, preferred_model=None, *, formal=True,
                                    allow_model_fallback=True, catalog=None,
                                    allow_unproven_audio_canary=None,
                                    proven_audio_models=(), manifest=None,
                                    formal_stage="video"):
    """Resolve model/ref requirements without submitting any paid video task.

    In particular, a currently selected Kling/non-native storyboard path must
    already contain a QA-passed, customer-confirmed expanded panel.  Reporting
    this before batch submission prevents earlier segments from being charged
    before a later segment discovers the missing approval.
    """
    segments = list(segments or [])
    if allow_unproven_audio_canary is None:
        allow_unproven_audio_canary = False
    rows = []
    blockers = []
    declared_chain = any(
        segment.get("chain_required") or segment.get("avatar_plate_chain") or
        segment.get("extend_video") or segment.get("extend_from_previous")
        for segment in segments[1:])
    # ``catalog={}`` is an intentional offline snapshot in tests and draft
    # tooling.  Only ``None`` authorizes a live lookup; truthiness would turn an
    # explicitly empty snapshot into a slow, nondeterministic network request.
    privacy_catalog = (
        _model_catalog("video") if formal and catalog is None else
        catalog if catalog is not None else {"records": {}, "aliases": {}})
    for index, segment in enumerate(segments):
        sid = str(segment.get("id") or "unknown")
        urls = list(segment.get("urls") or [])
        product_sku = segment.get("product_sku")
        if formal and product_sku and not urls:
            blockers.append({
                "segment_id": sid,
                "code": "PRODUCT_REFERENCES_MUST_BE_COMPILED_BEFORE_HANDOFF",
                "message": "产品 SKU 参考图必须在 split/handoff 前展开进 segments.json",
            })
        video_type = int(segment.get("video_type") or (5 if len(urls) > 1 else 2 if urls else 1))
        audio_requirement = _segment_integrated_audio_requirement(segment)
        storyboard_ref = bool(
            segment.get("storyboard_ref") or segment.get("storyboard_ref_mode") or
            segment.get("use_storyboard_reference"))
        reference_count = len(urls)
        if storyboard_ref:
            try:
                tags, _ = _storyboard_tagged_references(segment)
                selected, _omitted = _storyboard_budgeted_tags(segment)
                reference_count = 1 + len(selected)
            except (ValueError, br_client.BRError) as exc:
                blockers.append({"segment_id": sid, "code": str(exc).split(":", 1)[0],
                                 "message": str(exc)})
                tags = []
        else:
            tags = []
        try:
            model, existing_paid_task = _select_preflight_model(
                segment, preferred_model, video_type, audio_requirement,
                reference_count, formal=formal,
                allow_model_fallback=allow_model_fallback,
                catalog=privacy_catalog,
                allow_unproven_audio_canary=allow_unproven_audio_canary,
                proven_audio_models=proven_audio_models, manifest=manifest,
                formal_stage=formal_stage)
        except (ValueError, br_client.BRError) as exc:
            blockers.append({
                "segment_id": sid, "code": str(exc).split(":", 1)[0],
                "message": str(exc)})
            rows.append({
                "segment_id": sid, "model": None, "video_type": video_type,
                "storyboard_ref": storyboard_ref, "ready": False})
            continue
        (integrated_audio, audio_evidence, route_evidence,
         model_capability_evidence, model_contract_current,
         model_contract_ready) = \
            _inspect_model_contract(
                segment, sid, model, privacy_catalog,
                proven_audio_models, blockers,
                enforce=not bool(existing_paid_task))
        native = bool(storyboard_ref and _uses_native_storyboard_prompt(segment, model))
        requires_panel = bool(storyboard_ref and not native)
        panel_ready = not requires_panel
        (privacy_policy, recovery_attempt_ready, output_contract_ready,
         privacy_stop_authorized, privacy_policy_ready, audio_policy,
         audio_policy_ready) = \
            _inspect_recovery_contracts(
                segment, sid, manifest, formal_stage, blockers,
                catalog=privacy_catalog, existing_paid_task=existing_paid_task)
        if storyboard_ref:
            try:
                if native:
                    _prepare_native_storyboard_submission(segment)
                else:
                    _prepare_storyboard_panel_submission(None, segment, formal=True)
                    panel_ready = True
            except (ValueError, br_client.BRError) as exc:
                blockers.append({
                    "segment_id": sid,
                    "code": str(exc).split(":", 1)[0],
                    "message": str(exc),
                    "action": (
                        "运行 panel_expansion.py 生成/QA，展示 preview 后用客户原话确认；"
                        "再重新 capture-video" if requires_panel else
                        "修复 storyboard/ref_tags 绑定后重新 split"),
                })
        adds_tail = bool(
            declared_chain and _chain_tail_reference_required(segment, index))
        privacy_reference_count = reference_count + (1 if adds_tail else 0)
        privacy_video_type = 5 if adds_tail else video_type
        privacy_fallback_model = None
        privacy_fallback_ready = True
        if (formal and not existing_paid_task and not audio_policy and
                not privacy_stop_authorized and
                seedance_prompt.is_seedance_model(model) and
                privacy_reference_count > 0):
            try:
                privacy_fallback_model = _pick_privacy_fallback_model(
                    privacy_video_type,
                    audio_requirement,
                    formal=True, reference_count=privacy_reference_count,
                    catalog=privacy_catalog)
                _model_request_capability_evidence(
                    privacy_fallback_model,
                    dict(segment, video_type=privacy_video_type),
                    privacy_catalog)
            except (ValueError, br_client.BRError) as exc:
                privacy_fallback_ready = False
                blockers.append({
                    "segment_id": sid,
                    "code": str(exc).split(":", 1)[0],
                    "message": str(exc),
                    "action": (
                        "重新选择能承载当前身份/背景引用与前序尾帧的在线视频路由，"
                        "或重新编译为可执行的非链合同；随后重新 capture-video 并由客户确认。"),
                })
        eligible_fallback_models = ([] if audio_policy else
            _eligible_model_fallbacks(
                segment, privacy_video_type, audio_requirement,
                privacy_reference_count, formal, privacy_catalog))
        if (privacy_fallback_model and
                privacy_fallback_model not in eligible_fallback_models):
            eligible_fallback_models.append(privacy_fallback_model)
        rows.append({
            "segment_id": sid, "model": model, "video_type": video_type,
            "provider_route_key": route_evidence["provider_route_key"],
            "provider_model_id": route_evidence["provider_model_id"],
            "provider_submission_name": route_evidence[
                "provider_submission_name"],
            "model_route_evidence": route_evidence,
            **model_capability_evidence,
            "model_request_contract_ready": model_contract_ready,
            "model_request_contract_current": model_contract_current,
            "existing_paid_task_recovery": bool(existing_paid_task),
            "audio_required": bool(audio_requirement),
            "audio_canary_required": bool(
                audio_requirement and (
                    integrated_audio is None or bool(audio_policy))),
            "integrated_audio": integrated_audio,
            "integrated_audio_evidence": audio_evidence,
            "submission_source_fingerprint":
                _submission_source_fingerprint(segment),
            "storyboard_ref": storyboard_ref,
            "storyboard_ref_mode": (
                "native_storyboard" if native else "expanded_panel" if requires_panel else None),
            "ref_tags": list(tags), "reference_count": reference_count,
            "requires_panel_expansion": requires_panel,
            "panel_ready": panel_ready,
            "chain_tail_reference_reserved": adds_tail,
            "effective_reference_count": privacy_reference_count,
            "privacy_fallback_model": privacy_fallback_model,
            "privacy_fallback_video_type": privacy_video_type,
            "privacy_fallback_reference_count": privacy_reference_count,
            "privacy_fallback_ready": privacy_fallback_ready,
            "privacy_fallback_policy": privacy_policy,
            "privacy_fallback_policy_ready": privacy_policy_ready,
            "privacy_fallback_action": (
                PRIVACY_FALLBACK_STOP_POLICY if privacy_stop_authorized else
                "use_capability_checked_fallback"),
            "audio_capability_policy": audio_policy,
            "audio_capability_policy_contract": (
                segment.get("audio_capability_policy_contract") or {}),
            "audio_capability_policy_ready": audio_policy_ready,
            "audio_capability_action": (
                "submit_exact_single_canary_then_require_non_silent_media_audio_qc"
                if audio_policy else "require_proven_integrated_audio"),
            "output_artifact_contract": (
                segment.get("output_artifact_contract") or {}),
            "output_artifact_contract_ready": output_contract_ready,
            "recovery_attempt_authorization_ready": recovery_attempt_ready,
            "eligible_fallback_models": eligible_fallback_models,
            "ready": ((not requires_panel or panel_ready) and
                      privacy_fallback_ready and privacy_policy_ready and
                      audio_policy_ready and
                      output_contract_ready and recovery_attempt_ready and
                      model_contract_ready),
        })
    return _finalize_submission_preflight(
        rows, blockers, preferred_model, formal)


def _submit_video(api_key, segment, model, video_type, ref_urls, negative_prompt,
                   *, seed=None, storyboard_ref=False, extend_url=None,
                   request_id=None, submission_text=None):
    if request_id is None:
        handoff = (segment.get("video_handoff_fingerprint") or
                   artifact_contract.build_video_handoff(segment)["fingerprint"])
        request_id = _submission_request_id(segment, model, video_type, handoff)
    text = submission_text if submission_text is not None else _submission_text(
        segment, model, storyboard_ref, extend_url)
    audio = segment.get("audio_contract") or {}
    language_contract.ensure_dialogue_language(
        audio.get("dialogue") or segment.get("dialogue"),
        audio.get("language") or segment.get("language"),
        context="paid video segment %s" % (segment.get("id") or "unknown"))
    task_id = br_client.create_video(
        api_key, text, model=model, video_type=video_type, urls=ref_urls,
        resolution=segment.get("resolution", "1080p"), ratio=segment.get("ratio", "9:16"),
        duration=segment.get("duration", 5), negative_prompt=negative_prompt,
        seed=seed, extend_video_url=extend_url, request_id=request_id)
    return task_id, text




def _mark_submission_failed(manifest, manifest_path, segment, handoff_fingerprint,
                            attempt, model, request_id, error):
    if manifest is None or not segment or not handoff_fingerprint or not attempt:
        return
    fields = {"stage": "video", "unit_id": segment.get("id"),
              "handoff_fingerprint": handoff_fingerprint, "attempt": attempt,
              "model": model, "request_id": request_id, "status": "failed",
              "error": str(error), "failure_phase": "submit",
              # createVideo transport failures can be ambiguous: the provider
              # may have accepted the idempotent request before the response
              # was lost.  A retry must reuse this attempt/request identity,
              # never manufacture a new paid attempt automatically.
              "retry_same_request_id": True}
    if segment.get("storyboard_panel"):
        fields["storyboard_panel"] = segment["storyboard_panel"]
    _rm.upsert_task(manifest, fields)
    if manifest_path:
        _rm.save_manifest(manifest, manifest_path)


def _refresh_completed_video_url(api_key, task_id, cached_url=None):
    """Refresh an expiring signed URL from the existing paid task."""
    try:
        info = br_client.get_video(api_key, task_id)
    except br_client.BRError:
        return cached_url
    status = str(info.get("status") or "").lower()
    if status in ("succeeded", "succeed", "success", "completed"):
        return info.get("videoUrl") or cached_url
    return cached_url


def _provider_completed_media_url(url):
    """Upgrade only a provider-completed standard HTTP VOD URL to HTTPS.

    The generic downloader remains HTTPS-only.  This narrow adapter is called
    only after a BasicRouter video task reports completion; it does not accept
    credentials, custom ports or malformed URLs and does not relax DNS/peer,
    redirect, certificate or content checks performed by ``br_client``.
    """
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        port = parsed.port
    except ValueError:
        return url
    if parsed.scheme == "https":
        return url
    if (parsed.scheme != "http" or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            port not in (None, 80)):
        return url
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    return urllib.parse.urlunsplit((
        "https", host, parsed.path, parsed.query, parsed.fragment))


def _download_completed_video(api_key, task_id, url, out_path):
    """Download once, refreshing only the URL of the same task on auth expiry."""
    trusted_url = _provider_completed_media_url(url)
    try:
        return br_client.download(
            trusted_url, out_path, allow_nonpublic_peer=True)
    except br_client.BRError as exc:
        message = str(exc).lower()
        if (getattr(exc, "http_status", None) not in (401, 403, 404) and
                not any(token in message for token in ("expired", "signature"))):
            raise
        refreshed = _refresh_completed_video_url(api_key, task_id, url)
        refreshed_url = _provider_completed_media_url(refreshed)
        if not refreshed_url or refreshed_url == trusted_url:
            raise
        return br_client.download(
            refreshed_url, out_path, allow_nonpublic_peer=True)


def _download_completed_video_guarded(
        api_key, task_id, url, out_path, *, segment=None, task=None):
    """Atomically create recovery output without replacing existing bytes."""
    contract = (segment or {}).get("output_artifact_contract") or {}
    if not contract:
        return _download_completed_video(api_key, task_id, url, out_path)
    output = os.path.abspath(out_path)
    if os.path.abspath(str(contract.get("path") or "")) != output:
        raise ValueError("RECOVERY_CANARY_OUTPUT_CONTRACT_INVALID")
    if contract.get("mode") == "approved_canary_exact_media_reuse":
        proof = (segment or {}).get("audio_canary_proof") or {}
        if (task_id != contract.get("task_id") or
                contract.get("audio_canary_proof_fingerprint") !=
                proof.get("fingerprint") or
                artifact_contract.file_sha256(output) !=
                contract.get("sha256")):
            raise ValueError("APPROVED_CANARY_OUTPUT_REUSE_STALE")
        return output
    if os.path.exists(output):
        expected_take = (task or {}).get("take_fingerprint")
        current_take = take_review.take_fingerprint({
            "segment_id": (segment or {}).get("id"),
            "taskId": task_id, "videoUrl": url, "localPath": output,
            "video_handoff_fingerprint": (task or {}).get(
                "handoff_fingerprint") or (segment or {}).get(
                    "video_handoff_fingerprint"),
        })
        if expected_take and current_take == expected_take:
            return output
        raise ValueError(
            "RECOVERY_CANARY_OUTPUT_ALREADY_EXISTS: %s" % output)
    directory = os.path.dirname(output) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".%s.download-" % os.path.basename(output),
        suffix=".tmp", dir=directory)
    os.close(fd)
    os.remove(temporary)
    try:
        _download_completed_video(api_key, task_id, url, temporary)
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise ValueError(
                "RECOVERY_CANARY_OUTPUT_ALREADY_EXISTS: %s" % output) from exc
        return output
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _fallback_metadata(initial_model, model, reason=None):
    used = model != initial_model
    return {"model": model, "initial_model": initial_model,
            "fallback_from": initial_model if used else None,
            "fallback_reason": reason if used else None,
            "retry_count": 1 if used else 0}


def _storyboard_negative(base):
    extra = ("多格故事板整图动画, 把多格故事板当成一张图平移缩放, 分屏拼贴成片, "
             "素描风格, 铅笔画, 炭笔画, 黑白绘画, 插画, 动漫, 新增角色, 换装, "
             "换场景, 改产品外观, 改剧情顺序, "
             "红色箭头, 蓝色箭头, 绿色构图标记, 橙色灯光标记, 紫色声音标记, "
             "黑色镜头笔记, 彩色箭头, 箭头标注, 手写批注, 导演标注, 面板标签, "
             "故事板标注, contact sheet, storyboard grid, panel border, "
             "annotation marks, pencil sketch overlay, charcoal drawing effect")
    if not base:
        return extra
    return base + ", " + extra


def _heartbeat_formal_generation(manifest, manifest_path, stage, owner_token):
    """Refresh a paid-stage lease and replace the caller's stale snapshot."""
    if manifest is None or not manifest_path or not owner_token:
        return manifest
    refreshed = _rm.heartbeat_generation_lease(
        manifest_path, stage, owner_token, ttl_seconds=300)
    manifest.clear()
    manifest.update(refreshed)
    return manifest


def render(*_args, **_kwargs):
    """Disabled legacy single-request API; use a one-element reviewed batch."""
    raise ValueError(
        "LEGACY_SINGLE_RENDER_DISABLED: 单段也必须写入 segments.json，先完成 preflight "
        "与 capture/confirm，再调用 render_batch 或 --batch。")


def _extract_last_frame(video_path, log):
    """抽视频最后一帧存为 png，返回路径。用于尾帧串联(跨段一致性正解)。"""
    import subprocess, tempfile
    import media_tools
    ff = media_tools.resolve_ffmpeg_pair()[0]
    if not ff:
        log("[chain] 无外部 ffmpeg，无法抽尾帧，串联降级为独立生成")
        return None
    fd, out = tempfile.mkstemp(suffix="_lastframe.png")
    os.close(fd)
    os.remove(out)
    # -sseof -0.3 定位到结尾前 0.3s，取最后一帧
    r = subprocess.run([ff, "-hide_banner", "-y", "-sseof", "-0.3",
                        "-i", video_path, "-vframes", "1", out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    log("[chain] 抽尾帧失败")
    return None


def _validate_chain_preflight(
        segments, *, draft, client, manifest, manifest_path, ledger_path,
        results_out, prompt_review):
    """Fail closed before any chained paid-generation request is submitted."""
    undeclared = [
        str(segment.get("id") or index + 1)
        for index, segment in enumerate(segments[1:], 1)
        if not (segment.get("chain_required") or
                segment.get("avatar_plate_chain") or
                segment.get("extend_video") or
                segment.get("extend_from_previous"))
    ]
    if undeclared:
        raise ValueError(
            "CHAIN_CONTRACT_REQUIRED: render_chained 不得在客户确认后"
            "临时注入尾帧；请先把 chain_required/chain_contract 编译进 "
            "segments 并重新 capture/confirm：%s" % ",".join(undeclared))
    non_oral_extensions = [
        segment.get("id") for segment in segments
        if (segment.get("extend_video") or
            segment.get("extend_from_previous")) and
        not segment.get("oral_broadcast")]
    if non_oral_extensions:
        raise ValueError(
            "VIDEO_EXTENSION_BLOCKED: 只有 oral-broadcast 口播场景允许使用模型视频延长；"
            "其他场景必须独立分段生成后拼接：%s" %
            ", ".join(map(str, non_oral_extensions)))
    _validate_generation_contracts(segments, formal=not draft)
    if not prompt_review:
        raise ValueError(
            "PROMPT_REVIEW_REQUIRED: 生视频前必须提供已确认的逐模型完整提示词审核文件")
    _require_confirmed_prompt_review(prompt_review, "video", segments)
    if draft:
        if manifest is not None or manifest_path:
            raise ValueError(
                "DRAFT_MANIFEST_MUTATION_BLOCKED: 草稿不得读取或修改正式 run manifest")
        return
    if not client or manifest is None or not manifest_path or not results_out:
        raise ValueError(
            "FORMAL_VIDEO_REQUIRES_CLIENT_MANIFEST: chain 正式入口需要 "
            "client、manifest、results_out")
    if ledger_path and os.path.isfile(ledger_path):
        _rm.reconcile_tasks_from_ledger(manifest, ledger_path)
        _rm.save_manifest(manifest, manifest_path)
    _rm.identity_gate(manifest, client=client)
    _rm.generation_gate(manifest, "video", client=client)
    _rm.validate_pre_video_gates(manifest, prompt_review)
    _manifest_handoff_matches(manifest, segments)
    _validate_references(segments, client, manifest)
    _validate_reference_handoff(segments)
    import storyboard_enhancements
    stale = storyboard_enhancements.stale_artifacts(segments)
    if not stale["ok"]:
        raise ValueError(
            "STALE_ARTIFACTS_BLOCK_DELIVERY: %s" %
            json.dumps(stale["stale"], ensure_ascii=False))
    if any(not segment.get("out_path") for segment in segments):
        raise ValueError("OUT_PATH_REQUIRED: 正式 chain 每段必须提供 out_path")


def _chain_tail_reference_required(segment, index):
    return bool(
        index > 0 and (
            segment.get("chain_required") or
            segment.get("avatar_plate_chain") or
            segment.get("storyboard_ref") or
            segment.get("storyboard_ref_mode") or
            segment.get("use_storyboard_reference") or
            not segment.get("urls")))


def _validate_chain_reference_budget(segments, preflight, *, formal):
    """Reserve the future tail-frame slot before any earlier clip is paid."""
    rows = {
        str(row.get("segment_id")): row
        for row in (preflight.get("segments") or [])
        if isinstance(row, dict)
    }
    for index, segment in enumerate(segments[1:], 1):
        row = rows.get(str(segment.get("id") or "unknown")) or {}
        adds_tail = _chain_tail_reference_required(segment, index)
        reference_count = int(row.get("reference_count") or 0)
        if adds_tail:
            reference_count += 1
        try:
            _validate_model_reference_capacity(
                row.get("model"), reference_count, formal=formal)
        except (ValueError, br_client.BRError) as exc:
            raise ValueError(
                "CHAIN_REFERENCE_BUDGET_EXCEEDED: segment %s 的已确认参考图"
                "加尾帧共 %d 张，不得在生成时静默丢图；%s" %
                (segment.get("id") or index + 1, reference_count, exc)
            ) from exc


def _load_chain_resume_results(results_out, *, draft):
    if not draft or not results_out or not os.path.isfile(results_out):
        return {}
    try:
        with open(results_out, encoding="utf-8") as handle:
            prior_results = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(prior_results, list):
        return {}
    return {
        item["segment_id"]: item
        for item in prior_results
        if isinstance(item, dict) and item.get("segment_id")
    }


def _build_chain_success_result(
        *, url, local, ocr, task_id, segment, qc, task, attempt,
        initial_model, selected_model, dependency, fallback_reason=None):
    result = {
        "ok": True,
        "videoUrl": url,
        "localPath": local,
        "absPath": os.path.abspath(local) if local else None,
        "error": None,
        "attempt": task.get("attempt", attempt),
        "ocr_warning": bool(ocr and ocr.get("subtitle_detected")),
        "ocr_texts": ocr.get("texts") if ocr else [],
        "ocr_status": ocr.get("status") if ocr else "unavailable",
        "ocr_available": bool(ocr and ocr.get("available")),
        "ocr_frames_checked": ocr.get("frames_checked", 0) if ocr else 0,
        "ocr_expected": ocr.get("expected", 0) if ocr else 0,
        "ocr_error": ocr.get("error") if ocr else "OCR unavailable",
        "taskId": task_id,
        "segment_id": segment.get("id"),
        "scene_id": segment.get("scene_id"),
        "take_id": "%s-take-01" % segment.get("id"),
        "video_handoff_fingerprint": segment.get(
            "video_handoff_fingerprint"),
        "review_status": "pending",
        "actual_duration": (
            (qc.get("media") or {}).get("actual_duration") if qc else None),
        "media_qc": qc,
        "media_qc_report": qc.get("report_path") if qc else None,
        **_fallback_metadata(
            initial_model, selected_model,
            fallback_reason),
    }
    result.update(_submission_prompt_snapshot_fields(source=task))
    result["take_fingerprint"] = take_review.take_fingerprint(result)
    result["generation_dependency"] = (dependency or {}).get("payload")
    result["generation_dependency_fingerprint"] = (
        dependency or {}).get("fingerprint")
    return result


def _build_media_qc_failure_result(
        *, url, local, qc, task_id, segment, task,
        initial_model, selected_model, fallback_reason=None):
    """Preserve a failed formal take as exact regeneration evidence."""
    result = {
        "ok": False, "videoUrl": url, "localPath": local,
        "absPath": os.path.abspath(local) if local else None,
        "error": "MEDIA_QC_FAILED: %s" % ", ".join(
            qc.get("errors") or []),
        "taskId": task_id, "segment_id": segment.get("id"),
        "scene_id": segment.get("scene_id"),
        "take_id": "%s-take-%s" % (
            segment.get("id"), task.get("attempt", 1)),
        "video_handoff_fingerprint": (
            task.get("handoff_fingerprint") or
            segment.get("video_handoff_fingerprint")),
        "review_status": "pending",
        # Download succeeded. Re-fetching identical bytes cannot repair a
        # missing stream or corrupt media; only an explicitly authorized new
        # paid attempt can replace this take.
        "resume_available": False,
        "regeneration_required": True,
        "actual_duration": (qc.get("media") or {}).get("actual_duration"),
        "media_qc": qc, "media_qc_report": qc.get("report_path"),
        **_fallback_metadata(
            initial_model, selected_model, fallback_reason),
    }
    result.update(_submission_prompt_snapshot_fields(source=task))
    if local or url:
        result["take_fingerprint"] = take_review.take_fingerprint(result)
    return result


def _next_chain_tail(
        *, local, api_key, task_id, video_url, draft, log):
    """Extract and upload the continuity tail, cleaning temporary video bytes."""
    src_for_tail = local
    temporary_video = None
    if not src_for_tail:
        import tempfile
        fd, temporary_video = tempfile.mkstemp(suffix="_seg.mp4")
        os.close(fd)
        os.remove(temporary_video)
        _download_completed_video(
            api_key, task_id, video_url, temporary_video)
        src_for_tail = temporary_video
    try:
        tail_path = _extract_last_frame(src_for_tail, log)
    finally:
        if temporary_video and os.path.isfile(temporary_video):
            os.remove(temporary_video)
    tail_url = (_video_image_ref(
        tail_path, api_key=api_key, allow_local_draft=draft)
        if tail_path else None)
    return tail_path, tail_url


def _prepare_chain_references(
        segment, *, index, previous_tail_url, api_key, client, draft, log):
    """Resolve one segment's authored references and continuity tail policy."""
    segment = _expand_product_refs(segment, client)
    source_segment = dict(segment)
    storyboard_ref = bool(
        segment.get("storyboard_ref") or
        segment.get("storyboard_ref_mode") or
        segment.get("use_storyboard_reference"))
    has_own_explicit_urls = (
        bool(segment.get("urls")) and
        not segment.get("_locked_urls") and
        not storyboard_ref)
    storyboard_tail_url = (
        previous_tail_url
        if storyboard_ref and index > 0 and previous_tail_url else None)
    forced_avatar_chain = bool(
        segment.get("chain_required") or
        segment.get("avatar_plate_chain"))

    if forced_avatar_chain and index > 0 and previous_tail_url:
        own_refs = [
            _video_image_ref(url, api_key=api_key, allow_local_draft=draft)
            for url in (segment.get("urls") or []) if url]
        ref_urls = [previous_tail_url] + [
            url for url in own_refs if url != previous_tail_url]
        if len(ref_urls) > 4:
            raise ValueError(
                "REFERENCE_COUNT_UNSUPPORTED: segment %s 的尾帧、背景和身份参考超过模型上限；"
                "请把背景做成单一确认板或减少参考图。" %
                (segment.get("id") or "unknown"))
        video_type = 5 if len(ref_urls) > 1 else 2
        segment["_chain_tail_frame"] = True
        log("[chain %d] avatar plate：尾帧 + 同一背景/身份参考图" %
            (index + 1))
    elif storyboard_ref:
        video_type = segment.get("video_type", 5)
        ref_urls = None
        if storyboard_tail_url:
            segment["_chain_tail_frame"] = True
    elif index == 0 or has_own_explicit_urls:
        video_type = segment.get("video_type", 1)
        urls = segment.get("urls")
        ref_urls = ([
            _video_image_ref(url, api_key=api_key, allow_local_draft=draft)
            for url in urls] if urls else None)
    elif segment.get("_locked_urls"):
        ref_urls = [
            _video_image_ref(url, api_key=api_key, allow_local_draft=draft)
            for url in (segment.get("urls") or []) if url]
        if previous_tail_url:
            ref_urls = [previous_tail_url] + [
                url for url in ref_urls if url != previous_tail_url]
            if len(ref_urls) > 4:
                raise ValueError(
                    "REFERENCE_COUNT_UNSUPPORTED: chain segment %s needs %d references; "
                    "the workflow will not truncate confirmed refs." %
                    (segment.get("id") or "unknown", len(ref_urls)))
            video_type = 5 if len(ref_urls) > 1 else 4
            segment["_chain_tail_frame"] = True
            log("[chain %d] 尾帧串联 + 锁定参考图合并注入" %
                (index + 1))
        else:
            video_type = segment.get(
                "video_type", 4 if len(ref_urls) == 1 else 5)
        if not ref_urls:
            video_type = segment.get("video_type", 1)
    else:
        ref_urls = [previous_tail_url] if previous_tail_url else None
        if ref_urls:
            video_type = 2
            segment["_chain_tail_frame"] = True
        else:
            video_type = segment.get("video_type", 1)
            log("[chain %d] 无上段尾帧，降级独立生成" % (index + 1))
    return (segment, source_segment, storyboard_ref,
            storyboard_tail_url, ref_urls, video_type)


def render_chained(segments, model=None, verbose=True,
                   seed=None, negative_prompt=None, allow_model_fallback=True,
                   client=None, manifest=None, manifest_path=None, ledger_path=None,
                   results_out=None, allow_ocr_warning=False, draft=False,
                   max_wait=3600, prompt_review=None, budget_cap=None):
    """Render a continuity chain through the isolated serial state machine."""
    import video_chain_orchestrator
    return video_chain_orchestrator.render_chained(
        sys.modules[__name__], segments, model=model, verbose=verbose, seed=seed,
        negative_prompt=negative_prompt, allow_model_fallback=allow_model_fallback,
        client=client, manifest=manifest, manifest_path=manifest_path,
        ledger_path=ledger_path, results_out=results_out,
        allow_ocr_warning=allow_ocr_warning, draft=draft, max_wait=max_wait,
        prompt_review=prompt_review, budget_cap=budget_cap)


def _apply_locked_refs(segments, locked_refs):
    """把一组共享锚定参考图强制注入每一段（放最前，去重），并把该段
    videoType 升到参考图锚定（有数字人→4，多图→5，单图→2），实现「固定素材、只变
    台词/剧本/运镜」的跨段一致性锁。返回新的 segments 列表（不改原对象）。

    locked_refs: 已确认的共享参考图路径/URL 列表（如人物板正脸+全身、产品 hero、场景图）。
    段自身的 urls 会被保留并接在共享锚之后（共享锚优先，权重更高）。

    每段额外打上 seg["_locked_urls"]=True 标记，区分"这段 urls 是 locked_refs 注入的"
    还是"用户自己在 segments.json 里显式写的 urls"。**这个标记是 --chain + --locked-refs
    组合时不互相打架的关键**：render_chained 的串联分支判断需要知道段的 urls 是不是
    locked_refs 注入的，才能正确地把尾帧图和锁定参考图合并，而不是让 locked_refs 的
    存在直接短路掉尾帧串联逻辑（旧 bug：urls 存在就完全跳过 tail-frame 分支，导致
    --chain 和 --locked-refs 一起用时尾帧串联被静默完全禁用）。
    """
    if not locked_refs:
        return segments
    out = []
    for seg in segments:
        seg = dict(seg)
        own = seg.get("urls") or []
        ordered = (list(own) + list(locked_refs) if seg.get("storyboard_ref")
                   else list(locked_refs) + list(own))
        merged = []
        for u in ordered:
            if u and u not in merged:
                merged.append(u)
        if len(merged) > 4:
            raise ValueError(
                "REFERENCE_COUNT_UNSUPPORTED: segment %s requires %d locked references; "
                "locked references may not be truncated." %
                (seg.get("id") or "unknown", len(merged)))
        seg["urls"] = merged
        existing_refs = {ref.get("url"): dict(ref) for ref in seg.get("references") or []
                         if isinstance(ref, dict) and ref.get("url")}
        seg["references"] = [
            existing_refs.get(url, {
                "id": "locked_ref_%02d" % (index + 1), "url": url,
                "type": "generic_visual", "scope": "scene",
                "tag": "@locked_ref_%02d" % (index + 1),
                "label": "共享锁定参考图",
                "role": "固定人物/产品/场景",
                "intent": "只继承已确认主体身份和场景事实"
            })
            for index, url in enumerate(merged)
        ]
        seg["_locked_urls"] = True
        # Always recompute from the refs actually sent. Storyboard refs are kept
        # first so a shared lock can never silently evict the approved shot.
        seg["video_type"] = 4 if len(merged) == 1 else (5 if merged else 1)
        if seg.get("video_handoff_fingerprint"):
            seg["video_handoff_fingerprint"] = artifact_contract.build_video_handoff(seg)["fingerprint"]
        out.append(seg)
    return out


def _load_batch_segments(path, ratio_override=None):
    """Load either a raw segment list or the full script_splitter result.

    Keeping the splitter metadata in ``segments.json`` is useful for the later
    assemble/caption steps, so batch rendering should not require callers to
    manually extract ``result["segments"]`` first.
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict):
        segments = payload.get("segments")
        if not isinstance(segments, list):
            raise ValueError("batch JSON 对象必须包含 segments 数组")
    elif isinstance(payload, list):
        segments = payload
    else:
        raise ValueError("batch JSON 必须是 segments 数组或包含 segments 的对象")
    if not all(isinstance(seg, dict) for seg in segments):
        raise ValueError("batch segments 必须是对象数组")
    if ratio_override:
        segments = [dict(seg, ratio=ratio_override) for seg in segments]
    return segments


def _load_manifest_handoff_segments(manifest):
    """Load exact formal segments from the immutable handoff file."""
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    record = handoff.get("file") or {}
    if not _rm.file_record_is_current(record):
        raise ValueError("VIDEO_HANDOFF_FILE_REQUIRED_OR_STALE")
    segments = _load_batch_segments(record["path"])
    _manifest_handoff_matches(manifest, segments)
    return segments


def _apply_continuity_plan(segments, plan):
    """Inject one reviewed continuity/re-anchor plan without mutating input."""
    target = plan.get("segment_id")
    output, matched = [], False
    for original in segments:
        segment = dict(original)
        if segment.get("id") == target:
            matched = True
            references = list(plan.get("references") or []) + list(segment.get("references") or [])
            deduped = []
            for ref in references:
                if ref.get("url") and ref.get("url") not in [item.get("url") for item in deduped]:
                    deduped.append(ref)
            segment["references"] = deduped[:4]
            segment["urls"] = [ref["url"] for ref in segment["references"]]
            segment["reference_roles"] = segment["references"]
            segment["continuity_in"] = json.dumps(plan.get("continuity_in") or {}, ensure_ascii=False)
            segment["video_type"] = 5 if len(segment["urls"]) > 1 else (2 if segment["urls"] else 1)
            segment["take_review_required"] = True
            segment["continuity_plan"] = plan
            segment["video_handoff_fingerprint"] = artifact_contract.build_video_handoff(segment)["fingerprint"]
        output.append(segment)
    if not matched:
        raise ValueError("CONTINUITY_PLAN_MISMATCH: 找不到 segment %s" % target)
    return output


def _submission_assets(manifest, *, strict):
    """Translate manifest approvals to the generic policy asset vocabulary."""
    if not strict:
        return {}, []
    manifest = manifest if isinstance(manifest, dict) else {}
    stage_keys = [("storyboard", "storyboard")]
    if manifest.get("requires_product_board"):
        stage_keys.append(("product_board", "product_board"))
    if manifest.get("requires_cast_board"):
        stage_keys.append(("cast_board", "digital_human_board"))
    if manifest.get("requires_product_usage"):
        stage_keys.append(("product_usage", "product_usage_board"))
    assets = {}
    for stage, asset_key in stage_keys:
        try:
            confirmed = bool(_rm.approval_is_current(manifest, stage))
        except (OSError, TypeError, ValueError, KeyError):
            confirmed = False
        fingerprint = (manifest.get("approval_hashes") or {}).get(stage)
        assets[asset_key] = {
            "confirmed": confirmed,
            "fingerprint": fingerprint,
            "expected_fingerprint": fingerprint,
        }
    return assets, [asset_key for _, asset_key in stage_keys]


def _segment_reference_types(segment):
    explicit = segment.get("actual_reference_types")
    if explicit is not None:
        return sorted(set(explicit or []))
    return sorted({
        reference.get("type")
        for reference in (segment.get("references") or [])
        if isinstance(reference, dict) and reference.get("type")
    })


def _enforce_batch_budget(segments, default_model, budget_cap, log, *,
                          manifest=None, preflight_ready=False, strict=True):
    """Estimate a batch and fail closed on every paid-submission contract.

    Budget is batch-wide; reference completeness is evaluated per segment so
    one complete clip cannot mask another clip's missing confirmed reference.
    """
    estimated_cost = round(sum(
        cost_ledger.cost_estimate_for_task(
            segment.get("model") or default_model,
            segment.get("duration") or segment.get("duration_sec"),
        )["cost_estimate"]
        for segment in segments
    ), 4)
    log("[batch] 本次渲染预计成本 $%.4f（%d 段，按历史价目表估算）" %
        (estimated_cost, len(segments)))
    obs_log.log_event(
        "batch_cost_estimate", estimated_cost=estimated_cost,
        segments=len(segments), budget_cap=budget_cap)
    assets, required_asset_keys = _submission_assets(manifest, strict=strict)
    for segment in segments:
        policy_context = {
            "assets": assets,
            "required_asset_keys": required_asset_keys,
            "required_reference_types": list(
                segment.get("required_reference_types") or []),
            "actual_reference_types": _segment_reference_types(segment),
            "dropped_references": list(segment.get("dropped_references") or []),
            "model": segment.get("model") or default_model,
            "capability_preflight_passed": bool(preflight_ready),
            "reference_count": len(segment.get("urls") or []),
            "duration": segment.get("duration") or segment.get("duration_sec"),
            "video_type": segment.get("video_type"),
            "estimated_cost": estimated_cost,
            "budget_cap": budget_cap,
        }
        policy_check.enforce(
            "submit_video", policy_context,
            fail_on_indeterminate=strict)
    return estimated_cost


def _validate_storyboard_revisions(segments):
    """Reject segments compiled against stale storyboard bytes or revisions."""
    stale = []
    for segment in segments:
        handoff = artifact_contract.verify_video_handoff(segment)
        if handoff["strict"] and not handoff["ok"]:
            stale.append("%s (视频交接指纹已变化)" % segment.get("id", "?"))
        storyboard_path = segment.get("storyboard_path")
        if (segment.get("storyboard_ref") and storyboard_path and
                not os.path.isfile(storyboard_path)):
            stale.append("%s (%s)" % (segment.get("id", "?"), storyboard_path))
        storyboard_dir = segment.get("storyboard_dir")
        result_path = (os.path.join(storyboard_dir, "storyboard_result.json")
                       if storyboard_dir else None)
        if not result_path or not os.path.isfile(result_path):
            continue
        try:
            with open(result_path, encoding="utf-8") as handle:
                result = json.load(handle)
            actual_plan_fingerprint = result.get("plan_fingerprint")
            if not actual_plan_fingerprint:
                continue
            matching = next((
                item for item in result.get("shots") or []
                if str((item.get("shot") or {}).get("id")) == str(segment.get("id"))
            ), None)
            expected_shot = segment.get("storyboard_shot_fingerprint")
            if expected_shot:
                stored = (matching or {}).get("shot_fingerprint")
                try:
                    import storyboard as _storyboard
                    current = _storyboard.shot_fingerprint(
                        (matching or {}).get("shot") or {})
                except (OSError, ValueError, TypeError, KeyError):
                    current = None
                if not matching or expected_shot not in (stored, current):
                    stale.append("%s (故事板镜头画面已修订)" % segment.get("id", "?"))
                continue
            expected_plan = segment.get("storyboard_plan_fingerprint")
            recorded_result = segment.get("storyboard_result_fingerprint")
            if expected_plan and recorded_result and expected_plan != recorded_result:
                stale.append("%s (分段与故事板结果指纹不一致)" % segment.get("id", "?"))
            if expected_plan and expected_plan != actual_plan_fingerprint:
                stale.append("%s (故事板 revision 已不是分段编译时的版本)" %
                             segment.get("id", "?"))
        except (OSError, ValueError, TypeError):
            stale.append("%s (无法读取故事板结果)" % segment.get("id", "?"))
    if stale:
        raise ValueError(
            "STALE_STORYBOARD: 分段配置引用的故事板文件已不存在或已被修订：%s。"
            "请用最新 storyboard_plan.json 和最新 revision 目录重新运行 script_splitter split，"
            "不要继续使用旧的 segments JSON。" % "; ".join(stale))


def _finalize_batch_results(results, tasks, segments, *, allow_ocr_warning,
                            results_out, draft, log):
    """Attach immutable prompt evidence, report warnings, and persist results."""
    for index, item in enumerate(results):
        if item is None:
            continue
        item.setdefault("segment_id", segments[index].get("id"))
        task = tasks[index] if index < len(tasks) else None
        if task:
            item.update(_submission_prompt_snapshot_fields(source=task))
            item.setdefault(
                "video_handoff_fingerprint",
                task.get("handoff_fingerprint") or
                segments[index].get("video_handoff_fingerprint"))
            item.setdefault("taskId", task.get("task_id"))
        else:
            item.setdefault(
                "video_handoff_fingerprint",
                segments[index].get("video_handoff_fingerprint"))
        if segments[index].get("audio_capability_policy"):
            item.setdefault(
                "audio_capability_policy",
                segments[index].get("audio_capability_policy"))
            item.setdefault(
                "audio_capability_policy_contract",
                segments[index].get("audio_capability_policy_contract") or {})
    for result in results:
        if result and result.get("ocr_warning") and not allow_ocr_warning:
            result["delivery_blocked"] = True
            result["needs_confirmation"] = True
            obs_log.log_event(
                "ocr_warning", segment_id=result.get("segment_id"),
                ocr_texts=result.get("ocr_texts"))
    obs_log.log_event(
        "batch_done",
        succeeded=sum(1 for result in results if result and result.get("ok")),
        failed=sum(1 for result in results if not (result and result.get("ok"))))
    if results_out:
        _atomic_json_write(results_out, results)
        if any(segment.get("storyboard_panel") for segment in segments):
            try:
                import storyboard_enhancements
                storyboard_enhancements.write_binding_report(
                    segments, results, os.path.join(
                        os.path.dirname(os.path.abspath(results_out)),
                        "video_binding_report.html"))
            except (ImportError, OSError, ValueError, TypeError, RuntimeError) as report_error:
                log("[binding-report] 生成审计报告失败：%s" % report_error)
        _write_video_submission_report(results_out, results, draft=draft)
    return results


def _prepare_batch_task_submission(context, seg, index, state):
    i = index
    api_key = context["api_key"]
    client = context["client"]
    manifest = context["manifest"]
    manifest_path = context["manifest_path"]
    ledger_path = context["ledger_path"]
    default_model = context["default_model"]
    draft = context["draft"]
    seed = context["seed"]
    negative_prompt = context["negative_prompt"]
    log = context["log"]
    segments = context["segments"]
    handoff_fp = seg.get("video_handoff_fingerprint") or artifact_contract.build_video_handoff(seg)["fingerprint"]
    storyboard_ref = bool(seg.get("storyboard_ref") or seg.get("storyboard_ref_mode") or seg.get("use_storyboard_reference"))
    # ── product_sku 自动展开：若 seg 含 product_sku，通过 product_library.resolve()
    # 把多方位图展开为 urls，实现与数字人对等的多图锚定（videoType=5）
    seg_urls = seg.get("urls") or []
    product_sku = seg.get("product_sku")
    if product_sku and not seg_urls:
        try:
            import product_library as _pl
            _pl_client = seg.get("client") or client or ""
            if _pl_client:
                _pr = _pl.resolve(_pl_client, product_sku)
                expanded = []
                if _pr.get("hero"):
                    expanded.append(_pr["hero"])
                expanded.extend(_pr.get("refs") or [])
                # A confirmed 3x3 product board is a second identity
                # anchor: views control geometry, the board controls
                # cross-angle material/details and usage context.
                if _pr.get("product_board_confirmed") and _pr.get("product_board"):
                    expanded.append(_pr["product_board"])
                if expanded:
                    # Never truncate confirmed product anchors.  The
                    # model-capacity gate below must either select a
                    # capable model or fail before submission.
                    seg_urls = expanded
                    # 如果没显式设 video_type，根据 refs 数量自动升 vtype
                    if product_sku:
                        # A product is an environment/multi-reference
                        # subject even when only hero.png exists. Keep
                        # the segment on type=5 instead of falling back
                        # to single-image type=2.
                        seg = dict(seg, video_type=5)
                    log("[product_sku %d] %s → %d 产品一致性参考图 (vtype=%s)"
                        % (i + 1, product_sku, len(seg_urls), seg.get("video_type", 1)))
        except (ImportError, OSError, ValueError, TypeError, KeyError) as _pe:
            raise ValueError("PRODUCT_SKU_RESOLVE_FAILED: %s (%s)" % (product_sku, _pe))

    if product_sku and not seg_urls:
        raise ValueError("PRODUCT_SKU_NO_REFERENCES: %s" % product_sku)

    urls = seg_urls or None
    submission_source_seg = dict(seg)
    if not draft and not product_sku:
        bad = [ref for ref in (urls or [])
               if not _trusted_remote_segment_reference(seg, ref, manifest)
               and not _reference_is_trusted(client, ref, manifest)]
        if bad:
            raise ValueError("UNTRUSTED_VIDEO_REFERENCE: %s" % ", ".join(map(str, bad)))
    seg_vtype = seg.get("video_type", 1)
    pick_args = {"video_type": seg_vtype}
    if not draft:
        pick_args.update(dialogue=_segment_integrated_audio_requirement(seg),
                         formal=True,
                         proven_audio_models=context.get(
                             "proven_audio_models") or ())
    existing_paid_task = _existing_paid_task_for_segment(manifest, seg, None)
    if existing_paid_task:
        seg_model = (existing_paid_task.get("model") or seg.get("model") or
                     default_model)
    elif not draft and seg.get("audio_capability_policy"):
        seg_model = _validate_empirical_audio_canary_policy(
            seg, manifest=manifest,
            formal_stage=context.get("formal_stage") or "video",
            model_catalog=context.get("model_catalog"))
    else:
        seg_model = _pick_video_model(
            seg.get("model") or default_model,
            reference_count=len(seg_urls or []),
            catalog=context.get("model_catalog"),
            allow_unproven_audio=False, **pick_args)
    resumed = None
    completed = None
    intent = None
    if manifest is not None:
        resumed = _rm.find_resumable_task(
            manifest, "video", seg.get("id"), handoff_fp, model=seg_model)
        intent = _rm.find_submission_intent(
            manifest, "video", seg.get("id"), handoff_fp, model=seg_model)
        completed = _completed_task(
            manifest, seg.get("id"), handoff_fp, model=seg_model)
    if not draft:
        if context.get("model_catalog") is None:
            raise ValueError("MODEL_CATALOG_SNAPSHOT_REQUIRED")
        if not (resumed or completed):
            _model_request_capability_evidence(
                seg_model, seg, context["model_catalog"])
        _assert_confirmed_submission_for_model(seg, seg_model)
    expanded_panel = None
    state.update({
        "seg": seg, "handoff_fp": handoff_fp,
        "storyboard_ref": storyboard_ref, "seg_model": seg_model,
        "seg_vtype": seg_vtype, "completed": completed,
        "resumed": resumed, "intent": intent, "expanded_panel": expanded_panel,
        "urls": urls,
        "submission_source_seg": submission_source_seg,
    })


def _handle_batch_submit_privacy_error(context, state, privacy_error):
    seg, model = state["seg"], state["seg_model"]
    if _privacy_rejection_stops(seg):
        stopped_task = {
            "segment": seg, "task_id": None, "model": model,
            "initial_model": state["initial_model"],
            "handoff_fingerprint": state["handoff_fp"],
            "attempt": state["submission_attempt"],
            "request_id": state["request_id"],
        }
        stopped = _privacy_stop_result(
            stopped_task, seg, privacy_error,
            observation_source="provider_submit_rejection")
        _persist_task(
            context["manifest"], context["manifest_path"],
            context["ledger_path"], stopped_task, "failed",
            error=stopped["error"], provider_error=stopped["provider_error"],
            privacy_fallback_policy=PRIVACY_FALLBACK_STOP_POLICY,
            privacy_fallback_attempted=False,
            failure_phase="submit", retry_same_request_id=False)
        state["terminal_result"] = stopped
        raise ValueError(stopped["error"]) from privacy_error
    if not _privacy_fallback_allowed(
            state["initial_model"], state["seg_vtype"], state["ref_urls"],
            context["allow_model_fallback"], segment=seg):
        raise privacy_error
    fallback_task = {
        "video_type": state["seg_vtype"], "ref_urls": state["ref_urls"],
        "source_segment": state["submission_source_seg"],
        "storyboard_ref": state["storyboard_ref"],
        "storyboard_panel": state["expanded_panel"],
    }
    provider_error = _provider_error_evidence(
        privacy_error, model=model, task_id=None,
        observation_source="provider_submit_rejection")
    target_model, preparation_error = None, None
    try:
        fallback = _prepare_privacy_fallback_submission(
            context["api_key"], fallback_task, seg, draft=context["draft"])
        target_model = fallback["model"]
        blocked_error = (
            "FALLBACK_APPROVAL_REQUIRED: provider privacy rejection requires a "
            "new model/ref contract and customer-confirmed prompt packet")
    except (br_client.BRError, ValueError) as blocker:
        preparation_error = blocker
        blocked_error = "FALLBACK_APPROVAL_REQUIRED: %s" % blocker
    state["fallback_block"] = {
        "provider_error": provider_error,
        "fallback_preparation_error": (str(preparation_error)[:1000]
                                        if preparation_error else None),
        "fallback_target_model": target_model, "billing_status": "unknown",
        "action": _privacy_fallback_recovery_action(
            seg, target_model, task_exists=False),
    }
    raise ValueError(blocked_error) from privacy_error


def _submit_batch_model_fallback(context, index, state, submit_error):
    if (not context["allow_model_fallback"] or
            not br_client.is_video_model_not_found(submit_error)):
        raise submit_error
    seg, failed_model = state["seg"], state["seg_model"]
    if _empirical_audio_canary_requested(seg):
        # The customer authorized one exact endpoint.  A missing route is a
        # failed canary, never permission to charge another model.
        raise submit_error
    fallback_model = _pick_video_model(
        video_type=state["seg_vtype"],
        dialogue=_segment_integrated_audio_requirement(seg),
        formal=not context["draft"], allow_fallback=True,
        reference_count=len(state["ref_urls"] or []),
        exclude_models=[failed_model], catalog=context.get("model_catalog"),
        allow_unproven_audio=False,
        proven_audio_models=context.get("proven_audio_models") or ())
    if not context["draft"]:
        _model_request_capability_evidence(
            fallback_model, seg, context["model_catalog"])
        _assert_confirmed_submission_for_model(seg, fallback_model)
    prepared_seg, prepared_ref_urls = seg, state["ref_urls"]
    prepared_panel = state["expanded_panel"]
    if state["storyboard_ref"]:
        try:
            prepared_seg, raw_urls, prepared_panel = \
                _prepare_storyboard_submission_for_model(
                    context["api_key"], state["submission_source_seg"],
                    fallback_model, formal=not context["draft"])
            prepared_ref_urls = [_video_image_ref(
                url, api_key=context["api_key"],
                allow_local_draft=context["draft"])
                for url in (raw_urls or [])]
        except (br_client.BRError, ValueError) as blocker:
            raise ValueError(
                "FALLBACK_APPROVAL_REQUIRED: %s 不可用，切换 %s 前必须先完成"
                "该模型的参考图审批，将 segment.model 设为目标模型后重新 "
                "capture/confirm 并重跑；%s" %
                (failed_model, fallback_model, blocker)) from blocker
    if not context["draft"]:
        _model_request_capability_evidence(
            fallback_model,
            dict(prepared_seg, video_type=prepared_seg.get(
                "video_type", state["seg_vtype"])),
            context["model_catalog"])
    attempt = state["submission_attempt"] + 1
    context["log"]("[submit %d model-fallback] %s 不可用，改用 %s 重试一次" %
                   (index + 1, failed_model, fallback_model))
    request_id = _persist_submission_intent(
        context["manifest"], context["manifest_path"], context["ledger_path"],
        prepared_seg, fallback_model, state["seg_vtype"], state["handoff_fp"],
        attempt=attempt)
    submission_text = _submission_text(
        prepared_seg, fallback_model, state["storyboard_ref"])
    fields = _submission_prompt_snapshot_fields(
        submission_text=submission_text,
        storyboard_ref=state["storyboard_ref"])
    _persist_submission_prompt_snapshot(
        context["manifest"], context["manifest_path"], context["ledger_path"],
        prepared_seg, fallback_model, state["seg_vtype"], state["handoff_fp"],
        attempt=attempt, request_id=request_id,
        submission_text=submission_text,
        storyboard_ref=state["storyboard_ref"])
    task_id, _ = _submit_video(
        context["api_key"], prepared_seg, fallback_model, state["seg_vtype"],
        prepared_ref_urls, state["seg_negative"], seed=state["seg_seed"],
        storyboard_ref=state["storyboard_ref"], request_id=request_id,
        submission_text=submission_text)
    state.update({
        "seg": prepared_seg, "seg_model": fallback_model,
        "ref_urls": prepared_ref_urls, "expanded_panel": prepared_panel,
        "fallback_reason": "model_not_found", "submission_attempt": attempt,
        "request_id": request_id, "submission_text": submission_text,
        "submission_fields": fields, "tid": task_id,
    })
    return task_id


def _resolve_batch_task_submission(context, index, state):
    i, segments = index, context["segments"]
    api_key, manifest = context["api_key"], context["manifest"]
    manifest_path, ledger_path = context["manifest_path"], context["ledger_path"]
    draft, log = context["draft"], context["log"]
    allow_model_fallback = context["allow_model_fallback"]
    seg, handoff_fp = state["seg"], state["handoff_fp"]
    storyboard_ref, seg_model = state["storyboard_ref"], state["seg_model"]
    seg_vtype, completed = state["seg_vtype"], state["completed"]
    resumed, intent = state["resumed"], state["intent"]
    expanded_panel, urls = state["expanded_panel"], state["urls"]
    seed, negative_prompt = context["seed"], context["negative_prompt"]
    submission_source_seg = state["submission_source_seg"]
    tid, submission_attempt = state["tid"], state["submission_attempt"]
    request_id, submission_text = state["request_id"], state["submission_text"]
    submission_fields = state["submission_fields"]
    if storyboard_ref and not (completed or resumed):
        seg, urls, expanded_panel = _prepare_storyboard_submission_for_model(
            api_key, submission_source_seg, seg_model, formal=not draft)
        seg_vtype = seg.get("video_type", seg_vtype)
    if not draft and not (completed or resumed):
        _model_request_capability_evidence(
            seg_model, dict(seg, video_type=seg_vtype),
            context["model_catalog"])
    ref_urls = ([_video_image_ref(u, api_key=api_key,
                                  allow_local_draft=draft)
                  for u in urls] if urls else None)
    state.update({"seg": seg, "seg_vtype": seg_vtype,
                  "ref_urls": ref_urls, "expanded_panel": expanded_panel})
    _validate_model_reference_capacity(
        seg_model, len(ref_urls or []), formal=not draft)
    seg_seed = seg.get("seed", seed)  # 段级 seed 覆盖批级；批级 seed 保证跨段一致
    seg_negative = seg.get("negative_prompt", negative_prompt)
    if storyboard_ref:
        seg_negative = _storyboard_negative(seg_negative)
    initial_model = seg_model
    fallback_reason = None
    if completed:
        tid = completed["task_id"]
        seg_model = completed.get("model") or seg_model
        submission_fields = _submission_prompt_snapshot_fields(source=completed)
        log("[resume-download %d/%d] taskId=%s" % (i + 1, len(segments), tid))
    elif resumed:
        tid = resumed["task_id"]
        seg_model = resumed.get("model") or seg_model
        submission_fields = _submission_prompt_snapshot_fields(source=resumed)
        log("[resume %d/%d] taskId=%s" % (i + 1, len(segments), tid))
        _record_task_resume(
            manifest, manifest_path, ledger_path,
            {"segment": seg, "task_id": tid, "model": seg_model,
             "handoff_fingerprint": handoff_fp,
             "request_id": resumed.get("request_id"),
             "dependency_fingerprint": resumed.get("dependency_fingerprint")})
    else:
        _validate_recovery_attempt_authorization(seg, manifest)
        _validate_recovery_output_contract(
            seg, manifest=manifest,
            formal_stage=context.get("formal_stage") or "video",
            before_new_submission=not draft,
            allow_registered_task=False)
        submission_attempt = _current_attempt(manifest, seg.get("id"))
        state["submission_attempt"] = submission_attempt
        request_id = ((intent or {}).get("request_id") or
                      _persist_submission_intent(
                          manifest, manifest_path, ledger_path, seg,
                          seg_model, seg_vtype, handoff_fp,
                          attempt=submission_attempt))
        submission_text = _submission_text(seg, seg_model, storyboard_ref)
        state.update({"request_id": request_id,
                      "submission_text": submission_text})
        submission_fields = _submission_prompt_snapshot_fields(
            submission_text=submission_text, storyboard_ref=storyboard_ref)
        _persist_submission_prompt_snapshot(
            manifest, manifest_path, ledger_path, seg, seg_model, seg_vtype,
            handoff_fp, attempt=submission_attempt, request_id=request_id,
            submission_text=submission_text, storyboard_ref=storyboard_ref)
        state.update({
            "seg": seg, "seg_model": seg_model, "seg_vtype": seg_vtype,
            "ref_urls": ref_urls, "expanded_panel": expanded_panel,
            "initial_model": initial_model, "submission_attempt": submission_attempt,
            "request_id": request_id, "submission_text": submission_text,
            "submission_fields": submission_fields, "seg_seed": seg_seed,
            "seg_negative": seg_negative,
        })
        try:
            tid, _ = _submit_video(api_key, seg, seg_model, seg_vtype, ref_urls,
                                   seg_negative, seed=seg_seed,
                                   storyboard_ref=storyboard_ref,
                                   request_id=request_id,
                                   submission_text=submission_text)
        except br_client.BRVideoReferencePrivacyError as privacy_error:
            _handle_batch_submit_privacy_error(context, state, privacy_error)
        except br_client.BRError as submit_error:
            tid = _submit_batch_model_fallback(
                context, index, state, submit_error)
            seg, seg_model = state["seg"], state["seg_model"]
            ref_urls, expanded_panel = state["ref_urls"], state["expanded_panel"]
            fallback_reason = state["fallback_reason"]
            submission_attempt = state["submission_attempt"]
            request_id, submission_text = state["request_id"], state["submission_text"]
            submission_fields = state["submission_fields"]
        if ledger_path:
            generation_ledger.append_event(
                 ledger_path, "task_submitted", stage="video", unit_id=seg.get("id"),
                 task_id=tid, model=seg_model, handoff_fingerprint=handoff_fp,
                 request_id=request_id)
        if manifest is not None:
            _rm.upsert_task(manifest, {"stage": "video", "unit_id": seg.get("id"),
                                         "handoff_fingerprint": handoff_fp,
                                        "attempt": submission_attempt,
                                        "task_id": tid, "model": seg_model,
                                         "request_id": request_id, "status": "submitted",
                                         "storyboard_panel": expanded_panel,
                                         "source_segment": submission_source_seg,
                                         **submission_fields})
            if manifest_path:
                _rm.save_manifest(manifest, manifest_path)
    state.update({
        "seg": seg, "seg_model": seg_model, "tid": tid,
        "submission_attempt": submission_attempt, "request_id": request_id,
        "submission_text": submission_text,
        "submission_fields": submission_fields,
        "submission_source_seg": submission_source_seg,
        "ref_urls": ref_urls, "expanded_panel": expanded_panel,
        "fallback_reason": fallback_reason, "seg_vtype": seg_vtype,
        "seg_seed": seg_seed, "seg_negative": seg_negative,
        "initial_model": initial_model,
    })


def _submit_one_batch_task(context, seg, index):
    i = index
    segments = context["segments"]
    manifest, manifest_path = context["manifest"], context["manifest_path"]
    ledger_path, log = context["ledger_path"], context["log"]
    state = {
        "seg_model": None, "initial_model": None, "tid": None,
        "ref_urls": None, "seg_vtype": seg.get("video_type", 1),
        "submission_attempt": None, "request_id": None,
        "submission_text": None, "submission_fields": {},
        "submission_source_seg": dict(seg), "handoff_fp": None,
    }
    try:
        _prepare_batch_task_submission(context, seg, index, state)
        _resolve_batch_task_submission(context, index, state)
        seg = state["seg"]
        tid, seg_model = state["tid"], state["seg_model"]
        initial_model, seg_vtype = state["initial_model"], state["seg_vtype"]
        ref_urls, seg_negative = state["ref_urls"], state["seg_negative"]
        seg_seed, storyboard_ref = state["seg_seed"], state["storyboard_ref"]
        expanded_panel = state["expanded_panel"]
        submission_source_seg = state["submission_source_seg"]
        submission_fields = state["submission_fields"]
        fallback_reason = state["fallback_reason"]
        submission_attempt = state["submission_attempt"]
        completed, resumed = state["completed"], state["resumed"]
        handoff_fp = state["handoff_fp"]
        log("[submit %d/%d] taskId=%s (model=%s)" % (i + 1, len(segments), tid, seg_model))
        return {"idx": i, "task_id": tid, "segment": seg, "error": None,
                      "model": seg_model, "initial_model": initial_model,
                       "video_type": seg_vtype, "ref_urls": ref_urls,
                       "negative_prompt": seg_negative, "seed": seg_seed,
                       "storyboard_ref": storyboard_ref,
                       "storyboard_panel": expanded_panel,
                       "source_segment": submission_source_seg,
                       **submission_fields,
                        "completed_url": None,
                       "fallback_reason": fallback_reason,
                       "attempt": (completed or resumed or {}).get(
                            "attempt", submission_attempt),
                       "handoff_fingerprint": handoff_fp}
    except (br_client.BRError, ValueError) as e:
        seg_model = state["seg_model"]
        initial_model = state["initial_model"]
        seg_vtype = state["seg_vtype"]
        ref_urls = state["ref_urls"]
        submission_attempt = state["submission_attempt"]
        request_id = state["request_id"]
        submission_source_seg = state["submission_source_seg"]
        handoff_fp = state["handoff_fp"]
        log("[submit %d/%d] FAILED: %s" % (i + 1, len(segments), e))
        terminal_result = state.get("terminal_result")
        if terminal_result:
            return {
                "idx": i, "task_id": None, "segment": seg,
                "error": terminal_result.get("error"),
                "model": seg_model, "initial_model": initial_model,
                "video_type": seg_vtype, "ref_urls": ref_urls,
                "attempt": submission_attempt, "fallback_reason": None,
                "source_segment": submission_source_seg,
                "terminal_result": terminal_result,
            }
        fallback_block = state.get("fallback_block")
        if fallback_block:
            fallback_task = {
                "segment": seg, "task_id": None, "model": seg_model,
                "handoff_fingerprint": handoff_fp,
                "attempt": submission_attempt, "request_id": request_id,
            }
            _persist_task(
                manifest, manifest_path, ledger_path, fallback_task,
                "fallback_approval_required", error=str(e), **fallback_block)
        else:
            _mark_submission_failed(
                manifest, manifest_path, seg,
                locals().get("handoff_fp"), submission_attempt,
                seg_model, request_id, e)
        return {"idx": i, "task_id": None, "segment": seg, "error": str(e),
                      "model": seg_model, "initial_model": initial_model,
                      "video_type": seg_vtype, "ref_urls": ref_urls,
                      "attempt": submission_attempt, "fallback_reason": None,
                      "source_segment": submission_source_seg,
                      "needs_confirmation": bool(fallback_block),
                      "delivery_blocked": bool(fallback_block),
                      **(fallback_block or {})}


def _submit_batch_tasks(segments, *, api_key, client, manifest, manifest_path,
                        ledger_path, default_model, draft, seed, negative_prompt,
                        allow_model_fallback, log, allow_unproven_audio=False,
                        proven_audio_models=(), formal_stage="video",
                        model_catalog=None):
    """Submit every asynchronous task before polling to preserve parallelism."""
    context = dict(locals())
    return [
        _submit_one_batch_task(context, segment, index)
        for index, segment in enumerate(segments)
    ]


def _settle_formal_test_segment_results(
        manifest, manifest_path, results_out, results):
    """Close every terminal canary outcome, including settlement failures."""
    try:
        if all(item and item.get("ok") for item in results):
            _rm.record_test_segment_finished(
                manifest, results_out, results)
            _rm.save_manifest(manifest, manifest_path)
            return manifest
        closed, _settlement = (
            _rm.record_test_segment_failure_settlement(
                manifest_path, results_out, results))
        manifest.clear()
        manifest.update(closed)
        return manifest
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        closed = _rm.record_generation_failure_if_running(
            manifest_path, "test_segment", exc,
            error_code="TEST_SEGMENT_SETTLEMENT_FAILED",
            retryable=False,
            diagnostics=[results_out] if results_out else ())
        manifest.clear()
        manifest.update(closed)
        raise


def _pause_batch_privacy_fallback(context, task, segment, provider_failure):
    """Persist both failures and stop before any unconfirmed fallback charge."""
    api_key = context["api_key"]
    draft = context["draft"]
    manifest, manifest_path = context["manifest"], context["manifest_path"]
    ledger_path = context["ledger_path"]
    results, pending = context["results"], context["pending"]
    index = task["idx"]
    provider_error = _provider_error_evidence(
        provider_failure, model=task.get("model"), task_id=task.get("task_id"))
    target_model = None
    preparation_error = None
    try:
        fallback = _prepare_privacy_fallback_submission(
            api_key, task, segment, draft=draft)
        target_model = fallback["model"]
        blocked_error = (
            "FALLBACK_APPROVAL_REQUIRED: provider privacy rejection requires a new "
            "model/ref contract and customer-confirmed prompt packet")
    except (br_client.BRError, ValueError) as exc:
        preparation_error = exc
        blocked_error = "FALLBACK_APPROVAL_REQUIRED: %s" % exc
    action = _privacy_fallback_recovery_action(
        segment, target_model, task_exists=bool(task.get("task_id")))
    _persist_task(
        manifest, manifest_path, ledger_path, task,
        "fallback_approval_required", error=blocked_error,
        provider_error=provider_error,
        fallback_preparation_error=(str(preparation_error)[:1000]
                                    if preparation_error else None),
        fallback_target_model=target_model, billing_status="unknown")
    results[index] = {
        "ok": False, "videoUrl": None, "localPath": None,
        "error": blocked_error, "taskId": task.get("task_id"),
        "segment_id": segment.get("id"), "needs_confirmation": True,
        "resume_available": bool(task.get("task_id")),
        "delivery_blocked": True,
        "provider_error": provider_error,
        "fallback_preparation_error": (str(preparation_error)[:1000]
                                        if preparation_error else None),
        "fallback_target_model": target_model,
        "billing_status": "unknown", "action": action,
        **_fallback_metadata(task["initial_model"], task["model"], None),
    }
    if task in pending:
        pending.remove(task)



def _poll_batch_task_info(context, task):
    t = task
    api_key = context["api_key"]
    poll_err_count = context["poll_err_count"]
    allow_model_fallback = context["allow_model_fallback"]
    draft = context["draft"]
    manifest, manifest_path = context["manifest"], context["manifest_path"]
    ledger_path = context["ledger_path"]
    results, pending = context["results"], context["pending"]
    log = context["log"]
    idx, tid, seg = t["idx"], t["task_id"], t["segment"]
    try:
        info = br_client.get_video(api_key, tid)
        poll_err_count[tid] = 0  # 查询成功，重置计数
    except br_client.BRVideoReferencePrivacyError as e:
        if _privacy_rejection_stops(seg):
            results[idx] = _privacy_stop_result(t, seg, e)
            _persist_task(
                manifest, manifest_path, ledger_path, t, "failed",
                error=results[idx]["error"],
                provider_error=results[idx]["provider_error"],
                privacy_fallback_policy=PRIVACY_FALLBACK_STOP_POLICY,
                privacy_fallback_attempted=False)
            pending.remove(t)
            return None
        if (_privacy_fallback_allowed(t["model"], t["video_type"], t["ref_urls"],
                                      allow_model_fallback, segment=seg)
                and not t.get("fallback_reason")):
            _pause_batch_privacy_fallback(context, t, seg, e)
            return None
        results[idx] = {"ok": False, "videoUrl": None, "localPath": None,
                        "error": str(e),
                         **_fallback_metadata(t["initial_model"], t["model"], t["fallback_reason"])}
        _persist_task(manifest, manifest_path, ledger_path, t, "failed",
                      error=str(e))
        pending.remove(t)
        return None
    except br_client.BRError as e:
        poll_err_count[tid] = poll_err_count.get(tid, 0) + 1
        t["last_poll_error"] = str(e)
        log("[poll %d] 查询异常(%d 次)，保持同一 taskId 继续等待。%s" %
            (idx + 1, poll_err_count[tid], ux.friendly_error(e)))
        return None
    return {"idx": idx, "tid": tid, "seg": seg, "info": info}


def _consume_polled_batch_task(context, task, polled):
    t = task
    idx, tid, seg, info = (polled["idx"], polled["tid"],
                           polled["seg"], polled["info"])
    api_key = context["api_key"]
    poll_err_count = context["poll_err_count"]
    allow_model_fallback = context["allow_model_fallback"]
    draft = context["draft"]
    manifest, manifest_path = context["manifest"], context["manifest_path"]
    ledger_path = context["ledger_path"]
    results, pending = context["results"], context["pending"]
    log = context["log"]
    status = (info.get("status") or "").lower()
    if status in ("succeeded", "succeed", "success", "completed") and not info.get("videoUrl"):
        results[idx] = {"ok": False, "videoUrl": None, "localPath": None,
                        "error": "COMPLETED_WITHOUT_URL: 远端任务已完成但没有 videoUrl",
                        "taskId": tid, "segment_id": seg.get("id"),
                        "resume_available": False}
        if manifest is not None:
            _persist_task(manifest, manifest_path, ledger_path, t, "failed",
                          error=results[idx]["error"])
        pending.remove(t)
    elif status in ("succeeded", "succeed", "success", "completed"):
        url = info["videoUrl"]
        if manifest is not None:
            _persist_task(manifest, manifest_path, ledger_path, t, "succeeded",
                          video_url=url)
        local = None
        op = seg.get("out_path")
        if op:
            os.makedirs(os.path.dirname(os.path.abspath(op)), exist_ok=True)
            try:
                _download_completed_video_guarded(
                    api_key, tid, url, op, segment=seg, task=t)
                local = op
            except (br_client.BRError, OSError, ValueError) as download_error:
                results[idx] = {"ok": False, "videoUrl": url, "localPath": None,
                                "absPath": None, "error": "DOWNLOAD_PENDING: %s" % download_error,
                                "taskId": tid, "segment_id": seg.get("id"),
                                "resume_available": True}
                if manifest is not None:
                    _persist_task(manifest, manifest_path, ledger_path, t, "succeeded",
                                  video_url=url, download_error=str(download_error))
                pending.remove(t)
                return
        qc_task = dict(t, video_url=url)
        qc = _media_qc_guard(local, seg, draft=draft, manifest=manifest,
                             manifest_path=manifest_path, task=qc_task,
                             raise_on_failure=False) if local else None
        if qc and not qc.get("passed"):
            results[idx] = _build_media_qc_failure_result(
                url=url, local=local, qc=qc, task_id=tid, segment=seg,
                task=t, initial_model=t["initial_model"],
                selected_model=t["model"],
                fallback_reason=t.get("fallback_reason"))
            if ledger_path:
                generation_ledger.append_event(
                    ledger_path, "task_media_qc_failed", stage="video",
                    unit_id=seg.get("id"), task_id=tid,
                    handoff_fingerprint=t.get("handoff_fingerprint"),
                    take_fingerprint=results[idx].get("take_fingerprint"),
                    local_path=local)
            if manifest is not None:
                _rm.upsert_task(manifest, {
                    "stage": "video", "unit_id": seg.get("id"),
                    "handoff_fingerprint": t.get("handoff_fingerprint"),
                    "attempt": t.get("attempt", 1), "task_id": tid,
                    "model": t["model"], "status": "succeeded",
                    "take_id": results[idx].get("take_id"),
                    "take_fingerprint": results[idx].get("take_fingerprint"),
                    "media_qc_failed": True,
                })
                if manifest_path:
                    _rm.save_manifest(manifest, manifest_path)
            pending.remove(t)
            return
        log("[done %d] %s" % (idx + 1, local or url))
        ocr = _ocr_guard(local, log) if local else None
        results[idx] = {"ok": True, "videoUrl": url, "localPath": local,
                        "absPath": os.path.abspath(local) if local else None,
                        "error": None,
                         "ocr_warning": bool(ocr and ocr.get("subtitle_detected")),
                         "ocr_texts": (ocr.get("texts") if ocr else []),
                         "ocr_status": ocr.get("status") if ocr else "unavailable",
                         "ocr_available": bool(ocr and ocr.get("available")),
                         "ocr_frames_checked": ocr.get("frames_checked", 0) if ocr else 0,
                         "ocr_expected": ocr.get("expected", 0) if ocr else 0,
                         "ocr_error": ocr.get("error") if ocr else "OCR unavailable",
                        "taskId": t["task_id"],
                        "segment_id": seg.get("id"),
                        "scene_id": seg.get("scene_id"),
                        "take_id": "%s-take-01" % seg.get("id"),
                        "video_handoff_fingerprint": t.get("handoff_fingerprint"),
                        "review_status": "pending",
                        "actual_duration": (qc.get("media") or {}).get("actual_duration") if qc else None,
                        "media_qc": qc,
                        "media_qc_report": qc.get("report_path") if qc else None,
                        **_fallback_metadata(t["initial_model"], t["model"], t["fallback_reason"])}
        results[idx]["take_fingerprint"] = take_review.take_fingerprint(results[idx])
        if ledger_path:
            generation_ledger.append_event(
                ledger_path, "task_succeeded", stage="video", unit_id=seg.get("id"),
                task_id=t["task_id"], handoff_fingerprint=t.get("handoff_fingerprint"),
                video_url=url, local_path=local, take_fingerprint=results[idx]["take_fingerprint"])
        if manifest is not None:
            _rm.upsert_task(manifest, {"stage": "video", "unit_id": seg.get("id"),
                                       "handoff_fingerprint": t.get("handoff_fingerprint"),
                                       "attempt": t.get("attempt", 1), "task_id": t["task_id"],
                                        "model": t["model"], "status": "succeeded",
                                       "take_id": results[idx]["take_id"],
                                       "take_fingerprint": results[idx]["take_fingerprint"]})
            if manifest_path:
                _rm.save_manifest(manifest, manifest_path)
        pending.remove(t)
    elif status in ("failed", "error"):
        error = br_client.video_task_error(info)
        if (isinstance(error, br_client.BRVideoReferencePrivacyError) and
                _privacy_rejection_stops(seg)):
            results[idx] = _privacy_stop_result(t, seg, error)
            _persist_task(
                manifest, manifest_path, ledger_path, t, "failed",
                error=results[idx]["error"],
                provider_error=results[idx]["provider_error"],
                privacy_fallback_policy=PRIVACY_FALLBACK_STOP_POLICY,
                privacy_fallback_attempted=False)
            pending.remove(t)
        elif (isinstance(error, br_client.BRVideoReferencePrivacyError)
                and _privacy_fallback_allowed(t["model"], t["video_type"], t["ref_urls"],
                                              allow_model_fallback, segment=seg)
                and not t.get("fallback_reason")):
            _pause_batch_privacy_fallback(context, t, seg, error)
        else:
            results[idx] = {"ok": False, "videoUrl": None, "localPath": None,
                            "error": str(error),
                             **_fallback_metadata(t["initial_model"], t["model"], t["fallback_reason"])}
            _persist_task(manifest, manifest_path, ledger_path, t, "failed",
                          error=str(error))
            pending.remove(t)

def _poll_batch_tasks(tasks, segments, *, api_key, manifest, manifest_path,
                      ledger_path, draft, allow_model_fallback, max_wait, log,
                      lease_stage=None, lease_owner=None):
    # ---- phase 2: poll all live tasks together until each finishes ----
    results = [None] * len(segments)
    import time as _t
    pending = [t for t in tasks if t["task_id"]]
    # Poll transport failures do not mean the paid remote task failed. Keep the
    # same task pending until max_wait; only provider terminal status can fail it.
    poll_err_count = {}
    for t in tasks:
        if not t["task_id"]:
            if t.get("terminal_result"):
                results[t["idx"]] = dict(t["terminal_result"])
                continue
            results[t["idx"]] = {"ok": False, "videoUrl": None, "localPath": None,
                                  "error": t["error"],
                                  "needs_confirmation": bool(
                                      t.get("needs_confirmation")),
                                  "delivery_blocked": bool(
                                      t.get("delivery_blocked")),
                                  "provider_error": t.get("provider_error"),
                                  "fallback_preparation_error": t.get(
                                      "fallback_preparation_error"),
                                  "fallback_target_model": t.get(
                                      "fallback_target_model"),
                                  "billing_status": t.get("billing_status"),
                                  "action": t.get("action"),
                                  **_fallback_metadata(t["initial_model"], t["model"], t["fallback_reason"])}
    for idx, item in enumerate(results):
        if item is None:
            continue
        task = tasks[idx] if idx < len(tasks) else None
        if task:
            item.update(_submission_prompt_snapshot_fields(source=task))
    waited, interval = 0, 8
    poll_context = dict(locals())
    while pending and waited <= max_wait:
        for task in list(pending):
            polled = _poll_batch_task_info(poll_context, task)
            if polled is not None:
                _consume_polled_batch_task(poll_context, task, polled)
        if pending:
            if not draft:
                _heartbeat_formal_generation(
                    manifest, manifest_path, lease_stage, lease_owner)
            _t.sleep(interval)
            waited += interval
            completed = len(segments) - len(pending)
            log("[poll] %ss, %d/%d 仍在生成… %s" %
                (waited, len(pending), len(segments),
                 ux.progress_hint("poll", current=min(completed + 1, len(segments)), total=len(segments))))
    for t in pending:  # timed out
        results[t["idx"]] = {"ok": False, "videoUrl": None, "localPath": None,
                              "error": "timeout after %ss" % max_wait,
                              "taskId": t["task_id"], "resume_available": True,
                              "last_poll_error": t.get("last_poll_error"),
                              "segment_id": t["segment"].get("id"),
                              "video_handoff_fingerprint": t.get("handoff_fingerprint"),
                              **_fallback_metadata(t["initial_model"], t["model"], t["fallback_reason"])}
        if ledger_path:
            generation_ledger.append_event(
                ledger_path, "task_timed_out", stage="video", unit_id=t["segment"].get("id"),
                task_id=t["task_id"], handoff_fingerprint=t.get("handoff_fingerprint"))
    
    return results


def render_batch(segments, model=None, verbose=True, seed=None,
                 negative_prompt=None, client=None, locked_refs=None,
                 allow_model_fallback=True, manifest=None, manifest_path=None,
                 ledger_path=None, results_out=None, allow_ocr_warning=False,
                 draft=False, max_wait=3600, prompt_review=None, budget_cap=None,
                 formal_stage="video"):
    """Render multiple segments as a PARALLEL async workflow.

    seed：整批共用同一 seed，透传给模型。⚠️ 实测 kling-v3-omni-video 不真正锁定
      画面（同 seed 两条 SSIM≈0.59，差异明显）——seed 对 kling 无一致性效果，
      仅对声明支持 seed 的模型（如部分 seedance/wan）可能生效。**跨段人物一致性
      不要依赖 seed**，应靠同一张 confirmed 参考图(type4)或首尾帧串联(type3)。
      参数保留（无害、不报错），每段可用 seg['seed'] 覆盖。
    negative_prompt：默认压制畸形/糊/多手指等 AI 通病，提升成片干净度。

    IMPORTANT — this is NOT 'video first, then dub'. kling-v3-omni-video is an
    audio+video (音画一体) model: each call takes the spoken script as `text` and
    returns a finished clip WITH voiceover and lip-sync in one shot. There is no
    separate TTS/dub step.

    Because createVideo is async (returns a taskId), all segments are SUBMITTED
    first (fired concurrently), then polled together — so a 3-segment interview
    renders in ~1 clip's wall-time, not 3x. Serial rendering is the wrong model.

    模型选择：走 _pick_video_model 的实时能力链。若当前选中 Kling/非原生故事板
    路径，preflight 会先要求已通过 QA 和客户确认的单格展开图；不会在已提交
    其他付费任务后才发现该缺口。含台词段也不得回落到不支持音画一体的模型。
    每段也可用 seg['model'] 单独指定。视频不做两遍清洗。

    segments: list of dicts, each:
      {text, video_type, urls, resolution, ratio, duration, out_path, model?}
    Returns list of {ok, videoUrl, localPath, error} aligned to input order.
    """
    if negative_prompt is not None or seed is not None:
        normalized_segments = []
        for segment in segments:
            updated = dict(segment)
            if negative_prompt is not None:
                updated["negative_prompt"] = negative_prompt
            if seed is not None:
                updated["seed"] = seed
            normalized_segments.append(updated)
        segments = normalized_segments
    effective_negative_prompt = (
        DEFAULT_NEGATIVE if negative_prompt is None else negative_prompt)
    key_setup.ensure_session_id()
    _validate_generation_contracts(segments, formal=not draft)
    if draft and (manifest is not None or manifest_path):
        raise ValueError(
            "DRAFT_MANIFEST_MUTATION_BLOCKED: 草稿不得读取或修改正式 run manifest")
    if not draft and not allow_model_fallback:
        raise ValueError(
            "FORMAL_MODEL_FALLBACK_REQUIRED: 正式流程不得关闭能力感知的模型降级")
    if not prompt_review:
        raise ValueError(
            "PROMPT_REVIEW_REQUIRED: 生视频前必须提供已确认的逐模型完整提示词审核文件")
    if locked_refs:
        segments = _apply_locked_refs(segments, locked_refs)
    _require_confirmed_prompt_review(prompt_review, "video", segments)
    if not draft:
        if formal_stage not in ("video", "test_segment"):
            raise ValueError("FORMAL_VIDEO_STAGE_INVALID: %s" % formal_stage)
        if allow_ocr_warning:
            raise ValueError(
                "FORMAL_OCR_GLOBAL_WAIVER_BLOCKED: 正式流程必须按 segment/take/OCR texts 登记精确 waiver")
        if formal_stage == "test_segment" and len(segments) != 1:
            raise ValueError("TEST_SEGMENT_REQUIRES_EXACTLY_ONE_SEGMENT")
        if not client or manifest is None or not manifest_path or not results_out:
            raise ValueError("FORMAL_VIDEO_REQUIRES_CLIENT_MANIFEST: batch 正式入口需要 client、manifest、results_out")
        if ledger_path and os.path.isfile(ledger_path):
            _rm.reconcile_tasks_from_ledger(manifest, ledger_path)
            _rm.save_manifest(manifest, manifest_path)
        _rm.identity_gate(manifest, client=client)
        _rm.generation_gate(manifest, formal_stage, client=client)
        _rm.validate_pre_video_gates(manifest, prompt_review)
        _manifest_handoff_matches(
            manifest, segments, allow_subset=formal_stage == "test_segment")
        current_generation = (manifest.get("generation") or {}).get(formal_stage) or {}
        if current_generation.get("status") in ("pending_approval", "approved"):
            raise ValueError("%s_ALREADY_RENDERED_OR_APPROVED" % formal_stage.upper())
        # 结构化日志上下文：run_id 贯穿本批次所有事件（trace_id）
        try:
            obs_log.configure(client=client, run_id=manifest.get("run_id"),
                              run_dir=_rm.run_output_root(manifest))
        except (OSError, ValueError, TypeError, RuntimeError):
            pass  # 日志上下文失败不阻断业务
        if not all(seg.get("out_path") for seg in segments):
            raise ValueError("OUT_PATH_REQUIRED: 正式 batch 每段必须提供 out_path")
        _validate_references(segments, client, manifest)
        _validate_reference_handoff(segments)
        import storyboard_enhancements
        stale = storyboard_enhancements.stale_artifacts(segments)
        if not stale["ok"]:
            raise ValueError("STALE_ARTIFACTS_BLOCK_DELIVERY: %s" % json.dumps(
                stale["stale"], ensure_ascii=False))
    api_key = key_setup.load_key()
    if not api_key:
        raise br_client.BRError(
            "No API key. Use the host secure credential input and non-echoing "
            "key onboarding; never send a key in ordinary chat.")

    def log(m):
        if verbose:
            print(m, flush=True)

    # One immutable live-directory snapshot governs model selection, preflight
    # and every paid submit/fallback in this batch.  This avoids both repeated
    # lookups and route-capability drift between approval and submission.
    submission_catalog = _model_catalog("video")
    # 默认模型：走降级兜底
    default_model = _pick_video_model(
        model or DEFAULT_MODEL, allow_fallback=allow_model_fallback,
        catalog=submission_catalog)
    if model and default_model != model:
        log("[batch] 默认模型 %s 不可用，降级到 %s" % (model, default_model))
    proven_audio_models = _rm.approved_video_audio_canary_models(manifest) if not draft and formal_stage == "video" else ()
    preflight = inspect_submission_requirements(
        segments, default_model, formal=True,
        allow_model_fallback=allow_model_fallback,
        catalog=submission_catalog,
        allow_unproven_audio_canary=False,
        proven_audio_models=proven_audio_models,
        manifest=manifest, formal_stage=formal_stage)
    if not preflight["ready"]:
        raise ValueError(
            "VIDEO_PREFLIGHT_BLOCKED: %s" %
            json.dumps(preflight["blockers"], ensure_ascii=False))
    if not draft and formal_stage == "video":
        _rm.validate_video_audio_canary(manifest, preflight)
    _validate_storyboard_revisions(segments)
    _enforce_batch_budget(
        segments, default_model, budget_cap, log,
        manifest=manifest, preflight_ready=preflight["ready"],
        strict=not draft)
    if not draft:
        lease_owner = _video_generation_owner_token()
        acquired = _rm.acquire_generation_lease(
            manifest_path, formal_stage, lease_owner, ttl_seconds=300)
        manifest.clear()
        manifest.update(acquired)
    else:
        lease_owner = None

    obs_log.log_event("batch_start", segments=len(segments),
                      default_model=default_model, draft=draft)

    # 共享锚定素材锁（跨段固定人物/产品/场景，只变台词/剧本/运镜）
    if locked_refs:
        log("[batch] 已锁定 %d 张共享锚定参考图注入每段（固定素材，只变台词/运镜）" % len(locked_refs))

    tasks = _submit_batch_tasks(
        segments, api_key=api_key, client=client, manifest=manifest,
        manifest_path=manifest_path, ledger_path=ledger_path,
        default_model=default_model, draft=draft, seed=seed,
        negative_prompt=effective_negative_prompt,
        allow_model_fallback=allow_model_fallback, log=log,
        allow_unproven_audio=False,
        proven_audio_models=proven_audio_models,
        formal_stage=formal_stage,
        model_catalog=submission_catalog)
    results = _poll_batch_tasks(
        tasks, segments, api_key=api_key, manifest=manifest,
        manifest_path=manifest_path, ledger_path=ledger_path, draft=draft,
        allow_model_fallback=allow_model_fallback, max_wait=max_wait, log=log,
        lease_stage=formal_stage, lease_owner=lease_owner)
    finalized = _finalize_batch_results(
        results, tasks, segments,
        allow_ocr_warning=allow_ocr_warning,
        results_out=results_out, draft=draft, log=log)
    if not draft and formal_stage == "video":
        # One settlement transaction owns every terminal batch outcome.  It
        # clears the lease and routes success to take review, recoverable
        # remote tasks to recovery, and privacy rejection to re-approval.
        closed, _settlement = _rm.record_video_generation_settlement(
            manifest_path, results_out, finalized)
        manifest.clear()
        manifest.update(closed)
    elif not draft:
        _settle_formal_test_segment_results(
            manifest, manifest_path, results_out, finalized)
    return finalized


def main(argv):
    import video_engine_cli
    return video_engine_cli.main(sys.modules[__name__], argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        code = str(exc).split(":", 1)[0] or "VIDEO_WORKFLOW_BLOCKED"
        print("ERROR:%s\n下一步：请查看 run 状态、完成缺失审批或补齐素材后重试。\n详情：%s" %
              (code, exc), file=sys.stderr)
        raise SystemExit(2)
