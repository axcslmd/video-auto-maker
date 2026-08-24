#!/usr/bin/env python3
"""CLI adapter for the script-to-segment compiler and deterministic assembly."""
import argparse
import json
import os

import audio_contract

from project_utils import atomic_write_bytes


def _add_split(sub, service):
    command = sub.add_parser("split", help="定稿 storyboard_plan.json → render_batch segments")
    command.add_argument("--plan", required=True)
    command.add_argument("--storyboard-dir", help="分镜图目录（阶段3 storyboard.py 输出）")
    command.add_argument("--out", help="输出 segments JSON（默认 stdout）")
    command.add_argument("--fps", type=int, default=30)
    command.add_argument("--min-seconds", type=int, default=3)
    command.add_argument("--ratio", dest="ratio_override", choices=sorted(service._RATIO_MAP))
    command.add_argument("--color-storyboard", action="store_true")
    command.add_argument("--allow-text2video", action="store_true")
    command.add_argument("--client", required=True)
    command.add_argument("--allow-unconfirmed", action="store_true")
    command.add_argument("--run-id")
    command.add_argument("--manifest")
    command.add_argument(
        "--recovery-contract",
        help="正式失败恢复合同；绑定当前 handoff/失败 take 后重编译模型与 canary")
    command.add_argument("--draft", action="store_true")
    command.add_argument("--draft-allow-unapproved-storyboard", action="store_true")


def _add_assemble(sub):
    command = sub.add_parser("assemble", help="多段底片 → ffmpeg 拼接为最终版底片")
    command.add_argument("--segments", required=True)
    command.add_argument("--results")
    command.add_argument("--out", required=True)
    command.add_argument("--allow-ocr-warning", action="store_true")
    command.add_argument("--legacy-unsafe", action="store_true")
    command.add_argument("--client")
    command.add_argument("--manifest")
    command.add_argument("--reviews")
    command.add_argument("--draft", action="store_true")


def _add_captions(sub):
    derive = sub.add_parser("derive-captions", help="底片完成后生成待确认字幕与动效时间线")
    derive.add_argument("--segments", required=True)
    derive.add_argument("--fps", type=int, default=30)
    derive.add_argument("--whole-segment", action="store_true")
    derive.add_argument("--srt-out")
    derive.add_argument("--lines-out")
    derive.add_argument("--motion-out")
    derive.add_argument("--audio-video")
    derive.add_argument("--results")
    derive.add_argument("--client")
    derive.add_argument("--manifest")
    derive.add_argument("--basecut")
    derive.add_argument("--caption-manifest-out")
    derive.add_argument("--draft", action="store_true")
    confirm = sub.add_parser("confirm-captions", help="确认当前 caption timeline artifact")
    confirm.add_argument("--client", required=True)
    confirm.add_argument("--manifest", required=True)
    confirm.add_argument("--caption-manifest", required=True)
    confirm.add_argument("--customer-confirmation", required=True)


def _add_recovery(sub):
    finalize = sub.add_parser(
        "finalize-recovery-privacy-policy",
        help="恢复 split 后从已确认模板派生当前 handoff 的 stop-only 合同")
    finalize.add_argument("--manifest", required=True)
    finalize.add_argument("--segments", required=True)
    finalize.add_argument("--template", required=True)
    finalize.add_argument("--out", required=True)
    command = sub.add_parser(
        "bind-recovery-privacy-policy",
        help="把 stop-only 真人隐私策略原子绑定到当前 recovery canary")
    command.add_argument("--manifest", required=True)
    command.add_argument("--segments", required=True)
    command.add_argument("--policy-contract", required=True)
    output = sub.add_parser(
        "bind-recovery-canary-output",
        help="把 recovery canary 原子绑定到 attempt 专用且不可覆盖的输出路径")
    output.add_argument("--manifest", required=True)
    output.add_argument("--segments", required=True)
    attempt_output = sub.add_parser(
        "bind-recovery-attempt-output",
        help="把任一已拒绝 recovery 段绑定到下一 attempt 的独立输出路径")
    attempt_output.add_argument("--manifest", required=True)
    attempt_output.add_argument("--segments", required=True)
    attempt_output.add_argument("--segment-id", required=True)
    evidence = attempt_output.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--review")
    evidence.add_argument(
        "--failure-results",
        help="无媒体 provider 失败时使用已结算且已授权的 results JSON")
    promote = sub.add_parser(
        "promote-recovery-audio-canary",
        help="将已批准且非静音的 recovery audio canary 原子晋级为正式批次")
    promote.add_argument("--manifest", required=True)
    promote.add_argument("--segments", required=True)


