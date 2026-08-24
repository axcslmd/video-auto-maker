#!/usr/bin/env python3
"""Content-bound integrity helpers for prompt review artifacts.

The review file is the customer confirmation contract. Generation entrypoints
must not trust only ``status: confirmed`` because a later JSON edit could change
the actual submitted prompts while keeping the status field.
"""
import copy
import hashlib
import json


FINGERPRINT_FIELD = "review_fingerprint"


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def review_payload(review):
    """Return the bytes that are bound by ``review_fingerprint``."""
    if not isinstance(review, dict):
        return review
    payload = copy.deepcopy(review)
    payload.pop(FINGERPRINT_FIELD, None)
    return payload


def review_fingerprint(review):
    """Fingerprint a review without including the fingerprint field itself."""
    return _digest(review_payload(review))


def assert_current(review, *, path=None):
    """Raise if a saved review was edited after its fingerprint was written."""
    if not isinstance(review, dict):
        raise ValueError("PROMPT_REVIEW_INTEGRITY_FAILED: 审核文件不是 JSON object")
    stored = str(review.get(FINGERPRINT_FIELD) or "").strip()
    current = review_fingerprint(review)
    location = (" (%s)" % path) if path else ""
    if (review.get("status") == "confirmed" and
            (review.get("confirmed_by_customer") is not True or
             not str(review.get("customer_confirmation") or "").strip() or
             review.get("confirmation_source") != "customer_dialogue" or
             not review.get("confirmed_at"))):
        raise ValueError(
            "PROMPT_REVIEW_CUSTOMER_EVIDENCE_REQUIRED: 已确认提示词缺少客户确认原话%s；"
            "请重新展示并运行 prompt_review.py confirm --customer-confirmation '<客户确认原话>'。"
            % location)
    if not stored:
        raise ValueError(
            "PROMPT_REVIEW_INTEGRITY_REQUIRED: 已确认提示词审核文件缺少 review_fingerprint%s；"
            "请重新 capture/polish、展示给客户确认后再运行 "
            "prompt_review.py confirm --customer-confirmation '<客户确认原话>'。"
            % location)
    if stored != current:
        raise ValueError(
            "PROMPT_REVIEW_INTEGRITY_MISMATCH: 提示词审核文件在确认后被修改%s；"
            "stored=%s current=%s。请重新 capture/polish、展示给客户确认后再 confirm。"
            % (location, stored, current))
    return current
