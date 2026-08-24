#!/usr/bin/env python3
"""Re-sync gallery/source/*.md copies and library.json text fields from references/shots/ cards.

Run from repo root after editing any card:  python3 gallery/sync-from-cards.py
"""
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / 'references' / 'shots'
SOURCE = ROOT / 'gallery' / 'source'
LIB = ROOT / 'gallery' / 'api' / 'library.json'


def parse_card(path):
    text = path.read_text(encoding='utf-8')
    m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                fm[k.strip()] = v.strip()
    im = re.search(r'^## 意图\n(.*?)(?=\n## )', text, re.S | re.M)
    intention = re.sub(r'\s*\n\s*', ' ', im.group(1).strip()) if im else None
    return fm, intention


def main():
    cards = {p.stem: p for p in SHOTS.glob('*.md')}

    # 1. refresh gallery/source copies (drop copies whose card no longer exists)
    for old in SOURCE.glob('*.md'):
        if old.stem not in cards:
            old.unlink()
            print(f'removed stale copy: {old.name}')
    for name, p in cards.items():
        shutil.copyfile(p, SOURCE / p.name)

    # 2. refresh library.json text fields
    lib = json.loads(LIB.read_text(encoding='utf-8'))
    missing = []
    for card in lib['cards']:
        p = cards.get(card['name'])
        if not p:
            missing.append(card['name'])
            continue
        fm, intention = parse_card(p)
        if fm.get('一句话'):
            card['summary'] = fm['一句话']
        if fm.get('适用'):
            card['use'] = fm['适用']
        if fm.get('时长'):
            card['duration'] = fm['时长']
        if fm.get('能量'):
            card['energy'] = fm['能量']
        if intention:
            card['intention'] = intention
        if len(card.get('styles', [])) == 1:
            card['styles'][0]['description'] = card['summary']
    LIB.write_text(json.dumps(lib, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f"synced {len(lib['cards'])} cards; missing card files: {missing or 'none'}")

    # 3. regenerate SEO artifacts (prerendered card list, sitemap.xml, llms.txt)
    import runpy
    runpy.run_path(str(Path(__file__).parent / 'build-seo.py'))


if __name__ == '__main__':
    main()
