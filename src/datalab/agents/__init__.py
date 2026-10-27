"""Function Calling 工具层与最小 Agent 循环（A4）。

与旧的 roles/ 三角色流水线并存：旧路径（ModelBackend + Workflow）不改行为。
本包只提供通用机制——工具注册表（tools）、最小循环（loop）、离线录制回放（cassette）；
计划、分派、并行与上下文压缩由 A5 在 loop 的钩子上实现。
"""
