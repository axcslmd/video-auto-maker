#!/usr/bin/env python3
"""Customer-facing approval, status, and immutable delivery orchestration."""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import script_splitter  # noqa: E402
import cost_ledger  # noqa: E402 — v3 cost tracking
import derive_ratio  # noqa: E402 — v4 derive stage
import workflow_router  # noqa: E402
import delivery_contract  # noqa: E402

import run_manifest as rm


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def approve_stage(manifest, manifest_path, stage, *, storyboard_result=None,
                  customer_confirmation=None, caption_manifest=None,
                  geometry_review=None, test_review=None):
    """Approve through the sole formal gate for each customer-decision stage."""
    if stage in rm.VISUAL_CUSTOMER_EVIDENCE_STAGES:
        if not storyboard_result:
            raise ValueError("STORYBOARD_RESULT_REQUIRED")
        import storyboard
        if not str(customer_confirmation or "").strip():
            raise ValueError("CUSTOMER_CONFIRMATION_EVIDENCE_REQUIRED")
        result = _load(storyboard_result)
        bound_manifest = os.path.abspath(result.get("manifest_path") or "")
        if bound_manifest != os.path.abspath(manifest_path):
            raise ValueError("STORYBOARD_RESULT_MANIFEST_BINDING_REQUIRED")
        if stage == "storyboard":
            storyboard.confirm_storyboard(
                storyboard_result, customer_confirmation=customer_confirmation)
        else:
            kind = {
                "product_board": "product",
                "cast_board": "cast",
                "product_usage": "usage",
            }[stage]
            storyboard.confirm_board(
                storyboard_result, kind,
                geometry_review_path=geometry_review,
                customer_confirmation=customer_confirmation)
        refreshed = rm.load_manifest(manifest_path)
        if not rm.approval_is_current(refreshed, stage):
            raise ValueError("%s_APPROVAL_COMMIT_FAILED" % stage.upper())
        manifest.clear()
        manifest.update(refreshed)
        return manifest
    if stage == "captions":
        if not caption_manifest:
            raise ValueError("CAPTION_MANIFEST_REQUIRED")
        if not str(customer_confirmation or "").strip():
            raise ValueError("CUSTOMER_CONFIRMATION_EVIDENCE_REQUIRED")
        script_splitter.confirm_captions(
            manifest, manifest_path, caption_manifest,
            client=manifest.get("client"),
            customer_confirmation=customer_confirmation)
        refreshed = rm.load_manifest(manifest_path)
        manifest.clear()
        manifest.update(refreshed)
        return manifest
    if stage == "test_segment":
        if not test_review:
            raise ValueError("TEST_SEGMENT_ACCEPTED_REVIEW_REQUIRED")
        rm.approve_test_segment(manifest, test_review, customer_confirmation)
        rm.save_manifest(manifest, manifest_path)
        return manifest
    closure_validators = {
        "video": rm.validate_video_closure,
        "shotcraft_packaging": rm.validate_shotcraft_closure,
        "final": rm.validate_final_closure,
        "derive": rm.validate_derive_closure,
    }
    if stage in closure_validators:
        # Fail before writing a customer evidence marker. A customer decision
        # cannot repair or authorize an incomplete technical transaction.
        closure_validators[stage](manifest)
    if stage in rm.GENERIC_CUSTOMER_EVIDENCE_STAGES:
        rm.record_customer_approval_evidence(
            manifest, stage, customer_confirmation)
    rm.approve(manifest, stage, strict=True)
    rm.save_manifest(manifest, manifest_path)
    return manifest


def _try_cost_summary(manifest):
    """Best-effort cost estimation from generation ledger. Never raises."""
    try:
        run_dir = manifest.get("run_dir") or os.path.dirname(
            manifest.get("manifest_path", ""))
        ledger_path = os.path.join(run_dir, "generation_runs.jsonl")
        if os.path.isfile(ledger_path):
            return cost_ledger.estimate_run_cost(ledger_path)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    return None


def _try_cost_line(manifest):
    """Best-effort human-readable cost line for CLI output."""
    try:
        summary = _try_cost_summary(manifest)
        if summary:
            return cost_ledger.format_cost_line(summary)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    return ""


def pipeline_status(manifest):
    """Return one stable, customer-readable next action with absolute previews."""
    route = workflow_router.route_workflow(manifest=manifest)
    route["cost_line"] = _try_cost_line(manifest) or route.get("cost_line") or ""
    return route


