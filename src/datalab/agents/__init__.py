"""Function Calling 工具层与 Agent 运行时（A4 通用机制 + A5 编排者-工作者）。

与旧的 roles/ 三角色流水线并存：旧路径（ModelBackend + Workflow）不改行为。
A4：工具注册表（tools）、最小循环（loop）、离线录制回放（cassette）。
A5：工具接线（toolset，接语义层与归因算法，含按引用核验）、主 Agent 提示（lead）、
子 Agent 提示（subagent）与调查编排（investigation：分派、并行、压缩、预算、trace）。
"""