def build_parser(service):
    parser = argparse.ArgumentParser(description="阶段4 · 剧本按时长拆分 + 多段底片合成")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_split(sub, service)
    _add_assemble(sub)
    _add_captions(sub)
    _add_recovery(sub)
    return parser


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _read_recovery_source_handoff(service, manifest, out_path):
    """Load the exact handoff bytes bound by a recovery contract.

    Rebuilding the old source from an authored plan is unsafe after a model
    capability correction: provider selection metadata may change even though
    the customer-approved content did not.  Recovery contracts bind the
    existing handoff hash, so recompilation must start from that exact file.
    """
    handoff = (manifest.get("handoffs") or {}).get("video") or {}
    stored = handoff.get("file") or {}
    current = service.rm.file_record(stored.get("path"))
    if (not current or not service.rm.file_record_is_current(stored) or
            current.get("sha256") != stored.get("sha256")):
        raise ValueError("VIDEO_RECOVERY_SOURCE_HANDOFF_STALE")
    if os.path.abspath(out_path) != current.get("path"):
        raise ValueError("VIDEO_RECOVERY_OUTPUT_MUST_REPLACE_SOURCE_HANDOFF")
    source = _read_json(current["path"])
    if (str(source.get("client")) != str(manifest.get("client")) or
            str(source.get("run_id")) != str(manifest.get("run_id"))):
        raise ValueError("VIDEO_RECOVERY_SOURCE_HANDOFF_IDENTITY_MISMATCH")
    return source


def _run_split(service, parser, args):
    if args.draft and args.manifest:
        parser.error("split 的 --draft 与 --manifest 互斥；草稿不得读取或修改正式 run")
    if args.recovery_contract and (args.draft or not args.manifest):
        parser.error("--recovery-contract 仅允许绑定正式 --manifest")
    if not args.draft and (args.allow_unconfirmed or
                           args.draft_allow_unapproved_storyboard):
        parser.error("--allow-unconfirmed/--draft-allow-unapproved-storyboard 仅可与 --draft 同用")
    if not args.manifest and not args.draft:
        parser.error("split 正式流程必须提供 --manifest，以校验已确认 render plan；旧方式需 --draft")
    if args.manifest and not args.out:
        parser.error("split 使用 --manifest 时必须提供 --out，才能原子登记 handoff 文件")
    plan = _read_json(args.plan)
    manifest, render_plan_artifact, identity_plate_artifact = None, None, None
    approved_audio_direction = None
    if args.manifest:
        manifest = service.rm.load_manifest(args.manifest)
        service.rm.identity_gate(manifest, client=args.client)
        gate_stage = ("test_segment" if manifest.get("requires_test_segment") and
                      not service.rm.approval_is_current(manifest, "test_segment") else "video")
        service.rm.generation_gate(manifest, gate_stage, client=args.client)
        if service.rm.workflow_requirements(manifest).get("requires_audio"):
            approved_audio_direction = audio_contract.approved_direction_from_manifest(
                manifest)
        render_generation = (manifest.get("generation", {}).get(
            "render_plan", {}) or {})
        render_plan_artifact = render_generation.get("primary_artifact") or {}
        current_plan = service.rm.file_record(render_plan_artifact.get("path"))
        if (not current_plan or not current_plan.get("exists") or
                current_plan.get("sha256") != render_plan_artifact.get("sha256")):
            raise ValueError("RENDER_PLAN_ARTIFACT_REQUIRED")
        render_plan_artifact = current_plan
        if str((manifest.get("chain_profile") or {}).get("profile") or "") == (
                "document_explainer"):
            if not service.rm.identity_plate_generation_is_complete(manifest):
                raise ValueError("IDENTITY_PLATE_ARTIFACT_REQUIRED")
            identity_state = manifest.get("identity_plate_generation") or {}
            identity_plate_artifact = {
                "output": identity_state.get("output") or {},
                "remote_url": identity_state.get("remote_url"),
                "generation_result": (
                    identity_state.get("generation_result") or {}),
            }
    if args.recovery_contract:
        if manifest is None:
            raise ValueError("VIDEO_RECOVERY_MANIFEST_REQUIRED")
        result = _read_recovery_source_handoff(
            service, manifest, args.out)
        result = service.apply_video_recovery_contract(
            result, args.recovery_contract, manifest)
    else:
        result = service.split(
            plan, storyboard_dir=args.storyboard_dir, fps=args.fps,
            min_seconds=args.min_seconds,
            bw_storyboard=False if args.color_storyboard else None,
            allow_text2video=args.allow_text2video, client=args.client,
            allow_unconfirmed=args.allow_unconfirmed,
            ratio_override=args.ratio_override, run_id=args.run_id,
            draft_allow_unapproved_storyboard=args.draft_allow_unapproved_storyboard,
            render_plan_artifact=render_plan_artifact,
            identity_plate_artifact=identity_plate_artifact,
            approved_audio_direction=approved_audio_direction)
    if not args.out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.manifest:
        if str(result.get("run_id")) != str(manifest.get("run_id")):
            raise ValueError("VIDEO_HANDOFF_RUN_IDENTITY_MISMATCH")
        if result.get("needs_image") or result.get("missing_images") or not result.get("segments"):
            raise ValueError("VIDEO_HANDOFF_INCOMPLETE: split 尚有待补素材/缺失分镜图或无 segments")
        service._commit_video_handoff(manifest, args.manifest, args.out, result)
    else:
        service._atomic_json(args.out, result)
    print("已拆分: %s（%d 段，总时长 %ds）" %
          (args.out, len(result["segments"]), result["total_seconds"]))
    if result["missing_images"]:
        print("[警告] 以下 shot 未找到分镜图（将无首帧锚定）: %s" %
              ", ".join(result["missing_images"]))
    if result["needs_image"]:
        print("[待补素材] 以下 shot 零锚定素材，已跳过未生成 segment: %s\n"
              "若确为纯数字人口播无需产品图/场景图，加 --allow-text2video 显式放行。" %
              ", ".join(result["needs_image"]))
    if result.get("unconfirmed_refs"):
        print("[待确认] 以下素材未经客户确认(status=pending)，已显式放行: %s" %
              result["unconfirmed_refs"])


