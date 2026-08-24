#!/usr/bin/env python3
"""Immutable delivery validation independent from routing/orchestration.

The router may ask whether a delivery can be created, while ``pipeline`` owns
the customer-facing command.  Keeping the content-bound checks here prevents
those two modules from importing each other and makes the release gate a single
source of truth.
"""
import json
import os
from datetime import datetime

import cost_ledger
import run_manifest as rm
import script_splitter
import take_review
from artifact_contract import sha256_json
from project_utils import atomic_write_json


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def cost_summary(manifest):
    """Best-effort cost estimation; delivery validity never depends on it."""
    try:
        run_dir = manifest.get("run_dir") or os.path.dirname(
            manifest.get("manifest_path", ""))
        ledger_path = os.path.join(run_dir, "generation_runs.jsonl")
        if os.path.isfile(ledger_path):
            return cost_ledger.estimate_run_cost(ledger_path)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    return None


def _validate_derived_outputs(manifest):
    derive = (manifest.get("generation") or {}).get("derive") or {}
    if derive.get("status") == "skipped":
        rm.validate_derive_closure(manifest)
        return
    if derive.get("status") is None:
        return
    if not rm.approval_is_current(manifest, "derive"):
        raise ValueError("DELIVERY_DERIVE_APPROVAL_REQUIRED_OR_STALE")
    rm.validate_derive_closure(manifest)
    details = derive.get("derived_details") or []
    if not details:
        raise ValueError("DELIVERY_DERIVE_OUTPUTS_REQUIRED")
    for item in details:
        output = item.get("path")
        current = rm.file_record(output) if output else None
        if (not current or not current.get("exists") or
                current.get("sha256") != item.get("sha256") or
                not (item.get("media_qc") or {}).get("passed")):
            raise ValueError("DELIVERY_DERIVE_QC_REQUIRED_OR_STALE")


def _validate_final_output(manifest):
    records = rm._stage_artifacts(manifest, "final", refresh=True)
    final_qc = manifest.get("delivery_qc") or {}
    qc_file = final_qc.get("file") or {}
    matches = [record for record in records
               if record.get("path") == qc_file.get("path")]
    if len(matches) != 1 or not matches[0].get("exists"):
        raise ValueError("DELIVERY_FINAL_FILE_REQUIRED")
    final_record = matches[0]
    if (final_qc.get("profile") != "formal" or not final_qc.get("passed") or
            qc_file.get("path") != final_record.get("path") or
            qc_file.get("sha256") != final_record.get("sha256")):
        raise ValueError("DELIVERY_FINAL_FORMAL_QC_REQUIRED_OR_STALE")
    disclosure = manifest.get("disclosure") or {}
    if disclosure.get("applied"):
        alpha_path = disclosure.get("alpha_path")
        alpha_record = rm.file_record(alpha_path) if alpha_path else None
        if not alpha_record or alpha_record.get("sha256") != disclosure.get("alpha_sha256"):
            raise ValueError("DELIVERY_DISCLOSURE_ARTIFACT_STALE")
    return final_record


def _load_caption_artifact(manifest):
    caption_path = (manifest.get("caption_artifact") or {}).get("path")
    if not caption_path or not os.path.isfile(caption_path):
        raise ValueError("DELIVERY_CAPTION_ARTIFACT_REQUIRED")
    artifact = _load(caption_path)
    script_splitter.caption_artifact_is_current(
        manifest, artifact, client=manifest.get("client"), require_approved=True)
    return caption_path, artifact


def _formal_review_is_current(review):
    decision = review.get("decision") or {}
    quality = review.get("quality") or {}
    technical = quality.get("technical") or {}
    marketing = quality.get("marketing") or {}
    score = quality.get("overall_score")
    return bool(
        decision.get("acceptance_mode") == "formal" and
        review.get("review_sources") and
        all(technical.get(key) is True for key in take_review.TECHNICAL_REQUIRED) and
        all(
            marketing.get(key) is True or
            (not isinstance(marketing.get(key), bool) and
             isinstance(marketing.get(key), (int, float)) and
             80 <= marketing.get(key) <= 100)
            for key in take_review.MARKETING_REQUIRED
        ) and
        isinstance(score, (int, float)) and not isinstance(score, bool) and
        80 <= score <= 100 and take_review.artifact_is_current(review)
    )


