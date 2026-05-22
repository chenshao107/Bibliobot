# Biblebot — 企业知识探索 Agent

基于 **轻RAG + 强探索** 架构的企业知识库 Agent 系统。RAG 负责定位文档，Claude CLI 作为 Agent Runtime 负责探索与分析。

## 架构

```
用户 ──→ Claude CLI (Agent Runtime)
              │  ┌─ Bash (cat/grep/rg/find/tree) ─→ 探索知识库
              │  └─ python scripts/rag_search.py ─→ 语义检索 RAG 后端
              │
       ┌──────┴──────┐
       │  FastAPI     │  ← RAG 检索服务 (Qdrant + Rerank)
       │  Qdrant      │  ← 向量数据库
       └─────────────┘
```

- **RAG 后端**: 语义搜索返回文档路径 + 片段（仅定位，不返回完整答案）
- **Agent**: Claude CLI 原生 Bash/Read/Grep 等工具探索知识库，`rag_search` CLI 工具辅助定位
- **知识库**: `data/canonical_md` 以只读方式挂载，Agent 不可修改

## 快速开始

### 1. 环境准备

```bash
python3 -m venv .venv && source .venv/bin/activate  # 创建虚拟环境
pip install -r requirements.txt
cp .env.example .env   # 编辑 .env 填入 LLM API Key 等配置
```

> **国内用户**：Docker 需配置 registry mirror 才能拉取基础镜像：
> ```bash
> sudo tee /etc/docker/daemon.json <<< '{"registry-mirrors":["https://docker.1ms.run"]}'
> sudo systemctl restart docker
> ```

### 2. 启动 Qdrant

```bash
cd docker && docker-compose up -d
```

### 3. 导入知识库

```bash
# 将文档放入 data/raw，然后运行：
python scripts/ingest_folder.py
```

### 4. 启动 Agent

```bash
# 默认使用 Claude CLI
python start.py

# 仅启动 RAG 后端（不启动 Agent）
python start.py --server
```

### 5. 测试 RAG 检索

```bash
python scripts/rag_search.py "你的问题" --json
```

或访问 `http://localhost:8000/docs` 使用 Swagger UI 测试 API。

### 6. (可选) 对接 MCP 服务器

Claude CLI 原生支持 MCP (Model Context Protocol)，可接入外部工具如 Phabricator、Jira 等：

```bash
# 复制模板并编辑
cp mcp_config.json.example mcp_config.json
# 编辑 mcp_config.json 填入你的 MCP 服务器配置
```

配置后无需重启服务，claude 子进程会自动加载。支持两种方式：
- **文件方式**(推荐)：`.env` 中设置 `MCP_CONFIG_PATH=mcp_config.json`
- **内联方式**：`.env` 中设置 `MCP_CONFIG_JSON={"mcpServers":{...}}`

## 目录结构

```
├── app/
│   ├── api/routes.py         # FastAPI 路由 (/api/query 检索端点)
│   ├── core/config.py        # 配置管理
│   ├── services/
│   │   ├── ingestion/        # 文档摄取 (converter → chunker → indexer)
│   │   ├── rag/              # RAG 全流程 (rewriter → embedder → retriever → reranker)
│   │   └── storage/          # Qdrant 客户端
│   └── main.py               # 应用入口
├── scripts/
│   ├── rag_search.py         # RAG 语义搜索 CLI 工具
│   ├── ingest_folder.py      # 批量导入文档
│   └── ...                   # 其他运维脚本
├── prompts/                  # 系统提示词模板
├── docker/                   # Docker Compose & Dockerfile
├── data/
│   ├── raw/                  # 原始文档
│   └── canonical_md/         # 规范化 Markdown（Agent 只读探索）
└── start.py                  # 启动脚本
```

## RAG 检索 API

```
POST /api/query
{
  "query": "搜索内容",
  "top_k": 5,
  "category": null
}
```

返回格式（轻量，只返回定位信息）：
```json
{
  "results": [
    {
      "path": "data/canonical_md/xxx.md",
      "title": "章节名",
      "score": 0.95,
      "snippet": "匹配片段（最多200字符）..."
    }
  ],
  "query": "搜索内容",
  "total": 5
}
```

## 工作流程

Agent 的标准探索流程：

1. **语义定位**: `python scripts/rag_search.py "query"` 找到候选文档路径
2. **文件探索**: 使用 Bash 工具阅读实际文件（cat/grep/rg/head/tail）
3. **深入分析**: 交叉引用多个文档，提取完整上下文
4. **生成答案**: 基于原始文档内容给出准确回答

## 环境变量

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | LLM API 密钥 |
| `LLM_BASE_URL` | LLM API 地址 |
| `LLM_MODEL` | 模型名称 |
| `EMBEDDING_API_KEY` | Embedding API 密钥 |
| `EMBEDDING_BASE_URL` | Embedding API 地址 |
| `EMBEDDING_MODEL` | Embedding 模型 |
| `QDRANT_HOST` | Qdrant 服务地址 |
| `QDRANT_PORT` | Qdrant 端口（默认 6333） |
| `RERANK_API_KEY` | Rerank API 密钥 |
| `RERANK_BASE_URL` | Rerank API 地址 |
| `RERANK_MODEL` | Rerank 模型 |

## Docker 部署

```bash
cd docker
docker-compose up -d   # 启动 Qdrant + Biblebot RAG 服务
```

`docker-compose.yml` 包含两个服务：
- **qdrant**: 向量数据库
- **biblebot**: RAG 检索服务 + 知识库只读挂载

> 首次构建需下载 PyTorch（~2GB），国内默认走清华 pip 镜像。构建完成后访问 `http://localhost:8000/docs` 测试 API。