def _run_assemble(service, parser, args):
    if args.legacy_unsafe and not args.draft:
        parser.error("--legacy-unsafe 仅可与 --draft 同用，且产物不可进入正式交付")
    if not args.results and not args.legacy_unsafe:
        parser.error("assemble 正式流程必须提供 --results；旧文件回退需显式 --legacy-unsafe")
    if not args.draft and not args.legacy_unsafe and not (
            args.client and args.manifest and args.reviews):
        parser.error("assemble 正式流程必须提供 --client/--manifest/--reviews；旧方式需 --draft")
    if args.allow_ocr_warning and not (args.legacy_unsafe or args.draft):
        parser.error("正式 assemble 不接受全局 --allow-ocr-warning；请登记精确 OCR waiver")
    spec = _read_json(args.segments)
    results = _read_json(args.results) if args.results else None
    exact_ocr_allowed = False
    if not args.draft and not args.legacy_unsafe:
        manifest = _read_json(args.manifest)
        service.persist_and_gate_assemble_ocr(
            manifest, args.manifest, spec, results, client=args.client)
        service.begin_video_assembly(
            manifest, args.manifest, client=args.client,
            segments_path=args.segments, results_path=args.results,
            basecut_path=args.out, reviews_path=args.reviews)
        exact_ocr_allowed = True
    result = service.assemble(
        spec, results, args.out,
        allow_ocr_warning=args.allow_ocr_warning or exact_ocr_allowed,
        legacy_unsafe=args.legacy_unsafe)
    if not args.draft and not args.legacy_unsafe:
        service.record_video_stage_pending(
            manifest, args.manifest, client=args.client, segments_path=args.segments,
            results_path=args.results, basecut_path=args.out, reviews_path=args.reviews)
        result["status"] = "pending_approval"
    print(json.dumps(result, ensure_ascii=False))


def _write_caption_outputs(service, args, result):
    if args.srt_out:
        atomic_write_bytes(args.srt_out, result["srt"].encode("utf-8"))
    if args.lines_out:
        service._atomic_json(args.lines_out, result["lines"])
    if args.motion_out:
        service._atomic_json(args.motion_out, result["motion_plan"])


def _run_derive(service, parser, args):
    required = (args.client, args.manifest, args.basecut, args.results,
                args.caption_manifest_out, args.srt_out, args.lines_out, args.motion_out)
    if not args.draft and not all(required):
        parser.error("derive-captions 正式流程必须提供完整客户、manifest、底片、结果和输出路径")
    spec = _read_json(args.segments)
    manifest = None
    if not args.draft:
        manifest = _read_json(args.manifest)
        if args.audio_video and os.path.abspath(args.audio_video) != os.path.abspath(args.basecut):
            parser.error("正式 derive-captions 的 --audio-video 必须与已批准 --basecut 为同一文件")
        service.preflight_caption_inputs(
            manifest, client=args.client, segments_path=args.segments,
            basecut_path=args.basecut, results_path=args.results,
            output_paths=(("caption_lines", args.lines_out), ("caption_srt", args.srt_out),
                          ("caption_motion", args.motion_out),
                          ("caption_manifest", args.caption_manifest_out)))
    caption_results = _read_json(args.results) if args.results else None
    result = service.derive_captions(
        spec, fps=args.fps, per_sentence=not args.whole_segment, results=caption_results)
    if args.audio_video:
        result = service.align_captions_to_audio(result, args.audio_video)
    _write_caption_outputs(service, args, result)
    artifact = None
    if not args.draft:
        artifact = service.persist_caption_artifact(
            manifest, args.manifest, client=args.client, segments_path=args.segments,
            basecut_path=args.basecut, lines_path=args.lines_out, srt_path=args.srt_out,
            motion_path=args.motion_out, caption_manifest_path=args.caption_manifest_out)
    print(json.dumps({
        "ok": result["ok"], "needs_confirmation": result["needs_confirmation"],
        "total_seconds": result["total_seconds"], "caption_lines": len(result["lines"]),
        "motion_segments": len(result["motion_plan"]), "srt_out": args.srt_out,
        "lines_out": args.lines_out, "motion_out": args.motion_out,
        "caption_manifest": args.caption_manifest_out,
        "caption_identity": artifact.get("caption_identity") if artifact else None,
        "note": result["note"],
    }, ensure_ascii=False, indent=2))


