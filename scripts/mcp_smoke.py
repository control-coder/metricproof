"""MCP 冒烟：以 stdio 启动 datalab.api.mcp_server，列出工具并读取一个已有调查任务。

前提：本机接口已在 http://127.0.0.1:18080 运行（scripts/serve.py）。只读调用，不提交任务、不调用模型。

    python -B scripts/mcp_smoke.py <project_id> <investigation_task_id>
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(project_id: str, task_id: str):
    server = StdioServerParameters(command=sys.executable, args=['-B', '-m', 'datalab.api.mcp_server'])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = sorted(tool.name for tool in (await session.list_tools()).tools)
        print('工具：', '、'.join(tools), flush=True)
        for name in ('submit_investigation', 'get_investigation'):
            assert name in tools, f'缺少工具 {name}'
        result = await session.call_tool('get_investigation', {'project_id': project_id, 'task_id': task_id})
        assert not result.isError, result.content
        view = result.structuredContent or json.loads(result.content[0].text)
        view = view.get('result', view)
        print(json.dumps({key: view.get(key) for key in ('state', 'stop_reason', 'valid', 'usage', 'claims_issues')},
                         ensure_ascii=False), flush=True)
        for item in view.get('root_causes') or []:
            print('根因：', item.get('dimension'), item.get('value'), item.get('verification'), flush=True)
        # 非调查任务或不存在的任务应返回错误，而不是空结果。
        missing = await session.call_tool('get_investigation', {'project_id': project_id,
                                                                'task_id': '00000000-0000-4000-8000-000000000000'})
        print('不存在的任务返回错误：', missing.isError, flush=True)


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1], sys.argv[2]))
