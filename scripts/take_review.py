#!/usr/bin/env python3
"""Provider-neutral take review and acceptance gate."""
import argparse
import copy
import json
import os
import uuid
from datetime import datetime

from artifact_contract import file_sha256, sha256_json
import schema_validate
from project_utils import atomic_write_json

VERDICTS = {"pending", "accepted", "rejected"}
TECHNICAL_REQUIRED = {"video_integrity", "audio_integrity"}
MARKETING_REQUIRED = {"lip_sync", "identity_fidelity", "product_fidelity", "script_fidelity"}


class ReviewGateError(ValueError):
    pass


def _now():
    return datetime.now().isoformat(timespec="seconds")


def take_fingerprint(result):
    local = result.get("localPath") or result.get("absPath")
    payload = {"segment_id": result.get("segment_id"), "task_id": result.get("taskId"),
               "video_sha256": file_sha256(local),
               "video_url": None if file_sha256(local) else result.get("videoUrl"),
               "handoff": result.get("video_handoff_fingerprint")}
    return sha256_json(payload)


def artifact_is_current(review):
    artifact = review.get("artifact") or {}
    path = artifact.get("local_path")
    if not path or not os.path.isfile(path):
        return False
    current_sha = file_sha256(path)
    return bool(current_sha and current_sha == artifact.get("sha256"))


def artifact_fingerprint_is_current(review):
    """Recompute the review fingerprint from current bytes and bound metadata."""
    artifact = review.get("artifact") or {}
    result = {
        "segment_id": review.get("segment_id"),
        "taskId": artifact.get("task_id"),
        "videoUrl": artifact.get("video_url"),
        "localPath": artifact.get("local_path"),
        "video_handoff_fingerprint": artifact.get("video_handoff_fingerprint"),
    }
    return bool(artifact.get("take_fingerprint")
                and take_fingerprint(result) == artifact.get("take_fingerprint"))


def _require_quality(review):
    if not review.get("review_sources"):
        raise ReviewGateError("REVIEW_SOURCES_REQUIRED")
    quality = review.get("quality") or {}
    technical = quality.get("technical") or {}
    marketing = quality.get("marketing") or {}
    missing_technical = sorted(key for key in TECHNICAL_REQUIRED if technical.get(key) is None)
    missing_marketing = sorted(key for key in MARKETING_REQUIRED if marketing.get(key) is None)
    if missing_technical:
        raise ReviewGateError("TECHNICAL_REVIEW_REQUIRED: %s" % ",".join(missing_technical))
    if missing_marketing:
        raise ReviewGateError("MARKETING_REVIEW_REQUIRED: %s" % ",".join(missing_marketing))
    failed_technical = sorted(
        key for key in TECHNICAL_REQUIRED if technical.get(key) is not True)
    failed_marketing = sorted(
        key for key in MARKETING_REQUIRED
        if not (marketing.get(key) is True or
                (not isinstance(marketing.get(key), bool) and
                 isinstance(marketing.get(key), (int, float)) and
                 80 <= marketing.get(key) <= 100)))
    if failed_technical:
        raise ReviewGateError("TECHNICAL_REVIEW_FAILED: %s" % ",".join(failed_technical))
    if failed_marketing:
        raise ReviewGateError("MARKETING_REVIEW_FAILED: %s" % ",".join(failed_marketing))
    score = quality.get("overall_score")
    if (isinstance(score, bool) or not isinstance(score, (int, float)) or
            not 80 <= score <= 100):
        raise ReviewGateError("OVERALL_SCORE_BELOW_80")
    if not artifact_is_current(review) or not artifact_fingerprint_is_current(review):
        raise ReviewGateError("STALE_ARTIFACT")


def record_ocr_review(review, ocr_record):
    """Attach automated or human OCR evidence bound to this exact take."""
    review = copy.deepcopy(review)
    fingerprint = (review.get("artifact") or {}).get("take_fingerprint")
    if ocr_record.get("take_fingerprint") != fingerprint:
        raise ReviewGateError("OCR_TAKE_FINGERPRINT_MISMATCH")
    if ocr_record.get("status") not in ("clear", "detected", "unavailable", "error"):
        raise ReviewGateError("INVALID_OCR_STATUS")
    review["ocr"] = copy.deepcopy(ocr_record)
    review["history"].append({"event": "ocr_review_recorded", "status": ocr_record["status"],
                              "at": _now()})
    return review


