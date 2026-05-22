from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional, Union
from loguru import logger
from app.services.rag.retriever import RAGEngine
from app.core.config import settings
from app.agent.session_pool import SessionPool, make_session_key
from app.agent.claude_session import StreamChunk
from app.agent.prompt_builder import build_system_prompt
import json
import time
from datetime import datetime
from pathlib import Path

router = APIRouter()

# ── 交互报文日志 ──────────────────────────────────────────
_INTERACTION_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_INTERACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)

def _log_interaction(direction: str, session_key: str, payload: dict):
    """记录每次 HTTP 交互的完整报文到 logs/interactions_YYYY-MM-DD.log"""
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_file = _INTERACTION_LOG_DIR / f"interactions_{date_str}.log"
        line = json.dumps({
            "timestamp": ts,
            "direction": direction,  # "request" or "response"
            "session_key": session_key,
            "payload": payload,
        }, ensure_ascii=False, default=str)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 报文日志不影响主流程

# 全局会话池
_session_pool: Optional[SessionPool] = None

def get_session_pool() -> SessionPool:
    global _session_pool
    if _session_pool is None:
        _session_pool = SessionPool()
    return _session_pool


# ============== OpenAI 兼容模型定义 ==============

class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]

class ChatCompletionRequest(BaseModel):
    model: str = "biblebot"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # 可选：手动指定 session_id（覆盖消息指纹）
    user: Optional[str] = None

class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"

class ChatCompletionUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: ChatCompletionUsage

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "biblebot"

class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


# ============== RAG 检索接口 ==============

class QueryRequest(BaseModel):
    query: str
    top_k: int = 5
    category_filter: Optional[str] = None

class QueryResponse(BaseModel):
    results: List[Dict[str, Any]]
    query: str
    total: int

# 懒加载 RAG 引擎
_rag_engine: Optional[RAGEngine] = None

def get_rag_engine() -> RAGEngine:
    global _rag_engine
    if _rag_engine is None:
        logger.info("初始化 RAG 引擎...")
        _rag_engine = RAGEngine()
    return _rag_engine

@router.post("/query", response_model=QueryResponse)
async def query_rag(request: QueryRequest):
    """
    RAG 语义检索接口

    返回候选文档的路径、分数和摘要（snippet），遵循"轻RAG + 强探索"原则：
    - RAG 只负责"定位"候选文档
    - Claude CLI Agent 负责"探索"获取精确答案

    返回格式: [{path, title, score, snippet}, ...]
    """
    try:
        rag = get_rag_engine()
        raw_results = rag.search(
            query=request.query,
            top_k=request.top_k,
            category_filter=request.category_filter,
        )

        # 转换为轻量定位格式
        results = []
        for r in raw_results:
            if isinstance(r, dict):
                payload = r.get("payload", {})
                score = r.get("score", 0)
            else:
                payload = getattr(r, "payload", {})
                score = getattr(r, "score", 0)

            canonical_path = payload.get("canonical_path", payload.get("doc_id", "unknown"))
            section = payload.get("section", "")
            content = payload.get("content", "")

            max_len = settings.RAG_SEARCH_MAX_SNIPPET_LEN
            snippet = content[:max_len]
            if len(content) > max_len:
                snippet += "..."

            results.append({
                "path": canonical_path,
                "title": section if section and section != "Root" else "",
                "score": score,
                "snippet": snippet,
            })

        return QueryResponse(
            results=results,
            query=request.query,
            total=len(results),
        )

    except Exception as e:
        logger.error(f"RAG 检索失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "version": "3.0.0"}


# ============== OpenAI 兼容接口 ==============

def _extract_last_user_message(messages: List[ChatMessage]) -> str:
    """提取最后一条 user 消息的纯文本"""
    for m in reversed(messages):
        if m.role == "user":
            if isinstance(m.content, str):
                return m.content
            elif isinstance(m.content, list):
                parts = []
                for block in m.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                return "".join(parts)
    return ""


def _is_openwebui_internal(text: str) -> bool:
    """检测 Open WebUI 自动生成的内部消息（标题/标签/追问），不应调用 Agent"""
    markers = [
        "### Task:",
        "Suggest 3-5 relevant follow-up",
        "Generate a concise, 3-5 word title",
        "Generate 1-3 broad tags",
    ]
    return any(m in text for m in markers)


def _messages_to_openai_format(messages: List[ChatMessage]) -> list:
    """将 Pydantic ChatMessage 转为 dict 列表"""
    return [{"role": m.role, "content": m.content} for m in messages]


@router.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """
    OpenAI 兼容的 Chat Completions 端点。
    由 Claude CLI Agent 接管对话。
    """
    # 生成 session key
    raw_messages = _messages_to_openai_format(request.messages)
    session_key = request.user or make_session_key(raw_messages)

    # 记录请求报文
    _log_interaction("request", session_key, {
        "model": request.model,
        "stream": request.stream,
        "user_override": request.user,
        "messages": raw_messages,
    })

    # 提取用户消息
    user_message = _extract_last_user_message(request.messages)
    if not user_message:
        raise HTTPException(status_code=400, detail="No user message found")

    # 过滤 Open WebUI 内部消息（标题生成/标签生成/追问建议），直接返回空响应
    if _is_openwebui_internal(user_message):
        logger.debug(f"Filtered Open WebUI internal message: {user_message[:100]}...")
        filtered_resp = {"filtered": True, "reason": "openwebui_internal", "content": ""}
        _log_interaction("response", session_key, filtered_resp)
        if request.stream:
            async def _empty_stream():
                yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_empty_stream(), media_type="text/event-stream")
        else:
            return ChatCompletionResponse(
                id=f"chatcmpl-filtered-{session_key}",
                created=int(time.time()),
                model=request.model,
                choices=[ChatCompletionChoice(
                    message=ChatMessage(role="assistant", content=""),
                    finish_reason="stop",
                )],
                usage=ChatCompletionUsage(),
            )

    # 获取 system prompt
    system_prompt = build_system_prompt()

    # 获取或创建 session
    pool = get_session_pool()
    session = await pool.get_or_create(session_key, system_prompt)

    completion_id = f"chatcmpl-{session_key}"

    if request.stream:
        # 流式：仅记请求，响应由 SSE 边生成边发送不捕获
        return StreamingResponse(
            _stream_response(session, user_message, completion_id, request.model),
            media_type="text/event-stream",
        )
    else:
        content = await _collect_full_response(session, user_message)
        _log_interaction("response", session_key, {
            "stream": False,
            "content": content,
            "finish_reason": "stop",
        })
        return ChatCompletionResponse(
            id=completion_id,
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    message=ChatMessage(role="assistant", content=content),
                    finish_reason="stop",
                )
            ],
            usage=ChatCompletionUsage(),
        )


