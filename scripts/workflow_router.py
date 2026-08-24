#!/usr/bin/env python3
"""Central workflow router shared by CLI, canvas, and host agents."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import asset_graph  # noqa: E402
import delivery_contract  # noqa: E402
import interaction_contract as ic  # noqa: E402
import model_behavior  # noqa: E402
import run_manifest as rm  # noqa: E402
import workflow_contract  # noqa: E402


ROUTER_CONTRACT_VERSION = workflow_contract.CONTRACT_VERSION


def storyboard_result_approval_is_current(storyboard_result):
    """Fail closed when routing without a manifest; status text is not consent."""
    if not isinstance(storyboard_result, dict):
        return False
    out_dir = storyboard_result.get("out_dir")
    result_path = storyboard_result.get("result_json")
    if not result_path and out_dir:
        result_path = os.path.join(out_dir, "storyboard_result.json")
    if not result_path or not os.path.isfile(result_path):
        return False
    try:
        import storyboard
        return storyboard.storyboard_approval_is_current(
            result_path,
            client=storyboard_result.get("client"),
            run_id=storyboard_result.get("run_id"),
            out_dir=out_dir)
    except (OSError, ValueError, TypeError):
        return False


_STAGE_FLOW = {
    "idea": {
        "entry_functions": [
            {"module": "agent_entry", "function": "main", "cli": "python3 scripts/agent_entry.py prepare"},
            {"module": "customer_flow", "function": "main", "cli": "python3 scripts/customer_flow.py status --manifest <manifest.json>"},
            {"module": "workflow_router", "function": "route_workflow", "cli": "python3 scripts/workflow_router.py status --manifest <manifest.json>"},
        ],
        "actions": ["collect brief", "detect client/run", "route scene"],
        "required_inputs": ["user idea or existing run artifacts"],
        "expected_outputs": ["brief_path", "manifest"],
        "confirm_function": None,
        "recovery": ["If no run is discovered, ask for client slug once and create a manifest."],
        "next": "brief",
    },
    "brief": {
        "entry_functions": [
            {"module": "asset_prep", "function": "assess_assets", "cli": "python3 scripts/asset_prep.py assess --client <client>"},
            {"module": "asset_prep", "function": "ingest_image", "cli": "python3 scripts/asset_prep.py ingest-image --client <client> --file <path> --tag <tag>"},
            {"module": "asset_prep", "function": "set_render_plan", "cli": "python3 scripts/asset_prep.py set-render-plan --client <client> --file <render_plan.json>"},
            {"module": "approval_preview", "function": "render_brief_preview", "cli": "由 workflow_api review-present 自动生成并绑定当前 brief 的客户预览"},
        ],
        "required_inputs": ["client", "user materials or user idea"],
        "expected_outputs": ["assets/<client>/brief.json + source-bound brief_preview.md", "storyboard_plan.json", "render_plan"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage brief --customer-confirmation '<客户确认原话>'"},
        "recovery": ["If product images are pending, show them to the customer and confirm or refine before downstream use.", "If brief bytes change, review-present regenerates the deterministic preview and binds both source and preview hashes before confirmation."],
        "next": "script",
    },
    "script": {
        "entry_functions": [
            {"module": "guide_scaffold", "function": "scaffold", "cli": "python3 scripts/guide_scaffold.py scaffold --client <client> --kind <document|product|venue> --segments <n> --brand <hex> --out <guide.json>"},
            {"module": "guide_scaffold", "function": "compile_shots", "cli": "python3 scripts/guide_scaffold.py compile-shots --file <guide.json> --out <shotlist.json>"},
            {"module": "guide_scaffold", "function": "compile_segments", "cli": "python3 scripts/guide_scaffold.py compile-segments --file <guide.json> --urls-map <map.json> --out <segments.json>"},
        ],
        "required_inputs": ["approved brief", "customer-approved script direction"],
        "expected_outputs": ["storyboard_plan.json", "shotlist.json", "segments.json"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage script --customer-confirmation '<客户确认原话>'"},
        "recovery": ["If script/shot count is vague, return to co-creation instead of generating media."],
        "next": "styleframe",
    },
    "content_scaffold": {
        "entry_functions": [
            {"module": "content_scaffold", "function": "scaffold", "cli": "python3 scripts/content_scaffold.py scaffold --file <doc> --brand <hex> --out <content_spec.json>"},
            {"module": "content_scaffold", "function": "validate", "cli": "python3 scripts/content_scaffold.py validate --spec <content_spec.json> --json"},
        ],
        "required_inputs": ["document/PPT source", "brand primary color"],
        "expected_outputs": ["content scene spec"],
        "confirm_function": {"module": "run_manifest", "function": "approve", "cli": "python3 scripts/run_manifest.py approve --manifest <manifest.json> --stage content_scaffold"},
        "recovery": ["If placeholder text remains, do not render; fill the spec from source content first."],
        "next": "motion_design",
    },
    "motion_design": {
        "entry_functions": [
            {"module": "motion_design", "function": "design_from_plan", "cli": "python3 scripts/motion_design.py design --plan <storyboard_plan.json> --dialogue-source <approved_director_plan.json> --dialogue-binding-out <motion_dialogue_binding.json> --out <motion_design.json> --require-llm"},
            {"module": "motion_design", "function": "main.inject", "cli": "document_explainer: python3 scripts/motion_design.py inject --plan <storyboard_plan.json> --design <motion_design.json> --out output/<client>/<run-id>/storyboard_execution_plan.json --manifest <run_manifest.json>"},
            {"module": "motion_design", "function": "ensure_motion_plan_contract", "cli": "internal contract normalization"},
        ],
        "required_inputs": ["storyboard_plan.json", "approved dialogue or immutable dialogue binding", "brand/creative direction", "paired asset requirements"],
        "expected_outputs": ["motion_design.json with dialogue contract", "motion_dialogue_binding.json when authored plan omits dialogue", "document_explainer: storyboard_execution_plan.json (derived; authored storyboard_plan remains immutable)"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage motion_design --customer-confirmation '<客户确认动效方案原话>'"},
        "recovery": ["If LLM design fails, stop before render and ask for retry or explicit offline fallback; do not silently replace creative direction with renderer defaults."],
        "next": "audio",
    },
    "styleframe": {
        "entry_functions": [
            {"module": "styleframe", "function": "build_tokens", "cli": "python3 scripts/styleframe.py --brand <brand.json> --out <style_tokens.json> --preview-out <styleframe.png>"},
            {"module": "styleframe", "function": "render_preview", "cli": "internal remotion.Styleframe still render"},
        ],
        "required_inputs": ["approved script", "brand data"],
        "expected_outputs": ["style tokens", "same-renderer look frame PNG"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage styleframe --customer-confirmation '<客户确认风格帧原话>'"},
        "recovery": ["If brand primary color is missing, ask once and write it before generating style tokens.", "Do not approve token JSON alone; render and show the Remotion look frame."],
        "next": "audio",
    },
    "audio": {
        "entry_functions": [
            {"module": "audio_contract", "function": "prepare_stage", "cli": "python3 scripts/audio_contract.py prepare --manifest <manifest.json> --choice <complete-audio-choice.json> [--master-audio <customer-provided-audio>]"},
        ],
        "required_inputs": ["approved script", "complete voice mode/language/tone/BGM choice or music-only decision"],
        "expected_outputs": ["immutable audio direction contract", "customer-readable non-playable approval preview", "optional customer-provided master audio binding"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage audio --customer-confirmation '<客户确认音色/声音方案原话>'"},
        "recovery": ["Collect every required voice decision before prepare_stage; this stage has no standalone TTS or generated audition track.", "If voice mode conflicts with visible speech, fix the audio contract before video.", "The first generated voice audition is the audio-and-lip-sync test_segment."],
        "next": "product_board",
    },
    "product_board": {
        "entry_functions": [
            {"module": "storyboard", "function": "render_storyboard", "cli": "python3 scripts/storyboard.py --plan <storyboard_plan.json> --out-dir output/storyboard --run-id <run-id> --model gpt-image-2 --stage next --json"},
        ],
        "required_inputs": ["approved product image", "storyboard_plan.json"],
        "expected_outputs": ["product_board", "product_board confirmation"],
        "confirm_function": {"module": "storyboard", "function": "confirm_board", "cli": "python3 scripts/storyboard.py --confirm-board product --result-json <storyboard_result.json> --customer-confirmation '<客户确认原话>'"},
        "recovery": ["If source product image changes, invalidate stale product board before continuing."],
        "next": "cast_board",
    },
    "cast_board": {
        "entry_functions": [
            {"module": "storyboard", "function": "render_storyboard", "cli": "python3 scripts/storyboard.py --plan <storyboard_plan.json> --out-dir output/storyboard --run-id <run-id> --model gpt-image-2 --stage next --json"},
        ],
        "required_inputs": ["approved script", "character definition"],
        "expected_outputs": ["cast_board", "cast_board confirmation"],
        "confirm_function": {"module": "storyboard", "function": "confirm_board", "cli": "python3 scripts/storyboard.py --confirm-board cast --result-json <storyboard_result.json> --customer-confirmation '<客户确认原话>'"},
        "recovery": ["If face/full-body consistency fails, refine cast board before storyboard generation."],
        "next": "product_usage",
    },
    "product_usage": {
        "entry_functions": [
            {"module": "storyboard", "function": "generate_usage_panels", "cli": "internal per-panel generation for high-risk usage boards"},
            {"module": "storyboard", "function": "render_storyboard", "cli": "python3 scripts/storyboard.py --plan <storyboard_plan.json> --out-dir output/storyboard --run-id <run-id> --model gpt-image-2 --stage next --json"},
        ],
        "required_inputs": ["confirmed product_board", "confirmed cast_board", "physical relation contract"],
        "expected_outputs": ["product_usage_image", "usage_geometry_review.json"],
        "confirm_function": {"module": "storyboard", "function": "confirm_board", "cli": "python3 scripts/storyboard.py --confirm-board usage --result-json <storyboard_result.json> --geometry-review <usage_geometry_review.json> --customer-confirmation '<客户确认原话>'"},
        "recovery": ["If physical relationship is wrong, refine usage board and require customer confirmation again."],
        "next": "storyboard",
    },
    "storyboard": {
        "entry_functions": [
            {"module": "storyboard", "function": "render_storyboard", "cli": "python3 scripts/storyboard.py --plan <storyboard_plan.json; document_explainer uses storyboard_execution_plan.json> --out-dir output/storyboard --run-id <run-id> --model gpt-image-2 --stage next --json"},
            {"module": "explainer_layout_preview", "function": "render_execution_preview", "cli": "document_explainer only: python3 scripts/explainer_layout_preview.py --render-execution --plan <storyboard_execution_plan.json> --manifest <run_manifest.json> --quality high --out-dir output/<client>/<run-id>/layout_preview --json"},
            {"module": "explainer_layout_preview", "function": "submit_execution_visual_review", "cli": "document_explainer visual-QA closure: python3 scripts/explainer_layout_preview.py --submit-visual-review <agent_manual_review.json> --execution-result <execution_preview_result.json> --manifest <run_manifest.json> --json"},
            {"module": "explainer_layout_preview", "function": "revalidate_execution_visual_qa", "cli": "document_explainer pending-proof refresh: python3 scripts/explainer_layout_preview.py --revalidate-visual-qa --execution-result <execution_preview_result.json> --manifest <run_manifest.json> --json"},
        ],
        "required_inputs": ["approved script", "confirmed cast board", "confirmed required product boards"],
        "expected_outputs": ["storyboard_result.json", "storyboard_preview.html", "storyboard_index.md", "document_explainer: deterministic layout boards extracted from layout_animatic.mp4 + agent final-frame visual QA + commercial QC + frozen approved render contract"],
        "confirm_function": {"module": "storyboard", "function": "confirm_storyboard", "cli": "python3 scripts/storyboard.py --confirm --result-json <storyboard_result.json> --customer-confirmation '<客户确认原话>'"},
        "recovery": ["If storyboard shots are stale relative to plan, regenerate affected revision and re-confirm.", "For document_explainer, any shot change invalidates the shared Remotion timeline; re-render all layout boards and the Animatic together.", "Never submit document-explainer execution boards to the video model; regenerate only the clean avatar/scene plate."],
        "next": "render_plan",
    },
    "render_plan": {
        "entry_functions": [
            {"module": "explainer_render_plan", "function": "build", "cli": "document_explainer: python3 scripts/explainer_render_plan.py --manifest <manifest.json> --storyboard-result <storyboard_result.json> --execution-preview <execution_preview_result.json> --director-plan <approved_director_plan.json> --out <render_plan.json> --preview-out <render_plan_preview.md>"},
            {"module": "video_reverse", "function": "main", "cli": "python3 scripts/video_reverse.py reverse --basecut <basecut.mp4> --out-dir <out-dir>"},
            {"module": "motion_design", "function": "design_from_plan", "cli": "python3 scripts/motion_design.py design --plan <storyboard_plan.json> --out <motion_design.json>"},
            {"module": "visual_intent", "function": "compile_plan", "cli": "python3 scripts/visual_intent.py --plan <storyboard_plan.json> --strict --out <visual_intent.json>"},
            {"module": "layout_compiler", "function": "compile_layouts", "cli": "python3 scripts/layout_compiler.py --plan <storyboard_plan.json> --style-tokens <style_tokens.json> --visual-intent <visual_intent.json> --ratio <target-ratio> --out <layout_contract.json>"},
            {"module": "render_spec_compiler", "function": "compile_spec", "cli": "python3 scripts/render_spec_compiler.py --plan <storyboard_plan.json> --style-tokens <style_tokens.json> --motion-design <motion_design.json> --audio-contract <audio_contract.json> --engine-props <final_remotion_props.json> --formal --out <render_quality_spec.json>"},
            {"module": "explainer_commercial_qc", "function": "audit", "cli": "document_explainer: python3 scripts/explainer_commercial_qc.py --execution-preview <execution_preview_result.json> --stage approved --approval <storyboard_approval.json>"},
        ],
        "required_inputs": ["confirmed storyboard", "approved Design Token v3", "approved motion/audio contracts", "final renderer props for formal render"],
        "expected_outputs": ["render_plan.json + customer-readable render_plan_preview.md bound together", "visual_intent.json", "layout_contract.json", "immutable render_quality_spec.json", "document_explainer identity and exact-prompt pre-video sub-gates"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage render_plan --customer-confirmation '<客户确认渲染计划原话>'"},
        "recovery": ["If render_plan points to an old storyboard_result, re-register and approve render_plan.", "If formal RenderSpec reports unresolved capacity, target-ratio asset variants, audio timing, or renderer props, repair the upstream contract; do not shrink text or crop protected subjects automatically.", "For document_explainer, if the commercial QC contract is v1/missing, rerender and reconfirm the full same-renderer preview; do not grandfather the old board."],
        "next": "test_segment",
    },
    "identity_plate_reference": {
        "entry_functions": [],
        "required_inputs": ["approved render_plan JSON + customer Markdown", "confirmed cast board reference"],
        "expected_outputs": ["content-bound identity reference customer approval marker"],
        "confirm_function": {
            "module": "identity_plate_workflow",
            "function": "confirm_identity_plate_reference",
            "cli": "python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --confirm-reference --customer-confirmation '<客户明确确认身份图引用包原话>'",
        },
        "recovery": ["A changed render plan, source reference, catalog contract or request fingerprint invalidates this approval."],
        "next": "identity_plate_submission_capture",
    },
    "identity_plate_submission_capture": {
        "entry_functions": [{
            "module": "identity_plate_workflow",
            "function": "capture_identity_plate_submission",
            "cli": "python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --capture --out <identity_submission.json> --preview-out <identity_submission.md>",
        }],
        "required_inputs": ["current identity reference approval", "current live image model catalog"],
        "expected_outputs": ["exact local-only JSON request body + customer-readable Markdown preview"],
        "confirm_function": {
            "module": "identity_plate_workflow",
            "function": "confirm_identity_plate_submission",
            "cli": "python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --confirm-capture --customer-confirmation '<客户明确确认身份图提交包原话>'",
        },
        "recovery": ["Any payload, imageUrl, plan, source or live catalog change requires recapture and fresh confirmation."],
        "next": "identity_plate_generation",
    },
    "identity_plate_generation": {
        "entry_functions": [
            {
                "module": "identity_plate_workflow",
                "function": "submit_identity_plate",
                "cli": "python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --submit --packet <confirmed_identity_submission.json> --out <identity_plate.png>",
            },
            {
                "module": "identity_plate_workflow",
                "function": "recover_identity_plate_task",
                "cli": "python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --recover-task --out <identity_plate.png>",
            },
        ],
        "required_inputs": ["confirmed exact identity submission capture", "BasicRouter session key"],
        "expected_outputs": ["one downloaded identity candidate bound to task ID and actual request fingerprint", "deterministic decode/dimension/aspect QC", "immutable generation result JSON"],
        "confirm_function": None,
        "recovery": ["If a taskId exists, resume the same task without another create: python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --recover-task --out <identity_plate.png>.", "Only after the remote task is proven failed may a new paid attempt be authorized: python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --authorize-retry --customer-confirmation '<客户明确授权身份图重试原话>'."],
        "next": "identity_plate_review",
    },
    "identity_plate_review": {
        "entry_functions": [{
            "module": "identity_plate_workflow",
            "function": "record_identity_plate_visual_review",
            "cli": "python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --record-visual-review --out <identity_plate.visual-review.json> --reviewer-notes '<总体视觉检查结论>' --check single_person --observation 'single_person=<单人检查证据>' --check face_clear --observation 'face_clear=<人脸清晰检查证据>' --check cast_identity_consistent --observation 'cast_identity_consistent=<与引用人物板比对证据>' --check hair_outfit_accessories_consistent --observation 'hair_outfit_accessories_consistent=<发型服装配饰比对证据>' --check background_clean --observation 'background_clean=<背景检查证据>' --check no_ppt_text_logo_or_watermark --observation 'no_ppt_text_logo_or_watermark=<文字Logo水印检查证据>' --check avatar_plate_composition_usable --observation 'avatar_plate_composition_usable=<人物底片构图检查证据>'",
        }],
        "required_inputs": ["current generated identity candidate", "immutable generation result JSON", "passed deterministic media QC", "agent visual inspection of the rendered image"],
        "expected_outputs": ["content-bound visual review JSON", "customer-visible identity candidate preview"],
        "confirm_function": {
            "module": "identity_plate_workflow",
            "function": "confirm_generated_identity_plate",
            "cli": "python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --confirm-image --visual-review <identity_plate.visual-review.json> --customer-confirmation '<客户明确确认身份锚图原话>'",
        },
        "recovery": ["If any visual check fails, record the failed checklist and obtain explicit authorization before a new paid attempt.", "If the checklist passes but the customer rejects the visible image, record it with: python3 scripts/identity_plate_workflow.py --manifest <manifest.json> --reject-image --customer-confirmation '<客户明确拒绝身份图原话>'; then require separate retry authorization.", "A changed candidate, result JSON or deterministic QC invalidates the visual review and customer approval."],
        "next": "test_segment",
    },
    "test_segment": {
        "entry_functions": [
            {"module": "script_splitter", "function": "split", "cli": "python3 scripts/script_splitter.py split --plan <storyboard_plan.json> --storyboard-dir <confirmed_storyboard_dir> --client <client> --manifest <run_manifest.json> --out <segments.json>"},
            {"module": "video_engine", "function": "render_batch", "cli": "python3 scripts/video_engine.py --batch <segments.json> --formal-stage test_segment --test-segment-id <segment-id> --client <client> --manifest <run_manifest.json> --results-out <test_segment_results.json> --prompt-review <confirmed_video_prompt_review.json>"},
        ],
        "required_inputs": ["confirmed storyboard", "approved render_plan"],
        "expected_outputs": ["test segment video", "review report"],
        "confirm_function": {"module": "run_manifest", "function": "approve_test_segment", "cli": "python3 scripts/run_manifest.py confirm-test-segment --manifest <run_manifest.json> --review <accepted_review.json> --customer-confirmation '<客户确认原话>'"},
        "recovery": ["If test take is rejected, authorize a new attempt before paid retry."],
        "next": "video",
    },
    "video": {
        "entry_functions": [
            {"module": "video_engine", "function": "inspect_submission_requirements", "cli": "python3 scripts/video_engine.py --batch <segments.json> --model <preferred-model> --preflight --preflight-out <video_preflight.json>"},
            {"module": "panel_expansion", "function": "prepare", "cli": "仅 preflight 返回 requires_panel_expansion 时：python3 scripts/panel_expansion.py --segments <segments.json> --out <segments_with_panels.json> --out-dir <panel_dir>"},
            {"module": "panel_expansion", "function": "confirm", "cli": "展示 panel_expansion_preview.html 后：python3 scripts/panel_expansion.py --segments <segments_with_panels.json> --out <segments_confirmed_panels.json> --out-dir <panel_dir> --confirm --customer-confirmation '<客户确认单格展开图原话>'"},
            {"module": "prompt_review", "function": "capture_video_segments", "cli": "python3 scripts/prompt_review.py capture-video --plan <storyboard_plan.json> --segments <final_segments.json> --preflight-json <video_preflight.json> --out <video_prompt_review.json> --preview-out <video_prompt_review.md>"},
            {"module": "prompt_review", "function": "confirm", "cli": "展示完整逐模型提示词与素材包后：python3 scripts/prompt_review.py confirm --review <video_prompt_review.json> --manifest <run_manifest.json> --customer-confirmation '<客户确认原话>'"},
            {"module": "video_engine", "function": "render_batch", "cli": "python3 scripts/video_engine.py --batch <segments.json> --client <client> --manifest <run_manifest.json> --results-out <batch_results.json> --prompt-review <confirmed_video_prompt_review.json>"},
            {"module": "video_engine", "function": "render_chained", "cli": "python3 scripts/video_engine.py --batch <segments.json> --chain --client <client> --manifest <run_manifest.json> --results-out <batch_results.json> --prompt-review <confirmed_video_prompt_review.json>"},
            {"module": "run_manifest", "function": "record_video_generation_settlement", "cli": "worker 异常退出或旧 run 恢复时：python3 scripts/run_manifest.py settle-video-generation --manifest <run_manifest.json> --results <batch_results.json>"},
        ],
        "required_inputs": ["confirmed storyboard", "approved render_plan", "segments.json", "confirmed expanded panels when preflight requires Kling", "confirmed exact video prompt review"],
        "expected_outputs": ["basecut", "results", "reviews", "video_artifact"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage video --customer-confirmation '<客户确认底片原话>'"},
        "recovery": ["Every terminal worker path must settle batch_results: clear the lease, bind exact task/result evidence and route to take review or recovery; never leave a dead worker as running.", "If preflight requires an expanded panel for a video_storyboard, generate/QA/show/confirm it and recapture the video prompt packet before any paid video call.", "For a late real-person privacy rejection, persist the original provider error and stop before fallback submission. Re-resolve an active capacity-compatible route, recompile and recapture/confirm; design_board identity plates do not enter panel expansion.", "If OCR warning exists, stop and ask customer to accept or regenerate exact segment."],
        "next": "shotcraft_packaging",
    },
    "shotcraft_packaging": {
        "entry_functions": [
            {"module": "visual_intent", "function": "compile_plan", "cli": "python3 scripts/visual_intent.py --plan <storyboard_plan.json> --strict --out <visual_intent.json>"},
            {"module": "postproduction_motion_director", "function": "annotate_plan", "cli": "python3 scripts/postproduction_motion_director.py --storyboard-plan <storyboard_plan.json> --motion-design <motion_design.json> --brand-tokens <style_tokens.json> --out <shotcraft_plan.json>"},
            {"module": "shotcraft_compile", "function": "compile_spec", "cli": "python3 scripts/shotcraft_compile.py --plan <plan.json> --style-tokens <style_tokens.json> --out <shotcraft_spec.json>"},
            {"module": "shotcraft_qc", "function": "check", "cli": "python3 scripts/shotcraft_qc.py --spec <shotcraft_spec.json>"},
            {"module": "shotcraft_packaging", "function": "render_and_record", "cli": "python3 scripts/shotcraft_packaging.py --client <client> --manifest <manifest.json> --spec <shotcraft_spec.json> --out <packaging.mp4>"},
        ],
        "required_inputs": ["approved video", "storyboard_plan.json", "motion_design.json", "brand tokens"],
        "expected_outputs": ["structured visual intent", "motion-directed shotcraft plan", "shotcraft spec", "packaging mp4", "rendered-frame QC report"],
        "confirm_function": {"module": "shotcraft_qc", "function": "check", "cli": "python3 scripts/shotcraft_qc.py --spec <shotcraft_spec.json>"},
        "recovery": ["If auto-selected cards need missing assets, rerun director after binding refs or choose a no-asset card.", "If QC says text is embedded, move text to HyperFrames/captions layer."],
        "next": "captions",
    },
    "captions": {
        "entry_functions": [
            {"module": "script_splitter", "function": "persist_caption_artifact", "cli": "python3 scripts/script_splitter.py derive-captions --segments <segments.json> --results <batch_results.json> --basecut <approved_basecut.mp4> --client <client> --manifest <manifest.json> --caption-manifest-out <caption_manifest.json> --srt-out <captions.srt> --lines-out <lines.json> --motion-out <motion.json>"},
            {"module": "subtitle_overlay", "function": "formal_caption_gate", "cli": "python3 scripts/subtitle_overlay.py run --video <basecut.mp4> --lines <lines.json> --client <client> --manifest <manifest.json> --caption-manifest <caption_manifest.json> --out <captioned.mp4>"},
            {"module": "subtitle_overlay", "function": "record_caption_render", "cli": "same subtitle_overlay.py run formal path"},
        ],
        "required_inputs": ["approved video", "caption lines", "caption artifact"],
        "expected_outputs": ["caption_artifact", "caption render"],
        "confirm_function": {"module": "script_splitter", "function": "confirm_captions", "cli": "python3 scripts/script_splitter.py confirm-captions --client <client> --manifest <manifest.json> --caption-manifest <caption_manifest.json> --customer-confirmation '<客户确认字幕时间线原话>'"},
        "recovery": ["If caption identity mismatches basecut or lines, regenerate caption artifact."],
        "next": "final",
    },
    "final": {
        "entry_functions": [
            {"module": "final_edit", "function": "formal_final_gate", "cli": "python3 scripts/final_edit.py run --scheme <scheme.json> --basecut <captioned.mp4> --client <client> --manifest <manifest.json> --caption-manifest <caption_manifest.json> --out <final.mp4>"},
            {"module": "final_edit", "function": "record_final_generation", "cli": "same final_edit.py run formal path"},
            {"module": "final_edit", "function": "render", "cli": "internal render from final_edit.py run"},
            {"module": "explainer_commercial_qc", "function": "audit", "cli": "document_explainer: python3 scripts/explainer_commercial_qc.py --execution-preview <execution.json> --stage final --approval <approval.json> --final-manifest <integrated.json> --segments <segments.json>"},
        ],
        "required_inputs": ["approved captions", "caption render", "remotion scheme"],
        "expected_outputs": ["final.mp4", "delivery_qc"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage final --customer-confirmation '<客户确认最终成片原话>'"},
        "recovery": ["If media QC fails, fix final edit inputs and rerender before approval."],
        "next": "derive",
    },
    "derive": {
        "entry_functions": [
            {"module": "derive_ratio", "function": "derive_batch", "cli": "python3 scripts/pipeline.py derive --manifest <manifest.json> --source <final.mp4> --ratios <ratios> --out-dir <dir>"},
            {"module": "pipeline", "function": "derive_and_record", "cli": "python3 scripts/pipeline.py derive --manifest <manifest.json> --source <final.mp4> --ratios <ratios> --out-dir <dir>"},
        ],
        "required_inputs": ["approved final.mp4"],
        "expected_outputs": ["derived ratio outputs"],
        "confirm_function": {"module": "pipeline", "function": "approve_stage", "cli": "python3 scripts/pipeline.py approve --manifest <manifest.json> --stage derive --customer-confirmation '<客户确认派生版原话>'"},
        "recovery": ["If no ratios are requested, mark derive skipped and continue to delivery."],
        "next": "delivery",
    },
    "delivery": {
        "entry_functions": [
            {"module": "pipeline", "function": "create_delivery", "cli": "python3 scripts/pipeline.py delivery create --manifest <manifest.json> --out <delivery.json>"},
            {"module": "pipeline", "function": "verify_delivery", "cli": "python3 scripts/pipeline.py delivery verify --manifest <manifest.json> --delivery <delivery.json>"},
        ],
        "required_inputs": ["approved final", "approved captions", "approved video", "delivery_qc"],
        "expected_outputs": ["delivery.json"],
        "confirm_function": {"module": "pipeline", "function": "verify_delivery", "cli": "python3 scripts/pipeline.py delivery verify --manifest <manifest.json> --delivery <delivery.json>"},
        "recovery": ["If delivery evidence is stale, repair exact stale artifact instead of creating a new unchecked delivery."],
        "next": None,
    },
}


# Guidance is selected by the same code that selects the stage. Host-native
# skill discovery is only a convenience; correctness must not depend on a
# particular Agent finding the right markdown file by semantic matching.
_COMMON_GUIDANCE = (
    "AGENT_ENTRY_PROTOCOL.md",
    "AGENTS.md",
    "references/llm-agnostic-runtime.md",
)

_STAGE_GUIDANCE = {
    "idea": ("prompts/video-auto-maker.md", "prompts/script-cocreation.md"),
    "brief": ("prompts/asset-prep.md", "prompts/script-cocreation.md"),
    "script": (
        "prompts/script-cocreation.md",
        "references/professional-storyboard-enrichment.md",
    ),
    "content_scaffold": (
        "prompts/guide-document-class.md",
        "references/document-explainer-playbook.md",
    ),
    "motion_design": (
        "prompts/render-style-guide.md",
        "references/document-explainer-playbook.md",
    ),
    "styleframe": ("prompts/render-style-guide.md",),
    "audio": ("prompts/digital-human.md",),
    "product_board": ("skills/product-consistency/SKILL.md",),
    "cast_board": (
        "skills/product-consistency/SKILL.md",
        "skills/bw-storyboard-color-anchor/SKILL.md",
    ),
    "product_usage": ("skills/product-consistency/SKILL.md",),
    "storyboard": (
        "skills/story-video-orchestrator/SKILL.md",
        "skills/bw-storyboard-color-anchor/SKILL.md",
    ),
    "render_plan": (
        "skills/story-video-orchestrator/SKILL.md",
        "skills/seedance-video/SKILL.md",
    ),
    "test_segment": (
        "skills/story-video-orchestrator/SKILL.md",
        "skills/seedance-video/SKILL.md",
    ),
    "video": (
        "skills/story-video-orchestrator/SKILL.md",
        "skills/seedance-video/SKILL.md",
        "skills/basicrouter-multimodal-api/SKILL.md",
    ),
    "shotcraft_packaging": ("skills/video-shotcraft/SKILL.md",),
    "captions": ("skills/subtitle-overlay-vision/SKILL.md",),
    "final": (
        "skills/subtitle-overlay-vision/SKILL.md",
        "skills/horizontal-kinetic-talk-video/SKILL.md",
    ),
    "derive": ("skills/story-video-orchestrator/SKILL.md",),
    "delivery": ("skills/story-video-orchestrator/SKILL.md",),
}

_PROFILE_GUIDANCE = {
    "oral_broadcast": ("prompts/oral-broadcast.md",),
    "document_explainer": (
        "prompts/explainer-video.md",
        "references/document-explainer-playbook.md",
    ),
    "product_showcase": ("prompts/product-showcase.md",),
    "product_with_human": (
        "prompts/product-demo.md",
        "skills/product-consistency/SKILL.md",
    ),
    "venue_service": ("prompts/oral-scene-service.md",),
    "brand_tvc": ("prompts/brand-tvc.md",),
    "generic_video": ("prompts/video-auto-maker.md",),
}

_BASE_HOST_CAPABILITIES = ("local_files", "python", "stdout_json")
_STAGE_HOST_CAPABILITIES = {
    "idea": ("stdin_secret",),
    "brief": ("local_image_preview",),
    "product_board": ("local_image_preview", "progress_updates"),
    "cast_board": ("local_image_preview", "progress_updates"),
    "product_usage": ("local_image_preview", "progress_updates"),
    "storyboard": ("local_image_preview", "progress_updates"),
    "test_segment": ("local_video_preview", "progress_updates"),
    "video": ("local_video_preview", "progress_updates"),
    "captions": ("local_video_preview", "progress_updates"),
    "final": ("local_video_preview", "progress_updates"),
    "derive": ("local_video_preview",),
    "delivery": ("local_video_preview",),
}


def _unique_strings(values):
    result = []
    for value in values or ():
        if value and value not in result:
            result.append(value)
    return result


def guidance_resources(stage, profile_name=None):
    """Return code-selected guidance paths relative to the package root."""
    return _unique_strings(
        _COMMON_GUIDANCE +
        _STAGE_GUIDANCE.get(stage, ()) +
        _PROFILE_GUIDANCE.get(profile_name or "generic_video", ()))


def required_host_capabilities(stage):
    return _unique_strings(
        _BASE_HOST_CAPABILITIES + _STAGE_HOST_CAPABILITIES.get(stage, ()))


_CHAIN_PROFILES = {
    "oral_broadcast": {
        "entry_command": "/oral-broadcast",
        "package": "oral-broadcast",
        "guide_kind": "document",
        "storyboard_role": "video_storyboard",
        "rules": [
            "Use a confirmed digital-human/cast identity before storyboard and video.",
            "Allow text-to-video only for genuinely pure talking presenter shots.",
            "If physical product refs appear, switch to product_with_human.",
        ],
    },
    "document_explainer": {
        "entry_command": "/explainer-video",
        "package": "explainer",
        "guide_kind": "document",
        "storyboard_role": "design_board",
        "rules": [
            "Storyboard is a customer-facing design board, not a video-model storyboard_ref.",
            "PPT/PDF/Word surfaces are rendered by Remotion/HyperFrames/fuse.",
            "Video model keeps only presenter/scene identity anchors from video_ref_tags.",
        ],
    },
    "product_showcase": {
        "entry_command": "/brand-tvc",
        "package": "brand-tvc",
        "guide_kind": "product",
        "storyboard_role": "video_storyboard",
        "rules": [
            "Do not generate product-free video; missing product anchors must return to asset prep.",
            "Motion labels, prices and specs belong in HyperFrames/packaging, not generated backgrounds.",
        ],
    },
    "product_with_human": {
        "entry_command": "/oral-broadcast",
        "package": "oral-broadcast",
        "guide_kind": "product",
        "storyboard_role": "video_storyboard",
        "rules": [
            "Product board must bind the uploaded product fingerprint.",
            "Usage board must be confirmed by the customer when a human physically uses the product.",
            "Storyboard must be submitted as video_storyboard refs for each shot.",
        ],
    },
    "venue_service": {
        "entry_command": "/explainer-video",
        "package": "explainer",
        "guide_kind": "venue",
        "storyboard_role": "video_storyboard",
        "rules": [
            "Use venue/scene images as environment anchors.",
            "Do not require product boards unless a physical product is introduced.",
        ],
    },
    "brand_tvc": {
        "entry_command": "/brand-tvc",
        "package": "brand-tvc",
        "guide_kind": "product_or_scene",
        "storyboard_role": "video_storyboard",
        "rules": [
            "Use filmic multi-shot storyboard refs and keep adjacent shots visually distinct.",
            "Brand/logo/slogan finishing belongs in deterministic post layers unless it is native packaging text.",
        ],
    },
    "generic_video": {
        "entry_command": "/video-auto-maker",
        "package": "shared-core",
        "guide_kind": "document|product|venue",
        "storyboard_role": "video_storyboard_or_design_board",
        "rules": [
            "Infer a more specific profile as soon as material type, package or storyboard_role is known.",
        ],
    },
}

# Stage order is owned by the host-neutral workflow contract. Profile metadata
# remains here for customer guidance only.
for _profile_name, _profile in _CHAIN_PROFILES.items():
    _profile["stages"] = list(workflow_contract.PROFILE_STAGE_ORDERS[_profile_name])

_STAGE_PREVIEW_KEYS = {
    "brief": ("brief_path",),
    "script": ("script", "script_path", "storyboard_plan"),
    "content_scaffold": ("generation.content_scaffold.outputs",),
    "motion_design": ("generation.motion_design.outputs",),
    "styleframe": ("generation.styleframe.outputs",),
    "audio": ("generation.audio.outputs",),
    "product_board": ("generation.product_board.outputs",),
    "cast_board": ("storyboard_result.cast_board.path", "storyboard_result.cast_board.abspath"),
    "product_usage": ("storyboard_result.product_usage_image.path", "storyboard_result.product_usage_image.abspath"),
    "storyboard": ("storyboard_result.preview_html", "storyboard_result.animatic.path", "storyboard_result.embedded_md", "storyboard_result.index_md"),
    "render_plan": ("generation.render_plan.outputs",),
    "identity_plate_reference": ("generation.render_plan.outputs",),
    "identity_plate_submission_capture": (
        "identity_plate_generation.submission_capture.preview.path",
        "identity_plate_generation.submission_capture.packet.path"),
    "identity_plate_generation": ("identity_plate_generation.output.path",),
    "identity_plate_review": (
        "identity_plate_generation.output.path",
        "identity_plate_generation.visual_review_pending.path"),
    "test_segment": ("generation.test_segment.outputs",),
    "video": ("video_artifact.basecut.path", "video_artifact.results.path", "video_artifact.reviews.path"),
    "shotcraft_packaging": ("generation.shotcraft_packaging.outputs",),
    "captions": ("caption_artifact.path",),
    "final": ("generation.final.outputs",),
    "derive": ("generation.derive.outputs",),
}

_STAGE_NEXT_ACTION = {
    "brief": ("approve_brief", "准备或确认 brief"),
    "script": ("approve_script", "准备或确认 script"),
    "content_scaffold": ("approve_content_scaffold", "准备或确认 content_scaffold"),
    "motion_design": ("approve_motion_design", "准备或确认 motion_design"),
    "styleframe": ("approve_styleframe", "准备或确认 styleframe"),
    "audio": ("approve_audio", "准备或确认 audio"),
    "product_board": ("approve_product_board", "准备或确认 product_board"),
    "cast_board": ("approve_cast_board", "准备或确认 cast_board"),
    "product_usage": ("approve_product_usage", "准备或确认 product_usage"),
    "storyboard": ("approve_storyboard", "确认故事板"),
    "render_plan": ("approve_render_plan", "准备或确认 render_plan"),
    "identity_plate_reference": (
        "approve_identity_plate_reference", "确认身份锚图引用包"),
    "identity_plate_submission_capture": (
        "capture_identity_plate_submission", "捕获并确认身份锚图精确提交包"),
    "identity_plate_generation": (
        "submit_identity_plate", "按已确认请求生成并完成媒体质检"),
    "identity_plate_review": (
        "review_identity_plate", "逐项检查并确认身份锚图"),
    "test_segment": ("approve_test_segment", "准备或确认 test_segment"),
    "video": ("approve_video", "确认视频底片"),
    "shotcraft_packaging": ("approve_shotcraft_packaging", "准备或确认 shotcraft_packaging"),
    "captions": ("approve_captions", "确认字幕时间轴"),
    "final": ("approve_final", "确认最终成片"),
    "derive": ("approve_derive", "确认多比例派生"),
}

_STAGE_SOURCE_KEYS = {
    "brief": ("generation.brief.outputs", "brief", "brief_path"),
    "script": ("generation.script.outputs", "script", "storyboard_plan"),
    "content_scaffold": ("generation.content_scaffold.outputs",),
    "motion_design": ("generation.motion_design.outputs",),
    "styleframe": ("generation.styleframe.outputs",),
    "audio": ("generation.audio.outputs",),
    "product_board": ("generation.product_board.outputs",),
    "cast_board": ("storyboard_approval", "result"),
    "product_usage": ("storyboard_approval", "result"),
    "storyboard": ("storyboard_approval", "result"),
    "render_plan": ("generation.render_plan.outputs",),
    "identity_plate_reference": ("generation.render_plan.outputs",),
    "identity_plate_submission_capture": (
        "identity_plate_generation.submission_capture",),
    "identity_plate_generation": ("identity_plate_generation",),
    "identity_plate_review": ("identity_plate_generation",),
    "test_segment": ("generation.test_segment.outputs",),
    "video": ("video_artifact",),
    "shotcraft_packaging": ("generation.shotcraft_packaging.outputs",),
    "captions": ("caption_artifact",),
    "final": ("generation.final.outputs",),
    "derive": ("generation.derive.outputs",),
}


def _abs(path):
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.abspath(path)


def _unique_paths(paths):
    ordered = []
    for path in paths or []:
        abs_path = _abs(path)
        if abs_path and abs_path not in ordered:
            ordered.append(abs_path)
    return ordered


_CUSTOMER_PREVIEW_SUFFIXES = frozenset((
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    ".mp4", ".mp3", ".m4a", ".wav", ".html", ".md", ".pdf",
))
_INTERNAL_PREVIEW_NAME_TOKENS = (
    "motion_master", "alpha_master", "render-cache", "integrated_spec",
    "hyperframes_spec", "content_spec", "render_quality_spec", "commercial_qc",
    "final_frame_visual_qa", "run_manifest", "execution_preview_result",
)


def _customer_preview_paths(paths):
    """Expose only artifacts a non-technical customer can directly inspect."""
    result = []
    for path in _unique_paths(paths):
        if path.startswith(("https://", "http://")):
            result.append(path)
            continue
        name = os.path.basename(path).lower()
        suffix = os.path.splitext(name)[1]
        if suffix not in _CUSTOMER_PREVIEW_SUFFIXES:
            continue
        if any(token in name for token in _INTERNAL_PREVIEW_NAME_TOKENS):
            continue
        if suffix == ".mov" or suffix == ".webm":
            continue
        result.append(path)
    return result


def _flatten_values(value):
    if isinstance(value, dict):
        for item in value.values():
            for nested in _flatten_values(item):
                yield nested
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            for nested in _flatten_values(item):
                yield nested
    else:
        yield value


def _get_path_value(data, dotted_path):
    current = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _collect_stage_preview(route_source, stage):
    previews = []
    for path in _STAGE_PREVIEW_KEYS.get(stage, ()):
        value = _get_path_value(route_source, path)
        if value is None:
            continue
        for item in _flatten_values(value):
            if isinstance(item, str) and item:
                if os.path.exists(item):
                    previews.append(_abs(item))
    return _unique_paths(previews)


def _collect_stage_sources(route_source, stage):
    sources = []
    for path in _STAGE_SOURCE_KEYS.get(stage, ()):
        value = _get_path_value(route_source, path)
        if value is None:
            continue
        for item in _flatten_values(value):
            if isinstance(item, str) and item:
                if os.path.exists(item):
                    sources.append(_abs(item))
                elif item.startswith(("https://", "http://")):
                    sources.append(item)
    return _unique_strings(sources)


def _resolve_route_source(*, manifest=None, brief=None, storyboard_result=None,
                          segments=None, events=None, source_facts=None):
    return {
        "manifest": _ensure_dict(manifest),
        "brief": _ensure_dict(brief),
        "storyboard_result": _ensure_dict(storyboard_result),
        "segments": _ensure_list(segments),
        "events": _ensure_list(events),
        "generation": _ensure_dict((manifest or {}).get("generation")),
        "storyboard_approval": _ensure_dict((manifest or {}).get("storyboard_approval")),
        "video_artifact": _ensure_dict((manifest or {}).get("video_artifact")),
        "caption_artifact": _ensure_dict((manifest or {}).get("caption_artifact")),
        "delivery": _ensure_dict((manifest or {}).get("delivery")),
        "storyboard_plan": _ensure_dict((manifest or {}).get("storyboard_plan")),
        "script": _ensure_dict((manifest or {}).get("script")),
        "brief_path": (manifest or {}).get("brief_path") or (brief or {}).get("path"),
        "source_facts": _ensure_dict(source_facts),
    }


def _merge_storyboard_source(route_source):
    storyboard_result = _ensure_dict(route_source.get("storyboard_result"))
    approval_result = _ensure_dict(route_source.get("storyboard_approval")).get("result") or {}
    if not storyboard_result and approval_result:
        storyboard_result = approval_result if isinstance(approval_result, dict) else {}
    if storyboard_result and not route_source.get("storyboard_approval"):
        route_source["storyboard_approval"] = {"result": storyboard_result}
    route_source["storyboard_result"] = storyboard_result
    return route_source


def _load_json_or_jsonl(path):
    import json
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        pass
    items = []
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    items.append(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        return None
    return items


def _ensure_dict(value):
    return value if isinstance(value, dict) else {}


def _ensure_list(value):
    return value if isinstance(value, list) else []


def _text_blob(*values):
    parts = []
    for value in values:
        for item in _flatten_values(value):
            if isinstance(item, str) and item.strip():
                parts.append(item.strip().lower())
    return " ".join(parts)


def _has_any_token(value, tokens):
    blob = _text_blob(value)
    return any(token in blob for token in tokens)


def _infer_chain_profile(*, manifest=None, brief=None, storyboard_result=None, segments=None):
    manifest = _ensure_dict(manifest)
    brief = _ensure_dict(brief)
    storyboard_result = _ensure_dict(storyboard_result)
    segments = _ensure_list(segments)
    frozen = manifest.get("chain_profile") or {}
    frozen_name = str(frozen.get("profile") or "").strip()
    if frozen.get("frozen") is True and frozen_name in _CHAIN_PROFILES:
        return {
            "profile": frozen_name,
            "reason": frozen.get("reason") or "frozen_manifest_profile",
            "frozen": True,
            "plan_sha256": frozen.get("plan_sha256"),
        }
    plan_record = manifest.get("storyboard_plan") or {}
    plan_path = plan_record.get("path") if isinstance(plan_record, dict) else None
    plan = _load_json_or_jsonl(plan_path) if plan_path else {}
    if isinstance(plan, list):
        plan = plan[0] if plan else {}
    if not isinstance(plan, dict):
        plan = {}

    route_blob = _text_blob(manifest, brief, storyboard_result, plan, segments)
    storyboard_role = workflow_contract.normalized_storyboard_role(plan)
    if plan:
        # An authored plan is the authority even when its answer is generic.
        # Never reclassify it by scanning dialogue or partial runtime artifacts.
        return workflow_contract.infer_profile(plan)
    guide_kind = str(plan.get("kind") or plan.get("guide_kind") or brief.get("kind") or "").strip().lower()
    package_blob = _text_blob(plan.get("package"), plan.get("package_id"), plan.get("scene_type"), plan.get("workflow"))
    has_product = bool(
        manifest.get("requires_product_board") or manifest.get("requires_product_usage") or
        plan.get("product_type") or plan.get("product_sku") or plan.get("product_name") or
        _has_any_token(plan.get("asset_refs"), ("product_images", "product_boards", "product_usage_images", "product_reference_images")) or
        _has_any_token(storyboard_result, ("product_board", "product_usage_image")) or
        _has_any_token(segments, ("product_type", "product_sku", "product_board", "product_usage"))
    )
    has_human = bool(
        manifest.get("requires_product_usage") or manifest.get("requires_cast_board") or
        plan.get("characters") or plan.get("has_digital_human") or
        _has_any_token(plan.get("asset_refs"), ("digital_human_portraits",)) or
        _has_any_token(storyboard_result, ("cast_board", "digital_human", "presenter")) or
        _has_any_token(segments, ("characters", "cast_board", "presenter", "host"))
    )
    has_venue = bool(
        guide_kind == "venue" or _has_any_token(route_blob, ("venue", "factory", "store", "shop", "scene service")) or
        _has_any_token(plan.get("asset_refs"), ("scene_images", "background_images", "environment_images"))
    )
    has_document = bool(
        storyboard_role == "design_board" or
        guide_kind == "document" or _has_any_token(route_blob, ("ppt", "pdf", "word", "document", "solution", "course", "content_spec")) or
        bool(plan.get("content_spec") or plan.get("content_shotlist") or plan.get("source_ppt") or plan.get("source_pdf") or plan.get("source_doc"))
    )
    if _has_any_token(package_blob, ("brand-tvc", "brand_tvc", "tvc")) or _has_any_token(route_blob, ("brand-tvc", "tvc", "node marketing", "广告片")):
        return {"profile": "brand_tvc", "reason": "package_or_route_mentions_tvc"}
    if has_product and has_human:
        return {"profile": "product_with_human", "reason": "product_and_human_refs"}
    if has_document:
        return {"profile": "document_explainer", "reason": "document_or_ppt_source"}
    if has_product:
        return {"profile": "product_showcase", "reason": "product_refs_without_human"}
    if has_venue and has_human:
        return {"profile": "venue_service", "reason": "venue_and_human_refs"}
    if has_human:
        return {"profile": "oral_broadcast", "reason": "human_refs_without_product"}
    return {"profile": "generic_video", "reason": "fallback_generic"}


def _resolved_profile_stages(profile_name, manifest=None):
    if manifest:
        stages = ["idea"] + rm.required_stage_order(manifest) + ["delivery"]
        if (profile_name == "document_explainer" and
                "render_plan" in stages):
            index = stages.index("render_plan") + 1
            stages[index:index] = list(rm.IDENTITY_PLATE_PSEUDO_STAGES)
        return stages
    profile = _CHAIN_PROFILES.get(profile_name) or _CHAIN_PROFILES.get("generic_video") or {}
    stages = list(profile.get("stages") or [])
    if profile_name == "document_explainer" and "render_plan" in stages:
        index = stages.index("render_plan") + 1
        stages[index:index] = list(rm.IDENTITY_PLATE_PSEUDO_STAGES)
    return stages


def _profile_stage_index(profile_name, stage, manifest=None):
    stages = _resolved_profile_stages(profile_name, manifest)
    try:
        index = stages.index(stage)
    except ValueError:
        return {"profile": profile_name, "stage": stage, "index": -1, "next": None, "previous": None, "stages": stages}
    return {
        "profile": profile_name,
        "stage": stage,
        "index": index,
        "previous": stages[index - 1] if index > 0 else None,
        "next": stages[index + 1] if index + 1 < len(stages) else None,
        "stages": stages,
    }


_TRANSACTIONAL_GENERIC_STAGES = rm.TRANSACTIONAL_GENERIC_STAGES

_SPECIALIZED_TRANSACTION_FINISH = {
    "audio": "audio_contract.prepare_stage validates the complete choice before start and records immutable direction artifacts",
    "product_board": "storyboard.render_storyboard records pending product_board",
    "cast_board": "storyboard.render_storyboard records pending cast_board",
    "product_usage": "storyboard.render_storyboard records all nine panels and pending usage board",
    "storyboard": "storyboard/explainer preview renderer records pending storyboard approval",
    "test_segment": "run_manifest.record_test_segment_finished",
    "video": "script_splitter.record_video_artifact after formal assembly",
    "shotcraft_packaging": "shotcraft_packaging.render_and_record",
    "captions": "script_splitter.persist_caption_artifact",
    "final": "final_edit.formal_final_gate",
    "derive": "pipeline.derive_and_record",
}


def _resolved_flow(stage, chain, manifest=None):
    """Return a profile-resolved flow with an executable stage transaction."""
    flow = dict(_STAGE_FLOW.get(stage, {}))
    entries = [dict(item) for item in (flow.get("entry_functions") or [])]
    if manifest and stage in _TRANSACTIONAL_GENERIC_STAGES:
        begin = {
            "module": "run_manifest", "function": "mark_generation_started",
            "phase": "begin",
            "cli": "python3 scripts/run_manifest.py start-stage --manifest <manifest.json> --stage %s" % stage,
        }
        finish_paths = (
            "--path <motion_design.json> [--path <required-dialogue-binding.json>] "
            "[--path <document-execution-plan.json>]"
            if stage == "motion_design" else "--path <generated-output-path>")
        finish = {
            "module": "run_manifest", "function": "mark_generation_finished",
            "phase": "finish",
            "cli": ("python3 scripts/run_manifest.py finish-stage --manifest <manifest.json> "
                    "--stage %s %s" % (stage, finish_paths)),
        }
        entries = [begin] + [dict(item, phase="generate") for item in entries] + [finish]
        transaction = {
            "mode": "external_three_phase",
            "begin": begin,
            "generate": entries[1:-1],
            "finish": finish,
            "invariant": "begin must commit before generation; finish binds immutable output bytes",
        }
    elif stage in _SPECIALIZED_TRANSACTION_FINISH:
        entries = [dict(item, phase="generate") for item in entries]
        transaction = {
            "mode": "specialized_atomic",
            "begin": {"enforced_by": "specialized entry before paid/local render"},
            "generate": entries,
            "finish": {"enforced_by": _SPECIALIZED_TRANSACTION_FINISH[stage]},
            "invariant": "specialized implementation owns running→pending_approval and immutable evidence",
        }
    elif stage == "delivery":
        entries = [dict(item, phase="commit_or_verify") for item in entries]
        transaction = {
            "mode": "terminal_commit",
            "begin": {"enforced_by": "pipeline.create_delivery preflight"},
            "generate": entries,
            "finish": {"enforced_by": "verified delivery record persisted in run manifest"},
            "invariant": "route returns complete only while delivery bytes verify against current evidence",
        }
    elif stage == "identity_plate_submission_capture":
        entries = [dict(item, phase="local_capture") for item in entries]
        transaction = {
            "mode": "local_capture_then_customer_approval",
            "begin": {"enforced_by": "current reference approval and live catalog"},
            "generate": entries,
            "finish": {"enforced_by": "exact JSON + Markdown hashes and customer marker"},
            "invariant": "capture performs no paid create; changed bytes require recapture",
        }
    elif stage == "identity_plate_generation":
        entries = [dict(
            item,
            phase=("recover_poll_download_qc" if
                   item.get("function") == "recover_identity_plate_task" else
                   "paid_submit_poll_download_qc")) for item in entries]
        transaction = {
            "mode": "approval_bound_async_paid_generation",
            "begin": {"enforced_by": "current confirmed capture and CAS submission intent"},
            "generate": entries,
            "finish": {"enforced_by": "task/request/candidate/result identity plus deterministic media QC"},
            "invariant": "create body must equal the confirmed capture byte-for-byte; an existing taskId is recovered, never resubmitted",
        }
    elif stage == "identity_plate_review":
        entries = [dict(item, phase="visual_review") for item in entries]
        transaction = {
            "mode": "visual_review_then_customer_approval",
            "begin": {"enforced_by": "current candidate, result JSON and passed deterministic QC"},
            "generate": entries,
            "finish": {"enforced_by": "exact visual-review hash plus customer image approval marker"},
            "invariant": "video gates remain closed until all required visual checks and customer confirmation bind the same bytes",
        }
    else:
        transaction = {
            "mode": "non_generating",
            "begin": None, "generate": entries, "finish": None,
            "invariant": "no mutable production artifact transaction",
        }
    flow["entry_functions"] = entries
    flow["transaction"] = transaction
    stages = _resolved_profile_stages(chain.get("profile"), manifest)
    try:
        index = stages.index(stage)
        flow["next"] = stages[index + 1] if index + 1 < len(stages) else None
    except ValueError:
        flow["next"] = None
    flow["next_source"] = "workflow_contract"
    return flow


def _duration_seconds_from_brief(brief):
    """Parse authored duration fields without substring-based misrouting."""
    values = [brief.get(key) for key in (
        "duration", "seconds", "length", "video_duration", "total_seconds")]
    parsed = []
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed.append(float(value))
            continue
        text = str(value or "").strip().lower()
        if not text:
            continue
        clock = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", text)
        if clock:
            hours = int(clock.group(1) or 0)
            parsed.append(hours * 3600 + int(clock.group(2)) * 60 +
                          int(clock.group(3)))
            continue
        number = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*"
                           r"(minutes?|mins?|分钟|秒|seconds?|secs?|s)?(?!\d)",
                           text)
        if number:
            duration = float(number.group(1))
            if (number.group(2) or "") in {
                    "minute", "minutes", "min", "mins", "分钟"}:
                duration *= 60
            parsed.append(duration)
    return parsed


def _infer_route_variant(*, manifest=None, brief=None, storyboard_result=None, segments=None, chain_profile=None):
    manifest = _ensure_dict(manifest)
    brief = _ensure_dict(brief)
    storyboard_result = _ensure_dict(storyboard_result)
    segments = _ensure_list(segments)
    chain_profile = str((chain_profile or {}).get("profile") or "").strip()
    route_blob = _text_blob(manifest, brief, storyboard_result, segments)
    durations = _duration_seconds_from_brief(brief)
    has_short_duration = any(5 <= seconds <= 10 for seconds in durations)
    has_long_duration = any(30 <= seconds <= 60 for seconds in durations)

    # A frozen business profile is authored workflow evidence.  Task history,
    # prompts and product-fact prose are derived snapshots and must never
    # reclassify it by keyword after generation has started.
    frozen_variants = {
        "brand_tvc": ("brand_tvc", "/brand-tvc", "brand-tvc"),
        "document_explainer": ("explainer_video", "/explainer-video", "explainer"),
        "product_with_human": ("product_demo", "/oral-broadcast", "oral-broadcast"),
        "product_showcase": ("product_showcase", "/brand-tvc", "brand-tvc"),
        "venue_service": ("oral_scene_service", "/explainer-video", "explainer"),
        "oral_broadcast": ("oral_broadcast", "/oral-broadcast", "oral-broadcast"),
    }
    if chain_profile in frozen_variants:
        name, entry, package = frozen_variants[chain_profile]
        return {"name": name, "entry_command": entry, "package": package,
                "reason": "frozen_chain_profile"}

    if _has_any_token(route_blob, ("brand-tvc", "tvc", "节点营销大片", "品牌广告片")):
        return {"name": "brand_tvc", "entry_command": "/brand-tvc", "package": "brand-tvc", "reason": "brand_or_tvc_keywords"}
    if _has_any_token(route_blob, ("quick-promo", "quick promo", "reels", "shorts", "tiktok", "抖音快剪", "社交快剪")) or has_short_duration:
        return {"name": "quick_promo", "entry_command": "/brand-tvc", "package": "brand-tvc", "reason": "short_social_cut_subtype"}
    if _has_any_token(route_blob, ("product-demo", "product demo", "实拍演示", "开箱", "实测", "演示")) or has_long_duration:
        package = "oral-broadcast" if chain_profile == "product_with_human" else "brand-tvc"
        return {"name": "product_demo", "entry_command": ("/oral-broadcast" if package == "oral-broadcast" else "/brand-tvc"), "package": package, "reason": "demo_subtype_routed_to_sellable_package"}
    if _has_any_token(route_blob, ("oral-scene-service", "scene service", "实景", "服务介绍", "门店", "工厂", "场地")):
        return {"name": "oral_scene_service", "entry_command": "/explainer-video", "package": "explainer", "reason": "venue_subtype_routed_to_explainer"}
    if _has_any_token(route_blob, ("explainer-video", "讲解", "课程", "方案", "ppt", "pdf", "word")):
        return {"name": "explainer_video", "entry_command": "/explainer-video", "package": "explainer", "reason": "document_explainer_keywords"}
    if _has_any_token(route_blob, ("oral-broadcast", "oral broadcast", "口播", "对镜", "带货", "种草")):
        return {"name": "oral_broadcast", "entry_command": "/oral-broadcast", "package": "oral-broadcast", "reason": "talking_head_keywords"}
    return {"name": "generic_video", "entry_command": "/video-auto-maker", "package": None, "reason": "generic_fallback"}


def _validate_stage_flow():
    import ast
    import re

    errors = []
    errors.extend(workflow_contract.validate_contract())
    module_symbols = {}
    required_flows = tuple(rm.STAGES) + tuple(rm.IDENTITY_PLATE_PSEUDO_STAGES)
    missing = [stage for stage in required_flows if stage not in _STAGE_FLOW]
    if missing:
        errors.append("missing flow stages: %s" % ", ".join(missing))
    for stage, flow in _STAGE_FLOW.items():
        for field in ("entry_functions", "required_inputs", "expected_outputs", "recovery"):
            if field not in flow:
                errors.append("%s missing %s" % (stage, field))
        if "next" not in flow:
            errors.append("%s missing next" % stage)
        if "confirm_function" not in flow:
            errors.append("%s missing confirm_function" % stage)
        calls = list(flow.get("entry_functions") or [])
        if flow.get("confirm_function"):
            calls.append(flow["confirm_function"])
        for call in calls:
            module = str((call or {}).get("module") or "").strip()
            function = str((call or {}).get("function") or "").split(".", 1)[0].strip()
            module_path = os.path.join(HERE, module + ".py")
            if not module or not os.path.isfile(module_path):
                errors.append("%s references missing module %s" % (stage, module or "<empty>"))
                continue
            if module not in module_symbols:
                try:
                    with open(module_path, encoding="utf-8") as handle:
                        tree = ast.parse(handle.read(), filename=module_path)
                    module_symbols[module] = {
                        node.name for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    }
                except (OSError, SyntaxError) as exc:
                    errors.append("%s cannot inspect module %s: %s" % (stage, module, exc))
                    module_symbols[module] = set()
            if function and function not in module_symbols[module]:
                errors.append("%s references missing callable %s.%s" %
                              (stage, module, function))
            for relative_script in re.findall(
                    r"scripts/[A-Za-z0-9_./-]+\.py", str((call or {}).get("cli") or "")):
                if not os.path.isfile(os.path.join(ROOT, relative_script)):
                    errors.append("%s CLI references missing script %s" %
                                  (stage, relative_script))
            cli_text = str((call or {}).get("cli") or "").lower()
            if any(token in cli_text for token in (
                    "待实现", "placeholder", "todo", "not implemented")):
                errors.append("%s CLI contains placeholder implementation text" % stage)
    for name, profile in _CHAIN_PROFILES.items():
        for field in ("entry_command", "package", "guide_kind", "storyboard_role", "stages", "rules"):
            if field not in profile:
                errors.append("%s missing %s" % (name, field))
        for stage in profile.get("stages") or []:
            if stage not in _STAGE_FLOW and stage != "delivery":
                errors.append("%s references unknown stage %s" % (name, stage))
        if tuple(profile.get("stages") or ()) != workflow_contract.PROFILE_STAGE_ORDERS.get(name):
            errors.append("%s stage order drifted from workflow_contract" % name)
        expected_package = workflow_contract.PROFILE_PACKAGE_IDS.get(name)
        if expected_package and profile.get("package") != expected_package:
            errors.append("%s package drifted from workflow_contract" % name)
        if expected_package:
            expected_entry = {
                "oral-broadcast": "/oral-broadcast",
                "explainer": "/explainer-video",
                "brand-tvc": "/brand-tvc",
            }[expected_package]
            if profile.get("entry_command") != expected_entry:
                errors.append("%s entry command bypasses sellable package %s" %
                              (name, expected_package))
    for stage in _STAGE_GUIDANCE:
        if stage not in _STAGE_FLOW:
            errors.append("guidance references unknown stage %s" % stage)
    for label, resources in list(_STAGE_GUIDANCE.items()) + list(_PROFILE_GUIDANCE.items()):
        for resource in resources:
            if not os.path.isfile(os.path.join(ROOT, resource)):
                errors.append("%s guidance resource missing: %s" % (label, resource))
    for relative in (
        "scripts/workflow_api.py", "scripts/stage_packet.py",
        "scripts/artifact_registry.py", "scripts/review_task.py",
        "scripts/workflow_mcp_server.py",
        "scripts/runtime_audit.py", "scripts/release_audit.py",
        "scripts/interaction_contract.py", "scripts/model_behavior.py",
        "scripts/storyboard_plan_compiler.py", "scripts/text_provider.py",
        "schemas/host-capabilities.schema.json",
        "schemas/model-behavior-profile.schema.json",
        "schemas/interaction-contract.schema.json",
        "schemas/stage-packet.schema.json", "schemas/experience-packet.schema.json",
        "schemas/artifact-registry.schema.json",
        "schemas/review-presentation.schema.json", "schemas/source-facts.schema.json",
        "schemas/creative-brief.schema.json", "schemas/script-beats.schema.json",
        "schemas/shot-patch.schema.json",
        "schemas/runtime-audit-event.schema.json",
        "schemas/release-validation.schema.json",
    ):
        if not os.path.isfile(os.path.join(ROOT, relative)):
            errors.append("LLM-neutral runtime contract missing: %s" % relative)
    for stage in _STAGE_FLOW:
        try:
            ic.build_contract(stage, {"flow": _STAGE_FLOW[stage]}, {},
                              model_behavior.conservative_profile())
        except (ValueError, TypeError, OSError) as exc:
            errors.append("%s interaction contract invalid: %s" % (stage, exc))
    return errors


def _storyboard_previews(manifest):
    result_record = (manifest.get("storyboard_approval") or {}).get("result") or {}
    path = result_record.get("path")
    if not path or not os.path.isfile(path):
        return []
    try:
        import json
        with open(path, encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    previews = []
    for key in ("preview_html", "embedded_md", "index_md"):
        value = result.get(key)
        if value and os.path.exists(value):
            previews.append(_abs(value))
    return _unique_paths(previews)


def _stage_required_in_manifest(manifest, stage):
    return rm.stage_required(manifest, stage)


def _document_pre_video_gate_route(manifest, current, previews):
    """Block test/video until identity and exact prompt packets are current."""
    profile = str((manifest.get("chain_profile") or {}).get("profile") or "")
    if profile != "document_explainer" or current not in {"test_segment", "video"}:
        return None
    render_generation = (manifest.get("generation") or {}).get("render_plan") or {}
    primary = render_generation.get("primary_artifact") or {}
    current_plan = rm.file_record(primary.get("path"))
    if (not current_plan or not current_plan.get("exists") or
            current_plan.get("sha256") != primary.get("sha256")):
        return {
            "current_stage": "render_plan", "next_action": "repair_render_plan",
            "customer_message": "正式渲染计划已变化或缺失，必须重新生成并确认后才能准备视频。",
            "customer_preview": [], "stage_sources": [], "cost_line": "",
            "flow": _STAGE_FLOW.get("render_plan", {}),
        }
    gates = manifest.get("pre_video_gates") or {}
    identity = gates.get("identity_plate") or {}
    asset = identity.get("asset") or {}
    identity_record = rm.file_record(asset.get("path"))
    identity_current = bool(
        identity.get("status") == "confirmed" and
        identity.get("render_plan") == current_plan and
        asset.get("confirmed_by_customer") is True and
        asset.get("confirmation_source") == "customer_dialogue" and
        identity_record and identity_record.get("exists") and
        identity_record.get("sha256") == asset.get("sha256"))
    if not identity_current:
        return {
            "current_stage": current,
            "pre_video_substage": "identity_plate_pending_or_approval",
            "next_action": "generate_or_confirm_identity_plate",
            "customer_message": (
                "渲染计划已确认，但单人身份锚图尚未生成、展示并单独确认；"
                "完成看图确认和哈希绑定前，不会准备测试片或正式视频。"),
            "customer_preview": _customer_preview_paths(previews),
            "stage_sources": [current_plan["path"]], "cost_line": "",
            "flow": _STAGE_FLOW.get("render_plan", {}),
        }
    prompt_gate = gates.get("video_prompt_review") or {}
    review_record = prompt_gate.get("review") or {}
    prompt_current = bool(
        prompt_gate.get("status") == "confirmed" and
        prompt_gate.get("render_plan") == current_plan and
        rm.file_record_is_current(review_record))
    if prompt_current:
        try:
            import prompt_review
            review = _load_json_or_jsonl(review_record.get("path")) or {}
            prompt_review.assert_review_current(review, path=review_record.get("path"))
            prompt_current = bool(
                review.get("stage") == "video" and review.get("status") == "confirmed" and
                review.get("confirmed_by_customer") is True)
        except (OSError, ValueError, TypeError):
            prompt_current = False
    if not prompt_current:
        return {
            "current_stage": current,
            "pre_video_substage": "video_prompt_review_pending_or_approval",
            "next_action": "preflight_capture_and_confirm_video_prompts",
            "customer_message": (
                "身份锚图已确认。下一步先对最终 segments 做模型预检，并展示主模型、"
                "全部降级模型、逐字提示词和 imageUrls；客户明确确认后才允许提交。"),
            "customer_preview": _customer_preview_paths([asset.get("path"), *previews]),
            "stage_sources": [current_plan["path"]], "cost_line": "",
            "flow": _STAGE_FLOW.get("video", {}),
        }
    return None


def _identity_plate_manifest_route(manifest, current, previews, route_source):
    state = manifest.get("identity_plate_generation") or {}
    status = str(state.get("status") or "").strip().lower()
    if current == "identity_plate_reference":
        action = "approve_identity_plate_reference"
        message = (
            "正式渲染计划已确认。请单独核对身份锚图所用人物板、提示词、"
            "模型规格和完整请求指纹；明确确认前不会捕获或提交付费请求。")
    elif current == "identity_plate_submission_capture":
        if status == "submission_capture_pending_customer_confirmation":
            action = "approve_identity_plate_submission_capture"
            message = (
                "精确提交包已经本地捕获，尚未调用生成模型。请查看 Markdown 中的"
                "模型、画幅、分辨率、完整提示词、引用图和指纹，再明确确认。")
        else:
            action = "capture_identity_plate_submission"
            message = (
                "身份引用包已确认。下一步只在本地捕获精确 imageUrls 请求与实时规格，"
                "生成 JSON 和可读预览；这一步不产生图片费用。")
    elif current == "identity_plate_review":
        pending_review = state.get("visual_review_pending") or {}
        if rm.file_record_is_current(pending_review):
            action = "approve_generated_identity_plate"
            message = (
                "身份锚图已完成确定性媒体质检和逐项视觉复核。请查看真实图片与"
                "复核记录；只有客户明确确认同一图片后，视频提示词审核才会开放。")
        else:
            action = "inspect_and_record_identity_plate_visual_review"
            message = (
                "身份锚图已下载且媒体规格合格。请先实际查看图片，逐项检查单人、"
                "人脸清晰、人物一致性、发型服装配饰、干净背景、无 PPT/文字/Logo/"
                "水印及构图可用性，并记录复核证据；此时尚不能进入视频阶段。")
        previews = _customer_preview_paths([
            (state.get("output") or {}).get("path"),
            pending_review.get("path"), *previews])
    elif status in {"submitting", "submitted"}:
        action = "recover_identity_plate_task"
        message = (
            "身份锚图请求已绑定远程 taskId。下一步必须轮询并下载这个同一任务，"
            "完成解码、尺寸和画幅质检；禁止再次提交付费请求。")
    elif status == "failed":
        failure_record = state.get("failure") or {}
        failure_document = (
            _load_json_or_jsonl(failure_record.get("path"))
            if rm.file_record_is_current(failure_record) else {}) or {}
        if (failure_document.get("kind") in {
                "identity_plate_visual_review",
                "identity_plate_customer_rejection"} and
                failure_document.get("status") == "rejected"):
            action = "authorize_identity_plate_retry"
            message = (
                "身份锚图已生成并完成媒体质检，但视觉复核或客户验图未通过。拒绝"
                "证据已绑定当前图片；需要客户明确授权新的付费 attempt 后才能重生。")
            previews = _customer_preview_paths([
                (state.get("output") or {}).get("path"),
                failure_record.get("path"), *previews])
        elif (state.get("task_id") and
              state.get("remote_terminal_status") != "failed"):
            action = "recover_identity_plate_task"
            message = (
                "身份锚图本地流程失败，但远程任务可能仍在运行或已成功；"
                "必须先恢复并轮询同一 taskId，禁止直接发起第二次付费请求。")
        else:
            action = "authorize_identity_plate_retry"
            message = (
                "身份锚图本次生成已失败并安全登记实际请求与任务证据；"
                "远程任务已确认失败或尚未创建，需要客户明确授权一个新的付费 attempt 后才能重试。")
    else:
        action = "submit_identity_plate"
        message = (
            "身份锚图精确提交包已由客户确认，可以按已确认字节发起一次生成。"
            "任何参数或引用变化都会阻断并要求重新捕获。")
    return {
        "current_stage": current, "next_action": action,
        "customer_message": message,
        "customer_preview": _customer_preview_paths(previews),
        "stage_sources": _collect_stage_sources(route_source, current),
        "cost_line": "", "flow": _resolved_flow(
            current, manifest.get("chain_profile") or {}, manifest),
    }


def _audio_canary_promotion_route(manifest, current, previews):
    artifact = manifest.get("test_segment_artifact") or {}
    promotion = artifact.get("formal_batch_promotion") or {}
    if (current != "video" or artifact.get("status") != "approved" or
            artifact.get("audio_capability_policy") !=
            "empirical_canary_unknown_output_audio" or
            promotion.get("status") == "fulfilled"):
        return None
    sources = _unique_paths([
        (artifact.get("video") or {}).get("path"),
        (artifact.get("review") or {}).get("path"),
        *previews,
    ])
    return {
        "current_stage": "video",
        "next_action": "promote_audio_canary_for_batch",
        "customer_message": (
            "测试片已证明当前精确模型路由能输出非静音音轨。请先执行原子晋级："
            "退休仅限测试段的临时策略、绑定验证证据并复用已完成的试片任务；"
            "随后重新展示并确认完整批次提示词，不会重复生成该测试段。"),
        "customer_preview": sources,
        "stage_sources": sources,
        "cost_line": "本步骤只更新本地合同，不产生模型费用。",
        "flow": _STAGE_FLOW.get("video", {}),
    }


def _manifest_route(manifest):
    manifest = _ensure_dict(manifest)
    current = rm.next_required_stage(manifest)
    if current is None:
        current = "delivery"
    generation = (manifest.get("generation") or {}).get(current) or {}
    generation_status = str(generation.get("status") or "").strip().lower()
    pending = generation_status == "pending_approval"
    route_source = _merge_storyboard_source(_resolve_route_source(manifest=manifest))
    result_record = (manifest.get("storyboard_approval") or {}).get("result") or {}
    result_path = result_record.get("path")
    if result_path and os.path.isfile(result_path):
        route_source["storyboard_result"] = _load_json_or_jsonl(result_path) or {}
    previews = _collect_stage_preview(route_source, current)
    # Stage-specific records can point at a machine-readable JSON artifact
    # (for example storyboard_plan.json).  Keep the complete pending output
    # set alongside it so a customer-readable Markdown/image/video companion
    # is still available after ``_customer_preview_paths`` filters JSON.
    previews = _unique_paths(
        list(previews or []) + list(generation.get("outputs") or []))
    if current == "storyboard":
        previews = _unique_paths(_storyboard_previews(manifest) + previews)

    if current in rm.IDENTITY_PLATE_PSEUDO_STAGES:
        return _identity_plate_manifest_route(
            manifest, current, previews, route_source)

    if (current == "test_segment" and
            generation_status == "failed_pending_recovery"):
        result_record = generation.get("results") or {}
        result_path = result_record.get("path")
        previews = _unique_paths(
            ([result_path] if result_path else []) + previews)
        return {
            "current_stage": "test_segment",
            "next_action": "recover_test_segment",
            "customer_message": (
                "测试片任务已失败且执行 lease 已收口；当前不是生成中。请查看"
                "精确 provider、模型、handoff 与策略证据，重新选择可执行路线并"
                "授权新的单段 attempt；测试通过前不得进入完整批次。"),
            "customer_preview": previews,
            "stage_sources": previews,
            "cost_line": "",
            "flow": _STAGE_FLOW.get("test_segment", {}),
        }

    promotion_route = _audio_canary_promotion_route(
        manifest, current, previews)
    if promotion_route:
        return promotion_route

    pre_video_route = _document_pre_video_gate_route(manifest, current, previews)
    if pre_video_route:
        return pre_video_route

    if current == "storyboard" and generation_status == "running":
        execution_path = generation.get("planned_result")
        execution = (_load_json_or_jsonl(execution_path)
                     if execution_path and os.path.isfile(execution_path) else {}) or {}
        binding = execution.get("manifest_binding") or {}
        blocked_by = binding.get("approval_blocked_by")
        if blocked_by in {"commercial_qc_failed", "final_commercial_qc_failed"}:
            return {
                "current_stage": current,
                "next_action": "repair_execution_visual_review",
                "customer_message": (
                    "执行预演的视觉复核未通过，当前产物不会进入客户审批；"
                    "请修复对应镜头并重新渲染本次 attempt。"),
                "customer_preview": [],
                "stage_sources": _collect_stage_sources(route_source, current),
                "cost_line": "",
                "flow": _STAGE_FLOW.get(current, {}),
            }
        if blocked_by == "final_frame_visual_qa_required":
            previews = _customer_preview_paths([
                (execution.get("animatic") or {}).get("path"),
                *[item.get("path") for item in execution.get("layout_boards") or []],
                *previews,
            ])
            return {
                "current_stage": current,
                "next_action": "submit_execution_visual_review",
                "customer_message": (
                    "执行版式板与 Animatic 已完成，正在进行逐帧人工视觉复核；"
                    "通过后才会请你查看和确认。"),
                "customer_preview": previews,
                "stage_sources": _collect_stage_sources(route_source, current),
                "cost_line": "",
                "flow": _STAGE_FLOW.get(current, {}),
            }

    if current == "delivery":
        try:
            delivery_contract.delivery_snapshot(manifest)
            delivery = manifest.get("delivery") or {}
            delivery_path = delivery.get("path")
            current_record = rm.file_record(delivery_path) if delivery_path else None
            if (delivery.get("status") == "verified" and current_record and
                    current_record.get("exists") and
                    current_record.get("sha256") == delivery.get("sha256")):
                delivery_contract.verify_delivery(manifest, delivery_path)
                action = "complete"
                message = "交付清单与当前成片、审批和质检证据一致，本次任务已完成。"
                previews = _unique_paths([delivery_path] + previews)
            else:
                action = "create_delivery"
                message = "最终成片已确认，所有交付证据齐全，可以生成并登记可校验的交付清单。"
        except (ValueError, OSError, KeyError) as exc:
            action = "repair_delivery_evidence"
            message = "阶段审批已完成，但交付证据仍不完整：%s。请先补齐 reviews、take、OCR 或 formal QC。" % exc
    elif current == "derive":
        derive_artifacts = (manifest.get("generation") or {}).get("derive") or {}
        derive_outputs = _unique_paths(derive_artifacts.get("outputs") or [])
        if derive_outputs:
            previews = _unique_paths(derive_outputs + previews)
        derive_status = derive_artifacts.get("status")
        if derive_status == "pending_approval":
            action = "approve_derive"
            message = "多比例派生已完成，请查看各比例版本并确认。"
        elif derive_status == "skipped":
            action = "create_delivery"
            message = "多比例派生已按合同跳过，可直接生成交付清单。"
        else:
            action = "start_derive"
            message = "成片已确认，可进入多比例派生阶段（零模型成本，纯 ffmpeg 重排）。"
    elif current == "test_segment" and generation_status in {
            "", "authorized_retry"}:
        action = "start_test_segment"
        message = (
            "恢复合同要求先验证当前精确镜头、handoff 与模型路由；可以启动一次"
            "受控试片，验收通过前不会进入完整批量生成。")
    elif current == "video" and generation_status in {"", "authorized_retry"}:
        action = "start_video"
        message = (
            "视频生成合同已就绪，可以启动或继续受控 attempt；当前没有可供确认的"
            "底片，生成结果落盘并完成技术验收前不会请求客户审批。")
    elif generation_status in {"awaiting_take_review",
                               "generated_pending_take_review"}:
        action = "review_video_takes"
        message = (
            "所有已生成分段已安全收口，远程 task 与结果均已绑定；请逐段验片并登记"
            " accepted/rejected review，未完成前不会进入拼接或再次付费生成。")
    elif generation_status == "recovery_required":
        action = "recover_video_tasks"
        message = (
            "本次视频 worker 已结束并清除运行 lease，但仍有失败或待恢复分段。请优先"
            "恢复原 task/download；终态失败须先登记 rejected review，再受控授权新 attempt。")
    elif generation_status == "blocked":
        blocker = generation.get("blocker") or {}
        if blocker.get("blocker_code") == (
                "PRIVACY_FALLBACK_REAPPROVAL_REQUIRED"):
            action = "recompile_privacy_fallback_contract"
            message = (
                "原视频任务已因真人参考图隐私限制安全中止，原 task、拒绝原因与未知"
                "计费证据均已保留。请按实时模型容量重新选择链/非链执行合同，重新"
                "capture-video 并让客户确认；design-board 身份底片不需要单格展开图。")
        else:
            action = "repair_%s_contract" % current
            message = (
                "%s阶段已安全停止，等待新的可执行合同或客户确认；不会把它继续显示为生成中。" %
                current)
    elif generation_status == "failed":
        failure = generation.get("failure") or {}
        retryable = failure.get("retryable") is not False
        action = ("retry_%s" if retryable else "repair_%s") % current
        message = (
            "%s阶段本次执行已失败并安全结束，没有停留在运行中；请%s后重新开始。" %
            (current, "检查失败原因并重试" if retryable else "先修复输入或环境"))
    elif generation_status in {"running", "in_progress", "submitted"}:
        action = "wait_for_%s" % current
        message = "%s阶段正在执行；完成或失败后状态会自动更新，请勿重复启动。" % current
    elif pending:
        if current == "audio":
            action = "approve_audio"
            message = (
                "声音方案合同已准备好，请确认声音模式、语言、音色、语气和 BGM。"
                "本阶段没有独立 TTS 试听；第一次真实试听和口型检查发生在后续单段测试片。")
        else:
            action, message = _STAGE_NEXT_ACTION.get(current, ("approve_%s" % current, "请查看预览并确认%s阶段；文件变化后本次确认会自动失效。" % current))
    else:
        if current == "audio":
            action = "collect_audio_choice"
            message = (
                "下一步先确认声音模式、语言、音色方向、讲述语气和 BGM。"
                "全部选择完成后才会生成声音方案合同；当前没有独立声音模型，"
                "不会承诺或生成试听音轨。第一次真实试听在单段测试片。")
        else:
            action, message = _STAGE_NEXT_ACTION.get(current, ("start_%s" % current, "下一步进入%s阶段，完成后会先给你预览确认。" % current))

    return {
        "current_stage": current,
        "next_action": action,
        "customer_message": message,
        "customer_preview": previews,
        "stage_sources": _collect_stage_sources(route_source, current),
        "cost_line": "",
        "flow": _STAGE_FLOW.get(current, {}),
    }


def _artifact_route(brief, storyboard_result, segments, events):
    brief = _ensure_dict(brief)
    storyboard_result = _ensure_dict(storyboard_result)
    segments = _ensure_list(segments)
    events = _ensure_list(events)
    counts = {}
    for event in events:
        name = str((event or {}).get("event") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    video_paths = []
    graph = asset_graph.build_graph({}, storyboard_result, {"segments": segments})
    for node in graph.get("nodes") or []:
        if node.get("kind") == "segment":
            detail = node.get("detail") or ""
            if detail and os.path.isfile(detail):
                video_paths.append(_abs(detail))
    video_paths = _unique_paths(video_paths)
    if video_paths:
        if counts.get("ocr_warning"):
            return {
                "current_stage": "video",
                "next_action": "review_ocr_warning",
                "customer_message": "视频段已生成，OCR 检测发现画面文字，需要确认这些文字是否为产品/品牌原生内容，或选择重出对应段落。",
                "customer_preview": video_paths,
                "cost_line": "",
            }
        return {
            "current_stage": "video",
            "next_action": "review_generated_videos",
            "customer_message": "视频段已生成，等待客户查看成片效果并确认是否进入后期剪辑。",
            "customer_preview": video_paths,
            "cost_line": "",
        }
    if storyboard_result.get("shots"):
        approval_status = str(
            (storyboard_result.get("storyboard_approval") or {}).get("status") or ""
        ).strip().lower()
        invalidated = bool(
            storyboard_result.get("invalidated") or
            approval_status in {"invalidated", "stale", "failed", "rejected"})
        confirmed = bool(
            approval_status == "confirmed" and not invalidated and
            not storyboard_result.get("needs_confirmation") and
            storyboard_result_approval_is_current(storyboard_result))
        previews = _unique_paths([
            item.get("abspath") or item.get("path")
            for item in storyboard_result.get("shots") or []
            if isinstance(item, dict)
        ])
        return {
            "current_stage": "video" if confirmed else "storyboard",
            "next_action": ("review_storyboard_or_generate_video" if confirmed else
                            ("regenerate_storyboard_revision" if invalidated else
                             "review_storyboard")),
            "customer_message": (
                "故事板和引用素材已确认，可以准备视频生成。" if confirmed else
                ("旧故事板审批已失效，需要按当前合同生成新修订并重新确认。" if invalidated else
                 "故事板已准备好，等待客户查看并确认。")),
            "customer_preview": previews,
            "cost_line": "",
        }
    if brief.get("images"):
        return {
            "current_stage": "product_board",
            "next_action": "prepare_boards",
            "customer_message": "产品素材已导入，下一步生成或确认产品板、人物板和使用细节图。",
            "customer_preview": _unique_paths([
                item.get("path") for item in brief.get("images") or [] if isinstance(item, dict)
            ]),
            "cost_line": "",
        }
    return {}


def _stage_allowed_in_profile(stage, profile_name):
    profile = _CHAIN_PROFILES.get(profile_name) or _CHAIN_PROFILES.get("generic_video") or {}
    stages = profile.get("stages") or []
    return stage in stages


def stage_contract(route_source, stage, model_profile=None):
    manifest = _ensure_dict(route_source.get("manifest"))
    if manifest:
        rm.ensure_workflow_contract(manifest)
    brief = _ensure_dict(route_source.get("brief"))
    storyboard_result = _ensure_dict(route_source.get("storyboard_result"))
    segments = _ensure_list(route_source.get("segments"))
    events = _ensure_list(route_source.get("events"))
    chain = _infer_chain_profile(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result, segments=segments)
    variant = _infer_route_variant(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result,
        segments=segments, chain_profile=chain)
    source_facts = _ensure_dict(route_source.get("source_facts"))
    route_source = _merge_storyboard_source(_resolve_route_source(
        manifest=manifest,
        brief=brief,
        storyboard_result=storyboard_result,
        segments=segments,
        events=events,
        source_facts=source_facts,
    ))
    profile_name = chain.get("profile") or "generic_video"
    source_artifacts = _collect_stage_sources(route_source, stage)
    preview_sources = _collect_stage_preview(route_source, stage)
    if not preview_sources:
        preview_sources = [path for path in source_artifacts if os.path.exists(path)]
    runtime_route = _manifest_route(manifest) if manifest else {}
    runtime_current = runtime_route.get("current_stage") == stage
    if runtime_current:
        preview_sources = _unique_paths(
            list(runtime_route.get("customer_preview") or []) +
            list(preview_sources or []))
        source_artifacts = _unique_paths(
            list(runtime_route.get("stage_sources") or []) +
            list(source_artifacts or []))
    static_action, static_message = _STAGE_NEXT_ACTION.get(
        stage, ("start_%s" % stage, ""))
    contract = {
        "protocol_version": ROUTER_CONTRACT_VERSION,
        "code_source": "scripts/workflow_router.py",
        "stage": stage,
        "preview_sources": preview_sources,
        "source_artifacts": source_artifacts,
        "next_action": (runtime_route.get("next_action")
                        if runtime_current else static_action),
        "next_message": (runtime_route.get("customer_message")
                         if runtime_current else static_message),
        "flow": _resolved_flow(stage, chain, manifest),
        "guidance_resources": guidance_resources(stage, profile_name),
        "required_host_capabilities": required_host_capabilities(stage),
        "chain_profile": chain,
        "chain_stage": _profile_stage_index(chain.get("profile"), stage, manifest),
        "route_variant": variant,
        "host_capability_schema": "schemas/host-capabilities.schema.json",
        "model_behavior_schema": "schemas/model-behavior-profile.schema.json",
    }
    behavior = model_profile or model_behavior.conservative_profile()
    contract["interaction_contract"] = ic.build_contract(
        stage, contract, route_source, behavior)
    contract["allowed_actions"] = contract["interaction_contract"]["allowed_actions"]
    return contract


def route_workflow(*, manifest=None, brief=None, storyboard_result=None, segments=None,
                   events=None, source_facts=None, model_profile=None):
    """Return the canonical next-action contract for the current workflow."""
    manifest = _ensure_dict(manifest)
    if manifest:
        rm.ensure_workflow_contract(manifest)
    brief = _ensure_dict(brief)
    storyboard_result = _ensure_dict(storyboard_result)
    segments = _ensure_list(segments)
    events = _ensure_list(events)
    chain = _infer_chain_profile(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result, segments=segments)
    variant = _infer_route_variant(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result,
        segments=segments, chain_profile=chain)

    plan_record = manifest.get("storyboard_plan") or {}
    script_closed_without_plan = bool(
        manifest and (
            manifest.get("workflow_contract_error") or
            (rm.approval_is_current(manifest, "script") and
             (not isinstance(plan_record, dict) or
              not rm.file_record_is_current(plan_record)))))
    if script_closed_without_plan:
        route = {
            "current_stage": "script",
            "next_action": "bind_storyboard_plan",
            "customer_message": (
                "Run 尚未绑定有效的 storyboard_plan 与售卖包；请先执行 "
                "run_manifest.py bind-plan。绑定或计划变化会重新打开脚本确认，"
                "在此之前不得跳到人物板/故事板。"),
            "customer_preview": [],
            "cost_line": "",
            "flow": _STAGE_FLOW.get("script", {}),
        }
    else:
        route = _manifest_route(manifest) if manifest else {}
    artifact_route = _artifact_route(brief, storyboard_result, segments, events)
    if artifact_route:
        if not route:
            route = artifact_route
        elif artifact_route.get("current_stage") == route.get("current_stage"):
            if artifact_route.get("customer_preview"):
                route["customer_preview"] = _unique_paths(
                    list(artifact_route.get("customer_preview") or []) +
                    list(route.get("customer_preview") or []))
            if artifact_route.get("customer_message") and route.get("customer_message") in ("", None):
                route["customer_message"] = artifact_route["customer_message"]
    if not route:
        route = {
            "current_stage": "idea",
            "next_action": "start_brief",
            "customer_message": "等待从素材或需求开始创建第一版 brief。",
            "customer_preview": [],
            "cost_line": "",
            "flow": _STAGE_FLOW.get("idea", {}),
        }
    route["customer_preview"] = _customer_preview_paths(
        route.get("customer_preview") or [])
    route.setdefault("cost_line", "")
    route["flow"] = _resolved_flow(
        route.get("current_stage"), chain, manifest if manifest else None)
    route["route_kind"] = "manifest" if manifest else ("artifact" if artifact_route else "empty")
    route["chain_profile"] = chain
    route["chain_stage"] = _profile_stage_index(
        chain.get("profile"), route.get("current_stage") or "idea",
        manifest if manifest else None)
    route["route_variant"] = variant
    current_stage = route.get("current_stage") or "idea"
    profile_name = chain.get("profile") or "generic_video"
    route["protocol_version"] = ROUTER_CONTRACT_VERSION
    route["code_source"] = "scripts/workflow_router.py"
    route["guidance_resources"] = guidance_resources(current_stage, profile_name)
    route["required_host_capabilities"] = required_host_capabilities(current_stage)
    route_source = _merge_storyboard_source(_resolve_route_source(
        manifest=manifest, brief=brief, storyboard_result=storyboard_result,
        segments=segments, events=events, source_facts=source_facts))
    behavior = model_profile or model_behavior.conservative_profile()
    route["interaction_contract"] = ic.build_contract(
        current_stage, route, route_source, behavior)
    route["allowed_actions"] = route["interaction_contract"]["allowed_actions"]
    route["host_capability_schema"] = "schemas/host-capabilities.schema.json"
    route["model_behavior_schema"] = "schemas/model-behavior-profile.schema.json"
    return route


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Print canonical workflow routing state")
    parser.add_argument("command", choices=("status", "contract", "validate", "chain", "flow"))
    parser.add_argument("--manifest")
    parser.add_argument("--brief")
    parser.add_argument("--storyboard-result")
    parser.add_argument("--segments")
    parser.add_argument("--events")
    parser.add_argument("--source-facts")
    parser.add_argument("--model-profile")
    parser.add_argument("--stage")
    args = parser.parse_args(argv)
    if args.command == "validate":
        errors = _validate_stage_flow()
        payload = {
            "ok": not errors,
            "errors": errors,
            "stage_count": len(_STAGE_FLOW),
            "chain_profile_count": len(_CHAIN_PROFILES),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not errors else 2
    if args.command == "flow":
        resolved_catalog = {}
        for stage, flow in _STAGE_FLOW.items():
            resolved_catalog[stage] = dict(flow, next="profile-dependent",
                                           next_source="workflow_contract")
        print(json.dumps({
            "protocol_version": ROUTER_CONTRACT_VERSION,
            "code_source": "scripts/workflow_router.py",
            "workflow_contract": workflow_contract.contract_snapshot(),
            "stage_flow": resolved_catalog,
            "chain_profiles": _CHAIN_PROFILES,
            "stage_guidance": _STAGE_GUIDANCE,
            "profile_guidance": _PROFILE_GUIDANCE,
        }, ensure_ascii=False, indent=2))
        return 0
    payload = {}
    for label, path in (
        ("manifest", args.manifest),
        ("brief", args.brief),
        ("storyboard_result", args.storyboard_result),
        ("segments", args.segments),
        ("events", args.events),
        ("source_facts", args.source_facts),
    ):
        if not path:
            continue
        value = _load_json_or_jsonl(path)
        if value is None:
            value = []
        payload[label] = value
    behavior = model_behavior.load_profile(args.model_profile) if args.model_profile else None
    result = route_workflow(
        manifest=payload.get("manifest"),
        brief=payload.get("brief"),
        storyboard_result=payload.get("storyboard_result"),
        segments=payload.get("segments") if isinstance(payload.get("segments"), list) else (
            (payload.get("segments") or {}).get("segments") or []),
        events=payload.get("events") if isinstance(payload.get("events"), list) else [],
        source_facts=payload.get("source_facts") if isinstance(payload.get("source_facts"), dict) else {},
        model_profile=behavior,
    )
    if args.command == "chain":
        print(json.dumps(result.get("chain_profile") or {}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "contract":
        stage = args.stage or result.get("current_stage") or "idea"
        print(json.dumps(stage_contract(
            {
                "manifest": payload.get("manifest"),
                "brief": payload.get("brief"),
                "storyboard_result": payload.get("storyboard_result"),
                "segments": payload.get("segments") if isinstance(payload.get("segments"), list) else (
                    (payload.get("segments") or {}).get("segments") or []),
                "events": payload.get("events") if isinstance(payload.get("events"), list) else [],
                "source_facts": payload.get("source_facts") if isinstance(payload.get("source_facts"), dict) else {},
            },
            stage, model_profile=behavior), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
