#!/usr/bin/env python3
"""CLI adapter for the paid video orchestration service."""
import argparse
import json
import os


def build_parser(service):
    parser = argparse.ArgumentParser(description="BasicRouter video engine")
    parser.add_argument("--batch")
    parser.add_argument("--text", help="已停用；请改用一元素 --batch")
    parser.add_argument("--model", default=service.DEFAULT_MODEL)
    parser.add_argument("--ratio")
    parser.add_argument("--batch-ratio")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--negative")
    parser.add_argument("--candidates", type=int)
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--max-wait", type=int, default=3600)
    parser.add_argument("--allow-ocr-warning", action="store_true")
    parser.add_argument("--formal-stage", choices=("video", "test_segment"), default="video")
    parser.add_argument("--test-segment-id")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--chain", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--client")
    parser.add_argument("--results-out")
    parser.add_argument("--locked-refs", nargs="*")
    parser.add_argument("--manifest")
    parser.add_argument("--ledger")
    parser.add_argument("--continuity-plan")
    parser.add_argument("--prompt-review")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--preflight-out",
                        help="把实时模型/引用/隐私回退能力合同原子写入 JSON")
    parser.add_argument("--budget-cap", type=float)
    return parser


def _error(message, *, as_json=False, code=2):
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False)
          if as_json else "ERROR: %s" % message)
    return code


def _validate_mode(args):
    if args.preflight and not args.preflight_out:
        return ("--preflight 必须同时提供 --preflight-out <video_preflight.json>；"
                "后续 capture-video 必须消费同一份能力合同")
    if not args.draft and args.allow_ocr_warning:
        return "正式流程禁止全局接受 OCR 警告；请登记精确 segment/take/OCR texts waiver。"
    if args.draft and args.manifest:
        return "草稿模式不得读取或修改正式 run manifest；请移除 --manifest。"
    if not args.draft and args.no_fallback:
        return "正式流程必须保留能力感知的模型自动降级；--no-fallback 仅限草稿诊断。"
    if not args.batch:
        if args.preflight:
            return "--preflight 必须配合 --batch <segments.json>"
        if args.text:
            return "直接 --text 入口已停用；请完成 preflight 和提示词确认后使用 --batch。"
        return "需要 --batch <segments.json>"
    if args.candidates is not None:
        return ("旧 --candidates 无法形成 take/OCR/客户验收闭环，已停用；"
                "请用 rejected review + new-video-attempt 受控重试。")
    if not args.prompt_review and not args.preflight:
        return "生视频前必须提供 capture-video 生成并由客户确认的 --prompt-review"
    return None


def _validate_chain_declarations(segments, args):
    if not args.chain:
        return None
    undeclared = [
        str(segment.get("id") or index + 1)
        for index, segment in enumerate(segments[1:], 1)
        if not (segment.get("chain_required") or segment.get("avatar_plate_chain") or
                segment.get("extend_video") or segment.get("extend_from_previous"))
    ]
    if undeclared:
        return ("--chain 不能在提示词确认后临时改变参考图。请先把 chain_required/"
                "chain_contract 编译进 segments 并重新确认；未声明镜头=%s" %
                ",".join(undeclared))
    return None


def _select_test_segment(segments, args):
    if args.draft or args.formal_stage != "test_segment":
        return segments
    if not str(args.test_segment_id or "").strip():
        raise ValueError("--formal-stage test_segment 必须同时提供 --test-segment-id")
    selected = [segment for segment in segments
                if str(segment.get("id")) == str(args.test_segment_id)]
    if len(selected) != 1:
        raise ValueError("--test-segment-id 在已登记 batch 中必须精确匹配一个 segment")
    return selected


def _prepare_context(service, segments, args):
    manifest = service._rm.load_manifest(args.manifest) if args.manifest else None
    if not args.draft:
        if not args.client or manifest is None or not args.results_out:
            raise ValueError(
                "正式 batch 必须同时提供 --client、--manifest、--results-out；旧方式请显式使用 --draft")
        if args.locked_refs or args.continuity_plan:
            raise ValueError(
                "正式 batch 禁止在 handoff 后临时修改参考图；请重新编译并登记 video handoff")
    if args.locked_refs:
        segments = service._apply_locked_refs(segments, args.locked_refs)
    if args.continuity_plan:
        with open(args.continuity_plan, encoding="utf-8") as handle:
            segments = service._apply_continuity_plan(segments, json.load(handle))
    service._validate_reference_handoff(segments)
    return segments, manifest


def _render(service, segments, manifest, args):
    use_chain = args.chain or any(
        segment.get("chain_required") or segment.get("avatar_plate_chain") or
        segment.get("extend_video") or segment.get("extend_from_previous")
        for segment in segments[1:])
    if args.formal_stage == "test_segment" and use_chain:
        raise ValueError("TEST_SEGMENT_CHAIN_UNSUPPORTED: 测试片段必须是一个独立 segment")
    common = {
        "model": args.model, "verbose": not args.json, "seed": args.seed,
        "negative_prompt": args.negative,
        "allow_model_fallback": not args.no_fallback, "client": args.client,
        "manifest": manifest, "manifest_path": args.manifest,
        "ledger_path": args.ledger, "results_out": args.results_out,
        "allow_ocr_warning": args.allow_ocr_warning, "draft": args.draft,
        "max_wait": args.max_wait, "prompt_review": args.prompt_review,
        "budget_cap": args.budget_cap,
    }
    if use_chain:
        return service.render_chained(segments, **common)
    common["formal_stage"] = args.formal_stage
    return service.render_batch(segments, **common)


