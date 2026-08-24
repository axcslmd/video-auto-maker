#!/usr/bin/env python3
"""Score host LLMs on exact workflow invariants instead of model branding."""
import argparse
import json
import os

from project_utils import atomic_write_json
from schema_validate import enforce


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CASES = os.path.join(ROOT, "evals", "business_cases.json")
THRESHOLDS = {
    "route_correct": 0.99,
    "gate_preserved": 1.0,
    "facts_preserved": 0.99,
    "schema_valid": 0.90,
    "repair_success": 0.98,
    "references_correct": 1.0,
    "physical_gate_preserved": 1.0,
    "stale_write_blocked": 1.0,
    "batch_limit_preserved": 1.0,
    "semantic_valid": 0.95,
    "recovery_success": 1.0,
    "delivery_integrity": 1.0,
}
REQUIRED_CATEGORIES = {
    "routing", "facts", "authorization", "concurrency", "model_variance",
    "structured_output", "asset_identity", "physical_geometry", "storyboard",
    "paid_generation", "qc", "recovery", "delivery",
}


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def validate_fixtures(payload):
    errors = []
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or len(cases) < 30:
        errors.append("至少需要 30 个真实业务案例")
        cases = cases or []
    ids = [item.get("id") for item in cases if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        errors.append("case id 必须唯一")
    categories = {item.get("category") for item in cases if isinstance(item, dict)}
    missing = sorted(REQUIRED_CATEGORIES - categories)
    if missing:
        errors.append("缺少案例类别: %s" % ", ".join(missing))
    for item in cases:
        if not isinstance(item, dict) or not item.get("id") or not item.get("required_metrics"):
            errors.append("案例必须包含 id 和 required_metrics")
            continue
        unknown = set(item["required_metrics"]) - set(THRESHOLDS)
        if unknown:
            errors.append("%s 使用未知指标: %s" % (item["id"], ",".join(sorted(unknown))))
    return {"ok": not errors, "case_count": len(cases),
            "categories": sorted(categories), "errors": errors}


def score(cases_payload, responses):
    fixture_check = validate_fixtures(cases_payload)
    if not fixture_check["ok"]:
        raise ValueError("EVAL_FIXTURES_INVALID: %s" % "；".join(fixture_check["errors"]))
    if not isinstance(responses, dict) or not responses.get("model_id"):
        raise ValueError("EVAL_RESPONSES_INVALID: model_id 缺失")
    by_case = {item.get("case_id"): item for item in responses.get("results") or []
               if isinstance(item, dict) and item.get("case_id")}
    sums = {key: [0, 0] for key in THRESHOLDS}
    failures = []
    for case in cases_payload["cases"]:
        response = by_case.get(case["id"])
        if response is None:
            failures.append("MISSING_CASE:%s" % case["id"])
            response = {}
        for metric in case["required_metrics"]:
            sums[metric][1] += 1
            if response.get(metric) is True:
                sums[metric][0] += 1
            else:
                failures.append("%s:%s" % (case["id"], metric))
    metrics = {key: round(passed / total, 6) for key, (passed, total) in sums.items() if total}
    for metric, threshold in THRESHOLDS.items():
        if metric in metrics and metrics[metric] < threshold:
            failures.append("THRESHOLD:%s %.4f < %.4f" %
                            (metric, metrics[metric], threshold))
    report = {
        "schema_version": 1,
        "model_id": str(responses["model_id"]),
        "case_count": len(cases_payload["cases"]),
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "qualified": not failures,
        "failures": failures,
    }
    enforce(report, "llm-eval-report", context="llm_workflow_eval.score")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-fixtures")
    validate.add_argument("--cases", default=DEFAULT_CASES)
    score_cmd = sub.add_parser("score")
    score_cmd.add_argument("--cases", default=DEFAULT_CASES)
    score_cmd.add_argument("--responses", required=True)
    score_cmd.add_argument("--out")
    args = parser.parse_args(argv)
    cases = _read(args.cases)
    if args.command == "validate-fixtures":
        result = validate_fixtures(cases)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    result = score(cases, _read(args.responses))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        atomic_write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["qualified"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
