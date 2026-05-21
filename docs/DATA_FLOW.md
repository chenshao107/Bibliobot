# Biblebot 数据流全链路文档

## 总体架构

```
Open WebUI / 客户端
    │  POST /v1/chat/completions  (OpenAI 格式)
    ▼
┌──────────────────────────────────────────────────────┐
│  routes.py: chat_completions()                       │
│    1. 提取消息 → session_key (UUID v5)                │
│    2. 组装 system_prompt                             │
│    3. get_or_create QoderSession                     │
│    4. 流式/非流式返回                                 │
└──────────┬───────────────────────────────────────────┘
           │
    ┌──────▼──────┐     ┌─────────────┐
    │ prompt_builder│◄────│ prompts/*.txt│  (base, tools, strategy...)
    │              │◄────│ patches.yaml │  (用户 YAML 覆盖)
    │              │◄────│ RAG_TOOL_INJECTION (内置)
    └──────┬──────┘     └─────────────┘
           │ system_prompt (纯文本)
    ┌──────▼──────────────────────────────────────────┐
    │  QoderSession                                     │
    │    qodercli -p --session-id <uuid>                 │
    │            --system-prompt "..."                   │
    │            --add-dir data/canonical_md             │
    │            --permission-mode bypass_permissions    │
    │            "用户问题"                               │
    │            --output-format stream-json             │
    │                                                    │
    │    stdout → NDJSON 逐行解析 → StreamChunk 流        │
    └──────┬─────────────────────────────────────────────┘
           │ StreamChunk (kind=text|tool_call|tool_result|error)
    ┌──────▼──────────────────────────────────────────┐
    │  routes.py: _stream_response()                    │
    │    text       → SSE data: delta content            │
    │    tool_call  → SSE data: 🔧 ... (前端可见)         │
    │    tool_result→ logger.info (仅日志)               │
    │    error      → logger.error (仅日志)              │
    └──────┬──────────────────────────────────────────┘
           │ SSE (text/event-stream)
           ▼
    Open WebUI / 客户端
```

---

## 第1步: 客户端请求

Open WebUI 发送标准 OpenAI Chat Completions 请求：

```json
POST /v1/chat/completions
{
  "model": "biblebot",
  "stream": true,
  "messages": [
    {"role": "user", "content": "140服务器怎么拉取代码？"}
  ]
}
```

---

## 第2步: Session Key 生成

```python
# session_pool.py
def make_session_key(messages: list) -> str:
    # 取第一条 user 消息的纯文本
    # → UUID v5(BIBLEBOT_NAMESPACE, "140服务器怎么拉取代码？")
    # → "1357a4e9-6c95-5688-8e48-e22e0622d58d"
```

**关键特性**:
- 同一个问题永远产生相同的 UUID → 多轮对话自动续接
- 不同问题产生不同 UUID → 独立会话
- Open WebUI 可通过 `user` 字段覆盖 session key

---

## 第3步: System Prompt 组装

### 3.1 加载模板

```
prompts/
├── answer_format.txt    回答规范
├── base.txt             Biblebot 身份定义
├── knowledge_scope.txt  知识库范围
├── strategy.txt         回答策略
└── tools.txt            可用能力 & {{TOOLS_PLACEHOLDER}}
```

### 3.2 应用 YAML 补丁 (可选)

`prompts_override/patches.yaml` 示例:
```yaml
- op: after
  target: strategy
  content: |
    企业环境附加规则：
    - 优先搜索本地知识库
    - 禁止调用外网工具
```

支持 4 种操作: `after`(追加) / `before`(前置) / `replace`(替换) / `append_line`(追加行)

### 3.3 拼装顺序

```
base.txt
    ↓
tools.txt  ← {{TOOLS_PLACEHOLDER}} 替换为 RAG_TOOL_INJECTION
    ↓
strategy.txt
    ↓
knowledge_scope.txt
    ↓
answer_format.txt
    ↓
最终 system_prompt (纯文本)
```

### 3.4 注入内容

```python
RAG_TOOL_INJECTION = """
可用工具（注意工具名大小写）：
- Bash: scripts/rag_search.sh "查询词"   语义搜索知识库
- Bash: cat/grep/rg/head/tail/find/tree 阅读文件

data/raw/             原始文档，不要直接读
data/canonical_md/    已转换的Markdown，优先读这里
"""
```

---

## 第4步: Session 获取

```python
# session_pool.py
async def get_or_create(session_key, system_prompt):
    if session_key in self._sessions:
        return existing_session  # 复用，刷新时间戳
    else:
        # 新建 QoderSession，--session-id 模式
        return QoderSession(session_key, system_prompt)
```

- 最多 50 个并发会话
- 空闲 1 小时自动清理
- 每 5 分钟检查一次

---

## 第5步: Qoder CLI 子进程

### 新会话（首次）

