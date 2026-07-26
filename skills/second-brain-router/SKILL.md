---
name: second-brain-router
description: Standalone lightweight router for a personal second-brain loop. Use when the user asks knowledge, judgment, writing, reflection, decision, AI-agent, product, learning, or long-term thinking questions. It maps the question to existing knowledge pages, recalls local knowledge and experience context, answers, and optionally writes back to the right wiki layer.
license: MIT
metadata:
  hermes:
    tags: [second-brain, router, knowledge-pages, gbrain, cass, wiki]
    category: knowledge-management
---

# Second Brain Router

## Purpose

This is a standalone lightweight trigger skill for a personal second brain.

It routes a user question through existing knowledge pages, local search, and experience memory without loading a large wiki-maintenance skill by default. Cognitive anchors are recalled only when the user explicitly wants 人脑 training or when a user-reviewed hot-knowledge page has been promoted.

## Expected Vault Shape

Assume a Markdown wiki or vault with these optional paths:

- `_meta/my-core-questions.md` — long-term questions
- `_meta/anchors/index.md` — generated cognitive anchor dashboard
- canonical pages in `concepts/`, `entities/`, `queries/`, or `practices/` with `anchor:` frontmatter
- `concepts/` — stable concepts
- `queries/` — reusable answers and saved question explorations
- `practices/` — action routines and workflows
- `_meta/automation-runs/` — automation reports

For Dongge's machine, default paths are:

- Hbrain repo: `/Users/jianghaidong/hbrain`
- Wiki root: `/Users/jianghaidong/hbrain/llm-wiki`
- Core questions: `/Users/jianghaidong/hbrain/llm-wiki/_meta/my-core-questions.md`
- Anchor dashboard: `/Users/jianghaidong/hbrain/llm-wiki/_meta/anchors/index.md`

If these paths do not exist, infer the local wiki root from the current project or ask one concise question.

## Trigger Classifier

Classify each user request:

1. `ordinary`: answer normally; do not run the second-brain loop.
2. `second-brain-needed`: run the lightweight second-brain loop.
3. `writeback-needed`: run the lightweight loop, then write back only if the user explicitly requested saving/updating.

Use `second-brain-needed` when the user asks about:

- understanding, judgment, strategy, reflection, decision, writing, output, or review
- AI agents, coding agents, second brain, personal knowledge systems, memory, CASS, Gbrain, wiki
- cognition, metacognition, long-termism, compounding, feedback loops, self, no-self, meaning
- any long-term core question in the user's vault

Stay `ordinary` for simple commands, transient facts, basic calculations, or code tasks unrelated to the knowledge system.

Use `writeback-needed` only when the user says or clearly implies: save, write to wiki, update anchor, create page, preserve this, 沉淀, 保存, 写入 wiki, 更新锚点, 创建页面, 整理到第二大脑.

## Lightweight Loop

For `second-brain-needed` and `writeback-needed` requests:

1. Read only lightweight routing files when they exist:
   - `_meta/my-core-questions.md`
   - `_meta/anchors/index.md`
2. Route the question to existing knowledge pages; do not use the removed `links/` layer as fallback.
   - When available on Dongge's machine, run:
     `python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --query "<user question>"`
   - Route matching uses page title, headings, aliases, triggers, and local wiki text.
   - Route without recording by default. A route hit is grounding, not automatically an anchor-event.
   - At real session closeout, rerun relevant hits with `--record` so they enter `_meta/knowledge-events/YYYY-MM.jsonl` as hot-knowledge scoring evidence.
   - If the first dry route has zero relevant matches, and the query is genuinely from the user's 人脑 rather than an agent-generated task label, rerun with `--record --miss` to log a knowledge miss under `_meta/knowledge-events/knowledge-misses/YYYY-MM.jsonl`.
   - Knowledge misses are new-domain candidates for `concept` / `query` / `practice` pages, not cognitive-anchor candidates. Hot knowledge that appears in `knowledge-candidates` can later be reviewed by the user for possible anchor promotion.
3. Read only the selected knowledge pages. Read canonical `anchor:` pages only when the answer needs an active anchor capsule or the user explicitly asks to internalize/train it.
4. Run focused recall when available:
   - `gbrain search "<user question>" --limit 5`
   - `gbrain search "<knowledge page or anchor name>" --limit 5`
   - `cm context "<user question>" --json --limit 5 --history 5`
