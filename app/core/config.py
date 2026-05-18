from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # Qdrant 配置
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION_NAME: str = "kb_hybrid"

    # LLM 配置（用于 query rewrite 和 embedding API）
    LLM_API_KEY: Optional[str] = None
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"

    # 词嵌入 (Embedding) 配置
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-base-zh-v1.5"
    EMBEDDING_DIM: int = 768
    USE_EMBEDDING_API: bool = False
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_API_URL: str = "https://api.siliconflow.cn/v1/embeddings"
    EMBEDDING_API_TIMEOUT: int = 60
    EMBEDDING_API_MAX_RETRIES: int = 3
    EMBEDDING_API_RETRY_DELAY: int = 2

    # 重排 (Rerank) 配置
    RERANK_MODEL_NAME: str = "ms-marco-MiniLM-L-6-v2"
    RERANK_API_KEY: Optional[str] = None
    RERANK_API_URL: str = "https://api.siliconflow.cn/v1/rerank"
    
    # 路径配置
    BASE_DIR: str = "."
    DATA_RAW_DIR: str = "data/raw"
    DATA_CANONICAL_DIR: str = "data/canonical_md"
    DATA_CHUNKS_DIR: str = "data/chunks"
    DATA_EMBEDDINGS_DIR: str = "data/embeddings"
    KNOWLEDGE_BASE_PATH: str = "data/canonical_md"
    
    # 中间文件保存配置
    SAVE_INTERMEDIATE_FILES: bool = True

    # Docling 文档转换配置
    DOCLING_DO_OCR: bool = False
    DOCLING_OCR_THREADS: int = 4

    # RAG 搜索配置
    RAG_SEARCH_DEFAULT_TOP_K: int = 5
    RAG_SEARCH_MAX_SNIPPET_LEN: int = 200

    # 线程池配置
    THREAD_POOL_MAX_WORKERS: int = 32

    # Pydantic 配置
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

settings = Settings()
