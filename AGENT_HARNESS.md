# KamaClaude Agent Harness

这里的 Harness 指模型外部的生产运行系统，而不是测试框架。它负责把模型、上下文、
工具、权限、执行环境、会话状态和失败恢复组合成一个可运行的 Agent。

## 运行入口

生产入口是 `kama_claude.core.harness.AgentHarness`。Core daemon 为每个任务或会话创建
一个 Harness，并由它驱动完整生命周期：

```text
用户消息 / 任务
       ↓
AgentHarness
  ├─ Session 与追加式事件记录
  ├─ System Prompt + 全局/项目/运行时上下文
  ├─ AgentLoop 与模型流式调用
  ├─ ToolRegistry + PermissionManager
  ├─ Sandbox 后端 + sandbox_info
  ├─ Compactor 与长期 Notes
  ├─ VerificationController
  └─ Subagent 调度
       ↓
最终结果 + 可回放事件 + Git/验证证据
```

`AgentRunner` 作为旧名称暂时保留兼容性；新代码应该使用 `AgentHarness`。

## Sandbox 如何进入 Harness

Harness 在每次运行前将安全的环境摘要放进 runtime context，并注册只读
`sandbox_info` 工具。普通运行不需要先探测容器；只有命令缺失、依赖缺失、权限、
网络、架构或 OOM 等证据出现时，Agent 才应进一步调用该工具。这样既能感知环境，
又不会让环境探测污染每个任务的上下文。

Harness 不向 Agent 暴露 Docker Socket，也不会让模型自行重建镜像。镜像变更仍由用户或
CI 完成；Agent 负责发现镜像可能过期并给出明确建议。

## 与 Evaluator 的区别

`kama eval` 是外部 Evaluator：它创建隔离任务、运行 AgentHarness、执行可信检查并统计
行为。Evaluator 可以测试 Harness，但它本身不是生产 Harness。代码中的新名称是
`EvaluationRunner`；`EvaluationHarness` 仅作为兼容别名保留。

