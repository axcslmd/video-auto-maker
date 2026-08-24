"""canary_replay — 金丝雀回放（dry-run 全链路契约校验，绝不调用真实 API）。

加载冻结的金丝雀定义（canary/aeroclip_s1/canary.json；目录不存在时使用
代码内置默认定义：4 段、9:16、15+15+15+4 秒、seedance-2.0），依次走：
    1. plan_load          计划加载与字段校验
    2. segment            分段展开（时长/比例/分辨率/模型继承）
    3. reference_contract 参考图契约检查（required ⊆ actual，无丢弃）
    4. policy_gate        严格执行规则闸门（scripts/policy_check.py，可选）
    5. request_build      请求体构造（仅构造不发送）+ 产物契约字段断言

断言每段产物契约字段齐全，输出 CANARY PASS/FAIL + 各阶段耗时。
本模块不 import br_client，也不发起任何网络请求。

用法：
    python3 scripts/canary_replay.py [--dry-run] [--canary PATH]
                                     [--output-dir output/aeroclip-live-20260731]
"""

import argparse
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DEFAULT_CANARY = os.path.join(_ROOT, "canary", "aeroclip_s1", "canary.json")
_DEFAULT_OUTPUT_DIR = os.path.join(_ROOT, "output", "aeroclip-live-20260731")

# 冻结的默认金丝雀定义（与 REAL_TEST_INCIDENTS.md「当前成片」一致：
# 4 段、9:16、15+15+15+4 秒、seedance-2.0 / dreamina-seedance-2-0-260128）。
BUILTIN_CANARY = {
    "name": "aeroclip_s1",
    "frozen_at": "2026-07-31",
    "model": "seedance-2.0",
    "model_id": "dreamina-seedance-2-0-260128",
    "ratio": "9:16",
    "resolution": "1080p",
    "video_type": 5,
    "model_catalog": {
        "seedance-2.0": {"image_count": 1, "duration_min": 4,
                         "duration_max": 15, "video_types": [1, 5]}
    },
    "assets": {
        "product_board": {"confirmed": True, "fingerprint": "fp-product-board"},
        "digital_human_board": {"confirmed": True, "fingerprint": "fp-human-board"},
        "product_usage_board": {"confirmed": True, "fingerprint": "fp-usage-board"},
        "storyboard": {"confirmed": True, "fingerprint": "fp-storyboard"},
    },
    "segments": [
        {"id": "seg01", "duration": 15,
         "dialogue": "通勤路上，音乐也要跟上节奏。",
         "visual": "数字人佩戴 AeroClip S1 走在城市街道"},
        {"id": "seg02", "duration": 15,
         "dialogue": "不入耳的设计，戴一整天也舒服。",
         "visual": "特写耳夹式佩戴，办公室场景"},
        {"id": "seg03", "duration": 15,
         "dialogue": "跑步健身，稳固不掉。",
         "visual": "户外运动场景，产品细节展示"},
        {"id": "seg04", "duration": 4,
         "dialogue": "AeroClip S1。",
         "visual": "产品定格，品牌收尾"},
    ],
    "required_reference_types": ["storyboard"],
}

# 每段请求体必须携带的契约字段
SEGMENT_CONTRACT_FIELDS = (
    "id", "duration", "ratio", "resolution", "model", "video_type",
    "text", "urls", "required_reference_types", "actual_reference_types",
    "video_handoff_fingerprint",
)

NO_TEXT_CLAUSE = ("Follow the confirmed storyboard; do not add any text, "
                  "subtitles, logos, price or slogan to the visuals.")


class CanaryFailure(Exception):
    pass


def _fingerprint(payload):
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_canary(path=None):
    """加载冻结金丝雀定义；文件不存在时使用内置默认定义。"""
    path = path or _DEFAULT_CANARY
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), path
    return json.loads(json.dumps(BUILTIN_CANARY)), "builtin-default"


