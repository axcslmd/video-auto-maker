#!/usr/bin/env python3
"""Formal approval-bound Shotcraft packaging stage."""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import media_qc
import remotion_engine
import render_spec_compiler
import run_manifest as rm
import shotcraft_qc


def _require_running_transaction(manifest, *, spec_path, out_path, quality):
    transaction = (manifest.get("generation") or {}).get("shotcraft_packaging") or {}
    spec_record = rm.file_record(spec_path)
    if (transaction.get("status") != "running" or
            quality != "high" or transaction.get("render_quality") != "high" or
            transaction.get("planned_output") != os.path.abspath(out_path) or
            transaction.get("spec") != spec_record or
            not rm.file_record_is_current(transaction.get("spec"))):
        raise ValueError("SHOTCRAFT_RUNNING_TRANSACTION_REQUIRED")
    return transaction


@rm.close_failed_generation(
    "shotcraft_packaging", error_code="SHOTCRAFT_PACKAGING_FAILED")
def render_and_record(manifest, manifest_path, spec_path, out_path, *, client, quality="high"):
    if quality != "high":
        raise ValueError("FORMAL_SHOTCRAFT_HIGH_QUALITY_REQUIRED")
    rm.identity_gate(manifest, client=client)
    if not manifest.get("requires_shotcraft_packaging"):
        raise ValueError("SHOTCRAFT_PACKAGING_NOT_ENABLED")
    rm.generation_gate(manifest, "shotcraft_packaging", client=client)
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    report = shotcraft_qc.check(spec)
    if not report["passed"]:
        raise ValueError("SHOTCRAFT_QC_FAILED: %s" % ",".join(report["errors"]))
    planned = rm.require_run_output(
        manifest, out_path, label="shotcraft_packaging_output")
    rm.mark_generation_started(manifest, "shotcraft_packaging")
    qc_path = planned + ".qc.json"
    frame_qc_path = planned + ".shotcraft-qc.json"
    manifest["generation"]["shotcraft_packaging"].update({
        "spec": rm.file_record(spec_path),
        "planned_output": planned,
        "planned_qc": qc_path,
        "planned_frame_qc": frame_qc_path,
        "render_quality": quality,
    })
    quality_spec_path = os.path.abspath(out_path) + ".render-spec.json"
    rm.require_run_output(
        manifest, quality_spec_path, label="shotcraft_render_quality_spec")
    quality_spec = render_spec_compiler.compile_spec(
        spec, spec.get("theme") or {}, engine_props=spec,
        composition="Shotcraft", render_profile="direct_delivery", formal=True)
    from project_utils import atomic_write_json
    atomic_write_json(quality_spec_path, quality_spec)
    manifest["generation"]["shotcraft_packaging"].update({
        "render_quality_spec": rm.file_record(quality_spec_path),
        "planned_render_quality_spec": quality_spec_path,
    })
    rm.save_manifest(manifest, manifest_path)
    started_at = manifest["generation"]["shotcraft_packaging"].get("started_at")
    remotion_engine.render_quality_spec(quality_spec_path, out_path)
    _require_running_transaction(
        manifest, spec_path=spec_path, out_path=out_path, quality=quality)
    frame_qc_path = os.path.abspath(out_path) + ".shotcraft-qc.json"
    frames_dir = os.path.abspath(out_path) + ".qa-frames"
    frame_qc = shotcraft_qc.check_rendered_frames(
        out_path, spec, report_path=frame_qc_path, frames_dir=frames_dir)
    if not frame_qc["passed"]:
        raise ValueError("SHOTCRAFT_RENDERED_FRAME_QC_FAILED: %s" %
                         ",".join(frame_qc["errors"]))
    qc_path = os.path.abspath(out_path) + ".qc.json"
    qc = media_qc.check(out_path, profile="formal", expected_ratio=("%s:%s" % (spec["width"], spec["height"])),
                        audio_required=False, report_path=qc_path)
    media_qc.require_pass(qc)
    _require_running_transaction(
        manifest, spec_path=spec_path, out_path=out_path, quality=quality)
    frame_paths = [item["path"] for item in frame_qc.get("frames") or []]
    rm.mark_generation_finished(
        manifest, "shotcraft_packaging",
        [out_path, qc_path, frame_qc_path, quality_spec_path] + frame_paths)
    manifest["generation"]["shotcraft_packaging"].update({
        "started_at": started_at,
        "spec": rm.file_record(spec_path), "qc": qc,
        "shotcraft_frame_qc": frame_qc, "render_quality": quality,
        "planned_output": os.path.abspath(out_path),
        "planned_qc": qc_path, "planned_frame_qc": frame_qc_path,
        "render_quality_spec": rm.file_record(quality_spec_path),
        "planned_render_quality_spec": quality_spec_path})
    rm.save_manifest(manifest, manifest_path)
    return manifest["generation"]["shotcraft_packaging"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render and record formal Shotcraft packaging")
    parser.add_argument("--client", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--quality", default="high", choices=["draft", "standard", "high"])
    args = parser.parse_args(argv)
    manifest = rm.load_manifest(args.manifest)
    result = render_and_record(manifest, args.manifest, args.spec, args.out, client=args.client, quality=args.quality)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