5. If a recall tool fails or is unavailable, continue and state the failure briefly.
6. Answer using:
   - the user's 人脑 framing
   - the matched knowledge page or, when selected, the anchor minimal model
   - retrieved local context
   - CASS/experience lessons when available
7. Decide whether to write back or propose a writeback.

Do not read the whole wiki index, full log, full schema, large references, or raw sources during the lightweight loop.

## Minimal Anchor Template

When explicitly creating or normalizing an anchor, use:

```markdown
---
title: Anchor Name
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: concept
tags: [anchor, contact-point]
sources: []
aliases: []
triggers: [real phrase 1, real phrase 2]
core_questions: [Q1]
anchor: candidate
triggers: [real phrase 1, real phrase 2]
---

# Anchor Name

## TRIGGER

## 一句话

## 最小模型

## 为什么成立

## 关键区分

## 连接到第二大脑

## 常用提问

## 下一步要补的连接
```

## Writeback Rules

Default to no file edits.

Fast/slow loop rule:

- Fast loop, during a conversation: route knowledge pages, recall, answer, and create `_meta/writeback-inbox/` proposals only. Route closeout writes knowledge-events / knowledge-misses; it does not write anchor-events.
- Slow loop, daily/weekly: review inbox, knowledge-misses, and hot knowledge. Promote accepted material into `concepts/`, `entities/`, `queries/`, or `practices/`. Use `anchor:` only after user review says a hot or explicitly nominated page should become a trainable cognitive entry point.
- Append anchor-events only for human-side training signals: recall-check, explicit human nomination, or a direct request to internalize a specific anchor.
- Do not directly write durable wiki正文 during a normal conversation unless the user explicitly asks for that exact file edit.

If the user explicitly asks for writeback, first choose the target layer:

- canonical `concepts/` / `entities/` / `queries/` / `practices/` pages with `anchor:` only for explicitly nominated or governance-backed cognitive anchors
- `concepts/` for stable ideas
- `queries/` for reusable answers
- `practices/` for action routines
- `_meta/automation-runs/` for automation reports
- CASS/playbook only for repeated agent workflow lessons

Keep writeback small and auditable:

1. Prefer creating a writeback-inbox proposal with `provenance` and `judgment_changed`.
2. If the user explicitly requested direct editing, read the target file if it exists.
3. Preserve existing content and frontmatter.
4. Add only the smallest durable update.
5. Bump `updated` dates when editing wiki pages.
6. Add or propose an index/log update when the local wiki convention requires it.

Writeback discriminator:

- Prefer preserving changes in the user's judgment, stance, tradeoff, or rejection of an alternative.
- Do not preserve generic model summaries just because they sound complete.
- New durable pages should include `provenance: 人脑原创 | 对话合成 | AI生成`.
- New durable pages should link to at least 2 existing pages; otherwise keep them in inbox.
- Knowledge deposition must not route, append anchor-events, or add `anchor:` merely because the material is valuable.

Never delete, rename, migrate, publish, send external messages, edit raw source files, or handle sensitive raw data without explicit user confirmation.

## Routing Receipt

For every `second-brain-needed` or `writeback-needed` answer, include a concise receipt:

```text
第二大脑回路：
- 模式：second-brain-needed / writeback-needed
- 长期问题：Q?
- 知识点：[[concepts/...]], [[queries/...]]
- 锚点：无 / [[concepts/...]]
- 召回：Gbrain / CASS / anchor-only / unavailable
- 写回：不需要 / 建议 / 已按要求写入
```

If the request is `ordinary`, do not include the receipt unless the user asks whether the loop was used.

## Anti-Patterns

- Do not load a heavy wiki-maintenance skill by default.
- Do not do full-vault scans for ordinary questions.
- Do not create wiki pages just because a question is interesting.
- Do not create anchors just because a page was useful; anchors are promoted from user-reviewed hot knowledge or explicit 人脑 training demand.
- Do not use `route --record` for agent-internal prompts, AI task descriptions, or ordinary deposition.
- Do not hide recall failures; short transparency is better than pretending the loop ran.
- Do not answer as generic internet knowledge when a relevant anchor exists.