def _run_confirm(service, args):
    manifest = _read_json(args.manifest)
    artifact = service.confirm_captions(
        manifest, args.manifest, args.caption_manifest, client=args.client,
        customer_confirmation=args.customer_confirmation)
    print(json.dumps({"ok": True, "caption_identity": artifact["caption_identity"],
                      "status": artifact["status"]}, ensure_ascii=False, indent=2))


def _run_recovery_policy(service, args):
    manifest = service.rm.load_manifest(args.manifest)
    result = service.bind_recovery_privacy_policy(
        manifest, args.manifest, args.segments, args.policy_contract)
    canary = next(
        segment for segment in result.get("segments") or []
        if segment.get("privacy_fallback_policy"))
    print(json.dumps({
        "ok": True, "segment_id": canary.get("id"),
        "policy": canary.get("privacy_fallback_policy"),
        "video_handoff_fingerprint": canary.get(
            "video_handoff_fingerprint"),
    }, ensure_ascii=False, indent=2))


def _run_finalize_recovery_policy(service, args):
    manifest = service.rm.load_manifest(args.manifest)
    record = service.finalize_recovery_privacy_policy_contract(
        manifest, args.segments, args.template, args.out)
    print(json.dumps({
        "ok": True, "policy_contract": record,
    }, ensure_ascii=False, indent=2))


def _run_recovery_output(service, args):
    manifest = service.rm.load_manifest(args.manifest)
    result = service.bind_recovery_canary_output(
        manifest, args.manifest, args.segments)
    print(json.dumps({
        "ok": True, "segment_id": result["segment_id"],
        "attempt": result["attempt"], "out_path": result["out_path"],
        "video_handoff_fingerprint": result[
            "video_handoff_fingerprint"],
    }, ensure_ascii=False, indent=2))


def _run_recovery_attempt_output(service, args):
    manifest = service.rm.load_manifest(args.manifest)
    result = service.bind_recovery_attempt_output(
        manifest, args.manifest, args.segments,
        args.segment_id, args.review, args.failure_results)
    print(json.dumps({
        "ok": True, "segment_id": result["segment_id"],
        "attempt": result["attempt"], "out_path": result["out_path"],
        "video_handoff_fingerprint": result[
            "video_handoff_fingerprint"],
    }, ensure_ascii=False, indent=2))


def _run_promote_audio_canary(service, args):
    manifest = service.rm.load_manifest(args.manifest)
    result = service.promote_recovery_audio_canary(
        manifest, args.manifest, args.segments)
    promotion = result.get("promotion") or {}
    print(json.dumps({
        "ok": True,
        "already_promoted": bool(result.get("already_promoted")),
        "segment_id": promotion.get("segment_id"),
        "model_used": promotion.get("model_used"),
        "audio_canary_proof_fingerprint": promotion.get(
            "audio_canary_proof_fingerprint"),
        "next_action": "capture_and_confirm_formal_batch_video_prompts",
    }, ensure_ascii=False, indent=2))


def main(service, argv=None):
    parser = build_parser(service)
    args = parser.parse_args(argv)
    handlers = {
        "split": _run_split,
        "assemble": _run_assemble,
        "derive-captions": _run_derive,
    }
    if args.cmd == "confirm-captions":
        _run_confirm(service, args)
    elif args.cmd == "finalize-recovery-privacy-policy":
        _run_finalize_recovery_policy(service, args)
    elif args.cmd == "bind-recovery-privacy-policy":
        _run_recovery_policy(service, args)
    elif args.cmd == "bind-recovery-canary-output":
        _run_recovery_output(service, args)
    elif args.cmd == "bind-recovery-attempt-output":
        _run_recovery_attempt_output(service, args)
    elif args.cmd == "promote-recovery-audio-canary":
        _run_promote_audio_canary(service, args)
    else:
        handlers[args.cmd](service, parser, args)
