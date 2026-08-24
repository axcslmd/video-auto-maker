#!/usr/bin/env python3
"""Static architecture guardrails for the host-neutral workflow runtime.

This is a dependency/boundary audit, not a style linter. It protects the rules
whose violation creates state corruption or host-specific forks, while listing
large functions and broad exception handlers as ratchetable quality debt.
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import os
from pathlib import Path
import re

import workflow_contract


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HOST_NAMES = re.compile(r"\b(?:kilo|codex|hermes|workbuddy)\b", re.IGNORECASE)
HOST_AWARE_MODULES = frozenset((
    "agent_runtime", "key_setup", "skill_contract", "package_delivery",
    "module_packages",
))
APPROVAL_STATE_OWNERS = frozenset(("run_manifest",))
PRESENTATION_STATE_OWNERS = frozenset(("run_manifest", "review_task"))
STATE_MUTATIONS = {
    "approvals": re.compile(r"\.setdefault\(\s*['\"]approvals['\"]"),
    "approval_hashes": re.compile(
        r"\.setdefault\(\s*['\"]approval_hashes['\"]"),
    "approval_evidence": re.compile(
        r"\.setdefault\(\s*['\"]approval_evidence['\"]"),
    "review_presentations": re.compile(
        r"\.setdefault\(\s*['\"]review_presentations['\"]"),
}
BRANCH_NODES = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp)
MAX_BROAD_EXCEPTIONS = 0
MAX_LARGE_FUNCTIONS = 0
LARGE_FUNCTION_LINE_THRESHOLD = 200
LARGE_FUNCTION_BRANCH_THRESHOLD = 60


def _parse_modules(root):
    modules = {}
    errors = []
    for path in sorted(root.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append("MODULE_PARSE_FAILED:%s:%s" % (path.name, type(exc).__name__))
            continue
        modules[path.stem] = {"path": path, "source": source, "tree": tree}
    return modules, errors


def _top_level_imports(tree, known_modules):
    dependencies = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            dependencies.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            dependencies.add(node.module.split(".")[0])
    return sorted(dependencies & known_modules)


def _strongly_connected(imports):
    indices, lowlinks, stack, on_stack, components = {}, {}, [], set(), []

    def visit(module):
        indices[module] = lowlinks[module] = len(indices)
        stack.append(module)
        on_stack.add(module)
        for dependency in imports.get(module, ()):
            if dependency not in indices:
                visit(dependency)
                lowlinks[module] = min(lowlinks[module], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[dependency])
        if lowlinks[module] == indices[module]:
            component = []
            while True:
                dependency = stack.pop()
                on_stack.remove(dependency)
                component.append(dependency)
                if dependency == module:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for module in imports:
        if module not in indices:
            visit(module)
    return sorted(components, key=lambda item: (-len(item), item))


def _current_stage_literals(tree):
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "current_stage" and
                    isinstance(value, ast.Constant) and isinstance(value.value, str)):
                values.append((value.value, getattr(value, "lineno", 0)))
    return values


def _quality_metrics(module, record):
    source, tree = record["source"], record["tree"]
    broad = []
    functions = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and (
                node.type is None or
                isinstance(node.type, ast.Name) and node.type.id in {
                    "Exception", "BaseException"}):
            broad.append(getattr(node, "lineno", 0))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
            branches = sum(isinstance(item, BRANCH_NODES) for item in ast.walk(node))
            functions.append({
                "module": module, "function": node.name, "line": node.lineno,
                "lines": lines, "branch_points": branches,
            })
    return {
        "lines": len(source.splitlines()),
        "function_count": len(functions),
        "broad_exception_count": len(broad),
        "broad_exception_lines": broad,
        "functions": functions,
    }


def _subscript_key(node):
    """Return a constant string subscript across supported Python AST shapes."""
    if not isinstance(node, ast.Subscript):
        return None
    value = node.slice
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _direct_state_mutations(tree):
    """Find assignments/deletes/method calls that mutate protected state maps."""
    mutations = []
    protected = set(STATE_MUTATIONS)
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
        elif isinstance(node, ast.Delete):
            targets = node.targets
        for target in targets:
            keys = {_subscript_key(item) for item in ast.walk(target)}
            mutations.extend((key, node.lineno) for key in keys & protected)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
                node.func.attr in {"clear", "pop", "popitem", "setdefault", "update"}):
            keys = {_subscript_key(item) for item in ast.walk(node.func.value)}
            mutations.extend((key, node.lineno) for key in keys & protected)
    return mutations


def audit(root=HERE):
    root = Path(root).resolve()
    modules, errors = _parse_modules(root)
    known = set(modules)
    imports = {
        module: _top_level_imports(record["tree"], known)
        for module, record in modules.items()
    }
    cycles = _strongly_connected(imports)
    errors.extend("IMPORT_CYCLE:%s" % "->".join(cycle) for cycle in cycles)

    invalid_stages = []
    state_mutations = []
    host_leaks = []
    module_metrics = {}
    valid_stages = set(workflow_contract.ALL_STAGES)
    for module, record in modules.items():
        source, tree = record["source"], record["tree"]
        for stage, line in _current_stage_literals(tree):
            if stage not in valid_stages:
                invalid_stages.append({"module": module, "line": line, "stage": stage})
        for state_name, pattern in STATE_MUTATIONS.items():
            owners = (PRESENTATION_STATE_OWNERS if state_name == "review_presentations"
                      else APPROVAL_STATE_OWNERS)
            if module not in owners:
                for match in pattern.finditer(source):
                    line = source.count("\n", 0, match.start()) + 1
                    state_mutations.append({
                        "module": module, "line": line, "state": state_name})
        for state_name, line in _direct_state_mutations(tree):
            owners = (PRESENTATION_STATE_OWNERS if state_name == "review_presentations"
                      else APPROVAL_STATE_OWNERS)
            marker = {"module": module, "line": line, "state": state_name}
            if module not in owners and marker not in state_mutations:
                state_mutations.append(marker)
        if module not in HOST_AWARE_MODULES:
            for node in ast.walk(tree):
                if not isinstance(node, ast.If):
                    continue
                condition = " ".join(
                    str(item.value) if isinstance(item, ast.Constant) else
                    item.id if isinstance(item, ast.Name) else
                    item.attr if isinstance(item, ast.Attribute) else ""
                    for item in ast.walk(node.test)
                    if isinstance(item, (ast.Constant, ast.Name, ast.Attribute)))
                if HOST_NAMES.search(condition):
                    host_leaks.append({"module": module, "line": node.lineno})
        module_metrics[module] = _quality_metrics(module, record)

    errors.extend("ILLEGAL_CURRENT_STAGE:%s:%s:%s" % (
        item["module"], item["line"], item["stage"]) for item in invalid_stages)
    errors.extend("APPROVAL_STATE_OWNER_VIOLATION:%s:%s:%s" % (
        item["module"], item["line"], item["state"]) for item in state_mutations)
    errors.extend("HOST_SPECIFIC_BUSINESS_LOGIC:%s:%s" % (
        item["module"], item["line"]) for item in host_leaks)

    functions = [item for metric in module_metrics.values()
                 for item in metric["functions"]]
    hotspots = sorted(
        (item for item in functions
         if (item["lines"] >= LARGE_FUNCTION_LINE_THRESHOLD or
             item["branch_points"] >= LARGE_FUNCTION_BRANCH_THRESHOLD)),
        key=lambda item: (-item["branch_points"], -item["lines"], item["module"]),
    )
    largest_modules = sorted(({
        "module": module, "lines": metric["lines"],
        "function_count": metric["function_count"],
        "broad_exception_count": metric["broad_exception_count"],
    } for module, metric in module_metrics.items()),
        key=lambda item: (-item["lines"], item["module"]))[:20]
    broad_total = sum(metric["broad_exception_count"]
                      for metric in module_metrics.values())
    if broad_total > MAX_BROAD_EXCEPTIONS:
        errors.append("QUALITY_BUDGET_BROAD_EXCEPTIONS:%d>%d" % (
            broad_total, MAX_BROAD_EXCEPTIONS))
    if len(hotspots) > MAX_LARGE_FUNCTIONS:
        errors.append("QUALITY_BUDGET_LARGE_FUNCTIONS:%d>%d" % (
            len(hotspots), MAX_LARGE_FUNCTIONS))
    imported_by = collections.Counter(
        dependency for dependencies in imports.values() for dependency in dependencies)
    warnings = []
    if hotspots:
        warnings.append("QUALITY_DEBT_LARGE_FUNCTIONS:%d" % len(hotspots))
    if broad_total:
        warnings.append("QUALITY_DEBT_BROAD_EXCEPTIONS:%d" % broad_total)
    return {
        "schema_version": 1,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "module_count": len(modules),
            "import_cycle_count": len(cycles),
            "broad_exception_count": broad_total,
            "large_function_count": len(hotspots),
            "broad_exception_budget": MAX_BROAD_EXCEPTIONS,
            "large_function_budget": MAX_LARGE_FUNCTIONS,
            "large_function_line_threshold": LARGE_FUNCTION_LINE_THRESHOLD,
            "large_function_branch_threshold": LARGE_FUNCTION_BRANCH_THRESHOLD,
            "approval_state_owner_violations": len(state_mutations),
            "host_specific_business_logic": len(host_leaks),
        },
        "boundaries": {
            "approval_state_owner": "run_manifest",
            "presentation_state_owners": sorted(PRESENTATION_STATE_OWNERS),
            "host_identity_owner": "agent_runtime",
            "workflow_stage_owner": "workflow_contract",
        },
        "import_cycles": cycles,
        "most_imported": [{"module": module, "imported_by": count}
                          for module, count in imported_by.most_common(15)],
        "largest_modules": largest_modules,
        "hotspots": hotspots[:25],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "report"))
    args = parser.parse_args(argv)
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] or args.command == "report" else 2


if __name__ == "__main__":
    raise SystemExit(main())
