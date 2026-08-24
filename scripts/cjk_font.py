#!/usr/bin/env python3
"""Resolve a real CJK font for browser and libass subtitle renderers."""
import os
import platform


FONT_CANDIDATES = {
    "Darwin": [
        ("/System/Library/Fonts/PingFang.ttc", "PingFang SC"),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", "Hiragino Sans GB"),
    ],
    "Windows": [
        (r"C:\Windows\Fonts\msyh.ttc", "Microsoft YaHei"),
        (r"C:\Windows\Fonts\msyhbd.ttc", "Microsoft YaHei"),
        (r"C:\Windows\Fonts\simhei.ttf", "SimHei"),
    ],
    "Linux": [
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", "Noto Sans CJK SC"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "WenQuanYi Zen Hei"),
    ],
}


def resolve():
    for path, family in FONT_CANDIDATES.get(platform.system(), []):
        if os.path.isfile(path):
            return {"path": path, "family": family}
    return {"path": None, "family": "Noto Sans CJK SC"}


def family():
    return resolve()["family"]
