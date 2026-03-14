# LegalBrain MCP Server（中国法律检索 / 犯罪分析）

本项目提供一个 **MCP Server**，向大模型暴露工具 `analyze_crime`（以及辅助工具 `verify_legal_citations`），用于：

- 输入一段案情/行为描述
- 自动发起权威检索（通过你们已配置的“北大法宝法律智能检索 MCP 服务”）
- 返回 **罪名、法律条文、司法解释（如可检索到）、权威来源链接**
- 在检索不足时给出明确提示，避免“幻觉式编造”

> 重要：本服务 **不内置法条库**，也不会凭空生成法条内容；法条与解释正文均来自外部权威检索 MCP 的返回结果。

## 1、启动

### 1) 安装依赖

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

### 2) 配置北大法宝检索 MCP（必须）

本 Server 会在运行 `analyze_crime` 时，连接你们的“北大法宝法律智能检索 MCP Server”。支持两种方式：

- **方式 A（推荐）**：远程 `streamable_http`（例如 ModelScope MCP）
- **方式 B**：本地 `stdio`（启动一个子进程并通过 stdin/stdout 通信）

#### 方式 A：streamable_http（远程 URL）

- **`PKULAW_MCP_URL`**：远程 MCP 端点 URL（`streamable_http`）
- **`PKULAW_MCP_TOOL_SEARCH`**（可选）：检索工具名，默认 `search_article`

示例（PowerShell，使用你给的配置）：

```powershell
$env:PKULAW_MCP_URL="https://mcp.api-inference.modelscope.net/6533acfa4fc14f/mcp"
$env:PKULAW_MCP_TOOL_SEARCH="search_article"
```

#### 方式 B：stdio（本地启动）

本 Server 会在调用时，**通过 stdio 启动并连接**你们的北大法宝检索 MCP Server。请用环境变量告诉它如何启动：

- **`PKULAW_MCP_COMMAND`**：启动命令（如 `node` / `python` / 可执行文件路径）
- **`PKULAW_MCP_ARGS`**：启动参数，建议用 JSON 数组字符串
- **`PKULAW_MCP_TOOL_SEARCH`**（可选）：检索工具名，默认 `search_article`

示例（PowerShell）：

```powershell
$env:PKULAW_MCP_COMMAND="node"
$env:PKULAW_MCP_ARGS='["D:\\path\\to\\pkulaw-mcp-law-search\\server.js"]'
$env:PKULAW_MCP_TOOL_SEARCH="search_article"
```

如果你们的北大法宝 MCP Server 是 Python：

```powershell
$env:PKULAW_MCP_COMMAND="python"
$env:PKULAW_MCP_ARGS='["D:\\path\\to\\pkulaw_mcp_server.py"]'
```

### 3) 运行本 MCP Server

```bash
python run.py
```

随后可被标准 MCP Client（如 Claude Desktop / 你们的测试脚本）以 stdio 方式连接调用。

## 2、暴露的工具（Tools）

### `analyze_crime`

**Input**

- `case_description` (string, required)：案情/行为描述

**Output（JSON 字符串）**

- `crimes`: 从检索到的条文中提取到的 `【...罪】` 罪名（若存在）
- `statutes`: 相关法律条文（标题、正文、来源链接）
- `judicial_interpretations`: 相关司法解释/规定（若检索命中）
- `sources`: 权威来源链接（含北大法宝返回的 URL 列表、全国人大法工委法律法规库入口 `https://flk.npc.gov.cn/index`）
- 当检索不足时会返回 `recognized=false` 与友好提示

### `verify_legal_citations`

**Input**

- `citations` (list[string])：条文名/关键词

**Output（JSON 字符串）**

- `results`: 每个 query 的 top hits（标题与链接）
- `sources.flk_index`

## 3、本地验收建议

- 使用你们现有的 MCP Client 直接调用 `analyze_crime`
- 或自行写一个 MCP client 脚本连接本服务（见你们内部测试方式）

## 4、设计说明（抗幻觉）

- 本服务仅做 **“检索编排 + 结构化输出”**
- 条文/解释正文完全来自权威检索 MCP 返回结果
- 若结果不足，会明确提示“未能找到足够信息”，而不是猜测补全

## 5、快速启动

```powershell
$env:PKULAW_MCP_URL="https://mcp.api-inference.modelscope.net/6533acfa4fc14f/mcp"	# 使用你的 pkulaw MCP
$env:PKULAW_MCP_TOOL_SEARCH="search_article"
$env:CASE="某人利用职务之便，将公司资金50万元转入个人账户用于炒股，三个月后归还。"			 #（可选）修改测试案情
python test_client.py																# 运行测试脚本
```

