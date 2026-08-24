#!/usr/bin/env python3
"""Host-neutral customer workflow adapter; never bypasses formal gates."""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pipeline
import run_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="宿主无关 AI 营销视频客户流程")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--client", required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--out", required=True)
    init.add_argument("--script")
    init.add_argument("--plan")
    client_cmd = sub.add_parser("client", help="客户上下文操作；client init 等同于 init")
    client_cmd.add_argument("action", choices=("init", "status"))
    client_cmd.add_argument("--client")
    client_cmd.add_argument("--run-id")
    client_cmd.add_argument("--out")
    client_cmd.add_argument("--manifest")
    client_cmd.add_argument("--script")
    client_cmd.add_argument("--plan")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--manifest", required=True)
    bootstrap.add_argument("--stage", choices=("brief", "script"), required=True)
    bootstrap.add_argument("--path", action="append", required=True)
    status = sub.add_parser("status")
    status.add_argument("--manifest", required=True)
    run_cmd = sub.add_parser("run", help="run status/init 的宿主无关别名")
    run_cmd.add_argument("action", choices=("init", "status"))
    run_cmd.add_argument("--manifest")
    run_cmd.add_argument("--client")
    run_cmd.add_argument("--run-id")
    run_cmd.add_argument("--out")
    gate = sub.add_parser("gate")
    gate.add_argument("--manifest", required=True)
    gate.add_argument("--stage", choices=run_manifest.STAGES, required=True)
    verify = sub.add_parser("verify-delivery")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--delivery", required=True)
    args = parser.parse_args(argv)
    if args.command in ("init", "client") and (args.command == "init" or args.action == "init"):
        if not args.client or not args.run_id or not args.out:
            parser.error("client init 必须提供 --client/--run-id/--out")
        if os.path.exists(args.out):
            raise SystemExit("RUN_EXISTS: 请更换 run-id，不覆盖历史确认和成片")
        manifest = run_manifest.create_manifest(
            args.client, args.run_id,
            script_path=getattr(args, "script", None), plan_path=getattr(args, "plan", None))
        run_manifest.save_manifest(manifest, args.out, create_only=True)
        print(json.dumps({"ok": True, "manifest": os.path.abspath(args.out),
                          "client": args.client, "run_id": args.run_id,
                          "next": "bootstrap brief/script or start brief"}, ensure_ascii=False))
        return 0
    if args.command == "run":
        if args.action == "init":
            return main(["init", "--client", args.client, "--run-id", args.run_id,
                         "--out", args.out])
        args.manifest = args.manifest or args.out
        args.command = "status"
    if args.command == "client":
        if args.action == "status":
            args.manifest = args.manifest or args.out
            if not args.manifest:
                parser.error("client status 必须提供 --manifest")
        args.command = "status"
    try:
        manifest = run_manifest.load_manifest(args.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR:RUN_MANIFEST_UNAVAILABLE\n下一步：请先用 init 创建 run，或检查 manifest 路径。\n详情：%s" % exc,
              file=sys.stderr)
        return 2
    if args.command == "bootstrap":
        try:
            run_manifest.bootstrap_pending_approval(manifest, args.stage, args.path)
        except (ValueError, OSError) as exc:
            print("ERROR:BOOTSTRAP_OUTPUT_MISSING\n下一步：请先创建或上传真实 %s 文件，再重新执行 bootstrap。\n详情：%s" %
                  (args.stage, exc), file=sys.stderr)
            return 2
        run_manifest.save_manifest(manifest, args.manifest)
        print(json.dumps({"ok": True, "stage": args.stage,
                          "next": "客户查看后，用 pipeline.py approve 并传 --customer-confirmation 原话"}, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps(pipeline.pipeline_status(manifest), ensure_ascii=False))
    elif args.command == "gate":
        try:
            run_manifest.generation_gate(manifest, args.stage, client=manifest["client"])
        except (ValueError, OSError) as exc:
            print("ERROR:%s\n下一步：先查看 status，完成前置阶段确认后重试。\n详情：%s" %
                  (str(exc).split(":", 1)[0], exc), file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "stage": args.stage, "allowed": True}, ensure_ascii=False))
    elif args.command == "verify-delivery":
        print(json.dumps(pipeline.verify_delivery(manifest, args.delivery), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
