#!/usr/bin/env python3
"""Build and enforce bounded, model-neutral stage interaction contracts."""
import argparse
import copy
import datetime
import hashlib
import json
import os

from model_behavior import conservative_profile, interaction_policy, load_profile
from project_utils import FileLock, atomic_write_json
from schema_validate import enforce, validate as validate_schema


PACKET_VERSION = "1.0"
PROTECTED_PATHS = (
    "/approvals", "/approval_evidence", "/approval_hashes",
    "/customer_confirmation", "/confirmed_by_customer", "/approved",
    "/stage_complete", "/generation", "/tasks", "/task_id",
    "/board_sha256", "/source_fingerprint", "/artifact_sha256",
    "/ocr_waiver", "/review_presentations", "/client", "/run_id", "/revision",
)
CUSTOMER_CONFIRM_STAGES = {
    "brief", "script", "styleframe", "audio", "product_board", "cast_board",
    "product_usage", "storyboard", "render_plan", "video", "captions", "final",
    "derive", "identity_plate_reference", "identity_plate_submission_capture",
    "identity_plate_review",
}


_STAGE_SPECS = {
    "idea": {
        "objective": "把客户的一句话需求收敛为可路由的创作意图",
        "question_id": "idea.intent",
        "editable_paths": ["/idea", "/brand_slug_proposal", "/material_summary"],
        "criteria": ["明确服务品牌", "明确视频类型或保留待分诊状态"],
    },
    "brief": {
        "objective": "确认受众、目标、唯一主卖点、渠道、语气和 CTA",
        "question_id": "brief.primary_usp",
        "editable_paths": [
            "/profile", "/package", "/objective", "/audience", "/channel", "/primary_usp",
            "/secondary_points", "/tone", "/cta", "/fact_refs", "/output_ratio",
            "/storyboard_role", "/digital_human",
        ],
        "criteria": ["只有一个主卖点", "事实声明均可追溯", "数字人选择已明确"],
    },
    "script": {
        "objective": "把已确认简报拆成可计时、可溯源、可拍摄的脚本节拍",
        "question_id": "script.beats",
        "editable_paths": ["/beats", "/beats/*"],
        "criteria": ["每个节拍只有一个叙事任务", "台词与时长匹配", "事实均有 fact_refs"],
    },
    "content_scaffold": {
        "objective": "把文档事实映射为真实内容页层级，不保留待填占位",
        "question_id": "content.scenes",
        "editable_paths": ["/scenes", "/scenes/*", "/material_interpretation"],
        "criteria": ["没有待填文本", "每页信息来自源文档", "版式层级可执行"],
    },
    "motion_design": {
        "objective": "为已确认内容定义确定性的字幕、图形和转场语言",
        "question_id": "motion.design",
        "editable_paths": ["/global_style", "/shots", "/shots/*"],
        "criteria": ["文字动效归 HyperFrames", "素材槽有明确绑定", "安全区完整"],
    },
    "styleframe": {
        "objective": "确认色彩、字体层级和表面语言",
        "question_id": "styleframe.look",
        "editable_paths": ["/brand_primary", "/font_family", "/surface_language", "/feedback"],
        "criteria": ["使用真实品牌锚点", "客户已看到同渲染器预览"],
    },
    "audio": {
        "objective": "确认声音模式、语言、人物音色、语气和 BGM 合同",
        "question_id": "audio.voice",
        "editable_paths": [
            "/mode", "/language", "/voice_type", "/tone", "/bgm",
            "/speaker_role", "/speaker_profile", "/pace", "/articulation",
            "/emphasis", "/ending", "/pronunciation", "/avoid", "/feedback",
        ],
        "criteria": [
            "可见说话与声音模式一致", "声音关键选择完整且由客户确认",
            "本阶段只确认方向，不虚构独立试听音轨",
            "第一次模型声音试听与口型验收发生在 test_segment",
        ],
    },
    "product_board": {
        "objective": "确认产品身份板与上传源图一致",
        "question_id": "board.product.review",
        "editable_paths": ["/feedback", "/refinement_request"],
        "criteria": ["已展示产品板", "产品外观和源图一致", "确认绑定当前源图指纹"],
    },
    "cast_board": {
        "objective": "确认人物六视图身份、发型、服装和近景/全身一致性",
        "question_id": "board.cast.review",
        "editable_paths": ["/feedback", "/refinement_request"],
        "criteria": ["已展示人物板", "近景与全身是同一身份", "客户明确确认"],
    },
    "product_usage": {
        "objective": "逐格确认人物真实使用产品的物理接触关系",
        "question_id": "board.usage.geometry",
        "editable_paths": ["/feedback", "/refinement_request", "/panel_checks"],
        "criteria": ["9 个逐格结果和 URL 完整", "关键格接触面正确", "客户明确确认几何关系"],
    },
    "storyboard": {
        "objective": "确认人物、构图、产品表达、镜头递进和故事连续性",
        "question_id": "storyboard.review",
        "editable_paths": ["/feedback", "/refinement_request", "/shot_feedback"],
        "criteria": ["客户已看到真实预览", "所有引用与当前确认素材一致", "镜头可剪辑"],
    },
    "render_plan": {
        "objective": "确认渲染方向与确定性合成计划；付费模型的精确提交包另行捕获确认",
        "question_id": "render.prompt_review",
        "editable_paths": ["/feedback", "/segment_feedback", "/prompt_changes"],
        "criteria": ["渲染计划与当前故事板绑定", "文档讲解保留身份与视频提示词子闸门", "客户明确确认"],
    },
    "identity_plate_reference": {
        "objective": "单独确认身份锚图所用人物引用、提示词和计划请求身份",
        "question_id": "identity.reference.review",
        "editable_paths": ["/feedback", "/reference_feedback"],
        "criteria": ["人物板真实可见", "引用与当前人物确认一致", "客户明确确认身份引用包"],
    },
    "identity_plate_submission_capture": {
        "objective": "确认付费前捕获的精确模型、规格、提示词和 imageUrls 请求",
        "question_id": "identity.submission.review",
        "editable_paths": ["/feedback", "/request_feedback"],
        "criteria": ["JSON 与 Markdown 同时可用", "实时目录与引用哈希当前", "客户明确确认精确提交包"],
    },
    "identity_plate_generation": {
        "objective": "按已确认请求提交、恢复同一任务、下载身份锚图并完成确定性媒体质检",
        "question_id": "identity.generation.progress",
        "editable_paths": ["/progress_note"],
        "criteria": ["提交体与审批包逐字一致", "已有 taskId 时只恢复不重提", "结果 JSON 绑定实际任务和请求", "解码、尺寸、画幅质检通过"],
    },
    "identity_plate_review": {
        "objective": "逐项视觉检查生成的身份锚图并让客户看图确认",
        "question_id": "identity.image.review",
        "editable_paths": ["/feedback", "/refinement_request"],
        "criteria": ["单人且人脸清晰", "与确认人物板身份、发型、服装和配饰一致", "背景干净且无 PPT、文字、Logo 或水印", "构图可作为干净人物底片", "客户已看到并确认同一图片"],
    },
    "test_segment": {
        "objective": "审看付费测试片段并决定接受或授权新尝试",
        "question_id": "video.test_review",
        "editable_paths": ["/feedback", "/take_decision"],
        "criteria": ["测试片已展示", "拒绝时明确重试原因", "不复用已拒绝 take"],
    },
    "video": {
        "objective": "审看全部视频底片并处理 OCR 或质量告警",
        "question_id": "video.basecut_review",
        "editable_paths": ["/feedback", "/segment_feedback", "/take_decision"],
        "criteria": ["全部视频可见", "OCR 告警已处理", "客户确认底片"],
    },
    "shotcraft_packaging": {
        "objective": "确认后期卡片、运镜包装和图形层没有嵌入生成画面",
        "question_id": "packaging.review",
        "editable_paths": ["/feedback", "/card_changes", "/motion_changes"],
        "criteria": ["渲染帧 QC 通过", "所有文字由确定性后期层产生"],
    },
    "captions": {
        "objective": "确认字幕文案、时间线和底片身份一致",
        "question_id": "captions.review",
        "editable_paths": ["/feedback", "/line_changes"],
        "criteria": ["字幕时间线已展示", "字幕身份绑定当前底片"],
    },
    "final": {
        "objective": "确认最终成片和商业交付 QC",
        "question_id": "final.review",
        "editable_paths": ["/feedback", "/final_changes"],
        "criteria": ["最终成片已展示", "正式 QC 通过", "客户明确确认"],
    },
    "derive": {
        "objective": "确认各目标比例的安全区和构图结果",
        "question_id": "derive.review",
        "editable_paths": ["/feedback", "/ratio_changes"],
        "criteria": ["所有目标比例可见", "关键主体未被裁切"],
    },
    "delivery": {
        "objective": "生成并核验可追溯的最终交付清单",
        "question_id": "delivery.verify",
        "editable_paths": ["/delivery_note"],
        "criteria": ["所有文件哈希匹配", "无敏感信息", "交付证据闭环"],
    },
}


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _known_facts(route_source, chain_profile):
    route_source = route_source if isinstance(route_source, dict) else {}
    manifest = route_source.get("manifest") if isinstance(route_source.get("manifest"), dict) else {}
    brief = route_source.get("brief") if isinstance(route_source.get("brief"), dict) else {}
    facts = []
    for fact_id, value, source in (
        ("run.client", manifest.get("client") or brief.get("client"), "run_manifest"),
        ("run.id", manifest.get("run_id") or brief.get("run_id"), "run_manifest"),
        ("workflow.profile", (chain_profile or {}).get("profile"), "workflow_router"),
        ("brief.objective", brief.get("objective") or brief.get("goal"), "brief"),
        ("brief.audience", brief.get("audience"), "brief"),
        ("brief.product_name", brief.get("product_name"), "brief"),
        ("brief.output_ratio", brief.get("output_ratio"), "brief"),
    ):
        if value not in (None, "", [], {}):
            facts.append({"id": fact_id, "value": value, "source": source})
    source_facts = route_source.get("source_facts")
    if isinstance(source_facts, dict):
        for item in source_facts.get("facts") or []:
            if isinstance(item, dict) and item.get("id") and "value" in item:
                facts.append({"id": item["id"], "value": item["value"],
                              "source": item.get("source") or "source_facts"})
    return facts[:80]