```bash
qodercli -p \
  --output-format stream-json \
  --session-id 1357a4e9-6c95-5688-8e48-e22e0622d58d \
  --system-prompt "你是 Biblebot，一个企业级技术知识库智能助手..." \
  --add-dir /app/data/canonical_md \
  --permission-mode bypass_permissions \
  "140服务器怎么拉取代码？"
```

### 续接会话（同 UUID 的后续请求）

```bash
qodercli -p \
  --output-format stream-json \
  --resume 1357a4e9-6c95-5688-8e48-e22e0622d58d \
  --add-dir /app/data/canonical_md \
  --permission-mode bypass_permissions \
  "前面的那个run_repo_init_sync具体怎么用？"
```

此时 Qoder 自动加载之前的对话历史，Agent 知道上下文。

---

## 第6步: NDJSON 解析

qodercli 输出格式为 **NDJSON** (每行一个 JSON):

```jsonl
{"type":"system","subtype":"init","tools":["Agent","Bash","Glob","Grep","Read",...]}
{"type":"assistant","message":{"content":[{"type":"text","text":"我需要查找..."}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"scripts/rag_search.sh ..."}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","content":"No results found."}]}}
{"type":"result","subtype":"success","num_turns":5}
```

### NDJSON → StreamChunk 映射

| Qoder type | content block type | → StreamChunk.kind | 前端行为 |
|-----------|-------------------|---------------------|---------|
| assistant | text | `text` | ✅ SSE delta content |
| assistant | tool_use | `tool_call` | ✅ SSE `🔧 **Bash**` |
| assistant | tool_result | `tool_result` | ❌ 仅 `logger.info` |
| system | (任意) | `tool_result` 或 `text` | ❌ 仅日志 |
| error | — | `error` | ❌ 仅 `logger.error` |
| result | — | (break, 结束) | — |

### StreamChunk 数据结构

```python
@dataclass
class StreamChunk:
    kind: str          # "text" | "tool_call" | "tool_result" | "error"
    content: str = ""
    tool_name: str = ""
    tool_input: str = ""
    tool_output: str = ""
```

---

## 第7步: SSE 流式响应

### 流式 (stream=true)

```python
async def _stream_response(session, message, completion_id, model):
    async for chunk in session.send_message(message):
        if chunk.kind == "text":
            # → SSE: delta.content = 助手文本
            yield f"data: {json.dumps(chunk)}\n\n"
        elif chunk.kind == "tool_call":
            # → SSE: delta.content = "🔧 **Bash**\n```\n{input}\n```"
            yield f"data: {json.dumps(chunk)}\n\n"

    yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    yield "data: [DONE]\n\n"
```

### 非流式 (stream=false)

```python
async def _collect_full_response(session, message) -> str:
    parts = []
    async for chunk in session.send_message(message):
        if chunk.kind == "text":
            parts.append(chunk.content)
        elif chunk.kind == "tool_call":
            parts.append(f"\n🔧 `{chunk.tool_name}` ...\n")
    return "".join(parts)
```

### SSE 示例输出

```
data: {"choices":[{"delta":{"content":"我需要查找关于140服务器的使用文档..."}}]}

data: {"choices":[{"delta":{"content":"\n\n🔧 **Bash**\n```\nscripts/rag_search.sh \"140服务器\"\n```\n"}}]}

data: {"choices":[{"delta":{"content":"根据《140服务器使用文档》，拉取代码的命令是："}}]}

data: {"choices":[{"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

---

## 多轮对话数据流

```
第1轮: "140服务器怎么拉取代码？"
  → UUID v5 = 1357a4e9-...
  → _is_new = True
  → qodercli --session-id 1357a4e9-... --system-prompt "..."

第2轮: "前面的run_repo_init_sync怎么用？"
  → UUID v5 = 1357a4e9-... (相同!)
  → SessionPool 命中 → 复用同一个 QoderSession
  → _is_new = False
  → qodercli --resume 1357a4e9-...  (不传 --system-prompt)
  → Qoder 自动加载前一轮对话历史

第3轮: "帮我查一下RK3506"  (不同问题)
  → UUID v5 = abc4bb8b-... (新 UUID)
  → 全新独立会话
```

**关键**: Qoder 通过 `--resume` 自己管理对话历史（存在 `/root/.qoder/projects/`），Biblebot 不存储任何对话内容。

---

## 环境变量

| 变量 | 用途 | 必需 |
|------|------|------|
| `QODER_PERSONAL_ACCESS_TOKEN` | Qoder 认证 | ✅ |
| `QODER_DEBUG` | 开启 DEBUG 日志 (`true`/`1`) | ❌ |
| `LLM_API_KEY` | 查询重写 LLM API Key | ❌ |
| `EMBEDDING_API_KEY` | 硅基流动 Embedding API Key | ✅ |
| `RERANK_API_KEY` | 硅基流动 Rerank API Key | ✅ |
| `QDRANT_HOST` | Qdrant 地址 | ✅ |
| `QDRANT_PORT` | Qdrant 端口 | ✅ |
