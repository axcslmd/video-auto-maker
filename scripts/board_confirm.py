#!/usr/bin/env python3
"""Storyboard and cast-board confirmation and fingerprint binding.

Extracted from storyboard.py (v3 split). Contains:
  - confirm_board (confirm one board, bind to source fingerprint)
  - confirm_storyboard (confirm all storyboards in a run)
  - _approval_current (check if approval is still valid)
  - storyboard_approval_is_current (top-level check)
  - _source_refs_fingerprint (hash reference content for approval binding)
  - _file_sha256 (file content hash)

Dependencies: None (pure stdlib + hashlib)
"""
import os
import sys
import json
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

ROOT = os.path.dirname(HERE)
EMPTY_SOURCE_FINGERPRINT = hashlib.sha256(b"").hexdigest()


def _has_customer_confirmation_evidence(record):
    return bool(
        isinstance(record, dict) and
        record.get("confirmed_by_customer") is True and
        str(record.get("customer_confirmation") or "").strip() and
        record.get("confirmation_source") == "customer_dialogue")


def _file_sha256(path):
    """Compute sha256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _approval_path(out_dir, kind):
    """Return the path to the approval marker file."""
    return os.path.join(out_dir, ".%s_confirmed.json" % kind)


def _source_refs_fingerprint(refs):
    """Hash reference contents so approvals expire when an upload changes."""
    digest = hashlib.sha256()
    for value in refs or []:
        digest.update(str(value).encode("utf-8"))
        path = value if isinstance(value, str) and os.path.isabs(value) else os.path.join(ROOT, value) if isinstance(value, str) else None
        if path and os.path.isfile(path):
            with open(path, "rb") as handle:
                digest.update(handle.read())
        digest.update(b"\0")
    return digest.hexdigest()


def _approval_current(out_dir, kind, source_fingerprint):
    """Check if an approval marker is still valid (fingerprint + file integrity)."""
    try:
        with open(_approval_path(out_dir, kind), encoding="utf-8") as handle:
            record = json.load(handle)
        if (not source_fingerprint or source_fingerprint == EMPTY_SOURCE_FINGERPRINT or
                not _has_customer_confirmation_evidence(record) or
                record.get("status") != "confirmed" or
                record.get("source_fingerprint") != source_fingerprint):
            return False
        board_path = record.get("path")
        board_sha256 = record.get("board_sha256")
        if board_sha256:
            return bool(board_path and os.path.isfile(board_path) and
                        _file_sha256(board_path) == board_sha256)
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def confirm_board(result_json, kind):
    """Fail closed: only storyboard.py owns customer-evidenced approval writes."""
    raise RuntimeError(
        "DIRECT_CONFIRMATION_FORBIDDEN: use scripts/storyboard.py "
        "--confirm-board <kind> --result-json <path> "
        "--customer-confirmation <客户确认原话>")


def confirm_storyboard(result_json):
    """Fail closed: only storyboard.py owns customer-evidenced approval writes."""
    raise RuntimeError(
        "DIRECT_CONFIRMATION_FORBIDDEN: use scripts/storyboard.py --confirm "
        "--result-json <path> --customer-confirmation <客户确认原话>")


def storyboard_approval_is_current(result_json, *, client=None, run_id=None,
                                    plan_fingerprint=None):
    """Check if the storyboard approval is still valid.

    Returns True only if:
      1. An approval marker exists
      2. The marker's plan_fingerprint matches the current plan
      3. All shot files still exist with matching sha256
    """
    with open(result_json, encoding="utf-8") as handle:
        results = json.load(handle)

    out_dir = os.path.dirname(os.path.abspath(result_json))
    current_fp = plan_fingerprint or results.get("plan_fingerprint")

    if not _approval_current(out_dir, "storyboard", current_fp):
        return False

    # Also verify each shot file
    approval_path = _approval_path(out_dir, "storyboard")
    try:
        with open(approval_path, encoding="utf-8") as f:
            approval = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    for shot in approval.get("shots", []):
        path = shot.get("path")
        sha = shot.get("sha256")
        if not path or not os.path.isfile(path):
            return False
        if sha and _file_sha256(path) != sha:
            return False

    return True
