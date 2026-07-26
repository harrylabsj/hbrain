import os, re

raw_dir = '/Users/jianghaidong/hbrain/raw/微信读书'
entities_dir = '/Users/jianghaidong/hbrain/llm-wiki/entities'

# Get entity stems (all variants)
entity_stems = set()
for fname in os.listdir(entities_dir):
    if fname.endswith('.md'):
        stem = fname[:-3]
        entity_stems.add(stem)
        norm = stem
        for ch in '：：:': norm = norm.replace(ch, '-')
        for ch in '?？ ': norm = norm.replace(ch, '-')
        while '--' in norm: norm = norm.replace('--', '-')
        norm = norm.strip('-')
        entity_stems.add(norm)
        bare = stem.replace('《', '').replace('》', '')
        entity_stems.add(bare)
        bare_norm = norm.replace('《', '').replace('》', '')
        entity_stems.add(bare_norm)

# Scan raw files
unmatched = []
total_with_notes = 0
for fname in sorted(os.listdir(raw_dir)):
    if not fname.endswith('.md'):
        continue
    fpath = os.path.join(raw_dir, fname)
    with open(fpath, 'r') as f:
        content = f.read(2000)
    m = re.search(r'noteCount:\s*(\d+)', content)
    if not m:
        continue
    note_count = int(m.group(1))
    if note_count == 0:
        continue
    total_with_notes += 1

    stem = fname[:-3]
    norm_stem = stem
    for ch in '：：:': norm_stem = norm_stem.replace(ch, '-')
    for ch in '?？ ': norm_stem = norm_stem.replace(ch, '-')
    while '--' in norm_stem: norm_stem = norm_stem.replace('--', '-')
    norm_stem = norm_stem.strip('-')
    bare = stem.replace('《', '').replace('》', '')
    bare_norm = norm_stem.replace('《', '').replace('》', '')

    if stem in entity_stems or norm_stem in entity_stems or bare in entity_stems or bare_norm in entity_stems:
        continue

    unmatched.append((note_count, stem, fpath))

unmatched.sort(key=lambda x: -x[0])
print(f'Total raw files with noteCount>0: {total_with_notes}')
print(f'Unmatched (no entity page): {len(unmatched)}')
print()
for nc, stem, fpath in unmatched:
    print(f'noteCount={nc:4d} | {stem}')