async def _stream_response(session, message: str, completion_id: str, model: str):
    """SSE 流式响应生成器 — 工具调用/结果/错误均可见"""
    created = int(time.time())

    def _emit(content: str):
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }],
        }

    def _format_tool_chunk(sc: StreamChunk) -> str:
        """将工具调用/结果/错误格式化为可见的 markdown"""
        if sc.kind == "tool_call":
            name = sc.tool_name or "?"
            inp = sc.tool_input[:200] if sc.tool_input else ""
            return f"\n\n🔧 **{name}**\n```\n{inp}\n```\n"
        elif sc.kind == "tool_result":
            # 工具输出截取前 500 字符展示，让用户看到 Read 结果
            out = sc.tool_output[:500] if sc.tool_output else "(empty)"
            truncated = f"{out}..." if len(sc.tool_output) > 500 else out
            return f"\n\n📋 **结果**\n```\n{truncated}\n```\n"
        elif sc.kind == "error":
            return f"\n\n❌ **错误**: {sc.content[:300]}\n"
        return ""

    try:
        async for chunk in session.send_message(message):
            if chunk.kind == "text":
                if chunk.content:
                    yield f"data: {json.dumps(_emit(chunk.content), ensure_ascii=False)}\n\n"
            elif chunk.kind in ("tool_call", "tool_result", "error"):
                formatted = _format_tool_chunk(chunk)
                if formatted:
                    yield f"data: {json.dumps(_emit(formatted), ensure_ascii=False)}\n\n"
                # 同时记日志（完整内容）
                if chunk.kind == "tool_result":
                    logger.info(f"[TOOL_RESULT] session={session.session_id} "
                                f"tool={chunk.tool_name} output_len={len(chunk.tool_output)}")
                elif chunk.kind == "error":
                    logger.error(f"[TOOL_ERROR] session={session.session_id} {chunk.content}")

        # 发送结束标记
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        err_msg = str(e) or type(e).__name__
        logger.error(f"Stream error for session {session.session_id}: {err_msg}")
        logger.opt(exception=True).debug("Stream error traceback:")
        error_chunk = _emit(f"\n❌ [Error: {err_msg}]\n")
        error_chunk["choices"][0]["finish_reason"] = "error"
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


async def _collect_full_response(session, message: str) -> str:
    """非流式：收集完整响应（工具调用/结果/错误均可见）"""
    parts = []
    async for chunk in session.send_message(message):
        if chunk.kind == "text":
            parts.append(chunk.content)
        elif chunk.kind == "tool_call":
            parts.append(f"\n🔧 `{chunk.tool_name}` {chunk.tool_input[:100]}\n")
        elif chunk.kind == "tool_result":
            out = chunk.tool_output[:500] if chunk.tool_output else "(empty)"
            truncated = f"{out}..." if len(chunk.tool_output) > 500 else out
            parts.append(f"\n📋 结果: ```\n{truncated}\n```\n")
            logger.info(f"[TOOL_RESULT] session={session.session_id} "
                        f"tool={chunk.tool_name} output_len={len(chunk.tool_output)}")
        elif chunk.kind == "error":
            parts.append(f"\n❌ 错误: {chunk.content[:300]}\n")
            logger.error(f"[TOOL_ERROR] session={session.session_id} {chunk.content}")
    return "".join(parts)


@router.get("/v1/models")
async def list_models():
    """返回可用模型列表"""
    return ModelListResponse(
        data=[
            ModelInfo(id="biblebot", created=int(time.time())),
        ]
    )
