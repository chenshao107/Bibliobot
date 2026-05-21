#!/bin/bash
# ============================================================
# rag_search.sh — RAG 知识库语义搜索 wrapper
#
# 用法: rag_search.sh "查询词"
#
# 自动处理 python 路径、工作目录等兼容性问题，
# 让 Agent 无需关心底层是 python3 还是 .venv
# ============================================================
set -euo pipefail

# 找到本脚本所在目录（兼容符号链接）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/rag_search.py"

# 寻找可用的 python3 解释器
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: 找不到 python3 解释器" >&2
    exit 1
fi

# 切换到项目根目录（rag_search.py 需要相对 import app.*）
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$PROJECT_ROOT"

# 添加到 PATH 避免 Agent 找不到相关命令
export PATH="${SCRIPT_DIR}:${PROJECT_ROOT}/.venv/bin:${PATH}"

exec "$PYTHON" "$PY_SCRIPT" "$@"
