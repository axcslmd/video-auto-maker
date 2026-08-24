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

import agent_runtime
import interaction_contract
import model_behavior
import schema_validate
import storyboard_plan_compiler
import text_provider
import workflow_router
import llm_workflow_eval
import run_manifest
import motion_design
import workflow_mcp_server
import workflow_api
import stage_packet
import runtime_audit


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LlmAgnosticWorkflowTests(unittest.TestCase):
    def test_unknown_host_and_model_fail_to_conservative_profiles(self):
        host = agent_runtime.host_capabilities(environ={})
        self.assertEqual(host["host_id"], "unknown")
        self.assertEqual(schema_validate.validate(host, "host-capabilities"), [])
        profile = model_behavior.conservative_profile("private/model")
        policy = model_behavior.interaction_policy(profile)
        self.assertEqual(policy["mode"], "atomic")
        self.assertEqual(policy["max_items_per_turn"], 1)

    def test_model_earns_composite_mode_only_from_observed_success(self):
        profile = model_behavior.profile_from_catalog({
            "id": "provider/model", "capabilities": {
                "structured_output": True, "context_window": 128000,
            }})
        self.assertNotEqual(model_behavior.interaction_policy(profile)["mode"],
                            "composite")
        for _ in range(10):
            profile = model_behavior.observe(profile, {
                "tool_call_ok": True, "first_pass_schema_ok": True,
                "semantic_audit_ok": True, "repair_ok": True,
            })
        self.assertEqual(model_behavior.interaction_policy(profile)["mode"],
                         "composite")

    def test_router_contract_exposes_bounded_interaction_and_plan_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "storyboard_plan.json")
            Path(plan).write_text("{}", encoding="utf-8")
            contract = workflow_router.stage_contract({
                "manifest": {"storyboard_plan": {"path": plan, "sha256": sha256(plan)}}
            }, "script")
            self.assertIn(os.path.abspath(plan), contract["preview_sources"])
            interaction = contract["interaction_contract"]
            self.assertEqual(interaction["interaction_policy"]["mode"], "atomic")
            self.assertIn("/approvals", interaction["protected_paths"])
            self.assertEqual(schema_validate.validate(
                interaction, "interaction-contract"), [])

    def test_patch_is_atomic_evidence_bound_and_cannot_touch_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "creative.json")
            Path(target).write_text(json.dumps({"primary_usp": "old"}), encoding="utf-8")
            contract = interaction_contract.build_contract("brief")
            blocked = {
                "schema_version": 1, "question_id": contract["question_id"],
                "base_sha256": sha256(target),
                "patches": [{"path": "/approvals/script", "value": True}],
                "assistant_rationale": "", "unresolved": [],
            }
            errors = interaction_contract.validate_proposal(contract, blocked, target)
            self.assertTrue(any("PATCH_PROTECTED" in item for item in errors))
            proposal = {
                "schema_version": 1, "question_id": contract["question_id"],
                "base_sha256": sha256(target),
                "patches": [{"path": "/primary_usp", "value": "new",
                             "evidence": {"source": "customer_quote", "quote": "重点讲 new"}}],
                "assistant_rationale": "按客户原话", "unresolved": [],
            }
            result = interaction_contract.apply_proposal(contract, proposal, target)
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(Path(target).read_text())["primary_usp"], "new")
            self.assertTrue(os.path.isfile(result["audit"]))
            with self.assertRaisesRegex(ValueError, "PATCH_BASE_STALE"):
                interaction_contract.apply_proposal(contract, proposal, target)

    def test_atomic_patch_cannot_hide_a_large_list_in_one_operation(self):
        contract = interaction_contract.build_contract("script")
        proposal = {
            "schema_version": 1, "question_id": contract["question_id"],
            "patches": [{"path": "/beats", "value": [{"id": 1}, {"id": 2}]}],
            "assistant_rationale": "", "unresolved": [],
        }
        errors = interaction_contract.validate_proposal(contract, proposal)
        self.assertTrue(any("PATCH_BATCH_TOO_LARGE" in item for item in errors))

    def test_technical_and_customer_confirmation_actions_are_distinct(self):
        technical = workflow_router.stage_contract({}, "motion_design")
        self.assertIn("execute_confirm_function", technical["allowed_actions"])
        self.assertNotIn("workflow_confirm", technical["allowed_actions"])
        customer = workflow_router.stage_contract({}, "derive")
        self.assertIn("workflow_confirm", customer["allowed_actions"])
        self.assertIn("derive", run_manifest.CUSTOMER_EVIDENCE_STAGES)

    def _ir(self, profile="document_explainer"):
        facts = {"schema_version": 1, "client": "acme", "run_id": "run1",
                 "facts": [{"id": "product.name", "value": "Widget", "source": "customer"}],
                 "forbidden_inferences": ["不得虚构销量"]}
        brief = {"schema_version": 1, "client": "acme", "run_id": "run1",
                 "profile": profile, "objective": "介绍 Widget", "audience": "采购者",
                 "channel": "官网", "primary_usp": "清晰易懂", "tone": "专业",
                 "cta": "预约演示", "fact_refs": ["product.name"], "output_ratio": "16:9"}
        beats = {"schema_version": 1, "client": "acme", "run_id": "run1", "beats": [
            {"beat_id": "beat_01", "purpose": "hook", "message": "认识 Widget",
             "talk": "先认识 Widget", "duration": 3, "fact_refs": ["product.name"]},
            {"beat_id": "beat_02", "purpose": "proof", "message": "展示依据",
             "talk": "再看依据", "duration": 3, "fact_refs": ["product.name"]},
        ]}
        return facts, brief, beats

    def test_compiler_preserves_customer_selected_allowed_package(self):
        facts, brief, beats = self._ir("product_with_human")
        brief["package"] = "brand-tvc"
        plan, validation = storyboard_plan_compiler.compile_plan(
            facts, brief, beats, draft=True)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(plan["package"], "brand-tvc")

    def test_formal_compiler_requires_creative_patches_and_emits_strict_plan(self):
        facts, brief, beats = self._ir()
        with self.assertRaisesRegex(ValueError, "FORMAL_SHOT_PATCHES_REQUIRED"):
            storyboard_plan_compiler.compile_plan(facts, brief, beats)
        proposals = []
        for index in range(2):
            proposals.append({
                "schema_version": 1, "question_id": "script.shots",
                "patches": [
                    {"path": "/shots/%d/scene_prompt" % index, "value": "无文字的真实场景 %d" % index},
                    {"path": "/shots/%d/character_action" % index, "value": "完成可见动作 %d" % index},
                    {"path": "/shots/%d/composition" % index, "value": "构图 %d" % index},
                    {"path": "/shots/%d/lighting" % index, "value": "商业柔光 %d" % index},
                ], "assistant_rationale": "", "unresolved": [],
            })
        plan, validation = storyboard_plan_compiler.compile_plan(
            facts, brief, beats, proposals)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(plan["contract_strictness"], "production")
        self.assertEqual([shot["panel_index"] for shot in plan["shots"]], [1, 2])
        self.assertEqual(schema_validate.validate(plan, "storyboard-plan"), [])

    def test_model_selection_uses_capabilities_not_brand_substrings(self):
        catalog = [
            {"id": "gpt-super-name-only", "capabilities": {}},
            {"id": "neutral-structured", "capabilities": {
                "structured_output": True, "context_window": 128000}},
        ]
        record, profile = text_provider.select_text_model(catalog, schema_depth=4)
        self.assertEqual(text_provider.model_id(record), "neutral-structured")
        self.assertEqual(profile["structured_output_mode"], "native_strict")
        wrapped, _ = text_provider.select_text_model({"data": catalog}, schema_depth=4)
        self.assertEqual(text_provider.model_id(wrapped), "neutral-structured")

    def test_structured_chat_repairs_invalid_result_once(self):
        catalog = [{"id": "neutral-structured", "capabilities": {
            "structured_output": True, "context_window": 128000}}]
        responses = [
            {"choices": [{"message": {"content": '{"value": 1}'}}]},
            {"choices": [{"message": {"content": '{"value": "ok"}'}}]},
        ]
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["value"], "properties": {"value": {"type": "string"}}}
        with mock.patch.object(text_provider.br_client, "chat_completion",
                               side_effect=responses):
            result, metadata = text_provider.chat_json(
                "test", [{"role": "user", "content": "return"}], schema,
                catalog=catalog, max_repairs=1)
        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(len(metadata["attempts"]), 2)
        self.assertFalse(metadata["attempts"][0]["valid"])
        self.assertTrue(metadata["attempts"][1]["valid"])

    def test_portable_strict_schema_and_cross_provider_tool_blocks(self):
        schema = {"type": "object", "properties": {
            "required_value": {"type": "string"},
            "optional_value": {"type": "integer"},
        }, "required": ["required_value"]}
        compiled = text_provider.portable_strict_schema(schema)
        self.assertFalse(compiled["additionalProperties"])
        self.assertEqual(set(compiled["required"]),
                         {"required_value", "optional_value"})
        self.assertIn("null", compiled["properties"]["optional_value"]["type"])
        self.assertFalse(text_provider.strict_schema_constrainable({
            "type": "object"}))
        anthropic_response = {"content": [{
            "type": "tool_use", "name": "submit_result",
            "input": {"value": "ok"}}]}
        self.assertEqual(text_provider.br_client.extract_tool_arguments(
            anthropic_response, "submit_result"), {"value": "ok"})

    def test_refusal_is_terminal_and_does_not_spend_fallback_calls(self):
        catalog = [{"id": "strict", "capabilities": {"structured_output": True}}]
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["value"], "properties": {"value": {"type": "string"}}}
        response = {"choices": [{"finish_reason": "stop", "message": {
            "content": "", "refusal": "not allowed"}}]}
        with mock.patch.object(text_provider.br_client, "chat_completion",
                               return_value=response) as call:
            with self.assertRaisesRegex(text_provider.StructuredOutputTerminalError,
                                        "REFUSED"):
                text_provider.chat_json(
                    "test", [{"role": "user", "content": "return"}], schema,
                    catalog=catalog, max_repairs=1)
        self.assertEqual(call.call_count, 1)

    def test_motion_creative_proposal_compiles_deterministic_execution(self):
        plan = {"storyboard_role": "design_board", "output_ratio": "16:9",
                "shots": [{"id": "s1", "duration": 4, "dialogue": "你好",
                           "visual": "clean scene", "motion_elements": []}]}
        creative = {
            "global_style": {"font_family": "brand", "primary_color": "#112233",
                             "subtitle_position": "lower_third", "motion_density": "low"},
            "shots": [{
                "shot_id": "s1",
                "subtitle": {"text": "你好", "position": "lower_third",
                             "safe_zone": "lower_third",
                             "typography": {"size_px": 48, "max_width_px": 900,
                                            "max_lines": 1, "preset": "fade_up",
                                            "emphasis": []}},
                "motion_overlay": {"style": "none", "safe_zone": "center",
                                   "title": "", "bullets": [], "metric": "",
                                   "preset": "none", "size_px": 40, "width_px": 420,
                                   "reason": "画面无需额外卡片"},
                "motion_plan": {"schema_version": 2, "personality": "calm",
                                "primary": "low", "secondary": "low", "ambient": "low",
                                "density": "low", "timing": "slow", "safe_zone": "lower_third",
                                "a11y": {"subtitle_required": True, "max_lines": 1,
                                         "contrast": "high"}},
                "motion_elements": [], "video_safe_zones": ["lower_third"],
                "director_note": "克制入场",
            }],
        }
        provider = {"model": "neutral", "mode": "native_strict", "attempts": [],
                    "strict_schema_compatible": True}
        with mock.patch.object(motion_design.key_setup, "ensure_session_id"), \
                mock.patch.object(motion_design, "_llm_design",
                                  return_value=(creative, provider)):
            result = motion_design.design_from_plan(
                plan, api_key="test", require_llm=True)
        execution = result["shots"][0]["execution_plan"]
        self.assertEqual(execution["render_order"],
                         ["content_stage", "presenter_plate", "foreground_graphics"])
        self.assertEqual(result["design_engine"]["execution_owner"],
                         "deterministic_motion_compiler")

    def test_business_eval_fixture_has_required_critical_coverage(self):
        payload = json.loads(Path(os.path.join(
            ROOT, "evals", "business_cases.json")).read_text(encoding="utf-8"))
        result = llm_workflow_eval.validate_fixtures(payload)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["case_count"], 36)
        self.assertIn("physical_geometry", result["categories"])
        self.assertIn("authorization", result["categories"])

    def test_mcp_lifecycle_negotiates_then_exposes_schema_tools(self):
        server = workflow_mcp_server.WorkflowMCPServer()
        blocked = server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(blocked["error"]["code"], -32002)
        initialized = server.handle_message({
            "jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            }})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(server.handle_message({
            "jsonrpc": "2.0", "method": "notifications/initialized"}), None)
        listed = server.handle_message({
            "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        tools = listed["result"]["tools"]
        self.assertEqual({item["name"] for item in tools}, {
            "workflow_status", "workflow_next", "workflow_next_v2",
            "workflow_impact_preview", "artifact_get", "task_status", "workflow_submit_patch",
            "workflow_refine", "workflow_review_present", "workflow_confirm",
            "workflow_confirm_v2",
        })
        for tool in tools:
            self.assertIn("inputSchema", tool)
            self.assertIn("outputSchema", tool)
            if tool["name"] in workflow_mcp_server.WRITE_TOOLS:
                self.assertIn("request_id", tool["inputSchema"]["required"])

    def test_mcp_rejects_invalid_arguments_before_business_dispatch(self):
        server = workflow_mcp_server.WorkflowMCPServer()
        server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            }})
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = server.handle_message({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "workflow_submit_patch",
                "arguments": {"manifest": "manifest.json", "target": "x.json",
                              "proposal": {}},
            }})
        self.assertEqual(response["error"]["code"], -32602)

    def test_mcp_write_receipt_is_idempotent_and_detects_key_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = os.path.join(tmp, "operations.json")
            calls = []
            args = {"request_id": "write-request-0001", "manifest": "m.json",
                    "target": "t.json", "proposal": {"value": 1}}
            with mock.patch.object(workflow_mcp_server, "_receipt_path",
                                   return_value=receipt):
                first = workflow_mcp_server._execute_idempotent(
                    "workflow_submit_patch", args, "m.json",
                    lambda: calls.append(1) or {"ok": True})
                replay = workflow_mcp_server._execute_idempotent(
                    "workflow_submit_patch", args, "m.json",
                    lambda: calls.append(2) or {"ok": True})
                self.assertTrue(first["ok"])
                self.assertTrue(replay["idempotent_replay"])
                self.assertEqual(calls, [1])
                changed = dict(args)
                changed["proposal"] = {"value": 2}
                with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_KEY_REUSED"):
                    workflow_mcp_server._execute_idempotent(
                        "workflow_submit_patch", changed, "m.json", lambda: {"ok": True})

    def test_mcp_redacts_secret_like_output_values(self):
        fake_secret = "sk-" + "abcdefghijklmnop"
        result = workflow_mcp_server._tool_result({
            "api_key": "secret", "message": "do not expose " + fake_secret,
            "path": "/private/outside-project.txt"})
        self.assertEqual(result["structuredContent"]["api_key"], "[REDACTED]")
        self.assertNotIn(fake_secret, result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["path"],
                         "[BLOCKED_OUTSIDE_PROJECT]")

    def test_thin_api_never_hashes_preview_outside_project(self):
        outside = "/private/outside-project.txt"
        self.assertTrue(stage_packet._artifact(outside)["blocked"])
        with mock.patch.object(workflow_api, "_route", return_value={
                "current_stage": "storyboard", "customer_preview": [outside]}), \
                mock.patch.object(workflow_api, "_record") as record:
            result = workflow_api.artifact_get({})
        self.assertEqual(result["blocked_outside_project"], 1)
        record.assert_not_called()

    def test_runtime_audit_is_structured_and_drops_unsafe_attributes(self):
        event = runtime_audit.build_event(
            "workflow.tool.completed", trace="request-123",
            attributes={"tool.name": "workflow_next", "bad key": "ignored",
                        "freeform": "x" * 129, "count": 1})
        self.assertEqual(schema_validate.validate(event, "runtime-audit-event"), [])
        self.assertEqual(event["severity_number"], 9)
        self.assertNotIn("bad key", event["attributes"])
        self.assertNotIn("freeform", event["attributes"])


if __name__ == "__main__":
    unittest.main()