def stage_plan_load(canary):
    for field in ("name", "model", "ratio", "resolution", "segments"):
        if field not in canary:
            raise CanaryFailure("计划缺少字段：%s" % field)
    segments = canary["segments"]
    if len(segments) != 4:
        raise CanaryFailure("金丝雀应为 4 段，实际 %d 段" % len(segments))
    durations = [seg.get("duration") for seg in segments]
    if durations != [15, 15, 15, 4]:
        raise CanaryFailure("分段时长应为 15+15+15+4，实际 %s" % durations)
    if canary["ratio"] != "9:16":
        raise CanaryFailure("比例应为 9:16，实际 %s" % canary["ratio"])
    catalog = canary.get("model_catalog", {})
    record = catalog.get(canary["model"], {})
    dmin = record.get("duration_min")
    if dmin is not None:
        for seg in segments:
            if seg["duration"] < dmin:
                raise CanaryFailure(
                    "段 %s 时长 %ss 低于模型最短 %ss（INC-009 回归）"
                    % (seg.get("id"), seg["duration"], dmin))
    return {"segments": len(segments), "total_duration": sum(durations)}


def stage_segment(canary):
    """展开分段，继承计划级模型/比例/分辨率，构造参考图列表。"""
    expanded = []
    for seg in canary["segments"]:
        required = list(canary.get("required_reference_types") or [])
        references = [{"type": rtype,
                       "fingerprint": canary.get("assets", {})
                       .get({"storyboard": "storyboard"}.get(rtype, rtype), {})
                       .get("fingerprint", "fp-%s" % rtype)}
                      for rtype in required]
        item = {
            "id": seg["id"],
            "duration": seg["duration"],
            "ratio": canary["ratio"],
            "resolution": canary["resolution"],
            "model": canary["model"],
            "video_type": canary.get("video_type", 5),
            "dialogue": seg.get("dialogue", ""),
            "visual": seg.get("visual", ""),
            "required_reference_types": required,
            "actual_reference_types": [r["type"] for r in references],
            "references": references,
            "dropped_references": [],
            "urls": ["frozen://%s/%s" % (canary["name"], r["type"])
                     for r in references],
        }
        expanded.append(item)
    return expanded


def stage_reference_contract(segments, canary):
    """必需参考类型必须全部存在且无丢弃（INC-011/012 回归）。"""
    catalog = canary.get("model_catalog", {})
    for seg in segments:
        required = set(seg["required_reference_types"])
        actual = set(seg["actual_reference_types"])
        missing = sorted(required - actual)
        dropped = [d.get("type") for d in seg["dropped_references"]
                   if d.get("type") in required]
        if missing or dropped:
            raise CanaryFailure(
                "段 %s 参考图契约不完整：缺失 %s，丢弃 %s"
                % (seg["id"], missing, dropped))
        cap = (catalog.get(seg["model"]) or {}).get("image_count")
        if cap is not None and len(seg["urls"]) > cap:
            raise CanaryFailure(
                "段 %s 参考图 %d 张超过模型 %s 上限 %d 张（INC-008 回归）"
                % (seg["id"], len(seg["urls"]), seg["model"], cap))
    return {"segments_checked": len(segments)}


def stage_policy_gate(segments, canary):
    """接 scripts/policy_check.py 的严格执行规则闸门（可选，缺失时跳过）。"""
    try:
        sys.path.insert(0, _HERE)
        import policy_check
    except ImportError:
        return {"skipped": "policy_check 不可用"}
    per_segment = []
    for seg in segments:
        context = {
            "assets": canary.get("assets"),
            "required_reference_types": seg["required_reference_types"],
            "actual_reference_types": seg["actual_reference_types"],
            "dropped_references": seg["dropped_references"],
            "model": seg["model"],
            "model_catalog": canary.get("model_catalog"),
            "reference_count": len(seg["urls"]),
            "duration": seg["duration"],
            "video_type": seg["video_type"],
        }
        violations = policy_check.check("submit_video", context)
        if violations:
            raise CanaryFailure("段 %s 策略阻断：%s"
                                % (seg["id"], "；".join(map(str, violations))))
        per_segment.append(seg["id"])
    return {"enforced": per_segment}


