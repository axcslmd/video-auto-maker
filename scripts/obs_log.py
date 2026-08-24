#!/usr/bin/env python3
"""obs_log — 结构化事件日志（JSON Lines），run_id 贯穿全链 trace。

背景：项目此前零结构化日志，排查靠翻 manifest 考古。本模块提供最小侵入的
结构化事件流：每个关键执行点写一条 JSON 行到 run 目录下的 run.log，
run_id 作为 trace_id 贯穿提交/轮询/降级/OCR/成本所有事件。

用法：
    import obs_log
    obs_log.configure(client="acme", run_id="run-01", run_dir="output/acme/run-01")
    obs_log.log_event("batch_start", segments=4, model="seedance-2.0")

未 configure 时事件静默丢弃（安全 no-op，不影响既有 print 输出），
因此可以无条件接线到任何脚本。可用 obs_log.tee_stdout(True) 同步打到控制台。
"""
import json
import os
from datetime import datetime

_CONTEXT = {"client": None, "run_id": None, "run_dir": None}
_TEE = False


def configure(client=None, run_id=None, run_dir=None):
    """设置当前 run 的日志上下文；run_dir 下追加写 run.log。"""
    if client is not None:
        _CONTEXT["client"] = client
    if run_id is not None:
        _CONTEXT["run_id"] = run_id
    if run_dir is not None:
        _CONTEXT["run_dir"] = os.path.abspath(run_dir)


def current():
    return dict(_CONTEXT)


def tee_stdout(enabled=True):
    global _TEE
    _TEE = bool(enabled)


def log_path():
    run_dir = _CONTEXT.get("run_dir")
    return os.path.join(run_dir, "run.log") if run_dir else None


def log_event(event, **fields):
    """写一条结构化事件。未 configure(run_dir) 时为 no-op，绝不抛错。"""
    record = {"ts": datetime.now().isoformat(timespec="milliseconds"),
              "event": str(event),
              "run_id": _CONTEXT.get("run_id"),
              "client": _CONTEXT.get("client")}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, default=str)
    path = log_path()
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # 日志绝不阻断业务流程
    if _TEE:
        print("[obs] %s" % line, flush=True)
    return record


def read_events(run_dir):
    """读取某个 run 目录下 run.log 的全部事件（供 obs_report 聚合）。"""
    path = os.path.join(run_dir, "run.log")
    events = []
    if not os.path.isfile(path):
        return events
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
    return events


if __name__ == "__main__":
    import tempfile
    tmp = tempfile.mkdtemp()
    configure(client="selftest", run_id="r1", run_dir=tmp)
    log_event("batch_start", segments=2)
    log_event("segment_done", segment_id="s1", elapsed_ms=1234)
    events = read_events(tmp)
    assert len(events) == 2 and events[0]["run_id"] == "r1"
    print("PASS obs_log 自测：%d 条事件，run_id 贯穿" % len(events))