def create_review(take, segment, project_id="", run_id=""):
    take_id = take.get("take_id") or "%s-take-01" % segment.get("id")
    fingerprint = take.get("take_fingerprint") or take_fingerprint(take)
    return {
        "schema_version": 1, "review_id": "review-%s-%s" % (take_id, uuid.uuid4().hex[:8]),
        "project_id": project_id, "run_id": run_id, "segment_id": segment.get("id"),
        "scene_id": segment.get("scene_id"), "take_id": take_id, "created_at": _now(),
        "artifact": {"task_id": take.get("taskId"), "video_url": take.get("videoUrl"),
                     "local_path": take.get("localPath"), "sha256": file_sha256(take.get("localPath")),
                     "take_fingerprint": fingerprint, "video_handoff_fingerprint": take.get("video_handoff_fingerprint"),
                     "model_requested": take.get("initial_model"), "model_used": take.get("model"),
                      "fallback_reason": take.get("fallback_reason")},
        "review_sources": [], "quality": {"technical": {}, "marketing": {}, "overall_score": None},
        "requirements": {}, "issues": {"immutable_errors": [], "transient_warnings": []},
        "observed_end_state": None,
        "decision": {"verdict": "pending", "decided_by": None, "decided_at": None,
                     "reason": None, "accepted_with_warning_ids": []},
        "history": [{"event": "review_created", "at": _now()}],
    }


def import_observation(review, observation, source):
    review = copy.deepcopy(review)
    if observation.get("take_fingerprint") and observation["take_fingerprint"] != review["artifact"]["take_fingerprint"]:
        raise ReviewGateError("TAKE_FINGERPRINT_MISMATCH")
    source = dict(source)
    source.setdefault("source_id", "source-%s" % uuid.uuid4().hex[:8])
    source.setdefault("created_at", _now())
    review["review_sources"].append(source)
    for group in ("technical", "marketing"):
        review["quality"].setdefault(group, {}).update((observation.get("quality") or {}).get(group) or {})
    if (observation.get("quality") or {}).get("overall_score") is not None:
        review["quality"]["overall_score"] = observation["quality"]["overall_score"]
    review["requirements"].update(observation.get("requirements") or {})
    if observation.get("observed_end_state"):
        review["observed_end_state"] = observation["observed_end_state"]
    for issue in observation.get("issues") or []:
        add_issue(review, issue.get("classification", "transient_warning"),
                  issue.get("code", "UNKNOWN"), issue.get("message", ""),
                  source["source_id"], issue.get("evidence"))
    review["history"].append({"event": "observation_imported", "source_id": source["source_id"], "at": _now()})
    return review


def add_issue(review, issue_class, code, message, source_id=None, evidence=None):
    target = "immutable_errors" if issue_class == "immutable_error" else "transient_warnings"
    issue = {"issue_id": "issue-%s" % uuid.uuid4().hex[:8], "code": code,
             "message": message, "source_id": source_id, "evidence": evidence or {},
             "status": "open", "created_at": _now()}
    review.setdefault("issues", {}).setdefault(target, []).append(issue)
    return review


def decide(review, verdict, actor, reason, accepted_warning_ids=None, require_end_state=False,
           draft_acceptance=False):
    if verdict not in VERDICTS:
        raise ReviewGateError("UNKNOWN_VERDICT")
    review = copy.deepcopy(review)
    if not str(actor or "").strip() or not str(reason or "").strip():
        raise ReviewGateError("DECISION_ACTOR_REASON_REQUIRED")
    if review.get("decision", {}).get("verdict") == "rejected" and verdict == "accepted":
        raise ReviewGateError("REJECTED_REVIEW_IS_IMMUTABLE")
    if verdict == "accepted":
        if not draft_acceptance:
            _require_quality(review)
        if review.get("issues", {}).get("immutable_errors"):
            raise ReviewGateError("IMMUTABLE_ISSUE_PRESENT")
        open_ids = {item["issue_id"] for item in review.get("issues", {}).get("transient_warnings", [])
                    if item.get("status") in ("open", "acknowledged")}
        accepted = set(accepted_warning_ids or [])
        if not open_ids.issubset(accepted):
            raise ReviewGateError("UNACKNOWLEDGED_WARNING")
        if require_end_state and not review.get("observed_end_state"):
            raise ReviewGateError("OBSERVED_END_STATE_REQUIRED")
    old = review.get("decision", {}).get("verdict", "pending")
    review["decision"] = {"verdict": verdict, "decided_by": actor, "decided_at": _now(),
                           "reason": reason, "accepted_with_warning_ids": accepted_warning_ids or [],
                           "acceptance_mode": "draft" if draft_acceptance else "formal"}
    review["history"].append({"event": "verdict_set", "from": old, "to": verdict, "at": _now()})
    return review


def is_accepted(review, expected_take_fingerprint=None):
    if (review.get("decision", {}).get("verdict") != "accepted" or
            review.get("issues", {}).get("immutable_errors") or
            (expected_take_fingerprint and
             review.get("artifact", {}).get("take_fingerprint") != expected_take_fingerprint)):
        return False
    if review.get("decision", {}).get("acceptance_mode") == "draft":
        return True
    if review.get("decision", {}).get("acceptance_mode") != "formal":
        return False
    try:
        _require_quality(review)
    except ReviewGateError:
        return False
    return True


