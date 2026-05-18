"""
Qoder CLI 会话管理 — 封装 qodercli 子进程，支持 SSE 流式输出。

架构：
- 每个消息启动一次 qodercli -p，Qoder 自己的 session 文件管理对话历史
- 新会话: --session-id <id> --system-prompt "..."
- 续接:   --resume <id>
- 输出:   --output-format stream-json → 逐行 NDJSON → SSE
"""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import AsyncIterator, Optional
from loguru import logger

QODERCLI_BIN = shutil.which("qodercli") or "qodercli"


class QoderSession:
    """管理单个 Qoder CLI 会话"""

    def __init__(
        self,
        session_id: str,
        system_prompt: str = "",
        knowledge_base: str = "data/canonical_md",
        permission_mode: str = "auto",
    ):
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.knowledge_base = str(Path(knowledge_base).absolute())
        self.permission_mode = permission_mode
        self._is_new = True

    async def send_message(self, message: str) -> AsyncIterator[str]:
        """发送消息并返回 SSE 流式文本块"""
        cmd = self._build_command(message)
        logger.info(f"Qoder session={self.session_id} is_new={self._is_new}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            async for chunk in self._parse_stream(proc):
                yield chunk
            await asyncio.wait_for(proc.wait(), timeout=10)
            if proc.returncode != 0:
                stderr = (await proc.stderr.read()).decode(errors="replace")
                logger.warning(f"qodercli exit={proc.returncode}: {stderr[:200]}")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        self._is_new = False

    def _build_command(self, message: str) -> list:
        cmd = [QODERCLI_BIN, "-p", "--output-format", "stream-json"]

        if self._is_new:
            cmd += ["--session-id", self.session_id]
            if self.system_prompt:
                cmd += ["--system-prompt", self.system_prompt]
        else:
            cmd += ["--resume", self.session_id]

        cmd += [
            "--add-dir", self.knowledge_base,
            "--permission-mode", self.permission_mode,
            message,
        ]
        return cmd

    async def _parse_stream(self, proc: asyncio.subprocess.Process) -> AsyncIterator[str]:
        """解析 qodercli stream-json 输出，提取文本内容"""
        buffer = b""
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
            except asyncio.TimeoutError:
                logger.warning(f"Session {self.session_id} stdout timeout")
                break
            if not line:
                break

            try:
                obj = json.loads(line.decode())
            except json.JSONDecodeError:
                buffer += line
                continue

            obj_type = obj.get("type", "")
            if obj_type == "assistant":
                # 提取 assistant 文本块
                message_obj = obj.get("message", {})
                content = message_obj.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            yield block.get("text", "")
                elif isinstance(content, str):
                    yield content
            elif obj_type == "result":
                # 最终结果
                break
            elif obj_type == "system":
                # 系统消息（session ID 确认等）
                pass
            elif obj_type == "error":
                err_msg = obj.get("message", "unknown error")
                logger.error(f"Qoder error: {err_msg}")
                yield f"\n[Error: {err_msg}]"

    def fork(self) -> "QoderSession":
        """创建分叉会话（用于编辑/回滚场景）"""
        import uuid
        new_id = f"{self.session_id}-fork-{uuid.uuid4().hex[:8]}"
        new_session = QoderSession(
            session_id=new_id,
            system_prompt=self.system_prompt,
            knowledge_base=self.knowledge_base,
            permission_mode=self.permission_mode,
        )
        new_session._is_new = True
        return new_session
