#!/usr/bin/env python3
"""
Biblebot 启动脚本

Agent Runtime: Claude CLI

用法:
    python start.py              # 默认启动 Claude CLI Agent
    python start.py --server     # 仅启动 RAG 后端服务
    python start.py --debug      # 详细日志模式
"""

import argparse
import subprocess
import sys
import os
from pathlib import Path

# 自动检测并激活虚拟环境
_PROJECT_DIR = Path(__file__).resolve().parent
_VENV_PYTHON = _PROJECT_DIR / ".venv" / "bin" / "python"
if _VENV_PYTHON.exists():
    # 重新以 venv Python 执行当前脚本
    if sys.executable != str(_VENV_PYTHON):
        os.execv(str(_VENV_PYTHON), [_VENV_PYTHON, __file__] + sys.argv[1:])
    # 确保 venv 的 bin 在 PATH 中
    venv_bin = str(_VENV_PYTHON.parent)
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


def check_requirements():
    """检查运行要求"""
    issues = []

    if not Path(".env").exists():
        issues.append("未找到 .env 文件，请从 .env.example 复制并配置")

    return issues


def start_rag_server(debug=False, dev=False):
    """启动 RAG 服务"""
    if dev:
        os.environ.setdefault("BIBLEBOT_DEBUG", "1")
        print(f"{YELLOW}🔧 DEV 模式: BIBLEBOT_DEBUG=1, 所有 claude 命令行 + Debug 日志可见{RESET}")
    elif debug:
        os.environ.setdefault("BIBLEBOT_DEBUG", "1")

    print(f"\n{GREEN}Starting Biblebot RAG Server...{RESET}")
    print(f"{GREEN}  API:   http://localhost:8000/v1/chat/completions{RESET}")
    print(f"{GREEN}  Docs:  http://localhost:8000/docs{RESET}")
    print(f"{GREEN}  Logs:  logs/biblebot_$(date +%Y-%m-%d).log{RESET}")
    if dev or debug:
        print(f"{YELLOW}  Debug: BIBLEBOT_DEBUG=1 — 终端实时显示所有日志 + claude 命令行{RESET}")
        print(f"{YELLOW}  复现: 从日志复制 [CLAUDE_CMD] 即可手动执行{RESET}")
    print()

    log_level = "debug" if (debug or dev) else "info"

    cmd = [
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--reload",
        "--log-level", log_level,
    ]

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Server stopped{RESET}")


VENV_PYTHON = ".venv/bin/python"

SYSTEM_PROMPT = (
    "You are Biblebot, an enterprise knowledge exploration agent. "
    "You have access to a knowledge base at data/canonical_md (Markdown files). "
    "Use 'Bash: scripts/rag_search.sh \"query\"' to semantically search the knowledge base for candidate documents. "
    "RAG search returns file paths and snippets — it only LOCATES documents, it does NOT give you the full answer. "
    "After locating candidate documents, use Bash (cat, grep, head, tail, rg, find, tree) to READ and EXPLORE the actual files. "
    "Workflow: rag_search → locate → bash explore → refine → answer. "
    "Prefer parallel exploration: call multiple tools in one turn. "
    "The knowledge base is mounted read-only — you cannot modify it."
)


def _check_knowledge_base():
    """检查知识库目录是否存在"""
    knowledge_path = Path("data/canonical_md").absolute()
    if not knowledge_path.exists():
        print(f"{RED}知识库目录不存在: {knowledge_path}{RESET}")
        print(f"{YELLOW}请先运行: python scripts/ingest_folder.py{RESET}")
        sys.exit(1)
    return knowledge_path


def start_claude():
    """启动 Claude CLI Agent"""
    knowledge_path = _check_knowledge_base()

    print(f"\n{GREEN}Starting Claude CLI Agent with Biblebot...{RESET}")
    print(f"{GREEN}  Knowledge Base: {knowledge_path}{RESET}")
    print(f"{GREEN}  RAG Tool: scripts/rag_search.sh 'query'{RESET}\n")

    cmd = [
        "claude",
        "--add-dir", str(knowledge_path),
        "--append-system-prompt", SYSTEM_PROMPT,
    ]

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Claude CLI stopped{RESET}")
    except FileNotFoundError:
        print(f"{RED}Claude CLI 未安装或不在 PATH 中{RESET}")
        print(f"{YELLOW}请确认 claude 已正确安装并加入 PATH{RESET}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Biblebot 启动脚本 (Claude CLI)")
    parser.add_argument("--server", action="store_true", help="仅启动 RAG 后端服务（不启动 Agent）")
    parser.add_argument("--debug", action="store_true", help="详细日志模式（BIBLEBOT_DEBUG=1，兼容 QODER_DEBUG=1）")
    parser.add_argument("--dev", action="store_true", help="开发模式: --server + --debug + 彩色提示")
    args = parser.parse_args()

    issues = check_requirements()
    if issues:
        for issue in issues:
            print(f"{YELLOW}WARNING: {issue}{RESET}")

    if args.server or args.dev:
        start_rag_server(debug=args.debug, dev=args.dev)
    else:
        start_claude()


if __name__ == "__main__":
    main()
