import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

# 初始化 MCP Server
mcp = FastMCP("LegalBrain")

NPC_FLAW_INDEX_URL = "https://flk.npc.gov.cn/index"


def _force_utf8_stdio() -> None:
    """
    Windows 下部分终端/宿主会以非 UTF-8 编码读写 stdio，导致 MCP JSON 消息乱码。
    MCP 标准要求使用 UTF-8，这里强制 reconfigure。
    """
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfig = getattr(stream, "reconfigure", None)
        if callable(reconfig):
            try:
                reconfig(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_stdio()


def _looks_like_legal_context(text: str) -> bool:
    if not text or not text.strip():
        return False
    keywords = (
        "罪",
        "刑法",
        "拘役",
        "有期徒刑",
        "罚金",
        "公安",
        "检察",
        "法院",
        "职务",
        "资金",
        "公款",
        "强奸",
        "杀人",
        "杀害",
        "幼童",
        "幼女",
        "交通事故",
        "肇事",
        "驾驶",
        "酒驾",
        "醉驾",
        "挪用",
        "侵占",
        "贪污",
        "受贿",
        "诈骗",
        "盗窃",
        "故意伤害",
        "抢劫",
        "合同",
        "劳动",
    )
    return any(k in text for k in keywords)


def _extract_candidate_queries(case_description: str) -> List[str]:
    """
    只产生“检索提示词”，不直接下结论，避免臆断罪名/条文。
    """
    text = case_description.strip()
    queries = [text]

    # 一些常见行为到“检索词”的弱映射：用于提高命中率，不作为定性依据。
    mapping: List[Tuple[re.Pattern[str], List[str]]] = [
        (re.compile(r"(公司|单位).*(资金|款)"), ["挪用资金罪", "职务侵占罪", "刑法 第二百七十二条", "刑法 第二百七十一条"]),
        (re.compile(r"(国有|国家工作人员|公款)"), ["挪用公款罪", "刑法 第三百八十四条"]),
        # 醉酒驾驶/酒驾（不强制要求出现“机动车”）
        (re.compile(r"(醉酒驾驶|醉驾|酒驾)"), ["危险驾驶罪", "刑法 第一百三十三条之一"]),
        # 交通事故肇事逃逸
        (re.compile(r"(交通事故).*逃逸"), ["交通肇事罪", "刑法 第一百三十三条"]),
        (re.compile(r"(盗窃|偷)"), ["盗窃罪"]),
        (re.compile(r"(诈骗|骗取)"), ["诈骗罪"]),
        (re.compile(r"(故意伤害|打伤)"), ["故意伤害罪"]),
        (re.compile(r"(抢劫)"), ["抢劫罪"]),
        # 性侵害相关
        (re.compile(r"(强奸|奸淫)"), ["强奸罪", "刑法 第二百三十六条"]),
        # 对幼童/幼女等弱势对象的提示词
        (re.compile(r"(幼童|幼女|不满十四周岁)"), ["强奸罪", "刑法 第二百三十六条"]),
        # 杀害行为
        (re.compile(r"(杀害|致死)"), ["故意杀人罪", "刑法 第二百三十二条"]),
    ]

    for pattern, extra in mapping:
        if pattern.search(text):
            queries.extend(extra)

    # 去重保序
    seen = set()
    out: List[str] = []
    for q in queries:
        qn = q.strip()
        if not qn:
            continue
        if qn in seen:
            continue
        seen.add(qn)
        out.append(qn)
    return out[:12]


def _extract_crime_names_from_article(article_text: str) -> List[str]:
    # 典型刑法条文格式：第二百七十二条 【挪用资金罪】...
    crimes = re.findall(r"【([^【】]{1,30}?罪)】", article_text or "")
    # 去重保序
    seen = set()
    out = []
    for c in crimes:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


@dataclass(frozen=True)
class PkulawHit:
    title: str
    article: str
    url: str


def _normalize_law_title(title: str) -> str:
    """
    将标题规范到“法的名称”层级，用于不同年份修正本的归并。
    例如：
    - "中华人民共和国刑法(2002修正)" -> "中华人民共和国刑法"
    - "中华人民共和国刑法(1997修订)" -> "中华人民共和国刑法"
    """
    t = (title or "").strip()
    if not t:
        return t
    # 按第一个括号切分，去掉后面的“(xxxx修正)/(xxxx修订)”
    return re.split(r"[（(]", t, 1)[0].strip()


def _extract_year_from_title(title: str) -> int:
    """
    从标题中的括号内容中尽量提取出“年份”，用于选择最新版。
    解析失败时返回 0。
    """
    if not title:
        return 0
    m = re.search(r"[（(](\d{4})[^\)]*[)）]", title)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def _parse_pkulaw_hits(tool_result: Any) -> List[PkulawHit]:
    """
    兼容不同 MCP client/SDK 的 tool result 形态。
    目标：最终拿到 list[{title, article, url}]。
    """
    if tool_result is None:
        return []

    # MCP Python SDK: CallToolResult (pydantic model) with attributes
    structured = getattr(tool_result, "structuredContent", None)
    if isinstance(structured, dict):
        # 常见形态：{"result": [{title, article, url}, ...]}
        if isinstance(structured.get("result"), list):
            return _parse_pkulaw_hits(structured.get("result"))

    content_attr = getattr(tool_result, "content", None)
    if isinstance(content_attr, list):
        # content is list[TextContent|...], prefer text blocks
        texts: List[str] = []
        for c in content_attr:
            c_type = getattr(c, "type", None)
            if c_type == "text":
                texts.append(getattr(c, "text", "") or "")
        for t in texts:
            t = (t or "").strip()
            if not t:
                continue
            # 有的实现直接把 JSON list 序列化成 text
            if t.startswith("[") and t.endswith("]"):
                try:
                    return _parse_pkulaw_hits(json.loads(t))
                except Exception:
                    continue

    # FastMCP/ClientSession 常见返回：{"content":[{"type":"text","text":"..."}], ...}
    if isinstance(tool_result, dict) and "content" in tool_result and isinstance(tool_result["content"], list):
        texts = [c.get("text", "") for c in tool_result["content"] if isinstance(c, dict) and c.get("type") == "text"]
        for t in texts:
            t = t.strip()
            if not t:
                continue
            try:
                data = json.loads(t)
            except Exception:
                continue
            if isinstance(data, list):
                return _parse_pkulaw_hits(data)

    if isinstance(tool_result, list):
        hits: List[PkulawHit] = []
        for item in tool_result:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            article = str(item.get("article", "")).strip()
            url = str(item.get("url", "")).strip()
            if not (title and article and url):
                continue
            hits.append(PkulawHit(title=title, article=article, url=url))
        return hits

    # 最后兜底：如果是字符串且像 JSON
    if isinstance(tool_result, str):
        s = tool_result.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return _parse_pkulaw_hits(json.loads(s))
            except Exception:
                return []
    return []


def _keep_latest_valid_hits(hits: List[PkulawHit]) -> List[PkulawHit]:
    """
    只保留“最新版且有效”的法律/解释：
    - 同一部法或文件（按规范化标题分组）仅保留年份最大的版本
    - 标题中带有“失效”字样的版本会被丢弃
    """
    # 先过滤掉明显标注为失效的
    filtered = [h for h in hits if "失效" not in h.title]
    if not filtered:
        return []

    groups: Dict[str, Tuple[int, int, PkulawHit]] = {}
    for idx, h in enumerate(filtered):
        base = _normalize_law_title(h.title)
        year = _extract_year_from_title(h.title)
        current = groups.get(base)
        if current is None:
            groups[base] = (year, idx, h)
        else:
            cur_year, cur_idx, _cur_hit = current
            # 年份高者优先；若年份相同则保留更靠后的（更可能是新版）
            if year > cur_year or (year == cur_year and idx > cur_idx):
                groups[base] = (year, idx, h)

    # 还原为按原顺序的大致排序
    latest_hits = [entry[2] for entry in groups.values()]
    latest_hits.sort(key=lambda item: filtered.index(item))
    return latest_hits


class PkulawMcpClient:
    """
    连接“北大法宝检索 MCP Server”的轻量客户端。

    支持两种方式：
    - stdio：通过启动本地进程连接
    - streamable_http：连接远程 MCP HTTP 端点（例如 ModelScope）
    """

    def __init__(
        self,
        *,
        tool_search_name: str = "search_article",
        stdio_command: Optional[str] = None,
        stdio_args: Optional[Sequence[str]] = None,
        streamable_http_url: Optional[str] = None,
    ):
        self._tool_search_name = tool_search_name
        self._stdio_command = stdio_command
        self._stdio_args = list(stdio_args or [])
        self._streamable_http_url = streamable_http_url

    async def search_article(self, text: str) -> List[PkulawHit]:
        results = await self.search_articles([text])
        return results.get(text, [])

    async def search_articles(self, texts: Sequence[str]) -> Dict[str, List[PkulawHit]]:
        """
        批量检索：尽量在同一个 MCP session 内完成，降低延迟与连接开销。
        """
        queries = [(t or "").strip() for t in texts]
        queries = [q for q in queries if q]
        out: Dict[str, List[PkulawHit]] = {q: [] for q in queries}
        if not queries:
            return out

        if self._streamable_http_url:
            async with streamable_http_client(self._streamable_http_url) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    for q in queries:
                        result = await session.call_tool(self._tool_search_name, {"text": q})
                        out[q] = _parse_pkulaw_hits(result)
            return out

        if self._stdio_command:
            params = StdioServerParameters(command=self._stdio_command, args=self._stdio_args)
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    for q in queries:
                        result = await session.call_tool(self._tool_search_name, {"text": q})
                        out[q] = _parse_pkulaw_hits(result)
            return out

        return out


def _load_pkulaw_client_from_env() -> Optional[PkulawMcpClient]:
    """
    通过环境变量配置“北大法宝检索 MCP Server”的连接方式。

    优先使用远程 streamable_http：
    - PKULAW_MCP_URL: 例如 "https://.../mcp"

    若未提供 URL，则使用本地 stdio：
    - PKULAW_MCP_COMMAND: 例如 "node" / "python" / 可执行文件路径
    - PKULAW_MCP_ARGS: 可选，JSON 数组字符串，例如 '["path/to/pkulaw_server.js"]'
    - PKULAW_MCP_TOOL_SEARCH: 可选，默认 "search_article"
    """
    tool_search = (os.getenv("PKULAW_MCP_TOOL_SEARCH") or "search_article").strip() or "search_article"

    url = (os.getenv("PKULAW_MCP_URL") or "").strip()
    if url:
        return PkulawMcpClient(tool_search_name=tool_search, streamable_http_url=url)

    cmd = (os.getenv("PKULAW_MCP_COMMAND") or "").strip()
    if not cmd:
        return None

    args_raw = (os.getenv("PKULAW_MCP_ARGS") or "").strip()
    args: List[str] = []
    if args_raw:
        try:
            parsed = json.loads(args_raw)
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                args = list(parsed)
            else:
                # 兼容用户直接写成空格分隔
                args = args_raw.split()
        except Exception:
            args = args_raw.split()

    return PkulawMcpClient(tool_search_name=tool_search, stdio_command=cmd, stdio_args=args)


@mcp.tool()
async def verify_legal_citations(citations: List[str]) -> str:
    """
    接收一组法律条文名称/关键词，返回能够在权威检索源中命中的结果与链接。

    输出为 JSON 字符串：
    {
      "results": [{"query": "...", "matched": true/false, "top_hits": [...]}],
      "sources": {"flk_index": "https://flk.npc.gov.cn/index"}
    }
    """
    client = _load_pkulaw_client_from_env()
    if client is None:
        return json.dumps(
            {
                "error": "未配置北大法宝检索 MCP 连接参数。请设置环境变量 PKULAW_MCP_COMMAND/PKULAW_MCP_ARGS 后重试。",
                "sources": {"flk_index": NPC_FLAW_INDEX_URL},
            },
            ensure_ascii=False,
            indent=2,
        )

    queries = [(c or "").strip() for c in (citations or [])]
    queries = [q for q in queries if q]

    results = []
    try:
        batch = await client.search_articles(queries)
    except Exception as e:
        return json.dumps(
            {
                "error": f"检索失败: {str(e)}",
                "results": [{"query": q, "matched": False, "top_hits": []} for q in queries],
                "sources": {"flk_index": NPC_FLAW_INDEX_URL},
            },
            ensure_ascii=False,
            indent=2,
        )

    for q in queries:
        hits = batch.get(q, []) or []
        results.append({"query": q, "matched": len(hits) > 0, "top_hits": [{"title": h.title, "url": h.url} for h in hits[:5]]})

    return json.dumps({"results": results, "sources": {"flk_index": NPC_FLAW_INDEX_URL}}, ensure_ascii=False, indent=2)


@mcp.tool()
async def analyze_crime(case_description: str) -> str:
    """
    输入案情描述，返回：罪名、法律条文、司法解释（如能检索到）以及来源链接。

    严格原则：不凭空生成法条内容；所有条文内容必须来自检索结果。
    """
    text = (case_description or "").strip()
    if not text:
        return json.dumps(
            {"recognized": False, "message": "case_description 不能为空。", "sources": {"flk_index": NPC_FLAW_INDEX_URL}},
            ensure_ascii=False,
            indent=2,
        )

    if not _looks_like_legal_context(text):
        return json.dumps(
            {
                "recognized": False,
                "message": "未识别为法律相关内容。请补充更具体的行为、主体身份、时间地点、结果等要素。",
                "sources": {"flk_index": NPC_FLAW_INDEX_URL},
            },
            ensure_ascii=False,
            indent=2,
        )

    client = _load_pkulaw_client_from_env()
    if client is None:
        return json.dumps(
            {
                "recognized": True,
                "message": "已识别为法律问题，但未配置北大法宝检索 MCP 连接参数，无法进行权威检索。",
                "how_to_fix": "优先设置 PKULAW_MCP_URL（streamable_http 远程端点）；或设置 PKULAW_MCP_COMMAND/PKULAW_MCP_ARGS（stdio 本地启动）。",
                "sources": {"flk_index": NPC_FLAW_INDEX_URL},
            },
            ensure_ascii=False,
            indent=2,
        )

    queries = _extract_candidate_queries(text)

    all_hits: List[PkulawHit] = []
    errors: List[Dict[str, str]] = []
    try:
        batch = await client.search_articles(queries)
    except Exception as e:
        errors.append({"query": "batch", "error": str(e)})
        batch = {}

    for q in queries:
        hits = batch.get(q, []) or []
        all_hits.extend(hits[:8])

    # 去重（按 url）
    seen_urls = set()
    uniq_hits: List[PkulawHit] = []
    for h in all_hits:
        if h.url in seen_urls:
            continue
        seen_urls.add(h.url)
        uniq_hits.append(h)

    # 对命中的法律/解释做“最新版筛选”：
    # - 同一部法律/文件只保留最新版本
    # - 丢弃标题中标注为“失效”的版本
    uniq_hits = _keep_latest_valid_hits(uniq_hits)

    # 提取罪名
    crimes: List[str] = []
    for h in uniq_hits:
        crimes.extend(_extract_crime_names_from_article(h.article))
    crimes = list(dict.fromkeys(crimes))[:10]

    # 简单区分“司法解释/规定”等
    interpretations: List[Dict[str, str]] = []
    statutes: List[Dict[str, str]] = []
    for h in uniq_hits[:30]:
        item = {"title": h.title, "article": h.article, "url": h.url}
        if any(k in h.title for k in ("司法解释", "解释", "规定", "意见")):
            interpretations.append(item)
        else:
            statutes.append(item)

    result: Dict[str, Any] = {
        "recognized": True,
        "input": {"case_description": text},
        "crimes": crimes,
        "statutes": statutes[:12],
        "judicial_interpretations": interpretations[:12],
        "sources": {
            "pkulaw": [h.url for h in uniq_hits[:12]],
            "flk_index": NPC_FLAW_INDEX_URL,
        },
    }
    if errors:
        result["warnings"] = errors[:6]

    # 鲁棒性：如果检索结果不足以回答或上游出错，明确告知
    if not statutes and not interpretations:
        result["recognized"] = False
        if errors:
            # 上游检索服务本身出错
            result["message"] = "权威检索服务调用失败，未能获取到结果。请稍后重试或检查 pkulaw MCP 服务状态。"
        else:
            # 正常返回但没有足够匹配结果
            result["message"] = "基于现有权威检索结果，未能找到足够信息匹配该描述。建议补充关键事实（主体身份/是否公职人员/财物性质/金额/用途/是否归还等）。"

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def legal_research(case_description: str) -> str:
    """
    `analyze_crime` 的别名，便于不同客户端按习惯调用。
    """
    return await analyze_crime(case_description)


if __name__ == "__main__":
    # FastMCP 会按 MCP 标准协议启动 stdio server
    mcp.run()