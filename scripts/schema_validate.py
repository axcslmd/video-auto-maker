#!/usr/bin/env python3
"""schema_validate — schemas/ 契约的运行时强制校验（fail-closed）。

背景：schemas/ 下 7 份 JSON Schema 长期只是"文档性契约"，运行时零校验，
INC-005（旧计划复用）/ INC-011（参考图静默丢失）本质都是契约未在执行点强制。
本模块在关键写入/提交点统一校验，违例立即阻断。

设计：
- 优先使用 jsonschema（若环境已安装，语义最权威）；
- 未安装时使用内置 mini 校验器，覆盖本项目 schema 用到的全部关键字
  （type / required / properties / items / enum / const / string/array/object
  bounds / additionalProperties），
  保持"客户机零新增依赖"也能 fail-closed。
- 环境变量 SCHEMA_VALIDATE=0 可显式关闭（仅调试用，正式流程不应设置）。

用法：
    from schema_validate import enforce, validate, SchemaContractError
    enforce(event, "generation-run", context="generation_ledger.append_event")
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCHEMA_DIR = os.path.join(_HERE, "..", "schemas")

try:
    import jsonschema as _jsonschema  # type: ignore
except ImportError:
    _jsonschema = None

_SCHEMA_CACHE = {}


class SchemaContractError(ValueError):
    """契约校验失败，fail-closed 阻断。"""


def _enabled():
    return os.environ.get("SCHEMA_VALIDATE", "1") not in ("0", "false", "off")


def _load_schema(name):
    if name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[name]
    path = os.path.join(_SCHEMA_DIR, "%s.schema.json" % name)
    if not os.path.isfile(path):
        raise SchemaContractError(
            "SCHEMA_MISSING: 契约文件不存在 %s——schemas/ 是强制校验的权威来源，缺失即阻断" % path)
    with open(path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    _SCHEMA_CACHE[name] = schema
    return schema


# ------------------------------------------------------------ mini 校验器

_TYPE_MAP = {
    "object": dict, "array": list, "string": str,
    "integer": int, "number": (int, float), "boolean": bool,
}


def _type_ok(value, expected):
    if expected == "null":
        return value is None
    py = _TYPE_MAP.get(expected)
    if py is None:
        return True  # 未知类型不误判
    if expected in ("integer", "number") and isinstance(value, bool):
        return False  # bool 是 int 子类，JSON Schema 语义下不算 number
    return isinstance(value, py)


def _mini_validate(value, schema, path="$", errors=None):
    """返回错误列表；覆盖本项目 schema 实际用到的关键字子集。"""
    if errors is None:
        errors = []
    if not isinstance(schema, dict):
        return errors
    for branch in schema.get("allOf") or []:
        _mini_validate(value, branch, path, errors)
    if isinstance(schema.get("if"), dict):
        condition_errors = _mini_validate(value, schema["if"], path, [])
        branch = schema.get("then") if not condition_errors else schema.get("else")
        if isinstance(branch, dict):
            _mini_validate(value, branch, path, errors)
    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_ok(value, t) for t in types):
            errors.append("%s: 类型应为 %s，实际为 %s"
                          % (path, expected_type, type(value).__name__))
            return errors  # 类型不对时后续子校验无意义
    if "const" in schema and value != schema["const"]:
        errors.append("%s: 值必须恒为 %r，实际为 %r" % (path, schema["const"], value))
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: 值 %r 不在枚举 %s 内" % (path, value, schema["enum"]))
    if isinstance(value, str) and "minLength" in schema:
        if len(value) < schema["minLength"]:
            errors.append("%s: 长度 %d 小于 minLength %d"
                          % (path, len(value), schema["minLength"]))
    if isinstance(value, str) and "maxLength" in schema:
        if len(value) > schema["maxLength"]:
            errors.append("%s: 长度 %d 大于 maxLength %d"
                          % (path, len(value), schema["maxLength"]))
    if isinstance(value, str) and schema.get("pattern"):
        if re.search(schema["pattern"], value) is None:
            errors.append("%s: 值 %r 不匹配 pattern %s"
                          % (path, value, schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: 值 %s 小于 minimum %s"
                          % (path, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s: 值 %s 大于 maximum %s"
                          % (path, value, schema["maximum"]))
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append("%s.%s: 缺少必需字段" % (path, key))
        properties = schema.get("properties") or {}
        for key, sub in properties.items():
            if key in value:
                _mini_validate(value[key], sub, "%s.%s" % (path, key), errors)
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                errors.append("%s.%s: 不允许额外字段" % (path, key))
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            errors.append("%s: 对象字段数 %d 小于 minProperties %d"
                          % (path, len(value), schema["minProperties"]))
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errors.append("%s: 对象字段数 %d 大于 maxProperties %d"
                          % (path, len(value), schema["maxProperties"]))
    if isinstance(value, list) and "minItems" in schema and len(value) < schema["minItems"]:
        errors.append("%s: 数组长度 %d 小于 minItems %d"
                      % (path, len(value), schema["minItems"]))
    if isinstance(value, list) and "maxItems" in schema and len(value) > schema["maxItems"]:
        errors.append("%s: 数组长度 %d 大于 maxItems %d"
                      % (path, len(value), schema["maxItems"]))
    if isinstance(value, list) and schema.get("uniqueItems"):
        seen = set()
        for index, item in enumerate(value):
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if marker in seen:
                errors.append("%s[%d]: 数组项必须唯一" % (path, index))
            seen.add(marker)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _mini_validate(item, schema["items"], "%s[%d]" % (path, index), errors)
    return errors


# ------------------------------------------------------------ 对外入口

def validate(instance, name):
    """返回错误列表；空列表 = 通过。SCHEMA_VALIDATE=0 时恒返回 []。"""
    if not _enabled():
        return []
    schema = _load_schema(name)
    if _jsonschema is not None:
        validator = _jsonschema.validators.validator_for(schema)(schema)
        return ["%s: %s" % ("$" + "".join("[%s]" % p if isinstance(p, int) else "." + str(p)
                                          for p in error.absolute_path),
                            error.message)
                for error in validator.iter_errors(instance)]
    return _mini_validate(instance, schema)


def validate_inline(instance, schema):
    """Validate against a caller-supplied small schema using the same fallback."""
    if not _enabled():
        return []
    if not isinstance(schema, dict):
        return ["$: inline schema 必须是对象"]
    if _jsonschema is not None:
        validator = _jsonschema.validators.validator_for(schema)(schema)
        return ["%s: %s" % ("$" + "".join(
            "[%s]" % p if isinstance(p, int) else "." + str(p)
            for p in error.absolute_path), error.message)
                for error in validator.iter_errors(instance)]
    return _mini_validate(instance, schema)


def enforce(instance, name, context=""):
    """校验失败时 raise SchemaContractError（fail-closed）。"""
    errors = validate(instance, name)
    if errors:
        preview = "；".join(errors[:5])
        raise SchemaContractError(
            "SCHEMA_CONTRACT_VIOLATION[%s%s]: %d 处违例：%s%s"
            % (name, (" @%s" % context) if context else "",
               len(errors), preview, " …" if len(errors) > 5 else ""))


if __name__ == "__main__":
    print("== schema_validate 自测 ==")
    enforce({"schema_version": 1, "event_id": "e1", "timestamp": "t",
             "event": "submitted"}, "generation-run")
    print("PASS 合法 ledger 事件通过")
    try:
        enforce({"schema_version": 2, "event": ""}, "generation-run")
        raise AssertionError("应当阻断")
    except SchemaContractError as exc:
        assert "违例" in str(exc)
        print("PASS 非法事件被阻断：%s" % str(exc)[:80])
    enforce({"id": "r1", "url": "https://x", "type": "storyboard_composition",
             "scope": "clip", "tag": "@storyboard"}, "reference-contract")
    try:
        enforce({"id": "r1", "url": "https://x", "type": "bad_type",
                 "scope": "clip", "tag": "@storyboard"}, "reference-contract")
        raise AssertionError("应当阻断")
    except SchemaContractError:
        print("PASS 非法 reference type 被阻断")
    print("== 自测全部通过（jsonschema %s） =="
          % ("可用" if _jsonschema else "未安装，使用内置 mini 校验器"))
