#!/usr/bin/env python3
"""Resolve executable design tokens without coupling business code to an Agent.

The resolver accepts inline contracts or stable JSON paths and keeps legacy
plans readable. Design Token v3 is schema-enforced before it reaches a renderer.
"""
import copy
import json
import os

import artifact_contract
import schema_validate


INLINE_KEYS = ("designTokens", "design_tokens", "style_tokens")
PATH_KEYS = ("design_tokens_path", "style_tokens_path", "styleframe_tokens_path")
DEFAULT_FILENAMES = ("design_tokens.json", "style_tokens.json")


def _load(path):
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("DESIGN_TOKEN_CONTRACT_NOT_OBJECT: %s" % path)
    return value


def _containers(plan):
    yield plan
    for key in ("fusion", "render_profile", "styleframe", "quality_contract"):
        value = plan.get(key) if isinstance(plan, dict) else None
        if isinstance(value, dict):
            yield value


def resolve(plan, *, source_plan=None, required=False):
    """Return a validated token dict or ``None`` for an unstyled legacy plan."""
    plan = plan if isinstance(plan, dict) else {}
    for container in _containers(plan):
        for key in INLINE_KEYS:
            value = container.get(key)
            if isinstance(value, dict) and value:
                return validate(copy.deepcopy(value))

    path_candidates = []
    for container in _containers(plan):
        path_candidates.extend(container.get(key) for key in PATH_KEYS if container.get(key))
    if source_plan:
        base = os.path.dirname(os.path.abspath(source_plan))
        path_candidates.extend(os.path.join(base, name) for name in DEFAULT_FILENAMES)
    for candidate in path_candidates:
        path = os.path.abspath(os.path.expanduser(str(candidate)))
        if os.path.isfile(path):
            return validate(_load(path))
    if required:
        raise ValueError("DESIGN_TOKEN_CONTRACT_REQUIRED")
    return None


def validate(tokens):
    if not isinstance(tokens, dict):
        raise ValueError("DESIGN_TOKEN_CONTRACT_NOT_OBJECT")
    if int(tokens.get("schema_version") or 0) == 3:
        schema_validate.enforce(
            tokens, "design-tokens", context="design_token_contract.validate")
        expected = tokens.get("contract_sha256")
        payload = dict(tokens)
        payload.pop("contract_sha256", None)
        actual = artifact_contract.sha256_json(payload)
        if expected != actual:
            raise ValueError("DESIGN_TOKEN_CONTRACT_STALE: expected %s actual %s" % (
                expected, actual))
    return tokens


def primary(tokens, fallback=None):
    tokens = tokens if isinstance(tokens, dict) else {}
    color = tokens.get("color") if isinstance(tokens.get("color"), dict) else {}
    brand = tokens.get("brand") if isinstance(tokens.get("brand"), dict) else {}
    return color.get("primary") or brand.get("primary") or fallback
