"""policy_check — 严格执行规则的机器可执行闸门。

策略来源：REAL_TEST_INCIDENTS.md 末尾「严格执行规则」一节。
策略文件：policies/gates.json（环境无 pyyaml，使用 JSON 替代 yaml；
若运行时存在 yaml 模块且 policies/gates.yaml 存在，则优先加载 yaml）。

用法：
    from policy_check import check, enforce, PolicyBlock
    violations = check("submit_video", context)   # 空列表 = 通过
    enforce("submit_video", context)              # 有违例时 raise PolicyBlock

context 约定：正式触发点必须提供足够证据。未提供的必需证据标记为
``indeterminate`` 并默认阻断；只有通过 ``required_asset_keys`` 明确声明不
适用的资产闸门才会跳过。
    assets: {asset_key: {"confirmed": bool, "fingerprint": str,
                         "expected_fingerprint": str?}}
    asset:  单资产简写 {"type": asset_key, ...同上}
    fingerprints: {name: {"current": str, "expected": str}}
    required_reference_types / actual_reference_types / dropped_references
    model, model_catalog, reference_count, duration, video_type, candidates
    ocr_findings: [str]
    estimated_cost / budget_cap
"""

import json
import os

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_YAML = os.path.join(_HERE, "..", "policies", "gates.yaml")
_DEFAULT_JSON = os.path.join(_HERE, "..", "policies", "gates.json")


class PolicyViolation:
    def __init__(self, gate_id, name, message, detail=None, kind="violation"):
        self.gate_id = gate_id
        self.name = name
        self.message = message
        self.detail = detail
        self.kind = kind

    def __str__(self):
        base = "[%s] %s：%s" % (self.gate_id, self.name, self.message)
        return base + ("（%s）" % self.detail if self.detail else "")

    def to_dict(self):
        return {"gate_id": self.gate_id, "name": self.name,
                "message": self.message, "detail": self.detail,
                "kind": self.kind}


class _Indeterminate:
    def __init__(self, detail):
        self.detail = str(detail)


def _indeterminate(detail):
    return _Indeterminate(detail)


class PolicyBlock(Exception):
    """有闸门违例时由 enforce() 抛出，message 为中文违例汇总。"""

    def __init__(self, trigger, violations):
        self.trigger = trigger
        self.violations = list(violations)
        summary = "；".join(str(v) for v in self.violations)
        super().__init__("策略阻断（%s，%d 条违例）：%s"
                         % (trigger, len(self.violations), summary))


_POLICY_CACHE = {}


def load_policies(path=None):
    """加载策略文件。yaml 可用且存在时优先，否则回退 JSON。"""
    if path is None:
        if yaml is not None and os.path.isfile(_DEFAULT_YAML):
            path = _DEFAULT_YAML
        else:
            path = _DEFAULT_JSON
    path = os.path.abspath(path)
    if path in _POLICY_CACHE:
        return _POLICY_CACHE[path]
    with open(path, "r", encoding="utf-8") as fh:
        if path.endswith((".yaml", ".yml")):
            if yaml is None:
                raise RuntimeError("加载 %s 需要 pyyaml" % path)
            doc = yaml.safe_load(fh)
        else:
            doc = json.load(fh)
    gates = doc.get("gates") or []
    _POLICY_CACHE[path] = gates
    return gates


# ---------------------------------------------------------------- 检查实现

def _find_asset(context, asset_key):
    assets = context.get("assets")
    if isinstance(assets, dict) and asset_key in assets:
        return assets[asset_key]
    single = context.get("asset")
    if isinstance(single, dict) and single.get("type") == asset_key:
        return single
    return None


def _check_asset_confirmed(gate, context):
    key = gate.get("params", {}).get("asset_key")
    required = context.get("required_asset_keys")
    if required is not None and key not in set(required):
        return None
    entry = _find_asset(context, key)
    if entry is None:
        return _indeterminate("未提供 %s 的确认与指纹证据" % key)
    if not isinstance(entry, dict):
        return "%s 资产记录格式无效" % key
    problems = []
    if entry.get("confirmed") is not True:
        problems.append("未确认")
    if gate.get("params", {}).get("require_fingerprint"):
        if not entry.get("fingerprint"):
            problems.append("缺少指纹")
    expected = entry.get("expected_fingerprint")
    if expected and entry.get("fingerprint") and entry["fingerprint"] != expected:
        problems.append("指纹与当前确认版本不一致")
    return "、".join(problems) if problems else None