def _validate_accepted_takes(manifest):
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    artifact = manifest.get("video_artifact") or {}
    for name in ("segments", "results", "basecut", "reviews"):
        if not rm.file_record_is_current(artifact.get(name)):
            raise ValueError("DELIVERY_VIDEO_ARTIFACT_STALE: %s" % name)
    raw_results = _load(artifact["results"]["path"])
    results = raw_results.get("results") if isinstance(raw_results, dict) else raw_results
    result_by_id = {str(item.get("segment_id")): item for item in results or []}
    segment_ids = set((handoff.get("segments") or {}).keys())
    accepted = manifest.get("accepted_takes") or {}
    if not segment_ids or set(accepted.keys()) != segment_ids:
        raise ValueError("DELIVERY_ACCEPTED_TAKES_INCOMPLETE")
    review_summary = _load(artifact["reviews"]["path"])
    if (not isinstance(review_summary, dict) or
            set(map(str, review_summary.keys())) != segment_ids):
        raise ValueError("DELIVERY_REVIEWS_SUMMARY_MISMATCH")

    takes = {}
    for segment_id in sorted(segment_ids):
        accepted_take = accepted[segment_id]
        result = result_by_id.get(segment_id) or {}
        if not (result.get("media_qc") or {}).get("passed"):
            raise ValueError("DELIVERY_MEDIA_QC_FAILED: %s" % segment_id)
        review_record = accepted_take.get("review") or {}
        if not rm.file_record_is_current(review_record):
            raise ValueError("DELIVERY_REVIEW_STALE_OR_MISSING: %s" % segment_id)
        review = _load(review_record["path"])
        summarized = review_summary.get(segment_id)
        if (not isinstance(summarized, dict) or
                summarized.get("review_id") != review.get("review_id") or
                (summarized.get("artifact") or {}).get("take_fingerprint") !=
                (review.get("artifact") or {}).get("take_fingerprint")):
            raise ValueError("DELIVERY_REVIEWS_SUMMARY_MISMATCH: %s" % segment_id)
        take_fingerprint = accepted_take.get("take_fingerprint")
        if (not take_review.is_accepted(review, take_fingerprint) or
                (review.get("artifact") or {}).get("video_handoff_fingerprint") !=
                (handoff.get("segments") or {}).get(segment_id) or
                not _formal_review_is_current(review)):
            raise ValueError("DELIVERY_TAKE_QC_FAILED: %s" % segment_id)
        if not rm.ocr_take_is_clear_or_waived(
                manifest, segment_id, take_fingerprint):
            raise ValueError("DELIVERY_OCR_FAILED: %s" % segment_id)
        takes[segment_id] = {
            "take_fingerprint": take_fingerprint,
            "handoff": handoff["segments"][segment_id],
            "review": review_record,
            "ocr": (manifest.get("ocr_checks") or {}).get(segment_id),
            "waiver": (manifest.get("ocr_waivers") or {}).get(segment_id),
        }
    return handoff, artifact, takes


def delivery_snapshot(manifest):
    """Validate every release gate and return its content-bound identity."""
    rm.identity_gate(manifest)
    if not rm.approval_is_current(manifest, "final"):
        raise ValueError("DELIVERY_FINAL_APPROVAL_REQUIRED_OR_STALE")
    rm.validate_final_closure(manifest)
    if manifest.get("requires_shotcraft_packaging"):
        if not rm.approval_is_current(manifest, "shotcraft_packaging"):
            raise ValueError("DELIVERY_SHOTCRAFT_APPROVAL_REQUIRED_OR_STALE")
        rm.validate_shotcraft_closure(manifest)
    _validate_derived_outputs(manifest)
    if not rm.approval_is_current(manifest, "video"):
        raise ValueError("DELIVERY_VIDEO_APPROVAL_REQUIRED_OR_STALE")
    rm.validate_video_closure(manifest)
    if not rm.approval_is_current(manifest, "captions"):
        raise ValueError("DELIVERY_CAPTION_APPROVAL_REQUIRED_OR_STALE")

    final_record = _validate_final_output(manifest)
    caption_path, caption_artifact = _load_caption_artifact(manifest)
    handoff, video_artifact, takes = _validate_accepted_takes(manifest)
    return {
        "client": manifest.get("client"),
        "run_id": manifest.get("run_id"),
        "final": final_record,
        "caption_identity": caption_artifact.get("caption_identity"),
        "caption_artifact": rm.file_record(caption_path),
        "video_handoff_sha256": handoff.get("sha256"),
        "video_artifact": video_artifact,
        "accepted_takes": takes,
        "cost_summary": cost_summary(manifest),
    }


def create_delivery(manifest, path):
    snapshot = delivery_snapshot(manifest)
    delivery = {
        "schema_version": 1,
        "status": "verified",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "delivery_identity": sha256_json(snapshot),
        **snapshot,
    }
    atomic_write_json(path, delivery)
    try:
        import performance_feedback
        delivery["performance_feedback_reminder"] = (
            performance_feedback.generate_delivery_reminder(
                manifest.get("client"), manifest.get("run_id")))
        atomic_write_json(path, delivery)
    except (ImportError, OSError, ValueError, json.JSONDecodeError):
        pass
    return delivery


def verify_delivery(manifest, path):
    delivery = _load(path)
    snapshot = delivery_snapshot(manifest)
    identity = sha256_json(snapshot)
    if delivery.get("delivery_identity") != identity:
        raise ValueError("DELIVERY_STALE: manifest 或交付文件内容已变化")
    if {key: delivery.get(key) for key in snapshot} != snapshot:
        raise ValueError("DELIVERY_CONTENT_MISMATCH")
    return {"ok": True, "delivery": os.path.abspath(path),
            "delivery_identity": identity}
