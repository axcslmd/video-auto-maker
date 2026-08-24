#!/usr/bin/env python3
"""Video task persistence and resume management.

Extracted from video_engine.py (v3 split). Contains:
  - _persist_task (with v3 cost_ledger integration)
  - _record_task_resume
  - _persist_submission_intent
  - _submission_request_id (deterministic request identity)
  - _current_attempt / _completed_task / _task_video_url
  - _manifest_handoff_matches (video handoff verification)

Dependencies: run_manifest, generation_ledger, artifact_contract, br_client, cost_ledger
"""
import os
import sys
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_manifest as _rm
import generation_ledger
import artifact_contract
import br_client
import cost_ledger


def _provider_error_evidence(error, *, model=None, task_id=None,
                             observation_source="provider_terminal_status"):
    """Return a minimal, secret-safe record of the original provider failure."""
    payload = getattr(error, "payload", None)

    def first(keys, value):
        if isinstance(value, dict):
            for key in keys:
                if value.get(key) not in (None, ""):
                    return value.get(key)
            for nested in value.values():
                found = first(keys, nested)
                if found not in (None, ""):
                    return found
        elif isinstance(value, (list, tuple)):
            for nested in value:
                found = first(keys, nested)
                if found not in (None, ""):
                    return found
        return None

    raw_message = first(("message", "errorMessage", "detail", "reason"), payload)
    message = str(raw_message or error or type(error).__name__)
    message = re.sub(r"sk-[A-Za-z0-9._-]+", "sk-<redacted>", message)
    message = re.sub(r"data:[^,\s]+,[A-Za-z0-9+/=_-]+", "data:<redacted>", message)
    message = re.sub(r"(https?://[^?\s]+)\?[^\s]+", r"\1?<redacted>", message)
    summary = message[:1000]
    code = first(("code", "errorCode", "error_code", "type"), payload)
    status = first(("status", "state", "taskStatus"), payload)
    return {
        "kind": "provider_video_error_evidence",
        "observed_at": datetime.now().isoformat(timespec="seconds"),
        "observation_source": observation_source,
        "model": model,
        "task_id": task_id,
        "provider_error_code": str(code or type(error).__name__),
        "provider_status": str(status) if status not in (None, "") else None,
        "message_summary": summary,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "billing_status": "unknown",
    }


def _manifest_handoff_matches(manifest, segments, *, allow_subset=False):
    """Verify manifest recorded video handoff matches current segments."""
    recorded = (manifest.get("handoffs") or {}).get("video") or {}
    expected = {seg.get("id"): seg.get("video_handoff_fingerprint") for seg in segments}
    if not recorded or not recorded.get("segments"):
        raise ValueError("VIDEO_HANDOFF_REQUIRED: manifest 未记录当前 segments 的 video handoff")
    recorded_segments = recorded.get("segments") or {}
    matches = (all(recorded_segments.get(segment_id) == fingerprint
                   for segment_id, fingerprint in expected.items())
               if allow_subset else recorded_segments == expected)
    if not expected or not matches:
        raise ValueError("VIDEO_HANDOFF_MISMATCH: manifest recorded video handoff 与当前 segments 不一致")


def _persist_task(manifest, manifest_path, ledger_path, task, status, **fields):
    """Persist task result to manifest + ledger, with auto cost estimation.

    v3: auto-computes cost_estimate for succeeded tasks (fail-safe).
    """
    persisted_video_url = fields.pop("video_url", None)
    if persisted_video_url:
        fields["video_url_sha256"] = hashlib.sha256(
            str(persisted_video_url).encode("utf-8")).hexdigest()
    attempt = int(fields.pop("attempt", None) or task.get("attempt") or 1)
    # v3: auto-compute cost estimate for succeeded tasks
    if status == "succeeded" and "cost_estimate" not in fields:
        try:
            seg = task.get("segment") or {}
            seg_model = task.get("model") or "seedance-2.0"
            dur = seg.get("duration") or seg.get("duration_sec")
            fields["cost_estimate"] = cost_ledger.cost_estimate_for_task(
                seg_model, dur, "video", attempt)
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
    if ledger_path:
        generation_ledger.append_event(ledger_path, "task_%s" % status, stage="video",
                                       unit_id=task.get("segment", {}).get("id"),
                                       task_id=task.get("task_id"), attempt=attempt, **fields)
    if manifest is not None:
        for name in ("request_id", "dependency_fingerprint", "generation_dependency",
                     "supersedes"):
            if task.get(name) is not None and name not in fields:
                fields[name] = task[name]
        _rm.upsert_task(manifest, dict(stage="video", unit_id=task.get("segment", {}).get("id"),
                                      handoff_fingerprint=task.get("handoff_fingerprint"),
                                       task_id=task.get("task_id"), model=task.get("model"),
                                      attempt=attempt, status=status, **fields))
        if manifest_path:
            _rm.save_manifest(manifest, manifest_path)


