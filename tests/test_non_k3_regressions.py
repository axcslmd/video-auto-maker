import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import asset_prep
import explainer_render_plan
import explainer_layout_preview
import hf_engine
import identity_plate_workflow
import identity_request_contract
import identity_review_contract
import interaction_contract
import key_setup
import layout_compiler
import motion_design
import dialogue_timing
import render_spec_compiler
import run_manifest
import script_splitter
import storyboard
import storyboard_semantics
import stage_output_contract
import styleframe
import video_engine
import workflow_router


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NonK3RegressionTests(unittest.TestCase):
    def test_splitter_treats_omitted_false_usage_flags_as_unchanged(self):
        rendered = {
            "id": "shot_01", "panel_index": 1,
            "scene_prompt": "clean presenter plate",
        }
        canonical = dict(
            rendered, requires_usage=False, force_usage_reference=False)
        self.assertTrue(
            script_splitter._storyboard_result_shot_matches_source(
                rendered, canonical))
        self.assertFalse(
            script_splitter._storyboard_result_shot_matches_source(
                rendered, dict(canonical, requires_usage=True)))

    def test_splitter_keeps_explicit_deterministic_design_board_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            requested = os.path.join(tmp, "design_board_revision")
            os.makedirs(requested)
            Path(os.path.join(requested, "storyboard_result.json")).write_text(
                json.dumps({
                    "type": "deterministic_design_board_storyboard_result",
                }), encoding="utf-8")
            with mock.patch.object(
                    storyboard, "resolve_current_storyboard_dir") as resolve:
                actual = script_splitter._resolve_current_storyboard_dir(
                    requested, "run-1", {}, storyboard)
            self.assertEqual(actual, os.path.abspath(requested))
            resolve.assert_not_called()

    def test_explainer_render_plan_cli_closes_failed_running_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {"revision": 1, "generation": {}}
            paths = [os.path.join(tmp, name) for name in
                     ("storyboard.json", "execution.json", "director.json")]
            for path in paths:
                Path(path).write_text("{}", encoding="utf-8")
            out = os.path.join(tmp, "render_plan.json")
            preview = os.path.join(tmp, "render_plan.md")
            with mock.patch.object(explainer_render_plan.run_manifest,
                                   "load_manifest", return_value=manifest), \
                    mock.patch.object(explainer_render_plan.run_manifest,
                                      "generation_gate", return_value=True), \
                    mock.patch.object(explainer_render_plan.run_manifest,
                                      "save_manifest"), \
                    mock.patch.object(explainer_render_plan, "build",
                                      side_effect=ValueError("injected failure")), \
                    mock.patch.object(
                        explainer_render_plan.run_manifest,
                        "record_generation_failure_if_running") as closeout:
                with self.assertRaisesRegex(ValueError, "injected failure"):
                    explainer_render_plan.main([
                        "--manifest", os.path.join(tmp, "manifest.json"),
                        "--storyboard-result", paths[0],
                        "--execution-preview", paths[1],
                        "--director-plan", paths[2],
                        "--out", out, "--preview-out", preview,
                    ])
            self.assertEqual(
                manifest["generation"]["render_plan"]["status"], "running")
            closeout.assert_called_once()
            self.assertEqual(closeout.call_args.args[1], "render_plan")

    def test_failed_generation_is_terminal_and_keeps_diagnostics(self):
        manifest = {"generation": {"render_plan": {
            "status": "running", "planned_result": "/tmp/result.json",
            "lease": {"owner": "worker"}}}}
        run_manifest.mark_generation_failed(
            manifest, "render_plan", "builder failed", error_code="BUILD_FAILED")
        generation = manifest["generation"]["render_plan"]
        self.assertEqual(generation["status"], "failed")
        self.assertNotIn("lease", generation)
        self.assertEqual(generation["planned_result"], "/tmp/result.json")
        self.assertEqual(generation["failure"]["error_code"], "BUILD_FAILED")
        self.assertEqual(manifest["status"], "failed_render_plan")

    def test_worker_exception_auto_closes_persisted_running_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "run_manifest.json")
            Path(manifest_path).write_text("{}", encoding="utf-8")
            manifest = {"revision": 7, "generation": {
                "derive": {"status": "running", "planned_outputs": ["x"]}}}

            @run_manifest.close_failed_generation(
                "derive", error_code="DERIVE_GENERATION_FAILED")
            def worker(manifest_path):
                raise RuntimeError("renderer crashed")

            with mock.patch.object(run_manifest, "load_manifest",
                                   return_value=manifest), \
                    mock.patch.object(run_manifest, "save_manifest") as save:
                with self.assertRaisesRegex(RuntimeError, "renderer crashed"):
                    worker(manifest_path)
            self.assertEqual(manifest["generation"]["derive"]["status"], "failed")
            self.assertEqual(manifest["generation"]["derive"]["failure"]["error_code"],
                             "DERIVE_GENERATION_FAILED")
            save.assert_called_once()

    def test_vague_acknowledgement_cannot_approve(self):
        for value in ("继续", "好的", "OK", "下一步"):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "NOT_EXPLICIT"):
                run_manifest.require_explicit_customer_confirmation(value, "storyboard")
        self.assertEqual(
            run_manifest.require_explicit_customer_confirmation(
                "我确认当前版式板并同意定稿", "storyboard"),
            "我确认当前版式板并同意定稿")
        self.assertEqual(
            run_manifest.require_explicit_customer_confirmation(
                "可以了 就这样 进行视频生成", "storyboard"),
            "可以了 就这样 进行视频生成")
        self.assertEqual(
            run_manifest.require_explicit_customer_confirmation(
                "没问题，继续后续的流程", "test_segment"),
            "没问题，继续后续的流程")

    def test_invalidation_archives_but_removes_active_customer_evidence(self):
        manifest = {
            "approvals": {"render_plan": True},
            "approval_hashes": {"render_plan": "hash"},
            "approval_evidence": {"render_plan": {
                "status": "confirmed", "customer_confirmation": "我确认渲染计划"}},
        }
        run_manifest.invalidate_approvals(manifest, ["render_plan"])
        self.assertFalse(manifest["approvals"]["render_plan"])
        self.assertNotIn("render_plan", manifest["approval_evidence"])
        archived = manifest["approval_history"][0]
        self.assertEqual(archived["stage"], "render_plan")
        self.assertEqual(archived["approval_evidence"]["customer_confirmation"],
                         "我确认渲染计划")

    def test_failed_attempt_keeps_actual_request_not_future_retry_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            render_plan_path = os.path.join(tmp, "render_plan.json")
            failure_path = os.path.join(tmp, "failure.json")
            reference = os.path.join(tmp, "cast.jpg")
            Path(reference).write_bytes(b"cast")
            planned = {
                "model": "gpt-image-2", "ratio": "16:9", "resolution": "2k",
                "prompt_zh": "identity", "image_references": [{
                    "transport": "data_url_from_local_file",
                    "source_path": reference, "source_sha256": sha(reference)}],
            }
            plan = {"identity_plate_generation": {"request": {
                "model": "gpt-image-2", "source_fingerprint": "source-only",
                "planned_submission_request_spec": planned,
                "planned_submission_request_fingerprint":
                    explainer_render_plan._json_sha256(planned),
            }}}
            Path(render_plan_path).write_text(json.dumps(plan), encoding="utf-8")
            manifest = {"client": "acme", "run_id": "run-1", "revision": 1,
                        "tasks": []}
            submitted = dict(planned, resolution="1080p")
            with mock.patch.object(explainer_render_plan.run_manifest,
                                   "load_manifest", return_value=manifest), \
                    mock.patch.object(explainer_render_plan,
                                      "_current_render_plan",
                                      return_value=(plan, run_manifest.file_record(
                                          render_plan_path))), \
                    mock.patch.object(explainer_render_plan.run_manifest,
                                      "save_manifest"):
                result = explainer_render_plan.record_identity_plate_failure(
                    "manifest.json", failure_path, task_id="task-1", error="unsupported",
                    submitted_request_spec=submitted,
                    submitted_render_plan=run_manifest.file_record(render_plan_path),
                    submitted_request_fingerprint=explainer_render_plan._json_sha256(
                        submitted))
            self.assertEqual(result["failure"]["submitted_request_spec"]["resolution"],
                             "1080p")
            self.assertEqual(result["failure"]["planned_request_spec"]["resolution"],
                             "2k")
            self.assertNotEqual(result["failure"]["submitted_request_fingerprint"],
                                result["failure"]["planned_request_fingerprint"])
            self.assertEqual(result["task"]["request_spec"]["resolution"], "1080p")

    def test_dialogue_binding_is_complete_hash_bound_and_tamper_evident(self):
        plan = {"shots": [
            {"id": "s1", "duration": 4},
            {"id": "s2", "duration": 4}]}
        director = {"segments": [
            {"id": "s1", "dialogue": "第一段已审批台词"},
            {"id": "s2", "dialogue": "第二段已审批台词"},
        ]}
        binding = motion_design.build_dialogue_binding(
            plan, director, plan_sha256="a" * 64,
            director_plan_sha256="b" * 64)
        indexed = motion_design.validate_dialogue_binding(
            plan, binding, plan_sha256="a" * 64)
        self.assertEqual(indexed["s2"]["dialogue"], "第二段已审批台词")
        tampered = json.loads(json.dumps(binding))
        tampered["items"][0]["dialogue"] = "被修改"
        with self.assertRaisesRegex(ValueError, "AGGREGATE_HASH_MISMATCH"):
            motion_design.validate_dialogue_binding(plan, tampered)

    def test_dialogue_overflow_fails_before_motion_design_and_proposes_fixed_total(self):
        durations = [8, 12, 10, 10, 12, 8]
        plan = {"shots": [
            {"id": "s%d" % index, "duration": duration}
            for index, duration in enumerate(durations, 1)]}
        director = {"segments": [{
            "id": "s%d" % index, "duration": duration,
            "dialogue": (
                "确" * 58 if index == 2 else
                "认" * 42 if index == 6 else "已确认短台词"),
        } for index, duration in enumerate(durations, 1)]}
        proposal = dialogue_timing.propose_reallocation(
            plan["shots"], {item["id"]: item
                            for item in director["segments"]})
        self.assertTrue(proposal["feasible"])
        self.assertEqual(proposal["total_duration_seconds"], 60)
        self.assertTrue(proposal["dialogue_unchanged"])
        self.assertTrue(
            proposal["requires_execution_previs_rebuild_and_customer_reapproval"])
        with self.assertRaisesRegex(
                ValueError, "MOTION_DESIGN_DIALOGUE_DURATION_OVERFLOW.*proposal"):
            motion_design.build_dialogue_binding(plan, director)

    def test_formal_avatar_split_uses_only_approved_identity_and_bound_dialogue(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            identity = os.path.join(tmp, "identity_plate.png")
            cast = os.path.join(tmp, "cast_contact_sheet.png")
            Image.new("RGB", (1280, 720), "white").save(identity)
            Image.new("RGB", (1280, 720), "gray").save(cast)
            plan = {
                "client": "demo", "run_id": "run-1", "title": "Demo",
                "scene_type": "document_explainer",
                "storyboard_role": "design_board",
                "storyboard_aspect_ratio": "16:9", "output_ratio": "16:9",
                "asset_refs": {
                    "cast_boards": [cast],
                    "digital_human_portraits": [cast],
                },
                "shots": [{
                    "id": "s1", "panel_index": 1, "duration": 4,
                    "visual": "讲师中景", "characters": ["host"],
                    "scene_prompt": "深蓝科技演播室背景",
                    "ref_tags": ["@host"],
                }],
            }
            timing = dialogue_timing.build_contract([{
                "id": "s1", "duration": 4, "dialogue": "这是已确认台词"}])
            render_plan = {
                "kind": "document_explainer_render_plan",
                "client": "demo", "run_id": "run-1", "ratio": "16:9",
                "identity_plate_generation": {"required_before_video": True},
                "dialogue_timing_contract": timing,
                "voice_continuity_contract": {"voice_profile": "专业女声"},
                "segments": [{
                    "id": "s1", "duration_seconds": 4,
                    "dialogue": "这是已确认台词",
                    "dialogue_timing": timing["items"][0],
                    "submission_prompt_zh":
                        "干净单人讲师底片。口播原文：这是已确认台词。禁止PPT字幕Logo。",
                    "requires_identity_plate": True,
                    "video_model_usage": "avatar_plate_only",
                    "storyboard_ref": False,
                }],
            }
            render_path = os.path.join(tmp, "render_plan.json")
            Path(render_path).write_text(
                json.dumps(render_plan, ensure_ascii=False), encoding="utf-8")
            identity_record = run_manifest.file_record(identity)
            remote_url = "https://example.com/retrieve/identity-plate.png"
            identity_result_path = os.path.join(
                tmp, "identity_plate_generation_result.json")
            Path(identity_result_path).write_text(json.dumps({
                "kind": "identity_plate_generation_result",
                "status": "pending_visual_review",
                "client": "demo", "run_id": "run-1",
                "candidate": identity_record,
                "remote_url": remote_url,
            }, ensure_ascii=False), encoding="utf-8")
            identity_artifact = {
                "output": identity_record,
                "remote_url": remote_url,
                "generation_result": run_manifest.file_record(
                    identity_result_path),
            }
            result = script_splitter.split(
                plan, client="demo", run_id="run-1", allow_unconfirmed=True,
                render_plan_artifact=run_manifest.file_record(render_path),
                identity_plate_artifact=identity_artifact)
            self.assertEqual(len(result["segments"]), 1)
            segment = result["segments"][0]
            self.assertEqual(segment["dialogue"], "这是已确认台词")
            self.assertEqual(segment["urls"], [remote_url])
            self.assertNotIn(cast, segment["urls"])
            self.assertEqual(
                segment["references"][0]["source"],
                "identity_plate.approved_candidate")
            self.assertTrue(segment["references"][0]["background_anchor"])
            self.assertEqual(
                segment["required_reference_types"], ["character_identity"])
            self.assertEqual(
                segment["background_identity_lock"]["reference_urls"],
                [remote_url])
            self.assertTrue(
                segment["background_identity_lock"]["requires_reference"])
            self.assertEqual(
                segment["identity_plate_artifact"]["approved_local_artifact"],
                identity_record)
            self.assertFalse(segment["storyboard_ref"])
            self.assertEqual(segment["video_type"], 4)
            self.assertEqual(
                segment["video_handoff_fingerprint"],
                script_splitter.build_video_handoff(segment)["fingerprint"])

            overflow = "超" * 40
            overflow_timing = dialogue_timing.build_contract([{
                "id": "s1", "duration": 4, "dialogue": overflow}])
            render_plan["dialogue_timing_contract"] = overflow_timing
            render_plan["segments"][0].update({
                "dialogue": overflow,
                "dialogue_timing": overflow_timing["items"][0],
            })
            Path(render_path).write_text(
                json.dumps(render_plan, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "FORMAL_AVATAR_DIALOGUE_DURATION_OVERFLOW"):
                script_splitter.split(
                    plan, client="demo", run_id="run-1",
                    allow_unconfirmed=True,
                    render_plan_artifact=run_manifest.file_record(render_path),
                    identity_plate_artifact=identity_artifact)

    def test_generic_finish_cannot_skip_committed_running_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "content.json")
            Path(output).write_text("{}", encoding="utf-8")
            manifest = {"generation": {}}
            with self.assertRaisesRegex(
                    ValueError, "GENERATION_FINISH_REQUIRES_RUNNING"):
                run_manifest.mark_generation_finished(
                    manifest, "content_scaffold", [output])

    def test_video_privacy_block_closes_running_lease_and_routes_to_recompile(self):
        manifest = {
            "status": "generating_video",
            "generation": {"video": {
                "status": "running", "started_at": "2026-08-19T20:00:00",
                "lease": {"owner_token": "worker-1", "expires_at_epoch": 9999999999},
            }},
        }
        run_manifest.mark_generation_blocked(
            manifest, "video", "provider rejected real-person reference",
            blocker_code="PRIVACY_FALLBACK_REAPPROVAL_REQUIRED",
            recovery_action="重新编译合同并重新确认",
            evidence={"task_id": "task-1", "billing_status": "unknown"})
        generation = manifest["generation"]["video"]
        self.assertEqual(generation["status"], "blocked")
        self.assertNotIn("lease", generation)
        self.assertEqual(manifest["status"], "blocked_video")
        self.assertEqual(generation["blocker"]["evidence"]["task_id"],
                         "task-1")
        with mock.patch.object(run_manifest, "next_required_stage",
                               return_value="video"):
            route = workflow_router._manifest_route(manifest)
        self.assertEqual(route["next_action"],
                         "recompile_privacy_fallback_contract")
        self.assertIn("不需要单格展开图", route["customer_message"])

    def test_video_settlement_routes_success_to_take_review_and_clears_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = os.path.join(tmp, "video_results.json")
            results = [
                {"ok": True, "segment_id": "s1", "taskId": "task-1"},
                {"ok": True, "segment_id": "s2", "taskId": "task-2"},
            ]
            Path(results_path).write_text(
                json.dumps(results), encoding="utf-8")
            manifest = {
                "status": "generating_video",
                "generation": {"video": {
                    "status": "running", "lease": {"owner_token": "worker"},
                }},
                "handoffs": {"video": {"segments": {"s1": "a", "s2": "b"}}},
            }
            run_manifest.settle_video_generation(
                manifest, results_path, results)
        generation = manifest["generation"]["video"]
        self.assertEqual(generation["status"], "awaiting_take_review")
        self.assertNotIn("lease", generation)
        self.assertEqual(manifest["status"], "needs_video_take_review")
        self.assertEqual(generation["settlement"]["summary"]["succeeded"], 2)
        with mock.patch.object(run_manifest, "next_required_stage",
                               return_value="video"):
            route = workflow_router._manifest_route(manifest)
        self.assertEqual(route["next_action"], "review_video_takes")

    def test_video_settlement_preserves_failed_task_and_routes_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = os.path.join(tmp, "video_results.json")
            results = [{
                "ok": False, "segment_id": "s1", "taskId": "task-1",
                "error": "DOWNLOAD_PENDING", "resume_available": True,
            }]
            Path(results_path).write_text(
                json.dumps(results), encoding="utf-8")
            manifest = {
                "status": "generating_video",
                "generation": {"video": {
                    "status": "running", "lease": {"owner_token": "worker"},
                }},
                "handoffs": {"video": {"segments": {"s1": "a"}}},
            }
            run_manifest.settle_video_generation(
                manifest, results_path, results)
        generation = manifest["generation"]["video"]
        self.assertEqual(generation["status"], "recovery_required")
        self.assertEqual(
            generation["recovery"]["resumable_segment_ids"], ["s1"])
        self.assertEqual(generation["settlement"]["summary"]["task_ids"],
                         ["task-1"])
        self.assertEqual(manifest["status"], "needs_video_recovery")
        with mock.patch.object(run_manifest, "next_required_stage",
                               return_value="video"):
            route = workflow_router._manifest_route(manifest)
        self.assertEqual(route["next_action"], "recover_video_tasks")

    def test_video_recovery_restart_archives_settlement_and_remote_task_evidence(self):
        manifest = {
            "revision": 12,
            "status": "needs_video_recovery",
            "generation": {"video": {
                "status": "recovery_required",
                "settlement": {
                    "fingerprint": "settlement-fingerprint",
                    "summary": {"task_ids": ["task-remote-1"]},
                },
                "recovery": {
                    "failed_segment_ids": ["s1"],
                    "resumable_segment_ids": ["s1"],
                    "billing_status": "unknown",
                },
            }},
        }
        with mock.patch.object(run_manifest, "generation_gate",
                               return_value=True):
            run_manifest.mark_generation_started(manifest, "video")
        history = manifest["generation_history"]["video"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["record"]["status"], "recovery_required")
        self.assertEqual(
            history[0]["record"]["settlement"]["summary"]["task_ids"],
            ["task-remote-1"])
        self.assertEqual(
            history[0]["record"]["recovery"]["billing_status"], "unknown")
        self.assertEqual(manifest["generation"]["video"]["status"], "running")

    def test_authorized_video_attempt_archives_prior_settlement_before_restart(self):
        manifest = {
            "revision": 19,
            "handoffs": {"video": {"segments": {"s1": "handoff"}}},
            "accepted_takes": {"s1": {"take_fingerprint": "take-old"}},
            "generation": {"video": {
                "status": "awaiting_take_review",
                "settlement": {
                    "fingerprint": "settlement-old",
                    "summary": {"task_ids": ["task-old"]},
                },
            }},
        }
        run_manifest.request_video_attempt(
            manifest, "s1", actor="customer", reason="客户拒绝旧 take")
        with mock.patch.object(run_manifest, "generation_gate",
                               return_value=True):
            run_manifest.mark_generation_started(manifest, "video")
        archived = manifest["generation_history"]["video"][0]
        self.assertEqual(archived["record"]["status"], "authorized_retry")
        self.assertEqual(
            archived["record"]["settlement"]["summary"]["task_ids"],
            ["task-old"])
        self.assertEqual(archived["record"]["authorized_retry_segment"], "s1")

    def test_generation_history_deduplicates_the_same_transaction_snapshot(self):
        manifest = {
            "revision": 7,
            "generation": {"video": {
                "status": "recovery_required",
                "settlement": {"fingerprint": "same"},
            }},
        }
        first = run_manifest._archive_generation_attempt(
            manifest, "video", superseded_by="running")
        second = run_manifest._archive_generation_attempt(
            manifest, "video", superseded_by="running")
        self.assertEqual(first["transaction_fingerprint"],
                         second["transaction_fingerprint"])
        self.assertEqual(len(manifest["generation_history"]["video"]), 1)

    def test_generation_history_tampering_is_rejected(self):
        manifest = {
            "generation": {"video": {
                "status": "recovery_required",
                "settlement": {"fingerprint": "original"},
            }},
        }
        archived = run_manifest._archive_generation_attempt(
            manifest, "video", superseded_by="running")
        self.assertTrue(run_manifest._validate_generation_history(manifest))
        archived["record"]["settlement"]["fingerprint"] = "tampered"
        with self.assertRaisesRegex(ValueError, "GENERATION_HISTORY_TAMPERED"):
            run_manifest._validate_generation_history(manifest)

    def test_video_settlement_rejects_partial_batch_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = os.path.join(tmp, "video_results.json")
            results = [{"ok": True, "segment_id": "s1", "taskId": "task-1"}]
            Path(results_path).write_text(
                json.dumps(results), encoding="utf-8")
            manifest = {
                "generation": {"video": {"status": "running"}},
                "handoffs": {"video": {"segments": {"s1": "a", "s2": "b"}}},
            }
            with self.assertRaisesRegex(
                    ValueError, "VIDEO_SETTLEMENT_SEGMENT_COVERAGE_MISMATCH"):
                run_manifest.settle_video_generation(
                    manifest, results_path, results)

    def test_audio_canary_rejects_a_different_full_batch_provider_route(self):
        manifest = {
            "requires_audio": True,
            "requires_test_segment": True,
            "test_segment_artifact": {"model_used": "seedance-exact-route"},
        }
        matching = {"segments": [{
            "segment_id": "s1", "model": "seedance-exact-route",
            "integrated_audio": True, "audio_required": True,
        }, {
            "segment_id": "s2", "model": "seedance-exact-route",
            "integrated_audio": True, "audio_required": True,
        }]}
        changed = {"segments": [dict(
            matching["segments"][0], model="different-route"),
            matching["segments"][1]]}
        with mock.patch.object(run_manifest, "approval_is_current",
                               return_value=True), \
                mock.patch.object(run_manifest,
                                  "validate_test_segment_closure",
                                  return_value=True):
            self.assertTrue(run_manifest.validate_video_audio_canary(
                manifest, matching))
            with self.assertRaisesRegex(
                    ValueError, "VIDEO_AUDIO_CANARY_MODEL_MISMATCH"):
                run_manifest.validate_video_audio_canary(manifest, changed)

    def test_audio_canary_cannot_be_recorded_without_a_real_audio_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_path = os.path.join(tmp, "test-results.json")
            video_path = os.path.join(tmp, "test.mp4")
            Path(results_path).write_text("[]", encoding="utf-8")
            Path(video_path).write_bytes(b"video")
            manifest = {
                "requires_audio": True, "requires_test_segment": True,
                "handoffs": {"video": {"segments": {"s1": "handoff"}}},
            }
            result = {
                "ok": True, "segment_id": "s1", "localPath": video_path,
                "take_fingerprint": "take", "model": "3.0-Omni",
                "video_handoff_fingerprint": "handoff",
                "media_qc": {
                    "passed": True,
                    "media": {"audio": {"stream_count": 0}},
                    "checks": {"audio_track": {"passed": True}},
                },
            }
            with self.assertRaisesRegex(
                    ValueError, "TEST_SEGMENT_EMBEDDED_AUDIO_PROOF_REQUIRED"):
                run_manifest.record_test_segment_finished(
                    manifest, results_path, [result])

    def test_approved_canary_task_is_reused_by_full_video_batch(self):
        manifest = {"tasks": [{
            "stage": "video", "unit_id": "s1",
            "handoff_fingerprint": "handoff", "attempt": 1,
            "model": "seedance-exact-route", "status": "succeeded",
            "task_id": "paid-canary-task",
            "media_qc_passed": True,
        }]}
        completed = video_engine._completed_task(
            manifest, "s1", "handoff", model="seedance-exact-route")
        self.assertEqual(completed["task_id"], "paid-canary-task")

    def test_video_status_and_contract_share_transaction_action(self):
        manifest = {
            "chain_profile": {"profile": "oral_broadcast"},
            "generation": {"video": {"status": "authorized_retry"}},
        }
        with mock.patch.object(run_manifest, "next_required_stage",
                               return_value="video"), \
                mock.patch.object(run_manifest, "ensure_workflow_contract",
                                  side_effect=lambda value: value):
            status = workflow_router._manifest_route(manifest)
            contract = workflow_router.stage_contract(
                {"manifest": manifest}, "video")
        self.assertEqual(status["next_action"], "start_video")
        self.assertEqual(contract["next_action"], status["next_action"])
        self.assertEqual(contract["next_message"], status["customer_message"])
        self.assertEqual(contract["preview_sources"], [])

        manifest["generation"]["video"]["status"] = \
            "generated_pending_take_review"
        with mock.patch.object(run_manifest, "next_required_stage",
                               return_value="video"), \
                mock.patch.object(run_manifest, "ensure_workflow_contract",
                                  side_effect=lambda value: value):
            status = workflow_router._manifest_route(manifest)
            contract = workflow_router.stage_contract(
                {"manifest": manifest}, "video")
        self.assertEqual(status["next_action"], "review_video_takes")
        self.assertEqual(contract["next_action"], status["next_action"])

    def test_frozen_document_profile_ignores_product_words_in_task_history(self):
        manifest = {
            "chain_profile": {"profile": "document_explainer"},
            "tasks": [{"source_segment": {
                "product_facts": ["产品演示步骤与服务方案说明"],
            }}],
        }
        variant = workflow_router._infer_route_variant(
            manifest=manifest,
            chain_profile=manifest["chain_profile"])
        self.assertEqual(variant["name"], "explainer_video")
        self.assertEqual(variant["entry_command"], "/explainer-video")
        self.assertEqual(variant["reason"], "frozen_chain_profile")

    def test_route_duration_uses_numeric_ranges_not_digit_substrings(self):
        six_seconds = workflow_router._infer_route_variant(
            brief={"duration": "6 秒"})
        sixty_seconds = workflow_router._infer_route_variant(
            brief={"duration": "60s"})
        fifteen_seconds = workflow_router._infer_route_variant(
            brief={"duration": 15})
        one_minute = workflow_router._infer_route_variant(
            brief={"duration": "1分钟"})
        self.assertEqual(six_seconds["name"], "quick_promo")
        self.assertEqual(sixty_seconds["name"], "product_demo")
        self.assertEqual(fifteen_seconds["name"], "generic_video")
        self.assertEqual(one_minute["name"], "product_demo")

    def test_motion_finish_requires_separate_dialogue_binding_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "storyboard_plan.json")
            design_path = os.path.join(tmp, "motion_design.json")
            binding_path = os.path.join(tmp, "motion_dialogue_binding.json")
            director_path = os.path.join(tmp, "director_plan.json")
            plan = {"shots": [{"id": "s1", "duration": 4}]}
            director = {
                "segments": [{"id": "s1", "dialogue": "已确认台词"}]}
            Path(plan_path).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            Path(director_path).write_text(
                json.dumps(director, ensure_ascii=False), encoding="utf-8")
            binding = motion_design.build_dialogue_binding(
                plan, director, plan_sha256=sha(plan_path),
                director_plan_sha256=sha(director_path))
            design = {
                "schema_version": 3, "global_style": {},
                "shots": [{"shot_id": "s1"}], "dialogue_contract": binding,
            }
            Path(design_path).write_text(
                json.dumps(design, ensure_ascii=False), encoding="utf-8")
            Path(binding_path).write_text(
                json.dumps(binding, ensure_ascii=False), encoding="utf-8")
            manifest = {
                "storyboard_plan": run_manifest.file_record(plan_path),
                "chain_profile": {"profile": "generic_video"},
                "generation": {
                    "script": {"status": "approved", "artifacts": [
                        run_manifest.file_record(plan_path),
                        run_manifest.file_record(director_path),
                    ]},
                    "motion_design": {"status": "running"},
                },
            }
            with self.assertRaisesRegex(
                    ValueError, "DIALOGUE_BINDING_ARTIFACT_REQUIRED"):
                run_manifest.mark_generation_finished(
                    manifest, "motion_design", [design_path])
            run_manifest.mark_generation_finished(
                manifest, "motion_design", [design_path, binding_path])
            generation = manifest["generation"]["motion_design"]
            self.assertEqual(generation["status"], "pending_approval")
            self.assertEqual(
                generation["output_contract"]["dialogue_binding"]["sha256"],
                sha(binding_path))

    def test_document_motion_finish_requires_bound_execution_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "storyboard_plan.json")
            design_path = os.path.join(tmp, "motion_design.json")
            execution_path = os.path.join(tmp, "storyboard_execution_plan.json")
            plan = {
                "storyboard_role": "design_board",
                "shots": [{"id": "s1", "duration": 4, "dialogue": "正文"}],
            }
            Path(plan_path).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            authored_binding = motion_design.build_authored_dialogue_binding(
                plan, plan_sha256=sha(plan_path))
            design = {
                "schema_version": 3, "global_style": {},
                "shots": [{"shot_id": "s1"}],
                "dialogue_contract": authored_binding,
            }
            Path(design_path).write_text(
                json.dumps(design, ensure_ascii=False), encoding="utf-8")
            plan_record = run_manifest.file_record(plan_path)
            design_record = run_manifest.file_record(design_path)
            execution = json.loads(json.dumps(plan))
            execution["shots"][0]["motion_plan"] = {}
            execution["_execution_plan"] = {
                "kind": "motion_enriched_storyboard_plan", "version": 1,
                "source_plan": plan_record, "motion_design": design_record,
            }
            Path(execution_path).write_text(
                json.dumps(execution, ensure_ascii=False), encoding="utf-8")
            manifest = {
                "storyboard_plan": plan_record,
                "chain_profile": {"profile": "document_explainer"},
                "generation": {
                    "script": {"status": "approved", "artifacts": [plan_record]},
                    "motion_design": {"status": "running"},
                },
            }
            with self.assertRaisesRegex(
                    ValueError, "EXECUTION_PLAN_ARTIFACT_REQUIRED"):
                run_manifest.mark_generation_finished(
                    manifest, "motion_design", [design_path])
            run_manifest.mark_generation_finished(
                manifest, "motion_design", [design_path, execution_path])
            self.assertEqual(
                manifest["generation"]["motion_design"]["output_contract"]
                ["execution_plan"]["sha256"], sha(execution_path))

    def test_motion_binding_rejects_unapproved_director_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "plan.json")
            plan = {"shots": [{"id": "s1", "duration": 3}]}
            Path(plan_path).write_text(json.dumps(plan), encoding="utf-8")
            plan_record = run_manifest.file_record(plan_path)
            binding = motion_design.build_dialogue_binding(
                plan, {"segments": [{"id": "s1", "dialogue": "台词"}]},
                plan_sha256=plan_record["sha256"],
                director_plan_sha256="d" * 64)
            manifest = {"generation": {"script": {
                "status": "approved", "artifacts": [plan_record]}}}
            with self.assertRaisesRegex(
                    ValueError, "DIRECTOR_PLAN_NOT_SCRIPT_ARTIFACT"):
                stage_output_contract._validate_dialogue_binding(
                    manifest, plan, plan_record, binding)

    def test_motion_design_is_a_customer_evidence_stage(self):
        self.assertIn(
            "motion_design", run_manifest.GENERIC_CUSTOMER_EVIDENCE_STAGES)
        confirm = workflow_router._STAGE_FLOW["motion_design"]["confirm_function"]
        self.assertEqual(confirm["module"], "pipeline")
        self.assertIn("--customer-confirmation", confirm["cli"])

    def test_missing_dialogue_requires_binding_before_any_model_design(self):
        plan = {"output_ratio": "16:9", "shots": [
            {"id": "s1", "duration": 3, "visual": "clean"}]}
        with self.assertRaisesRegex(ValueError, "DIALOGUE_BINDING_REQUIRED"), \
                mock.patch.object(motion_design, "_llm_design") as paid_call:
            motion_design.design_from_plan(
                plan, api_key="test", require_llm=True)
        paid_call.assert_not_called()

    def test_image_catalog_blocks_bad_ratio_before_create_and_normalizes_legacy_resolution(self):
        specs = [{"id": "gpt-image-2", "ratios": ["16:9"],
                  "resolutions": ["1k", "2k"]}]
        self.assertEqual(asset_prep._validated_image_request_spec(
            "gpt-image-2", "16:9", "1080p", model_specs=specs),
            ("gpt-image-2", "16:9", "2k"))
        with self.assertRaisesRegex(ValueError, "RATIO_UNSUPPORTED_BEFORE_CREATE"):
            asset_prep._validated_image_request_spec(
                "gpt-image-2", "9:16", "2k", model_specs=specs)

    def test_fresh_asset_subprocess_resolves_public_session_before_key(self):
        calls = []
        with mock.patch.object(key_setup, "ensure_session_id",
                               side_effect=lambda: calls.append("session")), \
                mock.patch.object(key_setup, "load_key",
                                  side_effect=lambda: calls.append("key") or "secret"):
            self.assertEqual(asset_prep._session_api_key(), "secret")
        self.assertEqual(calls, ["session", "key"])

    def test_required_key_resolves_public_session_before_exact_cache_read(self):
        calls = []
        with mock.patch.object(
                key_setup, "ensure_session_id",
                side_effect=lambda: calls.append("session") or "br-session-current"), \
                mock.patch.object(
                    key_setup, "load_key",
                    side_effect=lambda sid: calls.append(("key", sid)) or "secret"), \
                mock.patch.object(key_setup, "validation_status",
                                  return_value="VALIDATED"):
            self.assertEqual(key_setup.require_key("UNIT_KEY_REQUIRED"), "secret")
        self.assertEqual(calls, ["session", ("key", "br-session-current")])

    def test_missing_required_key_is_a_non_secret_structured_block(self):
        with mock.patch.object(key_setup, "ensure_session_id",
                               return_value="br-session-current"), \
                mock.patch.object(key_setup, "load_key", return_value=None):
            with self.assertRaisesRegex(
                    key_setup.SessionKeyRequired, "UNIT_KEY_REQUIRED"):
                key_setup.require_key("UNIT_KEY_REQUIRED")
        message = key_setup.blocked_message()
        self.assertIn("当前任务会话", message)
        self.assertIn("不会从日志", message)
        self.assertNotIn("br-session-current", message)

    def test_stored_key_is_authenticated_and_bound_to_short_lived_marker(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-auth-test",
                }, clear=True), \
                mock.patch.object(key_setup, "_validate", return_value=(True, "ok")):
            key_setup.save_key("sk-valid-test")
            self.assertEqual(key_setup.require_key(), "sk-valid-test")
            marker = key_setup._read_validation_marker()
            self.assertEqual(marker["status"], "validated")
            self.assertNotIn("sk-valid-test", json.dumps(marker))
            self.assertEqual(
                key_setup.validation_status("sk-valid-test"), "VALIDATED")

    def test_explicit_401_revokes_only_the_current_invalid_key(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-invalid-test",
                }, clear=True), \
                mock.patch.object(
                    key_setup, "_validate",
                    return_value=(False, "HTTP 401 Invalid API Key")):
            key_setup.save_key("sk-invalid-test")
            with self.assertRaises(key_setup.SessionKeyRequired) as raised:
                key_setup.require_key()
            self.assertIn("旧授权已安全撤销", raised.exception.client_message)
            self.assertIsNone(key_setup.load_key())

    def test_transient_validation_failure_blocks_but_preserves_key(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-network-test",
                }, clear=True), \
                mock.patch.object(
                    key_setup, "_validate",
                    return_value=(False, "network timeout")):
            key_setup.save_key("sk-network-test")
            with self.assertRaises(key_setup.SessionKeyUnverified):
                key_setup.require_key()
            self.assertEqual(key_setup.load_key(), "sk-network-test")
            self.assertEqual(
                key_setup.validation_status("sk-network-test"),
                "STORED_UNVERIFIED")

    def test_common_http_client_revokes_exact_session_key_on_401(self):
        response = io.BytesIO(b'{"message":"Invalid API Key"}')
        rejection = urllib.error.HTTPError(
            "https://api.basicrouter.ai/api/v1/image-models", 401,
            "Unauthorized", {}, response)
        handler = mock.Mock()
        with mock.patch.object(
                key_setup.br_client, "_AUTH_REJECTION_HANDLER", handler), \
                mock.patch.object(
                    key_setup.br_client.urllib.request, "urlopen",
                    side_effect=rejection):
            with self.assertRaises(key_setup.br_client.BRError):
                key_setup.br_client._request(
                    "GET", "/v1/image-models", api_key="sk-old",
                    max_retries=0)
        handler.assert_called_once_with("sk-old")

    def test_common_http_client_does_not_revoke_key_for_policy_403(self):
        response = io.BytesIO(b'{"message":"content safety policy"}')
        rejection = urllib.error.HTTPError(
            "https://api.basicrouter.ai/api/v1/image-generations", 403,
            "Forbidden", {}, response)
        handler = mock.Mock()
        with mock.patch.object(
                key_setup.br_client, "_AUTH_REJECTION_HANDLER", handler), \
                mock.patch.object(
                    key_setup.br_client.urllib.request, "urlopen",
                    side_effect=rejection):
            with self.assertRaises(key_setup.br_client.BRError):
                key_setup.br_client._request(
                    "POST", "/v1/image-generations", api_key="sk-valid",
                    body={"model": "gpt-image-2"}, max_retries=0)
        handler.assert_not_called()

    def test_public_session_and_key_survive_two_isolated_subprocesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "session-id")
            env = dict(os.environ)
            env.update({"HOME": tmp, "BASICROUTER_SESSION_STATE_FILE": state})
            for name in (
                    "BASICROUTER_SESSION_ID", "BASICROUTER_HOST_SESSION_ID",
                    "KILO_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
                    "HERMES_SESSION_ID", "WORKBUDDY_SESSION_ID",
                    "WORKBUDDY_TASK_ID"):
                env.pop(name, None)
            first = subprocess.run([
                sys.executable, "-c",
                "import sys;sys.path.insert(0,'scripts');import key_setup;"
                "s=key_setup.ensure_session_id();key_setup.save_key('sk-test-only');print(s)",
            ], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
            second = subprocess.run([
                sys.executable, "-c",
                "import sys,hashlib;sys.path.insert(0,'scripts');import asset_prep;"
                "print(hashlib.sha256(asset_prep._session_api_key().encode()).hexdigest())",
            ], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
            self.assertTrue(first.stdout.strip().startswith("br-session-"))
            self.assertEqual(
                second.stdout.strip(), hashlib.sha256(b"sk-test-only").hexdigest())

    def test_layout_preview_html_accepts_css_percentages(self):
        payload = explainer_layout_preview.build_preview_payload(
            {"title": "百分比 CSS 回归"})
        html = explainer_layout_preview.render_html(payload, "preview.svg")
        self.assertIn("width: 100%", html)
        self.assertIn("preview.svg", html)

    def test_cjk_subtitle_break_obeys_line_head_and_phrase_integrity(self):
        value = "先确认关键素材、形象和脚本再进入下一步分镜"
        balanced = hf_engine._balance_subtitle_lines(
            value, width_px=430, size_px=52, max_lines=2)
        self.assertIn("\n", balanced)
        _, right = balanced.split("\n", 1)
        self.assertNotIn(right[:1], set("、，。；：！？）】》〉」』"))
        self.assertNotIn("分\n镜", balanced)
        self.assertNotIn("关键\n素材", balanced)

    def test_stacked_layout_is_native_three_band_and_capacity_is_preflighted(self):
        tokens = styleframe.build_tokens(direction="clean_corporate")
        plan = {"output_ratio": "16:9", "shots": [{
            "id": "s1", "duration": 3, "title": "三段原生布局",
            "visual_mode": "stacked_top_bottom", "human_slot": "presenter",
        }]}
        contract = layout_compiler.compile_layouts(
            plan, tokens=tokens, strict=True)
        selected = contract["shots"][0]["layouts"]["16:9"]
        self.assertEqual(selected["layout_family"], "native_stacked_three_band")
        regions = selected["regions"]
        self.assertLess(regions["primary"]["y"] + regions["primary"]["h"],
                        regions["subtitle_reserved"]["y"])
        self.assertLess(regions["subtitle_reserved"]["y"] +
                        regions["subtitle_reserved"]["h"], regions["secondary"]["y"])
        self.assertEqual(selected["content_capacity"]["fit_policy"],
                         "recompose_or_split_never_auto_shrink")

    def test_layout_contract_is_injected_into_actual_renderer_scene(self):
        tokens = styleframe.build_tokens(direction="clean_corporate")
        plan = {"output_ratio": "16:9", "fps": 30, "shots": [{
            "id": "s1", "duration": 3, "title": "原生排版",
            "visual_mode": "stacked_top_bottom",
        }]}
        props = {"width": 1920, "height": 1080, "fps": 30,
                 "scenes": [{"id": "s1", "durationInFrames": 90,
                              "visualMode": "stacked_top_bottom"}]}
        spec = render_spec_compiler.compile_spec(
            plan, tokens, engine_props=props, formal=True)
        scene = spec["engine_props"]["scenes"][0]
        self.assertEqual(scene["layoutFamily"], "native_stacked_three_band")
        self.assertIn("subtitle_reserved", scene["layoutRegions"])
        self.assertEqual(scene["contentCapacity"]["status"], "fits")

    def test_customer_preview_excludes_internal_contracts_and_alpha_masters(self):
        paths = [
            "/tmp/layout_animatic.mp4", "/tmp/01_s1.png",
            "/tmp/execution_preview_result.json", "/tmp/motion_master.mov",
            "/tmp/commercial_qc.json", "/tmp/render_plan_preview.md",
        ]
        self.assertEqual(workflow_router._customer_preview_paths(paths), [
            "/tmp/layout_animatic.mp4", "/tmp/01_s1.png",
            "/tmp/render_plan_preview.md",
        ])

    def test_render_plan_flow_declares_both_customer_approval_artifacts(self):
        outputs = workflow_router._STAGE_FLOW["render_plan"]["expected_outputs"]
        self.assertTrue(any("render_plan.json" in item and
                            "render_plan_preview.md" in item for item in outputs))

    def test_document_render_plan_finish_enforces_identity_and_preview_contract(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            cast = os.path.join(tmp, "cast.png")
            source = os.path.join(tmp, "source.json")
            plan_path = os.path.join(tmp, "render_plan.json")
            preview_path = os.path.join(tmp, "render_plan.md")
            Image.new("RGB", (1280, 720), "white").save(cast)
            Path(source).write_text("{}", encoding="utf-8")
            request = {
                "model": "gpt-image-2", "ratio": "16:9",
                "resolution": "2k", "prompt_zh": "干净人物身份底图",
                "source_references": [run_manifest.file_record(cast)],
            }
            request["source_fingerprint"] = stage_output_contract._json_sha256({
                "prompt": request["prompt_zh"],
                "reference": request["source_references"][0],
            })
            request_spec = identity_request_contract.submission_request_spec(request)
            request["planned_submission_request_spec"] = request_spec
            request["planned_submission_request_fingerprint"] = (
                identity_request_contract.json_sha256(request_spec))
            catalog = {
                "model": "gpt-image-2", "ratios": ["16:9"],
                "resolutions": ["2k"], "selected_ratio": "16:9",
                "selected_resolution": "2k",
            }
            catalog["fingerprint"] = stage_output_contract._json_sha256(catalog)
            request["catalog_contract"] = catalog
            base = {
                "schema_version": 2,
                "kind": "document_explainer_render_plan",
                "client": "demo", "run_id": "run-1",
                "local_only_build": True, "external_model_calls_made": False,
                "approval_scope":
                    "direction_and_local_composition_not_paid_submission",
                "source_contracts": {
                    "execution_preview": run_manifest.file_record(source)},
                "identity_plate_generation": {
                    "required_before_video": True,
                    "status": "awaiting_independent_customer_confirmation",
                    "request": request,
                },
                "pre_video_gates": {
                    "identity_plate":
                        "required_and_independently_customer_confirmed",
                    "exact_video_prompt_review": "required_after_preflight",
                },
                "voice_continuity_contract": {
                    "owner": "video_model_integrated_audio",
                    "image_does_not_lock_voice": True,
                    "content_full_bleed": {
                        "audio_required": True, "presenter_visible": False,
                        "visual_owner": "Remotion content layer",
                    },
                },
                "assembly": {
                    "owner": "video_use_bridge --formal",
                    "timing_change_requires_reapproval": True,
                },
                "segments": [{
                    "id": "s1", "dialogue": "已确认台词",
                    "duration_seconds": 4,
                    "requires_identity_plate": True,
                    "video_model_usage": "avatar_plate_only",
                    "storyboard_ref": False,
                }],
            }
            timing = dialogue_timing.build_contract([{
                "id": "s1", "dialogue": "已确认台词", "duration": 4}])
            base["dialogue_timing_contract"] = timing
            base["segments"][0]["dialogue_timing"] = timing["items"][0]
            canonical = stage_output_contract._json_sha256(base)
            Path(preview_path).write_text(
                "正式渲染计划 canonical=%s\n" % canonical, encoding="utf-8")
            base["customer_preview"] = {
                "path": preview_path, "sha256": sha(preview_path),
                "canonical_plan_fingerprint": canonical,
            }
            Path(plan_path).write_text(
                json.dumps(base, ensure_ascii=False), encoding="utf-8")
            manifest = {
                "client": "demo", "run_id": "run-1",
                "chain_profile": {"profile": "document_explainer"},
                "generation": {"render_plan": {"status": "running"}},
            }
            run_manifest.mark_generation_finished(
                manifest, "render_plan", [plan_path, preview_path])
            self.assertEqual(
                manifest["generation"]["render_plan"]["output_contract"]["kind"],
                "document_render_plan_stage_output_contract")
            invalid = json.loads(json.dumps(base))
            invalid["identity_plate_generation"]["required_before_video"] = False
            invalid.pop("customer_preview")
            invalid_canonical = stage_output_contract._json_sha256(invalid)
            Path(preview_path).write_text(
                "正式渲染计划 canonical=%s\n" % invalid_canonical,
                encoding="utf-8")
            invalid["customer_preview"] = {
                "path": preview_path, "sha256": sha(preview_path),
                "canonical_plan_fingerprint": invalid_canonical,
            }
            Path(plan_path).write_text(
                json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
            manifest["generation"]["render_plan"] = {"status": "running"}
            with self.assertRaisesRegex(ValueError, "IDENTITY_GATE_REQUIRED"):
                run_manifest.mark_generation_finished(
                    manifest, "render_plan", [plan_path, preview_path])

    def test_document_route_blocks_before_identity_and_exact_prompt_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "render_plan.json")
            Path(plan_path).write_text("{}", encoding="utf-8")
            record = run_manifest.file_record(plan_path)
            manifest = {
                "chain_profile": {"profile": "document_explainer"},
                "generation": {"render_plan": {
                    "primary_artifact": record}},
                "pre_video_gates": {},
            }
            route = workflow_router._document_pre_video_gate_route(
                manifest, "video", [])
            self.assertEqual(route["next_action"],
                             "generate_or_confirm_identity_plate")
            identity = os.path.join(tmp, "identity.png")
            Path(identity).write_bytes(b"identity")
            manifest["pre_video_gates"]["identity_plate"] = {
                "status": "confirmed", "render_plan": record,
                "asset": dict(run_manifest.file_record(identity),
                              confirmed_by_customer=True,
                              confirmation_source="customer_dialogue"),
            }
            route = workflow_router._document_pre_video_gate_route(
                manifest, "video", [])
            self.assertEqual(route["next_action"],
                             "preflight_capture_and_confirm_video_prompts")

    def test_legacy_document_identity_bind_cannot_bypass_visual_review(self):
        manifest = {"chain_profile": {"profile": "document_explainer"}}
        with mock.patch.object(explainer_render_plan.run_manifest,
                               "load_manifest", return_value=manifest):
            with self.assertRaisesRegex(ValueError,
                                        "LEGACY_IDENTITY_BIND_DISABLED"):
                explainer_render_plan.bind_identity_plate(
                    "manifest.json", "identity.png")

    def test_direct_paid_gate_accepts_metadata_rich_identity_but_blocks_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "render_plan.json")
            identity_path = os.path.join(tmp, "identity.png")
            review_path = os.path.join(tmp, "prompt_review.json")
            Path(plan_path).write_text("{}", encoding="utf-8")
            Path(identity_path).write_bytes(b"identity")
            Path(review_path).write_text(json.dumps({
                "stage": "video", "status": "confirmed",
                "confirmed_by_customer": True,
            }), encoding="utf-8")
            plan_record = run_manifest.file_record(plan_path)
            identity_evidence = {
                "path": os.path.abspath(identity_path), "sha256": sha(identity_path),
                "status": "confirmed", "confirmed_by_customer": True,
                "confirmation_source": "customer_dialogue",
                "customer_confirmation": "我确认该身份锚图",
                "source_fingerprint": "source", "model": "gpt-image-2",
            }
            manifest = {
                "chain_profile": {"profile": "document_explainer"},
                "generation": {"render_plan": {"primary_artifact": plan_record}},
                "pre_video_gates": {
                    "identity_plate": {"status": "confirmed",
                                       "render_plan": plan_record,
                                       "asset": identity_evidence},
                    "video_prompt_review": {
                        "status": "confirmed", "render_plan": plan_record,
                        "review": run_manifest.file_record(review_path)},
                },
            }
            with mock.patch.object(run_manifest, "approval_is_current",
                                   return_value=True), \
                    mock.patch.object(run_manifest,
                                      "identity_plate_submission_is_current",
                                      return_value=True), \
                    mock.patch.object(run_manifest,
                                      "identity_plate_generation_is_complete",
                                      return_value=True), \
                    mock.patch.object(run_manifest.prompt_review_contract,
                                      "assert_current"):
                self.assertTrue(run_manifest.validate_pre_video_gates(
                    manifest, review_path))
                Path(identity_path).write_bytes(b"tampered")
                with self.assertRaisesRegex(
                        ValueError, "IDENTITY_PLATE_CONFIRMATION_REQUIRED"):
                    run_manifest.validate_pre_video_gates(manifest, review_path)

    def test_storyboard_approval_uses_full_tamper_evident_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = os.path.join(tmp, "storyboard.customer-confirmed.json")
            marker = {
                "status": "confirmed", "kind": "storyboard",
                "confirmed_by_customer": True,
                "customer_confirmation": "我确认当前完整故事板",
                "confirmation_source": "customer_dialogue",
                "client": "acme", "run_id": "run-1", "shots": [{"id": "s1"}],
            }
            identity = storyboard._storyboard_approval_artifact_sha256(marker)
            marker["approval_artifact_sha256"] = identity
            marker["artifact_short_id"] = identity[:12]
            Path(marker_path).write_text(json.dumps(marker), encoding="utf-8")
            manifest = {
                "generation": {"storyboard": {
                    "artifacts": [run_manifest.file_record(marker_path)]}},
                "approval_evidence": {"storyboard": {
                    "approval_path": marker_path,
                    "artifact_sha256": identity,
                    "customer_confirmation": marker["customer_confirmation"],
                    "confirmed_by_customer": True,
                    "confirmation_source": "customer_dialogue",
                }},
            }
            self.assertEqual(len(identity), 64)
            self.assertTrue(run_manifest.customer_approval_evidence_is_current(
                manifest, "storyboard"))
            manifest["approval_evidence"]["storyboard"]["artifact_sha256"] = identity[:12]
            self.assertFalse(run_manifest.customer_approval_evidence_is_current(
                manifest, "storyboard"))
            marker["customer_confirmation"] = "被篡改"
            self.assertNotEqual(
                storyboard._storyboard_approval_artifact_sha256(marker), identity)

    def test_storyboard_semantics_and_splitter_share_schema_valid_enums(self):
        design = {"storyboard_role": "design_board"}
        storyboard_semantics.reconcile(design)
        self.assertEqual(design["storyboard_contract"]["composition_owner"],
                         "remotion")
        contracts = script_splitter._compile_segment_contracts(
            {"plan": design, "design_board": True}, {"id": "s1"},
            {"references": []}, {"background_required": False})
        video_contract = contracts["video_model_contract"]
        self.assertEqual(video_contract["composition_owner"], "remotion")
        self.assertEqual(video_contract["video_model_usage"],
                         "do_not_submit_as_storyboard_ref")
        self.assertEqual(video_contract["composition_components"],
                         ["remotion", "hyperframes", "ffmpeg"])

    def test_identity_request_contract_detects_byte_and_path_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "cast.jpg")
            moved = os.path.join(tmp, "moved.jpg")
            Path(source).write_bytes(b"cast")
            request = {
                "model": "gpt-image-2", "ratio": "16:9", "resolution": "2k",
                "prompt_zh": "单人身份锚图",
                "source_references": [{"path": source, "sha256": sha(source)}],
            }
            with mock.patch.object(identity_request_contract.br_client,
                                   "to_image_ref", return_value="data:image/jpeg;base64,Y2FzdA=="):
                spec = identity_request_contract.submission_request_spec(request)
                request["planned_submission_request_spec"] = spec
                request["planned_submission_request_fingerprint"] = (
                    identity_request_contract.json_sha256(spec))
                identity_request_contract.assert_request_matches_plan(request)
                Path(moved).write_bytes(b"cast")
                moved_request = json.loads(json.dumps(request))
                moved_request["source_references"][0]["path"] = moved
                with self.assertRaisesRegex(ValueError, "SPEC_DIVERGED"):
                    identity_request_contract.assert_request_matches_plan(moved_request)
                Path(source).write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "REFERENCE_STALE"):
                    identity_request_contract.assert_request_matches_plan(request)

    def test_identity_capture_is_local_only_and_contains_exact_image_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "cast.jpg")
            packet_path = os.path.join(tmp, "identity_submission.json")
            preview_path = os.path.join(tmp, "identity_submission.md")
            Path(source).write_bytes(b"cast")
            request = {
                "model": "gpt-image-2", "ratio": "16:9", "resolution": "2k",
                "prompt_zh": "单人身份锚图",
                "source_references": [{"path": source, "sha256": sha(source)}],
                "source_fingerprint": "s" * 64,
            }
            data_url = "data:image/jpeg;base64,Y2FzdA=="
            with mock.patch.object(identity_request_contract.br_client,
                                   "to_image_ref", return_value=data_url):
                spec = identity_request_contract.submission_request_spec(request)
            request["planned_submission_request_spec"] = spec
            request["planned_submission_request_fingerprint"] = (
                identity_request_contract.json_sha256(spec))
            manifest = {"client": "acme", "run_id": "run-1",
                        "identity_plate_generation": {
                            "reference_approval": {"ok": True}}}
            context = {
                "plan": {"client": "acme", "run_id": "run-1"},
                "plan_record": {"path": "/plan", "sha256": "p" * 64},
                "request": request, "request_spec": spec,
                "request_fingerprint": request[
                    "planned_submission_request_fingerprint"],
            }
            catalog = {"model": "gpt-image-2", "selected_ratio": "16:9",
                       "selected_resolution": "2k", "fingerprint": "c" * 64}

            def persist(_path, mutate, **_kwargs):
                return mutate(manifest)

            with mock.patch.object(identity_plate_workflow.run_manifest,
                                   "load_manifest", return_value=manifest), \
                    mock.patch.object(identity_plate_workflow.key_setup,
                                      "require_key", return_value="sk-test"), \
                    mock.patch.object(identity_plate_workflow.run_manifest,
                                      "identity_plate_reference_is_current",
                                      return_value=True), \
                    mock.patch.object(identity_plate_workflow, "_context",
                                      return_value=context), \
                    mock.patch.object(identity_plate_workflow,
                                      "_live_catalog_contract", return_value=catalog), \
                    mock.patch.object(identity_plate_workflow.run_manifest,
                                      "require_run_output",
                                      side_effect=lambda _m, path, **_k: path), \
                    mock.patch.object(identity_plate_workflow, "_save_state",
                                      side_effect=persist), \
                    mock.patch.object(identity_plate_workflow.br_client,
                                      "to_image_ref", return_value=data_url), \
                    mock.patch.object(identity_plate_workflow.br_client,
                                      "create_image_generation") as create:
                result = identity_plate_workflow.capture_identity_plate_submission(
                    "manifest.json", packet_path, preview_path)
            create.assert_not_called()
            packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
            self.assertEqual(packet["payload"]["imageUrls"], [data_url])
            self.assertFalse(result["external_calls"])
            self.assertIn("不会调用生成模型", Path(preview_path).read_text(
                encoding="utf-8"))

    def test_identity_live_catalog_401_revokes_readiness_before_state_change(self):
        rejection = identity_plate_workflow.br_client.BRError(
            "Invalid API Key", http_status=401)
        with mock.patch.object(
                identity_plate_workflow.br_client, "list_image_models",
                side_effect=rejection), \
                mock.patch.object(
                    identity_plate_workflow.key_setup, "reject_key",
                    return_value=True) as reject:
            with self.assertRaises(key_setup.SessionKeyRequired) as raised:
                identity_plate_workflow._live_catalog_contract(
                    {"model": "gpt-image-2"}, api_key="sk-invalid-test")
        reject.assert_called_once_with("sk-invalid-test")
        self.assertIn("旧授权已安全撤销", raised.exception.client_message)

    def test_identity_capture_never_overwrites_historical_packet_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet_path = os.path.join(tmp, "capture.json")
            preview_path = os.path.join(tmp, "capture.md")
            base = {
                "payload": {"model": "gpt-image-2", "ratio": "16:9",
                            "resolution": "2k", "count": 1, "text": "A"},
                "request_spec": {"image_references": []},
                "payload_fingerprint": "a" * 64,
                "catalog_fingerprint": "c" * 64,
                "captured_at": "first",
            }
            Path(packet_path).write_text(json.dumps(base), encoding="utf-8")
            Path(preview_path).write_text(
                identity_plate_workflow._capture_preview(base) + "\n",
                encoding="utf-8")
            before_packet = Path(packet_path).read_bytes()
            before_preview = Path(preview_path).read_bytes()
            changed = json.loads(json.dumps(base))
            changed["payload"]["text"] = "B"
            changed["captured_at"] = "second"
            with self.assertRaisesRegex(ValueError, "IMMUTABLE_OUTPUT_PATH"):
                identity_plate_workflow._persist_immutable_capture(
                    changed, packet_path, preview_path)
            self.assertEqual(Path(packet_path).read_bytes(), before_packet)
            self.assertEqual(Path(preview_path).read_bytes(), before_preview)

    def test_identity_confirmation_requires_specific_customer_wording(self):
        for function in (
                identity_plate_workflow.confirm_identity_plate_reference,
                identity_plate_workflow.confirm_identity_plate_submission):
            with self.subTest(function=function.__name__), self.assertRaises(
                    ValueError):
                function("missing.json", "继续")

    def test_identity_reference_confirmation_advances_top_level_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = os.path.join(tmp, "reference-approval.json")
            manifest = {"client": "acme", "run_id": "run-1"}
            context = {
                "plan_record": {"path": "/plan", "sha256": "p" * 64},
                "request_fingerprint": "r" * 64,
                "request_spec": {"model": "gpt-image-2"},
                "request": {
                    "source_references": [], "catalog_contract": {}},
            }

            def persist(_path, mutate, **_kwargs):
                return mutate(manifest)

            with mock.patch.object(identity_plate_workflow, "_context",
                                   return_value=context), \
                    mock.patch.object(
                        identity_plate_workflow.run_manifest,
                        "identity_plate_reference_is_current", return_value=False), \
                    mock.patch.object(identity_plate_workflow, "_marker_path",
                                      return_value=marker_path), \
                    mock.patch.object(identity_plate_workflow, "_save_state",
                                      side_effect=persist):
                result = identity_plate_workflow.confirm_identity_plate_reference(
                    "manifest.json", "我确认身份引用包")
            self.assertEqual(
                result["status"],
                "reference_confirmed_pending_submission_capture")
            self.assertEqual(
                manifest["status"], "needs_identity_plate_submission_capture")

    def test_unconfirmed_identity_capture_cannot_reach_paid_create(self):
        with mock.patch.object(identity_plate_workflow.key_setup, "require_key",
                               return_value="sk-test"), \
                mock.patch.object(identity_plate_workflow.run_manifest,
                               "load_manifest", return_value={}), \
                mock.patch.object(identity_plate_workflow, "_validated_capture",
                                  side_effect=ValueError(
                                      "IDENTITY_SUBMISSION_CUSTOMER_CONFIRMATION_REQUIRED")), \
                mock.patch.object(identity_plate_workflow.br_client,
                                  "create_image_generation") as create:
            with self.assertRaisesRegex(ValueError, "CUSTOMER_CONFIRMATION_REQUIRED"):
                identity_plate_workflow.submit_identity_plate(
                    "manifest.json", "packet.json", "identity.png")
        create.assert_not_called()

    def test_missing_identity_key_never_mutates_or_consumes_attempt(self):
        with mock.patch.object(
                identity_plate_workflow.key_setup, "require_key",
                side_effect=key_setup.SessionKeyRequired(
                    "IDENTITY_SUBMISSION_BASICROUTER_KEY_REQUIRED")), \
                mock.patch.object(identity_plate_workflow.run_manifest,
                                  "load_manifest") as load_manifest, \
                mock.patch.object(identity_plate_workflow, "_save_state") as save_state, \
                mock.patch.object(identity_plate_workflow,
                                  "_record_submission_failure") as record_failure, \
                mock.patch.object(identity_plate_workflow.br_client,
                                  "create_image_generation") as create:
            with self.assertRaisesRegex(
                    key_setup.SessionKeyRequired,
                    "IDENTITY_SUBMISSION_BASICROUTER_KEY_REQUIRED"):
                identity_plate_workflow.submit_identity_plate(
                    "manifest.json", "packet.json", "identity.png")
        load_manifest.assert_not_called()
        save_state.assert_not_called()
        record_failure.assert_not_called()
        create.assert_not_called()

    def test_existing_identity_task_is_recovered_without_second_paid_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "identity.png")
            packet = {
                "payload": {"model": "gpt-image-2"},
                "payload_fingerprint": "f" * 64,
                "request_spec": {"model": "gpt-image-2"},
            }
            state = {
                "status": "failed", "task_id": "task-1", "attempt": 1,
                "submission_request_id": "request-1",
                "generation_error": "DOWNLOAD_PEER_BLOCKED",
                "failure": {"path": "failure.json", "sha256": "e" * 64},
            }
            manifest = {
                "identity_plate_generation": state, "tasks": [],
                "generation_error": "temporary transport failure",
            }
            download_options = []

            def download(_url, path, **_kwargs):
                download_options.append(_kwargs)
                Path(path).write_bytes(b"generated")

            def persist(_path, mutate, **_kwargs):
                return mutate(manifest)

            with mock.patch.object(identity_plate_workflow.run_manifest,
                                   "load_manifest", return_value=manifest), \
                    mock.patch.object(identity_plate_workflow,
                                      "_validated_capture",
                                      return_value=({
                                          "plan": {"client": "demo",
                                                   "run_id": "run-1"},
                                          "plan_record": {"path": "plan.json",
                                                          "sha256": "p" * 64},
                                      }, state, packet, {}, {})), \
                    mock.patch.object(identity_plate_workflow.run_manifest,
                                      "require_run_output",
                                      side_effect=lambda _m, path, **_k: path), \
                    mock.patch.object(identity_plate_workflow.key_setup, "require_key",
                                      return_value="sk-test"), \
                    mock.patch.object(identity_plate_workflow.br_client,
                                      "wait_image_generation",
                                      return_value=["https://example.com/identity.png"]), \
                    mock.patch.object(identity_plate_workflow.br_client, "download",
                                      side_effect=download), \
                    mock.patch.object(identity_plate_workflow.review_contract,
                                      "inspect_image", return_value={
                                          "passed": True,
                                          "checks": {
                                              "decodable": True,
                                              "minimum_dimensions": True,
                                              "expected_aspect_ratio": True,
                                          }}), \
                    mock.patch.object(identity_plate_workflow, "_save_state",
                                      side_effect=persist), \
                    mock.patch.object(identity_plate_workflow.br_client,
                                      "create_image_generation") as create:
                result = identity_plate_workflow.recover_identity_plate_task(
                    "manifest.json", out)
            create.assert_not_called()
            self.assertEqual(result["status"],
                             "generated_pending_customer_confirmation")
            self.assertEqual(download_options, [{"allow_nonpublic_peer": True}])
            self.assertNotIn("generation_error", manifest)
            self.assertNotIn(
                "generation_error", manifest["identity_plate_generation"])
            history = manifest["identity_plate_generation_recovery_history"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["task_id"], "task-1")
            self.assertIn("generation_error", history[0]["prior_errors"])

    def test_identity_recovery_cannot_erase_visual_or_customer_rejection(self):
        for failure_kind in ("visual_review_rejection", "customer_rejection"):
            manifest = {"identity_plate_generation": {
                "status": "failed", "task_id": "task-1", "attempt": 1,
                "submission_request_id": "request-1",
                "failure_kind": failure_kind,
            }}
            with mock.patch.object(identity_plate_workflow.run_manifest,
                                   "load_manifest", return_value=manifest), \
                    mock.patch.object(identity_plate_workflow,
                                      "_validated_capture", return_value=(
                                          {"plan": {}, "plan_record": {}},
                                          manifest["identity_plate_generation"],
                                          {}, {}, {})), \
                    mock.patch.object(identity_plate_workflow.key_setup,
                                      "require_key", return_value="sk-test"), \
                    mock.patch.object(identity_plate_workflow.br_client,
                                      "wait_image_generation") as wait:
                with self.assertRaisesRegex(
                        ValueError, "REJECTED_CANDIDATE_FORBIDDEN"):
                    identity_plate_workflow.recover_identity_plate_task(
                        "manifest.json", "identity.png")
            wait.assert_not_called()

    def test_identity_media_failure_records_actual_downloaded_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "identity.png")
            Path(out).write_bytes(b"not-an-image")
            packet = {
                "payload": {"model": "gpt-image-2", "ratio": "16:9"},
                "payload_fingerprint": "f" * 64,
                "request_spec": {"model": "gpt-image-2"},
            }
            context = {
                "plan": {"client": "demo", "run_id": "run-1"},
                "plan_record": {"path": "plan.json", "sha256": "p" * 64},
            }
            manifest = {"identity_plate_generation": {}, "tasks": []}

            def persist(_path, mutate, **_kwargs):
                return mutate(manifest)

            with mock.patch.object(identity_plate_workflow, "_save_state",
                                   side_effect=persist):
                failure = identity_plate_workflow._record_submission_failure(
                    "manifest.json", context=context, packet=packet,
                    attempt=1, request_id="request-1", task_id="task-1",
                    error="decode failed", out_path=out,
                    remote_terminal_status="success")
            self.assertEqual(failure["candidate_images_created"], 1)
            self.assertEqual(failure["returned_media"][0]["sha256"], sha(out))
            self.assertFalse(failure["deterministic_media_qc"]["passed"])
            self.assertEqual(manifest["tasks"][0]["candidate_images_created"], 1)

    def test_retry_is_blocked_until_existing_remote_task_is_proven_failed(self):
        state = {
            "status": "failed", "failure": {"path": "failure.json"},
            "task_id": "task-1", "remote_terminal_status": "unknown",
        }
        manifest = {"identity_plate_generation": state}

        def persist(_path, mutate, **_kwargs):
            return mutate(manifest)

        with mock.patch.object(identity_plate_workflow.key_setup, "require_key",
                               return_value="sk-test"), \
                mock.patch.object(identity_plate_workflow.run_manifest,
                                  "load_manifest", return_value=manifest), \
                mock.patch.object(identity_plate_workflow, "_validated_capture"), \
                mock.patch.object(identity_plate_workflow, "_save_state",
                                  side_effect=persist):
            with self.assertRaisesRegex(ValueError, "MUST_BE_RECOVERED"):
                identity_plate_workflow.authorize_identity_plate_retry(
                    "manifest.json", "我确认身份锚图需要重试")

    def test_visual_rejection_can_authorize_new_attempt_without_repolling_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            rejection_path = os.path.join(tmp, "visual-review.json")
            approval_path = os.path.join(tmp, "retry-approval.json")
            Path(rejection_path).write_text(json.dumps({
                "kind": "identity_plate_visual_review", "status": "rejected",
                "passed": False,
            }), encoding="utf-8")
            state = {
                "status": "failed",
                "failure": run_manifest.file_record(rejection_path),
                "task_id": "task-1", "remote_terminal_status": "success",
                "attempt": 1,
                "submission_capture": {"payload_fingerprint": "f" * 64},
            }
            manifest = {
                "client": "demo", "run_id": "run-1",
                "identity_plate_generation": state,
            }

            def persist(_path, mutate, **_kwargs):
                return mutate(manifest)

            with mock.patch.object(identity_plate_workflow.key_setup,
                                   "require_key", return_value="sk-test"), \
                    mock.patch.object(identity_plate_workflow.run_manifest,
                                      "load_manifest", return_value=manifest), \
                    mock.patch.object(identity_plate_workflow,
                                      "_validated_capture"), \
                    mock.patch.object(identity_plate_workflow,
                                      "_marker_path", return_value=approval_path), \
                    mock.patch.object(identity_plate_workflow, "_save_state",
                                      side_effect=persist):
                result = identity_plate_workflow.authorize_identity_plate_retry(
                    "manifest.json", "我确认身份锚图需要重试")
            self.assertEqual(result["status"],
                             "submission_confirmed_ready_to_create")
            self.assertEqual(result["authorized_attempt"], 2)
            self.assertNotIn("failure", result)

    def test_pseudo_stages_are_ordered_in_core_status_and_chain_metadata(self):
        manifest = {"chain_profile": {"profile": "document_explainer"}}
        with mock.patch.object(run_manifest, "required_stage_order",
                               return_value=["render_plan", "video"]), \
                mock.patch.object(run_manifest, "stage_is_complete", return_value=True), \
                mock.patch.object(run_manifest, "identity_plate_reference_required",
                                  return_value=True), \
                mock.patch.object(run_manifest, "identity_plate_reference_is_current",
                                  return_value=False):
            self.assertEqual(run_manifest.next_required_stage(manifest),
                             "identity_plate_reference")
        with mock.patch.object(run_manifest, "required_stage_order",
                               return_value=["render_plan", "video"]):
            metadata = workflow_router._profile_stage_index(
                "document_explainer", "identity_plate_submission_capture", manifest)
        self.assertEqual(metadata["previous"], "identity_plate_reference")
        self.assertEqual(metadata["next"], "identity_plate_generation")
        self.assertGreater(metadata["index"], 0)
        with mock.patch.object(run_manifest, "required_stage_order",
                               return_value=["render_plan"]), \
                mock.patch.object(run_manifest, "stage_is_complete",
                                  return_value=True), \
                mock.patch.object(run_manifest,
                                  "identity_plate_reference_required",
                                  return_value=True), \
                mock.patch.object(run_manifest,
                                  "identity_plate_reference_is_current",
                                  return_value=True), \
                mock.patch.object(run_manifest,
                                  "identity_plate_submission_is_current",
                                  return_value=True), \
                mock.patch.object(run_manifest,
                                  "identity_plate_generation_candidate_is_current",
                                  return_value=True), \
                mock.patch.object(run_manifest,
                                  "identity_plate_generation_is_complete",
                                  return_value=False):
            self.assertEqual(run_manifest.next_required_stage(manifest),
                             "identity_plate_review")
        with mock.patch.object(run_manifest, "required_stage_order",
                               return_value=["render_plan", "video"]):
            review_metadata = workflow_router._profile_stage_index(
                "document_explainer", "identity_plate_review", manifest)
        self.assertEqual(review_metadata["previous"],
                         "identity_plate_generation")
        self.assertEqual(review_metadata["next"], "video")

    def test_identity_cli_never_prints_inline_image_payload(self):
        huge = "data:image/png;base64," + ("A" * 100000)
        result = {
            "payload": {"imageUrls": [huge], "model": "gpt-image-2"},
            "nested": {"imageUrls": [huge]},
            "payload_fingerprint": "f" * 64,
        }
        output = io.StringIO()
        with mock.patch.object(identity_plate_workflow,
                               "capture_identity_plate_submission",
                               return_value=result), \
                mock.patch("sys.stdout", output):
            code = identity_plate_workflow.main([
                "--manifest", "manifest.json", "--capture",
                "--out", "capture.json", "--preview-out", "capture.md",
            ])
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn("data:image", rendered)
        self.assertNotIn("imageUrls", rendered)
        self.assertNotIn("\"payload\"", rendered)
        self.assertIn("f" * 64, rendered)
        self.assertLess(len(rendered), 1000)

    def test_identity_media_qc_and_visual_review_are_exactly_bound(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.png")
            wrong = os.path.join(tmp, "wrong.png")
            Image.new("RGB", (1280, 720), "white").save(good)
            Image.new("RGB", (1280, 800), "white").save(wrong)
            qc = identity_review_contract.inspect_image(good)
            self.assertTrue(qc["passed"])
            self.assertFalse(identity_review_contract.inspect_image(wrong)["passed"])
            artifact = run_manifest.file_record(good)
            result = {"path": os.path.join(tmp, "result.json"),
                      "sha256": "r" * 64}
            refs = [{"source_path": "/tmp/cast.jpg",
                     "source_sha256": "c" * 64,
                     "data_url_sha256": "d" * 64,
                     "transport": "data_url_from_local_file"}]
            observations = {
                name: "已逐项比对并记录可见证据"
                for name in identity_review_contract.REQUIRED_VISUAL_CHECKS
            }
            review = {
                "kind": "identity_plate_visual_review", "status": "passed",
                "passed": True, "reviewer_type": "agent_visual_inspection",
                "artifact": artifact, "generation_result": result,
                "identity_references": refs, "deterministic_media_qc": qc,
                "checks": {name: True for name in
                           identity_review_contract.REQUIRED_VISUAL_CHECKS},
                "observations": observations,
            }
            self.assertTrue(identity_review_contract.validate_visual_review(
                review, candidate=artifact, generation_result=result,
                media_qc=qc, identity_references=refs))
            review["artifact"] = dict(artifact, sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "VISUAL_REVIEW_INVALID"):
                identity_review_contract.validate_visual_review(
                    review, candidate=artifact, generation_result=result,
                    media_qc=qc, identity_references=refs)

    def test_identity_result_review_customer_confirmation_closes_video_gate(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".approvals"), exist_ok=True)
            cast = os.path.join(tmp, "cast.png")
            candidate_path = os.path.join(tmp, "identity.png")
            Image.new("RGB", (1280, 720), "white").save(cast)
            Image.new("RGB", (1280, 720), "gray").save(candidate_path)
            request = {
                "model": "gpt-image-2", "ratio": "16:9",
                "resolution": "2k", "prompt_zh": "干净人物身份底图",
                "source_references": [run_manifest.file_record(cast)],
                "source_fingerprint": "source-current",
            }
            planned = identity_request_contract.submission_request_spec(request)
            request["planned_submission_request_spec"] = planned
            request["planned_submission_request_fingerprint"] = (
                identity_request_contract.json_sha256(planned))
            plan_path = os.path.join(tmp, "render_plan.json")
            plan = {
                "client": "demo", "run_id": "run-1",
                "identity_plate_generation": {
                    "required_before_video": True, "request": request},
            }
            Path(plan_path).write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            manifest = {
                "client": "demo", "run_id": "run-1", "revision": 1,
                "chain_profile": {"profile": "document_explainer"},
                "generation": {"render_plan": {
                    "status": "approved",
                    "primary_artifact": run_manifest.file_record(plan_path),
                }},
                "tasks": [],
            }

            def persist(_path, mutate, **_kwargs):
                return mutate(manifest)

            capture_path = os.path.join(tmp, "identity-submission.json")
            preview_path = os.path.join(tmp, "identity-submission.md")
            review_path = os.path.join(tmp, "identity.visual-review.json")
            catalog = {"fingerprint": "catalog-current"}
            with mock.patch.object(run_manifest, "run_output_root",
                                   return_value=tmp), \
                    mock.patch.object(run_manifest, "approval_is_current",
                                      return_value=True), \
                    mock.patch.object(run_manifest, "load_manifest",
                                      return_value=manifest), \
                    mock.patch.object(identity_plate_workflow,
                                      "_save_state", side_effect=persist), \
                    mock.patch.object(identity_plate_workflow.key_setup,
                                      "require_key", return_value="sk-test"), \
                    mock.patch.object(identity_plate_workflow,
                                      "_live_catalog_contract",
                                      return_value=catalog):
                identity_plate_workflow.confirm_identity_plate_reference(
                    "manifest.json", "我确认身份图参考引用包")
                identity_plate_workflow.capture_identity_plate_submission(
                    "manifest.json", capture_path, preview_path)
                identity_plate_workflow.confirm_identity_plate_submission(
                    "manifest.json", "我确认身份图精确提交请求")
                state = manifest["identity_plate_generation"]
                packet = json.loads(Path(capture_path).read_text(encoding="utf-8"))
                output = run_manifest.file_record(candidate_path)
                qc = identity_review_contract.inspect_image(candidate_path)
                result_path = candidate_path + ".result.json"
                result = {
                    "schema_version": 1,
                    "kind": "identity_plate_generation_result",
                    "status": "pending_visual_review", "client": "demo",
                    "run_id": "run-1", "task_id": "task-1",
                    "submission_request_id": "request-1", "attempt": 1,
                    "payload_fingerprint": packet["payload_fingerprint"],
                    "request_spec": packet["request_spec"],
                    "candidate": output,
                    "remote_url": "https://example.com/identity.png",
                    "deterministic_media_qc": qc,
                }
                Path(result_path).write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8")
                state.update({
                    "status": "generated_pending_customer_confirmation",
                    "task_id": "task-1", "submission_request_id": "request-1",
                    "attempt": 1, "output": output, "candidate": output,
                    "remote_url": result["remote_url"],
                    "generation_result": run_manifest.file_record(result_path),
                    "deterministic_media_qc": qc,
                })
                checks = {name: True for name in
                          identity_review_contract.REQUIRED_VISUAL_CHECKS}
                observations = {name: "已与当前人物引用逐项比对通过"
                                for name in checks}
                identity_plate_workflow.record_identity_plate_visual_review(
                    "manifest.json", review_path, checks=checks,
                    observations=observations,
                    reviewer_notes="逐项查看当前身份图，人物和干净底片构图均可用")
                identity_plate_workflow.confirm_generated_identity_plate(
                    "manifest.json", review_path, "我确认这张身份锚图可以使用")
            self.assertTrue(
                run_manifest.identity_plate_generation_is_complete(manifest))
            review = json.loads(Path(review_path).read_text(encoding="utf-8"))
            review["reviewer_notes"] = "被篡改"
            Path(review_path).write_text(
                json.dumps(review, ensure_ascii=False), encoding="utf-8")
            self.assertFalse(
                run_manifest.identity_plate_generation_is_complete(manifest))

    def test_identity_generation_and_review_have_distinct_actions_and_gates(self):
        self.assertNotIn("identity_plate_generation",
                         interaction_contract.CUSTOMER_CONFIRM_STAGES)
        self.assertIn("identity_plate_review",
                      interaction_contract.CUSTOMER_CONFIRM_STAGES)
        manifest = {
            "chain_profile": {"profile": "document_explainer"},
            "identity_plate_generation": {
                "status": "submitted", "task_id": "task-1"},
        }
        with mock.patch.object(run_manifest, "next_required_stage",
                               return_value="identity_plate_generation"):
            route = workflow_router._manifest_route(manifest)
        self.assertEqual(route["next_action"], "recover_identity_plate_task")
        with mock.patch.object(workflow_router, "_manifest_route", return_value={
                "current_stage": "identity_plate_generation",
                "next_action": "recover_identity_plate_task",
                "customer_message": "恢复同一任务",
                "customer_preview": ["/tmp/candidate.png"],
        }):
            contract = workflow_router.stage_contract(
                {"manifest": manifest}, "identity_plate_generation")
        self.assertEqual(contract["next_action"],
                         "recover_identity_plate_task")
        self.assertEqual(contract["next_message"], "恢复同一任务")
        with mock.patch.object(run_manifest, "identity_gate"), \
                mock.patch.object(run_manifest, "generation_dependencies",
                                  return_value=[]), \
                mock.patch.object(run_manifest,
                                  "identity_plate_reference_required",
                                  return_value=True), \
                mock.patch.object(run_manifest,
                                  "identity_plate_generation_is_complete",
                                  return_value=False):
            with self.assertRaisesRegex(
                    ValueError, "IDENTITY_PLATE_REVIEW_REQUIRED"):
                run_manifest.generation_gate(manifest, "video")

    def test_router_validation_rejects_missing_and_placeholder_entrypoints(self):
        bad_name = "unit_bad_pseudo_stage"
        workflow_router._STAGE_FLOW[bad_name] = {
            "entry_functions": [{
                "module": "identity_plate_workflow", "function": "missing_callable",
                "cli": "python3 scripts/identity_plate_workflow.py placeholder",
            }],
            "required_inputs": [], "expected_outputs": [],
            "confirm_function": None, "recovery": [], "next": None,
        }
        try:
            errors = workflow_router._validate_stage_flow()
        finally:
            workflow_router._STAGE_FLOW.pop(bad_name, None)
        self.assertTrue(any("missing callable" in item for item in errors))
        self.assertTrue(any("placeholder" in item for item in errors))
        removed = workflow_router._STAGE_FLOW.pop("identity_plate_reference")
        try:
            missing_errors = workflow_router._validate_stage_flow()
        finally:
            workflow_router._STAGE_FLOW["identity_plate_reference"] = removed
        self.assertTrue(any("identity_plate_reference" in item and
                            "missing flow" in item for item in missing_errors))

    def test_final_frame_qa_uses_actual_subtitle_and_content_only_presenter_na(self):
        with tempfile.TemporaryDirectory() as tmp:
            hf = os.path.join(tmp, "hf.json")
            integrated = os.path.join(tmp, "integrated.json")
            animatic = os.path.join(tmp, "animatic.mp4")
            board = os.path.join(tmp, "board.png")
            result = os.path.join(tmp, "execution.json")
            Path(hf).write_text(json.dumps({"scenes": [{
                "source_shot_id": "s1", "slot": "subtitle",
                "text": "完整字幕原文"}]}), encoding="utf-8")
            Path(integrated).write_text(json.dumps({"scenes": [{
                "id": "s1", "humanSlot": "none"}]}), encoding="utf-8")
            Path(animatic).write_bytes(b"animatic")
            Path(board).write_bytes(b"board")
            animatic_sha, board_sha = sha(animatic), sha(board)
            Path(result).write_text(json.dumps({
                "hyperframes_spec": {"path": hf, "sha256": sha(hf)},
                "integrated_spec": {"path": integrated,
                                    "sha256": sha(integrated)},
                "animatic": {"path": animatic, "sha256": animatic_sha},
                "layout_boards": [{
                    "id": "s1", "path": board, "sha256": board_sha,
                    "animatic_sha256": animatic_sha, "frame": 15,
                    "board_renderer": "ffmpeg_animatic_frame_extract",
                    "shot": {"dialogue": "口播"},
                }],
            }), encoding="utf-8")
            qa = explainer_layout_preview.write_final_frame_visual_qa(
                result, {
                    "checker": "codex", "reviewer_type": "agent",
                    "method": "manual_frame_by_frame_agent_visual_review",
                    "shots": {"s1": {
                        "content_complete": True,
                        "cross_layer_occlusion": False,
                        "safe_zone": True, "artifact_detected": False,
                        "subtitle_text_complete": True,
                        "presenter_visible": "not_applicable",
                    }},
                })
            self.assertTrue(qa["passed"])
            self.assertEqual(qa["shots"][0]["expected_subtitle_text"],
                             "完整字幕原文")
            self.assertEqual(qa["shots"][0]["presenter_visibility"],
                             "not_applicable")
            self.assertFalse(qa["customer_confirmed"])


if __name__ == "__main__":
    unittest.main()
