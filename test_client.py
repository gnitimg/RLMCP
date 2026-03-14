import asyncio
import json
import os

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _tool_result_to_text(result) -> str:
    # CallToolResult.content is a list of content blocks; we extract joined text blocks
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return str(result)
    texts = []
    for c in content:
        if getattr(c, "type", None) == "text":
            texts.append(getattr(c, "text", ""))
    return "\n".join(t for t in texts if t)


async def main() -> None:
    # 启动并连接本地 LegalBrain MCP Server
    server = StdioServerParameters(command="python", args=["run.py"], env=dict(os.environ))
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            case_description = (
                os.getenv("CASE")
                or "\u67d0\u4eba\u5229\u7528\u804c\u52a1\u4e4b\u4fbf\uff0c\u5c06\u516c\u53f8\u8d44\u91d150\u4e07\u5143\u8f6c\u5165\u4e2a\u4eba\u8d26\u6237\u7528\u4e8e\u7092\u80a1\uff0c\u4e09\u4e2a\u6708\u540e\u5f52\u8fd8\u3002"
            )

            result = await session.call_tool("analyze_crime", {"case_description": case_description})
            # analyze_crime 返回 JSON 字符串，这里直接打印便于验收
            text = _tool_result_to_text(result)
            try:
                # 尝试按 JSON 美化打印（便于验收）
                print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
            except Exception:
                print(text)


if __name__ == "__main__":
    asyncio.run(main())

