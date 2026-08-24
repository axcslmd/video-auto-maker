#!/usr/bin/env python3
"""Bind repository skills to the executable workflow code.

Skills explain how to reason about a stage. ``workflow_router.py`` remains the
authority for stage order, entry functions, confirmations and recovery. This
module exposes that relationship to every host and fails when skill resources
drift away from files or stages that actually exist.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import workflow_router  # noqa: E402


ALL_STAGES = tuple(workflow_router._STAGE_FLOW)
SKILL_BINDINGS = {
    "video-auto-maker": {
        "kind": "host-neutral-entry",
        "stages": ALL_STAGES,
        "profiles": tuple(workflow_router._CHAIN_PROFILES),
    },
    "story-video-orchestrator": {
        "kind": "workflow-guide",
        "stages": ALL_STAGES,
        "profiles": tuple(workflow_router._CHAIN_PROFILES),
    },
    "basicrouter-multimodal-api": {
        "kind": "executor-reference",
        "stages": ("product_board", "cast_board", "product_usage", "storyboard", "video"),
        "profiles": (),
    },
    "product-consistency": {
        "kind": "stage-guide",
        "stages": ("product_board", "cast_board", "product_usage", "storyboard", "video"),
        "profiles": ("product_showcase", "product_with_human", "brand_tvc"),
    },
    "bw-storyboard-color-anchor": {
        "kind": "stage-guide",
        "stages": ("cast_board", "storyboard", "render_plan", "video"),
        "profiles": ("oral_broadcast", "product_showcase", "product_with_human", "venue_service", "brand_tvc"),
    },
    "seedance-video": {
        "kind": "stage-guide",
        "stages": ("render_plan", "test_segment", "video"),
        "profiles": ("oral_broadcast", "product_showcase", "product_with_human", "venue_service", "brand_tvc"),
    },
    "subtitle-overlay-vision": {
        "kind": "stage-guide",
        "stages": ("captions", "final"),
        "profiles": tuple(workflow_router._CHAIN_PROFILES),
    },
    "horizontal-kinetic-talk-video": {
        "kind": "postproduction-guide",
        "stages": ("captions", "final"),
        "profiles": ("oral_broadcast", "document_explainer"),
    },
    "ugc-review": {
        "kind": "creative-recipe",
        "stages": ("brief", "script", "cast_board", "product_usage", "storyboard", "render_plan", "video"),
        "profiles": ("oral_broadcast", "product_with_human"),
    },
    "ugc-unboxing": {
        "kind": "creative-recipe",
        "stages": ("brief", "script", "product_board", "cast_board", "product_usage", "storyboard", "render_plan", "video"),
        "profiles": ("product_with_human",),
    },
    "video-shotcraft": {
        "kind": "optional-extension",
        "stages": ("shotcraft_packaging",),
        "profiles": ("product_showcase", "brand_tvc"),
    },
}

HOST_ADAPTERS = {
    "codex": ".codex/prompts/video-auto-maker.md",
    "kilo": ".kilo/command/video-auto-maker.md",
    "hermes": "adapters/hermes/START_HERE.md",
    "workbuddy": ".workbuddy/skills/video-auto-maker/SKILL.md",
    "generic": "START_HERE_AGENT.md",
}


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SCRIPT_REF_RE = re.compile(r"(?<![A-Za-z0-9_.-])(scripts/[A-Za-z0-9_./-]+\.py)")
_TEST_REF_RE = re.compile(r"(?<![A-Za-z0-9_.-])(tests/[A-Za-z0-9_./-]+\.py)")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _frontmatter(text):
    match = _FRONTMATTER_RE.search(text)
    if not match:
        return {}
    values = {}
    for raw in match.group(1).splitlines():
        key, separator, value = raw.partition(":")
        if not separator or raw[:1].isspace():
            continue
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _resolve_markdown_target(skill_dir, raw_target):
    target = raw_target.strip().strip("<>").split("#", 1)[0]
    if not target or target.startswith(("http://", "https://", "mailto:")):
        return None
    return (skill_dir / target).resolve()


def contract_payload(skill_name):
    if skill_name not in SKILL_BINDINGS:
        raise KeyError("UNKNOWN_SKILL: %s" % skill_name)
    binding = SKILL_BINDINGS[skill_name]
    stages = {}
    for stage in binding["stages"]:
        flow = workflow_router._STAGE_FLOW[stage]
        stages[stage] = {
            "entry_functions": flow.get("entry_functions") or [],
            "required_inputs": flow.get("required_inputs") or [],
            "expected_outputs": flow.get("expected_outputs") or [],
            "confirm_function": flow.get("confirm_function"),
            "recovery": flow.get("recovery") or [],
            "next": flow.get("next"),
        }
    skill_path = ROOT / "skills" / skill_name / "SKILL.md"
    return {
        "schema": "basicrouter.skill-code-contract.v1",
        "protocol_version": workflow_router.ROUTER_CONTRACT_VERSION,
        "code_source": "scripts/workflow_router.py",
        "skill": skill_name,
        "kind": binding["kind"],
        "profiles": list(binding["profiles"]),
        "skill_sha256": _sha256(skill_path) if skill_path.is_file() else None,
        "router_sha256": _sha256(ROOT / "scripts" / "workflow_router.py"),
        "stages": stages,
    }


def validate_skills():
    errors = []
    warnings = []
    skills_root = ROOT / "skills"
    discovered = {path.parent.name for path in skills_root.glob("*/SKILL.md")}
    for name in sorted(discovered - set(SKILL_BINDINGS)):
        errors.append("unbound skill: %s" % name)
    for name in sorted(set(SKILL_BINDINGS) - discovered):
        errors.append("bound skill missing SKILL.md: %s" % name)

    for name in sorted(discovered & set(SKILL_BINDINGS)):
        skill_dir = skills_root / name
        skill_path = skill_dir / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        metadata = _frontmatter(text)
        if metadata.get("name") != name:
            errors.append("%s frontmatter name must equal directory" % name)
        if not metadata.get("description"):
            errors.append("%s missing one-line description" % name)
        unexpected = sorted(set(metadata) - {"name", "description"})
        if unexpected:
            warnings.append("%s has host-specific frontmatter: %s" %
                            (name, ", ".join(unexpected)))

        for raw_target in _MARKDOWN_LINK_RE.findall(text):
            target = _resolve_markdown_target(skill_dir, raw_target)
            if target is not None and not target.exists():
                errors.append("%s broken markdown link: %s" % (name, raw_target))

        for reference in sorted(set(_SCRIPT_REF_RE.findall(text))):
            local_target = skill_dir / reference
            root_target = ROOT / reference
            if not local_target.is_file() and not root_target.is_file():
                errors.append("%s references missing script: %s" % (name, reference))
        for reference in sorted(set(_TEST_REF_RE.findall(text))):
            if not (ROOT / reference).is_file():
                errors.append("%s references missing test: %s" % (name, reference))

        for stage in SKILL_BINDINGS[name]["stages"]:
            if stage not in workflow_router._STAGE_FLOW:
                errors.append("%s binds unknown stage: %s" % (name, stage))
        for profile in SKILL_BINDINGS[name]["profiles"]:
            if profile not in workflow_router._CHAIN_PROFILES:
                errors.append("%s binds unknown profile: %s" % (name, profile))

    for stage, resources in workflow_router._STAGE_GUIDANCE.items():
        for resource in resources:
            if resource.startswith("skills/"):
                skill_name = Path(resource).parts[1]
                if skill_name not in SKILL_BINDINGS:
                    errors.append("router stage %s references unbound skill %s" %
                                  (stage, skill_name))

    for host, relative in HOST_ADAPTERS.items():
        adapter = ROOT / relative
        if not adapter.is_file():
            errors.append("missing %s host adapter: %s" % (host, relative))
            continue
        adapter_text = adapter.read_text(encoding="utf-8")
        if "AGENT_ENTRY_PROTOCOL.md" not in adapter_text:
            errors.append("%s adapter does not delegate to AGENT_ENTRY_PROTOCOL.md" % host)
        if host == "workbuddy":
            metadata = _frontmatter(adapter_text)
            if metadata.get("name") != "video-auto-maker":
                errors.append("workbuddy adapter frontmatter name mismatch")
            for required in ("skills/video-auto-maker/SKILL.md",
                             "scripts/workflow_router.py",
                             "scripts/agent_entry.py prepare",
                             "PUBLIC_SESSION_ID",
                             "key_setup.py save --stdin"):
                if required not in adapter_text:
                    errors.append("workbuddy adapter missing delegation: %s" % required)

    return {
        "ok": not errors,
        "schema": "basicrouter.skill-validation.v1",
        "protocol_version": workflow_router.ROUTER_CONTRACT_VERSION,
        "skill_count": len(discovered),
        "bound_count": len(SKILL_BINDINGS),
        "host_adapters": dict(HOST_ADAPTERS),
        "errors": errors,
        "warnings": warnings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate skills against executable workflow contracts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("list")
    contract = sub.add_parser("contract")
    contract.add_argument("--skill", required=True, choices=sorted(SKILL_BINDINGS))
    args = parser.parse_args(argv)

    if args.command == "validate":
        result = validate_skills()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    if args.command == "list":
        print(json.dumps({
            "schema": "basicrouter.skill-index.v1",
            "protocol_version": workflow_router.ROUTER_CONTRACT_VERSION,
            "skills": [
                {"name": name, **{key: list(value) if isinstance(value, tuple) else value
                                  for key, value in binding.items()}}
                for name, binding in sorted(SKILL_BINDINGS.items())
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(contract_payload(args.skill), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
