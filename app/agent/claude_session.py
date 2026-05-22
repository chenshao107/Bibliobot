"""
Claude CLI 会话管理 — 封装 claude 子进程，支持 SSE 流式输出。

架构：
- 每个消息启动一次 claude -p，Claude 自己的 session 文件管理对话历史
- 新会话: --session-id <id> --system-prompt "..."
- 续接:   --resume <id>
- 输出:   --output-format stream-json → 逐行 NDJSON → SSE
"""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional
from loguru import logger

# ── MCP 配置读取 ──────────────────────────────────────────
def _get_mcp_config_args() -> list:
    """从环境变量读取 MCP 配置，返回 claude --mcp-config 参数列表
    
    支持两种方式（优先级从高到低）：
    1. MCP_CONFIG_JSON — 直接写 JSON 字符串
    2. MCP_CONFIG_PATH — JSON 配置文件路径
    """
    # 方式1: 直接 JSON 字符串
    mcp_json = os.environ.get("MCP_CONFIG_JSON", "").strip()
    if mcp_json:
        try:
            json.loads(mcp_json)  # 验证 JSON 格式
            return ["--mcp-config", mcp_json]
        except json.JSONDecodeError:
            logger.warning("MCP_CONFIG_JSON is not valid JSON, ignoring")

    # 方式2: JSON 配置文件
    mcp_path = os.environ.get("MCP_CONFIG_PATH", "").strip()
    if mcp_path:
        config_file = Path(mcp_path)
        if not config_file.is_absolute():
            # 相对路径相对于项目根目录
            project_root = Path(__file__).resolve().parent.parent.parent
            config_file = project_root / mcp_path
        if config_file.exists():
            logger.info(f"Loading MCP config from: {config_file}")
            return ["--mcp-config", str(config_file)]
        else:
            logger.debug(f"MCP config file not found: {config_file}, skipping")

    return []

def _find_claude() -> str:
    """自动发现 claude 二进制，处理 Docker volume 挂载导致 symlink 过期的情况"""
    # 1. 先尝试 PATH
    found = shutil.which("claude")
    if found and os.path.isfile(found):
        return found
    # 2. 搜索安装目录（Docker 中 ~/.claude 被 volume 覆盖时 symlink 可能断链）
    import glob
    for base in ("/root/.claude/bin/claude", os.path.expanduser("~/.claude/bin/claude")):
        versions = sorted(glob.glob(f"{base}/claude-*"), reverse=True)
        for v in versions:
            if os.path.isfile(v) and os.access(v, os.X_OK):
                # 同时修复断链的 symlink
                local_link = os.path.expanduser("~/.local/bin/claude")
                try:
                    if os.path.islink(local_link):
                        os.unlink(local_link)
                    os.symlink(v, local_link)
                except OSError:
                    pass
                return v
    return "claude"

CLAUDE_BIN = _find_claude()


@dataclass
class StreamChunk:
    """claude 输出的一个流式块"""
    kind: str          # "text" | "tool_call" | "tool_result" | "error"
    content: str = ""
    tool_name: str = ""
    tool_input: str = ""
    tool_output: str = ""


