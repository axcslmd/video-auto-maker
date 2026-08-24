#!/usr/bin/env python3
"""Probe BasicRouter for online video+image models and their capabilities.

Usage:
  BR_KEY=sk-xxx python3 probe_models.py            # video + image
  BR_KEY=sk-xxx python3 probe_models.py video      # one category

The /employee/models endpoint needs NO auth, but we send the key anyway
if present. Note: /v1/models only returns chat/LLM models — video/image
models ONLY appear under /employee/models?category=video|image.
"""
import json, os, sys, urllib.request

BASE = "https://api.basicrouter.ai/api"
KEY = os.environ.get("BR_KEY", "")

def fetch(category):
    url = f"{BASE}/employee/models?category={category}"
    req = urllib.request.Request(url)
    if KEY:
        req.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def main():
    cats = sys.argv[1:] or ["video", "image"]
    for cat in cats:
        print(f"=== category={cat} ===")
        try:
            d = fetch(cat)
        except Exception as e:
            print("  ERROR:", e); continue
        for m in d.get("data", []):
            print(f"  {m.get('modelName')} | {m.get('provider')} "
                  f"| minDur={m.get('videoDurationMin')} "
                  f"| types={m.get('allowVideoType')} "
                  f"| online={m.get('online')}")

if __name__ == "__main__":
    main()