def _record_task_resume(manifest, manifest_path, ledger_path, task):
    """Record that a task is being resumed (for crash recovery)."""
    if ledger_path:
        generation_ledger.append_event(
            ledger_path, "task_resumed", stage="video",
            unit_id=task.get("segment", {}).get("id"), task_id=task.get("task_id"),
            handoff_fingerprint=task.get("handoff_fingerprint"),
            request_id=task.get("request_id"),
            dependency_fingerprint=task.get("dependency_fingerprint"))
    if manifest is not None:
        _rm.upsert_task(manifest, {"stage": "video",
                                   "unit_id": task.get("segment", {}).get("id"),
                                   "handoff_fingerprint": task.get("handoff_fingerprint"),
                                    "task_id": task.get("task_id"), "model": task.get("model"),
                                    "attempt": task.get("attempt", 1),
                                    "request_id": task.get("request_id"),
                                    "dependency_fingerprint": task.get("dependency_fingerprint"),
                                    "status": "running"})
        if manifest_path:
            _rm.save_manifest(manifest, manifest_path)


def _submission_request_id(segment, model, video_type, handoff_fingerprint,
                           attempt=1, dependency_fingerprint=None):
    """Deterministic paid-request identity, stable across process restarts."""
    payload = {
        "stage": "video", "unit_id": segment.get("id"),
        "handoff_fingerprint": handoff_fingerprint, "model": model,
        "video_type": int(video_type or 1), "attempt": int(attempt),
        "dependency_fingerprint": dependency_fingerprint,
    }
    return "video-" + artifact_contract.sha256_json(payload)


def _persist_submission_intent(manifest, manifest_path, ledger_path, segment,
                               model, video_type, handoff_fingerprint, attempt=None,
                               dependency_fingerprint=None):
    """Record the intent to submit a video task (for crash recovery dedup)."""
    if attempt is None:
        attempt = _rm.current_video_attempt(manifest or {}, segment.get("id"))
    request_id = _submission_request_id(
        segment, model, video_type, handoff_fingerprint, attempt,
        dependency_fingerprint)
    fields = {"stage": "video", "unit_id": segment.get("id"),
              "handoff_fingerprint": handoff_fingerprint,
              "attempt": attempt, "model": model,
              "dependency_fingerprint": dependency_fingerprint,
              "request_id": request_id, "status": "submitting"}
    if ledger_path:
        generation_ledger.append_event(
            ledger_path, "task_submitting", **fields)
    if manifest is not None:
        _rm.upsert_task(manifest, fields)
        if manifest_path:
            _rm.save_manifest(manifest, manifest_path)
    return request_id