class ClaudeSession:
    """管理单个 Claude CLI 会话"""

    def __init__(
        self,
        session_id: str,
        system_prompt: str = "",
        knowledge_base: str = "data/canonical_md",
        permission_mode: str = "bypassPermissions",
    ):
        # pool_key: 由消息指纹生成（UUID v5），仅用于 session_pool 内部查找
        # session_id: 每次新建时生成全新 UUID4，避免与历史 session 锁冲突
        import uuid
        self.pool_key = session_id
        self.session_id = str(uuid.uuid4())
        self.system_prompt = system_prompt
        self.knowledge_base = str(Path(knowledge_base).absolute())
        self.permission_mode = permission_mode
        self._is_new = True
        self._send_lock = asyncio.Lock()  # 串行化 send_message，防止并发时 session_id 冲突

    async def send_message(self, message: str) -> AsyncIterator[StreamChunk]:
        """发送消息并返回流式块（含工具调用/结果）
        
        串行化保护：同一 session 的多个并发请求会排队，防止两个 claude 进程
        同时争夺 `--session-id` 导致 "already in use" 错误。
        """
        async with self._send_lock:
            async for chunk in self._send_message_impl(message):
                yield chunk

    async def _send_message_impl(self, message: str) -> AsyncIterator[StreamChunk]:
        """实际发送逻辑（已由 send_message 加锁保护）"""
        cmd = self._build_command(message)
        cmd_log = self._format_cmd_for_log(cmd)
        logger.info(f"Claude session={self.session_id} is_new={self._is_new}\n[CLAUDE_CMD] {cmd_log}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        new_session_id = None  # fork-session 产生的新 ID
        try:
            async for chunk in self._parse_stream(proc):
                if chunk.kind == "_init" and chunk.content:
                    new_session_id = chunk.content
                    continue  # init 消息不对外暴露
                yield chunk
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode != 0:
                stderr = (await proc.stderr.read()).decode(errors="replace")
                logger.error(f"claude exit={proc.returncode} session={self.session_id}: {stderr[:500]}")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

        # fork-session 后更新 session_id，下次 resume 用新 ID
        if new_session_id:
            logger.info(f"Session forked: {self.session_id} → {new_session_id}")
            self.session_id = new_session_id
        self._is_new = False

    def _format_cmd_for_log(self, cmd: list) -> str:
        """将命令列表格式化为可复制粘贴的 shell 命令字符串，长参数截断。"""
        parts = []
        skip_next = False
        for i, arg in enumerate(cmd):
            if skip_next:
                skip_next = False
                continue
            # system-prompt 参数太长，截断展示
            if arg == "--system-prompt" and i + 1 < len(cmd):
                sp = cmd[i + 1]
                if len(sp) > 200:
                    parts.append(f"--system-prompt '{sp[:200]}...({len(sp)} chars)'")
                else:
                    parts.append(f"--system-prompt '{sp}'")
                skip_next = True
                continue
            # 用户消息太长也截断
            if i == len(cmd) - 1 and len(arg) > 500:
                parts.append(f"'{arg[:500]}...({len(arg)} chars)'")
                continue
            # 含空格或特殊字符的参数加引号
            if any(c in arg for c in (' ', '"', "'", '$', '`', '\\', '(', ')', '&', '|', ';')):
                escaped = arg.replace("'", "'\\''")
                parts.append(f"'{escaped}'")
            else:
                parts.append(arg)
        return " \\\n  ".join(parts)

    def _build_command(self, message: str) -> list:
        cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose"]

        # MCP 配置（从环境变量读取，用户自行配置 mcp_config.json）
        cmd += _get_mcp_config_args()

        if self._is_new:
            cmd += ["--session-id", self.session_id]
            if self.system_prompt:
                cmd += ["--system-prompt", self.system_prompt]
        else:
            cmd += ["--resume", self.session_id, "--fork-session"]

        cmd += [
            "--add-dir", self.knowledge_base,
            "--permission-mode", self.permission_mode,
            message,
        ]
        return cmd

    async def _parse_stream(self, proc: asyncio.subprocess.Process) -> AsyncIterator[StreamChunk]:
        """解析 claude stream-json 输出，提取所有有意义的事件
        
        使用 read() 分块 + 手动换行拆分，避免 asyncio readline() 的 64KB 缓冲区限制。
        """
        raw_buffer = b""  # 原始字节缓冲（按块读取）
        line_buffer = b""  # 累积的未完成行
        pending_tool = {"name": "", "input": ""}  # 累积 tool_use + tool_result 配对

        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=300)
            except asyncio.TimeoutError:
                logger.warning(f"Session {self.session_id} stdout timeout")
                break
            if not chunk:
                break

            raw_buffer += chunk
            # 按换行拆分：完整行逐个处理，不完整的留到下一次
            while b"\n" in raw_buffer:
                line_bytes, raw_buffer = raw_buffer.split(b"\n", 1)
                # 如有上次残留，拼接到前面
                if line_buffer:
                    line_bytes = line_buffer + line_bytes
                    line_buffer = b""

                try:
                    obj = json.loads(line_bytes.decode())
                except json.JSONDecodeError:
                    # JSON 解析失败，可能是被截断的超长行，保存到 line_buffer 等后续 chunk
                    line_buffer = line_bytes
                    continue

            obj_type = obj.get("type", "")
            # 原始消息 dump（debug 用）
            logger.debug(f"[CLAUDE_RAW] type={obj_type} keys={list(obj.keys())} "
                        f"preview={json.dumps(obj, ensure_ascii=False)[:300]}")

            if obj_type == "assistant":
                # 助手文本
                message_obj = obj.get("message", {})
                content = message_obj.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            yield StreamChunk(kind="text", content=block.get("text", ""))
                        elif isinstance(block, dict) and block.get("type") == "tool_use":
                            pending_tool["name"] = block.get("name", "")
                            pending_tool["input"] = json.dumps(block.get("input", {}), ensure_ascii=False)
                            yield StreamChunk(
                                kind="tool_call",
                                tool_name=pending_tool["name"],
                                tool_input=pending_tool["input"],
                            )
                        elif isinstance(block, dict) and block.get("type") == "tool_result":
                            output = block.get("content", "")
                            if isinstance(output, list):
                                output = "\n".join(
                                    o.get("text", "") for o in output if isinstance(o, dict)
                                )
                            yield StreamChunk(
                                kind="tool_result",
                                tool_name=pending_tool["name"] or "?",
                                tool_output=str(output),
                            )
                            pending_tool = {"name": "", "input": ""}
                elif isinstance(content, str):
                    yield StreamChunk(kind="text", content=content)

            elif obj_type == "system":
                # 检查是否是 init 消息（含 session_id，fork-session 后会变）
                if obj.get("subtype") == "init":
                    # claude 可能用 session_id 或 sessionId
                    sid = obj.get("session_id", "") or obj.get("sessionId", "")
                    if sid:
                        logger.debug(f"[CLAUDE_INIT] new session_id={sid} (keys={list(obj.keys())})")
                        yield StreamChunk(kind="_init", content=sid)
                    else:
                        logger.warning(f"[CLAUDE_INIT] no session_id found in init: {json.dumps(obj, ensure_ascii=False)[:500]}")
                    continue

                # 系统消息 — 尝试从中提取工具执行信息
                message_obj = obj.get("message", {})
                content = message_obj.get("content", [])
                text = ""
                if isinstance(content, list):
                    text = "\n".join(
                        block.get("text", "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                elif isinstance(content, str):
                    text = content
                if text.strip():
                    # 如果看起来像工具输出（多行、包含命令特征），标记为 tool_result
                    if _looks_like_tool_output(text):
                        yield StreamChunk(kind="tool_result", tool_output=text)
                    else:
                        yield StreamChunk(kind="text", content=text)

            elif obj_type == "result":
                break

            elif obj_type == "error":
                err_msg = obj.get("message", "unknown error")
                logger.error(f"Claude error: {err_msg}")
                yield StreamChunk(kind="error", content=str(err_msg))

    def fork(self) -> "ClaudeSession":
        """创建分叉会话（用于编辑/回滚场景）"""
        import uuid
        new_id = f"{self.pool_key}-fork-{uuid.uuid4().hex[:8]}"
        new_session = ClaudeSession(
            session_id=new_id,
            system_prompt=self.system_prompt,
            knowledge_base=self.knowledge_base,
            permission_mode=self.permission_mode,
        )
        new_session._is_new = True
        return new_session


def _looks_like_tool_output(text: str) -> bool:
    """判断 system 消息文本是否像是工具执行输出（而非对话文本）"""
    # 多行输出通常是命令/脚本结果
    lines = text.strip().split("\n")
    if len(lines) >= 3:
        return True
    # 包含典型的技术输出特征
    tech_markers = ["Permission", "Result", "Output", "Running", "Executing",
                    "Error", "Warning", "INFO", "DEBUG", "Exit code",
                    "total", "files", "directories"]
    for marker in tech_markers:
        if marker in text:
            return True
    return False
