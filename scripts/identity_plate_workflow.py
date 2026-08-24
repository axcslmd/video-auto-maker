#!/usr/bin/env python3
"""Approval-bound identity-plate workflow for document explainers.

This module owns one small state machine:

reference approval -> exact local capture -> capture approval -> paid async
submission -> deterministic media QC -> visual review -> generated image approval.

No router or host adapter may manufacture customer confirmation.  The paid
function reconstructs and compares the exact approved request body immediately
before create, then records the actual request/task identity for recovery.
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import br_client
import identity_request_contract as request_contract
import identity_review_contract as review_contract
import key_setup
import run_manifest
from project_utils import atomic_write_bytes, atomic_write_json


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _context(manifest, *, require_approval=True):
    if require_approval and not run_manifest.approval_is_current(
            manifest, "render_plan"):
        raise ValueError("IDENTITY_RENDER_PLAN_APPROVAL_REQUIRED")
    generation = (manifest.get("generation") or {}).get("render_plan") or {}
    primary = generation.get("primary_artifact") or {}
    current = run_manifest.file_record(primary.get("path"))
    if (not current or not current.get("exists") or
            current.get("sha256") != primary.get("sha256")):
        raise ValueError("IDENTITY_RENDER_PLAN_STALE")
    plan = _load(current["path"])
    if (plan.get("client") != manifest.get("client") or
            str(plan.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("IDENTITY_RENDER_PLAN_IDENTITY_MISMATCH")
    identity = plan.get("identity_plate_generation") or {}
    request = identity.get("request") or {}
    if identity.get("required_before_video") is not True or not request:
        raise ValueError("IDENTITY_RENDER_PLAN_CONTRACT_REQUIRED")
    request_spec, request_fingerprint = request_contract.assert_request_matches_plan(
        request)
    return {
        "plan": plan, "plan_record": current, "request": request,
        "request_spec": request_spec,
        "request_fingerprint": request_fingerprint,
    }


def _full_marker(marker):
    marker = dict(marker)
    identity = request_contract.json_sha256(marker)
    marker["approval_artifact_sha256"] = identity
    marker["artifact_short_id"] = identity[:12]
    return marker


def _marker_path(manifest, kind, content_fingerprint, marker):
    name = "%s.%s.%s.customer-confirmed.json" % (
        kind, content_fingerprint[:12],
        marker["approval_artifact_sha256"][:12])
    return run_manifest.require_run_output(
        manifest, os.path.join(
            run_manifest.run_output_root(manifest), ".approvals", name),
        label="identity_customer_approval")


def _save_state(manifest_path, mutate, *, retries=3):
    for _ in range(max(1, int(retries))):
        manifest = run_manifest.load_manifest(manifest_path)
        revision = int(manifest.get("revision") or 0)
        result = mutate(manifest)
        try:
            run_manifest.save_manifest(
                manifest, manifest_path, expected_revision=revision)
            return result
        except run_manifest.ManifestConflictError:
            continue
    raise run_manifest.ManifestConflictError("IDENTITY_STATE_CONFLICT")


def confirm_identity_plate_reference(manifest_path, customer_confirmation):
    """Confirm the source/reference package without submitting a paid task."""
    confirmation = run_manifest.require_explicit_customer_confirmation(
        customer_confirmation, "identity_plate_reference")
    if ("身份" not in confirmation or
            not any(token in confirmation for token in ("引用", "参考"))):
        raise ValueError("IDENTITY_REFERENCE_EXPLICIT_CUSTOMER_CONFIRMATION_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)

    def mutate(manifest):
        context = _context(manifest)
        if run_manifest.identity_plate_reference_is_current(manifest):
            existing = manifest.get("identity_plate_generation") or {}
            approved_text = ((existing.get("reference_approval") or {})
                             .get("customer_confirmation"))
            if approved_text == confirmation:
                return existing
            raise ValueError("IDENTITY_REFERENCE_ALREADY_CONFIRMED")
        request = context["request"]
        content = {
            "render_plan": context["plan_record"],
            "planned_request_fingerprint": context["request_fingerprint"],
            "planned_request_spec": context["request_spec"],
            "source_references": request.get("source_references") or [],
            "catalog_contract": request.get("catalog_contract") or {},
        }
        fingerprint = request_contract.json_sha256(content)
        marker = _full_marker({
            "schema_version": 1,
            "kind": "identity_plate_reference_customer_approval",
            "status": "confirmed", "client": manifest["client"],
            "run_id": manifest["run_id"], "confirmed_by_customer": True,
            "customer_confirmation": confirmation,
            "confirmation_source": "customer_dialogue",
            "content": content, "content_fingerprint": fingerprint,
            "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        })
        path = _marker_path(
            manifest, "identity_plate_reference", fingerprint, marker)
        atomic_write_json(path, marker)
        approval = dict(
            marker, approval_path=path,
            marker_sha256=run_manifest.file_record(path)["sha256"])
        state = {
            "status": "reference_confirmed_pending_submission_capture",
            "render_plan": context["plan_record"],
            "planned_request_fingerprint": context["request_fingerprint"],
            "reference_approval": approval,
        }
        manifest["identity_plate_generation"] = state
        manifest["status"] = "needs_identity_plate_submission_capture"
        return state

    return _save_state(manifest_path, mutate)


def _live_catalog_contract(request, *, api_key=None):
    api_key = api_key or key_setup.require_key(
        "IDENTITY_CAPTURE_BASICROUTER_KEY_REQUIRED")
    try:
        catalog = br_client.list_image_models(api_key)
    except (br_client.BRError, OSError, ValueError, RuntimeError, TypeError) as exc:
        if key_setup.is_auth_rejection(exc):
            key_setup.reject_key(api_key)
            error = key_setup.SessionKeyRequired(
                "IDENTITY_CAPTURE_BASICROUTER_KEY_INVALID")
            error.client_message = key_setup.invalid_key_message()
            raise error from exc
        raise
    return request_contract.catalog_contract(request, catalog)


def _capture_preview(packet):
    payload = packet["payload"]
    refs = packet["request_spec"]["image_references"]
    lines = [
        "# 身份锚图精确提交包（待客户确认）", "",
        "这一步只捕获请求，不会调用生成模型，也不会产生图片费用。", "",
        "- 模型：`%s`" % payload["model"],
        "- 画幅：`%s`" % payload["ratio"],
        "- 分辨率：`%s`" % payload["resolution"],
        "- 生成数量：`%s`" % payload["count"],
        "- 完整提交指纹：`%s`" % packet["payload_fingerprint"],
        "- 实时目录指纹：`%s`" % packet["catalog_fingerprint"], "",
        "## 将要提交的提示词", "", payload["text"], "",
        "## 引用图", "",
    ]
    for index, item in enumerate(refs, start=1):
        lines.extend([
            "%d. `%s`" % (index, item["source_path"]),
            "   - 文件 SHA-256：`%s`" % item["source_sha256"],
            "   - imageUrl SHA-256：`%s`" % item["data_url_sha256"],
        ])
    lines.extend([
        "", "确认本文件只授权以上精确请求；模型、画幅、分辨率、提示词、"
        "引用图或实时规格任一变化，都必须重新捕获并确认。", "",
    ])
    return "\n".join(lines)


def _persist_immutable_capture(packet, out_path, preview_out):
    """Write once, or reuse only byte-equivalent capture semantics."""
    write_packet = False
    if os.path.isfile(out_path):
        existing = _load(out_path)
        existing_contract = {key: value for key, value in existing.items()
                             if key != "captured_at"}
        incoming_contract = {key: value for key, value in packet.items()
                             if key != "captured_at"}
        if existing_contract != incoming_contract:
            raise ValueError(
                "IDENTITY_CAPTURE_IMMUTABLE_OUTPUT_PATH_REQUIRED: use a new revision path")
        packet = existing
    elif os.path.exists(out_path):
        raise ValueError("IDENTITY_CAPTURE_OUTPUT_NOT_REGULAR_FILE")
    else:
        write_packet = True
    preview_bytes = (_capture_preview(packet) + "\n").encode("utf-8")
    write_preview = False
    if os.path.isfile(preview_out):
        with open(preview_out, "rb") as handle:
            if handle.read() != preview_bytes:
                raise ValueError(
                    "IDENTITY_CAPTURE_IMMUTABLE_PREVIEW_PATH_REQUIRED: use a new revision path")
    elif os.path.exists(preview_out):
        raise ValueError("IDENTITY_CAPTURE_PREVIEW_NOT_REGULAR_FILE")
    else:
        write_preview = True
    if write_packet:
        atomic_write_json(out_path, packet)
    if write_preview:
        atomic_write_bytes(
            preview_out, preview_bytes, label="identity_submission_preview")
    return packet


def capture_identity_plate_submission(manifest_path, out_path, preview_out):
    """Persist JSON + Markdown for exact review; never calls image create."""
    # Credential readiness is a no-side-effect preflight.  In particular, do
    # not write a pending capture or alter the run when this task/session has
    # not been authorized.
    api_key = key_setup.require_key("IDENTITY_CAPTURE_BASICROUTER_KEY_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest.load_manifest(manifest_path)
    if not run_manifest.identity_plate_reference_is_current(manifest):
        raise ValueError("IDENTITY_CAPTURE_REFERENCE_CONFIRMATION_REQUIRED")
    context = _context(manifest)
    catalog = _live_catalog_contract(context["request"], api_key=api_key)
    payload = request_contract.submission_payload(context["request"])
    packet = {
        "schema_version": 1, "kind": "identity_plate_submission_capture",
        "status": "pending_customer_confirmation", "local_only": True,
        "client": manifest["client"], "run_id": manifest["run_id"],
        "render_plan": context["plan_record"],
        "planned_request_fingerprint": context["request_fingerprint"],
        "request_spec": context["request_spec"], "payload": payload,
        "payload_fingerprint": request_contract.json_sha256(payload),
        "catalog_contract": catalog, "catalog_fingerprint": catalog["fingerprint"],
        "source_fingerprint": context["request"].get("source_fingerprint"),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    out_path = run_manifest.require_run_output(
        manifest, os.path.abspath(out_path), label="identity_submission_packet")
    preview_out = run_manifest.require_run_output(
        manifest, os.path.abspath(preview_out), label="identity_submission_preview")
    packet = _persist_immutable_capture(packet, out_path, preview_out)
    packet_record = run_manifest.file_record(out_path)
    preview_record = run_manifest.file_record(preview_out)

    def mutate(current):
        if not run_manifest.identity_plate_reference_is_current(current):
            raise ValueError("IDENTITY_CAPTURE_REFERENCE_BECAME_STALE")
        current_context = _context(current)
        if current_context["plan_record"] != context["plan_record"]:
            raise ValueError("IDENTITY_CAPTURE_PLAN_CHANGED")
        previous = current.get("identity_plate_generation") or {}
        if previous.get("submission_approval"):
            archived = dict(previous, status="invalidated",
                            invalidated_at=datetime.now().isoformat(timespec="seconds"),
                            invalidation_reason="submission_packet_recaptured")
            current.setdefault("identity_plate_generation_history", []).append(archived)
        state = {
            "status": "submission_capture_pending_customer_confirmation",
            "render_plan": context["plan_record"],
            "planned_request_fingerprint": context["request_fingerprint"],
            "reference_approval": previous.get("reference_approval"),
            "submission_capture": {
                "packet": packet_record, "preview": preview_record,
                "payload_fingerprint": packet["payload_fingerprint"],
                "catalog_fingerprint": packet["catalog_fingerprint"],
            },
        }
        current["identity_plate_generation"] = state
        current["status"] = "needs_identity_plate_submission_approval"
        return state

    state = _save_state(manifest_path, mutate)
    return {"packet": packet_record, "preview": preview_record,
            "payload_fingerprint": packet["payload_fingerprint"], "state": state,
            "request_summary": {
                "model": packet["payload"].get("model"),
                "ratio": packet["payload"].get("ratio"),
                "resolution": packet["payload"].get("resolution"),
                "count": packet["payload"].get("count"),
                "image_url_count": len(packet["payload"].get("imageUrls") or []),
            },
            "external_calls": False}


def _validated_capture(manifest, *, require_confirmation=False,
                       revalidate_catalog=True, api_key=None):
    if not run_manifest.identity_plate_reference_is_current(manifest):
        raise ValueError("IDENTITY_CAPTURE_REFERENCE_STALE")
    if require_confirmation and not run_manifest.identity_plate_submission_is_current(
            manifest):
        raise ValueError("IDENTITY_SUBMISSION_CUSTOMER_CONFIRMATION_REQUIRED")
    context = _context(manifest)
    state = manifest.get("identity_plate_generation") or {}
    capture = state.get("submission_capture") or {}
    packet_record = capture.get("packet") or {}
    preview_record = capture.get("preview") or {}
    if (not run_manifest.file_record_is_current(packet_record) or
            not run_manifest.file_record_is_current(preview_record)):
        raise ValueError("IDENTITY_CAPTURE_ARTIFACT_STALE")
    packet = _load(packet_record["path"])
    payload = request_contract.submission_payload(context["request"])
    request_spec, request_fingerprint = request_contract.assert_request_matches_plan(
        context["request"])
    if (packet.get("render_plan") != context["plan_record"] or
            packet.get("request_spec") != request_spec or
            packet.get("planned_request_fingerprint") != request_fingerprint or
            packet.get("payload") != payload or
            packet.get("payload_fingerprint") !=
            request_contract.json_sha256(payload)):
        raise ValueError("IDENTITY_CAPTURE_PAYLOAD_STALE_OR_TAMPERED")
    if revalidate_catalog:
        live = _live_catalog_contract(context["request"], api_key=api_key)
        if live != packet.get("catalog_contract"):
            raise ValueError("IDENTITY_CAPTURE_LIVE_CATALOG_CHANGED_RECAPTURE_REQUIRED")
    return context, state, packet, packet_record, preview_record


def confirm_identity_plate_submission(manifest_path, customer_confirmation):
    confirmation = run_manifest.require_explicit_customer_confirmation(
        customer_confirmation, "identity_plate_submission_capture")
    if ("身份" not in confirmation or
            not any(token in confirmation for token in ("提交", "生成", "请求"))):
        raise ValueError("IDENTITY_SUBMISSION_EXPLICIT_CUSTOMER_CONFIRMATION_REQUIRED")
    api_key = key_setup.require_key(
        "IDENTITY_CAPTURE_BASICROUTER_KEY_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest.load_manifest(manifest_path)
    context, _, packet, packet_record, preview_record = _validated_capture(
        manifest, api_key=api_key)
    content = {
        "render_plan": context["plan_record"], "packet": packet_record,
        "preview": preview_record,
        "payload_fingerprint": packet["payload_fingerprint"],
        "catalog_fingerprint": packet["catalog_fingerprint"],
        "source_fingerprint": context["request"].get("source_fingerprint"),
    }
    fingerprint = request_contract.json_sha256(content)
    marker = _full_marker({
        "schema_version": 1,
        "kind": "identity_plate_submission_customer_approval",
        "status": "confirmed", "client": manifest["client"],
        "run_id": manifest["run_id"], "confirmed_by_customer": True,
        "customer_confirmation": confirmation,
        "confirmation_source": "customer_dialogue", "content": content,
        "content_fingerprint": fingerprint,
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
    })
    marker_path = _marker_path(
        manifest, "identity_plate_submission", fingerprint, marker)
    atomic_write_json(marker_path, marker)
    approval = dict(
        marker, approval_path=marker_path,
        marker_sha256=run_manifest.file_record(marker_path)["sha256"])

    def mutate(current):
        current_context, state, current_packet, current_record, current_preview = (
            _validated_capture(current, revalidate_catalog=False))
        if (current_context["plan_record"] != context["plan_record"] or
                current_packet["payload_fingerprint"] != packet["payload_fingerprint"] or
                current_record != packet_record or current_preview != preview_record):
            raise ValueError("IDENTITY_SUBMISSION_CAPTURE_CHANGED_DURING_CONFIRMATION")
        state = dict(state)
        state["status"] = "submission_confirmed_ready_to_create"
        state["submission_approval"] = approval
        state["authorized_attempt"] = 1
        current["identity_plate_generation"] = state
        current["status"] = "needs_identity_plate_generation"
        return state

    return _save_state(manifest_path, mutate)


def authorize_identity_plate_retry(manifest_path, customer_confirmation):
    """Authorize exactly one new paid attempt after a recorded failure."""
    confirmation = run_manifest.require_explicit_customer_confirmation(
        customer_confirmation, "identity_plate_retry")
    if ("身份" not in confirmation or
            not any(token in confirmation for token in ("重试", "再生成", "新尝试"))):
        raise ValueError("IDENTITY_RETRY_EXPLICIT_CUSTOMER_CONFIRMATION_REQUIRED")
    api_key = key_setup.require_key(
        "IDENTITY_RETRY_BASICROUTER_KEY_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)
    current_manifest = run_manifest.load_manifest(manifest_path)
    _validated_capture(
        current_manifest, require_confirmation=True, api_key=api_key)

    def mutate(manifest):
        _validated_capture(
            manifest, require_confirmation=True, revalidate_catalog=False)
        state = dict(manifest.get("identity_plate_generation") or {})
        if state.get("status") != "failed" or not state.get("failure"):
            raise ValueError("IDENTITY_RETRY_RECORDED_FAILURE_REQUIRED")
        failure_record = state.get("failure") or {}
        failure_document = (_load(failure_record.get("path"))
                            if run_manifest.file_record_is_current(
                                failure_record) else {})
        candidate_rejection = bool(
            (failure_document.get("kind") == "identity_plate_visual_review" and
             failure_document.get("status") == "rejected" and
             failure_document.get("passed") is False) or
            (failure_document.get("kind") ==
             "identity_plate_customer_rejection" and
             failure_document.get("status") == "rejected" and
             failure_document.get("confirmed_by_customer") is True and
             failure_document.get("confirmation_source") == "customer_dialogue"))
        if (state.get("task_id") and
                state.get("remote_terminal_status") != "failed" and
                not candidate_rejection):
            raise ValueError(
                "IDENTITY_RETRY_REMOTE_TASK_MUST_BE_RECOVERED_OR_PROVEN_FAILED")
        next_attempt = int(state.get("attempt") or 1) + 1
        marker = _full_marker({
            "schema_version": 1, "kind": "identity_plate_retry_customer_approval",
            "status": "confirmed", "client": manifest["client"],
            "run_id": manifest["run_id"], "confirmed_by_customer": True,
            "customer_confirmation": confirmation,
            "confirmation_source": "customer_dialogue",
            "content": {
                "failure": state.get("failure"),
                "payload_fingerprint": ((state.get("submission_capture") or {})
                                        .get("payload_fingerprint")),
                "authorized_attempt": next_attempt,
            },
            "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        })
        fingerprint = request_contract.json_sha256(marker["content"])
        marker["content_fingerprint"] = fingerprint
        marker.pop("approval_artifact_sha256", None)
        marker.pop("artifact_short_id", None)
        marker = _full_marker(marker)
        path = _marker_path(manifest, "identity_plate_retry", fingerprint, marker)
        atomic_write_json(path, marker)
        state.update({
            "status": "submission_confirmed_ready_to_create",
            "authorized_attempt": next_attempt,
            "last_failure": failure_record,
            "retry_approval": dict(
                marker, approval_path=path,
                marker_sha256=run_manifest.file_record(path)["sha256"]),
        })
        state.pop("failure", None)
        state.pop("failure_kind", None)
        state.pop("generation_error", None)
        state.pop("last_error", None)
        state.pop("failed_at", None)
        manifest.pop("generation_error", None)
        manifest["identity_plate_generation"] = state
        manifest["status"] = "needs_identity_plate_generation"
        return state

    return _save_state(manifest_path, mutate)


def _record_submission_failure(manifest_path, *, context, packet, attempt,
                               request_id, task_id, error, out_path,
                               remote_terminal_status=None):
    failure_path = out_path + ".failure.json"
    returned_record = run_manifest.file_record(out_path)
    returned_media = ([returned_record]
                      if returned_record and returned_record.get("exists") else [])
    failed_media_qc = (review_contract.inspect_image(
        out_path, expected_ratio=packet["payload"].get("ratio") or "16:9")
        if returned_media else None)
    failure = {
        "schema_version": 1, "kind": "identity_plate_attempt_failure",
        "status": "failed", "client": context["plan"].get("client"),
        "run_id": context["plan"].get("run_id"), "attempt": attempt,
        "submission_request_id": request_id, "task_id": task_id,
        "remote_terminal_status": remote_terminal_status or "unknown",
        "error": str(error), "submitted_request_spec": packet["request_spec"],
        "submitted_request_fingerprint": packet["payload_fingerprint"],
        "submitted_render_plan": context["plan_record"],
        "returned_media": returned_media,
        "candidate_images_created": len(returned_media),
        "deterministic_media_qc": failed_media_qc,
        "billing_status": "unknown_not_inferred",
        "failed_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_json(failure_path, failure)
    failure_record = run_manifest.file_record(failure_path)

    def mutate(manifest):
        state = dict(manifest.get("identity_plate_generation") or {})
        state.update({"status": "failed", "failure": failure_record,
                      "submission_request_id": request_id,
                      "task_id": task_id, "attempt": attempt,
                      "remote_terminal_status": failure["remote_terminal_status"]})
        manifest["identity_plate_generation"] = state
        run_manifest.upsert_task(manifest, {
            "stage": "render_plan", "unit_id": "identity_plate",
            "handoff_fingerprint": packet["payload_fingerprint"],
            "attempt": attempt, "model": packet["payload"]["model"],
            "task_id": task_id, "status": "failed",
            "request_spec": packet["request_spec"],
            "request_fingerprint": packet["payload_fingerprint"],
            "submission_request_id": request_id,
            "failure_evidence": failure_record,
            "returned_media": returned_media,
            "candidate_images_created": len(returned_media),
            "deterministic_media_qc": failed_media_qc,
            "billing_status": "unknown_not_inferred",
        })
        manifest["status"] = "identity_plate_generation_failed"
        return failure

    return _save_state(manifest_path, mutate)


def _remote_task_status(api_key, task_id):
    if not task_id:
        return "not_submitted"
    try:
        return str((br_client.get_image_generation(api_key, task_id) or {}).get(
            "status") or "unknown").lower()
    except (br_client.BRError, OSError, ValueError, RuntimeError, TypeError):
        return "unknown"


def _finish_existing_task(manifest_path, *, context, packet, task_id, out_path,
                          attempt, request_id, api_key):
    urls = br_client.wait_image_generation(api_key, task_id)
    if len(urls or []) != 1:
        raise ValueError("IDENTITY_SUBMISSION_EXPECTED_ONE_IMAGE")
    fd, temporary = tempfile.mkstemp(
        prefix=".identity-download-", dir=os.path.dirname(out_path))
    os.close(fd)
    os.remove(temporary)
    try:
        # Codex/Desktop and some enterprise transports terminate HTTPS through
        # a loopback peer.  The downloader still enforces HTTPS, public DNS,
        # redirect validation, certificates, size limits and atomic writes;
        # only the observed socket peer is allowed to be non-public here.
        br_client.download(
            urls[0], temporary, allow_nonpublic_peer=True)
        if not os.path.isfile(temporary):
            raise ValueError("IDENTITY_SUBMISSION_DOWNLOAD_MISSING")
        os.replace(temporary, out_path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    output = run_manifest.file_record(out_path)
    if not output or not output.get("exists"):
        raise ValueError("IDENTITY_SUBMISSION_OUTPUT_MISSING")
    media_qc = review_contract.inspect_image(
        out_path, expected_ratio=packet["payload"].get("ratio") or "16:9")
    if not media_qc.get("passed"):
        raise ValueError(
            "IDENTITY_SUBMISSION_MEDIA_QC_FAILED: %s" %
            json.dumps(media_qc, ensure_ascii=False, sort_keys=True))
    result_path = out_path + ".result.json"
    result = {
        "schema_version": 1, "kind": "identity_plate_generation_result",
        "status": "pending_visual_review",
        "client": context["plan"].get("client"),
        "run_id": context["plan"].get("run_id"),
        "task_id": task_id, "submission_request_id": request_id,
        "attempt": attempt,
        "payload_fingerprint": packet["payload_fingerprint"],
        "request_spec": packet["request_spec"],
        "candidate": output, "remote_url": urls[0],
        "deterministic_media_qc": media_qc,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    if os.path.isfile(result_path):
        existing = _load(result_path)
        volatile = {"completed_at", "remote_url"}
        if ({key: value for key, value in existing.items() if key not in volatile} !=
                {key: value for key, value in result.items() if key not in volatile}):
            raise ValueError("IDENTITY_GENERATION_RESULT_PATH_CONFLICT")
        result = existing
    elif os.path.exists(result_path):
        raise ValueError("IDENTITY_GENERATION_RESULT_NOT_REGULAR_FILE")
    else:
        atomic_write_json(result_path, result)
    result_record = run_manifest.file_record(result_path)

    def generated(current):
        active = dict(current.get("identity_plate_generation") or {})
        if active.get("task_id") != task_id:
            raise ValueError("IDENTITY_SUBMISSION_TASK_BINDING_CHANGED")
        transient_keys = (
            "failure", "failure_kind", "generation_error", "last_error",
            "failed_at")
        prior_errors = {
            key: active.get(key) for key in transient_keys if key in active}
        if "generation_error" in current:
            prior_errors["manifest_generation_error"] = current.get(
                "generation_error")
        if prior_errors:
            recovered_at = datetime.now().isoformat(timespec="seconds")
            unsigned = {
                "schema_version": 1,
                "kind": "identity_plate_generation_recovery",
                "task_id": task_id,
                "submission_request_id": request_id,
                "attempt": attempt,
                "payload_fingerprint": packet["payload_fingerprint"],
                "prior_errors": prior_errors,
                "recovered_output": output,
                "generation_result": result_record,
                "recovered_at": recovered_at,
            }
            recovery = dict(
                unsigned,
                recovery_fingerprint=request_contract.json_sha256(unsigned))
            history = list(current.get(
                "identity_plate_generation_recovery_history") or [])
            if not any(item.get("recovery_fingerprint") ==
                       recovery["recovery_fingerprint"] for item in history
                       if isinstance(item, dict)):
                history.append(recovery)
            current["identity_plate_generation_recovery_history"] = history
        for key in transient_keys:
            active.pop(key, None)
        current.pop("generation_error", None)
        for stale_key in ("visual_review_pending", "visual_review",
                          "output_approval"):
            active.pop(stale_key, None)
        active.update({
            "status": "generated_pending_customer_confirmation",
            "output": output, "candidate": output,
            "remote_url": result["remote_url"],
            "generation_result": result_record,
            "deterministic_media_qc": media_qc,
            "remote_terminal_status": "success",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        })
        current["identity_plate_generation"] = active
        run_manifest.upsert_task(current, {
            "stage": "render_plan", "unit_id": "identity_plate",
            "handoff_fingerprint": packet["payload_fingerprint"],
            "attempt": attempt, "model": packet["payload"]["model"],
            "task_id": task_id,
            "status": "succeeded_pending_customer_confirmation",
            "request_spec": packet["request_spec"],
            "request_fingerprint": packet["payload_fingerprint"],
            "submission_request_id": request_id, "returned_media": [output],
            "candidate_images_created": 1,
            "generation_result": result_record,
            "deterministic_media_qc": media_qc,
        })
        current["status"] = "needs_identity_plate_customer_confirmation"
        return active

    return _save_state(manifest_path, generated)


def submit_identity_plate(manifest_path, capture_path, out_path, *, attempt=1):
    """Submit only the exact confirmed capture and download its single result."""
    # Resolve once, before any manifest transition, then reuse this credential
    # for catalog revalidation, create and polling.  Missing session authority
    # must not consume an attempt or produce false remote-failure evidence.
    api_key = key_setup.require_key(
        "IDENTITY_SUBMISSION_BASICROUTER_KEY_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest.load_manifest(manifest_path)
    context, state, packet, packet_record, _ = _validated_capture(
        manifest, require_confirmation=True, api_key=api_key)
    if os.path.abspath(capture_path) != packet_record["path"]:
        raise ValueError("IDENTITY_SUBMISSION_CAPTURE_PATH_MISMATCH")
    try:
        attempt = int(attempt)
    except (TypeError, ValueError) as exc:
        raise ValueError("IDENTITY_SUBMISSION_ATTEMPT_INVALID") from exc
    if attempt < 1:
        raise ValueError("IDENTITY_SUBMISSION_ATTEMPT_INVALID")
    out_path = run_manifest.require_run_output(
        manifest, os.path.abspath(out_path), label="identity_plate_output")
    if os.path.exists(out_path):
        raise ValueError(
            "IDENTITY_SUBMISSION_IMMUTABLE_OUTPUT_PATH_REQUIRED: use a new attempt path")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    request_id = request_contract.json_sha256({
        "kind": "identity_plate_paid_submission",
        "payload_fingerprint": packet["payload_fingerprint"],
        "attempt": attempt,
    })

    def begin(current):
        # Live catalog was checked immediately above.  Inside the CAS/lock only
        # revalidate immutable local evidence; never perform network I/O while
        # holding the manifest transaction.
        _validated_capture(
            current, require_confirmation=True, revalidate_catalog=False)
        active = current.get("identity_plate_generation") or {}
        if active.get("status") != "submission_confirmed_ready_to_create":
            raise ValueError("IDENTITY_SUBMISSION_NOT_READY")
        if int(active.get("authorized_attempt") or 1) != attempt:
            raise ValueError("IDENTITY_SUBMISSION_ATTEMPT_NOT_CUSTOMER_AUTHORIZED")
        next_state = dict(active)
        for stale_key in ("output", "candidate", "generation_result",
                          "deterministic_media_qc", "remote_url",
                          "visual_review_pending", "visual_review",
                          "output_approval"):
            next_state.pop(stale_key, None)
        next_state.update(status="submitting", attempt=attempt,
                          planned_output_path=out_path,
                          submission_request_id=request_id,
                          submission_started_at=datetime.now().isoformat(
                              timespec="seconds"))
        current.setdefault("pre_video_gates", {}).pop("identity_plate", None)
        current["identity_plate_generation"] = next_state
        run_manifest.upsert_task(current, {
            "stage": "render_plan", "unit_id": "identity_plate",
            "handoff_fingerprint": packet["payload_fingerprint"],
            "attempt": attempt, "model": packet["payload"]["model"],
            "status": "submission_intent", "request_spec": packet["request_spec"],
            "request_fingerprint": packet["payload_fingerprint"],
            "submission_request_id": request_id,
        })
        current["status"] = "generating_identity_plate"
        return next_state

    _save_state(manifest_path, begin)
    payload = packet["payload"]
    task_id = None
    try:
        task_id = br_client.create_image_generation(
            api_key, payload["text"], model=payload["model"],
            image_urls=payload["imageUrls"], count=payload["count"],
            resolution=payload["resolution"], ratio=payload["ratio"],
            request_id=request_id)

        def submitted(current):
            active = dict(current.get("identity_plate_generation") or {})
            if (active.get("status") != "submitting" or
                    active.get("submission_request_id") != request_id):
                raise ValueError("IDENTITY_SUBMISSION_STATE_CHANGED")
            active.update({"status": "submitted", "task_id": task_id})
            current["identity_plate_generation"] = active
            run_manifest.upsert_task(current, {
                "stage": "render_plan", "unit_id": "identity_plate",
                "handoff_fingerprint": packet["payload_fingerprint"],
                "attempt": attempt, "model": payload["model"],
                "task_id": task_id, "status": "submitted",
                "request_spec": packet["request_spec"],
                "request_fingerprint": packet["payload_fingerprint"],
                "submission_request_id": request_id,
            })
            return active

        _save_state(manifest_path, submitted)
        return _finish_existing_task(
            manifest_path, context=context, packet=packet, task_id=task_id,
            out_path=out_path, attempt=attempt, request_id=request_id,
            api_key=api_key)
    except (br_client.BRError, OSError, ValueError, RuntimeError, TypeError) as exc:
        if key_setup.is_auth_rejection(exc):
            key_setup.reject_key(api_key)
        remote_status = _remote_task_status(api_key, task_id)
        try:
            _record_submission_failure(
                manifest_path, context=context, packet=packet, attempt=attempt,
                request_id=request_id, task_id=task_id, error=exc,
                out_path=out_path, remote_terminal_status=remote_status)
        except (OSError, ValueError, RuntimeError, TypeError) as closeout_exc:
            if hasattr(exc, "add_note"):
                exc.add_note("identity failure closeout failed: %s" % closeout_exc)
        raise


def recover_identity_plate_task(manifest_path, out_path):
    """Resume polling an existing task without issuing another paid create."""
    api_key = key_setup.require_key(
        "IDENTITY_RECOVERY_BASICROUTER_KEY_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest.load_manifest(manifest_path)
    context, state, packet, _, _ = _validated_capture(
        manifest, require_confirmation=True, api_key=api_key)
    task_id = state.get("task_id")
    if state.get("status") not in {"submitted", "failed"} or not task_id:
        raise ValueError("IDENTITY_RECOVERY_EXISTING_TASK_REQUIRED")
    if state.get("failure_kind") in {
            "visual_review_rejection", "customer_rejection"}:
        raise ValueError(
            "IDENTITY_RECOVERY_REJECTED_CANDIDATE_FORBIDDEN: authorize a new attempt")
    out_path = run_manifest.require_run_output(
        manifest, os.path.abspath(out_path), label="identity_plate_output")
    planned_output_path = state.get("planned_output_path")
    if planned_output_path and os.path.abspath(planned_output_path) != out_path:
        raise ValueError("IDENTITY_RECOVERY_OUTPUT_PATH_MISMATCH")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    attempt = int(state.get("attempt") or 1)
    request_id = state.get("submission_request_id")
    try:
        return _finish_existing_task(
            manifest_path, context=context, packet=packet, task_id=task_id,
            out_path=out_path, attempt=attempt, request_id=request_id,
            api_key=api_key)
    except (br_client.BRError, OSError, ValueError, RuntimeError, TypeError) as exc:
        if key_setup.is_auth_rejection(exc):
            key_setup.reject_key(api_key)
        remote_status = _remote_task_status(api_key, task_id)
        _record_submission_failure(
            manifest_path, context=context, packet=packet, attempt=attempt,
            request_id=request_id, task_id=task_id, error=exc,
            out_path=out_path, remote_terminal_status=remote_status)
        raise


def record_identity_plate_visual_review(manifest_path, out_path, *, checks,
                                        observations, reviewer_notes):
    """Persist an explicit visual inspection bound to the current candidate."""
    notes = str(reviewer_notes or "").strip()
    if not notes:
        raise ValueError("IDENTITY_VISUAL_REVIEW_NOTES_REQUIRED")
    if not isinstance(checks, dict):
        raise ValueError("IDENTITY_VISUAL_REVIEW_CHECKS_REQUIRED")
    if not isinstance(observations, dict):
        raise ValueError("IDENTITY_VISUAL_REVIEW_OBSERVATIONS_REQUIRED")
    normalized_checks = {
        str(name): value is True for name, value in checks.items()
    }
    passed = all(normalized_checks.get(name) is True
                 for name in review_contract.REQUIRED_VISUAL_CHECKS)
    manifest_path = os.path.abspath(manifest_path)
    manifest = run_manifest.load_manifest(manifest_path)
    if not run_manifest.identity_plate_generation_candidate_is_current(manifest):
        raise ValueError("IDENTITY_VISUAL_REVIEW_CURRENT_CANDIDATE_REQUIRED")
    context = _context(manifest)
    state = manifest.get("identity_plate_generation") or {}
    candidate = state.get("candidate") or state.get("output") or {}
    result_record = state.get("generation_result") or {}
    media_qc = state.get("deterministic_media_qc") or {}
    identity_references = (context["request_spec"].get("image_references") or [])
    review = {
        "schema_version": 1, "kind": "identity_plate_visual_review",
        "status": "passed" if passed else "rejected", "passed": passed,
        "client": manifest.get("client"), "run_id": manifest.get("run_id"),
        "reviewer_type": "agent_visual_inspection",
        "artifact": candidate, "generation_result": result_record,
        "identity_references": identity_references,
        "deterministic_media_qc": media_qc,
        "checks": normalized_checks, "observations": observations,
        "reviewer_notes": notes,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }
    review_contract.validate_visual_review_evidence(
        review, candidate=candidate, generation_result=result_record,
        media_qc=media_qc, identity_references=identity_references)
    out_path = run_manifest.require_run_output(
        manifest, os.path.abspath(out_path),
        label="identity_plate_visual_review")
    if os.path.isfile(out_path):
        existing = _load(out_path)
        existing_contract = {
            key: value for key, value in existing.items() if key != "reviewed_at"}
        incoming_contract = {
            key: value for key, value in review.items() if key != "reviewed_at"}
        if existing_contract != incoming_contract:
            raise ValueError(
                "IDENTITY_VISUAL_REVIEW_IMMUTABLE_OUTPUT_PATH_REQUIRED")
        review = existing
        passed = review.get("passed") is True
    elif os.path.exists(out_path):
        raise ValueError("IDENTITY_VISUAL_REVIEW_OUTPUT_NOT_REGULAR_FILE")
    else:
        atomic_write_json(out_path, review)
    review_record = run_manifest.file_record(out_path)

    def mutate(current):
        if not run_manifest.identity_plate_generation_candidate_is_current(current):
            raise ValueError("IDENTITY_VISUAL_REVIEW_CANDIDATE_CHANGED")
        active = dict(current.get("identity_plate_generation") or {})
        if (active.get("candidate") != candidate or
                active.get("generation_result") != result_record or
                active.get("deterministic_media_qc") != media_qc):
            raise ValueError("IDENTITY_VISUAL_REVIEW_BINDING_CHANGED")
        active["visual_review_pending"] = review_record
        if not passed:
            active.update({
                "status": "failed", "failure": review_record,
                "remote_terminal_status": "success",
                "failure_kind": "visual_review_rejection",
            })
        current["identity_plate_generation"] = active
        current["status"] = (
            "needs_identity_plate_customer_confirmation" if passed else
            "identity_plate_visual_review_failed")
        return {"visual_review": review_record, "candidate": candidate,
                "passed": passed}

    return _save_state(manifest_path, mutate)


def reject_generated_identity_plate(manifest_path, customer_confirmation):
    """Record a customer rejection so a fresh paid attempt can be authorized."""
    confirmation = run_manifest.require_explicit_customer_confirmation(
        customer_confirmation, "identity_plate_rejection")
    if ("身份" not in confirmation or not any(
            token in confirmation for token in
            ("拒绝", "不接受", "不通过", "重做", "重新生成"))):
        raise ValueError("IDENTITY_PLATE_EXPLICIT_CUSTOMER_REJECTION_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)

    def mutate(manifest):
        if not run_manifest.identity_plate_generation_candidate_is_current(manifest):
            raise ValueError("IDENTITY_PLATE_CURRENT_CANDIDATE_REQUIRED")
        context = _context(manifest)
        state = dict(manifest.get("identity_plate_generation") or {})
        output = state.get("candidate") or state.get("output") or {}
        result_record = state.get("generation_result") or {}
        media_qc = state.get("deterministic_media_qc") or {}
        review_record = state.get("visual_review_pending") or {}
        if not run_manifest.file_record_is_current(review_record):
            raise ValueError("IDENTITY_PLATE_VISUAL_REVIEW_REQUIRED_BEFORE_REJECTION")
        review = _load(review_record["path"])
        review_contract.validate_visual_review(
            review, candidate=output, generation_result=result_record,
            media_qc=media_qc,
            identity_references=(context["request_spec"].get(
                "image_references") or []))
        content = {
            "render_plan": context["plan_record"], "output": output,
            "generation_result": result_record, "visual_review": review_record,
            "deterministic_media_qc": media_qc, "task_id": state.get("task_id"),
            "payload_fingerprint": ((state.get("submission_capture") or {})
                                    .get("payload_fingerprint")),
        }
        fingerprint = request_contract.json_sha256(content)
        marker = _full_marker({
            "schema_version": 1,
            "kind": "identity_plate_customer_rejection",
            "status": "rejected", "client": manifest["client"],
            "run_id": manifest["run_id"], "confirmed_by_customer": True,
            "customer_confirmation": confirmation,
            "confirmation_source": "customer_dialogue",
            "content": content, "content_fingerprint": fingerprint,
            "rejected_at": datetime.now().isoformat(timespec="seconds"),
        })
        marker_path = _marker_path(
            manifest, "identity_plate_rejection", fingerprint, marker)
        atomic_write_json(marker_path, marker)
        rejection_record = run_manifest.file_record(marker_path)
        state.update({
            "status": "failed", "failure": rejection_record,
            "failure_kind": "customer_rejection",
            "remote_terminal_status": "success",
            "customer_rejection": dict(
                marker, approval_path=marker_path,
                marker_sha256=rejection_record["sha256"]),
        })
        manifest.setdefault("pre_video_gates", {}).pop("identity_plate", None)
        manifest["identity_plate_generation"] = state
        run_manifest.upsert_task(manifest, {
            "stage": "render_plan", "unit_id": "identity_plate",
            "handoff_fingerprint": ((state.get("submission_capture") or {})
                                    .get("payload_fingerprint")),
            "attempt": state.get("attempt"),
            "model": context["request"].get("model"),
            "task_id": state.get("task_id"),
            "status": "candidate_rejected_by_customer",
            "request_fingerprint": ((state.get("submission_capture") or {})
                                    .get("payload_fingerprint")),
            "submission_request_id": state.get("submission_request_id"),
            "returned_media": [output], "candidate_images_created": 1,
            "generation_result": result_record,
            "deterministic_media_qc": media_qc,
            "visual_review": review_record,
            "customer_rejection": rejection_record,
        })
        manifest["status"] = "identity_plate_rejected_by_customer"
        return state

    return _save_state(manifest_path, mutate)


def confirm_generated_identity_plate(manifest_path, review_path,
                                     customer_confirmation):
    confirmation = run_manifest.require_explicit_customer_confirmation(
        customer_confirmation, "identity_plate")
    if "身份" not in confirmation:
        raise ValueError("IDENTITY_PLATE_EXPLICIT_CUSTOMER_CONFIRMATION_REQUIRED")
    manifest_path = os.path.abspath(manifest_path)
    initial = run_manifest.load_manifest(manifest_path)
    review_path = run_manifest.require_run_output(
        initial, os.path.abspath(review_path),
        label="identity_plate_visual_review", must_exist=True)

    def mutate(manifest):
        context = _context(manifest)
        if not run_manifest.identity_plate_submission_is_current(manifest):
            raise ValueError("IDENTITY_PLATE_CONFIRMED_SUBMISSION_REQUIRED")
        state = dict(manifest.get("identity_plate_generation") or {})
        output = state.get("candidate") or state.get("output") or {}
        result_record = state.get("generation_result") or {}
        media_qc = state.get("deterministic_media_qc") or {}
        if (state.get("status") != "generated_pending_customer_confirmation" or
                not run_manifest.file_record_is_current(output) or
                not run_manifest.file_record_is_current(result_record) or
                not media_qc.get("passed")):
            raise ValueError("IDENTITY_PLATE_GENERATED_OUTPUT_REQUIRED")
        with open(review_path, encoding="utf-8") as handle:
            review = json.load(handle)
        review_contract.validate_visual_review(
            review, candidate=output, generation_result=result_record,
            media_qc=media_qc,
            identity_references=(context["request_spec"].get(
                "image_references") or []))
        review_record = run_manifest.file_record(review_path)
        pending_review = state.get("visual_review_pending") or {}
        if pending_review != review_record:
            raise ValueError("IDENTITY_VISUAL_REVIEW_NOT_RECORDED_FOR_CANDIDATE")
        marker = _full_marker({
            "schema_version": 1, "kind": "identity_plate_customer_approval",
            "status": "confirmed", "client": manifest["client"],
            "run_id": manifest["run_id"], "confirmed_by_customer": True,
            "customer_confirmation": confirmation,
            "confirmation_source": "customer_dialogue",
            "content": {"render_plan": context["plan_record"], "output": output,
                        "generation_result": result_record,
                        "visual_review": review_record,
                        "deterministic_media_qc": media_qc,
                        "task_id": state.get("task_id"),
                        "payload_fingerprint": ((state.get("submission_capture") or {})
                                                .get("payload_fingerprint"))},
            "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        })
        fingerprint = request_contract.json_sha256(marker["content"])
        marker["content_fingerprint"] = fingerprint
        # content_fingerprint is part of the signed marker; refresh its identity.
        marker.pop("approval_artifact_sha256", None)
        marker.pop("artifact_short_id", None)
        marker = _full_marker(marker)
        marker_path = _marker_path(
            manifest, "identity_plate", fingerprint, marker)
        atomic_write_json(marker_path, marker)
        asset = {
            "path": output["path"], "sha256": output["sha256"],
            "status": "confirmed", "confirmed_by_customer": True,
            "customer_confirmation": confirmation,
            "confirmation_source": "customer_dialogue",
            "confirmed_at": marker["confirmed_at"],
            "approval_path": marker_path,
            "marker_sha256": run_manifest.file_record(marker_path)["sha256"],
            "source_fingerprint": context["request"].get("source_fingerprint"),
            "model": context["request"].get("model"), "tag": "identity_plate",
            "generation_result": result_record,
            "visual_review": review_record,
            "deterministic_media_qc": media_qc,
        }
        manifest.setdefault("pre_video_gates", {})["identity_plate"] = {
            "status": "confirmed", "render_plan": context["plan_record"],
            "identity_request_fingerprint": context["request_fingerprint"],
            "asset": asset, "bound_at": marker["confirmed_at"],
        }
        state.update({"status": "confirmed", "visual_review": review_record,
                      "visual_review_pending": review_record,
                      "output_approval": dict(
            marker, approval_path=marker_path,
            marker_sha256=asset["marker_sha256"])})
        manifest["identity_plate_generation"] = state
        manifest["status"] = "needs_video_prompt_review"
        return state

    return _save_state(manifest_path, mutate)


def _public_cli_result(value):
    """Remove request bodies, credentials and inline media from CLI output."""
    forbidden = {"payload", "imageurls", "api_key", "apikey", "authorization",
                 "basicrouter_key", "secret", "remote_url"}
    if isinstance(value, dict):
        return {
            key: _public_cli_result(item)
            for key, item in value.items()
            if str(key).lower() not in forbidden
        }
    if isinstance(value, list):
        return [_public_cli_result(item) for item in value]
    if isinstance(value, tuple):
        return [_public_cli_result(item) for item in value]
    if isinstance(value, str) and "data:image/" in value.lower():
        return "<redacted-inline-image>"
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Identity plate approval workflow")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--confirm-reference", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--confirm-capture", action="store_true")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--confirm-image", action="store_true")
    parser.add_argument("--authorize-retry", action="store_true")
    parser.add_argument("--recover-task", action="store_true")
    parser.add_argument("--record-visual-review", action="store_true")
    parser.add_argument("--reject-image", action="store_true")
    parser.add_argument("--customer-confirmation")
    parser.add_argument("--packet")
    parser.add_argument("--out")
    parser.add_argument("--preview-out")
    parser.add_argument("--visual-review")
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--observation", action="append", default=[])
    parser.add_argument("--reviewer-notes")
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args(argv)
    selected = sum(bool(value) for value in (
        args.confirm_reference, args.capture, args.confirm_capture,
        args.submit, args.confirm_image, args.authorize_retry,
        args.recover_task, args.record_visual_review, args.reject_image))
    if selected != 1:
        parser.error("choose exactly one identity workflow action")
    try:
        if args.confirm_reference:
            result = confirm_identity_plate_reference(
                args.manifest, args.customer_confirmation)
        elif args.capture:
            if not args.out or not args.preview_out:
                parser.error("--capture requires --out and --preview-out")
            result = capture_identity_plate_submission(
                args.manifest, args.out, args.preview_out)
        elif args.confirm_capture:
            result = confirm_identity_plate_submission(
                args.manifest, args.customer_confirmation)
        elif args.submit:
            if not args.packet or not args.out:
                parser.error("--submit requires --packet and --out")
            result = submit_identity_plate(
                args.manifest, args.packet, args.out, attempt=args.attempt)
        elif args.authorize_retry:
            result = authorize_identity_plate_retry(
                args.manifest, args.customer_confirmation)
        elif args.recover_task:
            if not args.out:
                parser.error("--recover-task requires --out")
            result = recover_identity_plate_task(args.manifest, args.out)
        elif args.record_visual_review:
            if not args.out:
                parser.error("--record-visual-review requires --out")
            unknown = sorted(set(args.check) - set(
                review_contract.REQUIRED_VISUAL_CHECKS))
            if unknown:
                parser.error("unknown visual review checks: %s" % ",".join(unknown))
            observations = {}
            for item in args.observation:
                name, separator, detail = str(item).partition("=")
                if (not separator or name in observations or
                        name not in review_contract.REQUIRED_VISUAL_CHECKS):
                    parser.error(
                        "--observation must be a unique required check=name detail")
                observations[name] = detail.strip()
            result = record_identity_plate_visual_review(
                args.manifest, args.out,
                checks={name: name in set(args.check)
                        for name in review_contract.REQUIRED_VISUAL_CHECKS},
                observations=observations,
                reviewer_notes=args.reviewer_notes)
        elif args.reject_image:
            result = reject_generated_identity_plate(
                args.manifest, args.customer_confirmation)
        else:
            if not args.visual_review:
                parser.error("--confirm-image requires --visual-review")
            result = confirm_generated_identity_plate(
                args.manifest, args.visual_review, args.customer_confirmation)
    except key_setup.SessionKeyError as exc:
        print(exc.client_message)
        return 1
    print(json.dumps(_public_cli_result(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
