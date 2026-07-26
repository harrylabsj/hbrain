---
title: 海东认知运行时协议
created: 2026-07-26
updated: 2026-07-26
type: practice
status: active
tags:
  - haidong-os
  - second-brain
  - agent-runtime
  - multi-agent
---

# 海东认知运行时协议

## 核心定义

Hermes、OpenClaw、Claude Code、Claude-Kimi、Claude-DS、Codex CLI、Codex App 和 Kimi Code 都是同一认知系统的执行节点，不各自建设平行的“第二大脑”。

系统有五个彼此分离的事实域，海东操作系统位于其上作为控制平面：

| 事实域 | 权威来源 | 负责内容 |
|---|---|---|
| Knowledge | `/Users/jianghaidong/hbrain/llm-wiki` | 可跨情境复用的概念、实体、问题和实践 |
| Experience | CASS | Agent 反复验证过的程序性经验 |
| Project | `/Users/jianghaidong/hbrain/haidong-os/projects` | 项目目标、当前状态、下一步和决策引用 |
| Fact | `/Users/jianghaidong/hbrain/facts` | 已发生、可追溯、追加式事实事件 |
| Artifact | 当前项目仓库或源系统 | 代码、测试、文件、截图和原始交付证据 |

海东操作系统决定当前 Phase、唯一结果和优先级，但不复制五域正文。跨域通过稳定 ID 和来源引用连接。

各 Agent 的 `MEMORY.md`、会话历史和内部记忆只用于短期连续性，不替代上述事实域。长期结论必须进入 Hbrain，行动状态必须进入海东操作系统，代码事实必须以项目仓库为准。

## 每个重要会话的固定闭环

### 1. 对齐结果

新建重要会话时，先读：

- `/Users/jianghaidong/hbrain/haidong-os/海东操作系统.md`
- 与当前 Phase 对应的行动清单

先回答：

1. 当前 Phase 唯一要生成的现实结果是什么？
2. 本任务与该结果是什么关系：直接推进、底盘保障、维护，还是明确的旁支？
3. 本轮完成证据是什么？

简单问答、格式转换和纯机械命令可以跳过完整对齐，但不得悄悄改变 Phase、优先级或长期判断。

### 2. 调用第二大脑

需要事实、历史经验或长期判断时，优先运行：

```bash
gbrain search "<问题>" --source hbrain --limit 5
```

涉及长期战略、第二大脑或认知锚点的广泛回忆前，再读：

- `/Users/jianghaidong/hbrain/llm-wiki/_meta/anchors/index.md`
- `/Users/jianghaidong/hbrain/llm-wiki/_meta/my-core-questions.md`

项目代码、实时系统状态和用户刚提供的材料仍是对应任务的一手证据；不要为了“使用第二大脑”而用旧知识覆盖更新、更直接的事实。

#### 按需召回预算

第二大脑是按需调用的记忆系统，不是 Agent 的启动上下文。默认采用“零预载、先判定、后检索”的渐进式召回：

1. Agent 启动或新建会话时，不得读取或注入任何 `llm-wiki` 正文、anchors index、`my-core-questions.md`、搜索摘要、CASS 历史或其他第二大脑内容。
2. 全局提示、bootstrap、`MEMORY.md` 和上下文缓存只允许保留本协议的短入口与调用方法，不得固化第二大脑内容或上一次召回结果。
3. 先只根据当前任务判断是否确实需要长期记忆；简单问答、格式转换、机械任务和证据已充分的项目任务默认不召回。
4. 只有答案、判断或执行确实依赖历史知识时，才运行 `gbrain search` 或 route 获取 Top 5 候选摘要；搜索结果只用于路由，不等于全部加载。
5. 初次只打开最相关的 1–3 页，单轮知识召回软预算约 12k tokens；证据不足时才扩展下一批页面。
6. anchors index 与核心问题也只能在当前问题明确涉及长期战略或人脑锚点时按需读取，不能因“重要会话”而自动加载。
7. 不自动读取 `raw/`、日报归档、健康原始数据、邮件、聊天记录或其他敏感材料。

#### 五域路由与 context packet

Agent 启动时不运行任何检索。只有当前任务确实需要项目状态、事实、长期知识或执行经验时，才调用统一运行时：

```bash
python3 /Users/jianghaidong/hbrain/haidong-os/automation/five_domain_runtime.py classify \
  --query "<当前问题>"

python3 /Users/jianghaidong/hbrain/haidong-os/automation/five_domain_runtime.py context \
  --query "<当前问题>" --project-id "<project-id>"
```

默认 context 只读取显式指定的一个项目及最多 5 条相关事实，不调用知识域或 CASS。只有调用方明确需要时才增加：

```bash
--include knowledge
--include experience --cass-workspace /absolute/project/workspace
```

packet 上限为：1 个项目、5 条事实、5 条知识摘要、5 条经验摘要，默认约 12k tokens。模糊分类返回 `needs_review`，不得擅自回退为项目域；跨域候选只作为指针，不自动展开。

#### 自动知识缺口闸门

当知识会实质影响判断或执行结果时，Agent 自动检查覆盖度：

1. 对真实问题运行 route dry-run 或 `gbrain search`。
2. 命中页确实覆盖问题时，只读取最相关页面并记录核验后的 knowledge-hit。
3. 命中为空或实际偏题时，记录 knowledge-miss；随后仅围绕这个缺口研究，不扩展成无边界调研。
4. 研究完成后使用统一写入闸门：

