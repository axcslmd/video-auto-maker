#!/usr/bin/env python3
"""cleanup_output — output/ 前隔离时代遗留文件归档。

背景：run-id 隔离（output/<client>/<run_id>/）在代码层已强制执行，但 output/
顶层仍散落大量隔离前的扁平遗留（seg_*.mp4、*_run_manifest.json、调试脚本、
.DS_Store 等），干扰"哪些是正式产物"的判断，也是 INC-005 类旧产物误用的
环境诱因。

本脚本把 output/ 顶层（不进子目录）中非活跃 run 目录的遗留文件归档到
output/_legacy/<归档日期>/，**只移动不删除**，全程可逆。

默认 dry-run（只打印计划）；加 --apply 才实际移动。
"""
import argparse
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "..", "output")

# 明确保留在顶层的名字（目录=活跃 run 或正式归档；这些文件有功能性用途）
KEEP_FILES = {".gitkeep", "README.md"}


def find_legacy_files(output_dir):
    """返回 output/ 顶层需要归档的文件列表（不递归子目录）。"""
    legacy = []
    for name in sorted(os.listdir(output_dir)):
        path = os.path.join(output_dir, name)
        if os.path.isdir(path):
            continue  # run 目录与 _legacy 本身不动
        if name in KEEP_FILES:
            continue
        legacy.append(path)
    return legacy


def main(argv=None):
    parser = argparse.ArgumentParser(description="output/ 顶层遗留文件归档（只移动不删除）")
    parser.add_argument("--apply", action="store_true", help="实际执行移动（默认 dry-run）")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="output 目录（测试用）")
    args = parser.parse_args(argv)
    output_dir = os.path.abspath(args.output_dir)
    if not os.path.isdir(output_dir):
        print("ERROR: output 目录不存在：%s" % output_dir, file=sys.stderr)
        return 1
    legacy = find_legacy_files(output_dir)
    if not legacy:
        print("output/ 顶层无遗留文件，无需归档。")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = os.path.join(output_dir, "_legacy", stamp)
    print("发现 %d 个顶层遗留文件%s：" % (len(legacy), "" if args.apply else "（dry-run，未移动）"))
    for path in legacy:
        print("  %s (%d bytes)" % (os.path.basename(path), os.path.getsize(path)))
    if not args.apply:
        print("\n加 --apply 实际归档到 %s" % os.path.relpath(archive_dir, output_dir))
        return 0
    os.makedirs(archive_dir, exist_ok=True)
    moved, failed = 0, []
    for path in legacy:
        target = os.path.join(archive_dir, os.path.basename(path))
        try:
            shutil.move(path, target)
            moved += 1
        except OSError as exc:
            failed.append("%s: %s" % (path, exc))
    print("已归档 %d/%d 个文件到 %s" % (moved, len(legacy), archive_dir))
    for item in failed:
        print("  失败：%s" % item, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
