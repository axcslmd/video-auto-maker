import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


import design_token_contract
import layout_compiler
import postproduction_motion_director
import remotion_engine
import render_spec_compiler
import styleframe
import visual_intent


class RenderQualityCompilerTests(unittest.TestCase):
    def setUp(self):
        self.tokens = styleframe.build_tokens(
            {"colors": {"primary": "#335CFF", "accent": "#24C18A"}},
            direction="clean_corporate", energy="medium")
        self.plan = {
            "width": 1920, "height": 1080, "fps": 30, "output_ratio": "16:9",
            "shots": [
                {
                    "id": "s1", "duration": 3.0, "content_scene_kind": "metrics",
                    "title": "增长 42%",
                    "content_props": {"metrics": [{"value": "42%", "label": "增长"}]},
                },
                {
                    "id": "s2", "duration": 2.0, "title": "开始体验",
                    "visual_intent": {
                        "semantic_kind": "cta", "single_visual_thesis": "开始体验",
                        "primary_subject": "行动按钮",
                    },
                },
            ],
        }
        self.props = {
            "width": 1920, "height": 1080, "fps": 30,
            "brandPrimary": "#335CFF",
            "scenes": [
                {"kind": "metrics", "durationInFrames": 90, "props": {"title": "增长 42%"}},
                {"kind": "section", "durationInFrames": 60, "props": {"title": "开始体验"}},
            ],
        }

    def test_art_directions_change_executable_visual_system(self):
        premium = styleframe.build_tokens(direction="premium_editorial")
        corporate = styleframe.build_tokens(direction="clean_corporate")
        self.assertNotEqual(premium["color"]["background"], corporate["color"]["background"])
        self.assertNotEqual(premium["surface"]["material"], corporate["surface"]["material"])
        self.assertNotEqual(premium["typography"]["line_height"], corporate["typography"]["line_height"])
        self.assertEqual(design_token_contract.validate(premium), premium)

    def test_styleframe_cli_creates_nested_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "new", "nested", "tokens.json")
            self.assertEqual(styleframe.main(["--direction", "technical_system",
                                               "--out", target]), 0)
            self.assertTrue(os.path.isfile(target))

    def test_design_token_tamper_invalidates_contract(self):
        tampered = json.loads(json.dumps(self.tokens))
        tampered["color"]["primary"] = "#FF0000"
        with self.assertRaisesRegex(ValueError, "DESIGN_TOKEN_CONTRACT_STALE"):
            design_token_contract.validate(tampered)

    def test_structured_visual_intent_drives_shotcraft_before_keywords(self):
        shot = self.plan["shots"][0]
        intent = visual_intent.compile_shot(shot)
        self.assertEqual(intent["component_family"], "metric")
        shot = dict(shot, visual_intent=intent, marketing_intent="brand outro")
        self.assertEqual(postproduction_motion_director.infer_intent(shot), "metric")

    def test_formal_layout_blocks_over_capacity_and_protected_crop(self):
        too_dense = json.loads(json.dumps(self.plan))
        too_dense["shots"][0]["title"] = "信息" * 90
        with self.assertRaisesRegex(ValueError, "content_capacity_exceeded"):
            layout_compiler.compile_layouts(too_dense, tokens=self.tokens, strict=True)

        protected = json.loads(json.dumps(self.plan))
        protected["shots"][0]["ref_tags"] = ["@product"]
        with self.assertRaisesRegex(ValueError, "source_ratio_required"):
            layout_compiler.compile_layouts(protected, tokens=self.tokens, strict=True)

    def test_formal_render_spec_is_complete_and_tamper_evident(self):
        spec = render_spec_compiler.compile_spec(
            self.plan, self.tokens, engine_props=self.props,
            composition="Content", formal=True)
        self.assertEqual(spec["unresolved"], [])
        self.assertEqual(spec["renderer"]["frame_format"], "png")
        self.assertEqual(spec["engine_props"]["designTokens"]["schema_version"], 3)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "render-spec.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(spec, handle)
            with mock.patch.object(remotion_engine, "render", return_value="ok") as renderer:
                self.assertEqual(remotion_engine.render_quality_spec(
                    path, os.path.join(tmp, "out.mp4")), "ok")
                self.assertEqual(renderer.call_args.kwargs["profile"], "direct_delivery")
            spec["engine_props"]["width"] = 1080
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(spec, handle)
            with self.assertRaisesRegex(SystemExit, "RENDER_SPEC_STALE"):
                remotion_engine.render_quality_spec(path, os.path.join(tmp, "bad.mp4"))

    def test_encoder_profiles_prevent_accidental_delivery_defaults(self):
        delivery = remotion_engine.render_profile_args("direct_delivery", "out.mp4")
        master = remotion_engine.render_profile_args("commercial_master", "out.mov")
        self.assertIn("png", delivery)
        self.assertIn("16", delivery)
        self.assertIn("yuv422p10le", master)
        self.assertEqual(remotion_engine.resolve_render_profile("high", "out.mov"),
                         "commercial_master")
        with self.assertRaisesRegex(ValueError, "REQUIRES_MOV"):
            remotion_engine.render_profile_args("commercial_master", "out.mp4")


if __name__ == "__main__":
    unittest.main()
