#!/usr/bin/env python3
"""Video quality guards: OCR, media QC, reference validation.

Extracted from video_engine.py (v3 split). Contains:
  - _ocr_guard (OCR subtitle residue detection, macOS Vision)
  - _media_qc_guard (ffprobe-based QC gate)
  - _validate_references (reference image handoff validation)
  - _validate_reference_handoff (handoff fingerprint verification)
  - _validate_model_reference_capacity (model image count limits)

Dependencies: ocr_check (optional), media_qc, artifact_contract
"""
import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import artifact_contract
import media_qc
import run_manifest as _rm
from project_utils import atomic_write_json


def _ocr_guard(local_path, log):
    """Run OCR subtitle residue detection after video download.

    Detects text → prints [OCR_WARNING] for agent decision.
    Unavailable (non-Mac/missing deps) → silently skip.

    Returns structured result for batch/chained results (铁律#9):
      {"status": "clear"|"detected"|"error"|"unavailable",
       "available": bool, "subtitle_detected": bool,
       "texts": [str...], "frames_checked": int}
    or None on exception.
    """
    try:
        import ocr_check
    except ImportError:
        return None
    try:
        report = ocr_check.check_video(local_path, n_frames=5)
    except (OSError, ValueError, RuntimeError) as e:
        log("[OCR] 检测异常，已跳过：%s" % e)
        return None

    if not report.get("ocr_available"):
        log("[OCR] 不可用（%s），跳过字幕检测。" % report.get("error", ""))
        return {"status": "unavailable", "available": False, "subtitle_detected": False,
                "texts": [], "frames_checked": 0, "expected": 0,
                "error": report.get("error")}
    if report.get("error"):
        log("[OCR] 检测出错（%s），跳过。" % report["error"])
        return {"status": "error", "available": False, "subtitle_detected": False,
                "texts": [], "frames_checked": 0, "expected": report.get("expected", 0),
                "error": report.get("error")}
    texts = []
    if report.get("subtitle_detected"):
        log("[OCR_WARNING] subtitle_detected — 成片 %s 检出画面文字，"
            "疑似字幕残留，建议重新生成！" % os.path.basename(local_path))
        for d in report.get("detections", []):
            for t in d.get("texts", []):
                log("  帧 %s | 置信度 %.2f | 文字：%s" % (
                    d["frame"], t["confidence"], t["text"]))
                texts.append(t["text"])
    else:
        log("[OCR] OK — 抽检 %d 帧，未检出画面文字。" % report.get("frames_checked", 0))
    return {"status": "detected" if report.get("subtitle_detected") else "clear",
            "available": True, "subtitle_detected": bool(report.get("subtitle_detected")),
            "texts": texts, "frames_checked": report.get("frames_checked", 0),
            "expected": report.get("expected", report.get("frames_checked", 0)),
            "error": None}


def _reference_is_trusted(client, value, manifest=None):
    """Return True only for confirmed local references or approved generated URLs."""
    try:
        import asset_prep as _ap
        if _ap.is_confirmed(client, value):
            return True
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if (not isinstance(value, str) or not os.path.isfile(value) or
            os.path.islink(value)):
        return False
    path = os.path.abspath(value)

    # A formally confirmed actor (including a separately confirmed outfit) is
    # an identity library asset, not an asset_prep brief image. Re-resolve the
    # actor so current portrait/ref hashes and customer evidence are checked.
    try:
        import digital_human as _dh
        actor_root = os.path.abspath(os.path.join(_dh.ACTORS_DIR, client))
        if os.path.commonpath((path, actor_root)) == actor_root:
            relative = os.path.relpath(path, actor_root).split(os.sep)
            if relative:
                actor = relative[0]
                outfit = (relative[2] if len(relative) >= 4 and
                          relative[1] == "outfits" else None)
                resolved = _dh.resolve(client, actor, outfit=outfit)
                trusted = [resolved.get("portrait")] + list(resolved.get("refs") or [])
                if path in {os.path.abspath(item) for item in trusted if item}:
                    return True
    except (OSError, SystemExit, ValueError, TypeError):
        pass

    # Product-library hero/view/board files carry their own evidence markers.
    try:
        import product_library as _pl
        product_root = os.path.abspath(
            os.path.join(_pl.ASSETS_DIR, client, "product"))
        if os.path.commonpath((path, product_root)) == product_root:
            relative = os.path.relpath(path, product_root).split(os.sep)
            if relative:
                resolved = _pl.resolve(client, relative[0])
                trusted = [resolved.get("hero")] + list(resolved.get("refs") or [])
                if resolved.get("product_board_confirmed"):
                    trusted.append(resolved.get("product_board"))
                if path in {os.path.abspath(item) for item in trusted if item}:
                    return True
    except (OSError, SystemExit, ValueError, TypeError):
        pass

    if manifest:
        for record in (manifest.get("generation", {}).get("storyboard", {}).get("artifacts") or []):
            if os.path.abspath(record.get("path", "")) == path and record.get("exists"):
                return _rm.approval_is_current(manifest, "storyboard")
    return False


