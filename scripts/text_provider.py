#!/usr/bin/env python3
"""Capability-based text-model routing and bounded structured-output repair."""
import argparse
import json
import os
import re

import br_client
import model_behavior
from schema_validate import validate_inline


MODE_SCORE = {"native_strict": 3, "tool_schema": 2, "json_text": 1}
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_scorecards(path=None):
    """Load measured business scores only; absent data means no score bonus."""
    path = path or os.environ.get("BASICROUTER_MODEL_SCORECARD") or os.path.join(
        ROOT, "evals", "model-scorecards.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def model_id(record):
    return str((record or {}).get("modelId") or (record or {}).get("modelName") or
               (record or {}).get("id") or "")


def catalog_records(catalog):
    if isinstance(catalog, list):
        return catalog
    if isinstance(catalog, dict):
        for key in ("data", "models", "items"):
            if isinstance(catalog.get(key), list):
                return catalog[key]
    return []


def online(record):
    return not ((record or {}).get("online") is False or (record or {}).get("status") is False)


def select_text_model(catalog, *, explicit_model=None, task="structured_text",
                      scorecards=None, require_vision=False, schema_depth=3):
    records = [item for item in catalog_records(catalog)
               if isinstance(item, dict) and model_id(item)]
    if explicit_model:
        record = next((item for item in records if model_id(item) == explicit_model),
                      {"id": explicit_model})
        if not online(record):
            raise ValueError("TEXT_MODEL_OFFLINE: %s" % explicit_model)
        profile = model_behavior.profile_from_catalog(record, explicit_model)
        if require_vision and not profile["vision_available"]:
            raise ValueError("TEXT_MODEL_VISION_REQUIRED: %s" % explicit_model)
        return record, profile
    ranked = []
    scorecards = scorecards if isinstance(scorecards, dict) else {}
    for index, record in enumerate(records):
        if not online(record):
            continue
        profile = model_behavior.profile_from_catalog(record)
        if require_vision and not profile["vision_available"]:
            continue
        task_score = float(((scorecards.get(model_id(record)) or {}).get(task)) or 0.0)
        depth_fit = 1 if profile["max_safe_schema_depth"] >= schema_depth else 0
        semantic = profile["metrics"]["semantic_audit_rate"]
        ranked.append(((task_score, depth_fit, MODE_SCORE[profile["structured_output_mode"]],
                        semantic, -index), record, profile))
    if not ranked:
        raise ValueError("TEXT_MODEL_CAPABILITY_UNAVAILABLE")
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1], ranked[0][2]


def extract_json(value):
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.I)
    if fenced:
        text = fenced.group(1).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
            return parsed
        except ValueError:
            continue
    raise ValueError("STRUCTURED_OUTPUT_JSON_MISSING")


class StructuredOutputTerminalError(ValueError):
    """A refusal or truncated response that must not trigger paid fallbacks."""


def _terminal_response_error(resp):
    if not isinstance(resp, dict):
        return None
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    choices = data.get("choices") if isinstance(data, dict) else None
    if choices:
        choice = choices[0] or {}
        message = choice.get("message") or {}
        if message.get("refusal"):
            return "STRUCTURED_OUTPUT_REFUSED: %s" % message["refusal"]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            return "STRUCTURED_OUTPUT_INCOMPLETE: %s" % choice.get("finish_reason")
    if isinstance(data, dict):
        if data.get("stop_reason") in {"max_tokens", "refusal", "content_filter"}:
            return "STRUCTURED_OUTPUT_INCOMPLETE: %s" % data.get("stop_reason")
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "refusal":
                return "STRUCTURED_OUTPUT_REFUSED: %s" % (block.get("text") or "refused")
    return None


_PORTABLE_SCHEMA_KEYS = {
    "type", "title", "description", "properties", "required",
    "additionalProperties", "items", "enum", "const", "minimum", "maximum",
    "minItems", "maxItems", "minLength", "maxLength", "anyOf", "$defs", "$ref",
}


def portable_strict_schema(schema):
    """Compile a provider-portable strict subset; application validation stays full."""
    if not isinstance(schema, dict):
        return schema
    if not schema:
        return {"type": ["string", "number", "integer", "boolean", "object", "array", "null"]}
    result = {}
    for key, value in schema.items():
        if key not in _PORTABLE_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: portable_strict_schema(child)
                           for name, child in value.items()}
        elif key == "items":
            result[key] = portable_strict_schema(value)
        elif key in {"anyOf"} and isinstance(value, list):
            result[key] = [portable_strict_schema(item) for item in value]
        elif key == "$defs" and isinstance(value, dict):
            result[key] = {name: portable_strict_schema(child)
                           for name, child in value.items()}
        else:
            result[key] = value
    if result.get("type") == "object" or "properties" in result:
        properties = result.setdefault("properties", {})
        originally_required = set(schema.get("required") or [])
        for name, child in list(properties.items()):
            if name in originally_required:
                continue
            child = dict(child) if isinstance(child, dict) else {}
            child_type = child.get("type")
            if child_type is None:
                child["type"] = ["string", "number", "integer", "boolean", "object", "array", "null"]
            elif isinstance(child_type, list):
                if "null" not in child_type:
                    child["type"] = list(child_type) + ["null"]
            elif child_type != "null":
                child["type"] = [child_type, "null"]
            if "enum" in child and None not in child["enum"]:
                child["enum"] = list(child["enum"]) + [None]
            properties[name] = child
        result["required"] = list(properties.keys())
        result["additionalProperties"] = False
    return result


