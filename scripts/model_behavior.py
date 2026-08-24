#!/usr/bin/env python3
"""Capability- and observation-based LLM behavior profiles.

Model names are diagnostic identifiers, never capability or authorization
rules.  Unknown models start conservatively and earn larger interaction batches
only through observed business-contract performance.
"""
import argparse
import datetime
import json
import os
import re

from project_utils import atomic_write_json
from schema_validate import enforce


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _slug(value):
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "unknown")).strip("-.")
    return text[:96] or "unknown"


def conservative_profile(model_id="unknown"):
    payload = {
        "schema_version": 1,
        "profile_id": "unknown-%s" % _slug(model_id),
        "model_id": str(model_id or "unknown"),
        "source": "unknown",
        "structured_output_mode": "json_text",
        "max_safe_schema_depth": 3,
        "max_safe_items_per_turn": 1,
        "vision_available": False,
        "context_tier": "small",
        "metrics": {
            "tool_call_reliability": 0.5,
            "first_pass_schema_rate": 0.5,
            "semantic_audit_rate": 0.5,
            "repair_success_rate": 0.5,
        },
        "sample_count": 0,
        "updated_at": _now(),
    }
    enforce(payload, "model-behavior-profile", context="conservative_profile")
    return payload


def _truthy(record, *keys):
    for key in keys:
        value = record.get(key)
        if value is True:
            return True
        if isinstance(value, (list, tuple, set, dict)) and bool(value):
            return True
        if isinstance(value, str) and value.lower() in {"true", "yes", "supported", "strict"}:
            return True
    return False


def profile_from_catalog(record, model_id=None):
    """Normalize provider metadata without guessing from the model name."""
    record = record if isinstance(record, dict) else {}
    raw_capabilities = record.get("capabilities")
    if isinstance(raw_capabilities, dict):
        capabilities = raw_capabilities
    elif isinstance(raw_capabilities, (list, tuple, set)):
        capabilities = {str(item): True for item in raw_capabilities}
    else:
        capabilities = {}
    combined = dict(record)
    combined.update(capabilities)
    identifier = (model_id or record.get("modelId") or record.get("modelName") or
                  record.get("id") or "unknown")
    native = _truthy(combined, "structured_output", "structuredOutputs",
                     "json_schema", "response_schema", "strict")
    tools = _truthy(combined, "tool_calling", "tools", "function_calling")
    if native:
        mode, depth, items = "native_strict", 5, 6
    elif tools:
        mode, depth, items = "tool_schema", 4, 4
    else:
        mode, depth, items = "json_text", 3, 1
    modalities = (combined.get("input_modalities") or
                  combined.get("multimodelTypes") or [])
    if isinstance(modalities, str):
        try:
            modalities = json.loads(modalities)
        except ValueError:
            modalities = [modalities]
    context = combined.get("context_window") or combined.get("contextWindow") or 0
    try:
        context = int(context)
    except (TypeError, ValueError):
        context = 0
    context_tier = "large" if context >= 100000 else ("medium" if context >= 32000 else "small")
    baseline = 0.75 if native else (0.68 if tools else 0.5)
    payload = {
        "schema_version": 1,
        "profile_id": "declared-%s" % _slug(identifier),
        "model_id": str(identifier),
        "source": "declared",
        "structured_output_mode": mode,
        "max_safe_schema_depth": depth,
        "max_safe_items_per_turn": items,
        "vision_available": "image" in set(map(str, modalities)),
        "context_tier": context_tier,
        "metrics": {
            "tool_call_reliability": baseline,
            "first_pass_schema_rate": baseline,
            "semantic_audit_rate": 0.5,
            "repair_success_rate": baseline,
        },
        "sample_count": 0,
        "updated_at": _now(),
    }
    enforce(payload, "model-behavior-profile", context="profile_from_catalog")
    return payload


