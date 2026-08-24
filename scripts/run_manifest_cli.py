#!/usr/bin/env python3
"""CLI adapter for :mod:`run_manifest`.

The manifest module owns state transitions; this adapter owns argparse and
human-readable command dispatch so business invariants remain importable
without carrying a 200-line CLI function.
"""
import argparse
import json
import os


def _add_run_commands(sub, service):
    create = sub.add_parser("create")
    create.add_argument("--client", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--script")
    create.add_argument("--plan")
    create.add_argument("--out", required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--manifest", required=True)
    approve.add_argument("--stage", choices=service.STAGES, required=True)
    approve.add_argument("--reject", action="store_true")
    output = sub.add_parser("add-output")
    output.add_argument("--manifest", required=True)
    output.add_argument("--path", required=True)
    output.add_argument("--kind", default="file")
    for name, help_text in (
            ("gate", "检查某素材阶段是否允许开始生成"),
            ("start-stage", "通过依赖门禁并登记阶段开始")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--manifest", required=True)
        command.add_argument("--stage", choices=service.STAGES, required=True)
    finish = sub.add_parser("finish-stage", help="登记阶段产物并等待客户确认")
    finish.add_argument("--manifest", required=True)
    finish.add_argument("--stage", choices=service.STAGES, required=True)
    finish.add_argument("--path", action="append", required=True)
    fail = sub.add_parser("fail-stage", help="关闭失败的 running 事务并保留重试上下文")
    fail.add_argument("--manifest", required=True)
    fail.add_argument("--stage", choices=service.STAGES, required=True)
    fail.add_argument("--error", required=True)
    fail.add_argument("--error-code", default="GENERATION_FAILED")
    fail.add_argument("--diagnostic", action="append", default=[])
    fail.add_argument("--no-retry", action="store_true")
    for name, help_text in (("status", "输出 manifest 阶段状态"),
                            ("next", "输出下一待处理阶段")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--manifest", required=True)


def _add_contract_commands(sub, service):
    bootstrap = sub.add_parser(
        "bootstrap-approval", help="把 manifest 创建前已有的 brief/script 登记为 pending_approval")
    bootstrap.add_argument("--manifest", required=True)
    bootstrap.add_argument("--stage", choices=("brief", "script"), required=True)
    bootstrap.add_argument("--path", action="append", required=True)
    bind_plan = sub.add_parser(
        "bind-plan", help="绑定 storyboard_plan 并刷新阶段需求；计划变化会重开脚本确认")
    bind_plan.add_argument("--manifest", required=True)
    bind_plan.add_argument("--plan", required=True)
    repair_board = sub.add_parser(
        "repair-board-approval", help="把旧版产品/人物/使用板审批迁移到不可变产物合同")
    repair_board.add_argument("--manifest", required=True)
    repair_board.add_argument(
        "--stage", choices=sorted(service.STABLE_VISUAL_BOARD_STAGES), required=True)
    repair_pending = sub.add_parser(
        "replace-pending-artifacts", help="显式修复尚未批准阶段的产物所有权集合")
    repair_pending.add_argument("--manifest", required=True)
    repair_pending.add_argument("--stage", choices=service.STAGES, required=True)
    repair_pending.add_argument("--path", action="append", required=True)


def _add_video_commands(sub):
    test_confirm = sub.add_parser(
        "confirm-test-segment", help="用正式 accepted review 和客户原话确认测试片段")
    test_confirm.add_argument("--manifest", required=True)
    test_confirm.add_argument("--review", required=True)
    test_confirm.add_argument("--customer-confirmation", required=True)
    handoff = sub.add_parser("record-video-handoff", help="原子登记 segments 出片交接")
    handoff.add_argument("--manifest", required=True)
    handoff.add_argument("--segments", required=True)
    take = sub.add_parser("accept-take", help="登记已通过验片的精确 take")
    take.add_argument("--manifest", required=True)
    take.add_argument("--segment-id", required=True)
    take.add_argument("--review", required=True)
    attempt = sub.add_parser("new-video-attempt", help="验收/拒绝 take 后受控授权新 attempt")
    attempt.add_argument("--manifest", required=True)
    attempt.add_argument("--segment-id", required=True)
    attempt.add_argument("--actor", required=True)
    attempt.add_argument("--reason", required=True)
    attempt.add_argument("--review", help="正式 rejected review JSON；accepted take 可省略")
    attempt.add_argument(
        "--failure-results",
        help="无媒体 provider 失败时，传已由 settlement 绑定的精确 results JSON")
    reconcile = sub.add_parser(
        "reconcile-test-segment-recovery-authorization",
        help="仅凭当前失败 settlement 与重试授权修复测试段恢复路由")
    reconcile.add_argument("--manifest", required=True)
    reconcile.add_argument("--segment-id", required=True)
    settle = sub.add_parser(
        "settle-video-generation",
        help="按已落盘 batch results 原子收口 lease，并路由验片或恢复")
    settle.add_argument("--manifest", required=True)
    settle.add_argument("--results", required=True)
    settle_test = sub.add_parser(
        "settle-test-segment-failure",
        help="按失败试片 results 原子收口 lease，并路由到受控恢复")
    settle_test.add_argument("--manifest", required=True)
    settle_test.add_argument("--results", required=True)


def _add_ocr_commands(sub):
    ocr = sub.add_parser("record-ocr", help="登记一个精确 take 的 OCR 结果")
    ocr.add_argument("--manifest", required=True)
    ocr.add_argument("--segment-id", required=True)
    ocr.add_argument("--take-fingerprint", required=True)
    ocr.add_argument("--status", choices=("clear", "detected", "unavailable", "error"),
                     required=True)
    ocr.add_argument("--available", action=argparse.BooleanOptionalAction, default=None)
    ocr.add_argument("--frames-checked", type=int, default=0)
    ocr.add_argument("--expected", type=int, default=0)
    ocr.add_argument("--error")
    ocr.add_argument("--text", action="append", default=[])
    waiver = sub.add_parser("waive-ocr", help="仅豁免指定 segment/take/OCR texts")
    waiver.add_argument("--manifest", required=True)
    waiver.add_argument("--segment-id", required=True)
    waiver.add_argument("--take-fingerprint", required=True)
    waiver.add_argument("--text", action="append", default=[])
    waiver.add_argument("--actor", required=True)
    waiver.add_argument("--reason", required=True)
    manual = sub.add_parser("manual-ocr-review", help="登记绑定精确 take 的人工 OCR 复核")
    manual.add_argument("--manifest", required=True)
    manual.add_argument("--segment-id", required=True)
    manual.add_argument("--take-fingerprint", required=True)
    manual.add_argument("--status", choices=("clear", "detected"), required=True)
    manual.add_argument("--text", action="append", default=[])
    manual.add_argument("--frame-sha256", action="append", default=[])
    manual.add_argument("--reviewer", required=True)
    manual.add_argument("--reason", required=True)


def build_parser(service):
    parser = argparse.ArgumentParser(
        description="Create/update a resumable project run manifest")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_run_commands(sub, service)
    _add_contract_commands(sub, service)
    _add_video_commands(sub)
    _add_ocr_commands(sub)
    return parser


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _status(service, manifest, command):
    stages = []
    identity_required = service.identity_plate_reference_required(manifest)
    for stage in service.required_stage_order(manifest):
        stages.append({
            "stage": stage, "required": True,
            "complete": service.stage_is_complete(manifest, stage),
            "approved": service.approval_is_current(manifest, stage),
            "status": (manifest.get("generation", {}).get(stage) or {}).get("status"),
        })
        if stage == "render_plan" and identity_required:
            identity_status = (manifest.get("identity_plate_generation") or {}).get(
                "status")
            for pseudo, complete in (
                    ("identity_plate_reference",
                     service.identity_plate_reference_is_current(manifest)),
                    ("identity_plate_submission_capture",
                     service.identity_plate_submission_is_current(manifest)),
                    ("identity_plate_generation",
                     service.identity_plate_generation_candidate_is_current(manifest)),
                    ("identity_plate_review",
                     service.identity_plate_generation_is_complete(manifest))):
                stages.append({
                    "stage": pseudo, "required": True,
                    "complete": complete, "approved": complete,
                    "status": identity_status,
                })
    next_name = service.next_required_stage(manifest)
    pending = next((item for item in stages if item["stage"] == next_name), None)
    if command == "next":
        return pending
    return {
        "run_id": manifest.get("run_id"), "client": manifest.get("client"),
        "status": manifest.get("status"), "stages": stages, "next": pending,
        "delivery": manifest.get("delivery"),
        "workflow_contract_version": manifest.get("workflow_contract_version"),
    }


def _dispatch_contract(service, manifest, args):
    if args.command == "bootstrap-approval":
        service.bootstrap_pending_approval(manifest, args.stage, args.path)
    elif args.command == "bind-plan":
        return service.bind_storyboard_plan(manifest, args.plan)
    elif args.command == "repair-board-approval":
        return service.repair_visual_board_approval(manifest, args.stage)
    elif args.command == "replace-pending-artifacts":
        return service.replace_pending_stage_artifacts(manifest, args.stage, args.path)
    return None


def _dispatch_video(service, manifest, args):
    if args.command == "confirm-test-segment":
        service.approve_test_segment(manifest, args.review, args.customer_confirmation)
    elif args.command == "record-video-handoff":
        segments = _read_json(args.segments)
        if (segments.get("client") != manifest.get("client") or
                str(segments.get("run_id")) != str(manifest.get("run_id"))):
            raise ValueError("VIDEO_HANDOFF_RUN_IDENTITY_MISMATCH")
        service.record_video_handoff(manifest, segments, args.segments)
    elif args.command == "accept-take":
        review = _read_json(args.review)
        if str(review.get("segment_id")) != str(args.segment_id):
            raise ValueError("TAKE_REVIEW_SEGMENT_MISMATCH")
        expected = (((manifest.get("handoffs") or {}).get("video") or {})
                    .get("segments") or {}).get(args.segment_id)
        if (not expected or
                review.get("artifact", {}).get("video_handoff_fingerprint") != expected):
            raise ValueError("TAKE_REVIEW_HANDOFF_MISMATCH")
        service.accept_take(manifest, args.segment_id, review, args.review)
    elif args.command == "new-video-attempt":
        review = _read_json(args.review) if args.review else None
        service.request_video_attempt(
            manifest, args.segment_id, actor=args.actor, reason=args.reason,
            review=review, review_path=args.review,
            failure_results=args.failure_results)
    elif args.command == "reconcile-test-segment-recovery-authorization":
        return service.reconcile_authorized_test_segment_recovery(
            manifest, args.segment_id)
    elif args.command == "settle-video-generation":
        results = _read_json(args.results)
        # The dispatcher normally saves its in-memory manifest after return;
        # settlement must instead use the CAS entrypoint because a worker and a
        # recovery operator may race on the same expired lease.
        settled, _ = service.record_video_generation_settlement(
            args.manifest, args.results, results)
        manifest.clear()
        manifest.update(settled)


def _dispatch_ocr(service, manifest, args):
    if args.command == "record-ocr":
        service.record_ocr_result(
            manifest, args.segment_id, args.take_fingerprint, args.status, args.text,
            available=args.available, frames_checked=args.frames_checked,
            expected=args.expected, error=args.error)
    elif args.command == "waive-ocr":
        service.grant_ocr_waiver(
            manifest, args.segment_id, args.take_fingerprint, args.text,
            actor=args.actor, reason=args.reason)
    elif args.command == "manual-ocr-review":
        service.record_manual_ocr_review(
            manifest, args.segment_id, args.take_fingerprint, args.status,
            reviewer=args.reviewer, reason=args.reason, ocr_texts=args.text,
            frame_sha256s=args.frame_sha256)


def _dispatch(service, manifest, args):
    if args.command == "approve":
        service.approve(manifest, args.stage, not args.reject, strict=True)
    elif args.command == "add-output":
        service.add_output(manifest, args.path, args.kind)
    elif args.command in {"bootstrap-approval", "bind-plan", "repair-board-approval",
                          "replace-pending-artifacts"}:
        return _dispatch_contract(service, manifest, args)
    elif args.command == "start-stage":
        if args.stage == "audio":
            raise ValueError(
                "AUDIO_STAGE_SPECIALIZED_PREPARE_REQUIRED: 请先完成声音模式、语言、"
                "音色、语气和 BGM 选择，再使用 audio_contract.py prepare；"
                "禁止把尚待客户回答的 audio 阶段登记为运行中")
        service.mark_generation_started(manifest, args.stage)
    elif args.command == "fail-stage":
        service.mark_generation_failed(
            manifest, args.stage, args.error, error_code=args.error_code,
            retryable=not args.no_retry, diagnostics=args.diagnostic)
    elif args.command == "finish-stage":
        if args.stage in service.SPECIALIZED_FINISH_STAGES:
            raise ValueError(
                "SPECIALIZED_STAGE_FINISH_REQUIRED: %s 必须使用对应内容绑定记录器，不能用通用 finish-stage"
                % args.stage)
        service.mark_generation_finished(manifest, args.stage, args.path)
    elif args.command in {"confirm-test-segment", "record-video-handoff", "accept-take",
                          "new-video-attempt",
                          "reconcile-test-segment-recovery-authorization"}:
        _dispatch_video(service, manifest, args)
    elif args.command in {"record-ocr", "waive-ocr", "manual-ocr-review"}:
        _dispatch_ocr(service, manifest, args)
    return None


def _mutation_summary(manifest, stage, args, settlement=None):
    """Return a bounded recovery summary instead of echoing the manifest."""
    generation = (manifest.get("generation") or {}).get(stage) or {}
    settlement = settlement or generation.get("settlement") or {}
    summary = (settlement.get("summary") or {})
    return {
        "ok": True,
        "run_id": manifest.get("run_id"),
        "revision": manifest.get("revision"),
        "status": manifest.get("status"),
        "stage": stage,
        "generation_status": generation.get("status"),
        "segment_id": (generation.get("segment_id") or
                       getattr(args, "segment_id", None)),
        "task_id": generation.get("task_id"),
        "error_code": (generation.get("error_code") or
                       (generation.get("failure") or {}).get("error_code")),
        "result_counts": ({
            "total": summary.get("total"),
            "succeeded": summary.get("succeeded"),
            "failed": summary.get("failed"),
        } if summary else None),
    }


def main(service, argv=None):
    args = build_parser(service).parse_args(argv)
    if args.command == "create":
        if os.path.exists(args.out):
            raise SystemExit(
                "RUN_EXISTS: 这个项目记录已经存在。为避免覆盖历史确认和成片，请继续使用原记录恢复，"
                "或更换 --run-id/--out 创建新版本。")
        manifest = service.create_manifest(
            args.client, args.run_id, script_path=args.script, plan_path=args.plan)
        service.save_manifest(manifest, args.out, create_only=True)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    manifest = service.load_manifest(args.manifest)
    if args.command == "gate":
        service.generation_gate(manifest, args.stage)
        print(json.dumps({"ok": True, "stage": args.stage,
                          "dependencies": service.generation_dependencies(manifest, args.stage)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command in {"status", "next"}:
        print(json.dumps(_status(service, manifest, args.command),
                         ensure_ascii=False, indent=2))
        return 0
    if args.command in {
            "settle-video-generation", "settle-test-segment-failure"}:
        results = _read_json(args.results)
        if args.command == "settle-video-generation":
            settled, settlement = service.record_video_generation_settlement(
                args.manifest, args.results, results)
            stage = "video"
        else:
            settled, settlement = (
                service.record_test_segment_failure_settlement(
                    args.manifest, args.results, results))
            stage = "test_segment"
        print(json.dumps(
            _mutation_summary(settled, stage, args, settlement),
            ensure_ascii=False, indent=2))
        return 0
    result = _dispatch(service, manifest, args)
    service.save_manifest(manifest, args.manifest)
    if args.command in {
            "new-video-attempt",
            "reconcile-test-segment-recovery-authorization"}:
        print(json.dumps(
            _mutation_summary(
                manifest,
                ("test_segment" if args.command.startswith("reconcile-")
                 else "video"), args),
            ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(result if result is not None else manifest,
                     ensure_ascii=False, indent=2))
    return 0
