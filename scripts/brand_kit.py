#!/usr/bin/env python3
"""Brand kit: store a client's brand pack and apply brand compliance to outputs.

Brand pack = colors (hex), fonts, logo file + placement rules. Injected into
video片头/片尾/水印 and used as a style prefix for image prompts.

Layout:
  brand/<client>/
    brand.json    {colors:{primary,...}, fonts:{...}, logo, logo_pos, logo_scale, style_prefix}
    logo.png      brand logo (transparent PNG)

CLI:
  python3 brand_kit.py set --client <client> --logo /path/logo.png \
      --primary "#E60012" --font "PingFang SC" --pos tr --scale 0.12 \
      --style "科技感、緊湊、深色背景、產品居中"
  python3 brand_kit.py get --client <client>
  python3 brand_kit.py stamp --client <client> --input in.mp4 --out out.mp4   # overlay logo
  python3 brand_kit.py style-prefix --client <client>                         # print image style prefix
"""
import os
import sys
import json
import shutil
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRAND = os.path.join(ROOT, "brand")
sys.path.insert(0, HERE)
from project_utils import validate_client


def _dir(client):
    validate_client(client)
    d = os.path.join(BRAND, client)
    os.makedirs(d, exist_ok=True)
    return d


def _path(client):
    return os.path.join(_dir(client), "brand.json")


def load(client):
    p = _path(client)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"client": client, "colors": {}, "fonts": {}, "logo": None,
            "logo_pos": "tr", "logo_scale": 0.12, "style_prefix": ""}


def set_brand(client, logo=None, primary=None, secondary=None, font=None,
              pos=None, scale=None, style=None):
    b = load(client)
    if logo:
        if not os.path.isfile(logo):
            raise SystemExit("logo not found: %s" % logo)
        dest = os.path.join(_dir(client), "logo.png")
        shutil.copyfile(logo, dest)
        b["logo"] = os.path.relpath(dest, ROOT)
    if primary:
        b["colors"]["primary"] = primary
    if secondary:
        b["colors"]["secondary"] = secondary
    if font:
        b["fonts"]["primary"] = font
    if pos:
        b["logo_pos"] = pos
    if scale is not None:
        b["logo_scale"] = scale
    if style is not None:
        b["style_prefix"] = style
    with open(_path(client), "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=2)
    return b


def style_prefix(client):
    """Return a style string to prepend to image-generation prompts for brand consistency."""
    b = load(client)
    parts = []
    if b.get("style_prefix"):
        parts.append(b["style_prefix"])
    if b.get("colors", {}).get("primary"):
        parts.append("品牌主色 %s" % b["colors"]["primary"])
    return "，".join(parts)


def stamp(client, input_path, out_path):
    """Overlay the brand logo onto a video (brand watermark / compliance)."""
    import compose
    b = load(client)
    if not b.get("logo"):
        raise SystemExit("no brand logo set; run brand_kit.py set --logo ... first")
    logo_abs = os.path.join(ROOT, b["logo"])
    return compose.overlay_logo(input_path, logo_abs, out_path,
                                pos=b.get("logo_pos", "tr"),
                                scale=b.get("logo_scale", 0.12))


def main(argv):
    p = argparse.ArgumentParser(description="brand kit")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("set")
    ps.add_argument("--client", required=True)
    ps.add_argument("--logo")
    ps.add_argument("--primary")
    ps.add_argument("--secondary")
    ps.add_argument("--font")
    ps.add_argument("--pos", choices=["tr", "tl", "br", "bl"])
    ps.add_argument("--scale", type=float)
    ps.add_argument("--style")

    pg = sub.add_parser("get")
    pg.add_argument("--client", required=True)

    pp = sub.add_parser("style-prefix")
    pp.add_argument("--client", required=True)

    pt = sub.add_parser("stamp")
    pt.add_argument("--client", required=True)
    pt.add_argument("--input", required=True)
    pt.add_argument("--out", required=True)

    args = p.parse_args(argv)
    if args.cmd == "set":
        print(json.dumps(set_brand(args.client, logo=args.logo, primary=args.primary,
                                   secondary=args.secondary, font=args.font,
                                   pos=args.pos, scale=args.scale, style=args.style),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "get":
        print(json.dumps(load(args.client), ensure_ascii=False, indent=2))
    elif args.cmd == "style-prefix":
        print(style_prefix(args.client))
    elif args.cmd == "stamp":
        out = stamp(args.client, args.input, args.out)
        print(json.dumps({"ok": True, "out": out}))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