def observe(profile, outcome):
    """Update rates with one contract outcome; booleans only, no model secrets."""
    current = json.loads(json.dumps(profile))
    enforce(current, "model-behavior-profile", context="observe.input")
    outcome = outcome if isinstance(outcome, dict) else {}
    mapping = {
        "tool_call_reliability": "tool_call_ok",
        "first_pass_schema_rate": "first_pass_schema_ok",
        "semantic_audit_rate": "semantic_audit_ok",
        "repair_success_rate": "repair_ok",
    }
    missing = [key for key in mapping.values() if key not in outcome]
    if missing:
        raise ValueError("MODEL_OBSERVATION_INCOMPLETE: %s" % ", ".join(missing))
    count = int(current.get("sample_count") or 0)
    for metric, key in mapping.items():
        if key not in outcome:
            continue
        previous = float(current["metrics"].get(metric, 0.5))
        current["metrics"][metric] = round(
            ((previous * count) + (1.0 if bool(outcome[key]) else 0.0)) /
            float(count + 1), 6)
    current["sample_count"] = count + 1
    current["source"] = "observed"
    current["profile_id"] = "observed-%s" % _slug(current.get("model_id"))
    current["updated_at"] = _now()
    enforce(current, "model-behavior-profile", context="observe.output")
    return current


def interaction_policy(profile):
    """Choose granularity from evidence, never from provider/model branding."""
    profile = profile or conservative_profile()
    enforce(profile, "model-behavior-profile", context="interaction_policy")
    metrics = profile["metrics"]
    sample_count = int(profile.get("sample_count") or 0)
    minimum = min(float(metrics[key]) for key in (
        "tool_call_reliability", "first_pass_schema_rate",
        "semantic_audit_rate", "repair_success_rate"))
    if sample_count >= 10 and minimum >= 0.9:
        return {"tier": "high", "mode": "composite",
                "max_items_per_turn": min(12, profile["max_safe_items_per_turn"]),
                "reason": "至少 10 次业务观测且所有关键通过率均不低于 90%"}
    output_minimum = min(float(metrics[key]) for key in (
        "tool_call_reliability", "first_pass_schema_rate", "repair_success_rate"))
    if (profile["structured_output_mode"] in {"native_strict", "tool_schema"}
            and output_minimum >= 0.65):
        return {"tier": "medium", "mode": "batched",
                "max_items_per_turn": min(4, profile["max_safe_items_per_turn"]),
                "reason": "端点声明结构化能力，但尚未积累足够高置信业务样本"}
    return {"tier": "low", "mode": "atomic", "max_items_per_turn": 1,
            "reason": "未知或低置信模型按最小可验证任务运行"}


def load_profile(path=None, model_id="unknown"):
    if not path:
        return conservative_profile(model_id)
    try:
        with open(path, encoding="utf-8") as handle:
            profile = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("MODEL_PROFILE_UNAVAILABLE: %s" % exc) from exc
    enforce(profile, "model-behavior-profile", context="load_profile")
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    default = sub.add_parser("default")
    default.add_argument("--model", default="unknown")
    default.add_argument("--out")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--catalog-record", required=True)
    inspect.add_argument("--model")
    inspect.add_argument("--out")
    policy = sub.add_parser("policy")
    policy.add_argument("--profile")
    policy.add_argument("--model", default="unknown")
    observation = sub.add_parser("observe")
    observation.add_argument("--profile", required=True)
    observation.add_argument("--out", required=True)
    for key in ("tool-call-ok", "first-pass-schema-ok", "semantic-audit-ok", "repair-ok"):
        observation.add_argument("--%s" % key, choices=("true", "false"))
    args = parser.parse_args(argv)
    if args.command == "default":
        result = conservative_profile(args.model)
    elif args.command == "inspect":
        with open(args.catalog_record, encoding="utf-8") as handle:
            result = profile_from_catalog(json.load(handle), model_id=args.model)
    elif args.command == "policy":
        result = interaction_policy(load_profile(args.profile, args.model))
    else:
        profile = load_profile(args.profile)
        outcome = {}
        for attr, key in (("tool_call_ok", "tool_call_ok"),
                          ("first_pass_schema_ok", "first_pass_schema_ok"),
                          ("semantic_audit_ok", "semantic_audit_ok"),
                          ("repair_ok", "repair_ok")):
            value = getattr(args, attr)
            if value is not None:
                outcome[key] = value == "true"
        result = observe(profile, outcome)
    out = getattr(args, "out", None)
    if out:
        parent = os.path.dirname(os.path.abspath(out))
        os.makedirs(parent, exist_ok=True)
        atomic_write_json(out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
