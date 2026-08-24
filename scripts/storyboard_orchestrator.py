#!/usr/bin/env python3
"""Storyboard paid-generation stage orchestration.

Loaded lazily by :mod:`storyboard` so the public CLI and API stay compatible,
while stage sequencing is isolated from prompt construction and contracts.
"""
import storyboard as _storyboard

DEFAULT_MODEL = _storyboard.DEFAULT_MODEL
MAX_DESIGN_BOARD_QC_ATTEMPTS = _storyboard.MAX_DESIGN_BOARD_QC_ATTEMPTS
PRODUCT_USAGE_POLICY_VERSION = _storyboard.PRODUCT_USAGE_POLICY_VERSION
Path = _storyboard.Path
ROOT = _storyboard.ROOT
STORYBOARD_RATIO = _storyboard.STORYBOARD_RATIO
_StoryboardCheckpoint = _storyboard._StoryboardCheckpoint
_approval_current = _storyboard._approval_current
_approval_path = _storyboard._approval_path
_approval_record = _storyboard._approval_record
_backup_existing_board = _storyboard._backup_existing_board
_backup_existing_storyboard_shot = _storyboard._backup_existing_storyboard_shot
_begin_manifest_generation = _storyboard._begin_manifest_generation
_build_design_board_composite_plan = _storyboard._build_design_board_composite_plan
_collect_image_urls = _storyboard._collect_image_urls
_confirmed_product_identity_paths = _storyboard._confirmed_product_identity_paths
_discover_run_manifest = _storyboard._discover_run_manifest
_effective_board_approval = _storyboard._effective_board_approval
_existing_shot_matches_plan = _storyboard._existing_shot_matches_plan
_file_sha256 = _storyboard._file_sha256
_load_prompt_review_for_shots = _storyboard._load_prompt_review_for_shots
_looks_like_design_board = _storyboard._looks_like_design_board
_mark_board_regeneration_pending = _storyboard._mark_board_regeneration_pending
_merge_reference_urls = _storyboard._merge_reference_urls
_plan_product_facts = _storyboard._plan_product_facts
_prepare_usage_panel_reference_paths = _storyboard._prepare_usage_panel_reference_paths
_read_result = _storyboard._read_result
_refresh_workflow_canvas = _storyboard._refresh_workflow_canvas
_registered_board = _storyboard._registered_board
_source_refs_fingerprint = _storyboard._source_refs_fingerprint
_sync_usage_reference_contract = _storyboard._sync_usage_reference_contract
_usage_action_shot = _storyboard._usage_action_shot
_usage_reference_urls = _storyboard._usage_reference_urls
_validate_manifest_identity = _storyboard._validate_manifest_identity
_validate_reference_registry = _storyboard._validate_reference_registry
_validate_shot_reference_registry = _storyboard._validate_shot_reference_registry
_write_run_pointer = _storyboard._write_run_pointer
atomic_write_json = _storyboard.atomic_write_json
br_client = _storyboard.br_client
build_reference_registry = _storyboard.build_reference_registry
canonical_storyboard_plan = _storyboard.canonical_storyboard_plan
cast_prompt = _storyboard.cast_prompt
cast_source_fingerprint = _storyboard.cast_source_fingerprint
cast_reference_pack = _storyboard.cast_reference_pack
client_slug = _storyboard.client_slug
contact_sheet_prompt = _storyboard.contact_sheet_prompt
datetime = _storyboard.datetime
design_board_image_qc = _storyboard.design_board_image_qc
download_first_image = _storyboard.download_first_image
expand_product_sku_refs = _storyboard.expand_product_sku_refs
generate_usage_panels = _storyboard.generate_usage_panels
json = _storyboard.json
key_setup = _storyboard.key_setup
load_plan_json = _storyboard.load_plan_json
needs_product_board = _storyboard.needs_product_board
needs_product_usage_image = _storyboard.needs_product_usage_image
os = _storyboard.os
plan_fingerprint = _storyboard.plan_fingerprint
product_usage_geometry_contract = _storyboard.product_usage_geometry_contract
product_usage_outcome_context = _storyboard.product_usage_outcome_context
product_usage_physical_relation = _storyboard.product_usage_physical_relation
product_usage_prompt = _storyboard.product_usage_prompt
reference_fingerprint = _storyboard.reference_fingerprint
resolve_run_output_dir = _storyboard.resolve_run_output_dir
run_manifest_store = _storyboard.run_manifest_store
safe_name = _storyboard.safe_name
schema_validate = _storyboard.schema_validate
shot_fingerprint = _storyboard.shot_fingerprint
shot_reference_registry = _storyboard.shot_reference_registry
storyboard_plan_audit = _storyboard.storyboard_plan_audit
storyboard_semantics = _storyboard.storyboard_semantics
ux = _storyboard.ux
visual_plan_fingerprint = _storyboard.visual_plan_fingerprint
workflow_contract = _storyboard.workflow_contract


