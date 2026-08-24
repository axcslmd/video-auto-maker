#!/usr/bin/env python3
"""Run release gates and emit a content-bound, secret-free validation report."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

import package_delivery
from project_utils import atomic_write_json
from schema_validate import validate as validate_schema


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEST_RESULT_RE = re.compile(r"(?:^|\s)(\d+) passed(?:\s|$)")


def _digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def source_fingerprint():
    """Hash exactly the generic runtime source set (offline payload excluded)."""
    digest = hashlib.sha256()
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if package_delivery._is_excluded(
                path, relative, include_local_runtime=False):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        if relative.parts and relative.parts[0] in {"assets", "brand", "actors"}:
            if len(relative.parts) > 1 and relative.parts[1] != "_template":
                continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(package_delivery._sha256(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def _commands():
    python = sys.executable
    return (
        ("python_compile", [python, "-m", "compileall", "-q", "scripts"]),
        ("workflow_contract", [python, "scripts/workflow_router.py", "validate"]),
        ("architecture_contract", [python, "scripts/architecture_audit.py", "validate"]),
        ("skill_contract", [python, "scripts/skill_contract.py", "validate"]),
        ("module_packages", [python, "scripts/module_packages.py", "validate"]),
        ("llm_business_evals", [python, "scripts/llm_workflow_eval.py",
                                "validate-fixtures", "--cases", "evals/business_cases.json"]),
        ("offline_canary", [python, "scripts/canary_replay.py", "--dry-run"]),
        ("offline_runtime", [python, "scripts/offline_setup.py", "check"]),
        ("tests", [python, "-m", "pytest", "-q"]),
    )


def _run(name, argv):
    started = time.monotonic()
    completed = subprocess.run(argv, cwd=str(ROOT), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
    duration_ms = int((time.monotonic() - started) * 1000)
    record = {
        "name": name,
        "argv": [str(item) for item in argv],
        "exit_code": completed.returncode,
        "stdout_sha256": _digest_bytes(completed.stdout),
        "stderr_sha256": _digest_bytes(completed.stderr),
        "duration_ms": max(0, duration_ms),
    }
    return record, completed.stdout.decode("utf-8", errors="replace"), \
        completed.stderr.decode("utf-8", errors="replace")


def run_audit(output):
    command_records = []
    outputs = {}
    failures = []
    for name, argv in _commands():
        record, stdout, stderr = _run(name, argv)
        command_records.append(record)
        outputs[name] = stdout
        if record["exit_code"] != 0:
            # Do not persist tool output; only give the operator a short label.
            failures.append("%s(exit=%d)" % (name, record["exit_code"]))
    if failures:
        raise RuntimeError("RELEASE_GATES_FAILED: %s" % ", ".join(failures))
    match = TEST_RESULT_RE.search(outputs.get("tests") or "")
    if not match:
        raise RuntimeError("RELEASE_TEST_COUNT_MISSING")
    workflow = json.loads(outputs["workflow_contract"])
    skills = json.loads(outputs["skill_contract"])
    evals = json.loads(outputs["llm_business_evals"])
    offline = json.loads(outputs["offline_runtime"])
    report = {
        "schema_version": 1,
        "kind": "release_validation",
        "status": "passed",
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "project_fingerprint": source_fingerprint(),
        "python": "%s %s" % (platform.python_implementation(), platform.python_version()),
        "commands": command_records,
        "tests_passed": int(match.group(1)),
        "invariants": {
            "business_stage_count": workflow.get("stage_count"),
            "chain_profile_count": workflow.get("chain_profile_count"),
            "skill_count": skills.get("skill_count"),
            "host_adapter_count": len(skills.get("host_adapters") or {}),
            "eval_case_count": evals.get("case_count"),
            "offline_runtime_verified": offline.get("ok") is True,
        },
    }
    errors = validate_schema(report, "release-validation")
    if errors:
        raise RuntimeError("RELEASE_REPORT_SCHEMA_FAILED: %s" % "; ".join(errors[:20]))
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(output), report)
    return report


def validate_report(path, *, require_current=False):
    errors = []
    path = Path(path).expanduser().resolve()
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": ["RELEASE_REPORT_INVALID:%s" % type(exc).__name__],
                "project_fingerprint": None}
    errors.extend(validate_schema(report, "release-validation"))
    if require_current and report.get("project_fingerprint") != source_fingerprint():
        errors.append("RELEASE_REPORT_SOURCE_STALE")
    return {"ok": not errors, "errors": errors,
            "project_fingerprint": report.get("project_fingerprint"),
            "tests_passed": report.get("tests_passed")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--out", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--report", required=True)
    verify.add_argument("--require-current", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "run":
        report = run_audit(args.out)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    result = validate_report(args.report, require_current=args.require_current)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
