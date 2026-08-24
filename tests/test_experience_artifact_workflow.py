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


import artifact_registry
import architecture_audit
import approval_preview
import agent_runtime
import policy_check
import review_task
import run_manifest
import schema_validate
import stage_packet
import video_engine
import workflow_api
import workflow_canvas
import workflow_contract
import workflow_router


class ExperienceArtifactWorkflowTests(unittest.TestCase):
    def test_architecture_boundaries_have_no_cycles_or_state_owner_leaks(self):
        result = architecture_audit.audit()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["metrics"]["import_cycle_count"], 0)
        self.assertEqual(result["metrics"]["approval_state_owner_violations"], 0)
        self.assertEqual(result["metrics"]["broad_exception_count"], 0)
        self.assertEqual(result["metrics"]["large_function_count"], 0)
        self.assertEqual(result["metrics"]["broad_exception_budget"], 0)
        self.assertEqual(result["metrics"]["large_function_budget"], 0)

    def test_manifest_invalidation_is_central_and_prunes_only_affected_reviews(self):
        manifest = {
            "chain_profile": {"profile": "product_with_human"},
            "requires_product_board": True,
            "requires_cast_board": True,
            "requires_product_usage": True,
            "approvals": {
                "script": True, "cast_board": True, "product_board": True,
                "product_usage": True, "storyboard": True,
            },
            "approval_hashes": {
                "script": "s", "cast_board": "c", "product_board": "p",
                "product_usage": "u", "storyboard": "b",
            },
            "approval_evidence": {
                "script": {"id": "s"}, "storyboard": {"id": "b"},
                "product_board": {"id": "p"},
            },
            "review_presentations": {
                "r-script": {"stage": "script"},
                "r-story": {"stage": "storyboard"},
                "r-product": {"stage": "product_board"},
            },
        }
        result = run_manifest.invalidate_from(manifest, "script")
        self.assertIn("storyboard", result["stages"])
        self.assertFalse(manifest["approvals"]["script"])
        self.assertFalse(manifest["approvals"]["storyboard"])
        self.assertNotIn("storyboard", manifest["approval_evidence"])
        self.assertNotIn("r-story", manifest["review_presentations"])
        self.assertTrue(manifest["approvals"]["product_board"])
        self.assertIn("r-product", manifest["review_presentations"])

    def test_formal_policy_is_indeterminate_when_submission_assets_are_missing(self):
        result = policy_check.evaluate("submit_video", {})
        json.dumps(result)
        self.assertEqual(result["decision"], "indeterminate")
        self.assertTrue(any(item["gate_id"] == "GATE-002"
                            for item in result["indeterminate"]))
        with self.assertRaises(policy_check.PolicyBlock):
            policy_check.enforce("submit_video", {})

    def test_product_human_profile_can_be_authorized_by_oral_or_tvc_package(self):
        base = {
            "storyboard_role": "video_storyboard",
            "characters": [{"id": "host"}],
            "asset_refs": {"product_images": ["product.jpg"]},
        }
        for package in ("oral-broadcast", "brand-tvc"):
            with self.subTest(package=package):
                result = workflow_contract.validate_package_binding(
                    dict(base, package=package), require_authored=True)
                self.assertTrue(result["ok"], result["errors"])
        explicit_oral = dict(base, command="/oral-broadcast", package="brand-tvc")
        result = workflow_contract.validate_package_binding(
            explicit_oral, require_authored=True)
        self.assertFalse(result["ok"])

    def test_artifact_registry_exposes_ready_nodes_and_four_milestones(self):
        manifest = {
            "client": "acme", "run_id": "run-1",
            "chain_profile": {"profile": "product_with_human"},
            "requires_product_board": True,
            "requires_cast_board": True,
            "requires_product_usage": True,
            "requires_content_scaffold": False,
            "requires_motion_design": False,
            "requires_styleframe": False,
            "requires_audio": False,
            "requires_test_segment": False,
            "requires_shotcraft_packaging": False,
            "approvals": {"brief": True, "script": True},
            "generation": {},
        }
        with mock.patch("run_manifest.approval_is_current",
                        side_effect=lambda _, stage: stage in {"brief", "script"}):
            registry = artifact_registry.compile_registry(manifest)
        self.assertEqual([item["id"] for item in registry["milestones"]],
                         ["plan", "previs", "generation_review", "finish_delivery"])
        ready = {item["artifact_id"] for item in registry["ready_actions"]}
        self.assertIn("artifact:product_board", ready)
        self.assertIn("artifact:cast_board", ready)
        self.assertNotIn("artifact:product_usage", ready)
        self.assertEqual(schema_validate.validate(
            registry, "artifact-registry"), [])

    def test_invalidation_preview_preserves_unrelated_identity_board(self):
        manifest = {
            "client": "acme", "run_id": "run-1",
            "chain_profile": {"profile": "product_with_human"},
            "requires_product_board": True,
            "requires_cast_board": True,
            "requires_product_usage": True,
            "requires_content_scaffold": False,
            "requires_motion_design": False,
            "requires_styleframe": False,
            "requires_audio": False,
            "requires_test_segment": False,
            "requires_shotcraft_packaging": False,
            "approvals": {}, "generation": {},
        }
        with mock.patch("run_manifest.approval_is_current", return_value=False):
            impact = artifact_registry.preview_invalidation(
                manifest, ["artifact:product_board"])
        self.assertIn("artifact:product_usage", impact["will_invalidate"])
        self.assertIn("artifact:storyboard", impact["will_invalidate"])
        self.assertIn("artifact:cast_board", impact["will_preserve"])

    def test_review_presentation_is_hash_bound_and_reusable_only_for_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp, "preview.jpg")
            preview.write_bytes(b"v1")
            manifest = {"client": "acme", "run_id": "run-1", "generation": {
                "storyboard": {"status": "pending_approval", "outputs": [str(preview)],
                               "artifacts": [run_manifest.file_record(str(preview))]}}}
            receipt = review_task.present(
                manifest, "storyboard", [str(preview)], channel="codex_markdown")
            self.assertTrue(review_task.validate_presentation(
                manifest, "storyboard", receipt["presentation_id"], [str(preview)]))
            preview.write_bytes(b"v2")
            with self.assertRaisesRegex(ValueError, "REVIEW_PRESENTATION_STALE"):
                review_task.validate_presentation(
                    manifest, "storyboard", receipt["presentation_id"], [str(preview)])

    def test_review_presentation_stales_when_upstream_approval_contract_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp, "preview.jpg")
            preview.write_bytes(b"same-bytes")
            manifest = {
                "client": "acme", "run_id": "run-1",
                "chain_profile": {"profile": "generic_video"},
                "approval_hashes": {"script": "before"},
                "generation": {"storyboard": {
                    "status": "pending_approval", "outputs": [str(preview)],
                    "artifacts": [run_manifest.file_record(str(preview))]}},
            }
            with mock.patch("run_manifest.approval_is_current", return_value=True):
                receipt = review_task.present(
                    manifest, "storyboard", [str(preview)], channel="codex_markdown")
                manifest["approval_hashes"]["script"] = "after"
                with self.assertRaisesRegex(ValueError, "REVIEW_PRESENTATION_STALE"):
                    review_task.validate_presentation(
                        manifest, "storyboard", receipt["presentation_id"], [str(preview)])

    def test_review_presentation_stales_when_hidden_stage_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "brief.json")
            preview = Path(tmp, "brief.md")
            source.write_text('{"objective":"before"}', encoding="utf-8")
            preview.write_text("visible preview", encoding="utf-8")
            manifest = {
                "client": "acme", "run_id": "run-1", "generation": {"brief": {
                    "status": "pending_approval",
                    "outputs": [str(source), str(preview)],
                    "artifacts": [run_manifest.file_record(str(source)),
                                  run_manifest.file_record(str(preview))],
                }},
            }
            receipt = review_task.present(
                manifest, "brief", [str(preview)], channel="codex_markdown")
            source.write_text('{"objective":"after"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "待审批源文件已变更"):
                review_task.validate_presentation(
                    manifest, "brief", receipt["presentation_id"], [str(preview)])

    def test_brief_preview_is_deterministic_source_bound_and_customer_readable(self):
        brief = {
            "client": "acme", "objective": "介绍 <产品>", "audience": "市场团队",
            "primary_usp": "先确认，再生成", "secondary_points": ["减少返工"],
            "cta": "开始第一条视频", "profile": "document_explainer",
            "channel": "LinkedIn", "output_ratio": "16:9", "tone": "专业克制",
            "storyboard_role": "design_board", "fact_refs": ["ppt.product"],
            "digital_human": {"enabled": True, "role": "产品讲师"},
            "texts": [{"slide": 1, "text": "source text"}],
        }
        first = approval_preview.compile_brief_preview(
            brief, source_sha256="a" * 64, client="acme", run_id="run-1")
        second = approval_preview.compile_brief_preview(
            brief, source_sha256="a" * 64, client="acme", run_id="run-1")
        self.assertEqual(first, second)
        self.assertIn("先确认，再生成", first)
        self.assertIn("&lt;产品&gt;", first)
        self.assertIn("\"source_sha256\":\"%s\"" % ("a" * 64), first)
        self.assertNotIn("source text", first)

    def test_ensure_brief_preview_refreshes_source_and_preview_as_one_contract(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            source = Path(tmp, "brief.json")
            output = Path(tmp, "run")
            output.mkdir()
            source.write_text(json.dumps({
                "client": "acme", "objective": "首版目标", "audience": "市场团队",
                "primary_usp": "可确认", "cta": "开始", "channel": "官网",
                "output_ratio": "16:9", "tone": "专业",
            }, ensure_ascii=False), encoding="utf-8")
            manifest = {
                "client": "acme", "run_id": "run-1", "approvals": {"brief": False},
                "identity": {"client": "acme"},
                "generation": {"brief": {
                    "status": "pending_approval", "outputs": [str(source)],
                    "artifacts": [run_manifest.file_record(str(source))],
                }},
            }
            source.write_text(source.read_text(encoding="utf-8").replace(
                "首版目标", "更新目标"), encoding="utf-8")
            with mock.patch.object(run_manifest, "run_output_root",
                                   return_value=str(output)):
                result = approval_preview.ensure_stage_preview(manifest, "brief")
            preview = Path(result["preview_path"])
            self.assertIn("更新目标", preview.read_text(encoding="utf-8"))
            self.assertEqual(manifest["generation"]["brief"]["outputs"],
                             [str(source), str(preview)])
            self.assertEqual(
                manifest["generation"]["brief"]["artifacts"][0]["sha256"],
                run_manifest.file_record(str(source))["sha256"])
            self.assertEqual(result["stage_input_fingerprint"],
                             run_manifest.stage_input_fingerprint(manifest, "brief"))

    def test_review_presentation_save_failure_does_not_mutate_caller_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp, "preview.jpg")
            preview.write_bytes(b"preview")
            manifest = {
                "client": "acme", "run_id": "run-1", "revision": 3,
                "generation": {"storyboard": {
                    "status": "pending_approval", "outputs": [str(preview)],
                    "artifacts": [run_manifest.file_record(str(preview))],
                }},
            }
            route = {"current_stage": "storyboard", "customer_preview": [str(preview)]}
            with mock.patch.object(workflow_api, "_route", return_value=route), \
                    mock.patch.object(workflow_api, "_current_preview_paths",
                                      return_value=[str(preview)]), \
                    mock.patch.object(run_manifest, "save_manifest",
                                      side_effect=run_manifest.ManifestConflictError(
                                          "MANIFEST_REVISION_CONFLICT")):
                with self.assertRaises(run_manifest.ManifestConflictError):
                    workflow_api.workflow_review_present(
                        manifest, str(Path(tmp, "manifest.json")))
            self.assertNotIn("review_presentations", manifest)
            self.assertEqual(manifest["revision"], 3)

    def test_paid_video_policy_uses_real_manifest_approvals(self):
        manifest = {
            "requires_product_board": True,
            "requires_cast_board": False,
            "requires_product_usage": False,
            "approval_hashes": {"storyboard": "sb", "product_board": "pb"},
        }
        segment = {"id": "s1", "model": "seedance-2.0", "duration": 5,
                   "video_type": 5, "required_reference_types": [],
                   "actual_reference_types": [], "urls": []}
        with mock.patch.object(
                video_engine._rm, "approval_is_current",
                side_effect=lambda _, stage: stage == "storyboard"):
            with self.assertRaises(policy_check.PolicyBlock):
                video_engine._enforce_batch_budget(
                    [segment], "seedance-2.0", None, lambda _: None,
                    manifest=manifest, preflight_ready=True, strict=True)

    def test_confirmation_requires_hash_bound_presentation_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp, "preview.jpg")
            preview.write_bytes(b"preview")
            route = {"current_stage": "storyboard",
                     "customer_preview": [str(preview)]}
            with mock.patch.object(workflow_api, "_route", return_value=route), \
                    mock.patch.object(workflow_api, "_current_preview_paths",
                                      return_value=[str(preview)]):
                with self.assertRaisesRegex(ValueError, "REVIEW_PRESENTATION_REQUIRED"):
                    workflow_api.workflow_confirm(
                        {}, str(Path(tmp, "manifest.json")), "我确认")

    def test_canvas_defaults_to_four_customer_milestones(self):
        steps, registry = workflow_canvas._build_milestone_steps({}, "storyboard")
        self.assertEqual([item["id"] for item in steps],
                         ["plan", "previs", "generation_review", "finish_delivery"])
        self.assertEqual(sum(item["is_current"] for item in steps), 1)
        self.assertEqual(next(item["id"] for item in steps if item["is_current"]),
                         "previs")
        self.assertEqual(len(registry["milestones"]), 4)

    def test_canvas_builds_position_registry_before_rendering_edges(self):
        snapshot = {
            "title": "Canvas registry regression",
            "current_stage": "brief",
            "next_action": "approve_brief",
            "customer_message": "请确认 brief",
            "steps": [{
                "id": "plan", "title": "策划", "status": "current",
                "detail": "brief", "is_current": True,
            }],
            "assets": [{
                "tag": "product", "status": "confirmed", "path": "",
            }],
            "board_feedback": [{
                "kind": "product_board", "status": "pending", "path": "",
            }],
            "storyboard_shots": [{
                "shot_id": "s1", "status": "pending", "path": "",
                "ref_tags": ["product"],
            }],
            "references": [],
            "feedback_loops": [],
            "events": [],
            "graph": {
                "nodes": [
                    {"id": "segment:s1", "kind": "segment",
                     "label": "s1", "state": "pending", "detail": ""},
                    {"id": "output:assembled", "kind": "output",
                     "label": "assembled", "state": "pending", "detail": ""},
                ],
                "edges": [{"from": "segment:s1", "to": "output:assembled"}],
            },
        }
        context = workflow_canvas._workflow_canvas_render_context(snapshot)
        node_context = workflow_canvas._workflow_canvas_nodes(context)
        self.assertEqual(set(node_context["positioned_nodes"]), {
            "stage:brief", "asset:product", "board:product_board", "shot:s1",
            "segment:s1", "output:assembled",
        })
        rendered = workflow_canvas.render_html(snapshot)
        self.assertIn("Canvas registry regression", rendered)
        self.assertIn("canvas-node", rendered)

    def test_experience_packet_v2_is_schema_valid(self):
        route = {
            "protocol_version": "1",
            "current_stage": "storyboard",
            "next_action": "review_storyboard",
            "customer_message": "请查看故事板",
            "customer_preview": [],
            "chain_profile": {"profile": "generic_video"},
            "required_host_capabilities": [],
            "interaction_contract": {},
            "flow": {},
        }
        manifest = {
            "client": "acme", "run_id": "run-1",
            "chain_profile": {"profile": "generic_video"},
            "requires_product_board": False,
            "requires_cast_board": False,
            "requires_product_usage": False,
            "requires_content_scaffold": False,
            "requires_motion_design": False,
            "requires_styleframe": False,
            "requires_audio": False,
            "requires_test_segment": False,
            "requires_shotcraft_packaging": False,
            "approvals": {}, "generation": {},
        }
        host = agent_runtime.host_capabilities(environ={})
        with mock.patch.object(workflow_router, "route_workflow", return_value=route), \
                mock.patch("run_manifest.approval_is_current", return_value=False):
            packet = stage_packet.build_experience_packet(
                manifest=manifest, host_capability_record=host)
        self.assertEqual(packet["schema_version"], 2)
        self.assertEqual(packet["milestone"]["id"], "previs")
        self.assertEqual(schema_validate.validate(
            packet, "experience-packet"), [])

    def test_artifact_fallback_never_emits_non_contract_stage_names(self):
        video_route = workflow_router._artifact_route(
            {}, {}, [{"id": "s1", "out_path": __file__}], [])
        self.assertEqual(video_route["current_stage"], "video")
        board_route = workflow_router._artifact_route(
            {"images": [{"path": __file__}]}, {}, [], [])
        self.assertEqual(board_route["current_stage"], "product_board")


if __name__ == "__main__":
    unittest.main()
