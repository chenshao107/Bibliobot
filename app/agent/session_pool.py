"""
会话池 — 管理 QoderSession 生命周期

- session_key → QoderSession 映射
- 空闲超时自动清理
- 线程安全（asyncio.Lock）
"""

import asyncio
import hashlib
import time
import uuid
from typing import Dict, Optional, Tuple
from loguru import logger

from app.agent.qoder_session import QoderSession

# 默认配置
DEFAULT_IDLE_TIMEOUT = 3600       # 1小时空闲自动销毁
CLEANUP_INTERVAL = 300            # 每5分钟检查一次

# UUID 命名空间（固定，保证同一条首消息始终映射到同一个 session ID）
BIBLEBOT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def make_session_key(messages: list) -> str:
    """从 OpenAI messages 数组生成 UUID session 指纹"""
    for m in messages:
        if m.get("role") == "user":
            raw = m.get("content", "")
            if isinstance(raw, list):
                raw = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
            return str(uuid.uuid5(BIBLEBOT_NAMESPACE, raw))
    raw = str(messages)
    return str(uuid.uuid5(BIBLEBOT_NAMESPACE, raw))


class SessionPool:
    """Qoder CLI 会话池"""

    def __init__(
        self,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
        max_sessions: int = 50,
    ):
        self._sessions: Dict[str, Tuple[QoderSession, float]] = {}
        self._lock = asyncio.Lock()
        self._idle_timeout = idle_timeout
        self._max_sessions = max_sessions
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动后台清理任务"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        """停止后台清理"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None

    async def get_or_create(
        self,
        session_key: str,
        system_prompt: str = "",
        knowledge_base: str = "data/canonical_md",
    ) -> QoderSession:
        """获取或创建会话"""
        async with self._lock:
            now = time.time()

            # 检查已有会话
            if session_key in self._sessions:
                session, _ = self._sessions[session_key]
                self._sessions[session_key] = (session, now)  # 刷新时间戳
                return session

            # 超过上限时清理最旧的
            if len(self._sessions) >= self._max_sessions:
                oldest_key = min(
                    self._sessions, key=lambda k: self._sessions[k][1]
                )
                logger.info(f"Pool full, evicting oldest session: {oldest_key}")
                del self._sessions[oldest_key]

            # 创建新会话
            session = QoderSession(
                session_id=session_key,
                system_prompt=system_prompt,
                knowledge_base=knowledge_base,
            )
            self._sessions[session_key] = (session, now)
            logger.info(f"Session created: {session_key} (total: {len(self._sessions)})")
            return session

    async def _cleanup_loop(self):
        """定期清理空闲会话"""
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            async with self._lock:
                now = time.time()
                expired = [
                    k for k, (_, ts) in self._sessions.items()
                    if now - ts > self._idle_timeout
                ]
                for key in expired:
                    logger.info(f"Session idle timeout: {key}")
                    del self._sessions[key]

    @property
    def active_count(self) -> int:
        return len(self._sessions)