def _render_deterministic_design_board(
        *, enabled, plan, plan_path, out_dir, run_id, only_shot_ids,
        current_plan_fingerprint, results, reference_registry, finish):
    if enabled:
        if only_shot_ids:
            raise br_client.BRError(
                "DESIGN_BOARD_PARTIAL_RENDER_FORBIDDEN: 文档讲解的版式板与 Animatic 共用一条时间轴，"
                "修改任一镜头后必须由 Remotion 重渲全部执行预览，不能只换单张概念图。")
        print(
            "[design-board] 使用正式 Remotion Content + IntegratedExplainer 组件渲染执行版式板和 Animatic；"
            "不调用图像模型。", flush=True)
        import explainer_layout_preview as _layout_preview
        import explainer_commercial_qc as _commercial_qc

        manifest_path = os.path.join(
            ROOT, "output", str(plan.get("client") or ""),
            str(run_id or plan.get("run_id") or ""), "run_manifest.json")
        if not os.path.isfile(manifest_path):
            raise br_client.BRError(
                "DESIGN_BOARD_RUN_MANIFEST_REQUIRED: 正式设计板执行预演必须先创建权威 "
                "output/<client>/<run-id>/run_manifest.json，并完成上游阶段确认。")
        execution_out_dir = os.path.join(os.path.dirname(manifest_path), "layout_preview")

        human_image = None
        for value in (plan.get("asset_refs") or {}).get("digital_human_portraits") or []:
            if not isinstance(value, str) or value.startswith(("http://", "https://", "data:")):
                continue
            candidate = value if os.path.isabs(value) else os.path.join(ROOT, value)
            if os.path.isfile(candidate):
                human_image = os.path.abspath(candidate)
                break
        execution = _layout_preview.render_execution_preview(
            plan, execution_out_dir,
            source_plan=plan_path, human_image=human_image, quality="high",
            manifest_path=manifest_path, require_manifest=True)
        commercial_qc = _commercial_qc.audit(
            execution, stage="preview", probe_alpha=True,
            require_manifest_binding=True)
        commercial_qc_path = os.path.join(
            execution_out_dir, "commercial_qc.json")
        Path(commercial_qc_path).write_text(
            json.dumps(commercial_qc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        if not commercial_qc.get("passed"):
            raise br_client.BRError(
                "DESIGN_BOARD_COMMERCIAL_QC_FAILED: %s" %
                " | ".join(
                    "%s:%s" % (item.get("code"), item.get("message"))
                    for item in commercial_qc.get("blockers") or []))
        rendered_shots = []
        by_id = {
            str(shot.get("id") or index): shot
            for index, shot in enumerate(plan.get("shots") or [], 1)
        }
        for record in execution.get("shots") or []:
            shot = by_id.get(str(record.get("id") or "")) or record.get("shot") or {}
            shot_registry = shot_reference_registry(reference_registry, shot)
            item = dict(record)
            item.update({
                "shot": shot,
                "plan_fingerprint": current_plan_fingerprint,
                "shot_fingerprint": shot_fingerprint(shot),
                "reference_fingerprint": reference_fingerprint(shot_registry),
                "reference_registry": shot_registry,
                "design_board_qc": {
                    "ok": bool(commercial_qc.get("passed")),
                    "failures": [item.get("code") for item in
                                 commercial_qc.get("blockers") or []],
                    "method": "deterministic_remotion_render+commercial_semantic_qc",
                    "commercial_qc": os.path.abspath(commercial_qc_path),
                },
            })
            rendered_shots.append(item)
        if len(rendered_shots) != len(plan.get("shots") or []):
            raise br_client.BRError(
                "DESIGN_BOARD_RENDER_INCOMPLETE: Remotion 执行版式板数量与剧本镜头数不一致。")
        composite_plan = _build_design_board_composite_plan(plan, rendered_shots)
        composite_plan.setdefault("contract", {})["renderer_source_of_truth"] = (
            "remotion.IntegratedExplainer")
        composite_plan["contract"]["animatic_required_for_approval"] = True
        composite_path = os.path.join(out_dir, "composite_plan.json")
        atomic_write_json(composite_path, composite_plan)
        results.update({
            "shots": rendered_shots,
            "layout_boards": rendered_shots,
            "storyboard_role": "design_board",
            "storyboard_usage": "design_board",
            "video_model_usage": "do_not_submit_as_storyboard_ref",
            "design_board_renderer": "remotion.IntegratedExplainer",
            "design_board_artifact_contract": execution.get("artifact_contract") or {},
            "animatic": execution.get("animatic") or {},
            "content_layer": execution.get("content_layer") or {},
            "motion_layer": execution.get("motion_layer") or {},
            "hyperframes_spec": execution.get("hyperframes_spec") or {},
            "audio_timing_contract": execution.get("audio_timing_contract") or {},
            "approved_render_contract": execution.get("approved_render_contract") or {},
            "execution_preview_result": execution.get("result_json"),
            "manifest_path": os.path.abspath(manifest_path),
            "manifest_binding": execution.get("manifest_binding") or {},
            "integrated_previs_spec": execution.get("integrated_spec") or {},
            "generation_keyframe_contract": execution.get("generation_keyframe_contract") or {},
            "composite_plan": composite_plan,
            "composite_plan_path": os.path.abspath(composite_path),
        })
        return finish("storyboard", needs_confirmation=True)
    return None


def _render_storyboard_shot_set(
        *, plan, out_dir, only_shot_ids, force_shot_ids,
        current_plan_fingerprint, api_key, bw, plan_ref_urls, model,
        results, result_path, reference_registry, design_board_run,
        checkpoint):
    save_progress = checkpoint.save
    progress_callback = checkpoint.progress_callback
    previous_task = checkpoint.previous_task
    _begin_manifest_generation(results, "storyboard", force=True)
    previous_shots = {}
    for item in results.get("shots") or []:
        sid = str((item.get("shot") or {}).get("id") or item.get("id") or "").strip()
        if sid:
            previous_shots[sid] = item
    confirmed_shots = {}
    if only_shot_ids and not previous_shots:
        approval = _read_result(_approval_path(out_dir, "storyboard"))
        for item in (approval or {}).get("shots") or []:
            sid = str(item.get("id") or "")
            if sid:
                confirmed_shots[sid] = item
    rendered_shots = []
    for index, shot in enumerate(plan.get("shots") or [], 1):
        sid = str(shot.get("id") or index)
        shot_out = os.path.join(out_dir, "shot_%02d_%s.jpg" % (index, safe_name(sid)))
        existing = previous_shots.get(sid)
        shot_registry = shot_reference_registry(reference_registry, shot)
        _validate_shot_reference_registry(shot, shot_registry)
        shot_reference_fp = reference_fingerprint(shot_registry)
        if only_shot_ids and sid not in only_shot_ids:
            carry = existing or confirmed_shots.get(sid)
            carry_path = ((carry or {}).get("abspath") or (carry or {}).get("path") or
                          (shot_out if os.path.isfile(shot_out) else None))
            if not carry_path or not os.path.isfile(carry_path):
                raise br_client.BRError(
                    "ONLY_SHOT_CARRY_FORWARD_MISSING: %s 缺少可沿用的已确认故事板图。" % sid)
            carried = dict(carry or {})
            carried.update({"path": carry_path, "abspath": os.path.abspath(carry_path),
                            "id": sid,
                            "sha256": _file_sha256(carry_path),
                            "plan_fingerprint": current_plan_fingerprint,
                            "shot_fingerprint": shot_fingerprint(shot),
                            "reference_fingerprint": shot_reference_fp,
                            "reference_registry": shot_registry,
                            "shot": shot,
                            "carried_forward": True,
                            "carry_forward_reason": "only_shot_regeneration"})
            rendered_shots.append(carried)
            continue
        prior_design_qc_attempts = 0
        if _existing_shot_matches_plan(
                existing, shot, current_plan_fingerprint, shot_out,
                expected_reference_fingerprint=shot_reference_fp) and sid not in force_shot_ids:
            existing = dict(existing)
            existing_path = existing.get("abspath") or existing.get("path") or shot_out
            if design_board_run:
                qc = design_board_image_qc(existing_path)
                existing["design_board_qc"] = qc
                if not qc.get("ok"):
                    prior_design_qc_attempts = max(
                        1, int(existing.get("design_board_qc_attempts") or 0))
                    if (prior_design_qc_attempts >= MAX_DESIGN_BOARD_QC_ATTEMPTS and
                            sid not in force_shot_ids):
                        existing["design_board_qc_retry_exhausted"] = True
                        results["shots"] = rendered_shots + [existing]
                        results.pop("in_progress", None)
                        save_progress()
                        raise br_client.BRError(
                            "DESIGN_BOARD_QC_RETRY_EXHAUSTED: 镜头 %s 已连续 %d 次未通过单画面质检，"
                            "为避免继续产生费用，自动重试已停止。请先修改提示词或使用 --force-shot %s 明确重试。" %
                            (sid, prior_design_qc_attempts, sid))
                    print("[design-board-qc] existing shot %s rejected, regenerating: %s" %
                          (sid, ", ".join(qc.get("failures") or [])), flush=True)
                    existing = None
                else:
                    existing.update({"id": sid,
                                     "path": existing.get("path") or shot_out,
                                     "abspath": os.path.abspath(existing_path),
                                     "shot": shot})
                    rendered_shots.append(existing)
                    continue
            if existing is None:
                pass
            else:
                existing.update({"id": sid,
                                 "path": existing.get("path") or shot_out,
                                 "abspath": os.path.abspath(existing_path),
                                 "shot": shot})
                rendered_shots.append(existing)
                continue
        force_shot = sid in force_shot_ids
        backup_path = None
        if force_shot and os.path.isfile(shot_out):
            backup_path = _backup_existing_storyboard_shot(shot_out, out_dir)
            prior_design_qc_attempts = 0
        print("[gpt-image-2] rendering storyboard panel for shot %s…" % sid, flush=True)
        shot_ref_urls = _collect_image_urls(
            [item["url"] for item in shot_registry], api_key,
            fail_on_invalid=True, label="故事板参考图:%s" % sid) if shot_registry else []
        shot_plan = dict(plan, shots=[shot])
        submission_prompt = contact_sheet_prompt(
            shot_plan, bw=bw, reference_registry=shot_registry)
        if design_board_run and prior_design_qc_attempts:
            submission_prompt += (
                "\n[CORRECTIVE RETRY] The previous result was rejected because it looked like a grid, collage, "
                "contact sheet, or contained readable generated text. Return exactly ONE uninterrupted edge-to-edge "
                "16:9 composition frame. Do not divide the canvas with separator lines or repeat the scene.")
        image = download_first_image(
            api_key, submission_prompt,
            shot_out, model=model, ratio="16:9",
            image_urls=shot_ref_urls or plan_ref_urls or None,
            on_progress=progress_callback("storyboard", sid, shot_out),
            resume_task_id=(None if force_shot or os.path.isfile(shot_out)
                            else previous_task("storyboard", sid)),
            force=force_shot or os.path.isfile(shot_out))
        image.update({"path": shot_out, "abspath": os.path.abspath(shot_out),
                      "id": sid,
                      "sha256": _file_sha256(shot_out),
                      "plan_fingerprint": current_plan_fingerprint,
                      "shot_fingerprint": shot_fingerprint(shot),
                      "reference_fingerprint": shot_reference_fp,
                      "reference_registry": shot_registry,
                      "shot": shot})
        if backup_path:
            image["forced_regeneration"] = {
                "reason": "force_shot",
                "previous_backup": backup_path,
                "forced_at": datetime.now().isoformat(timespec="seconds"),
            }
        if design_board_run:
            image["design_board_qc_attempts"] = prior_design_qc_attempts + 1
            qc = design_board_image_qc(shot_out)
            image["design_board_qc"] = qc
            if not qc.get("ok"):
                image["design_board_qc_retry_exhausted"] = bool(
                    image["design_board_qc_attempts"] >= MAX_DESIGN_BOARD_QC_ATTEMPTS)
                results["shots"] = rendered_shots + [image]
                results.pop("in_progress", None)
                save_progress()
                raise br_client.BRError(
                    "DESIGN_BOARD_QC_FAILED: 镜头 %s 的设计板不是干净单画面，"
                    "已拒收，原因：%s。当前尝试 %d/%d；禁止展示给客户确认。%s" %
                    (sid, ", ".join(qc.get("failures") or []),
                     image["design_board_qc_attempts"], MAX_DESIGN_BOARD_QC_ATTEMPTS,
                     ("自动重试额度已用完，请先修改提示词或显式 --force-shot。"
                      if image["design_board_qc_retry_exhausted"] else
                      "下次运行会使用纠正提示词重试一次。")))
        rendered_shots.append(image)
        results["shots"] = rendered_shots
        results.pop("in_progress", None)
        save_progress()
        _refresh_workflow_canvas(
            results, result_path, reason="storyboard_shot_qc_passed:%s" % sid)
    return rendered_shots


def _finalize_storyboard_motion_contract(
        *, plan, out_dir, api_key, results, rendered_shots):
    results["shots"] = rendered_shots
    motion_design_bundle = None
    try:
        import motion_design as _motion_design
        motion_design_bundle = _motion_design.design_from_plan(plan, api_key=None)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print("[storyboard] motion_design fallback unavailable: %s" % exc, flush=True)
    if motion_design_bundle:
        motion_design_path = os.path.join(out_dir, "motion_design.json")
        atomic_write_json(motion_design_path, motion_design_bundle)
        results["motion_design"] = motion_design_bundle
        results["motion_design_path"] = os.path.abspath(motion_design_path)
        motion_design_by_id = {
            str(item.get("shot_id")): item
            for item in (motion_design_bundle.get("shots") or [])
            if isinstance(item, dict) and item.get("shot_id")
        }
        for rendered in rendered_shots:
            shot = rendered.get("shot") if isinstance(rendered, dict) else None
            if not isinstance(shot, dict):
                continue
            sid = str(shot.get("id") or rendered.get("id") or "")
            design_shot = motion_design_by_id.get(sid)
            if not design_shot:
                continue
            shot = dict(shot)
            shot["motion_design"] = design_shot
            shot["motion_plan"] = design_shot.get("motion_plan") or shot.get("motion_plan") or {}
            shot["asset_slots"] = design_shot.get("asset_slots") or shot.get("asset_slots") or []
            shot["motion_elements"] = design_shot.get("motion_elements") or shot.get("motion_elements") or []
            shot["video_safe_zones"] = design_shot.get("video_safe_zones") or shot.get("video_safe_zones") or []
            rendered["shot"] = shot
            if isinstance(rendered.get("motion_design"), dict):
                rendered["motion_design"] = design_shot
    if _looks_like_design_board(plan):
        composite_plan = _build_design_board_composite_plan(plan, rendered_shots)
        composite_path = os.path.join(out_dir, "composite_plan.json")
        atomic_write_json(composite_path, composite_plan)
        results["storyboard_role"] = "design_board"
        results["storyboard_usage"] = "design_board"
        results["video_model_usage"] = "do_not_submit_as_storyboard_ref"
        results["composite_plan"] = composite_plan
        results["composite_plan_path"] = os.path.abspath(composite_path)


def _render_final_storyboard_stage(
        *, plan, plan_path, out_dir, run_id, only_shot_ids,
        force_shot_ids, current_plan_fingerprint, api_key, bw,
        plan_ref_urls, model, results, result_path, checkpoint):
    """Render the final review contract after all reference boards are approved."""
    save_progress = checkpoint.save
    finish = checkpoint.finish
    progress_callback = checkpoint.progress_callback
    previous_task = checkpoint.previous_task
    # The contact sheet is a review artifact. Its N panels map 1:1 to the
    # authored shots. Narrative/TVC workflows may later use each panel as a
    # video reference; oral/PPT explainer workflows may use it only as a design
    # board. In both cases this registry is a whole-plan index. Enforce the
    # BasicRouter image-reference limit after filtering to one shot.
    reference_registry = build_reference_registry(plan)
    _validate_reference_registry(plan, reference_registry)
    results["reference_registry"] = reference_registry
    design_board_run = _looks_like_design_board(plan)
    import video_use_bridge as _video_use_bridge
    deterministic_design_board_run = bool(
        design_board_run and _video_use_bridge._is_document_explainer_plan(plan))
    deterministic_result = _render_deterministic_design_board(
        enabled=deterministic_design_board_run, plan=plan, plan_path=plan_path,
        out_dir=out_dir, run_id=run_id, only_shot_ids=only_shot_ids,
        current_plan_fingerprint=current_plan_fingerprint, results=results,
        reference_registry=reference_registry, finish=finish)
    if deterministic_result is not None:
        return deterministic_result
    rendered_shots = _render_storyboard_shot_set(
        plan=plan, out_dir=out_dir, only_shot_ids=only_shot_ids,
        force_shot_ids=force_shot_ids,
        current_plan_fingerprint=current_plan_fingerprint, api_key=api_key,
        bw=bw, plan_ref_urls=plan_ref_urls, model=model, results=results,
        result_path=result_path, reference_registry=reference_registry,
        design_board_run=design_board_run, checkpoint=checkpoint)
    _finalize_storyboard_motion_contract(
        plan=plan, out_dir=out_dir, api_key=api_key, results=results,
        rendered_shots=rendered_shots)
    return finish("storyboard", needs_confirmation=True)


def _render_product_board_stage(
        *, plan, brief, client, product_urls, out_dir, asset_refs, api_key,
        model, stage, results, checkpoint, portrait_urls, scene_urls,
        plan_ref_urls):
    """Generate or resume the product-only identity board gate."""
    save_progress = checkpoint.save
    finish = checkpoint.finish
    product_approved = not needs_product_board(plan)
    product_board_urls = []
    # Product board is conditional on actual user/product references. A pure
    # character or environment project receives only cast + storyboard.
    if needs_product_board(plan):
        product_entries = [entry for entry in (brief.get("images") or [])
                           if isinstance(entry, dict) and
                           asset_prep.is_product_asset_ready(client, entry.get("path"))]
        if not product_entries:
            raise br_client.BRError(
                "PRODUCT_CLEANUP_CONFIRMATION_REQUIRED: 产品素材必须先经过 gpt-image-2 清洗并确认；"
                "请先用 standardize/clean-image 生成 pending 候选，再执行 confirm-image。")
        if not product_urls:
            raise br_client.BRError(
                "检测到产品镜头，但没有可用的产品参考图。请先用 asset_prep.py ingest-image 导入真实产品图，"
                "并确认后再生成产品板/故事板；不会用文字描述替代产品锚图。")
        try:
            import product_board as _pb
            product_meta = _plan_product_facts(plan)
            product_fp = _pb.product_board_source_fingerprint(
                product_urls[:4], product_meta)
            registered_product = _registered_board(client, "product", product_fp)
            local_product_approved = bool(registered_product) or _approval_current(
                out_dir, "product", product_fp)
            product_approved = _effective_board_approval(
                results, "product_board", local_product_approved)
            if stage in ("cast", "usage", "storyboard") and not product_approved:
                raise br_client.BRError(
                    "GENERATION_BLOCKED: 请先展示并确认产品板，再生成后续素材。")
            if registered_product:
                print("[storyboard] reusing confirmed product board from asset registry", flush=True)
                results["product_board"] = registered_product
            else:
                _begin_manifest_generation(results, "product_board", force=True)
                results["product_board"] = _pb.generate_from_reference_urls(
                    api_key, product_urls, out_dir,
                    product_type=(plan.get("product_type") or asset_refs.get("product_type") or "the exact product"),
                    product_color=(plan.get("product_color") or (brief.get("product_color") if isinstance(brief, dict) else None)),
                    style_hint=(plan.get("visual_style") or "commercial product reference photography"),
                    model=model,
                    product_meta=product_meta)
            results["product_board"]["board_type"] = "product_only_3x3"
            results["product_board"]["description"] = (
                "产品本体九宫格：只展示同一产品的多角度、结构、材质和接口，不包含人物或使用场景。")
            save_progress()
            if not registered_product:
                print("[gpt-image-2] rendering conditional product board…", flush=True)
        except br_client.BRError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            raise br_client.BRError("检测到产品素材，但产品板生成失败：%s" % exc)
        product_fp = results["product_board"].get("source_fingerprint")
        local_product_approved = bool(registered_product) or _approval_current(
            out_dir, "product", product_fp)
        product_approved = _effective_board_approval(
            results, "product_board", local_product_approved)
        results["product_board"]["status"] = "confirmed" if product_approved else "pending"
        if stage == "product" or (stage == "next" and not product_approved):
            return (
                finish("product_board", needs_confirmation=True),
                product_approved,
                product_board_urls,
                plan_ref_urls,
            )
        if stage in ("cast", "usage", "storyboard") and not product_approved:
            raise br_client.BRError("GENERATION_BLOCKED: 请先展示并确认产品板，再生成后续素材。")
        if product_approved:
            product_board_path = results["product_board"].get("abspath") or results["product_board"].get("path")
            product_board_urls = _collect_image_urls(
                [product_board_path] if product_board_path else [], api_key,
                fail_on_invalid=True, label="已确认产品板")
            plan_ref_urls = _merge_reference_urls(
                portrait_urls, product_board_urls + product_urls, scene_urls)
    return None, product_approved, product_board_urls, plan_ref_urls


def _render_cast_board_stage(
        *, plan, out_dir, force_boards, client, results, stage, api_key,
        model, portrait_urls, product_board_urls, product_urls, scene_urls,
        checkpoint, plan_ref_urls):
    """Generate or resume the six-view character identity board gate."""
    save_progress = checkpoint.save
    finish = checkpoint.finish
    progress_callback = checkpoint.progress_callback
    previous_task = checkpoint.previous_task
    cast_out = None
    cast_board_urls = []
    cp = cast_prompt(plan)
    cast_approved = not bool(cp)
    if cp:
        cast_out = os.path.join(out_dir, "cast_board.jpg")
        force_cast = "cast" in force_boards
        # 人物板：用数字人肖像参考图做 img2img（确保生成的六视图与真实上传图一致）
        cast_fp = cast_source_fingerprint(plan, cp)
        existing_cast = results.get("cast_board") or {}
        registered_cast = None if force_cast else _registered_board(client, "cast", cast_fp)
        confirmed_cast = _approval_record(out_dir, "cast")
        local_cast_approved = (not force_cast and (
            bool(registered_cast) or _approval_current(out_dir, "cast", cast_fp)))
        cast_approved = _effective_board_approval(
            results, "cast_board", local_cast_approved)
        if stage in ("usage", "storyboard") and not cast_approved and not force_cast:
            raise br_client.BRError(
                "GENERATION_BLOCKED: 请先展示并确认人物板，再生成分段故事板。")
        if registered_cast:
            print("[storyboard] reusing confirmed cast board from asset registry", flush=True)
            results["cast_board"] = registered_cast
            cast_out = registered_cast["path"]
        cast_source_changed = (existing_cast.get("source_fingerprint") and
                                existing_cast.get("source_fingerprint") != cast_fp)
        if not registered_cast and cast_source_changed and os.path.isfile(cast_out):
            os.remove(cast_out)
        cast_backup = (_backup_existing_board(cast_out, out_dir, "cast")
                       if force_cast else None)
        if force_cast:
            _mark_board_regeneration_pending(
                results, "cast_board", kind="cast", path=cast_out,
                source_fingerprint=cast_fp, backup_path=cast_backup)
            save_progress()
        if not registered_cast and (force_cast or not os.path.isfile(cast_out) or os.path.getsize(cast_out) == 0):
            print("[gpt-image-2] rendering cast board…", flush=True)
            _begin_manifest_generation(results, "cast_board", force=True)
            results["cast_board"] = download_first_image(
                api_key, cp, cast_out, model=model, ratio="16:9",
                image_urls=portrait_urls or None,
                on_progress=progress_callback("cast_board", "cast", cast_out),
                resume_task_id=None if force_cast else previous_task("cast_board", "cast"),
                force=force_cast)
            results["cast_board"]["source_fingerprint"] = cast_fp
            results["cast_board"]["board_type"] = "digital_human_6_view"
            results["cast_board"]["description"] = "数字人六视图身份板：锁定脸部、发型、服装、配饰和身体比例。"
            if cast_backup:
                results["cast_board"]["forced_regeneration"] = {
                    "reason": "missing_remote_url",
                    "previous_backup": cast_backup,
                    "forced_at": datetime.now().isoformat(timespec="seconds"),
                }
        elif not registered_cast:
            results["cast_board"] = {"url": existing_cast.get("url") or confirmed_cast.get("url", ""),
                                     "abspath": os.path.abspath(cast_out), "skipped": True,
                                     "path": cast_out,
                                     "source_fingerprint": existing_cast.get("source_fingerprint") or cast_fp}
            for field in ("result_url", "task_id", "request_id", "board_sha256", "sha256"):
                value = existing_cast.get(field) or confirmed_cast.get(field)
                if value:
                    results["cast_board"][field] = value
        # Both names are kept for compatibility, but they always describe the
        # current bytes.  Rebuilding the status dictionary must never erase the
        # cast identity anchor recorded by customer confirmation.
        cast_sha256 = _file_sha256(cast_out)
        results["cast_board"]["board_sha256"] = cast_sha256
        results["cast_board"]["sha256"] = cast_sha256
        results.pop("in_progress", None)
        local_cast_approved = bool(registered_cast) or _approval_current(
            out_dir, "cast", cast_fp)
        cast_approved = _effective_board_approval(
            results, "cast_board", local_cast_approved)
        results["cast_board"]["status"] = "confirmed" if cast_approved else "pending"
        reference_pack = cast_reference_pack.derive(
            cast_out, os.path.join(out_dir, "cast_reference_pack"))
        results["cast_board"]["reference_pack"] = reference_pack
        save_progress()
        if stage == "cast" or (stage == "next" and not cast_approved):
            return (
                finish("cast_board", needs_confirmation=True),
                cast_approved,
                cast_board_urls,
                cast_out,
                plan_ref_urls,
            )
        if stage in ("usage", "storyboard") and not cast_approved:
            raise br_client.BRError("GENERATION_BLOCKED: 请先展示并确认人物板，再生成分段故事板。")
        if cast_approved:
            views = reference_pack.get("views") or {}
            cast_reference_paths = [
                (views.get(role) or {}).get("path")
                for role in ("face_front", "full_body_front")
                if (views.get(role) or {}).get("path")
            ]
            cast_board_urls = _collect_image_urls(
                cast_reference_paths, api_key, fail_on_invalid=True,
                label="已确认人物单视图身份锚")
            plan["asset_refs"] = dict(
                plan.get("asset_refs") or {},
                digital_human_portraits=cast_reference_paths,
                cast_boards=[])
            plan_ref_urls = _merge_reference_urls(
                cast_board_urls + portrait_urls,
                product_board_urls + product_urls, scene_urls)
    return None, cast_approved, cast_board_urls, cast_out, plan_ref_urls


def _materialize_usage_board(
        *, results, force_usage, usage_out, usage_fp, usage_backup,
        panel_prompt_text, usage_geometry_contract, usage_relation_contract,
        usage_prompt_shot, usage_generation_current, api_key, plan,
        panel_reference_urls, model, has_human, progress_callback,
        product_identity_paths, cast_board_path, asset_refs,
        panel_reference_paths, confirmed_usage, save_progress):
    if force_usage:
        _mark_board_regeneration_pending(
            results, "product_usage_image", kind="usage", path=usage_out,
            source_fingerprint=usage_fp, backup_path=usage_backup)
        results["product_usage_image"]["submission_prompt_zh"] = panel_prompt_text
        if usage_geometry_contract:
            results["product_usage_image"]["geometry_contract"] = usage_geometry_contract
        if usage_relation_contract:
            results["product_usage_image"]["physical_relation_contract"] = usage_relation_contract
        results["product_usage_image"]["usage_action_shot_id"] = usage_prompt_shot.get("id")
        results["product_usage_image"]["usage_outcome_context"] = product_usage_outcome_context(
            plan, usage_prompt_shot)
        save_progress()
    if (force_usage or not os.path.isfile(usage_out) or
            os.path.getsize(usage_out) == 0 or not usage_generation_current):
        print("[gpt-image-2] rendering product-in-use detail image as 9 independent panels…", flush=True)
        _begin_manifest_generation(results, "product_usage", force=True)
        panel_tasks = {
            str(item.get("panel_index")): item.get("task_id")
            for item in (results.get("in_progress_panels") or {}).values()
            if item.get("panel_index") and item.get("task_id")
        }
        results["product_usage_image"] = generate_usage_panels(
            api_key, plan, usage_prompt_shot, usage_out, panel_reference_urls,
            model=model, include_human=has_human, force=force_usage,
            resume_tasks=panel_tasks,
            on_progress=lambda index, event: progress_callback(
                "product_usage_image", "usage_panel_%02d" % index,
                os.path.join(os.path.dirname(usage_out), "usage_panels",
                             "panel_%02d.jpg" % index))(
                                 dict(event, panel_index=index)))
        results.pop("in_progress_panels", None)
        results["product_usage_image"]["source_fingerprint"] = usage_fp
        results["product_usage_image"]["board_type"] = "usage_3x3_local_composite"
        results["product_usage_image"]["generation_mode"] = "panelized_local_composite"
        results["product_usage_image"]["usage_policy_version"] = PRODUCT_USAGE_POLICY_VERSION
        if usage_geometry_contract:
            results["product_usage_image"]["geometry_contract"] = usage_geometry_contract
        if usage_relation_contract:
            results["product_usage_image"]["physical_relation_contract"] = usage_relation_contract
        results["product_usage_image"]["description"] = (
            "产品使用九宫格：9 张独立素材由模型分别生成，再由本地无模型拼版；每格保留原始 retrieve URL。")
        results["product_usage_image"]["submission_prompt_zh"] = panel_prompt_text
        results["product_usage_image"]["identity_reference_paths"] = [
            os.path.abspath(p) for p in product_identity_paths]
        results["product_usage_image"]["generation_reference_paths"] = [
            os.path.abspath(p) for p in
            product_identity_paths + ([cast_board_path] if cast_board_path else []) +
            (asset_refs.get("usage_reference_images") or [])]
        results["product_usage_image"]["panel_reference_paths"] = {
            str(index): [os.path.abspath(path) for path in paths]
            for index, paths in panel_reference_paths.items()
        }
        results["product_usage_image"]["panel_reference_policy"] = (
            "每次单格请求仅提交单视图产品锚点和单视图 Mina 锚点；"
            "产品板/人物板原图仅作为已确认来源与溯源元数据，不直接作为单格构图参考。")
        if usage_backup:
            results["product_usage_image"]["forced_regeneration"] = {
                "reason": "missing_remote_url",
                "previous_backup": usage_backup,
                "forced_at": datetime.now().isoformat(timespec="seconds"),
            }
    else:
        results["product_usage_image"] = {
            "url": existing_usage.get("url") or confirmed_usage.get("url", ""),
            "path": usage_out,
            "abspath": os.path.abspath(usage_out), "skipped": True,
            "source_fingerprint": existing_usage.get("source_fingerprint") or usage_fp,
            "board_type": existing_usage.get("board_type") or "usage_3x3",
            "generation_mode": existing_usage.get("generation_mode"),
            "panel_paths": existing_usage.get("panel_paths") or [],
            "panel_urls": existing_usage.get("panel_urls") or [],
            "imageUrls": existing_usage.get("imageUrls") or [],
            "representative_panel_index": existing_usage.get("representative_panel_index"),
            "panels": existing_usage.get("panels") or [],
            "usage_policy_version": (existing_usage.get("usage_policy_version") or
                                     confirmed_usage.get("usage_policy_version") or
                                     PRODUCT_USAGE_POLICY_VERSION),
            "geometry_contract": (existing_usage.get("geometry_contract") or
                                  confirmed_usage.get("geometry_contract") or
                                  usage_geometry_contract),
            "physical_relation_contract": (
                existing_usage.get("physical_relation_contract") or
                confirmed_usage.get("physical_relation_contract") or
                usage_relation_contract),
            "description": existing_usage.get("description") or
            "产品使用九宫格：根据产品板与数字人板（如有）展示真实操作、手部接触和使用结果。",
            "identity_reference_paths": existing_usage.get("identity_reference_paths") or
            confirmed_usage.get("identity_reference_paths") or
            [os.path.abspath(p) for p in product_identity_paths],
            "generation_reference_paths": existing_usage.get("generation_reference_paths"),
        }
        for field in ("result_url", "task_id", "request_id"):
            value = existing_usage.get(field) or confirmed_usage.get(field)
            if value:
                results["product_usage_image"][field] = value


def _render_usage_board_stage(
        *, plan, force_boards, product_approved, cast_approved, out_dir,
        results, asset_refs, api_key, client, model, product_board_urls,
        product_urls, cast_board_urls, portrait_urls, scene_urls, cast_out,
        stage, checkpoint, plan_ref_urls):
    """Generate or resume the panelized product-use geometry gate."""
    save_progress = checkpoint.save
    finish = checkpoint.finish
    progress_callback = checkpoint.progress_callback
    if needs_product_usage_image(plan):
        force_usage = "usage" in force_boards
        has_human = workflow_contract.plan_facts(plan)["has_human"]
        if not product_approved or not cast_approved:
            raise br_client.BRError("GENERATION_BLOCKED: 产品使用图必须基于已确认的产品板和人物板生成。")
        usage_out = os.path.join(out_dir, "product_usage_board.jpg")
        product_board_path = results["product_board"].get("path")
        cast_board_path = (results.get("cast_board") or {}).get("path")
        usage_prompt_shot = _usage_action_shot(plan)
        usage_geometry_contract = product_usage_geometry_contract(plan, usage_prompt_shot)
        usage_relation_contract = product_usage_physical_relation(plan, usage_prompt_shot)
        usage_sources = [path for path in (cast_board_path, product_board_path) if path]
        usage_sources.extend([
            PRODUCT_USAGE_POLICY_VERSION,
            usage_geometry_contract or "usage-geometry-contract:none",
            json.dumps(usage_relation_contract, ensure_ascii=False,
                       sort_keys=True, default=str),
        ])
        usage_fp = _source_refs_fingerprint(usage_sources)
        existing_usage = results.get("product_usage_image") or {}
        confirmed_usage = _approval_record(out_dir, "usage")
        local_usage_approved = (not force_usage and
                                _approval_current(out_dir, "usage", usage_fp))
        usage_approved = _effective_board_approval(
            results, "product_usage", local_usage_approved)
        if stage == "storyboard" and not usage_approved and not force_usage:
            raise br_client.BRError(
                "GENERATION_BLOCKED: 请先展示并确认产品使用图，再生成分段故事板。")
        if (not force_usage and existing_usage.get("source_fingerprint") and
                existing_usage.get("source_fingerprint") != usage_fp and os.path.isfile(usage_out)):
            os.remove(usage_out)
        usage_pose_refs = _collect_image_urls(
            (asset_refs.get("usage_reference_images") or []), api_key,
            fail_on_invalid=True, label="已确认佩戴姿势参考图")
        cast_views = (((results.get("cast_board") or {}).get(
            "reference_pack") or {}).get("views") or {})
        cast_single_view_paths = [
            (cast_views.get(role) or {}).get("path")
            for role in ("face_front", "full_body_front")
            if (cast_views.get(role) or {}).get("path")
        ]
        cast_usage_refs = (_collect_image_urls(
            cast_single_view_paths or portrait_urls, api_key,
            fail_on_invalid=True, label="已确认人物单视图身份锚") if has_human else [])
        product_identity_paths = _confirmed_product_identity_paths(
            plan, results, client=client, limit=2)
        product_usage_refs = _collect_image_urls(
            product_identity_paths,
            api_key, fail_on_invalid=True, label="已确认产品身份图")
        # Keep all three semantic anchors, with product identity first. The
        # product board exists specifically to lock shape before usage imagery;
        # putting cast first lets the model prioritize the person and drift the
        # product geometry in human-product interaction panels.
        usage_refs = _usage_reference_urls(
            product_usage_refs, cast_usage_refs, usage_pose_refs, limit=3)
        # Do not send the contact-sheet boards directly to a single-panel
        # img2img request. Their grid is a review layout and can be copied by
        # the model. Use deterministic single-view derivatives while keeping
        # the original confirmed boards in the source/fingerprint metadata.
        panel_reference_paths = _prepare_usage_panel_reference_paths(
            product_board_path, product_identity_paths, cast_board_path, out_dir,
            include_human=has_human)
        panel_reference_urls = {
            index: _collect_image_urls(
                paths, api_key, fail_on_invalid=True,
                label="产品使用图第%s格参考图" % index)
            for index, paths in panel_reference_paths.items()
        }
        usage_backup = (_backup_existing_board(usage_out, out_dir, "usage")
                        if force_usage else None)
        usage_prompt_text = product_usage_prompt(
            plan, usage_prompt_shot, include_human=has_human)
        panelized_usage = True
        panel_prompt_text = "\n\n".join(
            "[PANEL %d]\n%s" % (
                index, product_usage_panel_prompt(
                    plan, usage_prompt_shot, index, include_human=has_human))
            for index in range(1, 10))
        usage_generation_current = (
            existing_usage.get("generation_mode") == "panelized_local_composite" and
            all(os.path.isfile(path) for path in
                (existing_usage.get("panel_paths") or [])))
        _materialize_usage_board(
            results=results, force_usage=force_usage, usage_out=usage_out,
            usage_fp=usage_fp, usage_backup=usage_backup,
            panel_prompt_text=panel_prompt_text,
            usage_geometry_contract=usage_geometry_contract,
            usage_relation_contract=usage_relation_contract,
            usage_prompt_shot=usage_prompt_shot,
            usage_generation_current=usage_generation_current,
            api_key=api_key, plan=plan,
            panel_reference_urls=panel_reference_urls, model=model,
            has_human=has_human, progress_callback=progress_callback,
            product_identity_paths=product_identity_paths,
            cast_board_path=cast_board_path, asset_refs=asset_refs,
            panel_reference_paths=panel_reference_paths,
            confirmed_usage=confirmed_usage, save_progress=save_progress)
        results["product_usage_image"]["submission_prompt_zh"] = (
            panel_prompt_text if panelized_usage else usage_prompt_text)
        if usage_geometry_contract:
            results["product_usage_image"]["geometry_contract"] = usage_geometry_contract
        if usage_relation_contract:
            results["product_usage_image"]["physical_relation_contract"] = usage_relation_contract
        results["product_usage_image"]["usage_action_shot_id"] = usage_prompt_shot.get("id")
        results["product_usage_image"]["usage_outcome_context"] = product_usage_outcome_context(
            plan, usage_prompt_shot)
        results.pop("in_progress", None)
        local_usage_approved = _approval_current(out_dir, "usage", usage_fp)
        usage_approved = _effective_board_approval(
            results, "product_usage", local_usage_approved)
        results["product_usage_image"]["status"] = "confirmed" if usage_approved else "pending"
        save_progress()
        if stage == "usage" or (stage == "next" and not usage_approved):
            return finish("product_usage_image", needs_confirmation=True), plan_ref_urls
        if stage == "storyboard" and not usage_approved:
            raise br_client.BRError("GENERATION_BLOCKED: 请先展示并确认产品使用图，再生成分段故事板。")
        usage_urls = _collect_image_urls([usage_out], api_key, fail_on_invalid=True,
                                         label="已确认产品使用图")
        plan_ref_urls = _merge_reference_urls(
            usage_urls + cast_board_urls + portrait_urls,
            product_board_urls + product_urls, scene_urls)
        plan["asset_refs"] = dict(
            plan.get("asset_refs") or {}, product_usage_images=[usage_out],
            cast_boards=[], digital_human_portraits=cast_single_view_paths,
            product_boards=[product_board_path] if product_board_urls else [])
        # The plan was canonicalized before the usage board existed. Bind the
        # newly confirmed media atomically now, so every physical-use shot gets
        # both @usage and its matching references[] record before registry
        # validation and no non-use shot inherits the geometry anchor.
        _sync_usage_reference_contract(plan)
    return None, plan_ref_urls


def _prepare_storyboard_render_request(
        plan_path, out_dir, model, run_id, flat, stage, prompt_review,
        only_shot_ids, force_boards, force_shot_ids):
    if stage == "all":
        raise br_client.BRError(
            "STAGE_ALL_BLOCKED: 生产流程禁止一次生成全部素材；请使用 --stage next 逐阶段推进。")
    if model != DEFAULT_MODEL:
        raise br_client.BRError(
            "STORYBOARD_MODEL_REQUIRED: 人物板、产品板和故事板必须使用 %s。" % DEFAULT_MODEL)
    only_shot_ids = set(str(value) for value in (only_shot_ids or []) if value)
    if only_shot_ids and stage != "storyboard":
        raise br_client.BRError("ONLY_SHOT_REQUIRES_STORYBOARD_STAGE")
    force_shot_ids = set(str(value) for value in (force_shot_ids or []) if value)
    if force_shot_ids and stage != "storyboard":
        raise br_client.BRError("FORCE_SHOT_REQUIRES_STORYBOARD_STAGE")
    if force_shot_ids:
        only_shot_ids.update(force_shot_ids)
    force_boards = set(str(value) for value in (force_boards or []) if value)
    unsupported_force = sorted(force_boards - {"cast", "usage"})
    if unsupported_force:
        raise br_client.BRError("UNSUPPORTED_FORCE_BOARD: %s" % ", ".join(unsupported_force))
    if force_boards and stage not in ("cast", "usage", "storyboard", "next"):
        raise br_client.BRError("FORCE_BOARD_STAGE_UNSUPPORTED: %s" % stage)
    plan = load_plan_json(plan_path)
    semantic_resolution = storyboard_semantics.reconcile(
        plan, root=ROOT, plan_path=plan_path)
    if semantic_resolution.get("changed"):
        print(
            "[storyboard] 已按上游导演合同修正 storyboard_role: %s -> %s (%s)" % (
                semantic_resolution.get("previous_role"),
                semantic_resolution.get("role"),
                semantic_resolution.get("source") or "embedded contract"),
            flush=True)
    # Bind the authored plan before loading credentials, auditing, or touching
    # paid generation.  This closes the split-brain case where the router
    # inferred document_explainer from a PPT while the manifest still carried
    # generic/default optional-stage flags.
    early_client = plan.get("client") or client_slug(plan)
    early_run_id = run_id or plan.get("run_id")
    early_manifest_path = _discover_run_manifest(
        plan, early_client, early_run_id)
    if early_manifest_path:
        manifest = run_manifest_store.load_manifest(early_manifest_path)
        if isinstance(plan.get("_execution_plan"), dict):
            run_manifest_store.validate_execution_plan_binding(
                manifest, plan_path)
        else:
            expected_revision = int(manifest.get("revision") or 0)
            binding = run_manifest_store.bind_storyboard_plan(manifest, plan_path)
            if binding.get("changed"):
                run_manifest_store.save_manifest(
                    manifest, early_manifest_path, expected_revision=expected_revision)
            if binding.get("script_reapproval_required"):
                raise br_client.BRError(
                    "MANIFEST_PLAN_BOUND_REAPPROVAL_REQUIRED: 已把 storyboard_plan 绑定到当前 Run，"
                    "但旧脚本确认未覆盖这份计划；请展示完整脚本/逐段分镜并重新确认 script，"
                    "之后再生成任何图片。")
    key_setup.ensure_session_id()
    api_key = key_setup.load_key()
    if not api_key:
        raise br_client.BRError(ux.friendly_error("No API key. Run key onboarding first."))
    # The documented workflow writes the review beside the plan. Discover that
    # artifact when the caller omits the optional flag, while still requiring
    # the exact status and fingerprints below. This preserves the fail-closed
    # gate without making the public command sequence self-contradictory.
    if not prompt_review:
        adjacent_review = os.path.join(
            os.path.dirname(os.path.abspath(plan_path)),
            "storyboard_prompt_review.json",
        )
        if os.path.isfile(adjacent_review):
            prompt_review = adjacent_review
    from storyboard_validator import normalize_plan_motion_elements
    _, migrated_motion = normalize_plan_motion_elements(
        json.loads(json.dumps(plan, ensure_ascii=False)))
    if migrated_motion:
        print("[storyboard] 已将 %d 条文字/图形动效说明移到 motion_elements，" 
              "仅供底片生成后 HyperFrames 使用" % len(migrated_motion), flush=True)
    plan = expand_product_sku_refs(canonical_storyboard_plan(plan))
    schema_validate.enforce(
        plan, "storyboard-plan", context="storyboard.render_storyboard")
    requested_base_out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    # Storyboard/contact sheets are fixed 16:9 review contracts. Final delivery
    # ratio is handled later through output_ratio/render_plan/video segments.
    ratio = STORYBOARD_RATIO
    if run_id and not only_shot_ids and os.path.basename(out_dir) != safe_name(run_id):
        print("[storyboard] plan changed; creating new revision: %s" %
              os.path.basename(out_dir), flush=True)
    return {
        "plan": plan, "api_key": api_key, "prompt_review": prompt_review,
        "only_shot_ids": only_shot_ids, "force_shot_ids": force_shot_ids,
        "force_boards": force_boards,
        "requested_base_out_dir": requested_base_out_dir,
    }


def _prepare_storyboard_source_assets(
        request, *, plan_path, out_dir, run_id, flat, stage):
    plan = request["plan"]
    api_key = request["api_key"]
    prompt_review = request["prompt_review"]
    only_shot_ids = request["only_shot_ids"]
    asset_refs = plan.get("asset_refs") or {}
    import asset_prep
    client = plan.get("client") or client_slug(plan)
    brief = asset_prep._load_brief(client)
    if (brief.get("images") and asset_refs.get("product_images") and
            not asset_refs.get("product_sku")):
        ready_product_paths = [entry.get("path") for entry in (brief.get("images") or [])
                               if isinstance(entry, dict) and
                               asset_prep.is_product_asset_ready(
                                   brief.get("client"), entry.get("path"))]
        # Never let an authored free-form path reintroduce a raw or pending
        # product image when this client has a canonical asset brief. An
        # explicit product_sku is different: expand_product_sku_refs already
        # resolved the registered, formal multi-view product library and those
        # paths must not be erased merely because an older brief also exists.
        asset_refs = dict(asset_refs)
        authored_product_paths = {
            os.path.realpath(path if os.path.isabs(path) else os.path.join(ROOT, path))
            for path in (asset_refs.get("product_images") or []) if isinstance(path, str)
        }
        asset_refs["product_images"] = [path for path in ready_product_paths
                                         if os.path.realpath(path if os.path.isabs(path)
                                                             else os.path.join(ROOT, path))
                                         in authored_product_paths]
        plan["asset_refs"] = asset_refs

    portrait_urls = _collect_image_urls(
        asset_refs.get("digital_human_portraits"), api_key,
        fail_on_invalid=True, label="人物/佩戴参考图")
    product_urls = _collect_image_urls(
        asset_refs.get("product_images"), api_key,
        fail_on_invalid=True, label="产品参考图")
    scene_urls = _collect_image_urls(
        asset_refs.get("scene_images"), api_key,
        fail_on_invalid=True, label="场景参考图")
    # Collect/validate references before the user-prompt gate so diagnostics can
    # still show expanded SKU assets without allowing any model submission.
    # A document explainer's final design-board stage is rendered entirely by
    # Remotion/HyperFrames after all required identity boards are approved. It
    # submits no image prompt, so an image-model prompt review is not applicable
    # at this point. Earlier paid board stages remain fail-closed.
    deterministic_design_board = False
    if stage in ("next", "storyboard") and _looks_like_design_board(plan):
        import video_use_bridge as _video_use_bridge
        if _video_use_bridge._is_document_explainer_plan(plan):
            manifest_path = _discover_run_manifest(plan, client, run_id or plan.get("run_id"))
            if manifest_path:
                manifest = run_manifest_store.load_manifest(manifest_path)
                cast_ready = (not bool(plan.get("characters")) or
                              run_manifest_store.approval_is_current(manifest, "cast_board"))
                product_ready = (not bool(manifest.get("requires_product_board")) or
                                 run_manifest_store.approval_is_current(manifest, "product_board"))
                deterministic_design_board = bool(cast_ready and product_ready)
    if not deterministic_design_board:
        _load_prompt_review_for_shots(prompt_review, plan)

    # Resolve output dir AFTER prompt_review injects approved_prompt_zh, so the
    # fingerprint compared against the previous run matches the stored one.
    if only_shot_ids and run_id and not flat:
        out_dir = os.path.join(out_dir, safe_name(run_id))
    else:
        out_dir = resolve_run_output_dir(out_dir, plan, run_id=run_id, flat=flat)
    os.makedirs(out_dir, exist_ok=True)
    if run_id:
        print("[storyboard] resolved output dir: %s" %
              os.path.abspath(out_dir), flush=True)
    return {
        "out_dir": out_dir, "asset_refs": asset_refs, "client": client,
        "brief": brief, "portrait_urls": portrait_urls,
        "product_urls": product_urls, "scene_urls": scene_urls,
    }


def _prepare_storyboard_run_state(
        request, sources, *, plan_path, run_id, model, stage):
    plan = request["plan"]
    only_shot_ids = request["only_shot_ids"]
    requested_base_out_dir = request["requested_base_out_dir"]
    out_dir = sources["out_dir"]
    portrait_urls = sources["portrait_urls"]
    product_urls = sources["product_urls"]
    scene_urls = sources["scene_urls"]
    from storyboard_validator import validate_plan
    validation_plan = plan
    # @usage is a real dependency once a confirmed usage board exists. During
    # the usage stage itself, however, canonicalization already knows that a
    # physical-use shot will need @usage even though that asset is precisely
    # what this invocation is about to generate. Validate the current stage
    # without that not-yet-created registry entry, while leaving the canonical
    # plan unchanged for generation and later handoff.
    if (stage in ("product", "cast", "usage") and
            not (plan.get("asset_refs") or {}).get("product_usage_images")):
        validation_plan = json.loads(json.dumps(plan, ensure_ascii=False))
        for validation_shot in validation_plan.get("shots") or []:
            tags = validation_shot.get("ref_tags") or []
            if isinstance(tags, str):
                tags = [tags]
            validation_shot["ref_tags"] = [tag for tag in tags if tag != "@usage"]
    validation = validate_plan(validation_plan)
    plan_audit, plan_audit_json, plan_audit_md = storyboard_plan_audit.write_audit(
        plan, validation, out_dir)
    if not validation["ok"]:
        raise br_client.BRError(
            "故事板计划未通过生成前校验：%s。审计报告：%s" %
            ("；".join(validation["errors"]), plan_audit_md)
        )

    # 合并参考图（去重，最多 4 张）：人物 > 产品 > 场景
    usage_urls = []
    product_board_urls = []
    cast_board_urls = []
    plan_ref_urls = _merge_reference_urls(portrait_urls, product_urls, scene_urls)

    result_path = os.path.join(out_dir, "storyboard_result.json")
    current_plan_fingerprint = plan_fingerprint(plan)
    current_visual_plan_fingerprint = visual_plan_fingerprint(plan)
    results = {"ok": True, "model": model, "plan_fingerprint": current_plan_fingerprint,
               "client": plan.get("client") or client_slug(plan),
               "run_id": safe_name(run_id) if run_id else os.path.basename(out_dir),
               "out_dir": os.path.abspath(out_dir), "shots": [],
               "plan_audit": plan_audit,
               "plan_audit_json": plan_audit_json,
               "plan_audit_md": plan_audit_md,
               "storyboard_base_out_dir": requested_base_out_dir,
               "plan_source": os.path.abspath(plan_path),
               "plan_title": plan.get("project_title") or "",
               "expected_shot_ids": [str(shot.get("id") or index)
                                     for index, shot in enumerate(plan.get("shots") or [], 1)]}
    if os.path.isfile(result_path):
        try:
            previous = json.loads(Path(result_path).read_text(encoding="utf-8"))
            same_plan = (not previous.get("plan_fingerprint") or
                         previous.get("plan_fingerprint") == results["plan_fingerprint"])
            if previous.get("model") == model and same_plan:
                for key in (
                        "cast_board", "product_board", "product_usage_image", "shots",
                        "stage", "needs_confirmation", "in_progress",
                        "reference_registry", "storyboard_role", "storyboard_usage",
                        "video_model_usage", "composite_plan", "composite_plan_path",
                        "manifest_path", "manifest_binding"):
                    if key in previous:
                        results[key] = previous[key]
                print("[storyboard] resuming existing result: %s" % result_path, flush=True)
            elif previous.get("model") == model:
                print("[storyboard] checkpoint belongs to a different plan; starting clean", flush=True)
        except (OSError, ValueError):
            pass
    manifest_path = _discover_run_manifest(
        plan, results.get("client"), results.get("run_id"))
    if manifest_path:
        manifest = run_manifest_store.load_manifest(manifest_path)
        _validate_manifest_identity(results, manifest)
        results["manifest_path"] = os.path.abspath(manifest_path)
        results["manifest_binding"] = {
            "status": "bound",
            "manifest_path": os.path.abspath(manifest_path),
            "client": manifest.get("client"),
            "run_id": manifest.get("run_id"),
            "revision": int(manifest.get("revision") or 0),
        }
    # Never replace the last meaningful stage with the ambiguous word
    # ``started``.  Monitoring and recovery use this pointer as their first
    # cheap read while a long image task is in flight.
    requested_pointer_stage = {
        "product": "product_board",
        "cast": "cast_board",
        "usage": "product_usage_image",
        "storyboard": "storyboard",
    }.get(stage)
    pointer_stage = results.get("stage") or requested_pointer_stage or "storyboard"
    run_pointer = _write_run_pointer(
        requested_base_out_dir, run_id, plan, out_dir, result_path,
        stage=pointer_stage,
        plan_fingerprint_value=current_plan_fingerprint,
        visual_plan_fingerprint_value=current_visual_plan_fingerprint)
    if run_pointer:
        results["current_run_pointer"] = os.path.abspath(run_pointer)
    if only_shot_ids:
        plan_ids = {str(shot.get("id") or index)
                    for index, shot in enumerate(plan.get("shots") or [], 1)}
        unknown = sorted(only_shot_ids - plan_ids)
        if unknown:
            raise br_client.BRError("ONLY_SHOT_UNKNOWN: %s" % ", ".join(unknown))
        # A stale in-progress task for another shot must not be resumed when
        # the operator explicitly asked to regenerate only one shot.
        state = results.get("in_progress") or {}
        if state.get("kind") == "storyboard" and state.get("id") not in only_shot_ids:
            results.pop("in_progress", None)

    checkpoint = _StoryboardCheckpoint(
        results, result_path,
        requested_base_out_dir=requested_base_out_dir,
        run_id=run_id,
        plan=plan,
        out_dir=out_dir,
        plan_fingerprint_value=current_plan_fingerprint,
        visual_plan_fingerprint_value=current_visual_plan_fingerprint,
    )
    save_progress = checkpoint.save
    finish = checkpoint.finish
    progress_callback = checkpoint.progress_callback
    previous_task = checkpoint.previous_task

    print("[storyboard] staged generation: confirmed source assets → product board → cast board → product-in-use image → shot storyboards", flush=True)
    return {
        "results": results, "result_path": result_path,
        "current_plan_fingerprint": current_plan_fingerprint,
        "checkpoint": checkpoint, "plan_ref_urls": plan_ref_urls,
        "product_board_urls": product_board_urls,
        "cast_board_urls": cast_board_urls,
    }


def render_storyboard(plan_path, out_dir, model=DEFAULT_MODEL, run_id=None, flat=False, bw=True,
                      stage="next", debug_allow_all=False, prompt_review=None,
                      only_shot_ids=None, force_boards=None, force_shot_ids=None):
    request = _prepare_storyboard_render_request(
        plan_path, out_dir, model, run_id, flat, stage, prompt_review,
        only_shot_ids, force_boards, force_shot_ids)
    sources = _prepare_storyboard_source_assets(
        request, plan_path=plan_path, out_dir=out_dir, run_id=run_id, flat=flat,
        stage=stage)
    state = _prepare_storyboard_run_state(
        request, sources, plan_path=plan_path, run_id=run_id, model=model,
        stage=stage)
    plan = request["plan"]
    api_key = request["api_key"]
    only_shot_ids = request["only_shot_ids"]
    force_shot_ids = request["force_shot_ids"]
    force_boards = request["force_boards"]
    requested_base_out_dir = request["requested_base_out_dir"]
    out_dir = sources["out_dir"]
    asset_refs = sources["asset_refs"]
    client = sources["client"]
    brief = sources["brief"]
    portrait_urls = sources["portrait_urls"]
    product_urls = sources["product_urls"]
    scene_urls = sources["scene_urls"]
    results = state["results"]
    result_path = state["result_path"]
    current_plan_fingerprint = state["current_plan_fingerprint"]
    checkpoint = state["checkpoint"]
    plan_ref_urls = state["plan_ref_urls"]
    product_board_urls = state["product_board_urls"]
    cast_board_urls = state["cast_board_urls"]
    (completed, product_approved, product_board_urls,
     plan_ref_urls) = _render_product_board_stage(
        plan=plan,
        brief=brief,
        client=client,
        product_urls=product_urls,
        out_dir=out_dir,
        asset_refs=asset_refs,
        api_key=api_key,
        model=model,
        stage=stage,
        results=results,
        checkpoint=checkpoint,
        portrait_urls=portrait_urls,
        scene_urls=scene_urls,
        plan_ref_urls=plan_ref_urls,
    )
    if completed is not None:
        return completed

    (completed, cast_approved, cast_board_urls, cast_out,
     plan_ref_urls) = _render_cast_board_stage(
        plan=plan,
        out_dir=out_dir,
        force_boards=force_boards,
        client=client,
        results=results,
        stage=stage,
        api_key=api_key,
        model=model,
        portrait_urls=portrait_urls,
        product_board_urls=product_board_urls,
        product_urls=product_urls,
        scene_urls=scene_urls,
        checkpoint=checkpoint,
        plan_ref_urls=plan_ref_urls,
    )
    if completed is not None:
        return completed

    completed, plan_ref_urls = _render_usage_board_stage(
        plan=plan,
        force_boards=force_boards,
        product_approved=product_approved,
        cast_approved=cast_approved,
        out_dir=out_dir,
        results=results,
        asset_refs=asset_refs,
        api_key=api_key,
        client=client,
        model=model,
        product_board_urls=product_board_urls,
        product_urls=product_urls,
        cast_board_urls=cast_board_urls,
        portrait_urls=portrait_urls,
        scene_urls=scene_urls,
        cast_out=cast_out,
        stage=stage,
        checkpoint=checkpoint,
        plan_ref_urls=plan_ref_urls,
    )
    if completed is not None:
        return completed

    if stage in ("product", "cast", "usage"):
        return finish("product_usage_image" if stage == "usage" else "%s_board" % stage,
                      needs_confirmation=True)

    return _render_final_storyboard_stage(
        plan=plan,
        plan_path=plan_path,
        out_dir=out_dir,
        run_id=run_id,
        only_shot_ids=only_shot_ids,
        force_shot_ids=force_shot_ids,
        current_plan_fingerprint=current_plan_fingerprint,
        api_key=api_key,
        bw=bw,
        plan_ref_urls=plan_ref_urls,
        model=model,
        results=results,
        result_path=result_path,
        checkpoint=checkpoint,
    )
