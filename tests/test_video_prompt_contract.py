import json
import hashlib
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import prompt_review
import prompt_review_contract
import video_prompts
import panel_expansion
import video_engine
import video_engine_cli
import video_guard
import video_models
import br_client
import artifact_contract
import product_library
import run_manifest
import script_splitter


def _confirmed_review(payload):
    review = dict(payload)
    review.update({
        "status": "confirmed",
        "confirmed_by_customer": True,
        "customer_confirmation": "我确认以上每个模型的完整提示词和参考图",
        "confirmation_source": "customer_dialogue",
        "confirmed_at": "2026-08-18T12:00:00",
    })
    review["review_fingerprint"] = prompt_review_contract.review_fingerprint(review)
    return review


class VideoPromptContractTests(unittest.TestCase):
    def test_prompt_capture_can_scope_exact_registered_test_segment(self):
        segments = [{"id": "s1"}, {"id": "s2"}]
        self.assertEqual(
            prompt_review._select_capture_segments(segments, "s2"),
            [{"id": "s2"}])
        with self.assertRaisesRegex(ValueError, "MATCH_EXACTLY_ONE"):
            prompt_review._select_capture_segments(segments, "missing")

    def test_recovery_privacy_template_finalizes_post_recovery_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            recovery_path = Path(tmp, "recovery.json")
            recovery_path.write_text(json.dumps({
                "kind": "video_recovery_contract",
                "recovery_id": "recovery-finalize-1",
            }), encoding="utf-8")
            recovery_record = run_manifest.file_record(str(recovery_path))
            segment = {
                "client": "unit", "run_id": "run", "id": "s1",
                "model": "ep-route", "video_type": 5,
                "urls": ["https://example.invalid/identity.png"],
                "references": [{
                    "type": "character_identity",
                    "url": "https://example.invalid/identity.png",
                }],
                "audio_contract": {"dialogue": "中文测试口播"},
                "recovery_contract": {
                    "path": recovery_record["path"],
                    "sha256": recovery_record["sha256"],
                    "recovery_id": "recovery-finalize-1",
                },
            }
            fingerprint = artifact_contract.build_video_handoff(
                segment)["fingerprint"]
            segment["video_handoff_fingerprint"] = fingerprint
            spec = {
                "client": "unit", "run_id": "run", "segments": [segment],
            }
            segments_path = Path(tmp, "segments.json")
            segments_path.write_text(
                json.dumps(spec, ensure_ascii=False, indent=2),
                encoding="utf-8")
            segments_record = run_manifest.file_record(str(segments_path))
            payload = {"s1": fingerprint}
            aggregate = hashlib.sha256(json.dumps(
                payload, ensure_ascii=False,
                sort_keys=True).encode()).hexdigest()
            manifest = {
                "client": "unit", "run_id": "run",
                "requires_test_segment": True,
                "handoffs": {"video": {
                    "file": segments_record,
                    "sha256": aggregate,
                    "segments": payload,
                }},
                "recovery_test_segment_requirement": {
                    "segment_id": "s1",
                    "recovery_id": "recovery-finalize-1",
                    "video_handoff_fingerprint": fingerprint,
                    "contract": recovery_record,
                },
            }
            template_path = Path(tmp, "privacy-template.json")
            template_path.write_text(json.dumps({
                "schema_version": 1,
                "kind": "privacy_fallback_policy_template",
                "template_status": "awaiting_post_recovery_handoff_binding",
                "policy": "stop_without_paid_fallback",
                "scope": "formal_test_segment_only",
                "client": "unit", "run_id": "run", "segment_id": "s1",
                "recovery_id": "recovery-finalize-1",
                "target_model": "ep-route",
                "action": "stop and do not submit a paid fallback",
                "confirmed_by_customer": True,
                "confirmation_source": "customer_dialogue",
                "customer_confirmation": "直接确认并保证质量",
            }, ensure_ascii=False), encoding="utf-8")
            final_path = Path(tmp, "privacy-final.json")
            record = script_splitter.finalize_recovery_privacy_policy_contract(
                manifest, str(segments_path), str(template_path),
                str(final_path))
            self.assertTrue(run_manifest.file_record_is_current(record))
            contract = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(
                contract["source_video_handoff_fingerprint"], fingerprint)
            self.assertEqual(
                contract["source_segments_sha256"],
                segments_record["sha256"])
            self.assertEqual(
                contract["source_aggregate_handoff_sha256"], aggregate)

            with mock.patch.object(
                    script_splitter, "_commit_video_handoff") as commit:
                updated = script_splitter.bind_recovery_privacy_policy(
                    manifest, str(Path(tmp, "manifest.json")),
                    str(segments_path), str(final_path))
            commit.assert_called_once()
            canary = updated["segments"][0]
            self.assertEqual(
                canary["privacy_fallback_policy"],
                "stop_without_paid_fallback")
            self.assertNotEqual(
                canary["video_handoff_fingerprint"], fingerprint)

            stale_path = Path(tmp, "privacy-pre-recovery-invalid.json")
            stale_path.write_text(json.dumps({
                **contract,
                "do_not_bind": True,
                "status": "superseded_invalid_pre_recovery_binding",
                "source_video_handoff_fingerprint": None,
                "source_segments_sha256": "0" * 64,
                "source_aggregate_handoff_sha256": None,
            }), encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "RECOVERY_PRIVACY_POLICY_CONTRACT_INVALID"):
                script_splitter.bind_recovery_privacy_policy(
                    manifest, str(Path(tmp, "manifest.json")),
                    str(segments_path), str(stale_path))

    @staticmethod
    def _privacy_catalog(kling_image_count=1):
        return video_models._normalize_model_catalog([{
            "modelId": "seedance-active", "modelName": "seedance-2.0",
            "online": True, "status": "active", "allowVideoType": [2, 5],
            "imageCount": 4, "integratedAudio": True,
        }, {
            "modelId": "kling-v3-omni-video", "modelName": "Kling old",
            "online": False, "status": "inactive", "allowVideoType": [2, 5],
            "imageCount": 4, "integratedAudio": True,
        }, {
            "modelId": "kling-v3-omni-video-white",
            "modelName": "3.0-Omni", "aliases": ["Kling active white"],
            "online": True, "status": "active", "allowVideoType": [2, 5],
            "imageCount": kling_image_count, "integratedAudio": True,
        }])

    def test_live_catalog_family_resolution_uses_active_kling_route_not_dead_alias(self):
        catalog = self._privacy_catalog(kling_image_count=1)
        selected = video_engine._pick_video_model(
            "kling-v3-omni-video", video_type=5,
            dialogue="已确认中文台词", formal=True,
            allow_fallback=False, reference_count=1, catalog=catalog)
        self.assertEqual(selected, "3.0-Omni")
        self.assertEqual(
            video_engine._pick_privacy_fallback_model(
                5, "已确认中文台词", formal=True,
                reference_count=1, catalog=catalog),
            "3.0-Omni")

    def test_input_modalities_cannot_claim_embedded_output_audio(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "dreamina-seedance-2-0-260128",
            "modelName": "seedance-2.0-VS-white",
            "online": True, "status": "active",
            "allowVideoType": [2, 5], "imageCount": 4,
            "multimodelTypes": ["image", "video", "text"],
        }, {
            "modelId": "kling-unknown-audio-route",
            "modelName": "Unknown Omni Route",
            "online": True, "status": "active",
            "allowVideoType": [2, 5], "imageCount": 1,
            "multimodelTypes": ["image", "video", "text", "audio"],
        }])
        seedance = catalog["records"]["dreamina-seedance-2-0-260128"]
        self.assertIs(seedance["integrated_audio"], False)
        kling = catalog["records"]["kling-unknown-audio-route"]
        self.assertIsNone(kling["integrated_audio"])
        self.assertEqual(
            kling["audio_evidence"]["source"], "input_modalities_only")
        with self.assertRaisesRegex(ValueError, "NO_CAPABLE_VIDEO_MODEL"):
            video_engine._pick_video_model(
                "seedance-2.0", video_type=5, dialogue="已确认台词",
                formal=True, reference_count=1, catalog=catalog)

    def test_video_output_type_alone_does_not_disprove_embedded_audio(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "kling-unknown-audio-route",
            "modelName": "Unknown Omni Route", "online": True, "status": "active",
            "allowVideoType": [5], "imageCount": 1,
            "multimodelTypes": ["image", "video", "text", "audio"],
            "ouputMultiType": ["video"],
        }])
        record = catalog["records"]["kling-unknown-audio-route"]
        self.assertIsNone(record["integrated_audio"])
        self.assertEqual(record["audio_evidence"]["source"],
                         "input_modalities_only")
        self.assertIsNone(video_engine._model_integrated_audio("3.0-Omni"))
        result = video_engine.inspect_submission_requirements([{
            "id": "s1", "video_type": 5,
            "urls": ["https://example.invalid/identity.png"],
            "audio_contract": {"track": "required", "dialogue": "台词"},
            "model": "Unknown Omni Route",
        }], preferred_model="Unknown Omni Route", formal=True,
            allow_model_fallback=False, catalog=catalog)
        self.assertFalse(result["ready"])
        self.assertEqual(
            result["contract_version"],
            video_engine.VIDEO_PREFLIGHT_CONTRACT_VERSION)
        self.assertTrue(any(
            blocker["code"] == "NO_CAPABLE_VIDEO_MODEL"
            for blocker in result["blockers"]))

    def test_exact_kling_white_route_is_disproved_for_embedded_audio(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "3.0-Omni",
            "modelName": "kling-v3-omni-video-white",
            "provider": "Kling", "online": True, "status": "active",
            "allowVideoType": [1, 2, 3, 4, 5], "imageCount": 1,
            "resolution": ["720p", "1080p"],
            "ratio": ["16:9"],
            "multimodelTypes": ["image", "video", "text", "audio"],
            "ouputMultiType": ["video"],
        }])
        with self.assertRaisesRegex(ValueError, "NO_CAPABLE_VIDEO_MODEL"):
            video_engine._pick_video_model(
                "kling-v3-omni-video-white", video_type=4,
                dialogue="已确认台词", formal=True,
                allow_fallback=False, reference_count=1, catalog=catalog)
        integrated, evidence = video_engine._resolved_model_audio_evidence(
            "kling-v3-omni-video-white", catalog)
        self.assertIs(integrated, False)
        self.assertIs(evidence["resolved_value"], False)

    def test_empirical_seedance_route_accepts_missing_output_metadata(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "ep-seedance-25", "modelName": "seedance-2.5-white",
            "provider": "BytePlus", "online": True, "status": True,
            "allowVideoType": [1, 2, 3, 4, 5], "imageCount": 1,
            "resolution": ["480p", "720p"],
            "multimodelTypes": ["text", "audio", "video", "image"],
            "ouputMultiType": [],
        }])
        segment = {
            "id": "shot_01", "model": "seedance-2.5-white",
            "video_type": 4, "resolution": "720p",
            "urls": ["https://example.invalid/identity.jpg"],
            "dialogue": "一句想法怎么走到可交付的视频？",
        }
        self.assertEqual(
            video_engine._validate_empirical_audio_route(
                segment, model_catalog=catalog),
            "seedance-2.5-white")

    def test_disproved_kling_route_requires_explicit_retest_mode(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "3.0-Omni",
            "modelName": "kling-v3-omni-video-white",
            "online": True, "status": True,
            "allowVideoType": [4], "imageCount": 1,
            "resolution": ["1080p"],
            "multimodelTypes": ["text", "audio", "image", "video"],
            "ouputMultiType": ["video"],
        }])
        segment = {
            "id": "shot_01", "model": "kling-v3-omni-video-white",
            "video_type": 4, "resolution": "1080p",
            "urls": ["https://example.invalid/identity.jpg"],
            "dialogue": "已确认中文台词",
        }
        with self.assertRaisesRegex(
                ValueError, "REQUIRES_UNKNOWN_OUTPUT_AUDIO"):
            video_engine._validate_empirical_audio_route(
                segment, model_catalog=catalog)
        self.assertEqual(
            video_engine._validate_empirical_audio_route(
                segment, model_catalog=catalog, allow_disproved=True),
            "kling-v3-omni-video-white")

    def test_avatar_plate_prompt_distinguishes_no_bgm_from_no_voice(self):
        prompt = video_prompts._avatar_plate_submission_text({
            "id": "shot_01", "dialogue": "已确认中文台词",
            "language": "zh-CN",
            "audio_contract": {
                "dialogue": "已确认中文台词", "language": "zh-CN",
                "voice": "普通话女声", "lip_sync": True, "bgm": "none",
            },
        }, "seedance-2.5-white")
        self.assertIn("必须生成上述中文口播人声并写入成品视频音轨", prompt)
        self.assertIn("这只表示不要BGM，不得删除或静音口播人声", prompt)
        self.assertNotIn("BGM氛围：none", prompt)

    def test_explicit_integrated_audio_field_remains_positive_evidence(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "kling-v3-omni-video-white",
            "modelName": "3.0-Omni", "online": True, "status": "active",
            "allowVideoType": [5], "imageCount": 1,
            "multimodelTypes": ["image", "video", "text", "audio"],
            "integratedAudio": True,
        }])
        record = catalog["records"]["kling-v3-omni-video-white"]
        self.assertIs(record["integrated_audio"], True)
        self.assertEqual(record["audio_evidence"]["source"],
                         "explicit_integrated_audio")
        self.assertEqual(video_engine._pick_video_model(
            "3.0-Omni", video_type=5, dialogue="已确认台词",
            formal=True, reference_count=1, catalog=catalog), "3.0-Omni")

    def test_conflicting_explicit_output_audio_fields_fail_closed(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "conflicted-route", "modelName": "Conflicted",
            "online": True, "status": "active", "allowVideoType": [5],
            "imageCount": 1, "integratedAudio": False,
            "outputMultiType": ["video", "audio"],
        }])
        record = catalog["records"]["conflicted-route"]
        self.assertTrue(record["conflict"])
        self.assertIsNone(record["integrated_audio"])
        with self.assertRaisesRegex(ValueError, "NO_CAPABLE_VIDEO_MODEL"):
            video_engine._pick_video_model(
                "Conflicted", video_type=5, dialogue="台词", formal=True,
                allow_fallback=False, reference_count=1, catalog=catalog)

    def test_duplicate_provider_model_id_preserves_exact_routes(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "2.0", "modelName": "seedance-2.0-VS-white",
            "online": True, "status": "active", "allowVideoType": [5],
            "imageCount": 4, "multimodelTypes": ["image", "video", "audio"],
        }, {
            "modelId": "2.0", "modelName": "kling-2.0-video",
            "online": True, "status": "active", "allowVideoType": [4],
            "imageCount": 1, "multimodelTypes": ["image", "video", "audio"],
        }])
        self.assertEqual(
            set(catalog["records"]),
            {"seedance-2.0-VS-white", "kling-2.0-video"})
        self.assertEqual(
            catalog["records"]["seedance-2.0-VS-white"]["provider_model_id"],
            "2.0")
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS_MODEL_ALIAS"):
            video_models._resolve_catalog_model("2.0", catalog)
        self.assertEqual(
            video_models._resolve_catalog_model(
                "seedance-2.0-VS-white", catalog),
            "seedance-2.0-VS-white")
        self.assertEqual(video_engine._pick_video_model(
            "seedance-2.0-VS-white", video_type=5, dialogue="台词",
            formal=True, allow_fallback=False, reference_count=1,
            allow_unproven_audio=True, catalog=catalog),
            "seedance-2.0-VS-white")
        self.assertEqual(video_engine._pick_video_model(
            "kling-2.0-video", video_type=4, dialogue="台词",
            formal=True, allow_fallback=False, reference_count=1,
            allow_unproven_audio=True, catalog=catalog),
            "kling-2.0-video")
        with self.assertRaisesRegex(ValueError, "NO_CAPABLE_VIDEO_MODEL"):
            video_engine._pick_video_model(
                "seedance-2.0-VS-white", video_type=5, dialogue="台词",
                formal=True, allow_fallback=False, reference_count=1,
                proven_audio_models=("2.0",), catalog=catalog)
        self.assertEqual(video_engine._pick_video_model(
            "seedance-2.0-VS-white", video_type=5, dialogue="台词",
            formal=True, allow_fallback=False, reference_count=1,
            proven_audio_models=("seedance-2.0-VS-white",), catalog=catalog),
            "seedance-2.0-VS-white")

    def test_ambiguous_secondary_fallback_does_not_mask_primary_failure(self):
        catalog = video_engine._normalize_model_catalog([{
            "modelId": "seedance-route", "modelName": "seedance-exact",
            "online": True, "status": "active", "allowVideoType": [5],
            "imageCount": 1, "outputMultiType": ["video"],
        }, {
            "modelId": "wan-route-a", "modelName": "wan-a",
            "aliases": ["wan2.7-i2v"], "online": True,
            "status": "active", "allowVideoType": [2],
            "outputMultiType": ["video"],
        }, {
            "modelId": "wan-route-b", "modelName": "wan-b",
            "aliases": ["wan2.7-i2v"], "online": True,
            "status": "active", "allowVideoType": [2],
            "outputMultiType": ["video"],
        }])
        with self.assertRaisesRegex(
                ValueError,
                "NO_CAPABLE_VIDEO_MODEL:.*skipped_ambiguous_fallbacks=wan2.7-i2v"):
            video_models._pick_video_model(
                "seedance-exact", video_type=5, dialogue="台词",
                formal=True, allow_fallback=True, reference_count=1,
                model_catalog_fn=lambda _category: catalog,
                fallback_chain=["wan2.7-i2v"])
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS_MODEL_ALIAS"):
            video_models._pick_video_model(
                "wan2.7-i2v", video_type=2, formal=True,
                allow_fallback=False,
                model_catalog_fn=lambda _category: catalog)

    def test_submission_retry_never_falls_back_to_shared_provider_id(self):
        live_rows = [{
            "modelId": "2.0", "modelName": "seedance-2.0-VS-white",
        }, {
            "modelId": "2.0", "modelName": "kling-2.0-video",
        }]
        with mock.patch.object(
                br_client, "list_video_models", return_value=live_rows):
            candidates = br_client._legacy_video_model_candidates(
                "seedance-2.0-VS-white")
            self.assertEqual(candidates[0], "seedance-2.0-VS-white")
            self.assertNotIn("2.0", candidates)
            with self.assertRaisesRegex(
                    br_client.BRError, "AMBIGUOUS_MODEL_ALIAS"):
                br_client._legacy_video_model_candidates("2.0")

    def _bound_empirical_audio_canary(self, tmp):
        root = Path(tmp)
        contract_path = root / "recovery.json"
        contract_path.write_text(json.dumps({
            "schema_version": 1, "kind": "video_recovery_contract",
            "client": "unit", "run_id": "run",
            "recovery_id": "recovery-audio-1",
            "source": {},
            "failure": {"segment_id": "s1", "task_id": "task-old",
                        "attempt": 1, "status": "failed"},
            "strategy": {
                "mode": "independent_identity_anchor",
                "target_model": "ep-route", "resolution": "1080p",
                "output_ratio": "16:9",
                "audio_capability_policy":
                    "empirical_canary_unknown_output_audio",
                "integrated_audio_required": True,
                "preserve_confirmed_identity_reference": True,
                "tail_chain": False, "require_canary_before_batch": True,
                "canary_segment_id": "s1",
            },
            "confirmed_by_customer": True,
            "customer_confirmation": "确认当前单段精确路线音轨实证策略",
            "confirmation_source": "customer_dialogue",
        }), encoding="utf-8")
        contract_record = run_manifest.file_record(str(contract_path))
        segment = {
            "client": "unit", "run_id": "run", "id": "s1",
            "model": "ep-route", "video_type": 5,
            "resolution": "1080p", "ratio": "16:9",
            "urls": ["https://example.invalid/identity.png"],
            "references": [{"type": "character_identity",
                            "url": "https://example.invalid/identity.png"}],
            "audio_contract": {"track": "required", "dialogue": "中文口播"},
            "recovery_contract": {
                "path": contract_record["path"],
                "sha256": contract_record["sha256"],
                "recovery_id": "recovery-audio-1",
            },
            "audio_capability_policy":
                "empirical_canary_unknown_output_audio",
            "audio_capability_policy_contract": {
                "file": contract_record,
                "policy": "empirical_canary_unknown_output_audio",
                "scope": "formal_test_segment_only", "segment_id": "s1",
                "recovery_id": "recovery-audio-1",
            },
        }
        fingerprint = artifact_contract.build_video_handoff(
            segment)["fingerprint"]
        segment["video_handoff_fingerprint"] = fingerprint
        segments_path = root / "segments.json"
        segments_path.write_text(json.dumps({
            "client": "unit", "run_id": "run", "segments": [segment],
        }), encoding="utf-8")
        manifest = {
            "client": "unit", "run_id": "run", "requires_test_segment": True,
            "recovery_test_segment_requirement": {
                "segment_id": "s1", "recovery_id": "recovery-audio-1",
                "video_handoff_fingerprint": fingerprint,
                "contract": contract_record,
            },
            "handoffs": {"video": {
                "file": run_manifest.file_record(str(segments_path)),
                "segments": {"s1": fingerprint},
            }},
        }
        catalog = video_models._normalize_model_catalog([{
            "modelId": "ep-route", "modelName": "ep-route",
            "status": True, "online": True, "allowVideoType": [5],
            "imageCount": 1, "resolution": ["720p", "1080p"],
            "multimodelTypes": ["text", "image", "video", "audio"],
            "outputMultiType": ["video"],
        }])
        return segment, manifest, catalog

    def test_unknown_audio_route_requires_exact_bound_test_canary(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment, manifest, catalog = self._bound_empirical_audio_canary(tmp)
            result = video_engine.inspect_submission_requirements(
                [segment], formal=True, allow_model_fallback=True,
                catalog=catalog, manifest=manifest,
                formal_stage="test_segment")
            self.assertTrue(result["ready"], result["blockers"])
            row = result["segments"][0]
            self.assertEqual(row["model"], "ep-route")
            self.assertIsNone(row["integrated_audio"])
            self.assertTrue(row["audio_capability_policy_ready"])
            self.assertEqual(row["eligible_fallback_models"], [])
            self.assertEqual(
                row["audio_capability_action"],
                "submit_exact_single_canary_then_require_non_silent_media_audio_qc")
            blocked = video_engine.inspect_submission_requirements(
                [segment], formal=True, allow_model_fallback=True,
                catalog=catalog, manifest=manifest, formal_stage="video")
            self.assertFalse(blocked["ready"])
            self.assertTrue(any(
                item["code"] == "AUDIO_CAPABILITY_POLICY_NOT_AUTHORIZED"
                for item in blocked["blockers"]))

    def test_empirical_audio_canary_rejects_explicit_true_false_and_stale_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment, manifest, unknown_catalog = \
                self._bound_empirical_audio_canary(tmp)
            for explicit in (True, False):
                catalog = video_models._normalize_model_catalog([{
                    "modelId": "ep-route", "status": True, "online": True,
                    "allowVideoType": [5], "imageCount": 1,
                    "resolution": ["1080p"],
                    "multimodelTypes": ["image", "video", "audio"],
                    "outputMultiType": ["video"],
                    "integratedAudio": explicit,
                }])
                result = video_engine.inspect_submission_requirements(
                    [segment], formal=True, catalog=catalog, manifest=manifest,
                    formal_stage="test_segment")
                self.assertFalse(result["ready"])
                self.assertTrue(any(
                    item["code"] ==
                    "EMPIRICAL_AUDIO_CANARY_REQUIRES_UNKNOWN_OUTPUT_AUDIO"
                    for item in result["blockers"]))
            contract_path = Path(
                segment["audio_capability_policy_contract"]["file"]["path"])
            contract_path.write_text("{}", encoding="utf-8")
            result = video_engine.inspect_submission_requirements(
                [segment], formal=True, catalog=unknown_catalog,
                manifest=manifest,
                formal_stage="test_segment")
            self.assertFalse(result["ready"])
            self.assertTrue(any(
                item["code"] == "AUDIO_CAPABILITY_POLICY_NOT_AUTHORIZED"
                for item in result["blockers"]))

    def test_approved_empirical_canary_promotes_only_exact_route(self):
        catalog = video_models._normalize_model_catalog([{
            "modelId": "ep-route", "status": True, "online": True,
            "allowVideoType": [5], "imageCount": 1,
            "resolution": ["1080p"],
            "multimodelTypes": ["image", "video", "audio"],
            "outputMultiType": ["video"],
        }])
        segments = [{
            "id": "s1", "model": "ep-route", "video_type": 5,
            "resolution": "1080p",
            "urls": ["https://example.invalid/identity.png"],
            "audio_contract": {"track": "required", "dialogue": "台词"},
        }]
        blocked = video_engine.inspect_submission_requirements(
            segments, formal=True, allow_model_fallback=False, catalog=catalog)
        self.assertFalse(blocked["ready"])
        proven = video_engine.inspect_submission_requirements(
            segments, formal=True, allow_model_fallback=False, catalog=catalog,
            proven_audio_models=("ep-route",))
        self.assertTrue(proven["ready"], proven["blockers"])
        self.assertIs(proven["segments"][0]["integrated_audio"], True)

    def test_empirical_audio_policy_is_visible_and_bound_in_prompt_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment, manifest, catalog = self._bound_empirical_audio_canary(tmp)
            segment.update({
                "generation_role": "avatar_plate_only",
                "text": "讲师自然口播", "language": "普通话",
                "audio_contract": {
                    "track": "required", "dialogue": "这是音轨实证测试。",
                    "language": "普通话", "voice": "清晰沉稳",
                    "lip_sync": True,
                },
            })
            segment["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(segment)["fingerprint"])
            segments_path = Path(
                manifest["handoffs"]["video"]["file"]["path"])
            segments_path.write_text(json.dumps({
                "client": "unit", "run_id": "run", "segments": [segment],
            }), encoding="utf-8")
            manifest["handoffs"]["video"]["file"] = \
                run_manifest.file_record(str(segments_path))
            manifest["handoffs"]["video"]["segments"]["s1"] = \
                segment["video_handoff_fingerprint"]
            manifest["recovery_test_segment_requirement"].update({
                "video_handoff_fingerprint":
                    segment["video_handoff_fingerprint"],
            })
            preflight = video_engine.inspect_submission_requirements(
                [segment], formal=True, catalog=catalog, manifest=manifest,
                formal_stage="test_segment")
            self.assertTrue(preflight["ready"], preflight["blockers"])
            review = prompt_review.capture_video_segments(
                {"shots": [{"id": "s1"}]}, [segment],
                model="ep-route", preflight=preflight)
            item = review["prompts"][0]
            self.assertEqual(
                item["audio_capability_policy"],
                "empirical_canary_unknown_output_audio")
            self.assertEqual(item["fallback_submission_prompts"], [])
            review_path = Path(tmp, "review.json")
            preview_path = Path(tmp, "review.md")
            prompt_review.save_pending(review, str(review_path))
            prompt_review.preview(str(review_path), out=str(preview_path))
            preview = preview_path.read_text(encoding="utf-8")
            self.assertIn("输出音轨实证策略", preview)
            self.assertIn("不能证明返回视频内嵌音轨", preview)
            self.assertIn("禁止模型回落", preview)
            prompt_review.confirm(
                str(review_path), customer_confirmation="确认当前精确 canary 提交包")
            video_prompts._require_confirmed_prompt_review(
                str(review_path), "video", [dict(segment)])
            changed = dict(segment)
            changed.pop("audio_capability_policy")
            changed.pop("audio_capability_policy_contract")
            with self.assertRaisesRegex(
                    ValueError,
                    "PROMPT_REVIEW_AUDIO_CAPABILITY_POLICY_MISMATCH"):
                video_prompts._require_confirmed_prompt_review(
                    str(review_path), "video", [changed])

    def test_recovery_compiler_binds_empirical_audio_policy_only_to_canary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_segment = {
                "client": "unit", "run_id": "run", "id": "s1",
                "model": "old-route", "video_type": 5,
                "resolution": "720p", "ratio": "16:9",
                "urls": ["https://example.invalid/identity.png"],
                "references": [{"type": "character_identity",
                                "url": "https://example.invalid/identity.png"}],
                "audio_contract": {"dialogue": "中文口播"},
            }
            source_segment["video_handoff_fingerprint"] = (
                artifact_contract.build_video_handoff(
                    source_segment)["fingerprint"])
            spec = {"client": "unit", "run_id": "run",
                    "segments": [source_segment]}
            spec_path = root / "segments.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            spec_record = run_manifest.file_record(str(spec_path))
            aggregate_handoff = "h" * 64
            manifest = {
                "client": "unit", "run_id": "run",
                "handoffs": {"video": {
                    "sha256": aggregate_handoff, "file": spec_record,
                    "segments": {"s1": source_segment[
                        "video_handoff_fingerprint"]},
                }},
                "tasks": [{
                    "stage": "video", "unit_id": "s1", "task_id": "task-3",
                    "attempt": 3, "model": "old-route", "status": "failed",
                    "handoff_fingerprint": source_segment[
                        "video_handoff_fingerprint"],
                }],
            }
            contract_path = root / "recovery.json"
            contract_path.write_text(json.dumps({
                "schema_version": 1, "kind": "video_recovery_contract",
                "client": "unit", "run_id": "run",
                "recovery_id": "recovery-audio-1",
                "source": {"handoff_sha256": aggregate_handoff,
                           "segments_sha256": spec_record["sha256"]},
                "failure": {"segment_id": "s1", "task_id": "task-3",
                            "attempt": 3, "status": "failed"},
                "strategy": {
                    "mode": "independent_identity_anchor",
                    "target_model": "ep-route", "resolution": "1080p",
                    "output_ratio": "16:9",
                    "audio_capability_policy":
                        "empirical_canary_unknown_output_audio",
                    "integrated_audio_required": True,
                    "preserve_confirmed_identity_reference": True,
                    "tail_chain": False, "require_canary_before_batch": True,
                    "canary_segment_id": "s1",
                },
                "confirmed_by_customer": True,
                "customer_confirmation": "确认精确 canary 路线",
                "confirmation_source": "customer_dialogue",
            }), encoding="utf-8")
            catalog = video_models._normalize_model_catalog([{
                "modelId": "ep-route", "status": True, "online": True,
                "allowVideoType": [5], "imageCount": 1,
                "resolution": ["720p", "1080p"],
                "multimodelTypes": ["image", "video", "audio"],
                "outputMultiType": ["video"],
            }])
            with mock.patch.object(video_engine, "_model_catalog",
                                   return_value=catalog):
                compiled = script_splitter.apply_video_recovery_contract(
                    spec, str(contract_path), manifest)
            segment = compiled["segments"][0]
            self.assertEqual(segment["model"], "ep-route")
            self.assertEqual(segment["resolution"], "1080p")
            self.assertEqual(
                segment["audio_capability_policy"],
                "empirical_canary_unknown_output_audio")
            self.assertEqual(
                segment["audio_capability_policy_contract"]["scope"],
                "formal_test_segment_only")
            self.assertTrue(
                artifact_contract.verify_video_handoff(segment)["ok"])

    def test_empirical_audio_canary_never_uses_model_not_found_fallback(self):
        segment = {
            "id": "s1", "model": "ep-route", "video_type": 5,
            "audio_capability_policy":
                "empirical_canary_unknown_output_audio",
        }
        error = br_client.BRError("model not found")
        context = {"allow_model_fallback": True}
        state = {"seg": segment, "seg_model": "ep-route"}
        with mock.patch.object(
                br_client, "is_video_model_not_found", return_value=True), \
                mock.patch.object(video_engine, "_pick_video_model") as picker:
            with self.assertRaises(br_client.BRError):
                video_engine._submit_batch_model_fallback(
                    context, 0, state, error)
        picker.assert_not_called()

    def test_audio_policy_changes_video_handoff_identity(self):
        base = {"id": "s1", "model": "ep-route", "video_type": 5}
        original = artifact_contract.build_video_handoff(base)["fingerprint"]
        changed = artifact_contract.build_video_handoff(dict(
            base,
            audio_capability_policy="empirical_canary_unknown_output_audio",
            audio_capability_policy_contract={
                "scope": "formal_test_segment_only"},
        ))["fingerprint"]
        self.assertNotEqual(original, changed)

    def test_media_qc_can_return_a_formal_failure_for_batch_settlement(self):
        failed = {
            "passed": False,
            "errors": ["AUDIO_TRACK_FAILED", "SILENCE_FAILED"],
            "media": {"audio": {"stream_count": 0}},
        }
        segment = {
            "id": "s1", "duration": 5, "ratio": "16:9",
            "audio_contract": {"track": "required", "dialogue": "台词"},
        }
        with mock.patch.object(video_guard.media_qc, "check",
                               side_effect=lambda *args, **kwargs: dict(failed)):
            with self.assertRaisesRegex(ValueError, "MEDIA_QC_FAILED"):
                video_guard._media_qc_guard(
                    "/tmp/formal.mp4", segment, draft=False)
            report = video_guard._media_qc_guard(
                "/tmp/formal.mp4", segment, draft=False,
                raise_on_failure=False)
        self.assertFalse(report["passed"])
        self.assertIn("AUDIO_TRACK_FAILED", report["errors"])

    def test_formal_batch_reuse_keeps_approved_canary_qc_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = os.path.join(tmp, "canary.mp4")
            Path(video_path).write_bytes(b"approved canary bytes")
            approved_report = {
                "passed": True,
                "report_path": video_path + ".qc.json",
                "media": {"audio": {"stream_count": 1}},
                "checks": {
                    "audio_track": {"passed": True},
                    "silence": {"passed": True},
                },
            }
            segment = {
                "id": "s1", "duration": 5, "ratio": "16:9",
                "audio_contract": {"track": "required", "dialogue": "台词"},
                "output_artifact_contract": {
                    "mode": "approved_canary_exact_media_reuse",
                    "path": video_path,
                    "sha256": artifact_contract.file_sha256(video_path),
                    "take_fingerprint": "take-1",
                },
            }
            manifest = {"test_segment_artifact": {
                "status": "approved", "segment_id": "s1",
                "take_fingerprint": "take-1",
                "video": run_manifest.file_record(video_path),
                "media_qc": approved_report,
            }}
            with mock.patch.object(video_guard.media_qc, "check") as check:
                report = video_guard._media_qc_guard(
                    video_path, segment, draft=False, manifest=manifest)
            check.assert_not_called()
            self.assertEqual(report["report_path"], video_path + ".qc.json")
            self.assertTrue(report["passed"])

    def test_batch_collects_every_paid_result_when_each_media_qc_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            segments = [{
                "id": "s1", "out_path": os.path.join(tmp, "s1.mp4"),
            }, {
                "id": "s2", "out_path": os.path.join(tmp, "s2.mp4"),
            }]
            tasks = [{
                "idx": index, "task_id": "task-%d" % (index + 1),
                "segment": segment, "model": "model-a",
                "initial_model": "model-a", "fallback_reason": None,
                "handoff_fingerprint": "handoff-%d" % (index + 1),
            } for index, segment in enumerate(segments)]
            context = {
                "api_key": "test", "poll_err_count": {},
                "allow_model_fallback": True, "draft": False,
                "manifest": None, "manifest_path": None, "ledger_path": None,
                "results": [None, None], "pending": list(tasks),
                "log": lambda _message: None,
            }
            failed_qc = {
                "passed": False, "errors": ["AUDIO_TRACK_FAILED"],
                "media": {"audio": {"stream_count": 0}},
                "report_path": os.path.join(tmp, "failed.qc.json"),
            }
            with mock.patch.object(video_engine, "_download_completed_video"), \
                    mock.patch.object(video_engine, "_media_qc_guard",
                                      return_value=failed_qc) as qc:
                for task in tasks:
                    video_engine._consume_polled_batch_task(
                        context, task, {"idx": task["idx"],
                                        "tid": task["task_id"],
                                        "seg": task["segment"],
                                        "info": {"status": "succeeded",
                                                 "videoUrl": "https://example.invalid/v.mp4"}})
        self.assertEqual(len(context["pending"]), 0)
        self.assertTrue(all(item and not item["ok"]
                            for item in context["results"]))
        self.assertEqual(qc.call_count, 2)
        self.assertTrue(all(
            call.kwargs.get("raise_on_failure") is False
            for call in qc.call_args_list))
        for index, result in enumerate(context["results"], 1):
            self.assertEqual(result["taskId"], "task-%d" % index)
            self.assertEqual(result["take_id"], "s%d-take-1" % index)
            self.assertTrue(result["take_fingerprint"])
            self.assertEqual(
                result["video_handoff_fingerprint"],
                "handoff-%d" % index)
            self.assertEqual(result["model"], "model-a")
            self.assertFalse(result["resume_available"])
            self.assertTrue(result["regeneration_required"])
            self.assertEqual(result["review_status"], "pending")

    def test_multi_segment_preflight_reads_live_catalog_once(self):
        catalog = self._privacy_catalog(kling_image_count=2)
        segments = [{
            "id": "s1", "video_type": 5,
            "urls": ["https://example.invalid/identity.png"],
            "audio_contract": {"dialogue": "第一段台词"},
        }, {
            "id": "s2", "video_type": 5,
            "urls": ["https://example.invalid/identity.png"],
            "audio_contract": {"dialogue": "第二段台词"},
        }]
        with mock.patch.object(video_engine, "_model_catalog",
                               return_value=catalog) as lookup:
            result = video_engine.inspect_submission_requirements(
                segments, "seedance-2.0", formal=True)
        self.assertTrue(result["ready"])
        lookup.assert_called_once_with("video")

    def test_preflight_preserves_and_enforces_live_route_request_capabilities(self):
        catalog = video_models._normalize_model_catalog([{
            "modelId": "kling-v3-omni-video",
            "modelName": "Kling capability route",
            "online": True, "status": "active", "allowVideoType": [5],
            "imageCount": 2, "integratedAudio": True,
            "resolution": ["480p", "720P"],
            "multimodelTypes": ["text", "image"],
            "outputMultiType": ["video", "audio"],
        }])
        record = catalog["records"]["kling-v3-omni-video"]
        self.assertEqual(record["resolutions"], {"480p", "720P"})
        self.assertEqual(record["multimodel_types"], {"text", "image"})
        self.assertEqual(record["output_multi_type"], {"video", "audio"})
        segment = {
            "id": "s1", "model": "kling-v3-omni-video",
            "video_type": 5, "resolution": "1080p",
            "urls": ["https://example.invalid/identity.png"],
        }
        result = video_engine.inspect_submission_requirements(
            [segment], formal=True, catalog=catalog)
        self.assertFalse(result["ready"])
        self.assertTrue(any(
            item["code"] == "MODEL_RESOLUTION_UNSUPPORTED"
            for item in result["blockers"]))
        row = result["segments"][0]
        self.assertEqual(row["requested_resolution"], "1080p")
        self.assertEqual(row["advertised_resolutions"], ["480p", "720p"])
        self.assertEqual(row["model_input_modalities"], ["image", "text"])
        self.assertEqual(row["model_output_modalities"], ["audio", "video"])
        self.assertFalse(row["model_request_contract_ready"])

    def test_paid_request_contract_rejects_incompatible_declared_modalities(self):
        input_catalog = video_models._normalize_model_catalog([{
            "modelId": "route-input", "online": True, "status": True,
            "allowVideoType": [5], "multimodelTypes": ["text"],
            "outputMultiType": ["video"],
        }])
        with self.assertRaisesRegex(
                ValueError, "MODEL_INPUT_MODALITY_UNSUPPORTED"):
            video_engine._model_request_capability_evidence(
                "route-input", {"video_type": 5}, input_catalog)
        output_catalog = video_models._normalize_model_catalog([{
            "modelId": "route-output", "online": True, "status": True,
            "allowVideoType": [1], "multimodelTypes": ["text"],
            "outputMultiType": ["image"],
        }])
        with self.assertRaisesRegex(
                ValueError, "MODEL_OUTPUT_MODALITY_UNSUPPORTED"):
            video_engine._model_request_capability_evidence(
                "route-output", {"video_type": 1}, output_catalog)

    def test_live_capability_drift_does_not_block_existing_paid_task_recovery(self):
        catalog = video_models._normalize_model_catalog([{
            "modelId": "route-paid", "online": True, "status": True,
            "allowVideoType": [1], "resolution": ["720p"],
            "multimodelTypes": ["text"], "outputMultiType": ["video"],
        }])
        segment = {
            "id": "s1", "model": "route-paid", "video_type": 1,
            "resolution": "1080p", "video_handoff_fingerprint": "handoff",
        }
        manifest = {"tasks": [{
            "stage": "video", "unit_id": "s1", "task_id": "paid-task-1",
            "handoff_fingerprint": "handoff", "model": "route-paid",
            "attempt": 1, "status": "submitted",
        }]}
        result = video_engine.inspect_submission_requirements(
            [segment], formal=True, catalog=catalog, manifest=manifest)
        self.assertTrue(result["ready"])
        self.assertEqual(result["blockers"], [])
        row = result["segments"][0]
        self.assertTrue(row["existing_paid_task_recovery"])
        self.assertTrue(row["model_request_contract_ready"])
        self.assertFalse(row["model_request_contract_current"])
        self.assertIn(
            "MODEL_RESOLUTION_UNSUPPORTED",
            row["capability_drift_error"])

    def test_chain_preflight_blocks_when_privacy_route_cannot_hold_tail_plus_identity(self):
        catalog = self._privacy_catalog(kling_image_count=1)
        segments = [{
            "id": "s1", "video_type": 5,
            "urls": ["https://example.invalid/identity.png"],
            "audio_contract": {"dialogue": "第一段台词"},
        }, {
            "id": "s2", "video_type": 5, "chain_required": True,
            "chain_contract": {"predecessor_segment_id": "s1"},
            "urls": ["https://example.invalid/identity.png"],
            "audio_contract": {"dialogue": "第二段台词"},
        }]
        with mock.patch.object(video_engine, "_model_catalog",
                               return_value=catalog):
            result = video_engine.inspect_submission_requirements(
                segments, "seedance-2.0", formal=True)
        self.assertFalse(result["ready"])
        first, second = result["segments"]
        self.assertEqual(first["privacy_fallback_model"], "3.0-Omni")
        self.assertEqual(first["privacy_fallback_reference_count"], 1)
        self.assertTrue(second["chain_tail_reference_reserved"])
        self.assertEqual(second["privacy_fallback_reference_count"], 2)
        self.assertFalse(second["privacy_fallback_ready"])
        self.assertTrue(any(
            item["segment_id"] == "s2" and
            item["code"] == "PRIVACY_FALLBACK_CAPACITY_UNAVAILABLE"
            for item in result["blockers"]))

    def test_prompt_capture_consumes_preflight_resolved_model_contract(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {
            "id": "s1", "text": "讲师讲解", "video_type": 5,
            "urls": ["https://example.invalid/identity.png"],
            "audio_contract": {"dialogue": "已确认台词", "language": "普通话"},
        }
        preflight = {"contract_version": video_engine.VIDEO_PREFLIGHT_CONTRACT_VERSION,
                     "generated_at": "2026-08-19T20:00:00Z",
                     "ready": True, "blockers": [],
                     "preferred_model": "seedance-2.0", "formal": True,
        "segments": [{
            "segment_id": "s1", "model": "seedance-active",
            "integrated_audio": True,
            "integrated_audio_evidence": {
                "value": True, "source": "explicit_integrated_audio",
                "resolved_value": True, "conflict": False,
            },
            "submission_source_fingerprint":
                video_engine._submission_source_fingerprint(segment),
            "eligible_fallback_models": ["seedance-active",
                                         "kling-v3-omni-video-white"],
            "privacy_fallback_model": "kling-v3-omni-video-white",
        }]}
        preflight["request_fingerprint"] = \
            video_engine._preflight_request_fingerprint(
                preflight["segments"], preflight["preferred_model"], True)
        review = prompt_review.capture_video_segments(
            plan, [segment], preflight=preflight)
        item = review["prompts"][0]
        self.assertEqual(item["model"], "seedance-active")
        self.assertEqual(item["privacy_fallback_model"],
                         "kling-v3-omni-video-white")
        self.assertIn("kling-v3-omni-video-white",
                      item["model_submission_prompts"])
        self.assertTrue(item["integrated_audio"])
        self.assertEqual(item["integrated_audio_evidence"]["source"],
                         "explicit_integrated_audio")
        self.assertEqual(review["video_preflight"]["request_fingerprint"],
                         preflight["request_fingerprint"])

    def test_prompt_capture_rejects_stale_preflight_source_contract(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        original = {"id": "s1", "text": "旧台词", "video_type": 5,
                    "urls": ["https://example.invalid/identity.png"]}
        changed = dict(original, text="已经改变的台词")
        preflight = {"contract_version": video_engine.VIDEO_PREFLIGHT_CONTRACT_VERSION,
                     "generated_at": "2026-08-19T20:00:00Z",
                     "ready": True, "blockers": [],
                     "preferred_model": "seedance-2.0", "formal": True,
                     "segments": [{
            "segment_id": "s1", "model": "seedance-active",
            "submission_source_fingerprint":
                video_engine._submission_source_fingerprint(original),
        }]}
        preflight["request_fingerprint"] = \
            video_engine._preflight_request_fingerprint(
                preflight["segments"], preflight["preferred_model"], True)
        with self.assertRaisesRegex(
                ValueError, "PROMPT_REVIEW_PREFLIGHT_SOURCE_MISMATCH"):
            prompt_review.capture_video_segments(
                plan, [changed], preflight=preflight)

    def test_provider_error_evidence_keeps_root_code_without_secret_payload(self):
        error = video_engine.br_client.BRVideoReferencePrivacyError(
            "wrapper", payload={
                "code": "InputImageSensitiveContentDetected",
                "status": "failed",
                "message": "input image real person privacy sk-secret "
                           "data:image/png;base64,AAAA",
            })
        evidence = video_engine._provider_error_evidence(
            error, model="seedance-active", task_id="task-1")
        self.assertEqual(evidence["provider_error_code"],
                         "InputImageSensitiveContentDetected")
        self.assertEqual(evidence["billing_status"], "unknown")
        self.assertNotIn("sk-secret", evidence["message_summary"])
        self.assertNotIn("AAAA", evidence["message_summary"])

    def test_completed_provider_http_vod_is_narrowly_upgraded_to_https(self):
        source = "http://store.vod-qcloud.com:80/video/a.mp4?token=abc"
        expected = "https://store.vod-qcloud.com/video/a.mp4?token=abc"
        self.assertEqual(video_engine._provider_completed_media_url(source),
                         expected)
        with mock.patch.object(video_engine.br_client, "download",
                               return_value="out.mp4") as download:
            result = video_engine._download_completed_video(
                "key", "task-1", source, "out.mp4")
        self.assertEqual(result, "out.mp4")
        download.assert_called_once_with(
            expected, "out.mp4", allow_nonpublic_peer=True)
        for unsafe in (
                "http://user:pass@store.vod-qcloud.com/a.mp4",
                "http://store.vod-qcloud.com:8080/a.mp4"):
            self.assertEqual(
                video_engine._provider_completed_media_url(unsafe), unsafe)
        with self.assertRaisesRegex(
                video_engine.br_client.BRError, "只允许 HTTPS"):
            video_engine.br_client._validate_download_url(source)

    def test_provider_download_allows_only_transparent_proxy_fake_ip(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("198.18.0.23", 443))]
        with mock.patch.object(
                video_engine.br_client.socket, "getaddrinfo",
                return_value=fake):
            with self.assertRaisesRegex(
                    video_engine.br_client.BRError, "保留地址"):
                video_engine.br_client._validate_download_url(
                    "https://store.vod-qcloud.com/video.mp4")
            self.assertEqual(
                video_engine.br_client._validate_download_url(
                    "https://store.vod-qcloud.com/video.mp4",
                    allow_nonpublic_peer=True),
                "https://store.vod-qcloud.com/video.mp4")
            self.assertTrue(video_engine.br_client._peer_is_acceptable(
                "127.0.0.1", True,
                trusted_url="https://store.vod-qcloud.com/video.mp4"))
            self.assertFalse(video_engine.br_client._peer_is_acceptable(
                "127.0.0.1", False,
                trusted_url="https://store.vod-qcloud.com/video.mp4"))
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                    ("192.168.1.2", 443))]
        with mock.patch.object(
                video_engine.br_client.socket, "getaddrinfo",
                return_value=private), self.assertRaisesRegex(
                    video_engine.br_client.BRError, "私网"):
            video_engine.br_client._validate_download_url(
                "https://store.vod-qcloud.com/video.mp4",
                allow_nonpublic_peer=True)

    def test_design_board_privacy_recovery_never_requests_panel_expansion(self):
        action = video_engine._privacy_fallback_recovery_action({
            "id": "s1", "storyboard_ref": False,
            "storyboard_usage": "design_board",
        }, "kling-v3-omni-video-white")
        self.assertIn("不需要单格展开图", action)
        self.assertIn("重新 capture-video", action)

    def test_legacy_single_render_library_entry_is_disabled(self):
        with self.assertRaisesRegex(ValueError, "LEGACY_SINGLE_RENDER_DISABLED"):
            video_engine.render("unreviewed prompt")

    def test_base_only_confirmed_review_cannot_authorize_generation(self):
        segment = {"id": "s1", "text": "产品特写", "storyboard_panel_index": 1}
        review = _confirmed_review({
            "stage": "video",
            "prompts": [{"shot_id": "s1", "prompt_zh": "只确认了基础文案"}],
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_EXACT_SUBMISSION_REQUIRED"):
                video_prompts._require_confirmed_prompt_review(path, "video", [segment])

    def test_approved_model_submission_is_returned_byte_for_byte(self):
        exact = "完整已确认提示词\n【语言硬锁】只使用普通话。"
        segment = {
            "id": "s1",
            "audio_contract": {"dialogue": "这是客户确认过的完整中文台词", "language": "普通话"},
            "approved_submission_prompts_by_model": {"seedance-2.0": exact},
        }
        self.assertEqual(
            video_prompts._submission_text(segment, "seedance-2.0"), exact)
        self.assertEqual(
            video_prompts._submission_text(segment, "seedance-2.0", extend_url="real-url"), exact)

    def test_missing_exact_model_submission_is_blocked(self):
        segment = {
            "id": "s1", "text": "clean product shot",
            "approved_submission_prompts_by_model": {
                "seedance-2.0": "seedance approved prompt",
            },
        }
        with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_REQUIRED_FOR_MODEL"):
            video_prompts._submission_text(segment, "kling-v3-omni-video")

    def test_capture_covers_all_fallbacks_and_extension_variant(self):
        plan = {
            "client": "unit_client", "storyboard_aspect_ratio": "16:9",
            "output_ratio": "16:9",
            "shots": [{"id": "s1", "duration": 5, "dialogue": "这是确认用的中文台词内容"}],
        }
        segment = {
            "id": "s1", "text": "人物继续介绍产品",
            "seedance_native": False,
            "extend_from_previous": True,
            "audio_contract": {
                "dialogue": "这是确认用的中文台词内容",
                "language": "普通话", "audio_mode": "talking_presenter",
            },
        }
        review = prompt_review.capture_video_segments(
            plan, [segment], model="seedance-2.0")
        item = review["prompts"][0]
        self.assertTrue(item["extend_from_previous"])
        self.assertNotIn("wan2.7-i2v", item["model_submission_prompts"])
        self.assertIn("kling-v3-omni-video", item["model_submission_prompts"])
        for prompt in item["model_submission_prompts"].values():
            self.assertIn("将上一段视频自然延长", prompt)

    def test_chain_capture_shows_dynamic_tail_filters_silent_fallback_and_uses_canonical_plan(self):
        plan = {
            "client": "unit_client", "storyboard_aspect_ratio": "16:9",
            "output_ratio": "16:9", "shots": [
                {"id": "s1", "duration": 5},
                {"id": "s2", "duration": 5},
            ],
        }
        canonical = "canonical-plan-fingerprint"
        segments = [{
            "id": "s1", "text": "讲师开始讲解", "duration": 5,
            "storyboard_plan_fingerprint": canonical,
            "audio_contract": {"dialogue": "第一段确认台词", "language": "普通话"},
        }, {
            "id": "s2", "text": "讲师继续讲解", "duration": 5,
            "storyboard_plan_fingerprint": canonical,
            "chain_required": True,
            "chain_contract": {"predecessor_segment_id": "s1"},
            "audio_contract": {"dialogue": "第二段确认台词", "language": "普通话"},
        }]
        review = prompt_review.capture_video_segments(plan, segments)
        self.assertEqual(review["plan_fingerprint"], canonical)
        self.assertNotEqual(
            review["authored_plan_fingerprint"], review["plan_fingerprint"])
        chained = review["prompts"][1]
        self.assertEqual(
            chained["chain_reference_mode"],
            "previous_approved_segment_tail_plus_confirmed_references")
        self.assertNotIn("wan2.7-i2v", chained["model_submission_prompts"])
        for value in chained["model_submission_prompts"].values():
            self.assertIn("前序段 s1 的最后一帧", value)
        with tempfile.TemporaryDirectory() as tmp:
            review_path = os.path.join(tmp, "review.json")
            prompt_review.save_pending(review, review_path)
            rendered = prompt_review.preview(review_path)
        self.assertIn("@previous_tail", rendered)
        self.assertIn("predecessor_segment_id: `s1`", rendered)

    def test_confirmed_chain_contract_change_requires_recapture(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {
            "id": "s1", "text": "继续讲解", "chain_required": True,
            "chain_contract": {"predecessor_segment_id": "s0"},
        }
        review = _confirmed_review(
            prompt_review.capture_video_segments(plan, [segment]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(
                json.dumps(review, ensure_ascii=False), encoding="utf-8")
            changed = dict(segment, chain_contract={
                "predecessor_segment_id": "different"})
            with self.assertRaisesRegex(
                    ValueError, "PROMPT_REVIEW_CHAIN_MISMATCH"):
                video_prompts._require_confirmed_prompt_review(
                    path, "video", [changed])

    def test_confirmed_review_binds_reference_packet_and_variant(self):
        plan = {
            "client": "unit_client", "storyboard_aspect_ratio": "16:9",
            "output_ratio": "16:9",
            "shots": [{"id": "s1", "duration": 5}],
        }
        segment = {
            "id": "s1", "text": "产品特写", "seedance_native": False,
            "storyboard_panel_index": 1,
            "references": [{"id": "product", "tag": "@product",
                            "type": "product_identity", "url": "https://example.invalid/product.jpg"}],
        }
        pending = prompt_review.capture_video_segments(plan, [segment])
        review = _confirmed_review(pending)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            video_prompts._require_confirmed_prompt_review(path, "video", [segment])
            changed = dict(segment, references=[dict(segment["references"][0],
                                                    url="https://example.invalid/other.jpg")])
            with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_REFERENCE_MISMATCH"):
                video_prompts._require_confirmed_prompt_review(path, "video", [changed])

    def test_short_cta_change_invalidates_confirmed_submission_source(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {
            "id": "s1", "text": "主播口播", "dialogue": "立即购买",
            "audio_contract": {"dialogue": "立即购买", "language": "普通话"},
        }
        review = _confirmed_review(
            prompt_review.capture_video_segments(plan, [segment]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            changed = dict(segment, dialogue="马上下单",
                           audio_contract={"dialogue": "马上下单", "language": "普通话"})
            with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_SOURCE_MISMATCH"):
                video_prompts._require_confirmed_prompt_review(path, "video", [changed])

    def test_draft_locked_refs_cannot_be_injected_after_prompt_confirmation(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {"id": "s1", "text": "clean product shot", "video_type": 1}
        review = _confirmed_review(
            prompt_review.capture_video_segments(plan, [segment]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "PROMPT_REVIEW_(SOURCE|REFERENCE)_MISMATCH"):
                video_engine.render_batch(
                    [segment], draft=True,
                    locked_refs=["https://example.invalid/injected.jpg"],
                    prompt_review=path)

    def test_negative_override_invalidates_confirmed_submission_source(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {"id": "s1", "text": "clean product shot", "video_type": 1}
        review = _confirmed_review(
            prompt_review.capture_video_segments(plan, [segment]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_SOURCE_MISMATCH"):
                video_engine.render_batch(
                    [segment], draft=True, negative_prompt="", prompt_review=path)

    def test_seed_override_invalidates_confirmed_submission_source(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {"id": "s1", "text": "clean product shot", "video_type": 1}
        review = _confirmed_review(
            prompt_review.capture_video_segments(plan, [segment]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_SOURCE_MISMATCH"):
                video_engine.render_batch(
                    [segment], draft=True, seed=7, prompt_review=path)

    def test_chained_seed_override_is_not_silently_ignored(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {"id": "s1", "text": "clean product shot", "video_type": 1}
        review = _confirmed_review(
            prompt_review.capture_video_segments(plan, [segment]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_SOURCE_MISMATCH"):
                video_engine.render_chained(
                    [segment], draft=True, seed=7,
                    prompt_review=path, verbose=False)

    def test_chain_cli_cannot_inject_undeclared_continuity_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = os.path.join(tmp, "segments.json")
            Path(batch).write_text(json.dumps([
                {"id": "s1", "text": "first"},
                {"id": "s2", "text": "second"},
            ]), encoding="utf-8")
            self.assertEqual(video_engine.main(["--batch", batch, "--chain"]), 2)

    def test_chained_library_cannot_inject_undeclared_continuity_contract(self):
        with self.assertRaisesRegex(ValueError, "CHAIN_CONTRACT_REQUIRED"):
            video_engine.render_chained([
                {"id": "s1", "text": "first"},
                {"id": "s2", "text": "second"},
            ], draft=True, prompt_review="confirmed.json", verbose=False)

    def test_chain_preflight_reserves_tail_reference_slot(self):
        segments = [
            {"id": "s1", "urls": ["https://example.invalid/a.jpg"]},
            {"id": "s2", "chain_required": True, "urls": [
                "https://example.invalid/a.jpg",
                "https://example.invalid/b.jpg",
                "https://example.invalid/c.jpg",
                "https://example.invalid/d.jpg",
            ]},
        ]
        preflight = {"segments": [
            {"segment_id": "s1", "model": "kling-v3-omni-video",
             "reference_count": 1},
            {"segment_id": "s2", "model": "kling-v3-omni-video",
             "reference_count": 4},
        ]}
        def capacity(_model, count, **_kwargs):
            if count > 4:
                raise ValueError("REFERENCE_COUNT_UNSUPPORTED")
        with mock.patch.object(video_engine, "_validate_model_reference_capacity",
                               side_effect=capacity):
            with self.assertRaisesRegex(
                    ValueError, "CHAIN_REFERENCE_BUDGET_EXCEEDED"):
                video_engine._validate_chain_reference_budget(
                    segments, preflight, formal=True)

    def test_cli_preflight_reports_reserved_tail_reference_slot(self):
        segments = [
            {"id": "s1", "urls": ["https://example.invalid/identity.jpg"]},
            {"id": "s2", "chain_required": True,
             "chain_contract": {"predecessor_segment_id": "s1"},
             "urls": ["https://example.invalid/identity.jpg"]},
        ]
        preflight = {"ready": True, "blockers": [], "segments": [
            {"segment_id": "s1", "reference_count": 1},
            {"segment_id": "s2", "reference_count": 1},
        ]}
        args = mock.Mock(model="seedance-2.0", no_fallback=False,
                         chain=False, preflight_out=None, manifest=None,
                         client=None, formal_stage="video")
        with mock.patch.object(
                video_engine, "inspect_submission_requirements",
                return_value=preflight), mock.patch.object(
                video_engine, "_validate_chain_reference_budget") as budget, \
                mock.patch("builtins.print"):
            code = video_engine_cli._run_preflight(
                video_engine, segments, args)
        self.assertEqual(code, 0)
        budget.assert_called_once_with(segments, preflight, formal=True)
        self.assertFalse(preflight["segments"][0][
            "chain_tail_reference_reserved"])
        self.assertTrue(preflight["segments"][1][
            "chain_tail_reference_reserved"])
        self.assertEqual(preflight["segments"][1][
            "effective_reference_count"], 2)

    def test_identity_remote_reference_must_match_current_manifest_result(self):
        output = {"path": "/tmp/identity.png", "sha256": "a" * 64,
                  "exists": True}
        result = {"path": "/tmp/identity.result.json", "sha256": "b" * 64,
                  "exists": True}
        url = "https://example.invalid/retrieve/identity.png"
        manifest = {
            "client": "unit", "run_id": "run-1",
            "identity_plate_generation": {
                "output": output, "generation_result": result,
                "remote_url": url,
            },
        }
        segment = {
            "id": "s1", "client": "unit", "run_id": "run-1",
            "urls": [url],
            "references": [{
                "url": url, "source": "identity_plate.approved_candidate",
            }],
            "identity_plate_artifact": {
                "approved_local_artifact": output,
                "generation_result": result, "remote_url": url,
            },
        }
        with mock.patch.object(
                video_guard._rm, "identity_plate_generation_is_complete",
                return_value=True):
            self.assertTrue(video_guard._trusted_remote_segment_reference(
                segment, url, manifest))
            self.assertFalse(video_guard._trusted_remote_segment_reference(
                segment, url, None))
            self.assertFalse(video_guard._trusted_remote_segment_reference(
                segment, "https://example.invalid/retrieve/other.png",
                manifest))
            video_guard._validate_references(
                [segment], "unit", manifest,
                reference_is_trusted=lambda *_args: False)

    def test_adding_confirmed_expanded_panel_invalidates_older_prompt_packet(self):
        plan = {"client": "unit", "storyboard_aspect_ratio": "16:9",
                "output_ratio": "16:9", "shots": [{"id": "s1", "duration": 5}]}
        segment = {"id": "s1", "text": "shot", "seedance_native": False,
                   "storyboard_panel_index": 1}
        pending = prompt_review.capture_video_segments(plan, [segment])
        review = _confirmed_review(pending)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            changed = dict(segment, storyboard_panel={
                "url": "https://example.invalid/panel.jpg",
                "result_url": "https://example.invalid/panel.jpg",
            })
            with self.assertRaisesRegex(ValueError, "PROMPT_REVIEW_REFERENCE_MISMATCH"):
                video_prompts._require_confirmed_prompt_review(path, "video", [changed])

    def test_kling_storyboard_preflight_blocks_before_panel_confirmation(self):
        segment = {
            "id": "s1", "video_type": 4, "storyboard_ref": True,
            "storyboard_ref_mode": "native_storyboard",
            "storyboard_panel_index": 1,
            "ref_tags": ["@host"],
            "references": [{"tag": "@host", "type": "character_identity",
                            "url": "https://example.invalid/host.jpg"}],
        }
        with mock.patch.object(
                video_engine, "_pick_video_model", return_value="kling-v3-omni-video"):
            result = video_engine.inspect_submission_requirements(
                [segment], catalog={"records": {}, "aliases": {}})
        self.assertFalse(result["ready"])
        self.assertTrue(result["segments"][0]["requires_panel_expansion"])
        self.assertTrue(any("panel_expansion.py" in item.get("action", "")
                            for item in result["blockers"]))

    def test_kling_storyboard_preflight_accepts_hash_bound_confirmed_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            storyboard_path = os.path.join(tmp, "storyboard.jpg")
            panel_path = os.path.join(tmp, "panel.jpg")
            Path(storyboard_path).write_bytes(b"approved-storyboard")
            Path(panel_path).write_bytes(b"approved-panel")
            segment = {
                "id": "s1", "video_type": 4, "storyboard_ref": True,
                "storyboard_ref_mode": "native_storyboard",
                "storyboard_path": storyboard_path, "storyboard_dir": tmp,
                "storyboard_panel_index": 1,
                "ref_tags": ["@host"],
                "references": [{"tag": "@host", "type": "character_identity",
                                "url": "https://example.invalid/host.jpg"}],
            }
            recipe_sha = artifact_contract.sha256_json(
                artifact_contract.build_storyboard_panel_recipe(segment))
            panel_sha = artifact_contract.file_sha256(panel_path)
            quality = {
                "pass": True, "score": 93, "method": "automated_vision_qa",
                "checks": {
                    name: True for name in panel_expansion.PANEL_QA_CHECKS
                },
            }
            segment.update({
                "storyboard_panel": {
                    "path": panel_path, "sha256": panel_sha,
                    "recipe_sha256": recipe_sha,
                    "url": "https://example.invalid/panel.jpg",
                },
                "panel_quality": quality,
                "storyboard_panel_approval": {
                    "status": "confirmed", "confirmed_by_customer": True,
                    "customer_confirmation": "我确认这张单格展开图",
                    "confirmation_source": "customer_dialogue",
                    "at": "2026-08-18T12:00:00",
                    "panel_sha256": panel_sha, "recipe_sha256": recipe_sha,
                    "qa_method": "automated_vision_qa",
                    "qa_fingerprint": artifact_contract.sha256_json(quality),
                },
            })
            with mock.patch.object(
                    video_engine, "_pick_video_model",
                    return_value="kling-v3-omni-video"):
                result = video_engine.inspect_submission_requirements(
                    [segment], catalog={"records": {}, "aliases": {}})
        self.assertTrue(result["ready"])
        self.assertEqual(result["segments"][0]["storyboard_ref_mode"], "expanded_panel")
        self.assertTrue(result["segments"][0]["panel_ready"])

    def test_skipped_panel_qa_cannot_be_confirmed_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel_path = os.path.join(tmp, "panel.jpg")
            Path(panel_path).write_bytes(b"panel")
            segment = {
                "id": "s1", "storyboard_ref": True,
                "storyboard_panel_index": 1, "ref_tags": ["@host"],
                "references": [{"tag": "@host", "url": "https://example.invalid/host.jpg"}],
                "storyboard_panel": {"path": panel_path},
                "panel_quality": {"pass": False, "method": "qa_skipped_draft",
                                  "issues": ["QA_SKIPPED_DRAFT"]},
            }
            with self.assertRaisesRegex(ValueError, "PANEL_QUALITY_APPROVAL_REQUIRED"):
                panel_expansion.confirm([segment], customer_confirmation="确认")

    def test_manual_panel_qa_requires_hash_bound_complete_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel_path = os.path.join(tmp, "panel.jpg")
            Path(panel_path).write_bytes(b"panel")
            segment = {
                "id": "s1", "storyboard_ref": True,
                "storyboard_panel_index": 1, "ref_tags": ["@host"],
                "references": [{"tag": "@host", "url": "https://example.invalid/host.jpg"}],
                "storyboard_panel": {"path": panel_path},
                "panel_quality": {"pass": False, "method": "qa_skipped_draft"},
            }
            with self.assertRaisesRegex(ValueError, "MANUAL_PANEL_QA_REPORT_REQUIRED"):
                panel_expansion.confirm(
                    [segment], customer_confirmation="确认", manual_qa=True,
                    manual_qa_report={"status": "passed", "reviewer": "operator"})

    def test_automated_panel_qa_fails_closed_when_checks_are_missing(self):
        with mock.patch.object(
                panel_expansion.br_client, "analyze_image",
                return_value='{"pass":true,"score":99,"checks":{"identity":true}}'), \
                mock.patch.object(panel_expansion.br_client, "pick_vision_model",
                                  return_value="vision-model"):
            result = panel_expansion.assess_panel(
                "key", {"id": "s1", "storyboard_panel_index": 1}, "/tmp/panel.jpg")
        self.assertFalse(result["pass"])

    def test_late_privacy_fallback_pauses_before_supersede_or_paid_resubmit(self):
        task = {
            "idx": 0, "task_id": "seedance-task", "segment": {"id": "s1"},
            "source_segment": {"id": "s1"}, "model": "seedance-2.0",
            "initial_model": "seedance-2.0", "video_type": 5,
            "ref_urls": ["https://example.invalid/host.jpg"],
            "storyboard_ref": True, "fallback_reason": None,
            "attempt": 1, "negative_prompt": "", "seed": None,
        }
        privacy_error = video_engine.br_client.BRVideoReferencePrivacyError(
            "real person reference privacy")
        with mock.patch.object(video_engine.br_client, "get_video",
                               side_effect=privacy_error), \
                mock.patch.object(video_engine, "_privacy_fallback_allowed",
                                  return_value=True), \
                mock.patch.object(
                    video_engine, "_prepare_privacy_fallback_submission",
                    side_effect=ValueError("PANEL_EXPANSION_APPROVAL_REQUIRED")), \
                mock.patch.object(video_engine, "_persist_task") as persist, \
                mock.patch.object(video_engine, "_submit_video") as submit:
            results = video_engine._poll_batch_tasks(
                [task], [task["segment"]], api_key="key", manifest={},
                manifest_path=None, ledger_path=None, draft=False,
                allow_model_fallback=True, max_wait=0, log=lambda _message: None)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["needs_confirmation"])
        self.assertTrue(results[0]["resume_available"])
        self.assertIn("FALLBACK_APPROVAL_REQUIRED", results[0]["error"])
        self.assertEqual(persist.call_args.args[4], "fallback_approval_required")
        submit.assert_not_called()

    def test_chained_privacy_fallback_also_pauses_before_paid_resubmit(self):
        segment = {
            "id": "s1", "text": "shot", "video_type": 5,
            "urls": ["https://example.invalid/host.jpg"],
        }
        privacy_error = video_engine.br_client.BRVideoReferencePrivacyError(
            "real person reference privacy")
        with mock.patch.object(video_engine, "_validate_chain_preflight"), \
                mock.patch.object(video_engine.key_setup, "ensure_session_id"), \
                mock.patch.object(video_engine.key_setup, "load_key", return_value="key"), \
                mock.patch.object(video_engine, "_pick_video_model",
                                  return_value="seedance-2.0"), \
                mock.patch.object(video_engine, "inspect_submission_requirements",
                                  return_value={"ready": True, "blockers": []}), \
                mock.patch.object(video_engine, "_validate_model_reference_capacity"), \
                mock.patch.object(video_engine, "_submission_text",
                                  return_value="approved prompt"), \
                mock.patch.object(video_engine, "_persist_submission_intent",
                                  return_value="request-1"), \
                mock.patch.object(video_engine, "_persist_submission_prompt_snapshot"), \
                mock.patch.object(video_engine, "_persist_task") as persist, \
                mock.patch.object(video_engine, "_submit_video",
                                  return_value=("seedance-task", None)) as submit, \
                mock.patch.object(video_engine.br_client, "wait_video",
                                  side_effect=privacy_error), \
                mock.patch.object(video_engine, "_privacy_fallback_allowed",
                                  return_value=True), \
                mock.patch.object(
                    video_engine, "_prepare_privacy_fallback_submission",
                    side_effect=ValueError("PANEL_EXPANSION_APPROVAL_REQUIRED")):
            results = video_engine.render_chained(
                [segment], draft=True, prompt_review="confirmed.json",
                verbose=False)
        self.assertEqual(submit.call_count, 1)
        self.assertTrue(results[0]["needs_confirmation"])
        self.assertTrue(results[0]["resume_available"])
        self.assertEqual(results[0]["chain_abort_reason"],
                         "fallback_approval_required")
        self.assertEqual(persist.call_args.args[4],
                         "fallback_approval_required")
        self.assertEqual(
            persist.call_args.kwargs["provider_error"]["billing_status"],
            "unknown")
        self.assertNotIn("单格展开图", results[0]["action"].replace(
            "不需要单格展开图", ""))

    def test_late_privacy_fallback_never_auto_submits_even_when_route_is_capable(self):
        segment = {
            "id": "s1", "text": "shot", "video_type": 5,
            "urls": ["https://example.invalid/identity.jpg"],
            "storyboard_usage": "design_board",
        }
        privacy_error = video_engine.br_client.BRVideoReferencePrivacyError(
            "privacy", payload={
                "code": "InputImageSensitiveContentDetected",
                "message": "input image contains a real person and is rejected",
            })
        prepared = {
            "model": "kling-v3-omni-video-white", "segment": segment,
            "source_segment": segment, "ref_urls": segment["urls"],
            "storyboard_panel": None, "submission_text": "approved old text",
        }
        with mock.patch.object(video_engine, "_validate_chain_preflight"), \
                mock.patch.object(video_engine.key_setup, "ensure_session_id"), \
                mock.patch.object(video_engine.key_setup, "load_key", return_value="key"), \
                mock.patch.object(video_engine, "_pick_video_model",
                                  return_value="seedance-2.0"), \
                mock.patch.object(video_engine, "inspect_submission_requirements",
                                  return_value={"ready": True, "blockers": []}), \
                mock.patch.object(video_engine, "_validate_model_reference_capacity"), \
                mock.patch.object(video_engine, "_submission_text",
                                  return_value="approved prompt"), \
                mock.patch.object(video_engine, "_persist_submission_intent",
                                  return_value="request-1"), \
                mock.patch.object(video_engine, "_persist_submission_prompt_snapshot"), \
                mock.patch.object(video_engine, "_persist_task"), \
                mock.patch.object(video_engine, "_submit_video",
                                  return_value=("seedance-task", None)) as submit, \
                mock.patch.object(video_engine.br_client, "wait_video",
                                  side_effect=privacy_error), \
                mock.patch.object(video_engine, "_privacy_fallback_allowed",
                                  return_value=True), \
                mock.patch.object(video_engine,
                                  "_prepare_privacy_fallback_submission",
                                  return_value=prepared):
            results = video_engine.render_chained(
                [segment], draft=True, prompt_review="confirmed.json",
                verbose=False)
        self.assertEqual(submit.call_count, 1)
        self.assertTrue(results[0]["needs_confirmation"])
        self.assertEqual(results[0]["fallback_target_model"],
                         "kling-v3-omni-video-white")
        self.assertEqual(results[0]["provider_error"]["provider_error_code"],
                         "InputImageSensitiveContentDetected")

    def test_submit_time_privacy_fallback_does_not_advance_attempt_before_approval(self):
        segment = {
            "id": "s1", "text": "shot", "video_type": 5,
            "storyboard_ref": True, "urls": ["https://example.invalid/host.jpg"],
        }
        privacy_error = video_engine.br_client.BRVideoReferencePrivacyError(
            "real person reference privacy")
        with mock.patch.object(video_engine, "_pick_video_model",
                               return_value="seedance-2.0"), \
                mock.patch.object(
                    video_engine, "_prepare_storyboard_submission_for_model",
                    return_value=(segment, segment["urls"], None)), \
                mock.patch.object(video_engine, "_submission_text",
                                  return_value="approved prompt"), \
                mock.patch.object(video_engine, "_persist_submission_intent",
                                  return_value="request-1"), \
                mock.patch.object(video_engine, "_persist_submission_prompt_snapshot"), \
                mock.patch.object(video_engine, "_submit_video",
                                  side_effect=privacy_error) as submit, \
                mock.patch.object(video_engine, "_privacy_fallback_allowed",
                                  return_value=True), \
                mock.patch.object(
                    video_engine, "_prepare_privacy_fallback_submission",
                    side_effect=ValueError("PANEL_EXPANSION_APPROVAL_REQUIRED")), \
                mock.patch.object(video_engine, "_current_attempt", return_value=1), \
                mock.patch.object(video_engine, "_mark_submission_failed"):
            tasks = video_engine._submit_batch_tasks(
                [segment], api_key="key", client=None, manifest=None,
                manifest_path=None, ledger_path=None,
                default_model="seedance-2.0", draft=True, seed=None,
                negative_prompt="", allow_model_fallback=True,
                log=lambda _message: None)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(tasks[0]["attempt"], 1)
        self.assertIn("FALLBACK_APPROVAL_REQUIRED", tasks[0]["error"])

    def test_batch_model_fallback_records_the_actual_approved_prompt_snapshot(self):
        segment = {"id": "s1", "text": "shot", "video_type": 1}
        missing = video_engine.br_client.BRError(
            "model not found", http_status=400)
        with mock.patch.object(
                video_engine, "_pick_video_model",
                side_effect=["seedance-2.0", "kling-v3-omni-video"]), \
                mock.patch.object(
                    video_engine, "_submission_text",
                    side_effect=lambda _segment, model, *_args, **_kwargs:
                    "%s approved prompt" % model), \
                mock.patch.object(video_engine, "_persist_submission_intent",
                                  side_effect=["request-1", "request-2"]), \
                mock.patch.object(video_engine, "_persist_submission_prompt_snapshot"), \
                mock.patch.object(
                    video_engine, "_submit_video",
                    side_effect=[missing, ("fallback-task", None)]):
            tasks = video_engine._submit_batch_tasks(
                [segment], api_key="key", client=None, manifest=None,
                manifest_path=None, ledger_path=None,
                default_model="seedance-2.0", draft=True, seed=None,
                negative_prompt="", allow_model_fallback=True,
                log=lambda _message: None)
        self.assertEqual(tasks[0]["model"], "kling-v3-omni-video")
        self.assertEqual(tasks[0]["fallback_reason"], "model_not_found")
        self.assertEqual(
            tasks[0]["submission_prompt_zh"],
            "kling-v3-omni-video approved prompt")

    def test_product_library_references_are_not_silently_truncated(self):
        segment = {
            "id": "s1", "text": "product shot", "video_type": 5,
            "product_sku": "sku-1", "client": "unit",
        }
        resolved = {
            "hero": "https://example.invalid/hero.jpg",
            "refs": [
                "https://example.invalid/a.jpg",
                "https://example.invalid/b.jpg",
                "https://example.invalid/c.jpg",
            ],
            "product_board_confirmed": True,
            "product_board": "https://example.invalid/board.jpg",
        }
        def capacity(_model, count, **_kwargs):
            if count > 4:
                raise ValueError("REFERENCE_COUNT_UNSUPPORTED")
        with mock.patch.object(product_library, "resolve", return_value=resolved), \
                mock.patch.object(video_engine, "_pick_video_model",
                                  return_value="kling-v3-omni-video"), \
                mock.patch.object(video_engine, "_validate_model_reference_capacity",
                                  side_effect=capacity), \
                mock.patch.object(video_engine, "_submit_video") as submit:
            tasks = video_engine._submit_batch_tasks(
                [segment], api_key="key", client="unit", manifest=None,
                manifest_path=None, ledger_path=None,
                default_model="kling-v3-omni-video", draft=True,
                seed=None, negative_prompt="", allow_model_fallback=True,
                log=lambda _message: None)
        self.assertEqual(len(tasks[0]["ref_urls"]), 5)
        self.assertIn("REFERENCE_COUNT_UNSUPPORTED", tasks[0]["error"])
        submit.assert_not_called()

    def test_ambiguous_submit_failure_reuses_same_attempt_and_request_id(self):
        manifest = {"video_attempts": {"s1": {"attempt": 1}}}
        segment = {"id": "s1"}
        request_id = video_engine._submission_request_id(
            segment, "seedance-2.0", 1, "handoff", attempt=1)
        video_engine._mark_submission_failed(
            manifest, None, segment, "handoff", 1, "seedance-2.0",
            request_id, video_engine.br_client.BRError("network timeout"))
        self.assertEqual(manifest["video_attempts"]["s1"]["attempt"], 1)
        self.assertEqual(manifest["tasks"][0]["status"], "failed")
        self.assertTrue(manifest["tasks"][0]["retry_same_request_id"])
        self.assertEqual(
            video_engine._submission_request_id(
                segment, "seedance-2.0", 1, "handoff", attempt=1),
            request_id)

    def test_chained_model_not_found_uses_fallback_and_records_reason(self):
        segment = {"id": "s1", "text": "shot", "video_type": 1}
        missing = video_engine.br_client.BRError(
            "model not found", http_status=400)
        with mock.patch.object(video_engine, "_validate_chain_preflight"), \
                mock.patch.object(video_engine.key_setup, "ensure_session_id"), \
                mock.patch.object(video_engine.key_setup, "load_key", return_value="key"), \
                mock.patch.object(
                    video_engine, "_pick_video_model",
                    side_effect=["seedance-2.0", "seedance-2.0",
                                 "kling-v3-omni-video"]), \
                mock.patch.object(video_engine, "inspect_submission_requirements",
                                  return_value={"ready": True, "blockers": []}), \
                mock.patch.object(video_engine, "_validate_model_reference_capacity"), \
                mock.patch.object(
                    video_engine, "_submission_text",
                    side_effect=lambda _segment, model, *_args, **_kwargs:
                    "%s approved prompt" % model), \
                mock.patch.object(video_engine, "_persist_submission_intent",
                                  side_effect=["request-1", "request-2"]), \
                mock.patch.object(video_engine, "_persist_submission_prompt_snapshot"), \
                mock.patch.object(video_engine, "_persist_task"), \
                mock.patch.object(
                    video_engine, "_submit_video",
                    side_effect=[missing, ("fallback-task", None)]), \
                mock.patch.object(video_engine.br_client, "wait_video",
                                  return_value="https://example.invalid/video.mp4"):
            results = video_engine.render_chained(
                [segment], draft=True, prompt_review="confirmed.json",
                verbose=False)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["model"], "kling-v3-omni-video")
        self.assertEqual(results[0]["fallback_reason"], "model_not_found")
        self.assertEqual(
            results[0]["submission_prompt_zh"],
            "kling-v3-omni-video approved prompt")

    def test_chain_budget_gate_runs_before_generation_lease(self):
        segment = {"id": "s1", "text": "shot", "video_type": 1}
        with mock.patch.object(video_engine, "_validate_chain_preflight"), \
                mock.patch.object(video_engine.key_setup, "ensure_session_id"), \
                mock.patch.object(video_engine.key_setup, "load_key", return_value="key"), \
                mock.patch.object(video_engine, "_pick_video_model",
                                  return_value="seedance-2.0"), \
                mock.patch.object(video_engine, "inspect_submission_requirements",
                                  return_value={"ready": True, "blockers": [],
                                                "segments": []}), \
                mock.patch.object(video_engine, "_validate_chain_reference_budget"), \
                mock.patch.object(
                    video_engine, "_enforce_batch_budget",
                    side_effect=ValueError("BUDGET_CAP_EXCEEDED")), \
                mock.patch.object(
                    video_engine._rm, "acquire_generation_lease") as acquire:
            with self.assertRaisesRegex(ValueError, "BUDGET_CAP_EXCEEDED"):
                video_engine.render_chained(
                    [segment], client="unit", manifest={},
                    manifest_path="manifest.json", results_out="results.json",
                    prompt_review="confirmed.json", budget_cap=0,
                    verbose=False)
        acquire.assert_not_called()

    def test_fallback_approval_task_is_resumable_without_new_seedance_submit(self):
        manifest = {"tasks": [{
            "stage": "video", "unit_id": "s1", "task_id": "seedance-task",
            "handoff_fingerprint": "handoff", "model": "seedance-2.0",
            "attempt": 1, "status": "fallback_approval_required",
        }]}
        resumed = video_engine._rm.find_resumable_task(
            manifest, "video", "s1", "handoff", model="seedance-2.0")
        self.assertEqual(resumed["task_id"], "seedance-task")


if __name__ == "__main__":
    unittest.main()
