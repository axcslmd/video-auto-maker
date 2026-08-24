import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import artifact_contract
import br_client
import run_manifest
import run_manifest_cli
import script_splitter
import script_splitter_cli
import take_review
import video_engine
import video_models
import video_recovery
import workflow_contract
import workflow_router


class VideoRecoveryContractTests(unittest.TestCase):
    def test_recovery_cli_reads_exact_current_handoff_instead_of_rebuilding(self):
        with tempfile.TemporaryDirectory() as tmp:
            segments_path = Path(tmp, "segments.json")
            payload = {
                "client": "acme", "run_id": "run-1",
                "segments": [{"id": "s1", "model": "old-route"}],
            }
            segments_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            record = run_manifest.file_record(str(segments_path))
            manifest = {
                "client": "acme", "run_id": "run-1",
                "handoffs": {"video": {"file": record}},
            }
            service = types.SimpleNamespace(rm=run_manifest)
            self.assertEqual(
                script_splitter_cli._read_recovery_source_handoff(
                    service, manifest, str(segments_path)), payload)
            with self.assertRaisesRegex(
                    ValueError, "OUTPUT_MUST_REPLACE_SOURCE_HANDOFF"):
                script_splitter_cli._read_recovery_source_handoff(
                    service, manifest, str(Path(tmp, "other.json")))

    def _failed_qc_fixture(self, root, *, handoff="handoff-v7"):
        video_path = os.path.join(root, "failed-take.mp4")
        qc_path = os.path.join(root, "failed-take.qc.json")
        review_path = os.path.join(root, "failed-take.review.json")
        Path(video_path).write_bytes(b"failed formal video bytes")
        video_sha = artifact_contract.file_sha256(video_path)
        qc = {
            "passed": False, "errors": ["AUDIO_TRACK_FAILED"],
            "file": {"path": video_path, "sha256": video_sha},
            "media": {"actual_duration": 4.8,
                      "audio": {"stream_count": 0}},
            "report_path": qc_path,
        }
        Path(qc_path).write_text(
            json.dumps(qc, ensure_ascii=False), encoding="utf-8")
        segment = {"id": "s1", "scene_id": "scene-1"}
        task = {
            "task_id": "task-qc-1", "attempt": 1,
            "model": "seedance-old-route",
            "initial_model": "seedance-old-route",
            "handoff_fingerprint": handoff,
        }
        failed_take = video_engine._build_media_qc_failure_result(
            url="https://example.invalid/failed-take.mp4",
            local=video_path, qc=qc, task_id="task-qc-1",
            segment=segment, task=task,
            initial_model="seedance-old-route",
            selected_model="seedance-old-route")
        review = take_review.create_review(
            failed_take, segment, project_id="acme", run_id="run-1")
        review = take_review.decide(
            review, "rejected", "customer", "客户拒绝无音轨成片")
        Path(review_path).write_text(
            json.dumps(review, ensure_ascii=False), encoding="utf-8")
        failure = {
            "status": "media_qc_failed", "segment_id": "s1",
            "task_id": "task-qc-1", "attempt": 1,
            "take_fingerprint": failed_take["take_fingerprint"],
            "rejected_take_review": run_manifest.file_record(review_path),
            "media_qc_report": run_manifest.file_record(qc_path),
        }
        manifest = {
            "client": "acme", "run_id": "run-1",
            "tasks": [{
                "stage": "video", "unit_id": "s1",
                "task_id": "task-qc-1", "attempt": 1,
                "model": "seedance-old-route", "status": "succeeded",
                "handoff_fingerprint": handoff,
                "take_id": failed_take["take_id"],
                "take_fingerprint": failed_take["take_fingerprint"],
                "media_qc_failed": True,
            }],
            "video_attempts": {"s1": {
                "attempt": 2,
                "previous_take_fingerprint": failed_take["take_fingerprint"],
            }},
        }
        return manifest, failure, failed_take, video_path

    def _recovery_state(self, root, *, bind_policy=False):
        recovery_path = os.path.join(root, "video_recovery_contract.json")
        recovery_payload = {
            "kind": "video_recovery_contract", "schema_version": 1,
            "client": "acme", "run_id": "run-1",
            "recovery_id": "recovery-1",
        }
        Path(recovery_path).write_text(
            json.dumps(recovery_payload, ensure_ascii=False), encoding="utf-8")
        recovery_record = run_manifest.file_record(recovery_path)
        segment = {
            "client": "acme", "run_id": "run-1", "id": "s1",
            "text": "已确认恢复试片", "dialogue": "已确认台词",
            "video_type": 5, "model": "seedance-exact-route",
            "urls": ["https://example.invalid/identity.jpg"],
            "references": [{
                "id": "identity-1", "tag": "@host",
                "type": "character_identity",
                "url": "https://example.invalid/identity.jpg",
            }],
            "audio_contract": {
                "track": "required", "dialogue": "已确认台词"},
            "recovery_contract": {
                "path": recovery_record["path"],
                "sha256": recovery_record["sha256"],
                "recovery_id": "recovery-1",
                "mode": "independent_identity_anchor",
                "target_model": "seedance-exact-route",
            },
        }
        segment["video_handoff_fingerprint"] = (
            artifact_contract.build_video_handoff(segment)["fingerprint"])
        segments_path = os.path.join(root, "segments.json")
        spec = {
            "client": "acme", "run_id": "run-1",
            "segments": [segment],
            "video_recovery_contract": {
                "file": recovery_record, "recovery_id": "recovery-1",
                "strategy": {
                    "require_canary_before_batch": True,
                    "canary_segment_id": "s1",
                },
            },
        }
        Path(segments_path).write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        base_segments_record = run_manifest.file_record(segments_path)
        base_fp = segment["video_handoff_fingerprint"]
        base_payload = {"s1": base_fp}
        base_aggregate = run_manifest._digest(json.dumps(
            base_payload, ensure_ascii=False, sort_keys=True))

        if bind_policy:
            policy_path = os.path.join(
                root, "privacy_fallback_policy_contract.json")
            policy = {
                "kind": "privacy_fallback_policy_contract",
                "schema_version": 1,
                "policy": video_engine.PRIVACY_FALLBACK_STOP_POLICY,
                "scope": "formal_test_segment_only",
                "client": "acme", "run_id": "run-1",
                "segment_id": "s1", "recovery_id": "recovery-1",
                "source_video_handoff_fingerprint": base_fp,
                "source_segments_sha256": base_segments_record["sha256"],
                "source_aggregate_handoff_sha256": base_aggregate,
                "confirmed_by_customer": True,
                "confirmation_source": "customer_dialogue",
                "customer_confirmation": (
                    "我确认若试片触发真人隐私拒绝就立即停止，"
                    "不提交第二个付费回退任务。"),
            }
            Path(policy_path).write_text(
                json.dumps(policy, ensure_ascii=False), encoding="utf-8")
            policy_record = run_manifest.file_record(policy_path)
            segment["privacy_fallback_policy"] = (
                video_engine.PRIVACY_FALLBACK_STOP_POLICY)
            segment["privacy_fallback_policy_contract"] = {
                "file": policy_record,
                "policy": video_engine.PRIVACY_FALLBACK_STOP_POLICY,
                "scope": "formal_test_segment_only",
                "segment_id": "s1", "recovery_id": "recovery-1",
                "source_video_handoff_fingerprint": base_fp,
            }
            segment["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(segment)["fingerprint"])
            spec["privacy_fallback_policy_contract"] = {
                "file": policy_record,
                "policy": video_engine.PRIVACY_FALLBACK_STOP_POLICY,
                "scope": "formal_test_segment_only",
                "segment_id": "s1", "recovery_id": "recovery-1",
            }
            Path(segments_path).write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        handoff_record = run_manifest.file_record(segments_path)
        current_fp = segment["video_handoff_fingerprint"]
        current_payload = {"s1": current_fp}
        manifest = {
            "client": "acme", "run_id": "run-1",
            "requires_test_segment": True,
            "handoffs": {"video": {
                "path": segments_path, "file": handoff_record,
                "sha256": run_manifest._digest(json.dumps(
                    current_payload, ensure_ascii=False, sort_keys=True)),
                "segments": current_payload,
            }},
            "recovery_test_segment_requirement": {
                "reason": "empirical_integrated_audio_and_quality_canary",
                "segment_id": "s1",
                "video_handoff_fingerprint": current_fp,
                "recovery_id": "recovery-1",
                "contract": recovery_record,
            },
        }
        self.assertTrue(
            run_manifest.recovery_test_segment_requirement_is_current(manifest))
        return manifest, spec, segment, segments_path

    def test_qc_failed_provider_success_requires_exact_rejected_take_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, failure, failed_take, video_path = (
                self._failed_qc_fixture(tmp))
            contract = {"failure": failure}
            task = video_recovery._matching_failure_task(
                contract, manifest, {"segments": {"s1": "handoff-v7"}})
            self.assertEqual(task["task_id"], "task-qc-1")
            self.assertFalse(failed_take["resume_available"])
            self.assertTrue(failed_take["regeneration_required"])

            Path(video_path).write_bytes(b"tampered bytes")
            with self.assertRaisesRegex(
                    ValueError, "REJECTED_TAKE_EVIDENCE_INVALID"):
                video_recovery._matching_failure_task(
                    contract, manifest,
                    {"segments": {"s1": "handoff-v7"}})

    def test_qc_failed_success_is_never_reused_as_completed_take(self):
        manifest = {"tasks": [{
            "stage": "video", "unit_id": "s1",
            "task_id": "paid-task", "attempt": 1,
            "model": "seedance-exact-route", "status": "succeeded",
            "handoff_fingerprint": "handoff-v7",
            "media_qc_failed": True,
        }]}
        self.assertIsNone(video_engine._completed_task(
            manifest, "s1", "handoff-v7",
            model="seedance-exact-route"))

    def test_every_failed_recovery_segment_needs_its_own_attempt_and_output(self):
        segment = {
            "id": "s2", "model": "seedance-new-route",
            "video_handoff_fingerprint": "new-handoff",
            "recovery_contract": {"recovery_id": "recovery-1"},
        }
        manifest = {"tasks": [{
            "stage": "video", "unit_id": "s2",
            "task_id": "old-paid-task", "attempt": 2,
            "model": "old-route", "status": "succeeded",
            "handoff_fingerprint": "old-handoff",
            "media_qc_failed": True,
        }]}
        with self.assertRaisesRegex(
                ValueError, "RECOVERY_ATTEMPT_AUTHORIZATION_REQUIRED"):
            video_engine._validate_recovery_attempt_authorization(
                segment, manifest)
        manifest["video_attempts"] = {"s2": {
            "attempt": 3, "previous_take_fingerprint": "take-old"}}
        segment["output_artifact_contract"] = {
            "attempt": 3, "previous_take_fingerprint": "take-old"}
        self.assertTrue(
            video_engine._validate_recovery_attempt_authorization(
                segment, manifest))

    def test_empty_optional_recovery_fields_preserve_existing_v7_handoff(self):
        segment = {
            "client": "acme", "run_id": "run-1", "id": "s1",
            "text": "confirmed", "video_type": 5,
            "model": "seedance-exact-route",
            "recovery_contract": {"recovery_id": "recovery-1"},
        }
        original = artifact_contract.build_video_handoff(segment)
        explicit_empty = dict(
            segment, privacy_fallback_policy=None,
            privacy_fallback_policy_contract={})
        self.assertEqual(
            artifact_contract.build_video_handoff(
                explicit_empty)["fingerprint"],
            original["fingerprint"])
        isolated_output = dict(segment, output_artifact_contract={
            "mode": "attempt_specific_no_overwrite", "attempt": 3,
            "path": "/tmp/s1_attempt3_recovery_canary.mp4",
        })
        self.assertNotEqual(
            artifact_contract.build_video_handoff(
                isolated_output)["fingerprint"],
            original["fingerprint"])

    def test_recovery_recompile_changes_model_and_handoff_only_after_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment = {
                "client": "acme", "run_id": "run-1", "id": "s1",
                "text": "confirmed", "video_type": 4,
                "model": "seedance-old-route",
                "urls": ["https://example.invalid/identity.jpg"],
                "references": [{
                    "id": "identity-1", "tag": "@host",
                    "type": "character_identity",
                    "url": "https://example.invalid/identity.jpg",
                }],
                "chain_required": True,
            }
            old_fp = artifact_contract.build_video_handoff(segment)["fingerprint"]
            segment["video_handoff_fingerprint"] = old_fp
            manifest, failure, _failed_take, _video_path = (
                self._failed_qc_fixture(tmp, handoff=old_fp))
            segments_path = os.path.join(tmp, "segments.json")
            spec = {"segments": [segment]}
            Path(segments_path).write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            payload = {"s1": old_fp}
            manifest["handoffs"] = {"video": {
                "file": run_manifest.file_record(segments_path),
                "segments": payload,
                "sha256": run_manifest._digest(json.dumps(
                    payload, ensure_ascii=False, sort_keys=True)),
            }}
            contract_path = os.path.join(tmp, "recovery.json")
            contract = {
                "kind": "video_recovery_contract", "schema_version": 1,
                "client": "acme", "run_id": "run-1",
                "recovery_id": "recovery-2",
                "confirmed_by_customer": True,
                "confirmation_source": "customer_dialogue",
                "customer_confirmation": "我确认按独立身份锚策略先生成一段试片",
                "source": {
                    "handoff_sha256": manifest["handoffs"]["video"]["sha256"],
                    "segments_sha256": manifest["handoffs"]["video"]["file"]["sha256"],
                },
                "failure": failure,
                "strategy": {
                    "mode": "independent_identity_anchor",
                    "preserve_confirmed_identity_reference": True,
                    "tail_chain": False,
                    "target_model": "seedance-new-exact-route",
                    "video_type": 5,
                    "require_canary_before_batch": True,
                    "canary_segment_id": "s1",
                },
            }
            Path(contract_path).write_text(
                json.dumps(contract, ensure_ascii=False), encoding="utf-8")
            output = video_recovery.apply_contract(
                spec, contract_path, manifest,
                pick_video_model=lambda preferred, **_kwargs: preferred)
            recovered = output["segments"][0]
            self.assertEqual(recovered["model"], "seedance-new-exact-route")
            self.assertEqual(recovered["video_type"], 5)
            self.assertEqual(
                recovered["recovery_contract"]["target_video_type"], 5)
            self.assertFalse(recovered["chain_required"])
            self.assertNotEqual(
                recovered["video_handoff_fingerprint"], old_fp)
            self.assertTrue(
                artifact_contract.verify_video_handoff(recovered)["ok"])
            self.assertEqual(
                recovered["recovery_contract"]["recovery_id"], "recovery-2")
            self.assertTrue(output["video_recovery_contract"]["strategy"][
                "require_canary_before_batch"])

    def test_dynamic_recovery_canary_survives_workflow_contract_refresh_and_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, _spec, _segment, _segments_path = (
                self._recovery_state(tmp))
            plan_path = os.path.join(tmp, "storyboard_plan.json")
            Path(plan_path).write_text(json.dumps({
                "client": "acme", "run_id": "run-1",
                "package": "oral-broadcast", "command": "/oral-broadcast",
                "storyboard_role": "video_storyboard",
                "characters": [{"id": "host"}],
                "shots": [{"id": "s1", "duration": 5}],
            }, ensure_ascii=False), encoding="utf-8")
            manifest.update({
                "storyboard_plan": run_manifest.file_record(plan_path),
                "workflow_contract_version": "old",
                "chain_profile": {
                    "profile": "oral_broadcast", "frozen": True,
                    "contract_version": "old",
                },
            })
            run_manifest.ensure_workflow_contract(manifest)
            self.assertEqual(
                manifest["workflow_contract_version"],
                workflow_contract.CONTRACT_VERSION)
            self.assertTrue(manifest["requires_test_segment"])
            self.assertTrue(
                run_manifest.workflow_requirements(manifest)[
                    "requires_test_segment"])
            self.assertIn(
                "test_segment", run_manifest.generation_dependencies(
                    manifest, "video"))
            with mock.patch.object(
                    run_manifest, "stage_is_complete",
                    side_effect=lambda _manifest, stage: stage != "test_segment"):
                self.assertEqual(
                    run_manifest.next_required_stage(manifest), "test_segment")
                route = workflow_router._manifest_route(manifest)
            self.assertEqual(route["current_stage"], "test_segment")
            self.assertEqual(route["next_action"], "start_test_segment")

    def test_recovery_canary_output_preserves_rejected_take_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            prior_path = os.path.join(tmp, "failed-take.mp4")
            recovery_path = os.path.join(tmp, "recovery.json")
            Path(recovery_path).write_text(json.dumps({
                "kind": "video_recovery_contract", "schema_version": 1,
                "client": "acme", "run_id": "run-1",
                "recovery_id": "recovery-output-1",
            }), encoding="utf-8")
            recovery_record = run_manifest.file_record(recovery_path)
            segment = {
                "client": "acme", "run_id": "run-1", "id": "s1",
                "text": "confirmed", "video_type": 5,
                "model": "seedance-new-exact-route",
                "out_path": prior_path,
                "recovery_contract": {
                    "path": recovery_record["path"],
                    "sha256": recovery_record["sha256"],
                    "recovery_id": "recovery-output-1",
                    "mode": "independent_identity_anchor",
                    "target_model": "seedance-new-exact-route",
                },
            }
            handoff = artifact_contract.build_video_handoff(
                segment)["fingerprint"]
            segment["video_handoff_fingerprint"] = handoff
            manifest, failure, failed_take, _ = self._failed_qc_fixture(
                tmp, handoff=handoff)
            prior_sha = artifact_contract.file_sha256(prior_path)
            spec = {
                "client": "acme", "run_id": "run-1",
                "segments": [segment],
                "video_recovery_contract": {
                    "file": recovery_record,
                    "recovery_id": "recovery-output-1",
                    "failure": failure,
                    "strategy": {
                        "require_canary_before_batch": True,
                        "canary_segment_id": "s1",
                    },
                },
            }
            segments_path = os.path.join(tmp, "segments.json")
            Path(segments_path).write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            payload = {"s1": handoff}
            manifest.update({
                "requires_test_segment": True,
                "handoffs": {"video": {
                    "path": segments_path,
                    "file": run_manifest.file_record(segments_path),
                    "segments": payload,
                    "sha256": run_manifest._digest(json.dumps(
                        payload, ensure_ascii=False, sort_keys=True)),
                }},
                "recovery_test_segment_requirement": {
                    "segment_id": "s1",
                    "video_handoff_fingerprint": handoff,
                    "recovery_id": "recovery-output-1",
                    "contract": recovery_record,
                },
            })
            self.assertEqual(
                manifest["video_attempts"]["s1"][
                    "previous_take_fingerprint"],
                failed_take["take_fingerprint"])
            committed = {}

            def commit(_manifest, _manifest_path, _segments_path, updated):
                committed["spec"] = updated

            result = video_recovery.bind_canary_output(
                manifest, "manifest.json", segments_path,
                commit_handoff=commit)
            updated = committed["spec"]["segments"][0]
            output_contract = updated["output_artifact_contract"]
            self.assertFalse(os.path.exists(result["out_path"]))
            self.assertNotEqual(result["out_path"], prior_path)
            self.assertEqual(
                artifact_contract.file_sha256(prior_path), prior_sha)
            self.assertEqual(
                output_contract["prior_take_sha256"], prior_sha)
            self.assertEqual(output_contract["attempt"], 2)
            self.assertTrue(
                output_contract["must_not_exist_before_generation"])
            self.assertNotEqual(
                updated["video_handoff_fingerprint"], handoff)
            self.assertTrue(
                artifact_contract.verify_video_handoff(updated)["ok"])

            Path(result["out_path"]).write_bytes(b"collision")
            with self.assertRaisesRegex(
                    ValueError, "RECOVERY_CANARY_OUTPUT_ALREADY_EXISTS"):
                video_recovery.bind_canary_output(
                    manifest, "manifest.json", segments_path,
                    commit_handoff=commit)

    def test_no_media_failure_gets_new_attempt_specific_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, spec, segment, segments_path = self._recovery_state(tmp)
            failed_path = os.path.join(tmp, "s1_attempt3.mp4")
            segment["out_path"] = failed_path
            segment["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(segment)["fingerprint"])
            spec["segments"] = [segment]
            Path(segments_path).write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            old_fp = segment["video_handoff_fingerprint"]
            manifest["handoffs"]["video"].update({
                "file": run_manifest.file_record(segments_path),
                "segments": {"s1": old_fp},
            })
            manifest["recovery_test_segment_requirement"].update({
                "video_handoff_fingerprint": old_fp,
            })
            self.assertTrue(
                run_manifest.recovery_test_segment_requirement_is_current(
                    manifest))
            manifest.update({
                "video_attempts": {"s1": {"attempt": 3}},
                "tasks": [{
                    "stage": "video", "unit_id": "s1",
                    "task_id": "task-3", "attempt": 3,
                    "model": "seedance-exact-route", "status": "failed",
                    "handoff_fingerprint": old_fp,
                }],
            })
            result = {
                "ok": False, "segment_id": "s1", "taskId": "task-3",
                "video_handoff_fingerprint": old_fp,
                "error": "PROVIDER_TERMINAL_FAILURE: no media",
                "resume_available": False,
            }
            results_path = os.path.join(tmp, "failed-results.json")
            Path(results_path).write_text(
                json.dumps([result], ensure_ascii=False), encoding="utf-8")
            result_record = run_manifest.file_record(results_path)
            manifest["generation"] = {"test_segment": {
                "status": "failed_pending_recovery",
                "results": result_record, "segment_id": "s1",
                "task_id": "task-3",
                "video_handoff_fingerprint": old_fp,
            }}
            authorization = run_manifest.request_video_attempt(
                manifest, "s1", actor="customer", reason="retry exact canary",
                failure_results=results_path)
            self.assertEqual(authorization["attempt"], 4)
            self.assertTrue(run_manifest.video_attempt_failure_evidence_is_current(
                manifest, "s1", authorization))

            def commit(current, _manifest_path, current_segments_path, updated):
                Path(current_segments_path).write_text(
                    json.dumps(updated, ensure_ascii=False), encoding="utf-8")
                updated_segment = updated["segments"][0]
                new_fp = updated_segment["video_handoff_fingerprint"]
                current["handoffs"]["video"].update({
                    "file": run_manifest.file_record(current_segments_path),
                    "segments": {"s1": new_fp},
                })
                current["recovery_test_segment_requirement"].update({
                    "video_handoff_fingerprint": new_fp,
                })

            bound = video_recovery.bind_attempt_output(
                manifest, "manifest.json", segments_path,
                commit_handoff=commit, segment_id="s1",
                failure_results_path=results_path)
            updated = bound["segments"]["segments"][0]
            output = updated["output_artifact_contract"]
            self.assertEqual(output["evidence_type"],
                             "settled_provider_failure")
            self.assertEqual(output["previous_task_id"], "task-3")
            self.assertEqual(output["failure_results"], result_record)
            self.assertNotEqual(bound["out_path"], failed_path)
            self.assertFalse(os.path.exists(bound["out_path"]))
            self.assertTrue(
                artifact_contract.verify_video_handoff(updated)["ok"])
            with mock.patch.object(
                    run_manifest, "require_run_output", return_value=True):
                self.assertTrue(video_engine._validate_recovery_output_contract(
                    updated, manifest=manifest,
                    formal_stage="test_segment"))
            self.assertTrue(
                video_engine._validate_recovery_attempt_authorization(
                    updated, manifest))

    def test_submit_time_failure_without_task_id_can_authorize_exact_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            handoff = "handoff-submit-rejected"
            results_path = os.path.join(tmp, "failed-results.json")
            result = {
                "ok": False, "segment_id": "s1", "taskId": None,
                "video_handoff_fingerprint": handoff,
                "error": "InputImageSensitiveContentDetected",
                "resume_available": False,
            }
            Path(results_path).write_text(
                json.dumps([result], ensure_ascii=False), encoding="utf-8")
            result_record = run_manifest.file_record(results_path)
            manifest = {
                "handoffs": {"video": {"segments": {"s1": handoff}}},
                "video_attempts": {"s1": {"attempt": 2}},
                "tasks": [{
                    "stage": "video", "unit_id": "s1", "attempt": 2,
                    "model": "seedance-2.5-white", "status": "failed",
                    "task_id": None, "request_id": "video-request-2",
                    "handoff_fingerprint": handoff,
                }],
                "generation": {"test_segment": {
                    "status": "failed_pending_recovery",
                    "results": result_record, "segment_id": "s1",
                    "task_id": None,
                    "video_handoff_fingerprint": handoff,
                }},
            }
            authorization = run_manifest.request_video_attempt(
                manifest, "s1", actor="customer",
                reason="switch exact route",
                failure_results=results_path)
            self.assertEqual(authorization["attempt"], 3)
            self.assertIsNone(authorization["previous_task_id"])
            self.assertTrue(
                run_manifest.video_attempt_failure_evidence_is_current(
                    manifest, "s1", authorization))

    def test_stop_only_privacy_policy_is_exact_canary_preflight_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, _spec, segment, _segments_path = (
                self._recovery_state(tmp, bind_policy=True))
            self.assertTrue(video_engine._validate_privacy_stop_policy(
                segment, manifest=manifest, formal_stage="test_segment"))
            with self.assertRaisesRegex(
                    ValueError, "PRIVACY_FALLBACK_POLICY_NOT_AUTHORIZED"):
                video_engine._validate_privacy_stop_policy(
                    segment, manifest=manifest, formal_stage="video")

            catalog = video_models._normalize_model_catalog([{
                "modelId": "seedance-exact-id",
                "modelName": "seedance-exact-route",
                "online": True, "status": "active",
                "allowVideoType": [5], "imageCount": 4,
                "integratedAudio": True,
            }, {
                "modelId": "kling-no-audio-id",
                "modelName": "3.0-Omni",
                "online": True, "status": "active",
                "allowVideoType": [5], "imageCount": 1,
                "integratedAudio": False,
            }])
            preflight = video_engine.inspect_submission_requirements(
                [segment], "seedance-exact-route", formal=True,
                catalog=catalog, manifest=manifest,
                formal_stage="test_segment",
                allow_unproven_audio_canary=False)
            self.assertTrue(preflight["ready"])
            row = preflight["segments"][0]
            self.assertEqual(
                row["privacy_fallback_action"],
                video_engine.PRIVACY_FALLBACK_STOP_POLICY)
            self.assertIsNone(row["privacy_fallback_model"])
            self.assertTrue(row["privacy_fallback_policy_ready"])

            without_policy = dict(segment)
            without_policy.pop("privacy_fallback_policy")
            without_policy.pop("privacy_fallback_policy_contract")
            without_policy["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(
                    without_policy)["fingerprint"])
            blocked = video_engine.inspect_submission_requirements(
                [without_policy], "seedance-exact-route", formal=True,
                catalog=catalog, allow_unproven_audio_canary=False)
            self.assertFalse(blocked["ready"])
            self.assertTrue(any(
                item["code"] in {
                    "NO_PRIVACY_FALLBACK_MODEL",
                    "PRIVACY_FALLBACK_CAPACITY_UNAVAILABLE",
                }
                for item in blocked["blockers"]))

    def test_provider_privacy_rejection_stops_without_second_paid_task(self):
        segment = {
            "id": "s1",
            "privacy_fallback_policy": (
                video_engine.PRIVACY_FALLBACK_STOP_POLICY),
        }
        task = {
            "idx": 0, "task_id": "seedance-paid-task",
            "segment": segment, "model": "seedance-exact-route",
            "initial_model": "seedance-exact-route", "video_type": 5,
            "ref_urls": ["https://example.invalid/identity.jpg"],
            "handoff_fingerprint": "handoff-v7",
            "fallback_reason": None,
        }
        context = {
            "api_key": "test", "poll_err_count": {},
            "allow_model_fallback": True, "draft": False,
            "manifest": {}, "manifest_path": None, "ledger_path": None,
            "results": [None], "pending": [task],
            "log": lambda _message: None,
        }
        rejection = br_client.BRVideoReferencePrivacyError(
            "REFERENCE_REAL_PERSON_PRIVACY")
        with mock.patch.object(
                video_engine.br_client, "get_video", side_effect=rejection), \
                mock.patch.object(video_engine, "_persist_task") as persist, \
                mock.patch.object(
                    video_engine, "_prepare_privacy_fallback_submission") as fallback:
            self.assertIsNone(
                video_engine._poll_batch_task_info(context, task))
        result = context["results"][0]
        self.assertFalse(result["ok"])
        self.assertFalse(result["resume_available"])
        self.assertTrue(result["regeneration_required"])
        self.assertFalse(result["privacy_fallback_attempted"])
        self.assertEqual(context["pending"], [])
        fallback.assert_not_called()
        persist.assert_called_once()
        self.assertFalse(
            persist.call_args.kwargs["privacy_fallback_attempted"])
        self.assertEqual(
            video_engine._test_segment_failure_contract(result), {
                "error_code": "TEST_SEGMENT_PRIVACY_STOPPED",
                "retryable": False,
            })

    def test_failed_test_segment_settlement_clears_lease_and_routes_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, _spec, segment, _segments_path = (
                self._recovery_state(tmp, bind_policy=True))
            manifest["generation"] = {"test_segment": {
                "status": "running",
                "lease": {"owner_token": "worker-1"},
            }}
            result = video_engine._privacy_stop_result({
                "task_id": "canary-task-1",
                "model": "seedance-exact-route",
                "initial_model": "seedance-exact-route",
                "handoff_fingerprint": segment[
                    "video_handoff_fingerprint"],
            }, segment, br_client.BRVideoReferencePrivacyError(
                "InputImageSensitiveContentDetected"))
            results = [result]
            results_path = os.path.join(tmp, "test-segment-results.json")
            Path(results_path).write_text(
                json.dumps(results, ensure_ascii=False), encoding="utf-8")
            settlement = run_manifest.settle_test_segment_failure(
                manifest, results_path, results)
            generation = manifest["generation"]["test_segment"]
            self.assertEqual(
                generation["status"], "failed_pending_recovery")
            self.assertNotIn("lease", generation)
            self.assertEqual(
                manifest["status"], "needs_test_segment_recovery")
            self.assertEqual(
                settlement["error_code"], "TEST_SEGMENT_PRIVACY_STOPPED")
            self.assertFalse(settlement["privacy_fallback_attempted"])
            self.assertFalse(settlement["retryable"])
            self.assertEqual(
                run_manifest.settle_test_segment_failure(
                    manifest, results_path, results)["fingerprint"],
                settlement["fingerprint"])
            with mock.patch.object(
                    run_manifest, "next_required_stage",
                    return_value="test_segment"):
                route = workflow_router._manifest_route(manifest)
            self.assertEqual(route["next_action"], "recover_test_segment")
            with mock.patch.object(
                    run_manifest, "next_required_stage",
                    return_value="test_segment"):
                contract = workflow_router.stage_contract(
                    {"manifest": manifest}, "test_segment")
            expected_result = os.path.abspath(results_path)
            self.assertIn(expected_result, contract["preview_sources"])
            self.assertIn(expected_result, contract["source_artifacts"])

    def test_approved_audio_canary_promotes_atomically_and_reuses_paid_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, spec, segment, segments_path = self._recovery_state(
                tmp, bind_policy=True)
            recovery_record = manifest[
                "recovery_test_segment_requirement"]["contract"]
            video_path = os.path.join(tmp, "approved-canary.mp4")
            Path(video_path).write_bytes(b"approved canary media")
            segment["out_path"] = video_path
            segment["output_artifact_contract"] = {
                "mode": "attempt_specific_no_overwrite",
                "evidence_type": "rejected_take",
                "attempt": 1, "path": video_path,
                "must_not_exist_before_generation": True,
                "prior_take_path": "", "prior_take_sha256": None,
                "target_model": "seedance-exact-route",
            }
            segment["audio_capability_policy"] = (
                video_engine.EMPIRICAL_AUDIO_CANARY_POLICY)
            segment["audio_capability_policy_contract"] = {
                "file": recovery_record,
                "policy": video_engine.EMPIRICAL_AUDIO_CANARY_POLICY,
                "scope": "formal_test_segment_only",
                "segment_id": "s1", "recovery_id": "recovery-1",
            }
            segment["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(segment)["fingerprint"])
            second = copy.deepcopy(segment)
            second.update({"id": "s2", "scene_id": "scene-2",
                           "text": "第二段正式批次台词",
                           "dialogue": "第二段正式批次台词"})
            for key in (
                    "audio_capability_policy",
                    "audio_capability_policy_contract",
                    "privacy_fallback_policy",
                    "privacy_fallback_policy_contract",
                    "output_artifact_contract", "out_path"):
                second.pop(key, None)
            second["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(second)["fingerprint"])
            spec["segments"] = [segment, second]
            Path(segments_path).write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            old_fp = segment["video_handoff_fingerprint"]
            old_segments = {
                "s1": old_fp,
                "s2": second["video_handoff_fingerprint"],
            }
            manifest["handoffs"]["video"] = {
                "path": segments_path,
                "file": run_manifest.file_record(segments_path),
                "segments": old_segments,
                "sha256": run_manifest._digest(json.dumps(
                    old_segments, ensure_ascii=False, sort_keys=True)),
            }
            manifest["recovery_test_segment_requirement"].update({
                "video_handoff_fingerprint": old_fp,
            })
            results_path = os.path.join(tmp, "canary-results.json")
            review_path = os.path.join(tmp, "canary-review.json")
            Path(results_path).write_text("[]", encoding="utf-8")
            Path(review_path).write_text(json.dumps({
                "segment_id": "s1", "decision": {"verdict": "accepted"},
            }), encoding="utf-8")
            media_qc = {
                "passed": True,
                "media": {"audio": {"stream_count": 1}},
                "checks": {
                    "audio_track": {"passed": True},
                    "silence": {"passed": True},
                },
            }
            manifest.update({
                "requires_audio": True,
                "requires_test_segment": True,
                "test_segment_artifact": {
                    "status": "approved", "segment_id": "s1",
                    "take_fingerprint": "take-canary-1",
                    "model_used": "seedance-exact-route",
                    "audio_capability_policy":
                        video_engine.EMPIRICAL_AUDIO_CANARY_POLICY,
                    "audio_capability_policy_contract": copy.deepcopy(
                        segment["audio_capability_policy_contract"]),
                    "video_handoff_fingerprint": old_fp,
                    "results": run_manifest.file_record(results_path),
                    "video": run_manifest.file_record(video_path),
                    "review": run_manifest.file_record(review_path),
                    "media_qc": media_qc,
                },
                "tasks": [{
                    "stage": "video", "unit_id": "s1",
                    "task_id": "paid-canary-task", "attempt": 1,
                    "model": "seedance-exact-route", "status": "succeeded",
                    "handoff_fingerprint": old_fp,
                    "take_fingerprint": "take-canary-1",
                }],
            })

            def commit(current, _manifest_path, current_segments_path,
                       updated, manifest_mutator=None):
                previous = json.loads(json.dumps(current))
                Path(current_segments_path).write_text(
                    json.dumps(updated, ensure_ascii=False), encoding="utf-8")
                payload = {
                    item["id"]: item["video_handoff_fingerprint"]
                    for item in updated["segments"]}
                current.setdefault("handoffs", {})["video"] = {
                    "path": current_segments_path,
                    "file": run_manifest.file_record(current_segments_path),
                    "segments": payload,
                    "sha256": run_manifest._digest(json.dumps(
                        payload, ensure_ascii=False, sort_keys=True)),
                }
                current.pop("recovery_test_segment_requirement", None)
                if manifest_mutator:
                    manifest_mutator(current, previous)

            with mock.patch.object(
                    run_manifest, "approval_is_current", return_value=True), \
                    mock.patch.object(
                        run_manifest.take_review, "is_accepted",
                        return_value=True), \
                    mock.patch.object(
                        run_manifest, "ocr_take_is_clear_or_waived",
                        return_value=True), \
                    mock.patch.object(
                        run_manifest, "require_run_output",
                        return_value=True):
                with self.assertRaisesRegex(
                        ValueError, "PRIVACY_FALLBACK_POLICY_NOT_AUTHORIZED"):
                    video_engine._validate_privacy_stop_policy(
                        segment, manifest=manifest, formal_stage="video")
                with mock.patch.object(
                        run_manifest, "next_required_stage",
                        return_value="video"), \
                        mock.patch.object(
                            run_manifest, "ensure_workflow_contract",
                            side_effect=lambda value: value):
                    self.assertEqual(
                        workflow_router._manifest_route(manifest)[
                            "next_action"],
                        "promote_audio_canary_for_batch")
                promoted = video_recovery.promote_audio_canary_to_batch(
                    manifest, "manifest.json", segments_path,
                    commit_handoff=commit)
                promoted_by_id = {
                    item["id"]: item
                    for item in promoted["segments"]["segments"]}
                promoted_segment = promoted_by_id["s1"]
                new_fp = promoted_segment["video_handoff_fingerprint"]
                self.assertNotEqual(new_fp, old_fp)
                self.assertNotIn(
                    "audio_capability_policy", promoted_segment)
                self.assertEqual(
                    promoted_segment["privacy_fallback_policy"],
                    video_engine.PRIVACY_FALLBACK_STOP_POLICY)
                self.assertTrue(promoted_segment["audio_canary_proof"])
                self.assertEqual(
                    promoted_by_id["s2"]["audio_canary_proof"],
                    promoted_segment["audio_canary_proof"])
                self.assertEqual(
                    promoted_by_id["s2"]["model"],
                    "seedance-exact-route")
                self.assertNotIn(
                    "output_artifact_contract", promoted_by_id["s2"])
                self.assertTrue(
                    video_engine._validate_privacy_stop_policy(
                        promoted_segment, manifest=manifest,
                        formal_stage="video"))
                completed = video_engine._completed_task(
                    manifest, "s1", new_fp,
                    model="seedance-exact-route")
                self.assertEqual(completed["task_id"], "paid-canary-task")
                self.assertEqual(
                    completed["promoted_from_handoff"], old_fp)
                reuse = promoted_segment["output_artifact_contract"]
                self.assertEqual(
                    reuse["mode"], "approved_canary_exact_media_reuse")
                self.assertEqual(
                    reuse["sha256"],
                    artifact_contract.file_sha256(video_path))
                self.assertTrue(
                    video_engine._validate_recovery_output_contract(
                        promoted_segment, manifest=manifest,
                        formal_stage="video"))
                with mock.patch.object(
                        video_engine, "_download_completed_video") as download:
                    reused_path = video_engine._download_completed_video_guarded(
                        "unused", "paid-canary-task",
                        "https://example.invalid/canary.mp4", video_path,
                        segment=promoted_segment, task=completed)
                self.assertEqual(reused_path, video_path)
                download.assert_not_called()
                self.assertTrue(
                    run_manifest.validate_test_segment_closure(manifest))
                catalog = video_models._normalize_model_catalog([{
                    "modelId": "seedance-exact-id",
                    "modelName": "seedance-exact-route",
                    "online": True, "status": "active",
                    "allowVideoType": [5], "imageCount": 4,
                    "inputModalities": ["text", "image", "audio"],
                    "outputModalities": ["video"],
                }])
                preflight = video_engine.inspect_submission_requirements(
                    [promoted_segment], "seedance-exact-route",
                    formal=True, catalog=catalog, manifest=manifest,
                    formal_stage="video",
                    proven_audio_models=("seedance-exact-route",))
                self.assertTrue(preflight["ready"], preflight["blockers"])
                self.assertTrue(
                    preflight["segments"][0]["integrated_audio"])
                self.assertEqual(
                    preflight["segments"][0]["privacy_fallback_action"],
                    video_engine.PRIVACY_FALLBACK_STOP_POLICY)
                again = video_recovery.promote_audio_canary_to_batch(
                    manifest, "manifest.json", segments_path,
                    commit_handoff=commit)
                self.assertTrue(again["already_promoted"])

                # A paid retry for another segment changes the aggregate
                # handoff, but must not invalidate unchanged canary media.
                retry_spec = copy.deepcopy(promoted["segments"])
                retry_s2 = next(
                    item for item in retry_spec["segments"]
                    if item["id"] == "s2")
                retry_s2["out_path"] = os.path.join(tmp, "s2-attempt2.mp4")
                retry_s2["video_handoff_fingerprint"] = (
                    artifact_contract.build_video_handoff(retry_s2)[
                        "fingerprint"])
                commit(manifest, "manifest.json", segments_path, retry_spec)
                with self.assertRaisesRegex(
                        ValueError, "TEST_SEGMENT_HANDOFF_STALE"):
                    run_manifest.validate_test_segment_closure(manifest)
                refreshed = video_recovery.promote_audio_canary_to_batch(
                    manifest, "manifest.json", segments_path,
                    commit_handoff=commit)
                self.assertTrue(refreshed["already_promoted"])
                self.assertTrue(refreshed["refreshed"])
                self.assertEqual(
                    refreshed["promotion"]["target"]["segments"]["s2"],
                    retry_s2["video_handoff_fingerprint"])
                self.assertTrue(
                    run_manifest.validate_test_segment_closure(manifest))

                Path(video_path).write_bytes(b"tampered canary media")
                with self.assertRaisesRegex(
                        ValueError, "TEST_SEGMENT_ARTIFACT_STALE"):
                    run_manifest.validate_test_segment_closure(manifest)

    def test_handoff_manifest_mutator_failure_leaves_old_file_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            final_path = os.path.join(tmp, "segments.json")
            manifest_path = os.path.join(tmp, "manifest.json")
            Path(final_path).write_text(
                '{"version":"old"}', encoding="utf-8")
            manifest = {"revision": 4, "sentinel": "unchanged"}
            original = copy.deepcopy(manifest)

            def register(candidate, _spec, staged_path):
                candidate["handoffs"] = {"video": {
                    "path": staged_path,
                    "file": run_manifest.file_record(staged_path),
                    "segments": {"s1": "new"}, "sha256": "aggregate",
                }}

            def reject(_candidate, _previous):
                raise ValueError("PROMOTION_MUTATOR_REJECTED")

            with mock.patch.object(
                    script_splitter.rm, "record_video_handoff",
                    side_effect=register), \
                    mock.patch.object(
                        script_splitter.rm, "save_manifest") as save:
                with self.assertRaisesRegex(
                        ValueError, "PROMOTION_MUTATOR_REJECTED"):
                    script_splitter._commit_video_handoff(
                        manifest, manifest_path, final_path,
                        {"segments": [{"id": "s1"}]},
                        manifest_mutator=reject)
            self.assertEqual(manifest, original)
            self.assertEqual(
                Path(final_path).read_text(encoding="utf-8"),
                '{"version":"old"}')
            save.assert_not_called()
            self.assertFalse(any(
                name.startswith(".segments.json.handoff-")
                for name in os.listdir(tmp)))

    def test_high_frequency_recovery_cli_outputs_bounded_summary(self):
        giant = "DO_NOT_ECHO_" + ("x" * 200000)
        base_manifest = {
            "run_id": "run-1", "revision": 17,
            "status": "needs_video_generation",
            "huge_internal_evidence": giant,
            "generation": {"video": {"status": "authorized_retry"}},
        }

        def load_manifest(_path):
            return json.loads(json.dumps(base_manifest))

        def authorize(manifest, segment_id, **_kwargs):
            manifest["generation"]["video"].update({
                "status": "authorized_retry", "segment_id": segment_id})

        service = types.SimpleNamespace(
            STAGES=run_manifest.STAGES,
            STABLE_VISUAL_BOARD_STAGES=run_manifest.STABLE_VISUAL_BOARD_STAGES,
            SPECIALIZED_FINISH_STAGES=run_manifest.SPECIALIZED_FINISH_STAGES,
            load_manifest=load_manifest,
            save_manifest=lambda *_args, **_kwargs: None,
            request_video_attempt=authorize,
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_manifest_cli.main(service, [
                "new-video-attempt", "--manifest", "manifest.json",
                "--segment-id", "s1", "--actor", "customer",
                "--reason", "客户拒绝旧 take",
            ])
        rendered = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertLess(len(rendered), 2000)
        self.assertNotIn("DO_NOT_ECHO_", rendered)
        summary = json.loads(rendered)
        self.assertEqual(summary["segment_id"], "s1")

        with tempfile.TemporaryDirectory() as tmp:
            results_path = os.path.join(tmp, "results.json")
            Path(results_path).write_text("[]", encoding="utf-8")
            settled = load_manifest("manifest.json")
            settled["status"] = "needs_test_segment_recovery"
            settled["generation"] = {"test_segment": {
                "status": "failed_pending_recovery",
                "segment_id": "s1", "task_id": "task-1",
                "error_code": "TEST_SEGMENT_PRIVACY_STOPPED",
            }}
            service.record_test_segment_failure_settlement = (
                lambda *_args, **_kwargs: (
                    settled, {"fingerprint": "settlement"}))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = run_manifest_cli.main(service, [
                    "settle-test-segment-failure",
                    "--manifest", "manifest.json",
                    "--results", results_path,
                ])
            rendered = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertLess(len(rendered), 2000)
            self.assertNotIn("DO_NOT_ECHO_", rendered)
            summary = json.loads(rendered)
            self.assertEqual(
                summary["generation_status"],
                "failed_pending_recovery")


if __name__ == "__main__":
    unittest.main()
