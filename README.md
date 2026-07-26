# hbrain

海东第二大脑的可公开代码包：提供按需记忆运行时、五域认知系统自动化、事实账本、项目注册表、知识成长日报、经验审查和可验证的运行协议。

## 边界

本项目只发布可复用的代码、协议、schema、测试和合成示例。个人知识正文、Logseq/raw 数据、CASS 经验、事实与项目运行数据、日报、凭据和本机配置不在仓库内。第二大脑内容仍然遵循“按需调用”，不会被预加载到 Agent 上下文。

## 目录

- `haidong-os/automation/`：Python 标准库自动化与测试
- `haidong-os/agent-runtime-protocol.md`：Agent 运行与按需记忆协议
- `haidong-os/schema/`：运行回执契约
- `haidong-os/scripts/`：macOS/zsh 调度封装
- `facts/schema/`：追加式事实事件契约（不含事实数据）

## 快速验证

```bash
python3 -m unittest discover -s haidong-os/automation/tests -v
```

运行前请通过命令行参数或环境变量配置本机的 wiki、事实、项目、回执和工具路径。仓库中的默认路径仅用于海东本机部署示例，不会携带任何本机数据。

## 许可证

暂未指定许可证。未经许可不得将本项目作为闭源商业产品重新分发。
