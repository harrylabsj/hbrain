---
title: 五域架构阶段 4 日报与学习桥接入报告
created: 2026-07-26
updated: 2026-07-26
type: implementation-report
status: canary
tags: [haidong-os, five-domain, daily-report, learning-bridge]
---

# 五域架构阶段 4：日报与学习桥

## 当前交付

- 新增 `automation/five_domain_daily.py`，只用标准库编译五域日报。
- 默认报告前一自然日；支持显式日期、各域根目录、输出位置、`--no-write` 和 JSON 摘要。
- 事实域只读取当月 Fact Ledger 和当日事实提案。
- 项目域只选取由当日事实支撑的已应用 Project Registry 变更。
- 知识域只读取当月 learning/event/miss 元数据，不扫描规范知识页正文。
- 经验域只汇总当日 completion receipt 中的 `experience_candidate`。
- 证据域只汇总 receipt evidence 和事实 `source_ref`。
- 每域最多展示 20 条；坏 JSON 计入 issues；秘密样式内容脱敏；输出叶子软链接被拒绝。
- 新增 `automation/experience_review.py`：将 receipt 经验候选编译为独立、追加式待审事件；只保留候选与最小来源，不复制 action/result/query。
- 经验事件固定 `status: inbox`、`auto_promote: false`、`cass_write: false`；没有 review/apply/promote 命令，不访问 CASS。
- daily reliability runner 已按固定顺序接入：认知维护 → 经验候选编译 → 五域日报 → 项目低影响提案编译；之后才执行 3 个可选 Gbrain 索引步骤。
- 新增 `automation/project_change_compiler.py`：只把当日已验证、明确归属已注册项目的事实编译为低影响 Project Registry 提案（`last_fact_id`、`last_reviewed_at`），不自动应用高影响字段。
- 新增经验候选人工复核状态：候选事件保持不可变，复核决定追加到 `experience-review/reviews/YYYY-MM.jsonl`，支持 `accept/reject/defer`、复核人、理由和可复用性；`accept + reusable` 只生成 `cass_recommendation`，不写 CASS、不自动晋升。

## 不变量

- 日报是可重建派生视图，不是新的事实源。
- `proposal_only: true`，`auto_promote: false`。
- 不修改 Fact Ledger、Project Registry、Wiki 规范页或 CASS。
- 不创建人脑 anchor-event，不修改 `weight`、`last_active` 或 `hot`。
- Agent 仍按需调用记忆；日报生成不等于把日报预载进 Agent 上下文。

## 验收证据

- 五域日报专项 8 项、经验队列专项 24 项；完整自动化测试 122 项通过。
- 首份当日 canary：[2026-07-26 五域日报](reports/five-domain-daily/2026-07-26-五域日报.md)。
- canary 最新重建汇总：正式事实 8、项目变化 7、知识候选 1、知识调用 16、知识缺口 9、证据 21、经验候选 1。
- 数据问题 0，秘密脱敏 0，未发生自动晋升。
- Claude-Kimi 完成实现和自审；Codex App 独立复跑完整测试及真实数据 dry-run/write canary。
- 经验候选正式 inbox：1 条事件、0 个问题；重复编译新增 0，幂等通过。
- `automation-daily.zsh` 真实路径 dry-run：7 个步骤全部 `planned`、`attempts=0`、无 history，确认未执行维护或 Gbrain。
- 首次真实前一日 daily runner 已完成：7/7 步骤 `ok`，history=`/Users/jianghaidong/.hermes/state/hbrain-automation/history/daily/20260726T075333-8f8e04d8.json`；经验候选 0、新增项目提案 0、日报数据问题 0，Gbrain sync/embed/health 均成功。

## 当前状态

阶段 4 继续处于 canary。五域日报、经验候选待审队列、daily runner 编排、事实到低影响项目提案编译器、首次真实前一日运行观察和人工复核状态最小切片已经完成；尚未建立多次现实验证后进入知识/CASS 写回闸门的晋升流程。

## 下一步

1. 复核 `project_change_compiler.py` 生成的低影响提案，不自动应用高影响字段。
2. 观察人工复核记录的重复、跨任务复用证据；只有满足条件时才建议进入 CASS。
3. 为知识/经验晋升建立统一 review 状态，不直接写规范知识或成熟 playbook。