def proposal_response_schema(max_items):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "question_id", "patches", "assistant_rationale", "unresolved"],
        "properties": {
            "schema_version": {"const": 1},
            "question_id": {"type": "string"},
            "base_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "patches": {"type": "array", "minItems": 1, "maxItems": int(max_items),
                        "items": {"type": "object", "additionalProperties": False,
                                  "required": ["path", "value"],
                                  "properties": {"path": {"type": "string", "pattern": "^/"},
                                                 "value": {}, "evidence": {"type": "object"}}}},
            "assistant_rationale": {"type": "string"},
            "unresolved": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        },
    }


def build_contract(stage, route=None, route_source=None, model_profile=None):
    spec = _STAGE_SPECS.get(stage) or {
        "objective": "完成当前阶段的最小可验证动作",
        "question_id": "%s.review" % stage,
        "editable_paths": ["/feedback"],
        "criteria": ["产物可见且通过当前阶段校验"],
    }
    route = route if isinstance(route, dict) else {}
    route_source = route_source if isinstance(route_source, dict) else {}
    profile = model_profile or conservative_profile()
    policy = interaction_policy(profile)
    previews = list(route.get("customer_preview") or route.get("preview_sources") or [])
    forbidden = ["不得虚构客户未提供的参数、销量、认证、排名或承诺",
                 "不得把 Agent 或模型自评写成客户确认"]
    source_facts = route_source.get("source_facts")
    if isinstance(source_facts, dict):
        forbidden.extend(str(item) for item in source_facts.get("forbidden_inferences") or [])
    next_action = str(route.get("next_action") or "")
    flow = route.get("flow") if isinstance(route.get("flow"), dict) else {}
    confirmation = flow.get("confirm_function")
    approval_request = None
    if next_action.startswith("approve_") or next_action.startswith("review_"):
        customer_decision = stage in CUSTOMER_CONFIRM_STAGES
        approval_request = {
            "required": bool(confirmation),
            "evidence_source": ("customer_dialogue" if customer_decision else
                                "technical_validation"),
            "confirm_function": confirmation,
            "artifact_must_be_shown_first": customer_decision,
        }
    actions = [
        "workflow_status", "workflow_next_v2", "workflow_impact_preview",
        "artifact_get",
    ]
    if stage != "delivery":
        actions.extend(["workflow_submit_patch", "workflow_refine"])
    if confirmation and stage in CUSTOMER_CONFIRM_STAGES:
        actions.extend([
            "workflow_review_present", "workflow_confirm_v2", "workflow_confirm"
        ])
    elif confirmation:
        actions.append("execute_confirm_function")
    payload = {
        "schema_version": 1,
        "packet_version": PACKET_VERSION,
        "stage": stage,
        "question_id": spec["question_id"],
        "objective": spec["objective"],
        "known_facts": _known_facts(route_source, route.get("chain_profile")),
        "immutable_facts": ["客户确认、资产指纹和付费任务状态只能由正式业务函数写入",
                            "宿主 Agent 名和模型名仅作为诊断元数据"],
        "forbidden_claims": list(dict.fromkeys(forbidden)),
        "choices": [],
        "editable_paths": spec["editable_paths"],
        "protected_paths": list(PROTECTED_PATHS),
        "response_schema": proposal_response_schema(policy["max_items_per_turn"]),
        "validation_command": "python3 scripts/interaction_contract.py validate-proposal --contract <contract.json> --proposal <proposal.json>",
        "success_criteria": spec["criteria"],
        "max_items_per_turn": policy["max_items_per_turn"],
        "repair_actions": ["remove_unsupported_claim", "repair_schema", "reduce_batch", "ask_customer"],
        "allowed_actions": actions,
        "artifacts_to_show": previews,
        "approval_request": approval_request,
        "interaction_policy": {key: policy[key] for key in ("tier", "mode", "reason")},
    }
    enforce(payload, "interaction-contract", context="build_contract")
    return payload


