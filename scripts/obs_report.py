#!/usr/bin/env python3
"""obs_report — 运行可观测性报表：聚合 generation ledger + run.log 事件。

把 append-only 账本里的原始事件聚合成工程指标：
- 每模型成功率 / 首次通过率（first-pass rate）/ 平均重试次数
- 单段生成时长统计（若事件带 elapsed_ms）
- 成本汇总（复用 cost_ledger.estimate_run_cost）+ 成本异常检测
  （单段成本 > 历史 P95 的 2 倍时打标，提示降级失效等异常）
- OCR 拦截次数（ocr_warning / task_rejected 事件）

CLI:
  python3 scripts/obs_report.py --ledger output/<client>/<run>/generation_ledger.jsonl
  python3 scripts/obs_report.py --run-dir output/<client>/<run>   # 自动找 ledger + run.log
  python3 scripts/obs_report.py --ledger X --json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cost_ledger  # noqa: E402
import generation_ledger  # noqa: E402
import obs_log  # noqa: E402


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def aggregate(events):
    """把 ledger 事件聚合成模型/质量指标。"""
    per_model = {}
    first_pass_total = 0
    first_pass_ok = 0
    unit_attempts = {}
    ocr_blocks = 0
    elapsed = []
    for event in events:
        name = event.get("event", "")
        model = event.get("model") or "unknown"
        bucket = per_model.setdefault(model, {"submitted": 0, "succeeded": 0, "failed": 0})
        if name == "task_submitted":
            bucket["submitted"] += 1
        elif name == "task_succeeded":
            bucket["succeeded"] += 1
            unit = event.get("unit_id") or "?"
            attempt = event.get("attempt") or 1
            unit_attempts[unit] = max(unit_attempts.get(unit, 0), attempt)
            first_pass_total += 1
            if attempt == 1:
                first_pass_ok += 1
            if isinstance(event.get("elapsed_ms"), (int, float)):
                elapsed.append(event["elapsed_ms"])
        elif name in ("task_failed", "task_rejected"):
            bucket["failed"] += 1
            if "ocr" in str(event.get("reason", "")).lower():
                ocr_blocks += 1
        elif name == "ocr_warning":
            ocr_blocks += 1
    for bucket in per_model.values():
        done = bucket["succeeded"] + bucket["failed"]
        bucket["success_rate"] = round(bucket["succeeded"] / done, 4) if done else None
    retries = [max(0, n - 1) for n in unit_attempts.values()]
    return {
        "per_model": per_model,
        "first_pass_rate": round(first_pass_ok / first_pass_total, 4) if first_pass_total else None,
        "succeeded_tasks": first_pass_total,
        "avg_retries_per_unit": round(sum(retries) / len(retries), 3) if retries else 0.0,
        "ocr_blocks": ocr_blocks,
        "elapsed_ms": {"p50": _percentile(elapsed, 50), "p95": _percentile(elapsed, 95)},
    }


def cost_anomalies(cost_summary, multiplier=2.0):
    """单段成本超过全部段 P95 的 multiplier 倍时打标。"""
    segments = cost_summary.get("by_segment") or {}
    values = list(segments.values())
    p95 = _percentile(values, 95)
    if p95 is None or p95 <= 0:
        return []
    threshold = p95 * multiplier
    return [{"unit_id": unit, "cost": cost, "threshold": round(threshold, 4)}
            for unit, cost in segments.items() if cost > threshold]


def build_report(ledger_path=None, run_dir=None):
    events = []
    if ledger_path and os.path.isfile(ledger_path):
        events = generation_ledger.read_events(ledger_path)
    report = {"ledger": ledger_path, "run_dir": run_dir,
              "metrics": aggregate(events),
              "cost": None, "cost_anomalies": [], "run_log_events": 0}
    if events:
        report["cost"] = cost_ledger.estimate_run_cost(events)
        report["cost_anomalies"] = cost_anomalies(report["cost"])
    if run_dir:
        report["run_log_events"] = len(obs_log.read_events(run_dir))
    return report


def _format(report):
    lines = ["== obs_report =="]
    metrics = report["metrics"]
    lines.append("模型成功率：")
    for model, bucket in sorted((metrics["per_model"] or {}).items()):
        rate = bucket["success_rate"]
        lines.append("  %-32s 提交 %d / 成功 %d / 失败 %d / 成功率 %s"
                     % (model, bucket["submitted"], bucket["succeeded"],
                        bucket["failed"], "%.1f%%" % (rate * 100) if rate is not None else "n/a"))
    fpr = metrics["first_pass_rate"]
    lines.append("首次通过率：%s（成功任务 %d，平均每单元重试 %.2f 次）"
                 % ("%.1f%%" % (fpr * 100) if fpr is not None else "n/a",
                    metrics["succeeded_tasks"], metrics["avg_retries_per_unit"]))
    lines.append("生成耗时：P50=%s ms / P95=%s ms"
                 % (metrics["elapsed_ms"]["p50"], metrics["elapsed_ms"]["p95"]))
    lines.append("OCR 拦截次数：%d" % metrics["ocr_blocks"])
    cost = report.get("cost")
    if cost:
        lines.append("成本：总计 $%.4f（重试浪费 $%.4f，占比 %.1f%%，价格表 %s）"
                     % (cost["total_est"], cost["retry_waste"],
                        cost["retry_waste_ratio"] * 100, cost["price_table_version"]))
    for anomaly in report.get("cost_anomalies") or []:
        lines.append("⚠️ 成本异常：段 %s 成本 $%.4f 超过阈值 $%.4f（P95×2），"
                     "请检查模型降级是否失效" % (anomaly["unit_id"], anomaly["cost"], anomaly["threshold"]))
    if report.get("run_dir"):
        lines.append("run.log 结构化事件：%d 条" % report["run_log_events"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行可观测性报表")
    parser.add_argument("--ledger", default=None, help="generation_ledger.jsonl 路径")
    parser.add_argument("--run-dir", default=None, help="run 输出目录（自动找 ledger/run.log）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    ledger = args.ledger
    if args.run_dir and not ledger:
        candidate = os.path.join(args.run_dir, "generation_ledger.jsonl")
        if os.path.isfile(candidate):
            ledger = candidate
    if not ledger and not args.run_dir:
        parser.error("需要 --ledger 或 --run-dir")
    report = build_report(ledger_path=ledger, run_dir=args.run_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