def _trusted_remote_segment_reference(segment, value, manifest=None):
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    trusted_sources = {
        "storyboard",
        "asset_refs.product_usage_images",
        "asset_refs.cast_boards",
        "asset_refs.product_boards",
    }
    for ref in segment.get("references") or []:
        if not isinstance(ref, dict) or ref.get("url") != value:
            continue
        if ref.get("source") == "identity_plate.approved_candidate":
            state = (manifest or {}).get("identity_plate_generation") or {}
            artifact = segment.get("identity_plate_artifact") or {}
            return bool(
                value.startswith("https://") and manifest and
                segment.get("client") == manifest.get("client") and
                str(segment.get("run_id")) == str(manifest.get("run_id")) and
                value == state.get("remote_url") == artifact.get("remote_url") and
                artifact.get("approved_local_artifact") == state.get("output") and
                artifact.get("generation_result") == state.get("generation_result") and
                _rm.identity_plate_generation_is_complete(manifest))
        if ref.get("source") in trusted_sources:
            return True
    return False


def _media_qc_guard(local_path, segment, *, draft, manifest=None,
                    manifest_path=None, task=None, raise_on_failure=True):
    """Run ffprobe-based media QC gate and persist the audit trail."""
    audio_contract = segment.get("audio_contract") or {}
    report_path = os.path.abspath(local_path) + ".qc.json"
    check_raised = False
    output_contract = segment.get("output_artifact_contract") or {}
    approved_canary = (manifest or {}).get("test_segment_artifact") or {}
    canary_report = approved_canary.get("media_qc") or {}
    reuse_approved_canary_qc = bool(
        not draft and
        output_contract.get("mode") == "approved_canary_exact_media_reuse" and
        str(approved_canary.get("segment_id") or "") ==
        str(segment.get("id") or "") and
        approved_canary.get("status") == "approved" and
        canary_report.get("passed") is True and
        output_contract.get("path") == os.path.abspath(local_path) and
        output_contract.get("sha256") ==
        artifact_contract.file_sha256(os.path.abspath(local_path)) and
        output_contract.get("take_fingerprint") ==
        approved_canary.get("take_fingerprint") and
        _rm.file_record_is_current(approved_canary.get("video") or {}))
    if reuse_approved_canary_qc:
        # The exact paid canary bytes already have approved formal QC. Reusing
        # that immutable evidence avoids rewriting its timestamped .qc.json,
        # which would otherwise make the customer's test-segment approval
        # stale during an unrelated segment retry.
        report = copy.deepcopy(canary_report)
        report_path = str(report.get("report_path") or report_path)
    else:
        try:
            report = media_qc.check(
                local_path, profile="draft" if draft else "formal",
                expected_duration=segment.get("duration"), expected_ratio=segment.get("ratio"),
                audio_required=audio_contract.get("track") == "required",
                report_path=report_path)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            if not draft and raise_on_failure:
                raise ValueError("MEDIA_QC_ERROR: %s" % exc)
            report = {
                "passed": False, "draft": bool(draft), "error": str(exc),
                "errors": ["MEDIA_QC_ERROR"], "media": {},
            }
            check_raised = True
    report["audio_contract"] = audio_contract
    report["audio_contract_fingerprint"] = artifact_contract.sha256_json(audio_contract)
    report["semantic_audio_review_required"] = [
        key for key in ("voice", "language", "bgm", "sfx", "lip_sync")
        if audio_contract.get(key) not in (None, "", False)]
    report["report_path"] = report_path
    if check_raised:
        atomic_write_json(report_path, report)
    if manifest is not None:
        segment_id = str(segment.get("id") or "unknown")
        manifest.setdefault("media_qc", {}).setdefault("video", {})[segment_id] = report
        if task is not None:
            from video_tasks import _persist_task as _persist_video_task
            _persist_video_task(
                manifest, manifest_path, None, task, "succeeded",
                video_url=task.get("video_url"), media_qc=report,
                media_qc_passed=bool(report.get("passed")),
                actual_duration=(report.get("media") or {}).get("actual_duration"))
        elif manifest_path:
            _rm.save_manifest(manifest, manifest_path)
    if not report.get("passed") and not draft and raise_on_failure:
        raise ValueError("MEDIA_QC_FAILED: %s" % report.get("errors", []))
    return report


