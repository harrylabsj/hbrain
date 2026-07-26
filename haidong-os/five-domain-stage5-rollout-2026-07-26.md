---
title: 五域架构阶段 5 统一复核状态接入报告
created: 2026-07-26
updated: 2026-07-26
type: implementation-report
status: completed
tags: [haidong-os, five-domain, review-state, human-gate]
---

# 五域架构阶段 5：统一复核状态

## 当前交付

- 新增 `automation/review_state.py`，把知识、经验、项目、事实四个需要人工判断的入口归一成一个有界 Review State。
- `report` 默认只输出，不写文件；只有显式 `--output` 且未指定 `--no-write` 才原子写入派生报告。
- 知识域只读 knowledge-learning 元数据和 writeback frontmatter，不读 Wiki 正文。
- 经验域只读候选与领域复核记录；项目域只读提案与 applied audit；事实域只读提案和 disputed 事件。
- 统一复核记录追加到 `review-state/reviews/YYYY-MM.jsonl`，不修改任何领域源记录。
- 同一语义决定的 `review_id` 不包含时间戳，因此跨时间重试仍幂等；并发追加使用全局 flock、`O_APPEND`、`O_NOFOLLOW` 和 `fsync`。
- 统一复核与领域复核按真实、带时区的 `reviewed_at` 选择较新记录。
- reviewer、rationale、事实 refs 和所有输出证据都执行秘密检测或安全化；异常信息不回显秘密值。
- 加固 `project_change_compiler.py` 的 existing-proposal 快路径：现有提案必须与确定性预览的身份和安全字段完全一致，否则 fail closed，不覆盖。

## 不变量

- 第二大脑是按需调用的记忆系统；Review State 只聚合待审元数据，不预载知识库。
- `auto_promote`、`wiki_write`、`cass_write`、`project_apply`、`fact_append` 恒为 `false`。
- 不访问 `links/`，不调用 Gbrain/CASS，不创建 anchor-event，不修改 `weight`、`last_active` 或 `hot`。
- 报告和建议不是事实源；人工复核也只是意见记录，不等于晋升或应用。
- 任一输入损坏、字段缺失、秘密样式内容或不安全路径都会使报告 fail closed：`valid=false`，条目和建议为空。

## 验收证据

- Claude-DS 完成代码修正并运行全量自动化测试：216/216 通过。
- 新的隔离 Claude-DS 会话完成只读复审和重点回归：76/76 通过，结论 APPROVE。
- `git diff --check` 通过。
- 真实 Hbrain 只读 canary：报告有效，得到 3 个待审条目和 3 条建议，无文件写入。
- 严格 validate 首次识别到一条历史 knowledge-learning 元数据缺少 `schema_version`；完成最小 schema 迁移后复验 `valid=true`、0 issues。
- 首次完整人工复核 canary 发现：已写入 `audit/applied.jsonl` 的项目提案仍以 inbox 副本重复进入待审队列。现已改为先按 `proposal_id` 全量归一、核对 `project_id` 与 `changes`，再执行条目限额；默认报告抑制已应用副本，`--include-closed` 只保留一条 applied 记录，身份冲突则 fail closed。
- Claude-DS 为真实 canary 修复新增 5 项专项测试，修复后完整真实队列由错误的 13 项收敛为 5 项：知识 2、经验 1、项目 2；0 issues、无领域写入。

## 当前状态与下一步

阶段 5 的最小治理闭环已经完成：发现待审项、统一呈现、显式人工决定、追加式留痕，但不自动晋升。

下一阶段不应立即增加自动写入权限。先运行一个真实人工复核周期，观察待审数量、重复率、defer 原因和跨域冲突；积累证据后，再设计“已批准决定到各领域执行器”的独立 apply 层，并继续保留逐域权限、dry-run、审计和可回滚边界。
