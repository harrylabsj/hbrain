---
name: hbrain-cognitive-loop
description: Close the Hbrain second-brain loop across knowledge deposition, human-side cognitive-anchor training, writeback proposals, daily/weekly maintenance, and monthly anchor lifecycle governance. Use when working on 认知锚点, 第二大脑, Hbrain/wiki writeback, anchor-events, knowledge-events, knowledge-misses, writeback-inbox, anchor capsules, or second-brain automation.
---

# Hbrain Cognitive Loop

Default to Chinese for user-facing summaries. Keep edits low-risk and auditable.

## Highest Doctrine

Keep two pipelines separate:

- Knowledge deposition: turn conversations, raw material, and reading notes into durable `concepts/`, `entities/`, `queries/`, or `practices/` pages. Do not route, append anchor-events, or add `anchor:` just because a page was written.
- Anchor training: train the user's 人脑 on a tiny subset of pages only when there is demand evidence: hot knowledge repeatedly used across days and user-reviewed for promotion, repeated recall failure/usage, or explicit human nomination.

AI may expand the user's cognitive bandwidth, but it does not replace the user's 人脑 judgment.

## Roots

- Hbrain repo: `/Users/jianghaidong/hbrain`
- Wiki root: `/Users/jianghaidong/hbrain/llm-wiki`
- Anchor dashboard: `/Users/jianghaidong/hbrain/llm-wiki/_meta/anchors/index.md`
- Anchor events: `/Users/jianghaidong/hbrain/llm-wiki/_meta/anchor-events/YYYY-MM.jsonl`
- Knowledge events: `/Users/jianghaidong/hbrain/llm-wiki/_meta/knowledge-events/YYYY-MM.jsonl`
- Knowledge misses: `/Users/jianghaidong/hbrain/llm-wiki/_meta/knowledge-events/knowledge-misses/YYYY-MM.jsonl`
- Legacy anchor router misses: `/Users/jianghaidong/hbrain/llm-wiki/_meta/anchor-events/router-misses/YYYY-MM.jsonl`
- Writeback inbox: `/Users/jianghaidong/hbrain/llm-wiki/_meta/writeback-inbox/`
- Anchor governance: `/Users/jianghaidong/hbrain/llm-wiki/_meta/anchor-governance/`

## Modes

- `route`: semantic-route a real question to existing knowledge pages for hot-knowledge scoring, not to cognitive anchors. Run without recording when inspecting hits.
- `recall`: read anchor pages and run focused `gbrain search` / `cm context` when useful.
- `closeout`: split knowledge deposition from anchor training; create writeback proposals for durable value without logging anchor usage unless there is a human-side training signal.
- `govern`: audit anchor lifecycle, inbox backlog, cron health, and event-derived metrics.

## Event Log

Use event logs as the source of truth for human-side anchor demand and recall training, not for AI material usage. Record only:

- Morning recall / `recall-check` results.
- Explicit human call, nomination, or "我要内化这个" instruction for a specific trainable anchor.

Do not record:

- AI using a page as background material.
- Knowledge deposition or writeback itself.
- Agent-generated task descriptions routed for its own convenience.

Schema:

```json
{"date":"YYYY-MM-DD","anchor":"认知锚点","source":"human-nomination","action":"used","weight_delta":1,"summary":"short reason"}
```

Use the bundled helper:

```bash
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py ensure
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --query "<human-source question>"
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --record --record-slug "<accepted knowledge slug>" --query "<human-source question>"
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --query "<human-source knowledge miss>" --record --miss
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py record --anchor "认知锚点" --summary "human explicit nomination" --source human-nomination
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py recall-check --anchor "认知锚点" --response "<human recall answer>"
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py morning-recall
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py morning-recall-review --latest-before-today --apply-frontmatter
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py summarize --days 7
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py knowledge-dashboard
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py knowledge-candidates
```

Before recording, first run `route` without `--record` and inspect whether the hits cover the question. If hits are relevant, rerun with `--record` plus one `--record-slug` for each accepted slug; this records the reviewed pages exactly and avoids ranking drift between two backend searches. If hits are empty or off-point, rerun with `--record --miss`. Backend failures abort and record nothing. Only canonical knowledge layers (`concepts/`, `entities/`, `practices/`, `queries/`, `comparisons/`) are eligible; backups, archives, action pages, and publishing drafts are excluded from knowledge scoring.

## 会话收尾仪式（route 收尾）

每次真实思考会话（与用户对话或独处沉思）结束时，必须运行 route 完成收尾——这是把"活思考"接进第二大脑闸门的关键动作，不是 cron 自动跑的。

```bash
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --query "<本次会话核心问题>"
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --record --record-slug "<已核验的知识 slug>" --query "<本次会话核心问题>"
```

- **命中**：从 dry-run 结果中显式传入已核验的 `--record-slug`；知识点写入 `_meta/knowledge-events/<月>.jsonl`，+1 计分，积累热知识。
- **未命中**（返回的 hits 实际未覆盖本次主题）：改加 `--miss`，记入 `_meta/knowledge-events/knowledge-misses/` 作为新领域候选，提示该新建 `concept` / `query` / `practice` 页面。

> ⚠️ route 收尾必须在会话末**手动执行**，否则引擎空转——思考发生了，但知识未被接进系统。

