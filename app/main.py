from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router, get_session_pool
from loguru import logger
import os
import sys
from pathlib import Path
import uvicorn

# ── 日志配置 ──────────────────────────────────────────────
logger.remove()

_debug = os.environ.get("QODER_DEBUG", "").lower() in ("1", "true", "yes")
_log_level = "DEBUG" if _debug else "INFO"

# console 日志（Docker 中通过 docker logs 可见）
logger.add(sys.stderr, level=_log_level,
           format="{time:HH:mm:ss.SSS} | {level:<7} | {message}")

# 文件日志（持久化，按天轮转，保留 7 天）
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger.add(
    LOG_DIR / "biblebot_{time:YYYY-MM-DD}.log",
    level="DEBUG",  # 文件始终记 DEBUG，方便事后排查
    rotation="00:00",
    retention="7 days",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}"
)
logger.info(f"Log file: {LOG_DIR.resolve()}")
# ──────────────────────────────────────────────────────────

app = FastAPI(
    title="Biblebot Knowledge Server",
    description="企业知识库 RAG 检索 + OpenAI兼容 Agent API。",
    version="3.0.0"
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(router, prefix="/api")


@app.on_event("startup")
async def startup():
    pool = get_session_pool()
    await pool.start()


@app.on_event("shutdown")
async def shutdown():
    pool = get_session_pool()
    await pool.stop()

@app.get("/")
async def root():
    return {
        "message": "Biblebot Knowledge Server is running.",
        "version": "3.0.0",
        "architecture": "轻RAG + 强探索",
        "agent_runtime": "Qoder CLI",
        "endpoints": {
            "rag": "/api/query - RAG 语义检索",
            "openai_compat": "/v1/chat/completions - OpenAI 兼容 Agent API",
            "models": "/v1/models - 模型列表",
            "docs": "/docs - API 文档",
        },
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