def _submission_prompt_snapshot_fields(*, submission_text=None, source=None,
                                       storyboard_ref=None, extend_url=None):
    """Return persisted submission-prompt snapshot fields."""
    text = submission_text
    if text is None and source is not None:
        text = source.get("submission_prompt_zh")
    fields = {}
    if text is not None:
        text = str(text)
        fields["submission_prompt_zh"] = text
        fields["submission_prompt_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fields["submission_prompt_length"] = len(text)
    elif source is not None:
        for name in ("submission_prompt_zh", "submission_prompt_sha256",
                     "submission_prompt_length"):
            if source.get(name) is not None:
                fields[name] = source.get(name)
    if storyboard_ref is None:
        if source is not None and source.get("submission_prompt_storyboard_ref") is not None:
            fields["submission_prompt_storyboard_ref"] = bool(
                source.get("submission_prompt_storyboard_ref"))
    else:
        fields["submission_prompt_storyboard_ref"] = bool(storyboard_ref)
    if extend_url is None:
        if source is not None and source.get("submission_prompt_extend_url") is not None:
            fields["submission_prompt_extend_url"] = str(
                source.get("submission_prompt_extend_url"))
    else:
        fields["submission_prompt_extend_url"] = str(extend_url)
    return fields


def _persist_submission_prompt_snapshot(manifest, manifest_path, ledger_path, segment,
                                        model, video_type, handoff_fingerprint, *,
                                        attempt=None, dependency_fingerprint=None,
                                        request_id=None, submission_text="",
                                        storyboard_ref=False, extend_url=None):
    """Persist the exact prompt text used for a paid submission."""
    fields = _submission_prompt_snapshot_fields(
        submission_text=submission_text, storyboard_ref=storyboard_ref,
        extend_url=extend_url)
    fields = {
        "stage": "video",
        "unit_id": segment.get("id"),
        "handoff_fingerprint": handoff_fingerprint,
        "attempt": attempt or _current_attempt(manifest or {}, segment.get("id")),
        "model": model,
        "dependency_fingerprint": dependency_fingerprint,
        "request_id": request_id,
        **fields,
    }
    if ledger_path:
        generation_ledger.append_event(
            ledger_path, "submission_prompt_snapshot", stage="video",
            unit_id=segment.get("id"), handoff_fingerprint=handoff_fingerprint,
            request_id=request_id, model=model, attempt=fields["attempt"],
            submission_prompt_sha256=fields["submission_prompt_sha256"],
            submission_prompt_length=fields["submission_prompt_length"])
    if manifest is not None:
        _rm.upsert_task(manifest, fields)
        if manifest_path:
            _rm.save_manifest(manifest, manifest_path)
    return fields


def _current_attempt(manifest, segment_id):
    """Get the current attempt number for a segment."""
    if manifest is None:
        return 1
    return _rm.current_video_attempt(manifest, segment_id)


def _completed_task(manifest, segment_id, handoff_fingerprint, model=None,
                    dependency_fingerprint=None):
    """Reuse success only inside the currently authorized attempt."""
    if manifest is None:
        return None
    attempt = _current_attempt(manifest, segment_id)
    return next((item for item in reversed(manifest.get("tasks", []))
                 if item.get("stage") == "video" and item.get("unit_id") == segment_id
                 and item.get("handoff_fingerprint") == handoff_fingerprint
                 and _rm.task_model_matches(item, model)
                 and _rm.task_dependency_matches(item, dependency_fingerprint)
                 and int(item.get("attempt", 1)) == attempt
                 and item.get("status") == "succeeded"
                 and item.get("media_qc_failed") is not True
                 and item.get("task_id")), None)


def _task_video_url(api_key, task):
    """Refresh the video URL for a completed task (CDN URLs expire)."""
    info = br_client.get_video(api_key, task["task_id"])
    status = str(info.get("status") or "").lower()
    if status not in ("succeeded", "succeed", "success", "completed") or not info.get("videoUrl"):
        raise br_client.BRError("TASK_URL_REFRESH_FAILED: task %s" % task.get("task_id"))
    return info["videoUrl"]


def _write_video_submission_report(results_out, results, *, draft=False, title=None):
    """Write a human-readable report for the final submission results."""
    if not results_out:
        return None
    report_path = os.path.abspath("%s.report.md" % results_out)
    lines = []
    lines.append("# Video Submission Report")
    lines.append("")
    lines.append("> 仅记录提交、QC 与 OCR 结果；不做视觉辅助判定，也不替代客户确认。")
    lines.append("")
    lines.append("- 模式：%s" % ("draft" if draft else "formal"))
    if title:
        lines.append("- 标题：%s" % title)
    lines.append("")
    segments = results or []
    for index, item in enumerate(segments, 1):
        seg = item.get("segment") or {}
        lines.append("## %s. %s" % (index, seg.get("id") or "segment"))
        lines.append("- 结果：%s" % ("成功" if item.get("ok") else "失败"))
        if item.get("model"):
            lines.append("- 模型：%s" % item.get("model"))
        if item.get("taskId"):
            lines.append("- taskId：%s" % item.get("taskId"))
        if item.get("localPath") or item.get("absPath"):
            lines.append("- 文件：%s" % (item.get("absPath") or item.get("localPath")))
        if item.get("submission_prompt_sha256"):
            lines.append("- 提交提示词哈希：%s" % item.get("submission_prompt_sha256"))
            lines.append("- 提交提示词长度：%s" % item.get("submission_prompt_length"))
        if item.get("media_qc"):
            qc = item["media_qc"]
            lines.append("- 媒体QC：%s" % ("通过" if qc.get("passed") else "未通过"))
        if item.get("ocr_warning") is not None:
            lines.append("- OCR：%s" % ("警告" if item.get("ocr_warning") else "清洁"))
            if item.get("ocr_texts"):
                lines.append("- OCR文字：%s" % "; ".join(map(str, item.get("ocr_texts") or [])))
        if item.get("delivery_blocked"):
            lines.append("- 交付：阻断")
        elif item.get("ok"):
            lines.append("- 交付：可继续后续检查")
        lines.append("")
    Path(report_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report_path