def _check_fingerprint_current(gate, context):
    fps = context.get("fingerprints")
    if not isinstance(fps, dict) or not fps:
        return _indeterminate("未提供当前计划与已确认版本的指纹对照")
    stale = []
    for name, pair in fps.items():
        if not isinstance(pair, dict):
            continue
        current, expected = pair.get("current"), pair.get("expected")
        if current and expected and current != expected:
            stale.append(name)
    return "指纹不一致项：%s" % ", ".join(sorted(stale)) if stale else None


def _check_required_refs_complete(gate, context):
    required = context.get("required_reference_types")
    if required is None:
        return _indeterminate("未声明该提交包的必需参考类型")
    if context.get("actual_reference_types") is None:
        return _indeterminate("未提供实际交给模型的参考类型")
    actual = set(context.get("actual_reference_types") or [])
    missing = sorted(set(required) - actual)
    dropped = []
    if gate.get("params", {}).get("fail_on_dropped"):
        for item in context.get("dropped_references") or []:
            rtype = item.get("type") if isinstance(item, dict) else item
            if rtype in set(required):
                dropped.append(rtype)
    problems = []
    if missing:
        problems.append("缺失参考类型 [%s]" % ", ".join(missing))
    if dropped:
        problems.append("被丢弃的必需参考 [%s]" % ", ".join(sorted(set(dropped))))
    return "；".join(problems) if problems else None


def _capability_failures(record, context, capability_keys):
    failures = []
    if "image_count" in capability_keys:
        need = context.get("reference_count")
        cap = record.get("image_count")
        if need is not None and cap is not None and need > cap:
            failures.append("参考图 %d 张超过模型上限 %d 张" % (need, cap))
    duration = context.get("duration")
    if duration is not None:
        if "duration_min" in capability_keys:
            dmin = record.get("duration_min")
            if dmin is not None and duration < dmin:
                failures.append("时长 %ss 低于模型最短 %ss" % (duration, dmin))
        if "duration_max" in capability_keys:
            dmax = record.get("duration_max")
            if dmax is not None and duration > dmax:
                failures.append("时长 %ss 高于模型最长 %ss" % (duration, dmax))
    if "video_types" in capability_keys:
        vtype = context.get("video_type")
        supported = record.get("video_types")
        if vtype is not None and supported and vtype not in supported:
            failures.append("videoType=%s 不在模型支持列表 %s" % (vtype, supported))
    return failures


def _check_model_capability_match(gate, context):
    if context.get("capability_preflight_passed") is True:
        return None
    model = context.get("model")
    catalog = context.get("model_catalog")
    if not model or not isinstance(catalog, dict):
        return _indeterminate("缺少目标模型或实时能力目录，无法证明提交包可承接")
    keys = gate.get("params", {}).get("capability_keys") or []
    record = catalog.get(model)
    failures = ("模型 %s 不在实时目录中" % model) if record is None \
        else _capability_failures(record, context, keys)
    if not failures:
        return None
    if gate.get("params", {}).get("allow_full_match_fallback"):
        for candidate in context.get("candidates") or []:
            crecord = catalog.get(candidate)
            if crecord and not _capability_failures(crecord, context, keys):
                return None  # 存在完整能力匹配的候选，允许降级
    if isinstance(failures, str):
        return failures + "，且无完整匹配候选"
    return "；".join(failures) + "；且无完整匹配候选模型"


def _check_ocr_clear(gate, context):
    findings = context.get("ocr_findings")
    if findings is None:
        ocr = context.get("ocr")
        findings = ocr.get("texts") if isinstance(ocr, dict) else None
    if findings is None:
        return _indeterminate("未提供当前成片的 OCR 检查结果")
    if findings:
        preview = "、".join(str(t) for t in list(findings)[:5])
        return "检出画面文字 %d 处，如：%s" % (len(findings), preview)
    return None


def _check_budget_within(gate, context):
    cost = context.get("estimated_cost")
    cap = context.get("budget_cap")
    if cap is None:
        return None
    if cost is None:
        return _indeterminate("已设定预算上限，但未提供本次生成的费用估算")
    if cost > cap:
        return "预计费用 %s 超出预算上限 %s" % (cost, cap)
    return None


_CHECKERS = {
    "asset_confirmed": _check_asset_confirmed,
    "fingerprint_current": _check_fingerprint_current,
    "required_refs_complete": _check_required_refs_complete,
    "model_capability_match": _check_model_capability_match,
    "ocr_clear": _check_ocr_clear,
    "budget_within": _check_budget_within,
}


