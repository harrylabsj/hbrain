#!/usr/bin/env python3
"""Audit WeRead raw files (noteCount>0) that lack entity pages.
Handles hbrain split-path: raw at raw/微信读书/ and llm-wiki/微信读书/,
entities at llm-wiki/entities/."""

import os, re, sys

VAULT = "/Users/jianghaidong/hbrain"
RAW_DIRS = [
    os.path.join(VAULT, "raw/微信读书"),
    os.path.join(VAULT, "llm-wiki/微信读书"),
]
ENTITY_DIR = os.path.join(VAULT, "llm-wiki/entities")

def normalize(s):
    for ch in '：：:': s = s.replace(ch, '-')
    for ch in '?？ ': s = s.replace(ch, '-')
    while '--' in s: s = s.replace('--', '-')
    s = s.replace('\u2014', '').replace('\u2013', '')
    s = re.sub(r'-*\[[美英法日德俄意西荷瑞印巴韩]\]', '-', s)
    while '--' in s: s = s.replace('--', '-')
    return s.strip('-')

# Collect raw files with noteCount>0
raw_note_counts = {}
for raw_dir in RAW_DIRS:
    if not os.path.isdir(raw_dir):
        continue
    for fname in os.listdir(raw_dir):
        fpath = os.path.join(raw_dir, fname)
        if not fname.endswith('.md') or not os.path.isfile(fpath):
            continue
        try:
            with open(fpath) as f:
                content = f.read()
        except:
            continue
        m = re.search(r'noteCount:\s*(\d+)', content)
        if m:
            nc = int(m.group(1))
            if nc > 0:
                raw_note_counts[fname] = {
                    'noteCount': nc,
                    'path': fpath,
                }

# Collect entity stems
entity_stems = {}  # norm_stem -> fname
if os.path.isdir(ENTITY_DIR):
    for fname in os.listdir(ENTITY_DIR):
        if fname.endswith('.md'):
            stem = fname.replace('.md', '')
            norm_stem = normalize(stem)
            entity_stems[norm_stem] = fname

# Match raw -> entity
unmapped = []
for raw_fname, info in raw_note_counts.items():
    raw_stem = raw_fname.replace('.md', '')
    norm_raw = normalize(raw_stem)
    matched = False
    # Exact match
    if norm_raw in entity_stems:
        matched = True
    else:
        # Substring containment
        for es, efname in entity_stems.items():
            if norm_raw in es or es in norm_raw:
                matched = True
                break
    if not matched:
        unmapped.append((raw_fname, info))

print(f"Raw files with noteCount>0: {len(raw_note_counts)}")
print(f"Entity stems: {len(entity_stems)}")
print(f"Unmapped: {len(unmapped)}")
print()

for raw_fname, info in sorted(unmapped, key=lambda x: -x[1]['noteCount']):
    raw_stem = raw_fname.replace('.md', '')
    norm_raw = normalize(raw_stem)
    print(f"noteCount={info['noteCount']:4d}  {raw_fname}")
    print(f"  norm: {norm_raw}")
    print(f"  path: {info['path']}")
    print()

# Also output JSON for downstream use
import json
result = {
    'total_raw': len(raw_note_counts),
    'total_entities': len(entity_stems),
    'unmapped_count': len(unmapped),
    'unmapped': [{'filename': f, 'noteCount': i['noteCount'], 'path': i['path']} for f, i in unmapped],
}
with open(os.path.join(VAULT, '_scripts/unmapped.json'), 'w') as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print(f"JSON written to _scripts/unmapped.json")