def _path_matches(path, allowed):
    if allowed.endswith("/*"):
        return path.startswith(allowed[:-1])
    return path == allowed or path.startswith(allowed.rstrip("/") + "/")


def _path_is_protected(path, protected_paths):
    return any(path == root or path.startswith(root.rstrip("/") + "/")
               for root in protected_paths)


def validate_proposal(contract, proposal, target_path=None):
    errors = validate_schema(proposal, "shot-patch")
    if not isinstance(contract, dict):
        errors.append("CONTRACT_INVALID")
        return errors
    else:
        errors.extend(validate_schema(contract, "interaction-contract"))
    if proposal.get("question_id") != contract.get("question_id"):
        errors.append("QUESTION_ID_STALE: proposal 与当前阶段问题不一致")
    patches = proposal.get("patches") if isinstance(proposal.get("patches"), list) else []
    patch_weight = sum(max(1, len((item or {}).get("value")))
                       if isinstance((item or {}).get("value"), list) else 1
                       for item in patches)
    if patch_weight > int(contract.get("max_items_per_turn") or 1):
        errors.append("PATCH_BATCH_TOO_LARGE: 当前模型每轮最多 %s 项" %
                      contract.get("max_items_per_turn"))
    for index, patch in enumerate(patches):
        path = str((patch or {}).get("path") or "")
        if _path_is_protected(path, contract.get("protected_paths") or PROTECTED_PATHS):
            errors.append("PATCH_PROTECTED[%d]: %s" % (index, path))
        if not any(_path_matches(path, allowed)
                   for allowed in contract.get("editable_paths") or []):
            errors.append("PATCH_PATH_NOT_ALLOWED[%d]: %s" % (index, path))
        fact_bearing = any(token in path.lower() for token in
                           ("fact", "primary_usp", "message", "talk", "claim"))
        if fact_bearing and not isinstance((patch or {}).get("evidence"), dict):
            errors.append("PATCH_EVIDENCE_REQUIRED[%d]: %s" % (index, path))
    if target_path and os.path.isfile(target_path):
        expected = proposal.get("base_sha256")
        if not expected:
            errors.append("PATCH_BASE_SHA256_REQUIRED")
        elif expected != _file_sha256(target_path):
            errors.append("PATCH_BASE_STALE")
    return errors


