"""
Prompt 构建器 — 组装 Claude CLI 的 SYSTEM_PROMPT

来源：
1. prompts/*.txt                    → 基础模板
2. prompts_override/patches.yaml    → 用户 YAML 补丁（after/before/replace/append_line）
3. 内置注入                          → RAG 工具说明、知识库路径、MCP 工具列表
"""

import json
import os
from pathlib import Path
from typing import Dict
import yaml
from loguru import logger

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

PROMPTS_DIR = PROJECT_ROOT / "prompts"
OVERRIDE_DIR = PROJECT_ROOT / "prompts_override"
PATCHES_FILE = OVERRIDE_DIR / "patches.yaml"

# 内置注入的 RAG 工具说明
RAG_TOOL_INJECTION = """
🔍 知识库RAG搜索（优先使用）：
- Bash: scripts/rag_search.sh "查询词"   语义搜索知识库，返回候选文档路径+摘要，用户初步定位文档阅读方向。

data/raw/     原始文档（pdf/docx等），不要直接读
data/canonical_md/  已转换的Markdown文件，优先读这里
"""


def _build_mcp_injection() -> str:
    """检测 MCP 配置，生成 MCP 工具可用性提示"""
    # 方式1: MCP_CONFIG_JSON 直接字符串
    mcp_json = os.environ.get("MCP_CONFIG_JSON", "").strip()
    if mcp_json:
        try:
            cfg = json.loads(mcp_json)
        except json.JSONDecodeError:
            cfg = None
    else:
        # 方式2: MCP_CONFIG_PATH 配置文件
        mcp_path = os.environ.get("MCP_CONFIG_PATH", "").strip()
        if mcp_path:
            config_file = Path(mcp_path)
            if not config_file.is_absolute():
                config_file = PROJECT_ROOT / mcp_path
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    cfg = None
            else:
                cfg = None
        else:
            cfg = None

    if not cfg:
        return ""

    servers = cfg.get("mcpServers", {})
    if not servers:
        return ""

    lines = ["🌐 已连接的 MCP 外部服务："]
    for name, svr in servers.items():
        desc = svr.get("description", "") if isinstance(svr, dict) else ""
        if desc:
            lines.append(f"- {desc}: 通过 {name} MCP 工具集调用，不要手动 curl/wget 访问")
        else:
            lines.append(f"- {name}: 通过 {name} MCP 工具集调用，不要手动 curl/wget 访问")
    return "\n".join(lines)


def _load_prompts() -> Dict[str, str]:
    """从 prompts/*.txt 加载基础模板"""
    sections = {}
    if not PROMPTS_DIR.exists():
        return sections

    for f in sorted(PROMPTS_DIR.glob("*.txt")):
        key = f.stem  # base, strategy, knowledge_scope, tools, answer_format
        content = f.read_text(encoding="utf-8").strip()
        sections[key] = content
    return sections


def _apply_patches(sections: Dict[str, str]) -> Dict[str, str]:
    """应用 prompts_override/patches.yaml 的补丁"""
    if not PATCHES_FILE.exists():
        return sections

    try:
        patches = yaml.safe_load(PATCHES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load patches.yaml: {e}")
        return sections

    if not isinstance(patches, list):
        return sections

    for patch in patches:
        if not isinstance(patch, dict):
            continue
        op = patch.get("op")
        target = patch.get("target")
        content = str(patch.get("content", "")).strip()

        if not op or not target or target not in sections:
            continue

        current = sections[target]

        if op == "after":
            sections[target] = current + "\n\n" + content
        elif op == "before":
            sections[target] = content + "\n\n" + current
        elif op == "replace":
            sections[target] = content
        elif op == "append_line":
            sections[target] = current + "\n" + content

    return sections


def build_system_prompt() -> str:
    """组装最终的 SYSTEM_PROMPT"""
    sections = _load_prompts()
    sections = _apply_patches(sections)

    # 组装顺序: base → tools → strategy → knowledge_scope → answer_format
    parts = []
    for key in ["base", "tools", "strategy", "knowledge_scope", "answer_format"]:
        if key in sections and sections[key]:
            parts.append(sections[key])

    # 注入 RAG 工具说明（替换 tools 中的占位符）
    prompt = "\n\n".join(parts)
    prompt = prompt.replace("{{TOOLS_PLACEHOLDER}}", RAG_TOOL_INJECTION.strip())

    # 注入 MCP 工具可用性（如有配置）
    mcp_injection = _build_mcp_injection()
    if mcp_injection:
        prompt += "\n\n" + mcp_injection

    return prompt