def strict_schema_constrainable(schema):
    """Return false when strict compilation would erase unconstrained objects."""
    if not isinstance(schema, dict) or not schema:
        return False
    schema_type = schema.get("type")
    if schema_type == "object" and not isinstance(schema.get("properties"), dict):
        return False
    for child in (schema.get("properties") or {}).values():
        if not strict_schema_constrainable(child):
            return False
    if "items" in schema and not strict_schema_constrainable(schema["items"]):
        return False
    for child in schema.get("anyOf") or []:
        if not strict_schema_constrainable(child):
            return False
    return True


def _call_mode(api_key, messages, model, schema, mode, timeout):
    strict_schema = portable_strict_schema(schema)
    if mode == "native_strict":
        raw = br_client.chat_completion(
            api_key, messages, model=model, timeout=timeout,
            response_format={"type": "json_schema", "json_schema": {
                "name": "submit_result", "strict": True, "schema": strict_schema}})
        terminal = _terminal_response_error(raw)
        if terminal:
            raise StructuredOutputTerminalError(terminal)
        return extract_json(br_client._extract_chat_text(raw)), raw
    if mode == "tool_schema":
        raw = br_client.chat_completion(
            api_key, messages, model=model, timeout=timeout,
            tools=[{"type": "function", "function": {
                "name": "submit_result",
                "description": "提交符合当前业务合同的唯一结构化结果",
                "parameters": strict_schema,
                "strict": True}}],
            tool_choice={"type": "function", "function": {"name": "submit_result"}},
            parallel_tool_calls=False)
        terminal = _terminal_response_error(raw)
        if terminal:
            raise StructuredOutputTerminalError(terminal)
        arguments = br_client.extract_tool_arguments(raw, "submit_result")
        if arguments is None:
            arguments = extract_json(br_client._extract_chat_text(raw))
        return arguments, raw
    json_messages = list(messages)
    json_messages.insert(0, {"role": "system", "content":
        "只返回一个 JSON 对象，不要 Markdown。输出必须符合给定 JSON Schema；应用层会再次校验。Schema: %s" %
        json.dumps(schema, ensure_ascii=False, separators=(",", ":"))})
    raw = br_client.chat_completion(api_key, json_messages, model=model, timeout=timeout)
    terminal = _terminal_response_error(raw)
    if terminal:
        raise StructuredOutputTerminalError(terminal)
    return extract_json(br_client._extract_chat_text(raw)), raw


def chat_json(api_key, messages, schema, *, model=None, catalog=None,
              task="structured_text", scorecards=None, timeout=180, max_repairs=1):
    """Return validated JSON plus auditable provider metadata.

    Fallback order follows declared capability, then smaller compatible modes.
    Every mode is application-validated; repair is bounded and never approves a
    business artifact.
    """
    catalog = catalog if catalog is not None else br_client.list_models(category="text")
    scorecards = load_scorecards() if scorecards is None else scorecards
    record, profile = select_text_model(
        catalog, explicit_model=model,
        task=task, scorecards=scorecards, schema_depth=4)
    selected = model_id(record) or str(model)
    preferred = profile["structured_output_mode"]
    strict_compatible = strict_schema_constrainable(schema)
    modes = ((["native_strict", "tool_schema", "json_text"] if preferred == "native_strict"
              else (["tool_schema", "json_text"] if preferred == "tool_schema" else ["json_text"]))
             if strict_compatible else ["json_text"])
    attempts = []
    last_error = None
    for mode in modes:
        current_messages = list(messages)
        for repair_index in range(max_repairs + 1):
            try:
                result, _raw = _call_mode(api_key, current_messages, selected, schema, mode, timeout)
                errors = validate_inline(result, schema)
                attempts.append({"mode": mode, "repair": repair_index, "valid": not errors,
                                 "errors": errors[:8]})
                if not errors:
                    return result, {"model": selected, "mode": mode,
                                    "profile": profile, "attempts": attempts,
                                    "strict_schema_compatible": strict_compatible}
                last_error = "；".join(errors[:8])
                current_messages.extend([
                    {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
                    {"role": "user", "content": "上次输出未通过合同。只修复以下错误，不改变已正确事实：%s" % last_error},
                ])
            except StructuredOutputTerminalError:
                raise
            except (br_client.BRError, OSError, ValueError, TypeError, RuntimeError) as exc:
                last_error = str(exc)
                attempts.append({"mode": mode, "repair": repair_index, "valid": False,
                                 "errors": [last_error[:500]]})
                break
    raise ValueError("STRUCTURED_OUTPUT_FAILED[%s]: %s" % (selected, last_error or "unknown"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--model")
    parser.add_argument("--task", default="structured_text")
    parser.add_argument("--vision", action="store_true")
    args = parser.parse_args(argv)
    with open(args.catalog, encoding="utf-8") as handle:
        catalog = json.load(handle)
    record, profile = select_text_model(catalog, explicit_model=args.model,
                                        task=args.task, require_vision=args.vision)
    print(json.dumps({"model": model_id(record), "profile": profile},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