周期回顾：

- route 优先使用 Gbrain；如果 Gbrain CLI 在脚本子进程里失败，会退回本地 wiki 词法扫描，避免把后端故障记成 miss。
- 运行 `knowledge-dashboard` 看哪些知识点被反复激活（热知识）。
- 运行 `knowledge-candidates` 查看是否有知识点在 ≥3 个不同日被激活，够格晋升为认知锚点候选。

## Anchor Capsule

An anchor page may be long, but the trainable anchor is the capsule at the top. Use this shape when creating or normalizing anchor material:

```md
## TRIGGER
When should this light up?

## 一句话
The gist that can regenerate the rest.

## 最小模型
No more than 4 chunks.

## 为什么成立
One line that lets the user rebuild it.

## 关键区分
X is not Y.
```

Morning recall and recall-check test the capsule, not the full page. If `最小模型` cannot stay within 4 chunks or 5 lines, compress it or split the anchor.

## Anchor Lifecycle

Allowed canonical `anchor` roles: `candidate`, `active`, `merge-candidate`, `retired`. `retired` means graduated into 人脑 memory, not deprecated. `hot`, `weight`, and `last_active` are derived in `_meta/anchors/index.md`.

Weekly governance identifies high-frequency-but-thin, long-dormant, duplicate-like, and output-ready anchors. Monthly governance generates at most one merge/retire review per month (proposals only, no direct modifications). Knowledge misses (>=3 sharing a theme) become new-domain candidates for concept/query/practice pages, not cognitive-anchor candidates. Hot knowledge in `knowledge-candidates` can be reviewed by the user for possible anchor promotion. New pages do not become candidates merely because they were written.

Helper commands:
```bash
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py govern-weekly --days 7
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py govern-monthly
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py governance-audit
```

## Gbrain Health Diagnostics

When running `gbrain health`, the score is typically 4–7/10. The real quality metric is `embed_coverage` (>98% = healthy, <95% = investigate). High orphan page counts (2000–4000) and 0% entity link/timeline coverage are expected for this monorepo — do not flag them as issues. After `gbrain sync`, a note about un-extracted edges is a recurring maintenance cue, not an error. See `references/gbrain-health-diagnostics.md` for full interpretation.

## Automation Upgrade

Keep deterministic mechanics in scripts. Hermes cron orchestrates commands and writes reports; it does not hand-edit weights, `last_active`, or lifecycle status from prompt prose.

## Agent Auto-Learning Gate

The second brain is an on-demand memory system, never startup context. At Agent/session startup, do not read or inject wiki pages, anchor indexes, core questions, search summaries, CASS history, or prior recall results. Keep only the short routing protocol and tool entrypoints in persistent prompts. First decide from the current task whether long-term memory is needed; for substantive knowledge work that truly needs it:

1. Search Top 5 only after the need is established, then open only the best 1–3 pages. Search hits are routing candidates, not content to preload wholesale. Keep the initial recall around 12k tokens and expand only when evidence is insufficient.
2. If the hits actually cover the question, record only the reviewed slugs as knowledge-events.
3. If the hits are empty or off-point, record a knowledge-miss, research only that gap, then pass the source-backed result through:

```bash
python3 /Users/jianghaidong/hbrain/haidong-os/automation/knowledge_growth.py capture --help
```

The gate may create a retrievable `learning-candidate` only for low-risk, confidence >= 0.80, valid official/local/user evidence, at least two existing canonical links, and a new target path. Otherwise it creates a writeback proposal. It never overwrites canonical knowledge or writes anchor-events.

Daily reporting is deterministic and defaults to the previous natural day:

```bash
python3 /Users/jianghaidong/hbrain/haidong-os/automation/knowledge_growth.py daily-report
```

It writes `_meta/daily-reports/YYYY-MM-DD-第二大脑日报.md`. Candidate knowledge may be recalled, but important decisions must verify its source, date, and confidence first.

## Mechanism Gates

- B1 output check: anchor recall tested with `recall-check`.
- B2 anchor budget: canonical `anchor: active <= 30`.
- B3 explicit triggers: route real questions through knowledge pages before deposition. Knowledge hits go to `knowledge-events`; off-point/empty hits go to `knowledge-events/knowledge-misses`; neither path writes anchor-events or trains 人脑 anchors by itself.
- B4 blocker escalation: reports surface stuck blockers.
- B5 writeback discriminator: proposals carry provenance and judgment_changed.
- B6 fast/slow loop: conversation closeout can create proposals; durable promotion happens in review. Knowledge deposition never automatically creates anchor-events or `anchor:` fields.
- B7 connection density: new pages link to >= 2 existing pages.
- B8 judgment log: stance, confidence, review date.
- C governance: freeze new anchors unless event-backed; priority orphans in concepts/queries/canonical anchors; path casing lint; model-agnostic storage.

## Safety

Allowed without confirmation: create _meta/anchor-events/, _meta/knowledge-events/, writeback-inbox/, anchor-governance/; append human-side JSONL; create proposals and automation reports; run gbrain commands and cm context.
Require confirmation: deleting, renaming, merging, retiring, migrating; editing raw source; publishing external messages; handling sensitive raw data.
