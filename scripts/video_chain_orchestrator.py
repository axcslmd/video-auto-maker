#!/usr/bin/env python3
"""Serial continuity-chain orchestration for ``video_engine``.

The public API remains in ``video_engine.render_chained``. This module isolates
the state machine into small phases: preflight, segment preparation,
submit/resume, controlled fallback, media acceptance and continuity advance.
"""
import hashlib
import json
import os


class ChainRunner:
    def __init__(self, service, segments, options):
        self.s = service
        self.options = options
        self.segments = self._with_cli_overrides(segments)
        self.results = []
        self.api_key = None
        self.default_model = None
        self.lease_owner = None
        self.resume_by_segment = {}
        self.previous_tail_url = None
        self.previous_tail_path = None
        self.previous_video_url = None
        self.previous_result = None
        self.model_catalog = None

    def _with_cli_overrides(self, segments):
        negative = self.options["negative_prompt"]
        seed = self.options["seed"]
        if negative is None and seed is None:
            return list(segments)
        output = []
        for segment in segments:
            updated = dict(segment)
            if negative is not None:
                updated["negative_prompt"] = negative
            if seed is not None:
                updated["seed"] = seed
            output.append(updated)
        return output

    def log(self, message):
        if self.options["verbose"]:
            print(message, flush=True)

    def flush(self):
        path = self.options["results_out"]
        if path:
            self.s._atomic_json_write(path, self.results)
            self.s._write_video_submission_report(
                path, self.results, draft=self.options["draft"])

    def prepare(self):
        draft = self.options["draft"]
        if not draft and self.options["allow_ocr_warning"]:
            raise ValueError(
                "FORMAL_OCR_GLOBAL_WAIVER_BLOCKED: 正式流程必须按 segment/take/OCR texts 登记精确 waiver")
        if not draft and not self.options["allow_model_fallback"]:
            raise ValueError("FORMAL_MODEL_FALLBACK_REQUIRED: 正式流程不得关闭能力感知的模型降级")
        self.s._validate_chain_preflight(
            self.segments, draft=draft, client=self.options["client"],
            manifest=self.options["manifest"], manifest_path=self.options["manifest_path"],
            ledger_path=self.options["ledger_path"], results_out=self.options["results_out"],
            prompt_review=self.options["prompt_review"])
        self.s.key_setup.ensure_session_id()
        self.api_key = self.s.key_setup.load_key()
        if not self.api_key:
            raise self.s.br_client.BRError(
                "No API key. Use the host secure credential input and non-echoing "
                "key onboarding; never send a key in ordinary chat.")
        self.model_catalog = self.s._model_catalog("video")
        self.default_model = self.s._pick_video_model(
            self.options["model"] or self.s.DEFAULT_MODEL,
            allow_fallback=self.options["allow_model_fallback"],
            catalog=self.model_catalog)
        self.proven_audio_models = (
            self.s._rm.approved_video_audio_canary_models(
                self.options["manifest"]) if not draft else ())
        preflight = self.s.inspect_submission_requirements(
            self.segments, self.default_model, formal=True,
            allow_model_fallback=self.options["allow_model_fallback"],
            catalog=self.model_catalog,
            allow_unproven_audio_canary=False,
            proven_audio_models=self.proven_audio_models,
            manifest=self.options["manifest"], formal_stage="video")
        if not preflight["ready"]:
            raise ValueError("VIDEO_PREFLIGHT_BLOCKED: %s" %
                             json.dumps(preflight["blockers"], ensure_ascii=False))
        if not draft:
            self.s._rm.validate_video_audio_canary(
                self.options["manifest"], preflight)
        self.s._validate_chain_reference_budget(
            self.segments, preflight, formal=not draft)
        self._validate_handoffs()
        self.s._enforce_batch_budget(
            self.segments, self.default_model, self.options["budget_cap"], self.log,
            manifest=self.options["manifest"], preflight_ready=True, strict=not draft)
        if not draft:
            self.lease_owner = self.s._video_generation_owner_token()
            acquired = self.s._rm.acquire_generation_lease(
                self.options["manifest_path"], "video", self.lease_owner,
                ttl_seconds=300)
            self.options["manifest"].clear()
            self.options["manifest"].update(acquired)
        self.resume_by_segment = self.s._load_chain_resume_results(
            self.options["results_out"], draft=draft)

    def _validate_handoffs(self):
        stale = []
        for segment in self.segments:
            handoff = self.s.artifact_contract.verify_video_handoff(segment)
            if handoff["strict"] and not handoff["ok"]:
                stale.append(segment.get("id", "?"))
        if stale:
            raise ValueError("STALE_VIDEO_HANDOFF: 串联段生成契约已变化，请重新 split：%s" %
                             ", ".join(stale))

    def _storyboard_refs(self, segment, target_model, tail_url=None):
        prepared, prepared_urls, panel = self.s._prepare_storyboard_submission_for_model(
            self.api_key, segment, target_model, formal=not self.options["draft"])
        urls = list(prepared_urls or [])
        if tail_url:
            if len(urls) + 1 > 4:
                raise ValueError(
                    "REFERENCE_COUNT_UNSUPPORTED: segment %s 的尾帧、展开格和已确认 ref_tags "
                    "超过模型上限；不允许静默丢弃身份或产品锚。" %
                    (segment.get("id") or "unknown"))
            urls = [tail_url] + urls
            prepared["_chain_tail_frame"] = True
        refs = [self.s._video_image_ref(
            value, api_key=self.api_key, allow_local_draft=self.options["draft"])
                for value in urls] if urls else None
        video_type = 5 if refs and len(refs) > 1 else (
            2 if refs else prepared.get("video_type", 1))
        return prepared, refs, panel, video_type

    def _heartbeat(self):
        if not self.options["draft"]:
            self.s._heartbeat_formal_generation(
                self.options["manifest"], self.options["manifest_path"],
                "video", self.lease_owner)

    def _dependency(self, index, segment):
        extend_url = (self.previous_video_url
                      if index > 0 and segment.get("extend_from_previous") else None)
        dependency = (self.s.artifact_contract.build_generation_dependency(
            self.previous_result, tail_path=self.previous_tail_path,
            extend_source=extend_url) if index > 0 else None)
        if index > 0:
            declared = (segment.get("chain_contract") or {}).get("predecessor_segment_id")
            actual = (self.previous_result or {}).get("segment_id")
            if declared and str(declared) != str(actual):
                raise ValueError("CHAIN_PREDECESSOR_MISMATCH: %s != %s" % (declared, actual))
        return extend_url, dependency

    def _task_candidates(self, segment, handoff, model, dependency):
        if self.options["manifest"] is None:
            return None, None, None
        fingerprint = (dependency or {}).get("fingerprint")
        manifest = self.options["manifest"]
        resumed = self.s._rm.find_resumable_task(
            manifest, "video", segment.get("id"), handoff,
            model=model, dependency_fingerprint=fingerprint)
        intent = self.s._rm.find_submission_intent(
            manifest, "video", segment.get("id"), handoff,
            model=model, dependency_fingerprint=fingerprint)
        completed = self.s._completed_task(
            manifest, segment.get("id"), handoff,
            model=model, dependency_fingerprint=fingerprint)
        return resumed, intent, completed

    def _prior_is_reusable(self, prior, segment, attempt, model, dependency):
        if not (self.options["draft"] and prior and prior.get("ok") and
                prior.get("localPath") and os.path.isfile(prior.get("localPath"))):
            return False
        handoff = (segment.get("video_handoff_fingerprint") or
                   self.s.artifact_contract.build_video_handoff(segment)["fingerprint"])
        return bool(
            prior.get("video_handoff_fingerprint") == handoff and
            prior.get("attempt") == attempt and
            self.s._rm.task_model_matches(prior, model) and
            self.s._result_dependency_fingerprint(prior) ==
            (dependency or {}).get("fingerprint"))

    def _reuse_prior(self, prior, index):
        self.log("[chain %d/%d] 续跑：复用已完成结果 %s（%s），跳过重新生成" %
                 (index + 1, len(self.segments), prior.get("segment_id"),
                  prior.get("localPath")))
        self.results.append(dict(
            prior, **self.s._submission_prompt_snapshot_fields(source=prior)))
        self.previous_result = prior
        self.previous_video_url = prior.get("videoUrl")
        if index < len(self.segments) - 1:
            tail = self.s._extract_last_frame(prior["localPath"], self.log)
            self.previous_tail_path = tail
            self.previous_tail_url = (self.s._video_image_ref(
                tail, api_key=self.api_key, allow_local_draft=self.options["draft"])
                                      if tail else None)
        else:
            self.previous_tail_path = self.previous_tail_url = None
        self.flush()

    def _prepare_segment(self, index, authored):
        self._heartbeat()
        attempt = self.s._current_attempt(self.options["manifest"], authored.get("id"))
        handoff = (authored.get("video_handoff_fingerprint") or
                   self.s.artifact_contract.build_video_handoff(authored)["fingerprint"])
        (segment, source, storyboard_ref, storyboard_tail, refs,
         video_type) = self.s._prepare_chain_references(
            authored, index=index, previous_tail_url=self.previous_tail_url,
            api_key=self.api_key, client=self.options["client"],
            draft=self.options["draft"], log=self.log)
        pick = {"video_type": video_type}
        if not self.options["draft"]:
            pick.update(
                dialogue=self.s._segment_integrated_audio_requirement(segment),
                formal=True, proven_audio_models=self.proven_audio_models)
        model = self.s._pick_video_model(
            segment.get("model") or self.default_model,
            allow_fallback=self.options["allow_model_fallback"],
            reference_count=len(refs or []), catalog=self.model_catalog,
            **pick)
        extend_url, dependency = self._dependency(index, segment)
        resumed, intent, completed = self._task_candidates(
            segment, handoff, model, dependency)
        prior = self.resume_by_segment.get(authored.get("id"))
        if self._prior_is_reusable(prior, segment, attempt, model, dependency):
            self._reuse_prior(prior, index)
            return None
        panel = None
        if storyboard_ref and not (completed or resumed):
            segment, refs, panel, video_type = self._storyboard_refs(
                segment, model, tail_url=storyboard_tail)
        if not self.options["draft"] and not (completed or resumed):
            self.s._model_request_capability_evidence(
                model, dict(segment, video_type=video_type),
                self.model_catalog)
        self.s._validate_model_reference_capacity(
            model, len(refs or []), formal=not self.options["draft"])
        segment["product_identity_lock"] = bool(
            segment.get("product_identity_lock") or segment.get("product_sku"))
        segment["character_identity_lock"] = bool(
            segment.get("character_identity_lock") or segment.get("actor"))
        source["product_identity_lock"] = segment["product_identity_lock"]
        source["character_identity_lock"] = segment["character_identity_lock"]
        negative = segment.get("negative_prompt", self.options["effective_negative"])
        if storyboard_ref:
            negative = self.s._storyboard_negative(negative)
        self._log_segment(index, segment, model, video_type, extend_url, refs)
        return {
            "index": index, "segment": segment, "source": source,
            "storyboard_ref": storyboard_ref, "storyboard_tail": storyboard_tail,
            "refs": refs, "video_type": video_type, "model": model,
            "initial_model": model, "extend_url": extend_url,
            "dependency": dependency, "handoff": handoff, "attempt": attempt,
            "resumed": resumed, "intent": intent, "completed": completed,
            "panel": panel, "negative": negative, "task": None, "task_id": None,
            "request_id": None, "fallback_reason": None,
        }

    def _log_segment(self, index, segment, model, video_type, extend_url, refs):
        chained = (index > 0 and video_type in (2, 4, 5) and self.previous_tail_url and
                   (not segment.get("urls") or segment.get("_locked_urls")))
        mode = "(视频延长)" if index > 0 and segment.get("extend_video") and extend_url else (
            "(尾帧串联)" if chained else "")
        self.log("[chain %d/%d] model=%s type=%s %s" %
                 (index + 1, len(self.segments), model, video_type, mode))
        self.log("[体验提示] 正在处理第 %d/%d 段；串联模式按顺序生成，每段通常需要 1–3 分钟。" %
                 (index + 1, len(self.segments)))
        if extend_url:
            self.log("[chain %d] 使用视频延长，新增时长=%ss" %
                     (index + 1, segment.get("duration", 5)))

    def _tick(self, context):
        def callback(status, waited):
            self._heartbeat()
            self.log("[chain %d poll] %ss %s" %
                     (context["index"] + 1, waited, status))
        return callback

    def _task_record(self, context, submission_text):
        dependency = context["dependency"] or {}
        return {
            "segment": context["segment"], "task_id": context["task_id"],
            "model": context["model"], "handoff_fingerprint": context["handoff"],
            "request_id": context["request_id"], "attempt": context["attempt"],
            "dependency_fingerprint": dependency.get("fingerprint"),
            "generation_dependency": dependency.get("payload"),
            "storyboard_panel": context["panel"], "source_segment": context["source"],
            "submission_prompt_zh": submission_text,
            "submission_prompt_sha256": hashlib.sha256(
                submission_text.encode("utf-8")).hexdigest(),
            "submission_prompt_length": len(submission_text),
            "submission_prompt_storyboard_ref": bool(context["storyboard_ref"]),
            "submission_prompt_extend_url": context["extend_url"],
        }

    def _apply_model_fallback(self, context, submit_error):
        if (not self.options["allow_model_fallback"] or
                not self.s.br_client.is_video_model_not_found(submit_error)):
            raise submit_error
        failed = context["model"]
        fallback = self.s._pick_video_model(
            video_type=context["video_type"],
            dialogue=self.s._segment_integrated_audio_requirement(
                context["segment"]),
            formal=not self.options["draft"], allow_fallback=True,
            reference_count=len(context["refs"] or []), exclude_models=[failed],
            proven_audio_models=self.proven_audio_models,
            catalog=self.model_catalog)
        if not self.options["draft"]:
            self.s._assert_confirmed_submission_for_model(context["segment"], fallback)
        segment, refs, panel, video_type = (
            context["segment"], context["refs"], context["panel"], context["video_type"])
        if context["storyboard_ref"]:
            try:
                segment, refs, panel, video_type = self._storyboard_refs(
                    context["source"], fallback, tail_url=context["storyboard_tail"])
            except (self.s.br_client.BRError, ValueError) as exc:
                raise ValueError(
                    "FALLBACK_APPROVAL_REQUIRED: %s 不可用，切换 %s 前必须完成参考图审批并"
                    "重新确认；%s" % (failed, fallback, exc)) from exc
        if not self.options["draft"]:
            self.s._model_request_capability_evidence(
                fallback, dict(segment, video_type=video_type),
                self.model_catalog)
        self.s._validate_model_reference_capacity(
            fallback, len(refs or []), formal=not self.options["draft"])
        context.update(segment=segment, refs=refs, panel=panel, video_type=video_type,
                       model=fallback, fallback_reason="model_not_found")
        context["attempt"] += 1
        return failed

    def _submit_new(self, context):
        segment = context["segment"]
        text = self.s._submission_text(
            segment, context["model"], context["storyboard_ref"], context["extend_url"])
        dependency_fp = (context["dependency"] or {}).get("fingerprint")
        context["request_id"] = ((context["intent"] or {}).get("request_id") or
                                 self.s._persist_submission_intent(
            self.options["manifest"], self.options["manifest_path"],
            self.options["ledger_path"], segment, context["model"],
            context["video_type"], context["handoff"], attempt=context["attempt"],
            dependency_fingerprint=dependency_fp))
        self._persist_prompt(context, text)
        try:
            context["task_id"], _ = self.s._submit_video(
                self.api_key, segment, context["model"], context["video_type"],
                context["refs"], context["negative"], seed=segment.get("seed"),
                storyboard_ref=context["storyboard_ref"], extend_url=context["extend_url"],
                request_id=context["request_id"], submission_text=text)
        except self.s.br_client.BRVideoReferencePrivacyError:
            raise
        except self.s.br_client.BRError as exc:
            failed = self._apply_model_fallback(context, exc)
            text = self.s._submission_text(
                context["segment"], context["model"], context["storyboard_ref"],
                context["extend_url"])
            context["request_id"] = self.s._persist_submission_intent(
                self.options["manifest"], self.options["manifest_path"],
                self.options["ledger_path"], context["segment"], context["model"],
                context["video_type"], context["handoff"], attempt=context["attempt"],
                dependency_fingerprint=dependency_fp)
            self._persist_prompt(context, text)
            context["task_id"], _ = self.s._submit_video(
                self.api_key, context["segment"], context["model"], context["video_type"],
                context["refs"], context["negative"],
                seed=context["segment"].get("seed"),
                storyboard_ref=context["storyboard_ref"], extend_url=context["extend_url"],
                request_id=context["request_id"], submission_text=text)
            self.log("[chain %d model-fallback] %s 不可用，已按确认合同改用 %s 重试一次" %
                     (context["index"] + 1, failed, context["model"]))
        context["task"] = self._task_record(context, text)
        self.s._persist_task(
            self.options["manifest"], self.options["manifest_path"],
            self.options["ledger_path"], context["task"], "submitted",
            request_id=context["request_id"])

    def _persist_prompt(self, context, text):
        self.s._persist_submission_prompt_snapshot(
            self.options["manifest"], self.options["manifest_path"],
            self.options["ledger_path"], context["segment"], context["model"],
            context["video_type"], context["handoff"], attempt=context["attempt"],
            dependency_fingerprint=(context["dependency"] or {}).get("fingerprint"),
            request_id=context["request_id"], submission_text=text,
            storyboard_ref=context["storyboard_ref"], extend_url=context["extend_url"])

    def _resume_or_submit(self, context):
        completed, resumed = context["completed"], context["resumed"]
        if completed:
            context["task_id"] = completed["task_id"]
            context["task"] = dict(completed)
            context["task"].update(
                self.s._submission_prompt_snapshot_fields(source=completed))
            self.log("[chain %d resume-download] taskId=%s" %
                     (context["index"] + 1, context["task_id"]))
        elif resumed:
            context["task_id"] = resumed["task_id"]
            context["model"] = resumed.get("model") or context["model"]
            context["task"] = dict(resumed)
            context["task"].update(self.s._submission_prompt_snapshot_fields(source=resumed))
            self.log("[chain %d resume] taskId=%s" %
                     (context["index"] + 1, context["task_id"]))
            self.s._record_task_resume(
                self.options["manifest"], self.options["manifest_path"],
                self.options["ledger_path"], {
                    "segment": context["segment"], "task_id": context["task_id"],
                    "model": context["model"], "handoff_fingerprint": context["handoff"],
                    "request_id": resumed.get("request_id"),
                    "dependency_fingerprint": (context["dependency"] or {}).get("fingerprint")
                                              or resumed.get("dependency_fingerprint")})
        else:
            context["attempt"] = self.s._current_attempt(
                self.options["manifest"], context["segment"].get("id"))
            self._submit_new(context)
        return self.s.br_client.wait_video(
            self.api_key, context["task_id"], interval=8,
            max_wait=self.options["max_wait"], on_tick=self._tick(context))

    def _privacy_fallback_block(
            self, context, fallback_task, error, *, provider_error,
            fallback_target_model=None, preparation_error=None):
        task_id = fallback_task.get("task_id")
        action = self.s._privacy_fallback_recovery_action(
            context["segment"], fallback_target_model,
            task_exists=bool(task_id))
        self.s._persist_task(
            self.options["manifest"], self.options["manifest_path"],
            self.options["ledger_path"], fallback_task, "fallback_approval_required",
            error=error, provider_error=provider_error,
            fallback_preparation_error=(str(preparation_error)[:1000]
                                        if preparation_error else None),
            fallback_target_model=fallback_target_model,
            billing_status="unknown")
        self.results.append({
            "ok": False, "videoUrl": None, "localPath": None, "absPath": None,
            "error": error, "taskId": task_id,
            "segment_id": context["segment"].get("id"), "needs_confirmation": True,
            "resume_available": bool(task_id), "delivery_blocked": True,
            "chain_aborted": True, "chain_abort_reason": "fallback_approval_required",
            "provider_error": provider_error,
            "fallback_preparation_error": (str(preparation_error)[:1000]
                                            if preparation_error else None),
            "fallback_target_model": fallback_target_model,
            "billing_status": "unknown", "action": action,
            **self.s._submission_prompt_snapshot_fields(source=fallback_task),
        })
        for remaining in self.segments[context["index"] + 1:]:
            self.results.append({
                "ok": False, "videoUrl": None, "localPath": None, "absPath": None,
                "segment_id": remaining.get("id"),
                "error": "CHAIN_ABORTED: predecessor fallback approval required",
                "chain_aborted": True,
                "chain_abort_reason": "fallback_approval_required"})
        self.flush()
        if not self.options["draft"] and self.options["manifest_path"]:
            closed = self.s._rm.record_generation_blocked_if_running(
                self.options["manifest_path"], "video", error,
                blocker_code="PRIVACY_FALLBACK_REAPPROVAL_REQUIRED",
                recovery_action=action,
                diagnostics=[self.options["results_out"]]
                if self.options["results_out"] else (),
                evidence={
                    "segment_id": context["segment"].get("id"),
                    "task_id": task_id, "attempt": fallback_task.get("attempt"),
                    "provider_error": provider_error,
                    "fallback_target_model": fallback_target_model,
                    "billing_status": "unknown",
                })
            self.options["manifest"].clear()
            self.options["manifest"].update(closed)

    def _privacy_fallback(self, context, provider_failure):
        if not self.s._privacy_fallback_allowed(
                context["model"], context["video_type"], context["refs"],
                self.options["allow_model_fallback"],
                segment=context["segment"]):
            raise self.s.br_client.BRVideoReferencePrivacyError(
                "REFERENCE_REAL_PERSON_PRIVACY")
        existing = context["task"] or context["resumed"] or context["completed"] or {}
        old_task_id = context["task_id"] or existing.get("task_id")
        old_attempt = int(existing.get("attempt") or context["attempt"] or 1)
        fallback_task = dict(existing)
        fallback_task.update({
            "segment": context["segment"], "source_segment": context["source"],
            "task_id": old_task_id, "model": context["model"],
            "video_type": context["video_type"], "ref_urls": list(context["refs"] or []),
            "storyboard_ref": context["storyboard_ref"], "storyboard_panel": context["panel"],
            "negative_prompt": context["negative"], "seed": context["segment"].get("seed"),
            "handoff_fingerprint": context["handoff"], "attempt": old_attempt,
        })
        provider_error = self.s._provider_error_evidence(
            provider_failure, model=context["model"], task_id=old_task_id)
        try:
            fallback = self.s._prepare_privacy_fallback_submission(
                self.api_key, fallback_task, context["source"],
                draft=self.options["draft"])
        except (self.s.br_client.BRError, ValueError) as exc:
            self._privacy_fallback_block(
                context, fallback_task, "FALLBACK_APPROVAL_REQUIRED: %s" % exc,
                provider_error=provider_error, preparation_error=exc)
            return None
        target = fallback["model"]
        self._privacy_fallback_block(
            context, fallback_task,
            "FALLBACK_APPROVAL_REQUIRED: provider privacy rejection requires a new "
            "model/ref contract and customer-confirmed prompt packet",
            provider_error=provider_error, fallback_target_model=target)
        return None

    def _obtain_url(self, context):
        try:
            url = self._resume_or_submit(context)
            if not url:
                raise self.s.br_client.BRError("COMPLETED_WITHOUT_URL")
            return url
        except self.s.br_client.BRVideoReferencePrivacyError as exc:
            return self._privacy_fallback(context, exc)

    def _download(self, context, url):
        path = context["segment"].get("out_path")
        if not path:
            return None, True
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            self.s._download_completed_video_guarded(
                self.api_key, context["task_id"], url, path,
                segment=context["segment"], task=context.get("task"))
            return path, True
        except (self.s.br_client.BRError, OSError, ValueError) as exc:
            self.results.append({
                "ok": False, "videoUrl": url, "localPath": None, "absPath": None,
                "error": "DOWNLOAD_PENDING: %s" % exc, "taskId": context["task_id"],
                "segment_id": context["segment"].get("id"), "resume_available": True,
                **self.s._submission_prompt_snapshot_fields(source=context["task"]),
            })
            self.previous_tail_url = None
            self.flush()
            return None, False

    def _accept_media(self, context, url):
        task = dict(context["task"] or {})
        task.update({
            "segment": context["segment"], "task_id": context["task_id"],
            "model": context["model"], "handoff_fingerprint": context["handoff"],
            "attempt": task.get("attempt", context["attempt"]),
            "dependency_fingerprint": (context["dependency"] or {}).get("fingerprint"),
            "generation_dependency": (context["dependency"] or {}).get("payload"),
            "storyboard_panel": context["panel"],
            "fallback_reason": context["fallback_reason"],
        })
        context["task"] = task
        self.s._persist_task(
            self.options["manifest"], self.options["manifest_path"],
            self.options["ledger_path"], task, "succeeded", video_url=url)
        local, downloaded = self._download(context, url)
        if not downloaded:
            return False
        qc_task = dict(task, video_url=url)
        qc = (self.s._media_qc_guard(
            local, context["segment"], draft=self.options["draft"],
            manifest=self.options["manifest"], manifest_path=self.options["manifest_path"],
            task=qc_task, raise_on_failure=False) if local else None)
        if qc and not qc.get("passed"):
            self.results.append(self.s._build_media_qc_failure_result(
                url=url, local=local, qc=qc,
                task_id=context["task_id"], segment=context["segment"],
                task=task, initial_model=context["initial_model"],
                selected_model=context["model"],
                fallback_reason=context["fallback_reason"]))
            self.previous_tail_url = None
            self.flush()
            return False
        ocr = self.s._ocr_guard(local, self.log) if local else None
        result = self.s._build_chain_success_result(
            url=url, local=local, ocr=ocr, task_id=context["task_id"],
            segment=context["segment"], qc=qc, task=task, attempt=context["attempt"],
            initial_model=context["initial_model"], selected_model=context["model"],
            dependency=context["dependency"], fallback_reason=context["fallback_reason"])
        self.results.append(result)
        self.previous_result = result
        self.previous_video_url = url
        self.flush()
        if context["index"] < len(self.segments) - 1:
            self.previous_tail_path, self.previous_tail_url = self.s._next_chain_tail(
                local=local, api_key=self.api_key, task_id=context["task_id"],
                video_url=url, draft=self.options["draft"], log=self.log)
        return True

    def _abort_remaining(self, index, reason):
        for remaining in self.segments[index + 1:]:
            self.results.append({
                "ok": False, "videoUrl": None, "localPath": None,
                "absPath": None, "segment_id": remaining.get("id"),
                "error": "CHAIN_ABORTED: %s" % reason,
                "chain_aborted": True,
                "chain_abort_reason": "predecessor_result_failed",
            })
        self.flush()

    def _settle(self):
        if self.options["draft"] or not self.options["manifest_path"]:
            return
        current = self.s._rm.load_manifest(self.options["manifest_path"])
        status = ((current.get("generation") or {}).get("video") or {}).get(
            "status")
        if status != "running":
            self.options["manifest"].clear()
            self.options["manifest"].update(current)
            return
        closed, _settlement = self.s._rm.record_video_generation_settlement(
            self.options["manifest_path"], self.options["results_out"],
            self.results)
        self.options["manifest"].clear()
        self.options["manifest"].update(closed)

    def _abort(self, index, segment, task, error):
        self.log("[chain %d] FAILED: %s" % (index + 1, error))
        self.results.append({
            "ok": False, "videoUrl": None, "localPath": None, "absPath": None,
            "error": str(error), "segment_id": segment.get("id"),
            "chain_aborted": True, "chain_abort_reason": "predecessor_failed",
            **self.s._submission_prompt_snapshot_fields(source=task),
        })
        for remaining in self.segments[index + 1:]:
            self.results.append({
                "ok": False, "videoUrl": None, "localPath": None, "absPath": None,
                "segment_id": remaining.get("id"),
                "error": "CHAIN_ABORTED: predecessor segment failed",
                "chain_aborted": True})
        self.flush()

    def run(self):
        self.prepare()
        for index, authored in enumerate(self.segments):
            context = None
            try:
                context = self._prepare_segment(index, authored)
                if context is None:
                    continue
                url = self._obtain_url(context)
                if url is None:
                    return self.results
                self.previous_video_url = url
                if not self._accept_media(context, url):
                    self._abort_remaining(
                        index, "predecessor media recovery or QC required")
                    break
            except (self.s.br_client.BRError, ValueError) as exc:
                self._abort(index, authored, (context or {}).get("task"), exc)
                break
        for result in self.results:
            if (result and result.get("ocr_warning") and
                    not self.options["allow_ocr_warning"]):
                result["delivery_blocked"] = True
                result["needs_confirmation"] = True
        self.flush()
        self._settle()
        return self.results


def render_chained(service, segments, **options):
    options["effective_negative"] = (
        service.DEFAULT_NEGATIVE if options["negative_prompt"] is None
        else options["negative_prompt"])
    return ChainRunner(service, segments, options).run()
