import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import explainer_layout_preview
import cast_reference_pack
import hf_engine
import motion_design
import run_manifest
import script_splitter
import video_engine
import visible_layer_contract


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class VisibleLayerContractTests(unittest.TestCase):
    @staticmethod
    def _write_cast_board(path):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (900, 600), "white")
        draw = ImageDraw.Draw(image)
        colors = ("red", "green", "blue", "yellow", "purple", "orange")
        for index, color in enumerate(colors):
            column, row = index % 3, index // 3
            draw.rectangle(
                (column * 300, row * 300, (column + 1) * 300 - 1,
                 (row + 1) * 300 - 1), fill=color)
        image.save(path, format="JPEG", quality=95)

    def test_director_instructions_never_become_visible_overlay_copy(self):
        shot = {
            "id": "s1", "dialogue": "这是观众应该看到的字幕。",
            "storyboard_role": "design_board",
            "motion_elements": ["白色卡片从左侧滑入", "主持人区域保持干净"],
            "motion_design": {
                "subtitle": {"text": "这是观众应该看到的字幕。", "safe_zone": "lower_third"},
                "motion_overlay": {"style": "title_reveal", "title": "明确标题"},
            },
            "asset_slots": [
                {"slot": "motion_overlay_bullet_1", "description": "主持人区域保持干净",
                 "renderable": True},
            ],
        }
        plan = motion_design.ensure_motion_plan_contract(shot, {"storyboard_role": "design_board"})
        by_name = {item["slot"]: item for item in plan["asset_slots"]}
        self.assertNotIn("motion_overlay_bullet_1", by_name)
        self.assertEqual(by_name["motion_element_1"]["source_kind"], "director_instruction")
        self.assertFalse(by_name["motion_element_1"]["renderable"])
        self.assertEqual(by_name["motion_overlay"]["description"], "明确标题")

    def test_hyperframes_compiler_rejects_renderable_director_note(self):
        spec = hf_engine.compile_motion_plan_to_spec({
            "duration": 3,
            "asset_slots": [{
                "slot": "motion_element_1", "kind": "motion_element",
                "description": "制作说明", "renderable": True,
            }],
        }, end=3)
        self.assertEqual(spec["scenes"], [])
        self.assertEqual(spec["motion_grammar"]["elements"][0]["source_kind"],
                         "director_instruction")

    def test_document_timeline_resolves_remotion_hyperframes_ownership(self):
        motion_plan = {
            "duration": 4, "safe_zone": "lower_third",
            "asset_slots": [
                {"slot": "subtitle", "kind": "subtitle", "description": "口播字幕",
                 "renderable": True, "source_kind": "audience_copy", "owner": "hyperframes"},
                {"slot": "motion_overlay", "kind": "title_reveal", "description": "内容标题",
                 "renderable": True, "source_kind": "audience_copy", "owner": "hyperframes"},
            ],
        }
        fusion = {
            "width": 1920, "height": 1080, "fps": 30,
            "content_spec": {"scenes": [{
                "sceneId": "s1", "props": {"title": "内容标题"},
            }]},
            "edl": {"total_duration": 4, "shots": [{
                "id": "s1", "start_sec": 0, "end_sec": 4, "duration": 4,
                "visual_mode": "split_left_right", "motion_plan": motion_plan,
            }]},
        }
        spec = explainer_layout_preview.build_hyperframes_timeline_spec(fusion)
        self.assertEqual([item["slot"] for item in spec["scenes"]], ["subtitle"])
        self.assertTrue(spec["visible_layer_contract"]["ok"])
        reasons = {item["reason"] for item in spec["deduplicated_scenes"]}
        self.assertIn("remotion_already_owns_visible_copy", reasons)

    def test_geometry_audit_blocks_colliding_exclusive_overlays(self):
        scenes = []
        for slot in ("a", "b"):
            scenes.append({
                "source_shot_id": "s1", "slot": slot, "text": slot,
                "source_kind": "audience_copy", "owner": "hyperframes",
                "start": 0, "end": 3, "left_px": 20, "top_px": 20,
                "width_px": 400, "max_height_px": 120,
            })
        result = visible_layer_contract.audit(
            {"resolution": [1920, 1080], "scenes": scenes},
            {"edl": {"shots": [{"id": "s1", "visual_mode": "split_left_right"}]}},
            {"scenes": []})
        codes = {item["code"] for item in result["errors"]}
        self.assertIn("HYPERFRAMES_VISIBLE_GEOMETRY_COLLISION", codes)

    def test_remotion_slide_slots_fail_closed_on_visibility_ownership(self):
        source = Path(ROOT, "remotion_engine", "src", "content", "SceneRenderer.tsx").read_text(
            encoding="utf-8")
        self.assertIn('slot.owner === "remotion"', source)
        self.assertIn('slot.source_kind === "audience_copy"', source)
        self.assertNotIn('slots.some((slot) => slot.slot === "title_area")', source)

    def test_formal_preview_auto_binds_current_confirmed_cast_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = os.path.join(tmp, "cast_board.jpg")
            Path(board).write_bytes(b"confirmed-cast")
            marker = os.path.join(tmp, ".cast_confirmed.json")
            Path(marker).write_text(json.dumps({
                "path": board, "board_sha256": sha(board),
                "confirmed_by_customer": True,
            }), encoding="utf-8")
            manifest = {"approval_evidence": {"cast_board": {
                "approval_path": marker, "artifact_sha256": sha(board),
            }}}
            with mock.patch.object(
                    explainer_layout_preview.run_manifest_store,
                    "stage_required", return_value=True), mock.patch.object(
                    explainer_layout_preview.run_manifest_store,
                    "approval_is_current", return_value=True):
                resolved = explainer_layout_preview._confirmed_cast_board_from_manifest(manifest)
            self.assertEqual(resolved, board)

    def test_formal_revision_directory_is_deterministic_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")
            source = os.path.join(tmp, "plan.json")
            Path(manifest_path).write_text("{}", encoding="utf-8")
            Path(source).write_text("{}", encoding="utf-8")
            with mock.patch.object(
                    explainer_layout_preview.run_manifest_store,
                    "load_manifest", return_value={}), mock.patch.object(
                    explainer_layout_preview, "_confirmed_cast_board_from_manifest",
                    return_value=None):
                first = explainer_layout_preview._formal_preview_revision(
                    {"shots": []}, tmp, manifest_path=manifest_path,
                    source_plan=source, human_image=None, preview_audio=None, quality="high")
                second = explainer_layout_preview._formal_preview_revision(
                    {"shots": []}, tmp, manifest_path=manifest_path,
                    source_plan=source, human_image=None, preview_audio=None, quality="high")
            self.assertEqual(first, second)
            self.assertIn(os.path.join("revisions", first[2]), first[0])

    def test_cast_board_is_split_into_six_auditable_single_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = os.path.join(tmp, "cast_board.jpg")
            self._write_cast_board(board)
            pack = cast_reference_pack.derive(board, os.path.join(tmp, "pack"))
            self.assertEqual(set(pack["views"]), set(cast_reference_pack.VIEW_CELLS))
            self.assertEqual(pack["primary_identity_anchor"], "face_front")
            self.assertEqual(pack["full_body_identity_anchor"], "full_body_front")
            self.assertTrue(pack["source_board"]["review_artifact_only"])
            self.assertFalse(pack["source_board"]["direct_video_model_input_allowed"])
            for view in pack["views"].values():
                self.assertTrue(os.path.isfile(view["path"]))
                self.assertEqual(view["sha256"], sha(view["path"]))
                self.assertFalse(view["remote_video_input_allowed"])
            cached = cast_reference_pack.derive(board, os.path.join(tmp, "pack"))
            self.assertEqual(pack, cached)

    def test_cast_reference_pack_matches_schema(self):
        import jsonschema

        with tempfile.TemporaryDirectory() as tmp:
            board = os.path.join(tmp, "cast_board.jpg")
            self._write_cast_board(board)
            pack = cast_reference_pack.derive(board, os.path.join(tmp, "pack"))
            schema = json.loads(Path(
                ROOT, "schemas", "cast-reference-pack.schema.json").read_text(
                    encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(pack)

    def test_video_engine_forbids_cast_contact_sheet_even_if_handcrafted(self):
        segment = {
            "id": "s1", "required_reference_types": [],
            "references": [{
                "id": "ref_01", "type": "character_board",
                "url": "https://example.invalid/cast-board.jpg",
            }],
        }
        with self.assertRaisesRegex(
                ValueError, "CAST_CONTACT_SHEET_VIDEO_INPUT_FORBIDDEN"):
            video_engine._validate_reference_handoff([segment])

    def test_formal_human_shot_requires_single_person_identity(self):
        required = script_splitter._required_reference_types(
            has_human=True, storyboard_required=False)
        self.assertIn("character_identity", required)
        self.assertNotIn("character_board", required)

    def test_segment_compiler_filters_cast_contact_sheet_from_references(self):
        references, dropped = script_splitter._collect_typed_references(
            {"id": "s1", "ref_tags": ["@host"]},
            {"reference_registry": [{
                "tag": "@host", "type": "character_board",
                "url": "https://example.invalid/cast-board.jpg",
            }]},
            None, include_storyboard_ref=False)
        self.assertEqual(references, [])
        self.assertEqual(dropped[0]["reason"],
                         "approval_contact_sheet_not_video_input")

    def test_stale_worker_cannot_fail_current_generation_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            manifest = run_manifest.create_manifest("acme", "lease-owner-test")
            run_manifest.save_manifest(manifest, path, create_only=True)
            run_manifest.acquire_generation_lease(
                path, "brief", "owner-a", ttl_seconds=300)
            run_manifest.record_generation_failure_if_running(
                path, "brief", "stale worker failed", owner_token="owner-b")
            current = run_manifest.load_manifest(path)
            self.assertEqual(current["generation"]["brief"]["status"], "running")
            self.assertEqual(
                current["generation"]["brief"]["lease"]["owner_token"],
                "owner-a")

    def test_execution_preview_transaction_is_bound_to_lease_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "plan.json")
            Path(source).write_text("{}", encoding="utf-8")
            out_dir = os.path.join(tmp, "revision")
            os.makedirs(out_dir)
            plan = {"shots": []}
            code_sha = "a" * 64
            manifest = {"generation": {"storyboard": {
                "status": "running", "render_quality": "high",
                "source_plan": run_manifest.file_record(source),
                "plan_fingerprint":
                    explainer_layout_preview._execution_plan_fingerprint(plan),
                "preview_code_sha256": code_sha,
                "planned_output_dir": out_dir,
                "planned_result": os.path.join(
                    out_dir, "execution_preview_result.json"),
                "planned_animatic": os.path.join(out_dir, "layout_animatic.mp4"),
                "lease": {"owner_token": "owner-a"},
            }}}
            with mock.patch.object(
                    explainer_layout_preview, "_execution_preview_code_sha",
                    return_value=code_sha):
                with self.assertRaisesRegex(
                        ValueError, "EXECUTION_PREVIEW_RUNNING_TRANSACTION_REQUIRED"):
                    explainer_layout_preview._require_execution_preview_transaction(
                        manifest, source_plan=source, plan=plan,
                        out_dir=out_dir, quality="high", lease_owner="owner-b")
                result = explainer_layout_preview._require_execution_preview_transaction(
                    manifest, source_plan=source, plan=plan,
                    out_dir=out_dir, quality="high", lease_owner="owner-a")
            self.assertEqual(result["lease"]["owner_token"], "owner-a")


if __name__ == "__main__":
    unittest.main()
