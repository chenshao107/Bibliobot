#!/usr/bin/env python3
"""
rag_search - 企业知识库语义搜索 CLI 工具

遵循"轻RAG + 强探索"原则：
- RAG 只负责"定位"候选文档（返回路径 + 分数 + 片段）
- Agent 负责"探索"获取精确答案

用法:
    python scripts/rag_search.py "你的问题"
    python scripts/rag_search.py "你的问题" --json
    python scripts/rag_search.py "你的问题" --top_k 10 --category RK3506
"""

import argparse
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="rag_search - 企业知识库语义搜索")
    parser.add_argument("query", help="搜索查询")
    parser.add_argument("--top_k", "-k", type=int, default=5, help="返回结果数量（默认: 5）")
    parser.add_argument("--category", "-c", default=None, help="按类别过滤（如: RK3506）")
    parser.add_argument("--json", "-j", action="store_true", help="JSON 格式输出")
    parser.add_argument("--max_snippet_len", type=int, default=200, help="摘要最大长度（默认: 200）")
    args = parser.parse_args()

    try:
        from app.services.rag.retriever import RAGEngine

        rag = RAGEngine()
        raw = rag.search(
            query=args.query,
            top_k=args.top_k,
            category_filter=args.category,
        )

        results = []
        for r in raw:
            if isinstance(r, dict):
                payload = r.get("payload", {})
                score = r.get("score", 0)
            else:
                payload = getattr(r, "payload", {})
                score = getattr(r, "score", 0)

            path = payload.get("canonical_path", payload.get("doc_id", "?"))
            section = payload.get("section", "")
            content = payload.get("content", "")

            snip = content[:args.max_snippet_len]
            if len(content) > args.max_snippet_len:
                snip += "..."

            results.append({
                "path": path,
                "title": section if section and section != "Root" else "",
                "score": score,
                "snippet": snip,
            })

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            if not results:
                print("No results found.")
                return
            print(f"Found {len(results)} results:\n")
            for i, r in enumerate(results, 1):
                print(f"[{i}] {r['path']}  (score: {r['score']:.3f})")
                if r["title"]:
                    print(f"    Section: {r['title']}")
                print(f"    {r['snippet']}")
                print()

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