def _validate_references(segments, client, manifest, draft=False,
                         reference_is_trusted=None,
                         trusted_remote_segment_reference=None):
    """Validate that all reference images are confirmed and current."""
    if draft:
        return
    reference_is_trusted = reference_is_trusted or _reference_is_trusted
    trusted_remote_segment_reference = (
        trusted_remote_segment_reference or _trusted_remote_segment_reference)
    for seg in segments:
        refs = list(seg.get("urls") or [])
        product_refs = set()
        if seg.get("product_sku"):
            try:
                import product_library as _pl
                resolved = _pl.resolve(client, seg["product_sku"])
                product_refs.update(os.path.abspath(p) for p in
                                    ([resolved.get("hero")] + list(resolved.get("refs") or [])) if p)
                if resolved.get("product_board_confirmed") and resolved.get("product_board"):
                    product_refs.add(os.path.abspath(resolved["product_board"]))
            except (ImportError, OSError, ValueError, TypeError, KeyError) as error:
                raise ValueError("PRODUCT_SKU_RESOLVE_FAILED: %s (%s)" %
                                 (seg["product_sku"], error))
        bad = [ref for ref in refs
               if not trusted_remote_segment_reference(seg, ref, manifest)
               and not reference_is_trusted(client, ref, manifest)
               and (not isinstance(ref, str) or not os.path.isfile(ref)
                    or os.path.abspath(ref) not in product_refs)]
        if bad:
            raise ValueError("UNTRUSTED_VIDEO_REFERENCE: %s" % ", ".join(map(str, bad)))


def _validate_reference_handoff(segments):
    """Verify each segment has a video_handoff_fingerprint from the storyboard."""
    missing = [seg.get("id", "?") for seg in segments
               if not seg.get("video_handoff_fingerprint")]
    if missing:
        raise ValueError(
            "VIDEO_HANDOFF_FINGERPRINT_MISSING: segments %s 缺少故事板确认指纹"
            % ", ".join(missing))


def _validate_model_reference_capacity(model, reference_count, *, formal,
                                       model_catalog=None):
    """Check if the model supports the given number of reference images."""
    if not formal:
        return
    catalog = model_catalog
    if callable(catalog):
        catalog_payload = catalog("video")
    elif isinstance(catalog, dict):
        catalog_payload = catalog
    else:
        import video_models as _video_models
        catalog_payload = _video_models._model_catalog("video")
    import video_models as _video_models
    model_id = (_video_models._resolve_catalog_model(model, catalog_payload)
                or model)
    record = (catalog_payload.get("records", {}).get(model_id) or {})
    max_count = record.get("image_count")
    if max_count is not None and reference_count > max_count:
        raise ValueError(
            "REFERENCE_COUNT_UNSUPPORTED: model %s supports at most %s reference image(s), "
            "but this confirmed handoff requires %s. No reference may be dropped or merged "
            "automatically; choose a model/endpoint whose declared capacity satisfies the "
            "confirmed handoff." % (model, max_count, reference_count))
