import json
import contextlib
import io
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

import key_setup
import artifact_contract
import pipeline
import run_manifest
import storyboard
import storyboard_validator
import video_engine
import video_segmentation
import workflow_contract
import workflow_router


class WorkflowContractRegressionTests(unittest.TestCase):
    def test_duration_partition_never_reintroduces_fixed_twelve_panel_padding(self):
        parts = video_segmentation.partition_shots([{
            "id": "long", "duration": 20,
            "dialogue": "第一句。第二句。第三句。第四句。",
            "panel_plan": ["a", "b", "c", "d"],
        }], max_seconds=10)
        self.assertEqual(len(parts), 2)
        self.assertEqual([len(part["panel_plan"]) for part in parts], [2, 2])
        self.assertTrue(all(len(part["panel_plan"]) != 12 for part in parts))
        self.assertTrue(all("voiceover" not in part for part in parts))

    def test_duration_partition_does_not_merge_more_than_twelve_panels(self):
        shots = [{"id": "a", "duration": 4,
                  "panel_plan": ["a%d" % i for i in range(8)]},
                 {"id": "b", "duration": 4,
                  "panel_plan": ["b%d" % i for i in range(8)]}]
        parts = video_segmentation.partition_shots(shots, max_seconds=15)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(len(part["panel_plan"]) <= 12 for part in parts))

    def test_demo_brand_logo_tag_has_no_client_specific_bypass(self):
        with self.assertRaisesRegex(Exception, "REFERENCE_REGISTRY_MISSING_TAGS"):
            storyboard._validate_reference_registry(
                {"shots": [{"id": "s1", "ref_tags": ["@momax_logo"]}]}, [])

    def test_video_handoff_binds_reference_tag_role_and_intent(self):
        segment = {
            "id": "s1", "text": "shot", "video_type": 5,
            "references": [{
                "id": "ref_01", "tag": "@host", "type": "character_identity",
                "scope": "scene", "label": "主持人", "role": "身份锚",
                "intent": "保持面部一致", "url": "https://example.invalid/host.jpg",
            }],
        }
        segment["video_handoff_fingerprint"] = artifact_contract.build_video_handoff(
            segment)["fingerprint"]
        changed = dict(segment, references=[dict(
            segment["references"][0], tag="@product", role="产品锚")])
        self.assertFalse(artifact_contract.verify_video_handoff(changed)["ok"])

    def test_video_handoff_binds_urls_and_prompt_source(self):
        segment = {
            "id": "s1", "text": "shot", "video_type": 5,
            "model": "seedance-2.0", "camera_movement": "slow push",
            "urls": ["https://example.invalid/one.jpg"],
        }
        segment["video_handoff_fingerprint"] = artifact_contract.build_video_handoff(
            segment)["fingerprint"]
        for changed in (
                dict(segment, urls=["https://example.invalid/two.jpg"]),
                dict(segment, camera_movement="locked off")):
            self.assertFalse(artifact_contract.verify_video_handoff(changed)["ok"])

    def test_video_handoff_v7_binds_provider_route_and_recovery_contract(self):
        segment = {
            "id": "s1", "text": "shot", "video_type": 5,
            "model": "seedance-2.0-exact-route",
            "urls": ["https://example.invalid/identity.jpg"],
            "recovery_contract": {
                "recovery_id": "recovery-1", "mode": "independent_identity_anchor",
                "target_model": "seedance-2.0-exact-route",
            },
        }
        handoff = artifact_contract.build_video_handoff(segment)
        self.assertEqual(handoff["payload"]["schema_version"], 7)
        segment["video_handoff_fingerprint"] = handoff["fingerprint"]
        changed_model = dict(segment, model="different-exact-route")
        changed_recovery = dict(
            segment, recovery_contract=dict(
                segment["recovery_contract"], recovery_id="recovery-2"))
        self.assertFalse(
            artifact_contract.verify_video_handoff(changed_model)["ok"])
        self.assertFalse(
            artifact_contract.verify_video_handoff(changed_recovery)["ok"])

    def test_all_business_profiles_are_inferred_from_authoritative_plan_facts(self):
        cases = {
            "oral_broadcast": {
                "storyboard_role": "video_storyboard", "characters": [{"id": "host"}],
            },
            "document_explainer": {
                "storyboard_role": "design_board", "source_pdf": "proposal.pdf",
            },
            "product_showcase": {
                "storyboard_role": "video_storyboard",
                "asset_refs": {"product_images": ["product.jpg"]},
            },
            "product_with_human": {
                "storyboard_role": "video_storyboard", "characters": [{"id": "host"}],
                "asset_refs": {"product_images": ["product.jpg"]},
            },
            "venue_service": {
                "storyboard_role": "video_storyboard", "characters": [{"id": "host"}],
                "asset_refs": {"scene_images": ["store.jpg"]},
            },
            "brand_tvc": {
                "storyboard_role": "video_storyboard", "scene_type": "brand-tvc",
            },
            "generic_video": {"storyboard_role": "video_storyboard"},
        }
        for expected, plan in cases.items():
            with self.subTest(expected=expected):
                contract = workflow_contract.plan_requirements(plan)
                self.assertEqual(contract["profile"], expected)
                order = workflow_contract.required_stage_order(
                    expected, contract["requirements"], include_boundaries=True)
                self.assertEqual(order[0], "idea")
                self.assertEqual(order[-1], "delivery")

    def test_every_profile_has_one_exact_required_stage_matrix(self):
        plans = {
            "oral_broadcast": {
                "chain_profile": "oral_broadcast", "characters": [{"id": "host"}],
            },
            "document_explainer": {
                "chain_profile": "document_explainer", "storyboard_role": "design_board",
                "source_pdf": "proposal.pdf", "characters": [{"id": "host"}],
            },
            "product_showcase": {
                "chain_profile": "product_showcase",
                "asset_refs": {"product_images": ["product.jpg"]},
            },
            "product_with_human": {
                "chain_profile": "product_with_human", "characters": [{"id": "host"}],
                "asset_refs": {"product_images": ["product.jpg"]},
            },
            "venue_service": {
                "chain_profile": "venue_service", "characters": [{"id": "host"}],
                "asset_refs": {"scene_images": ["store.jpg"]},
            },
            "brand_tvc": {"chain_profile": "brand_tvc"},
            "generic_video": {},
        }
        expected = {
            "oral_broadcast": (
                "brief", "script", "styleframe", "audio", "cast_board",
                "storyboard", "render_plan", "video", "captions", "final", "derive"),
            "document_explainer": (
                "brief", "script", "content_scaffold", "motion_design", "styleframe",
                "audio", "cast_board", "storyboard", "render_plan", "video",
                "captions", "final", "derive"),
            "product_showcase": (
                "brief", "script", "styleframe", "product_board", "storyboard",
                "render_plan", "video", "captions", "final", "derive"),
            "product_with_human": (
                "brief", "script", "styleframe", "audio", "product_board",
                "cast_board", "product_usage", "storyboard", "render_plan", "video",
                "captions", "final", "derive"),
            "venue_service": (
                "brief", "script", "styleframe", "audio", "cast_board",
                "storyboard", "render_plan", "video", "captions", "final", "derive"),
            "brand_tvc": (
                "brief", "script", "styleframe", "audio", "storyboard",
                "render_plan", "video", "captions", "final", "derive"),
            "generic_video": (
                "brief", "script", "storyboard", "render_plan", "video",
                "captions", "final", "derive"),
        }
        for profile, plan in plans.items():
            with self.subTest(profile=profile):
                contract = workflow_contract.plan_requirements(plan)
                self.assertEqual(contract["profile"], profile)
                self.assertEqual(
                    tuple(workflow_contract.required_stage_order(
                        profile, contract["requirements"])), expected[profile])
                self.assertEqual(
                    contract["expected_package"],
                    workflow_contract.PROFILE_PACKAGE_IDS[profile])

    def test_required_stage_dependencies_are_topological_for_every_profile(self):
        profile_plans = {
            "oral_broadcast": {"chain_profile": "oral_broadcast"},
            "document_explainer": {
                "chain_profile": "document_explainer", "storyboard_role": "design_board"},
            "product_showcase": {"chain_profile": "product_showcase"},
            "product_with_human": {"chain_profile": "product_with_human"},
            "venue_service": {"chain_profile": "venue_service"},
            "brand_tvc": {"chain_profile": "brand_tvc"},
        }
        for profile, plan in profile_plans.items():
            with self.subTest(profile=profile):
                contract = workflow_contract.plan_requirements(plan)
                manifest = {
                    "chain_profile": {"profile": profile, "frozen": True},
                    **contract["requirements"],
                }
                order = run_manifest.required_stage_order(manifest)
                positions = {stage: index for index, stage in enumerate(order)}
                for stage in order:
                    for dependency in run_manifest.generation_dependencies(manifest, stage):
                        self.assertIn(dependency, positions)
                        self.assertLess(positions[dependency], positions[stage])

    def test_optional_motion_test_and_packaging_extensions_work_for_every_profile(self):
        for profile in workflow_contract.PROFILE_STAGE_ORDERS:
            if profile == "generic_video":
                plan = {}
            else:
                plan = {"chain_profile": profile}
            plan.update({
                "motion_elements": ["deterministic overlay"],
                "requires_test_segment": True,
                "requires_shotcraft_packaging": True,
            })
            with self.subTest(profile=profile):
                contract = workflow_contract.plan_requirements(plan)
                order = workflow_contract.required_stage_order(
                    contract["profile"], contract["requirements"])
                self.assertIn("motion_design", order)
                self.assertIn("test_segment", order)
                self.assertIn("shotcraft_packaging", order)
                self.assertLess(order.index("script"), order.index("motion_design"))
                self.assertLess(order.index("render_plan"), order.index("test_segment"))
                self.assertLess(order.index("video"), order.index("shotcraft_packaging"))
                manifest = {
                    "chain_profile": {"profile": contract["profile"], "frozen": True},
                    **contract["requirements"],
                }
                motion_dependencies = run_manifest.generation_dependencies(
                    manifest, "motion_design")
                self.assertIn("script", motion_dependencies)
                if not contract["requirements"]["requires_content_scaffold"]:
                    self.assertNotIn("content_scaffold", motion_dependencies)

    def test_multi_segment_audio_plan_requires_one_reusable_canary_take(self):
        multi = workflow_contract.plan_requirements({
            "chain_profile": "document_explainer",
            "storyboard_role": "design_board",
            "source_pdf": "proposal.pdf",
            "shots": [
                {"id": "s1", "dialogue": "第一段"},
                {"id": "s2", "dialogue": "第二段"},
            ],
        })
        single = workflow_contract.plan_requirements({
            "chain_profile": "document_explainer",
            "storyboard_role": "design_board",
            "source_pdf": "proposal.pdf",
            "shots": [{"id": "s1", "dialogue": "唯一一段"}],
        })
        self.assertTrue(multi["requirements"]["requires_audio"])
        self.assertTrue(multi["requirements"]["requires_test_segment"])
        self.assertFalse(single["requirements"]["requires_test_segment"])
        order = workflow_contract.required_stage_order(
            multi["profile"], multi["requirements"])
        self.assertLess(order.index("test_segment"), order.index("video"))

    def test_product_only_and_product_human_usage_decisions_cannot_split(self):
        product_only = {
            "storyboard_role": "video_storyboard", "characters": [],
            "asset_refs": {"product_images": ["product.jpg"]},
            "shots": [{"id": "s1", "ref_tags": ["@product"]}],
        }
        with_human = {
            **product_only,
            "characters": [{"id": "host"}],
            "asset_refs": {
                "product_images": ["product.jpg"],
                "digital_human_portraits": ["host.jpg"],
            },
        }
        for plan, expected in ((product_only, False), (with_human, True)):
            requirements = run_manifest.plan_requirements_from_plan(plan)
            self.assertEqual(requirements["requires_product_usage"], expected)
            self.assertEqual(storyboard.needs_product_usage_image(plan), expected)
            if not expected:
                self.assertFalse(requirements["requires_cast_board"])

    def test_design_board_role_alone_selects_document_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp, "plan.json")
            plan_path.write_text(json.dumps({
                "storyboard_role": "design_board", "shots": [],
            }), encoding="utf-8")
            manifest = {"storyboard_plan": run_manifest.file_record(str(plan_path))}
            inferred = workflow_router._infer_chain_profile(
                manifest=manifest, brief={}, storyboard_result={}, segments=[])
            self.assertEqual(inferred["profile"], "document_explainer")

    def test_design_board_document_with_presenter_and_product_stays_document(self):
        plan = {
            "storyboard_role": "design_board", "source_pdf": "proposal.pdf",
            "characters": [{"id": "host"}],
            "asset_refs": {"product_images": ["screenshot.png"]},
        }
        contract = workflow_contract.plan_requirements(plan)
        self.assertEqual(contract["profile"], "document_explainer")
        self.assertTrue(contract["requirements"]["requires_cast_board"])
        self.assertFalse(contract["requirements"]["requires_product_board"])
        self.assertFalse(contract["requirements"]["requires_product_usage"])

    def test_dialogue_words_cannot_reclassify_a_product_plan(self):
        plan = {
            "storyboard_role": "video_storyboard",
            "asset_refs": {"product_images": ["product.jpg"]},
            "shots": [{
                "id": "s1", "ref_tags": ["@product"],
                "dialogue": "打开 PDF，我来讲解这套方案。",
                "visual": "产品特写",
            }],
        }
        self.assertEqual(
            workflow_contract.infer_profile(plan)["profile"],
            "product_showcase")

    def test_explicit_entry_commands_select_their_business_chain_before_assets_exist(self):
        cases = {
            "oral_broadcast": {"command": "/oral-broadcast"},
            "venue_service": {"command": "/oral-scene-service"},
            "document_explainer": {"command": "/explainer-video"},
            "product_showcase": {"command": "/product-showcase"},
            "brand_tvc": {"command": "/brand-tvc"},
        }
        for expected, plan in cases.items():
            with self.subTest(expected=expected):
                contract = workflow_contract.plan_requirements(plan)
                self.assertEqual(contract["profile"], expected)
        product = workflow_contract.plan_requirements({"command": "/product-showcase"})
        self.assertTrue(product["requirements"]["requires_product_board"])
        oral = workflow_contract.plan_requirements({"command": "/oral-broadcast"})
        self.assertTrue(oral["requirements"]["requires_cast_board"])

    def test_production_package_must_match_the_inferred_profile(self):
        validation = storyboard_validator.validate_plan({
            "contract_strictness": "production",
            "command": "/oral-broadcast",
            "package": "brand-tvc",
            "storyboard_role": "video_storyboard",
            "storyboard_aspect_ratio": "16:9", "output_ratio": "9:16",
            "shots": [],
        })
        self.assertTrue(any(
            "PACKAGE_PROFILE_MISMATCH" in error
            for error in validation["errors"]))

    def test_formal_manifest_binding_requires_explicit_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp, "storyboard_plan.json")
            plan_path.write_text(json.dumps({
                "client": "acme", "run_id": "package-bind-run",
                "chain_profile": "oral_broadcast",
                "shots": [{"id": "s1", "duration": 3,
                           "panel_plan": ["a", "b", "c", "d"]}],
            }), encoding="utf-8")
            manifest = run_manifest.create_manifest("acme", "package-bind-run")
            with self.assertRaisesRegex(ValueError, "PACKAGE_SELECTION_REQUIRED"):
                run_manifest.bind_storyboard_plan(manifest, str(plan_path))

    def test_legacy_bound_plan_without_package_blocks_generation_and_routes_to_rebind(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp, "storyboard_plan.json")
            plan_path.write_text(json.dumps({
                "client": "acme", "run_id": "legacy-package-run",
                "chain_profile": "oral_broadcast",
                "shots": [{"id": "s1", "duration": 3,
                           "panel_plan": ["a", "b", "c", "d"]}],
            }), encoding="utf-8")
            manifest = run_manifest.create_manifest("acme", "legacy-package-run")
            manifest["storyboard_plan"] = run_manifest.file_record(str(plan_path))
            run_manifest.ensure_workflow_contract(manifest)
            self.assertIn("PACKAGE_SELECTION_REQUIRED",
                          manifest["workflow_contract_error"])
            with self.assertRaisesRegex(ValueError, "WORKFLOW_CONTRACT_BLOCKED"):
                run_manifest.generation_gate(manifest, "brief")
            route = workflow_router.route_workflow(manifest=manifest)
            self.assertEqual(route["next_action"], "bind_storyboard_plan")

    def test_router_never_bypasses_the_three_sellable_entry_commands(self):
        allowed = {"/oral-broadcast", "/explainer-video", "/brand-tvc",
                   "/video-auto-maker"}
        self.assertTrue(all(
            profile["entry_command"] in allowed
            for profile in workflow_router._CHAIN_PROFILES.values()))
        variants = [
            workflow_router._infer_route_variant(
                brief={"kind": keyword}, chain_profile={"profile": profile})
            for keyword, profile in (
                ("quick-promo", "brand_tvc"),
                ("product-demo", "product_with_human"),
                ("oral-scene-service", "venue_service"),
            )
        ]
        self.assertTrue(all(item["entry_command"] in allowed for item in variants))

    def test_legacy_manifest_with_current_plan_migrates_to_current_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp, "plan.json")
            plan_path.write_text(json.dumps({
                "package": "brand-tvc",
                "storyboard_role": "video_storyboard",
                "asset_refs": {"product_images": ["product.jpg"]},
            }), encoding="utf-8")
            manifest_path = Path(tmp, "manifest.json")
            manifest_path.write_text(json.dumps({
                "revision": 7,
                "storyboard_plan": run_manifest.file_record(str(plan_path)),
                "requires_cast_board": True,
                "chain_profile": {
                    "profile": "oral_broadcast", "frozen": True,
                    "contract_version": "old",
                },
            }), encoding="utf-8")
            migrated = run_manifest.load_manifest(str(manifest_path))
        self.assertEqual(
            migrated["workflow_contract_version"],
            workflow_contract.CONTRACT_VERSION)
        self.assertEqual(migrated["chain_profile"]["profile"], "product_showcase")
        self.assertFalse(migrated["requires_cast_board"])
        self.assertTrue(migrated["requires_product_board"])
        self.assertTrue(migrated["workflow_contract_history"])

    def test_skipped_derive_is_a_terminal_stage_not_an_approval_request(self):
        manifest = {"generation": {"derive": {"status": "skipped"}}}
        with mock.patch.object(run_manifest, "approval_is_current", return_value=False), \
                mock.patch.object(run_manifest, "validate_derive_closure", return_value=True):
            self.assertTrue(run_manifest.stage_is_complete(manifest, "derive"))

    def test_generic_stage_packet_flow_contains_begin_generate_finish_transaction(self):
        manifest = {
            "chain_profile": {"profile": "generic_video", "frozen": True},
            "requires_styleframe": True,
        }
        flow = workflow_router._resolved_flow(
            "styleframe", {"profile": "generic_video"}, manifest)
        phases = [item.get("phase") for item in flow["entry_functions"]]
        self.assertEqual(phases[0], "begin")
        self.assertEqual(phases[-1], "finish")
        self.assertTrue(all(phase == "generate" for phase in phases[1:-1]))
        self.assertIn("start-stage", flow["entry_functions"][0]["cli"])
        self.assertIn("finish-stage", flow["entry_functions"][-1]["cli"])
        self.assertEqual(flow["transaction"]["mode"], "external_three_phase")

    def test_specialized_stage_packet_declares_transaction_ownership(self):
        manifest = {
            "workflow_contract_version": workflow_contract.CONTRACT_VERSION,
            "chain_profile": {"profile": "generic_video", "frozen": True},
        }
        for stage in ("storyboard", "test_segment", "video", "captions", "final"):
            with self.subTest(stage=stage):
                flow = workflow_router._resolved_flow(
                    stage, {"profile": "generic_video"}, manifest)
                self.assertEqual(flow["transaction"]["mode"], "specialized_atomic")
                self.assertTrue(flow["transaction"]["begin"])
                self.assertTrue(flow["transaction"]["finish"])

    def test_every_customer_decision_cli_requires_customer_confirmation(self):
        for stage in sorted(run_manifest.CUSTOMER_EVIDENCE_STAGES):
            with self.subTest(stage=stage):
                confirm = workflow_router._STAGE_FLOW[stage]["confirm_function"]
                self.assertIn("--customer-confirmation", confirm["cli"])

    def test_storyboard_cli_allows_adjacent_prompt_review_discovery(self):
        fake_result = {"index_md": "index.md", "result_json": "result.json"}
        with mock.patch.object(storyboard, "render_storyboard", return_value=fake_result) as render:
            code = storyboard.main([
                "--plan", "output/storyboard_plan.json", "--json",
            ])
        self.assertEqual(code, 0)
        self.assertIsNone(render.call_args.kwargs["prompt_review"])

    def test_storyboard_missing_panel_plan_has_no_hidden_twelve_panel_default(self):
        self.assertEqual(storyboard.normalize_panel_plan({}), [])
        shot_a = {"id": "s1", "panel_plan": ["a", "b", "c", "d"]}
        shot_b = {"id": "s1", "panel_plan": ["a", "b", "c", "different"]}
        self.assertNotEqual(
            storyboard.shot_fingerprint(shot_a),
            storyboard.shot_fingerprint(shot_b))

    def test_storyboard_paid_stage_lease_blocks_a_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "manifest.json"))
            manifest = run_manifest.create_manifest("acme", "lease-run")
            run_manifest.save_manifest(manifest, path, create_only=True)
            first = run_manifest.acquire_generation_lease(
                path, "brief", "owner-a", ttl_seconds=300)
            self.assertEqual(
                first["generation"]["brief"]["lease"]["owner_token"], "owner-a")
            with self.assertRaisesRegex(ValueError, "GENERATION_ALREADY_RUNNING"):
                run_manifest.acquire_generation_lease(
                    path, "brief", "owner-b", ttl_seconds=300)
            refreshed = run_manifest.heartbeat_generation_lease(
                path, "brief", "owner-a", ttl_seconds=300)
            self.assertEqual(
                refreshed["generation"]["brief"]["lease"]["owner_token"], "owner-a")

    def test_video_lease_heartbeat_refreshes_the_callers_manifest_snapshot(self):
        manifest = {"revision": 1, "generation": {"video": {"status": "running"}}}
        refreshed = {
            "revision": 2,
            "generation": {"video": {"status": "running", "lease": {
                "owner_token": "owner-a", "expires_at_epoch": 123,
            }}},
        }
        with mock.patch.object(
                video_engine._rm, "heartbeat_generation_lease",
                return_value=refreshed) as heartbeat:
            video_engine._heartbeat_formal_generation(
                manifest, "/tmp/manifest.json", "video", "owner-a")
        heartbeat.assert_called_once_with(
            "/tmp/manifest.json", "video", "owner-a", ttl_seconds=300)
        self.assertEqual(manifest, refreshed)

    def test_delivery_creation_records_terminal_manifest_state(self):
        manifest = {"client": "acme", "run_id": "delivery-run"}
        with mock.patch.object(pipeline.rm, "require_run_output", return_value="/tmp/delivery.json"), \
                mock.patch.object(pipeline.delivery_contract, "create_delivery", return_value={
                    "delivery_identity": "identity", "created_at": "2026-08-18T12:00:00",
                }), \
                mock.patch.object(pipeline.rm, "file_record", return_value={
                    "path": "/tmp/delivery.json", "sha256": "abc", "exists": True,
                }), \
                mock.patch.object(pipeline.rm, "save_manifest") as save:
            pipeline.create_delivery(manifest, "/tmp/manifest.json", "/tmp/delivery.json")
        self.assertEqual(manifest["status"], "delivered")
        self.assertEqual(manifest["delivery"]["status"], "verified")
        save.assert_called_once()

    def test_formal_global_ocr_waiver_is_rejected_before_generation(self):
        code = video_engine.main([
            "--text", "hello", "--allow-ocr-warning",
        ])
        self.assertEqual(code, 2)

    def test_formal_chained_library_rejects_global_ocr_waiver(self):
        with self.assertRaisesRegex(ValueError, "FORMAL_OCR_GLOBAL_WAIVER_BLOCKED"):
            video_engine.render_chained([], allow_ocr_warning=True, draft=False)

    def test_draft_library_cannot_mutate_formal_manifest(self):
        with self.assertRaisesRegex(ValueError, "DRAFT_MANIFEST_MUTATION_BLOCKED"):
            video_engine.render_batch(
                [], draft=True, manifest={}, manifest_path="manifest.json")

    def test_formal_library_cannot_disable_model_fallback(self):
        with self.assertRaisesRegex(ValueError, "FORMAL_MODEL_FALLBACK_REQUIRED"):
            video_engine.render_batch([], draft=False, allow_model_fallback=False)

    def test_new_video_attempt_reopens_video_and_downstream_approvals(self):
        manifest = {
            "handoffs": {"video": {"segments": {"s1": "handoff"}}},
            "accepted_takes": {"s1": {"take_fingerprint": "take-old"}},
            "approvals": {"video": True, "captions": True, "final": True},
            "approval_hashes": {
                "video": "v", "captions": "c", "final": "f",
            },
            "approval_evidence": {"video": {"customer_confirmation": "ok"}},
        }
        result = run_manifest.request_video_attempt(
            manifest, "s1", actor="customer", reason="rejected old take")
        self.assertEqual(result["attempt"], 2)
        self.assertFalse(manifest["approvals"]["video"])
        self.assertFalse(manifest["approvals"]["captions"])
        self.assertFalse(manifest["approvals"]["final"])
        self.assertNotIn("video", manifest["approval_evidence"])
        self.assertEqual(
            manifest["generation"]["video"]["status"], "authorized_retry")

    def _settled_test_segment_failure(self, tmp):
        results_path = Path(tmp, "failed-results.json")
        handoff = "h" * 64
        result = {
            "ok": False, "segment_id": "s1", "taskId": "task-3",
            "video_handoff_fingerprint": handoff,
            "error": "PRIVACY_REJECTED_STOPPED_NO_PAID_FALLBACK: rejected",
        }
        results_path.write_text(json.dumps([result]), encoding="utf-8")
        record = run_manifest.file_record(str(results_path))
        manifest = run_manifest.create_manifest("unit", "run")
        manifest.update({
            "handoffs": {"video": {"segments": {"s1": handoff}}},
            "video_attempts": {"s1": {"attempt": 3}},
            "tasks": [{
                "stage": "video", "unit_id": "s1", "task_id": "task-3",
                "attempt": 3, "model": "seedance-route", "status": "failed",
                "handoff_fingerprint": handoff,
            }],
            "generation": {"test_segment": {
                "status": "failed_pending_recovery", "results": record,
                "segment_id": "s1", "task_id": "task-3",
                "video_handoff_fingerprint": handoff,
            }},
            "approvals": {}, "approval_hashes": {},
        })
        return manifest, results_path, record

    def test_new_attempt_accepts_only_exact_settled_provider_failure_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, results_path, record = \
                self._settled_test_segment_failure(tmp)
            authorization = run_manifest.request_video_attempt(
                manifest, "s1", actor="customer-standing-authorization",
                reason="provider failure requires a new exact canary",
                failure_results=str(results_path))
            self.assertEqual(authorization["attempt"], 4)
            self.assertEqual(authorization["previous_task_id"], "task-3")
            self.assertEqual(authorization["failure_stage"], "test_segment")
            self.assertEqual(authorization["failure_results"], record)
            self.assertIsNone(authorization["previous_take_fingerprint"])
            self.assertEqual(manifest["status"], "needs_test_segment_recovery")
            reconciled = run_manifest.reconcile_authorized_test_segment_recovery(
                manifest, "s1")
            self.assertEqual(reconciled["attempt"], 4)
            self.assertEqual(reconciled["previous_task_id"], "task-3")
            results_path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "TEST_SEGMENT_RECOVERY_AUTHORIZATION_STALE"):
                run_manifest.reconcile_authorized_test_segment_recovery(
                    manifest, "s1")

    def test_new_attempt_rejects_unbound_inline_rejected_review(self):
        manifest = {
            "handoffs": {"video": {"segments": {"s1": "h" * 64}}},
            "approvals": {}, "approval_hashes": {},
        }
        fabricated = {
            "segment_id": "s1",
            "artifact": {"video_handoff_fingerprint": "h" * 64},
            "decision": {"verdict": "rejected", "acceptance_mode": "formal"},
        }
        with self.assertRaisesRegex(
                ValueError, "VIDEO_ATTEMPT_REVIEW_ARTIFACT_REQUIRED"):
            run_manifest.request_video_attempt(
                manifest, "s1", actor="agent", reason="fabricated",
                review=fabricated)

    def test_new_attempt_binds_current_persisted_rejected_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp, "take.mp4")
            video_path.write_bytes(b"rejected-take-bytes")
            handoff = "h" * 64
            take = {
                "segment_id": "s1", "taskId": "task-1",
                "localPath": str(video_path),
                "video_handoff_fingerprint": handoff,
            }
            fingerprint = run_manifest.take_review.take_fingerprint(take)
            review = {
                "segment_id": "s1",
                "artifact": {
                    "task_id": "task-1", "video_url": None,
                    "local_path": str(video_path),
                    "sha256": artifact_contract.file_sha256(str(video_path)),
                    "take_fingerprint": fingerprint,
                    "video_handoff_fingerprint": handoff,
                },
                "decision": {
                    "verdict": "rejected", "acceptance_mode": "formal"},
            }
            review_path = Path(tmp, "review.json")
            review_path.write_text(json.dumps(review), encoding="utf-8")
            manifest = {
                "handoffs": {"video": {"segments": {"s1": handoff}}},
                "tasks": [{"stage": "video", "unit_id": "s1",
                           "attempt": 1}],
                "approvals": {}, "approval_hashes": {},
            }
            authorization = run_manifest.request_video_attempt(
                manifest, "s1", actor="customer", reason="reject take",
                review=review, review_path=str(review_path))
            self.assertEqual(authorization["attempt"], 2)
            self.assertEqual(authorization["previous_take_fingerprint"],
                             fingerprint)
            self.assertEqual(authorization["rejected_review"],
                             run_manifest.file_record(str(review_path)))

    def test_recovery_mutation_cli_outputs_are_compact_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, results_path, _ = self._settled_test_segment_failure(tmp)
            manifest["huge_internal"] = "x" * 200000
            manifest_path = Path(tmp, "manifest.json")
            run_manifest.save_manifest(
                manifest, str(manifest_path), create_only=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_manifest.main([
                    "new-video-attempt", "--manifest", str(manifest_path),
                    "--segment-id", "s1", "--actor", "customer",
                    "--reason", "new exact canary",
                    "--failure-results", str(results_path),
                ])
            self.assertEqual(code, 0, output.getvalue())
            self.assertLess(len(output.getvalue()), 2000)
            self.assertNotIn("huge_internal", output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_manifest.main([
                    "reconcile-test-segment-recovery-authorization",
                    "--manifest", str(manifest_path),
                    "--segment-id", "s1",
                ])
            self.assertEqual(code, 0, output.getvalue())
            self.assertLess(len(output.getvalue()), 2000)
            self.assertNotIn("huge_internal", output.getvalue())

    def test_formal_single_text_entry_is_disabled_in_favor_of_one_item_batch(self):
        code = video_engine.main(["--text", "hello"])
        self.assertEqual(code, 2)

    def test_test_segment_has_finish_review_and_customer_approval_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results_path = root / "test-results.json"
            video_path = root / "test.mp4"
            review_path = root / "review.json"
            results_path.write_text("[]", encoding="utf-8")
            video_path.write_bytes(b"video")
            review_path.write_text(json.dumps({"segment_id": "s1"}), encoding="utf-8")
            manifest = run_manifest.create_manifest("acme", "test-segment-run")
            manifest["requires_test_segment"] = True
            manifest["handoffs"] = {"video": {"segments": {"s1": "handoff"}}}
            result = {
                "ok": True, "segment_id": "s1", "localPath": str(video_path),
                "take_fingerprint": "take", "video_handoff_fingerprint": "handoff",
                "media_qc": {"passed": True},
                "ocr_status": "clear", "ocr_available": True,
                "ocr_frames_checked": 5, "ocr_expected": 5,
                "ocr_warning": False, "ocr_texts": [],
            }
            with mock.patch.object(run_manifest, "generation_dependencies", return_value=()), \
                    mock.patch.object(run_manifest, "run_output_root", return_value=str(root)), \
                    mock.patch.object(run_manifest.take_review, "is_accepted", return_value=True):
                run_manifest.record_test_segment_finished(
                    manifest, str(results_path), [result])
                self.assertTrue(run_manifest.ocr_take_is_clear_or_waived(
                    manifest, "s1", "take"))
                run_manifest.approve_test_segment(
                    manifest, str(review_path), "客户确认测试段")
                self.assertTrue(run_manifest.approval_is_current(
                    manifest, "test_segment"))
                review_path.write_text(json.dumps({"segment_id": "changed"}), encoding="utf-8")
                self.assertFalse(run_manifest.approval_is_current(
                    manifest, "test_segment"))

    def test_empirical_audio_canary_requires_non_silent_media_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp, "canary.mp4")
            results_path = Path(tmp, "results.json")
            video_path.write_bytes(b"video")
            results_path.write_text("[]", encoding="utf-8")
            manifest = run_manifest.create_manifest("acme", "audio-canary")
            manifest.update({
                "requires_test_segment": True, "requires_audio": True,
                "handoffs": {"video": {"segments": {"s1": "handoff"}}},
            })
            result = {
                "ok": True, "segment_id": "s1", "localPath": str(video_path),
                "take_fingerprint": "take", "model": "ep-route",
                "video_handoff_fingerprint": "handoff",
                "audio_capability_policy":
                    "empirical_canary_unknown_output_audio",
                "audio_capability_policy_contract": {
                    "policy": "empirical_canary_unknown_output_audio",
                    "scope": "formal_test_segment_only", "segment_id": "s1"},
                "media_qc": {
                    "passed": True,
                    "media": {"audio": {"stream_count": 1}},
                    "checks": {
                        "audio_track": {"passed": True},
                        "silence": {"passed": False},
                    },
                },
            }
            with self.assertRaisesRegex(
                    ValueError, "TEST_SEGMENT_EMBEDDED_AUDIO_PROOF_REQUIRED"):
                run_manifest.record_test_segment_finished(
                    manifest, str(results_path), [result])

    def test_key_cli_rejects_positional_secret(self):
        with mock.patch.dict(os.environ, {key_setup.SESSION_ENV: "session-test"}, clear=True):
            code = key_setup.main(["save", "sk-" + "secret-on-command-line"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