```bash
python3 /Users/jianghaidong/hbrain/haidong-os/automation/knowledge_growth.py capture \
  --question "<真实问题>" \
  --title "<知识点标题>" \
  --summary "<一句话结论>" \
  --body-file "<研究摘要文件>" \
  --source "<来源>" --source-kind official-primary \
  --target-layer queries --confidence 0.85 --risk low \
  --link "concepts/已有知识A" --link "practices/已有知识B" \
  --agent "<agent-name>" --auto-promote --index
```

只有低风险、置信度不低于 0.80、来源类型可验证、至少连接 2 个既有规范知识页、且不存在同名页时，才自动新建 `status: learning-candidate`。`--index` 只增量导入这一页，不触发整库重建。其余情况进入 `_meta/writeback-inbox/`；任何自动学习都不得覆盖既有规范页。

`learning-candidate` 可以参与以后召回，但重要判断必须打开原页核验来源、日期和置信度。知识写入不会自动产生人脑 anchor-event。

### 3. 生成结果

固定链路：

> 三张力定方向 → 当前 Phase 只选一个结果 → Hbrain 提供证据 → 拆成下一动作 → 执行 → 留下可核验证据 → 现实反馈判定结果。

Agent 应报告真实完成状态，不把“命令运行过”“文档写了”或“模型回答了”当作现实结果。

### 4. 分域回写

- 可跨项目复用的知识写入 `llm-wiki/concepts/`、`entities/`、`queries/`、`practices/` 或 `comparisons/`。
- Agent 的重复程序性经验进入 CASS 候选，单次观察不得直接成为成熟规则。
- 项目当前状态通过 Project Registry proposal 更新，高影响字段保留人工批准。
- 已发生事项追加到 Fact Ledger；错误使用后续事件纠正，不改历史行。
- 项目实现、测试和原始交付物留在项目仓库或源系统。
- 未验证的外部材料和 Agent 输出不得直接升级为规范知识。
- 不创建或恢复旧 `links/` 层。

知识沉淀与人脑锚点训练必须分开：写了知识页、使用了知识页或完成了任务，都不会自动产生 `anchor-events`，也不会自动增加 `anchor:`。

#### Completion receipt

重要任务完成后，Agent 生成一个 JSON 文件并提交：

```bash
python3 /Users/jianghaidong/hbrain/haidong-os/automation/five_domain_runtime.py receipt \
  --receipt-file /caller/selected/completion-receipt.json
```

receipt 必须包含：`packet_id`、`agent`、`project_id`、`action`、`result`、非空 `evidence`、`knowledge_gap`、`experience_candidate` 和 `privacy`。它只幂等追加到 `haidong-os/receipts/inbox/`，固定为 `auto_promote: false`；不得直接修改 Fact Ledger、Project Registry、`llm-wiki` 或 CASS。阶段 4 的确定性编译器再把 receipt 转成各域提案。

### 5. 会话收尾

真实思考会话结束前先做 dry-run：

```bash
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --query "<本次核心问题>"
```

人工核验命中后再显式记录：

```bash
python3 /Users/jianghaidong/.agents/skills/hbrain-cognitive-loop/scripts/hbrain_loop.py route --query "<本次核心问题>" --record --record-slug "<已核验 slug>"
```

命中实际不覆盖问题时使用 `--record --miss`。route 只写知识事件或知识缺口，不写锚点事件。

## 隐私与权限

- 默认只检索规范知识层，不自动读取 `raw/`、健康原始数据、邮件、聊天记录或其他敏感材料。
- 群聊、共享会话和外部 Agent 不加载私人长期记忆；只提供完成当前任务所需的最小上下文。
- 对外发送、发布、不可逆操作以及健康、法律、财务高影响判断保留人工确认。
- 任何写回都要可追溯、可审计、可回滚；不得手工修改派生字段 `weight`、`last_active` 或 `hot`。

## Agent 适配

| Agent | 全局入口 |
|---|---|
| Codex CLI / Codex App | `~/.codex/AGENTS.md` |
| Claude / Claude-Kimi / Claude-DS | `~/.claude/CLAUDE.md` |
| Kimi Code | `~/.kimi-code/AGENTS.md` |
| OpenClaw | `~/.openclaw/AGENTS.md` 和各 Agent workspace 的 `AGENTS.md` |
| Hermes | `~/.hermes/SOUL.md` |

各入口只放稳定的短协议，详细规则以本页和 `hbrain-cognitive-loop` skill 为准。

## 每日第二大脑日报

每日确定性任务运行：

```bash
python3 /Users/jianghaidong/hbrain/haidong-os/automation/knowledge_growth.py daily-report
```

它默认报告前一自然日，写入 `llm-wiki/_meta/daily-reports/YYYY-MM-DD-第二大脑日报.md`，汇总：新增与更新的规范知识、Agent 自动学习候选、待审提案、knowledge-hit、knowledge-miss。日报只汇总事实，不调用模型，也不产生人脑锚点事件。

## 成功标准

一个 Agent 真正运行在海东认知系统中，不是因为它能访问目录，而是因为它能够：

1. 用当前 Phase 约束结果选择。
2. 在需要时召回 Hbrain，而不是凭空重建背景。
3. 用现实证据区分“执行过”和“完成了”。
4. 把长期知识、行动状态和项目交付写回正确事实域。
5. 在重要思考会话结束时完成 route 收尾，同时不污染人脑锚点事件。
6. 先做五域判定，再按需获取最小 context packet，并用 completion receipt 留下可编译的完成回执。
