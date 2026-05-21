# Biblebot 日志与调试指南

## 日志框架

使用 `loguru`，配置在 `app/main.py` 启动时初始化。

### 日志级别

| 环境变量 | 级别 | 内容 |
|---------|------|------|
| 未设置 / `QODER_DEBUG=false` | INFO | 会话创建/销毁、qodercli 退出码、TOOL_RESULT/TOOL_ERROR |
| `QODER_DEBUG=true` | DEBUG | 以上 + 每条 Qoder NDJSON raw 消息 |

### 日志输出目标

`sys.stderr` — 在 Docker 中通过 `docker logs` 查看。

### 日志格式

```
HH:mm:ss.SSS | LEVEL   | message
```

示例：
```
11:17:48.956 | INFO    | Retrieved 31 unique hits from 4 query variations
11:19:20.725 | WARNING | qodercli exit=42: Session ID ... is already in use.
```

---

## 调试步骤

### 1. 开启 DEBUG 模式

```bash
# docker-compose 中加环境变量
environment:
  - QODER_DEBUG=true

# 或 docker run
docker run ... -e QODER_DEBUG=true ...
```

### 2. 查看日志

```bash
# 实时跟踪
docker logs -f biblebot-server

# 只看最近的
docker logs biblebot-server --tail 100

# 过滤错误
docker logs biblebot-server 2>&1 | grep -iE "ERROR|WARNING"
```

### 3. 关键日志标记

| 标记 | 含义 | 位置 |
|------|------|------|
| `[QODER_RAW]` | Qoder NDJSON 原始消息（需 DEBUG） | `qoder_session.py:113` |
| `qodercli exit=N` | Qoder 子进程退出码，非 0 即为异常 | `qoder_session.py:67` |
| `[TOOL_RESULT]` | Agent 工具执行输出（仅日志） | `routes.py:264/301` |
| `[TOOL_ERROR]` | Agent 工具执行报错（仅日志） | `routes.py:267/304` |
| `Session created` | 新会话建立 | `session_pool.py:94` |
| `Session idle timeout` | 会话因空闲被清理 | `session_pool.py:108` |

### 4. Qoder 退出码速查

| 码 | 含义 |
|----|------|
| 0 | 正常结束 |
| 42 | Session ID 已被占用（残留进程或锁文件） |

### 5. 常见问题排查

**Agent 说 "RAG 搜索不可用"**
1. 检查 `[QODER_RAW]` 日志中 type=user 的 tool_result
2. 看到 `Error: Tool "bash" not found.` → prompt 里工具名大小写不对，应该是 `Bash`
3. 看到 `Error: Permission confirmation required` → `permission_mode` 不是 `bypass_permissions`

**RAG 返回 0 结果**
1. 检查 Qdrant 是否有数据：
```bash
docker exec biblebot-server python3 -c "
from qdrant_client import QdrantClient
from app.core.config import settings
c = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
print(c.get_collection('kb_hybrid').points_count)
"
```
2. 如果是 0，清除缓存重新入库：
```bash
rm -rf data/embeddings/* data/chunks/*
python scripts/ingest_folder.py
```

**Session ID already in use (exit=42)**
- 清空 Qoder session 目录：`docker exec biblebot-server rm -rf /root/.qoder/projects/*`
- 或者换一个不同的用户提问（不同的问题会产生不同的 UUID session key）

**响应为空 content=""**
- 查看 `docker logs` 中 `qodercli exit=N:` 行
- exit=42 且 stderr 包含 "already in use" → 按上条处理
