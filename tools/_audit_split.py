#!/usr/bin/env python3
"""Audit hbrain split-path: find WeRead books with noteCount>0 lacking entity pages."""
import os, re, sys

def normalize(s):
    s = os.path.splitext(os.path.basename(s))[0]
    for ch in '：：:': s = s.replace(ch, '-')
    for ch in '?？': s = s.replace(ch, '-')
    s = s.replace(' ', '-')
    s = s.replace('《', '').replace('》', '')
    s = s.replace('\u2014', '').replace('\u2013', '')
    while '--' in s: s = s.replace('--', '-')
    s = re.sub(r'-*\[[美英法日德俄意西荷瑞印巴韩]\]', '-', s)
    while '--' in s: s = s.replace('--', '-')
    return s.strip('-').lower()

VAULT = sys.argv[1] if len(sys.argv) > 1 else '/Users/jianghaidong/hbrain'
RAW_DIRS = [
    os.path.join(VAULT, 'raw', '微信读书'),
    os.path.join(VAULT, 'llm-wiki', '微信读书'),
]
ENTITY_DIRS = [
    os.path.join(VAULT, 'llm-wiki', 'entities'),
    os.path.join(VAULT, 'llm-wiki', '微信读书'),
]

# Collect raw files with noteCount
raw_books = []
for raw_dir in RAW_DIRS:
    if not os.path.isdir(raw_dir):
        continue
    for fname in os.listdir(raw_dir):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(raw_dir, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
        except:
            continue
        nc_match = re.search(r'noteCount:\s*(\d+)', content)
        note_count = int(nc_match.group(1)) if nc_match else 0
        raw_books.append((fpath, fname, note_count))

total_raw = len(raw_books)
raw_with_notes = [(p, s, nc) for p, s, nc in raw_books if nc > 0]
print(f"Total raw files: {total_raw}")
print(f"With noteCount>0: {len(raw_with_notes)}")

# Build normalized entity stem set
entity_norm_stems = set()
for entity_dir in ENTITY_DIRS:
    if not os.path.isdir(entity_dir):
        continue
    for fname in os.listdir(entity_dir):
        if not fname.endswith('.md'):
            continue
        entity_norm_stems.add(normalize(fname))

print(f"Entity pages: {len(entity_norm_stems)}")

# Find unmatched
unmatched = []
for fpath, stem, nc in raw_with_notes:
    norm_stem = normalize(stem)
    if norm_stem in entity_norm_stems:
        continue
    # Substring
    found = any(norm_stem in en or en in norm_stem for en in entity_norm_stems)
    if not found:
        unmatched.append((fpath, stem, nc, norm_stem))

print(f"\nUnmatched: {len(unmatched)}")
for fpath, stem, nc, norm_stem in unmatched:
    print(f"  [{nc} notes] {stem}")
    print(f"    norm: {norm_stem}")
    print(f"    path: {fpath}")

if not unmatched:
    print("\n[SILENT] All books with noteCount>0 have entity pages.")
else:
    print(f"\n{len(unmatched)} books need entity pages created.")