# ---------------------------------------------------------------- 入口

def _evaluate_objects(trigger, context, policies_path=None):
    context = context or {}
    violations = []
    indeterminate = []
    for gate in load_policies(policies_path):
        if gate.get("trigger") != trigger:
            continue
        checker = _CHECKERS.get(gate.get("check"))
        if checker is None:
            continue
        result = checker(gate, context)
        if isinstance(result, _Indeterminate):
            indeterminate.append(PolicyViolation(
                gate.get("id"), gate.get("name"),
                gate.get("block_message") or gate.get("name"), result.detail,
                kind="indeterminate"))
        elif result:
            violations.append(PolicyViolation(
                gate.get("id"), gate.get("name"),
                gate.get("block_message") or gate.get("name"), result))
    return violations, indeterminate


def evaluate(trigger, context, policies_path=None):
    """返回可 JSON 序列化的三态结果：allow / deny / indeterminate。"""
    violations, indeterminate = _evaluate_objects(
        trigger, context, policies_path)
    return {
        "decision": ("deny" if violations else
                     "indeterminate" if indeterminate else "allow"),
        "violations": [item.to_dict() for item in violations],
        "indeterminate": [item.to_dict() for item in indeterminate],
    }


def check(trigger, context, policies_path=None, *, fail_on_indeterminate=True):
    """返回阻断项；默认将证据不足也视为阻断。"""
    violations, indeterminate = _evaluate_objects(
        trigger, context, policies_path)
    blockers = list(violations)
    if fail_on_indeterminate:
        blockers.extend(indeterminate)
    return blockers


def enforce(trigger, context, policies_path=None, *, fail_on_indeterminate=True):
    """有违例时 raise PolicyBlock（中文消息汇总），否则静默通过。"""
    violations = check(
        trigger, context, policies_path,
        fail_on_indeterminate=fail_on_indeterminate)
    if violations:
        raise PolicyBlock(trigger, violations)


if __name__ == "__main__":
    print("== policy_check 自测 ==")

    ok_ctx = {
        "assets": {
            "product_board": {"confirmed": True, "fingerprint": "fp-pb"},
            "digital_human_board": {"confirmed": True, "fingerprint": "fp-dh"},
            "product_usage_board": {"confirmed": True, "fingerprint": "fp-pu"},
            "storyboard": {"confirmed": True, "fingerprint": "fp-sb"},
        },
        "required_reference_types": ["storyboard"],
        "actual_reference_types": ["storyboard"],
        "model": "seedance-2.0",
        "model_catalog": {"seedance-2.0": {"image_count": 1,
                                           "duration_min": 4,
                                           "duration_max": 15,
                                           "video_types": [1, 5]}},
        "reference_count": 1, "duration": 15, "video_type": 5,
        "estimated_cost": 3.2, "budget_cap": 10.0,
    }
    v = check("submit_video", ok_ctx)
    assert v == [], v
    print("PASS 全合规 context 无违例")

    bad_ctx = dict(ok_ctx)
    bad_ctx["assets"] = dict(ok_ctx["assets"],
                             storyboard={"confirmed": False, "fingerprint": "fp-sb"})
    bad_ctx["actual_reference_types"] = []
    bad_ctx["dropped_references"] = [{"type": "storyboard"}]
    bad_ctx["reference_count"] = 4
    bad_ctx["estimated_cost"] = 99.0
    v = check("submit_video", bad_ctx)
    ids = {x.gate_id for x in v}
    assert {"GATE-005", "GATE-007", "GATE-008", "GATE-010"} <= ids, ids
    print("PASS 违例检出：%s" % sorted(ids))

    try:
        enforce("submit_video", bad_ctx)
        raise AssertionError("enforce 未阻断")
    except PolicyBlock as exc:
        assert "策略阻断" in str(exc)
        print("PASS enforce 抛出 PolicyBlock，消息为中文汇总")

    v = check("deliver", {"ocr_findings": ["399元", "IPX5"]})
    assert [x.gate_id for x in v] == ["GATE-009"], v
    v = check("deliver", {})
    assert [x.gate_id for x in v] == ["GATE-009"], v
    assert v[0].kind == "indeterminate"
    print("PASS OCR 闸门：有文字阻断 / 缺证据时不确定阻断")

    v = check("load_plan", {"fingerprints": {
        "plan": {"current": "old", "expected": "new"}}})
    assert [x.gate_id for x in v] == ["GATE-006"], v
    print("PASS 指纹闸门：旧计划被阻断")

    print("== 自测全部通过 ==")
