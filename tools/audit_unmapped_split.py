#!/usr/bin/env python3
"""Audit WeRead raw files in hbrain split-path vault."""
import os, re, sys, json

def normalize(s):
    s = os.path.splitext(os.path.basename(s))[0]
    for ch in '：：:': s = s.replace(ch, '-')
    for ch in '?？ ': s = s.replace(ch, '-')
    s = s.replace('《', '').replace('》', '')
    s = s.replace('\u2014', '').replace('\u2013', '')
    s = re.sub(r'-*\[[美英法日德俄意西荷瑞印巴韩]\]', '-', s)
    while '--' in s: s = s.replace('--', '-')
    return s.strip('-').lower()

def parse_raw_frontmatter(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    meta = {'path': path, 'filename': os.path.basename(path)}
    fm_match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).split('\n'):
            if ':' in line:
                key, _, val = line.partition(':')
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                meta[key] = val
    title_match = re.search(r'书名：\s*(.+)', content)
    if title_match:
        meta['title'] = title_match.group(1).strip()
    meta['noteCount'] = int(meta.get('noteCount', 0))
    meta['reviewCount'] = int(meta.get('reviewCount', 0))
    meta['has_highlights'] = bool(re.search(r'# 高亮划线\n', content))
    return meta

# Hbrain split paths
raw_dirs = [
    '/Users/jianghaidong/hbrain/raw/微信读书',
    '/Users/jianghaidong/hbrain/llm-wiki/微信读书',
]
entity_dir = '/Users/jianghaidong/hbrain/llm-wiki/entities'

# Collect entity files
entity_files = set()
if os.path.isdir(entity_dir):
    for f in os.listdir(entity_dir):
        if f.endswith('.md'):
            entity_files.add(os.path.splitext(f)[0])

# Scan raw files
raw_files = []
for raw_dir in raw_dirs:
    if os.path.isdir(raw_dir):
        for f in os.listdir(raw_dir):
            if f.endswith('.md'):
                meta = parse_raw_frontmatter(os.path.join(raw_dir, f))
                raw_files.append(meta)

raw_files.sort(key=lambda x: x['noteCount'], reverse=True)

# Find unmatched: noteCount > 0 and no entity page
unmatched = []
for meta in raw_files:
    if meta['noteCount'] == 0:
        continue
    raw_stem = os.path.splitext(meta['filename'])[0]
    norm_stem = normalize(raw_stem)

    match = norm_stem in entity_files
    if not match:
        for ef in entity_files:
            nf = normalize(ef)
            if norm_stem == nf or norm_stem in nf or nf in norm_stem:
                match = True
                break

    if not match:
        unmatched.append({
            'filename': meta['filename'],
            'title': meta.get('title', raw_stem),
            'author': meta.get('author', ''),
            'noteCount': meta['noteCount'],
            'bookId': meta.get('bookId', ''),
            'norm_stem': norm_stem,
            'path': meta['path'],
        })

print(f"Raw sources scanned: {raw_dirs}")
print(f"Total raw files: {len(raw_files)}")
print(f"Raw files with noteCount>0: {sum(1 for m in raw_files if m['noteCount'] > 0)}")
print(f"Entity pages: {len(entity_files)}")
print(f"Unmatched books: {len(unmatched)}")
print()

if unmatched:
    for i, b in enumerate(unmatched):
        print(f"{i+1}. [{b['noteCount']} notes] {b['title']}")
        print(f"   Author: {b['author']}, Raw file: {b['filename']}")
        print(f"   Norm: {b['norm_stem']}")
        print()

# Always exit 0
sys.exit(0)
