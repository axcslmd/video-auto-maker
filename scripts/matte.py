#!/usr/bin/env python3
"""数字人 + 背景融合 —— 全部交给外部模型能力，本地不做抠像。

定位铁律：LLM 是视频工厂，我们是「提示词 + 素材」的共创前台。抠像/换背景这类
像素级活儿一律走外部模型，不在客户机本地跑 rembg/onnxruntime（既慢又出毛边，
且与「零依赖轻交付」矛盾）。

两条外部融合路线（本脚本实现路线 C；路线 A 无需本脚本，直接在 video_engine 用
videoType 4/5 把场景图作参考图，提示词写「数字人站在此场景中讲解」，模型直接
生成人景融合、零接缝）：

- 路线 A（主线·推荐，零融合脚本）：数字人「出生」就在背景里。将场景图/PPT 事实素材
  编译进 video_type 4/5 的最终 segment，用 video_engine.py --batch；台词提示词写明人站在场景中。无接缝、
  最省。→ 用 video_engine，不用本脚本。
- 路线 C（精控备选，本脚本）：先用 createImage 的 img2img，把数字人形象 + 目标
  背景合成为一张「人已在背景中」的图（hosted URL），再交 video_engine 用该图做
  首帧/参考图驱动成视频。外部模型完成融合，本地只做编排。

CLI（路线 C）:
  # 把数字人形象 + 背景图 融合成一张人景合一的图，返回 hosted URL
  python3 matte.py compose --client x --human actors/x/portrait.png --scene assets/x/factory.jpg \
      --prompt "数字人讲师自然站在工厂产线前讲解，光影融合，写实" --out output/fused_frame.png
  python3 matte.py doctor
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import br_client            # noqa: E402
import key_setup            # noqa: E402
import video_guard          # noqa: E402

FUSE_MODEL = "kling-v3-omni-image"   # 多图参考/编辑能力强，适合人景合成


def compose_scene(human_path, scene_path, prompt, out_path=None,
                  model=FUSE_MODEL, ratio="9:16", api_key=None, verbose=True,
                  client=None, allow_unconfirmed=False, manifest=None):
    """路线 C：外部模型把「数字人形象 + 背景图」融合成一张人景合一的图。

    做法：把两张本地图作为 img2img 的多图参考喂给 createImage，提示词描述
    「人自然站在该背景中、光影融合」。外部模型完成抠像+合成+打光，本地零像素处理。
    返回 {ok, hosted_url, local_path}。hosted_url 应在审批前写入
    segment.urls，并在 segment 内设置 video_type 4/5。

    client/allow_unconfirmed：与 script_splitter.split(client=...) 对齐的确认闸门
    ——同一个 confirmed/pending 状态机（asset_prep.gen_image/refine_image 产出的
    候选图默认 status='pending'，只有 confirm_image() 之后才是 confirmed）此前只在
    script_splitter.py 里被校验，matte.py 这条人景融合支线完全没检查，客户没确认的
    候选图（可能是被拒绝的版本）可以被直接拿去融合、进而喂给 video_engine 出片。
    正式模式必须传 client；human_path/scene_path 会跨 asset_prep、digital_human、
    product_library 和可选 manifest 复核当前客户证据与文件哈希。任一引用不受信即
    拒绝（SystemExit("UNCONFIRMED_ASSET: ...")）；仅做草稿预览才应显式传
    allow_unconfirmed=True，且结果会标记 draft/unconfirmed_refs。
    """
    if not client and not allow_unconfirmed:
        raise SystemExit(
            "CLIENT_REQUIRED: formal matte compose must identify the client so "
            "actor, scene and product confirmation evidence can be revalidated.")
    if isinstance(manifest, str):
        try:
            with open(manifest, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            raise SystemExit("INVALID_MANIFEST: %s" % exc)
    if manifest is not None and not isinstance(manifest, dict):
        raise SystemExit("INVALID_MANIFEST: expected JSON object")
    unconfirmed_refs = []
    for label, value in (("human", human_path), ("scene", scene_path)):
        trusted = bool(client and video_guard._reference_is_trusted(
            client, value, manifest))
        if value and not trusted:
            unconfirmed_refs.append({"role": label, "path": value})
    if unconfirmed_refs and not allow_unconfirmed:
        first = unconfirmed_refs[0]
        raise SystemExit(
            "UNCONFIRMED_ASSET: %s image %r has no current customer/hash evidence. "
            "Confirm it in asset_prep/digital_human/product_library first, or pass "
            "allow_unconfirmed=True for an explicit draft-only preview." %
            (first["role"], first["path"]))

    api_key = api_key or key_setup.load_key()
    if not api_key:
        raise br_client.BRError(
            "No API key. Use the host secure credential input and non-echoing "
            "key onboarding; never send a key in ordinary chat.")

    def log(m):
        if verbose:
            print(m, flush=True)

    # 本地图 → 平台可用的图像引用（data URL / hosted）
    refs = []
    for p in (human_path, scene_path):
        if not p:
            continue
        if p.startswith(("http://", "https://")):
            refs.append(p)
        else:
            if not os.path.exists(p):
                raise br_client.BRError("素材不存在: %s" % p)
            refs.append(br_client._to_data_url(p))
    log("[compose] 外部模型融合 人+景 (%d 张参考图) model=%s" % (len(refs), model))

    task_id = br_client.create_image_generation(
        api_key, prompt, model=model, count=1,
        resolution="2k", ratio=ratio, image_urls=refs)
    log("[compose] image task submitted=%s" % task_id)
    urls = br_client.wait_image_generation(api_key, task_id, interval=5, max_wait=900)
    if not urls:
        raise br_client.BRError("融合失败：平台未返回图像 URL")
    hosted = urls[0]
    log("[compose] hosted=%s" % hosted)

    local = None
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        br_client.download(hosted, out_path, allow_nonpublic_peer=True)
        local = out_path
        log("[saved] %s" % local)
    return {"ok": True, "hosted_url": hosted, "local_path": local,
            "draft": bool(unconfirmed_refs),
            "unconfirmed_refs": unconfirmed_refs}


def doctor():
    print("=== 人景融合（外部模型能力）体检 ===")
    key = key_setup.load_key()
    print("API Key:", "已配置" if key else "未配置（先做 key onboarding）")
    print("融合模型:", FUSE_MODEL, "（外部，走 BasicRouter；本地不跑抠像）")
    print("说明: 路线A(在已确认 segment 内设 video_type 4/5 做参考图人景融合)优先；"
          "本脚本=路线C(img2img人景合成)")
    ok = bool(key)
    print("结论:", "READY" if ok else "需先配 API Key")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="数字人+背景融合（外部模型能力，本地不抠像）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compose", help="路线C：外部模型把数字人融合进背景图 → hosted URL")
    c.add_argument("--human", required=True, help="数字人形象图（本地路径或 URL）")
    c.add_argument("--scene", required=True, help="目标背景/场景图（本地路径或 URL）")
    c.add_argument("--prompt", required=True, help="融合提示词（人如何站位/光影/风格）")
    c.add_argument("--out", default=None, help="可选：把融合图下载到本地")
    c.add_argument("--ratio", default="9:16")
    c.add_argument("--client", default=None,
                   help="正式融合必填：跨 asset_prep/digital_human/product_library 校验当前确认")
    c.add_argument("--manifest", default=None,
                   help="可选 run_manifest.json：验证当前 run 的已确认故事板产物")
    c.add_argument("--allow-unconfirmed", dest="allow_unconfirmed", action="store_true",
                   help="放行引用未确认(pending)候选图（仅草稿预览用）")
    c.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="检测 API Key（外部能力就绪度）")
    a = ap.parse_args()
    if a.cmd == "compose":
        try:
            res = compose_scene(a.human, a.scene, a.prompt, out_path=a.out,
                                ratio=a.ratio, verbose=not a.json,
                                client=a.client, allow_unconfirmed=a.allow_unconfirmed,
                                manifest=a.manifest)
        except br_client.BRError as e:
            print(json.dumps({"ok": False, "error": str(e)}) if a.json else "ERROR: %s" % e)
            sys.exit(1)
        if a.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print("融合图: %s" % (res["local_path"] or res["hosted_url"]))
            print("→ 先把 hosted URL 写入待确认 segments 并登记 video handoff，再正式执行：")
            print("  python3 scripts/video_engine.py --batch <segments.json> --client %s "
                  "--manifest <run_manifest.json> --results-out <batch_results.json> "
                  "--prompt-review <confirmed_video_prompt_review.json>" %
                  (a.client or "<client>"))
    elif a.cmd == "doctor":
        sys.exit(doctor())


if __name__ == "__main__":
    main()
