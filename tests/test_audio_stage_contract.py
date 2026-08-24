import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import approval_preview
import audio_contract
import run_manifest
import run_manifest_cli
import script_splitter
import stage_output_contract
import video_prompts
import workflow_router


def _choice(**updates):
    value = {
        "mode": "talking_presenter",
        "language": "zh-CN",
        "voice_type": "亲和、清晰的产品经理女声",
        "tone": "轻快但不强推销，关键确认节点适度强调",
        "bgm": "none",
        "pace": "中速偏快",
    }
    value.update(updates)
    return value


class AudioStageContractTests(unittest.TestCase):
    def test_contract_declares_no_standalone_voice_and_test_segment_audition(self):
        choice = audio_contract.normalize_choice(_choice())
        brief = audio_contract.compile_voice_brief(choice)
        contract = audio_contract.create(choice["mode"], voice_brief=brief)

        generation = contract["generation_contract"]
        self.assertEqual(generation["voice_owner"], "video_model_integrated_audio")
        self.assertFalse(generation["standalone_voice_generation_available"])
        self.assertFalse(generation["stage_generates_playable_audio"])
        self.assertEqual(generation["first_generated_audition_stage"], "test_segment")

    def test_incomplete_choice_fails_before_manifest_transaction(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            choice_path = Path(tmp, "choice.json")
            choice = _choice()
            choice.pop("language")
            choice_path.write_text(json.dumps(choice), encoding="utf-8")
            with mock.patch.object(run_manifest, "load_manifest") as load:
                with self.assertRaisesRegex(ValueError, "AUDIO_CHOICE_LANGUAGE_REQUIRED"):
                    audio_contract.prepare_stage(
                        str(Path(tmp, "manifest.json")), str(choice_path))
            load.assert_not_called()

    def test_prepare_stage_closes_success_and_failure_transactions(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            manifest_path = Path(tmp, "manifest.json")
            choice_path = Path(tmp, "choice.json")
            choice_path.write_text(
                json.dumps(_choice(), ensure_ascii=False), encoding="utf-8")
            manifest_path.write_text(json.dumps({
                "schema_version": 5,
                "client": "acme", "run_id": "run-1", "revision": 0,
                "identity": {"client": "acme"},
                "approvals": {}, "approval_hashes": {}, "generation": {},
                "status": "created",
            }), encoding="utf-8")
            with mock.patch.object(
                    run_manifest, "next_required_stage", return_value="audio"), \
                    mock.patch.object(run_manifest, "generation_gate"), \
                    mock.patch.object(run_manifest, "run_output_root", return_value=tmp):
                result = audio_contract.prepare_stage(
                    str(manifest_path), str(choice_path))
            persisted = run_manifest.load_manifest(str(manifest_path))
            self.assertEqual(result["generation_status"], "pending_approval")
            self.assertEqual(
                persisted["generation"]["audio"]["status"], "pending_approval")
            self.assertEqual(
                persisted["generation"]["audio"]["output_contract"]["kind"],
                "audio_direction_stage_output_contract")

            # A fresh run that fails after begin must become failed, never remain
            # in the wait_for_audio deadlock that prompted this regression fix.
            failed_manifest = Path(tmp, "failed_manifest.json")
            failed_manifest.write_text(json.dumps({
                "schema_version": 5,
                "client": "acme", "run_id": "run-2", "revision": 0,
                "identity": {"client": "acme"},
                "approvals": {}, "approval_hashes": {}, "generation": {},
                "status": "created",
            }), encoding="utf-8")
            with mock.patch.object(
                    run_manifest, "next_required_stage", return_value="audio"), \
                    mock.patch.object(run_manifest, "generation_gate"), \
                    mock.patch.object(audio_contract, "write_stage_artifacts",
                                      side_effect=ValueError("LOCAL_RENDER_FAILED")):
                with self.assertRaisesRegex(ValueError, "LOCAL_RENDER_FAILED"):
                    audio_contract.prepare_stage(
                        str(failed_manifest), str(choice_path))
            failed = run_manifest.load_manifest(str(failed_manifest))
            self.assertEqual(failed["generation"]["audio"]["status"], "failed")
            self.assertEqual(failed["status"], "failed_audio")

    def test_stage_artifacts_are_content_addressed_and_strictly_bound(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            result = audio_contract.write_stage_artifacts(
                _choice(), tmp, client="acme", run_id="run-1")
            manifest = {"client": "acme", "run_id": "run-1"}
            contract = stage_output_contract.validate_audio_outputs(
                manifest, result["outputs"])
            self.assertEqual(contract["voice_owner"], "video_model_integrated_audio")
            self.assertEqual(
                contract["first_generated_audition_stage"], "test_segment")
            preview = Path(result["preview"]).read_text(encoding="utf-8")
            self.assertIn("# acme 声音方案确认", preview)
            self.assertIn("不会调用独立 TTS", preview)
            self.assertIn("第一次真实试听：单段测试片 `test_segment`", preview)
            self.assertNotIn("生成可试听的预演音轨", preview)
            self.assertNotIn("声音预演合同", preview)

            with self.assertRaisesRegex(
                    ValueError,
                    "AUDIO_OUTPUT_EXACT_CHOICE_BRIEF_CONTRACT_PREVIEW_REQUIRED"):
                stage_output_contract.validate_audio_outputs(
                    manifest, result["outputs"][:-1])

    def test_approved_direction_reaches_segment_and_paid_prompt(self):
        choice = audio_contract.normalize_choice(_choice())
        brief = audio_contract.compile_voice_brief(choice)
        direction = {
            "contract": audio_contract.create(
                choice["mode"], voice_brief=brief),
            "artifact": {"path": "/tmp/audio-contract.json", "sha256": "a" * 64},
            "stage_input_fingerprint": "b" * 64,
        }
        compiled = script_splitter._build_audio_contract(
            {"audio": {"voice": "未确认旧音色"}, "language": "zh-CN"},
            {"id": "s1", "characters": ["host"]}, "这是确认台词",
            direction)
        self.assertEqual(compiled["audio_mode"], "talking_presenter")
        self.assertEqual(compiled["voice"], choice["voice_type"])
        self.assertIn(choice["tone"], compiled["voice_direction"])
        self.assertEqual(
            compiled["approved_direction"]["artifact"], direction["artifact"])
        prompt = video_prompts._submission_text({
            "id": "s1", "text": "干净讲解画面", "dialogue": "这是确认台词",
            "audio_contract": compiled, "seedance_native": False,
        }, "kling-v3-omni-video", allow_unconfirmed=True)
        self.assertIn(choice["tone"], prompt)

        music = audio_contract.create(
            "music_only", voice_brief=audio_contract.compile_voice_brief({
                "mode": "music_only", "bgm": "轻快科技氛围", "feedback": ""}))
        with self.assertRaisesRegex(ValueError, "AUDIO_DIRECTION_DIALOGUE_CONFLICT"):
            script_splitter._build_audio_contract(
                {}, {"id": "s1"}, "不应存在的台词", {"contract": music})

    def test_review_preparation_keeps_audio_preview_bound_to_all_inputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            result = audio_contract.write_stage_artifacts(
                _choice(), tmp, client="acme", run_id="run-1")
            manifest = {
                "client": "acme", "run_id": "run-1",
                "identity": {"client": "acme"},
                "approvals": {"audio": False},
                "generation": {"audio": {
                    "status": "pending_approval",
                    "outputs": result["outputs"],
                    "artifacts": [run_manifest.file_record(path)
                                  for path in result["outputs"]],
                    "output_contract": stage_output_contract.validate_audio_outputs(
                        {"client": "acme", "run_id": "run-1"},
                        result["outputs"]),
                }},
            }
            with mock.patch.object(
                    run_manifest, "run_output_root", return_value=tmp):
                prepared = approval_preview.ensure_stage_preview(manifest, "audio")
            self.assertEqual(prepared["preview_path"], result["preview"])
            self.assertEqual(
                prepared["stage_input_fingerprint"],
                run_manifest.stage_input_fingerprint(manifest, "audio"))

    def test_router_collects_choice_before_specialized_audio_transaction(self):
        manifest = {
            "chain_profile": {"profile": "generic_video", "frozen": True},
        }
        flow = workflow_router._resolved_flow(
            "audio", manifest["chain_profile"], manifest)
        self.assertEqual(flow["transaction"]["mode"], "specialized_atomic")
        self.assertIn("audio_contract.py prepare", flow["entry_functions"][0]["cli"])
        self.assertNotIn("start-stage", flow["entry_functions"][0]["cli"])

        with self.assertRaisesRegex(
                ValueError, "AUDIO_STAGE_SPECIALIZED_PREPARE_REQUIRED"):
            run_manifest_cli._dispatch(
                run_manifest, {}, SimpleNamespace(
                    command="start-stage", stage="audio"))

    def test_historical_v1_contract_remains_readable(self):
        legacy = {
            "schema_version": 1,
            "audio_mode": "talking_presenter",
            "voice_brief": {"voice_type": "历史音色"},
            "allow_visible_speech": True,
        }
        self.assertIs(audio_contract.validate(legacy), legacy)


if __name__ == "__main__":
    unittest.main()