def validation_problems(review):
    problems = []
    decision = review.get("decision") or {}
    verdict = decision.get("verdict")
    if verdict not in VERDICTS:
        problems.append("UNKNOWN_VERDICT")
        return problems
    if verdict == "accepted":
        if not str(decision.get("decided_by") or "").strip() \
                or not str(decision.get("reason") or "").strip():
            problems.append("DECISION_ACTOR_REASON_REQUIRED")
        if decision.get("acceptance_mode") != "draft":
            try:
                _require_quality(review)
            except ReviewGateError as exc:
                problems.append(str(exc))
        if review.get("issues", {}).get("immutable_errors"):
            problems.append("IMMUTABLE_ISSUE_PRESENT")
    return problems


def attach_to_result(result, review):
    result = copy.deepcopy(result)
    expected = result.get("take_fingerprint")
    if not is_accepted(review, expected):
        raise ReviewGateError("TAKE_REVIEW_REQUIRED")
    if (result.get("video_handoff_fingerprint")
            and review.get("artifact", {}).get("video_handoff_fingerprint")
            != result.get("video_handoff_fingerprint")):
        raise ReviewGateError("STALE_VIDEO_HANDOFF")
    result["review_status"] = "accepted"
    result["review_id"] = review.get("review_id")
    result["accepted_at"] = review.get("decision", {}).get("decided_at")
    return result


def save_review(review, path):
    # 契约强制：take-review schema 运行时校验（fail-closed，防畸形评审落盘）
    schema_validate.enforce(review, "take-review", context="take_review.save_review")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    atomic_write_json(path, review)
    return os.path.abspath(path)


def _main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--review", required=True)
    create = sub.add_parser("create")
    create.add_argument("--take", required=True)
    create.add_argument("--segment", required=True)
    create.add_argument("--project-id", default="")
    create.add_argument("--run-id", default="")
    create.add_argument("--out", required=True)
    decide_cmd = sub.add_parser("decide")
    decide_cmd.add_argument("--review", required=True)
    decide_cmd.add_argument("--verdict", choices=sorted(VERDICTS), required=True)
    decide_cmd.add_argument("--actor", required=True)
    decide_cmd.add_argument("--reason", required=True)
    decide_cmd.add_argument("--accept-warning", action="append", default=[])
    decide_cmd.add_argument("--require-end-state", action="store_true")
    decide_cmd.add_argument("--draft-acceptance", action="store_true",
                            help="仅草稿兼容：跳过正式质量证据门禁")
    decide_cmd.add_argument("--out", required=True)
    attach = sub.add_parser("attach-result")
    attach.add_argument("--result", required=True)
    attach.add_argument("--review", required=True)
    attach.add_argument("--out", required=True)
    import_cmd = sub.add_parser("import")
    import_cmd.add_argument("--review", required=True)
    import_cmd.add_argument("--observation", required=True)
    import_cmd.add_argument("--source-type", choices=("human", "external_vision"), required=True)
    import_cmd.add_argument("--provider")
    import_cmd.add_argument("--model")
    import_cmd.add_argument("--reviewer")
    import_cmd.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "create":
        with open(args.take, encoding="utf-8") as handle:
            take = json.load(handle)
        with open(args.segment, encoding="utf-8") as handle:
            segment = json.load(handle)
        review = create_review(take, segment, args.project_id, args.run_id)
        save_review(review, args.out)
        output, code = review, 0
    elif args.command == "attach-result":
        with open(args.result, encoding="utf-8") as handle:
            result = json.load(handle)
        with open(args.review, encoding="utf-8") as handle:
            review = json.load(handle)
        output = attach_to_result(result, review)
        save_review(output, args.out)
        code = 0
    elif args.command == "import":
        with open(args.review, encoding="utf-8") as handle:
            review = json.load(handle)
        with open(args.observation, encoding="utf-8") as handle:
            observation = json.load(handle)
        source = {"source_type": args.source_type, "provider": args.provider,
                  "model": args.model, "reviewer": args.reviewer,
                  "confidence": 1.0 if args.source_type == "human" else observation.get("confidence")}
        output = import_observation(review, observation, source)
        save_review(output, args.out)
        code = 0
    else:
        with open(args.review, encoding="utf-8") as handle:
            review = json.load(handle)
        if args.command == "decide":
            output = decide(review, args.verdict, args.actor, args.reason,
                            args.accept_warning, args.require_end_state,
                            args.draft_acceptance)
            save_review(output, args.out)
            code = 0
        else:
            problems = validation_problems(review)
            output, code = {"ok": not problems, "problems": problems}, (0 if not problems else 2)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return code


def main(argv=None):
    try:
        return _main(argv)
    except (ReviewGateError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc),
                          "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