def _pointer_parts(path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("PATCH_POINTER_INVALID: %s" % path)
    return [part.replace("~1", "/").replace("~0", "~")
            for part in path[1:].split("/") if part != ""]


def _apply_pointer(document, path, value):
    parts = _pointer_parts(path)
    if not parts:
        raise ValueError("PATCH_ROOT_REPLACE_FORBIDDEN")
    cursor = document
    for part in parts[:-1]:
        if isinstance(cursor, list):
            try:
                cursor = cursor[int(part)]
            except (ValueError, IndexError):
                raise ValueError("PATCH_POINTER_MISSING: %s" % path)
        elif isinstance(cursor, dict):
            if part not in cursor:
                cursor[part] = {}
            cursor = cursor[part]
        else:
            raise ValueError("PATCH_POINTER_MISSING: %s" % path)
    leaf = parts[-1]
    if isinstance(cursor, list):
        try:
            index = int(leaf)
        except ValueError:
            raise ValueError("PATCH_ARRAY_INDEX_INVALID: %s" % path)
        if index < 0 or index >= len(cursor):
            raise ValueError("PATCH_ARRAY_INDEX_MISSING: %s" % path)
        cursor[index] = copy.deepcopy(value)
    elif isinstance(cursor, dict):
        cursor[leaf] = copy.deepcopy(value)
    else:
        raise ValueError("PATCH_POINTER_MISSING: %s" % path)


def apply_proposal(contract, proposal, target_path, target_schema=None):
    errors = validate_proposal(contract, proposal, target_path=target_path)
    if errors:
        raise ValueError("PATCH_REJECTED: %s" % "；".join(errors[:12]))
    with open(target_path, encoding="utf-8") as handle:
        target = json.load(handle)
    updated = copy.deepcopy(target)
    for patch in proposal["patches"]:
        _apply_pointer(updated, patch["path"], patch.get("value"))
    if target_schema:
        schema_errors = validate_schema(updated, target_schema)
        if schema_errors:
            raise ValueError("PATCH_TARGET_SCHEMA_FAILED: %s" % "；".join(schema_errors[:12]))
    before = _file_sha256(target_path)
    atomic_write_json(target_path, updated)
    after = _file_sha256(target_path)
    audit_path = target_path + ".patch-audit.json"
    history = []
    lock = FileLock(audit_path + ".lock")
    with lock:
        if os.path.isfile(audit_path):
            try:
                with open(audit_path, encoding="utf-8") as handle:
                    history = json.load(handle)
            except (OSError, ValueError, TypeError):
                history = []
        if not isinstance(history, list):
            history = []
        history.append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "question_id": proposal["question_id"],
            "paths": [item["path"] for item in proposal["patches"]],
            "before_sha256": before,
            "after_sha256": after,
            "proposal_sha256": hashlib.sha256(json.dumps(
                proposal, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            "evidence": [{"source": (item.get("evidence") or {}).get("source"),
                          "ref": (item.get("evidence") or {}).get("ref")}
                         for item in proposal["patches"] if item.get("evidence")],
        })
        atomic_write_json(audit_path, history)
    return {"ok": True, "target": os.path.abspath(target_path),
            "before_sha256": before, "after_sha256": after,
            "audit": os.path.abspath(audit_path)}


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--stage", required=True)
    build.add_argument("--route")
    build.add_argument("--route-source")
    build.add_argument("--model-profile")
    build.add_argument("--out")
    check = sub.add_parser("validate-proposal")
    check.add_argument("--contract", required=True)
    check.add_argument("--proposal", required=True)
    check.add_argument("--target")
    apply = sub.add_parser("apply")
    apply.add_argument("--contract", required=True)
    apply.add_argument("--proposal", required=True)
    apply.add_argument("--target", required=True)
    apply.add_argument("--target-schema")
    args = parser.parse_args(argv)
    if args.command == "build":
        profile = load_profile(args.model_profile) if args.model_profile else conservative_profile()
        result = build_contract(args.stage, _read(args.route) if args.route else {},
                                _read(args.route_source) if args.route_source else {}, profile)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            atomic_write_json(args.out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-proposal":
        errors = validate_proposal(_read(args.contract), _read(args.proposal), args.target)
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if not errors else 2
    result = apply_proposal(_read(args.contract), _read(args.proposal), args.target,
                            target_schema=args.target_schema)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