def _delivery_snapshot(manifest):
    """Compatibility wrapper for callers that imported the old private gate."""
    return delivery_contract.delivery_snapshot(manifest)


def create_delivery(manifest, manifest_path, path):
    """Create and atomically register the terminal delivery state."""
    path = rm.require_run_output(
        manifest, path, label="delivery_manifest", must_exist=False)
    delivery = delivery_contract.create_delivery(manifest, path)
    record = rm.file_record(path)
    manifest["delivery"] = {
        "status": "verified",
        "path": record["path"],
        "sha256": record["sha256"],
        "delivery_identity": delivery["delivery_identity"],
        "created_at": delivery["created_at"],
    }
    manifest["status"] = "delivered"
    manifest["delivered_at"] = delivery["created_at"]
    rm.save_manifest(manifest, manifest_path)
    return delivery


def verify_delivery(manifest, path):
    return delivery_contract.verify_delivery(manifest, path)


@rm.close_failed_generation("derive", error_code="DERIVE_GENERATION_FAILED")
def derive_and_record(manifest, manifest_path, source_path, target_ratios, out_dir):
    """Run multi-ratio derivation and record results in the manifest.

    This is the pipeline entry point for the "derive" stage. It calls
    derive_ratio.derive_batch (pure ffmpeg, zero model cost) and records
    the derived video artifacts with their sha256 fingerprints.

    Args:
        manifest: the run manifest dict
        manifest_path: path to manifest file (for atomic save)
        source_path: path to the approved final video
        target_ratios: list of ratio strings, e.g. ["16:9", "1:1"]
        out_dir: output directory for derived videos

    Returns:
        The updated manifest's derive generation record.
    """
    if not rm.approval_is_current(manifest, "final"):
        raise ValueError("DERIVE_REQUIRES_FINAL_APPROVAL")
    rm.validate_final_closure(manifest)

    source_record = rm.file_record(source_path)
    final_file = (manifest.get("delivery_qc") or {}).get("file") or {}
    if (not source_record or not source_record.get("exists") or
            source_record.get("path") != final_file.get("path") or
            source_record.get("sha256") != final_file.get("sha256")):
        raise ValueError("DERIVE_SOURCE_MUST_MATCH_APPROVED_FINAL")

    out_dir = rm.require_run_output(
        manifest, out_dir, label="derive_output_dir", must_exist=False)
    target_ratios = list(dict.fromkeys(target_ratios or []))

    if not target_ratios:
        # Skip derive stage — mark as skipped
        rm.invalidate_approvals(manifest, ["derive"])
        gen = manifest.setdefault("generation", {})
        gen["derive"] = {
            "status": "skipped",
            "outputs": [],
            "source": source_record,
            "reason": "no_target_ratios",
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        rm.validate_derive_closure(manifest)
        rm.save_manifest(manifest, manifest_path)
        return gen["derive"]

    ffmpeg, ffprobe = derive_ratio.media_tools.resolve_ffmpeg_pair(allow_fetch=False)
    if not ffmpeg or not ffprobe:
        raise RuntimeError("DERIVE_FFMPEG_UNAVAILABLE")
    source_ratio = derive_ratio._detect_ratio(*derive_ratio._detect_resolution(
        source_path, ffprobe_bin=ffprobe, required=True))
    base_name = os.path.splitext(os.path.basename(source_path))[0]
    planned_outputs = [os.path.abspath(os.path.join(
        out_dir, "%s_%s.mp4" % (base_name, ratio.replace(":", "x"))))
        for ratio in target_ratios if ratio != source_ratio]
    if not planned_outputs:
        rm.invalidate_approvals(manifest, ["derive"])
        gen = manifest.setdefault("generation", {})
        gen["derive"] = {
            "status": "skipped", "outputs": [], "source": source_record,
            "source_ratio": source_ratio, "target_ratios": target_ratios,
            "reason": "targets_match_source_ratio",
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        rm.validate_derive_closure(manifest)
        rm.save_manifest(manifest, manifest_path)
        return gen["derive"]

    rm.mark_generation_started(manifest, "derive")
    manifest["generation"]["derive"].update({
        "source": source_record,
        "source_ratio": source_ratio,
        "target_ratios": target_ratios,
        "planned_output_dir": out_dir,
        "planned_outputs": planned_outputs,
    })
    rm.save_manifest(manifest, manifest_path)
    started_at = manifest["generation"]["derive"]["started_at"]

    result = derive_ratio.derive_batch(source_path, target_ratios, out_dir)

    # Record derived artifacts with fingerprints
    derived_files = []
    qc_files = []
    for item in result.get("derived", []):
        path = item.get("path", "")
        if path:
            derived_files.append(rm.require_run_output(
                manifest, path, label="derived_output", must_exist=True))
        qc_path = item.get("media_qc_path")
        if not qc_path:
            raise ValueError("DERIVE_OUTPUT_QC_OR_IDENTITY_INVALID")
        qc_files.append(rm.require_run_output(
            manifest, qc_path, label="derived_qc", must_exist=True))
        current = rm.file_record(path) if path else None
        qc = item.get("media_qc") or {}
        qc_file = qc.get("file") or {}
        if (not current or current.get("sha256") != item.get("sha256") or
                qc.get("profile") != "formal" or not qc.get("passed") or
                qc_file.get("path") != current.get("path") or
                qc_file.get("sha256") != current.get("sha256") or
                (qc.get("expectations") or {}).get("aspect_ratio") != item.get("ratio") or
                _load(qc_path) != qc):
            raise ValueError("DERIVE_OUTPUT_QC_OR_IDENTITY_INVALID")

    transaction = (manifest.get("generation") or {}).get("derive") or {}
    if (transaction.get("status") != "running" or
            transaction.get("source") != rm.file_record(source_path) or
            not rm.file_record_is_current(transaction.get("source")) or
            transaction.get("target_ratios") != target_ratios or
            transaction.get("planned_output_dir") != out_dir or
            transaction.get("planned_outputs") != derived_files or
            result.get("source_ratio") != source_ratio):
        raise ValueError("DERIVE_RUNNING_TRANSACTION_REQUIRED")
    rm.mark_generation_finished(manifest, "derive", derived_files + qc_files)
    manifest["generation"]["derive"].update({
        "started_at": started_at,
        "source": source_record,
        "source_ratio": source_ratio,
        "target_ratios": target_ratios,
        "planned_output_dir": out_dir,
        "planned_outputs": planned_outputs,
        "derived_details": result.get("derived", []),
    })
    rm.validate_derive_closure(manifest)
    rm.save_manifest(manifest, manifest_path)
    return manifest["generation"]["derive"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="正式制作审批、状态与交付编排")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--manifest", required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--manifest", required=True)
    approve.add_argument("--stage", choices=rm.STAGES, required=True)
    approve.add_argument("--storyboard-result")
    approve.add_argument("--caption-manifest")
    approve.add_argument("--geometry-review")
    approve.add_argument("--test-review",
                         help="test_segment 阶段的正式 accepted review JSON")
    approve.add_argument("--customer-confirmation",
                         help="客户明确确认原话；所有客户决策阶段必填")
    delivery = sub.add_parser("delivery")
    delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    for name in ("create", "verify"):
        command = delivery_sub.add_parser(name)
        command.add_argument("--manifest", required=True)
        command.add_argument("--out" if name == "create" else "--delivery", required=True)
    derive = sub.add_parser("derive", help="多比例派生（纯 ffmpeg，零模型成本）")
    derive.add_argument("--manifest", required=True)
    derive.add_argument("--source", required=True, help="已确认的最终成片路径")
    derive.add_argument("--ratios", nargs="*", default=["16:9", "1:1"],
                       choices=list(derive_ratio.RATIO_DIMS.keys()),
                       help="目标比例列表；显式传 --ratios 且不跟值表示跳过派生")
    derive.add_argument("--out-dir", required=True, help="输出目录")
    args = parser.parse_args(argv)
    manifest_path = args.manifest
    try:
        manifest = rm.load_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR:RUN_MANIFEST_UNAVAILABLE\n下一步：请先用 START_HERE_AGENT.py init 创建 run，或检查 manifest 路径。\n详情：%s" % exc,
              file=sys.stderr)
        return 2
    if args.command == "status":
        result = pipeline_status(manifest)
    elif args.command == "approve":
        approve_stage(manifest, manifest_path, args.stage,
                      storyboard_result=args.storyboard_result,
                      customer_confirmation=args.customer_confirmation,
                      caption_manifest=args.caption_manifest,
                      geometry_review=args.geometry_review,
                      test_review=args.test_review)
        result = pipeline_status(manifest)
    elif args.command == "derive":
        result = derive_and_record(manifest, manifest_path, args.source,
                                   args.ratios, args.out_dir)
        result = pipeline_status(manifest)
    elif args.delivery_command == "create":
        result = create_delivery(manifest, manifest_path, args.out)
    else:
        result = verify_delivery(manifest, args.delivery)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