def stage_request_build(segments, canary):
    """构造请求体（仅构造不发送），并断言产物契约字段齐全。"""
    requests = []
    for seg in segments:
        handoff_fp = _fingerprint({
            "segment": seg["id"], "model": seg["model"],
            "duration": seg["duration"], "ratio": seg["ratio"],
            "refs": seg["actual_reference_types"],
            "ref_fingerprints": [r["fingerprint"] for r in seg["references"]],
        })
        text = "\n".join(filter(None, [
            NO_TEXT_CLAUSE,
            "Dialogue: %s" % seg["dialogue"],
            "Visual: %s" % seg["visual"],
        ]))
        request = dict(seg)
        request.update({
            "text": text,
            "negative_prompt": "text, subtitles, watermark, logo, price",
            "video_handoff_fingerprint": handoff_fp,
            "endpoint": "/ai/createVideo",  # 仅记录，绝不发送
            "dry_run": True,
        })
        missing = [f for f in SEGMENT_CONTRACT_FIELDS if f not in request]
        if missing:
            raise CanaryFailure("段 %s 请求体缺契约字段：%s"
                                % (seg["id"], missing))
        requests.append(request)
    return requests


def scan_output_dir(output_dir):
    """若历史运行目录存在，读取其产物清单作为旁证（只读）。"""
    if not os.path.isdir(output_dir):
        return {"present": False}
    entries = sorted(os.listdir(output_dir))
    return {"present": True, "files": len(entries),
            "batch_results": [f for f in entries if f.startswith("batch_results")],
            "qc_reports": [f for f in entries if f.endswith(".qc.json")]}


def run(canary_path=None, output_dir=_DEFAULT_OUTPUT_DIR, verbose=True):
    stages = []
    def log(msg):
        if verbose:
            print(msg, flush=True)

    def timed(name, fn, *args):
        start = time.perf_counter()
        result = fn(*args)
        elapsed_ms = (time.perf_counter() - start) * 1000
        stages.append((name, elapsed_ms))
        log("  [ok] %-20s %8.2f ms" % (name, elapsed_ms))
        return result

    try:
        canary, source = load_canary(canary_path)
        log("金丝雀定义来源：%s（%s，冻结于 %s）"
            % (source, canary.get("name"), canary.get("frozen_at", "n/a")))
        timed("plan_load", stage_plan_load, canary)
        segments = timed("segment", stage_segment, canary)
        timed("reference_contract", stage_reference_contract, segments, canary)
        timed("policy_gate", stage_policy_gate, segments, canary)
        requests = timed("request_build", stage_request_build, segments, canary)
        evidence = scan_output_dir(output_dir)
        if evidence.get("present"):
            log("  [info] 历史运行目录旁证：%d 个文件，batch=%s，qc=%d 份"
                % (evidence["files"], evidence["batch_results"],
                   len(evidence["qc_reports"])))
        total = sum(ms for _, ms in stages)
        log("CANARY PASS — %d 段契约齐全，请求体仅构造未发送，总耗时 %.2f ms"
            % (len(requests), total))
        return 0
    except CanaryFailure as exc:
        total = sum(ms for _, ms in stages)
        log("CANARY FAIL — %s（已完成阶段耗时 %.2f ms）" % (exc, total))
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="金丝雀回放（dry-run，不调真实 API）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式声明 dry-run；无论是否给出，本工具都不会发起网络请求")
    parser.add_argument("--canary", default=None,
                        help="金丝雀定义路径（默认 canary/aeroclip_s1/canary.json，缺失用内置）")
    parser.add_argument("--output-dir", default=_DEFAULT_OUTPUT_DIR,
                        help="历史运行目录（只读旁证）")
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("[info] 未指定 --dry-run：本工具永不调用真实 API，仍按 dry-run 执行")
    return run(canary_path=args.canary, output_dir=args.output_dir)


if __name__ == "__main__":
    sys.exit(main())
