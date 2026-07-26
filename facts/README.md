# Hbrain Fact Ledger

`facts/` 是海东认知系统的追加式事实域，回答“什么在什么时候发生了”。它位于 `llm-wiki` 之外，不参与第二大脑启动上下文，也不自动进入 Gbrain 语义召回。

目录：

```text
facts/
  schema/                 # 事实事件契约
  events/YYYY-MM.jsonl    # 正式事实，只追加
  inbox/YYYY-MM-DD.jsonl  # 待审事实提案
  projections/daily/      # 可重建的日报
```

CLI：

```bash
python3 /Users/jianghaidong/hbrain/haidong-os/automation/fact_ledger.py --help
```

边界：不自动读取 Logseq、聊天、健康或关系原文；不自动修改项目状态；不把事实自动晋升为知识、CASS 成熟经验或人脑锚点。