def _persist_results(service, results, args):
    if not args.results_out:
        return True
    try:
        os.makedirs(os.path.dirname(os.path.abspath(args.results_out)) or ".", exist_ok=True)
        persisted = None
        if os.path.isfile(args.results_out):
            with open(args.results_out, encoding="utf-8") as handle:
                persisted = json.load(handle)
        if persisted != results:
            service._atomic_json_write(args.results_out, results)
        if not args.json:
            print("[batch] 结果已写入 %s（可直接喂给 script_splitter assemble --results）" %
                  args.results_out)
        return True
    except (OSError, TypeError, ValueError) as exc:
        print("ERROR: 结果落盘失败 %s：%s" % (args.results_out, exc))
        return False


def _print_results(results, args):
    succeeded = sum(1 for result in results if result and result.get("ok"))
    if args.json:
        print(json.dumps({
            "ok": succeeded == len(results), "count": len(results),
            "succeeded": succeeded, "results": results,
            "results_out": os.path.abspath(args.results_out) if args.results_out else None,
        }, ensure_ascii=False))
    else:
        print("[batch] %d/%d 段成片完成" % (succeeded, len(results)))
        for index, result in enumerate(results, 1):
            value = (result.get("localPath") or result.get("videoUrl") or
                     "FAILED: " + str(result.get("error")))
            print("  seg%d: %s" % (index, value))
    return succeeded


def _mark_ocr_block(service, results, args):
    blocked = any(result and result.get("ocr_warning") for result in results)
    if not blocked or args.allow_ocr_warning:
        return blocked, True
    for result in results:
        if result and result.get("ocr_warning"):
            result["delivery_blocked"] = True
            result["needs_confirmation"] = True
    try:
        persisted = None
        if args.results_out and os.path.isfile(args.results_out):
            with open(args.results_out, encoding="utf-8") as handle:
                persisted = json.load(handle)
        if persisted != results:
            service._atomic_json_write(args.results_out, results)
        return blocked, True
    except (OSError, TypeError, ValueError):
        return blocked, False


def _run_preflight(service, segments, args):
    try:
        segments = _select_test_segment(segments, args)
        manifest = service._rm.load_manifest(
            args.manifest) if args.manifest else None
        if manifest is not None and args.client:
            service._rm.identity_gate(manifest, client=args.client)
        proven_audio_models = (
            service._rm.approved_video_audio_canary_models(manifest)
            if manifest is not None and
            args.formal_stage == "video" and not args.draft else ())
        result = service.inspect_submission_requirements(
            segments, args.model, formal=True,
            allow_model_fallback=not args.no_fallback,
            proven_audio_models=proven_audio_models,
            manifest=manifest, formal_stage=args.formal_stage)
        use_chain = args.chain or any(
            segment.get("chain_required") or
            segment.get("avatar_plate_chain") or
            segment.get("extend_video") or
            segment.get("extend_from_previous")
            for segment in segments[1:])
        if result.get("ready") and use_chain:
            service._validate_chain_reference_budget(
                segments, result, formal=True)
            for index, row in enumerate(result.get("segments") or []):
                segment = segments[index]
                reserved = service._chain_tail_reference_required(
                    segment, index)
                row["chain_tail_reference_reserved"] = reserved
                row["effective_reference_count"] = int(
                    row.get("reference_count") or 0) + (1 if reserved else 0)
            result["chain"] = {
                "enabled": True,
                "tail_reference_budget_validated": True,
            }
        if result.get("ready"):
            result["request_fingerprint"] = \
                service._preflight_request_fingerprint(
                    result.get("segments") or [],
                    result.get("preferred_model"), result.get("formal"))
    except (service.br_client.BRError, ValueError) as exc:
        result = {"ok": False, "ready": False, "segments": [],
                  "blockers": [{"code": "MODEL_PREFLIGHT_FAILED", "message": str(exc)}]}
    if args.preflight_out:
        service._atomic_json_write(args.preflight_out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ready") else 2


def main(service, argv):
    args = build_parser(service).parse_args(argv)
    mode_error = _validate_mode(args)
    if mode_error:
        return _error(mode_error, as_json=args.json)
    try:
        segments = service._load_batch_segments(
            args.batch, ratio_override=args.batch_ratio or args.ratio)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _error("批量段落文件格式错误：%s" % exc, as_json=args.json)
    chain_error = _validate_chain_declarations(segments, args)
    if chain_error:
        return _error(chain_error, as_json=args.json)
    if args.preflight:
        return _run_preflight(service, segments, args)
    try:
        segments = _select_test_segment(segments, args)
        segments, manifest = _prepare_context(service, segments, args)
        results = _render(service, segments, manifest, args)
    except service.br_client.BRError as exc:
        message = service.ux.friendly_error(exc)
        return _error("%s；下一步：%s" % (exc, message), as_json=args.json, code=1)
    if not _persist_results(service, results, args):
        return 1
    succeeded = _print_results(results, args)
    blocked, persisted = _mark_ocr_block(service, results, args)
    if not persisted:
        return 1
    return 0 if succeeded == len(results) and (not blocked or args.allow_ocr_warning) else 1
