import requests
from typing import List, Dict, Any, Optional
import numpy as np
from loguru import logger
from app.core.config import settings
import re
import json
import time
from pathlib import Path
from collections import Counter

# SentenceTransformer 懒加载（仅本地模式需要，服务器用 API 模式无需安装）
_dense_model = None


def _get_dense_model():
    global _dense_model
    if _dense_model is None:
        from sentence_transformers import SentenceTransformer
        _dense_model = SentenceTransformer(settings.EMBEDDING_MODEL_NAME)
    return _dense_model


# tqdm 懒加载
def _get_tqdm():
    try:
        from tqdm import tqdm
        return tqdm, True
    except ImportError:
        return None, False

class HybridEmbedder:
    def __init__(self):
        self.use_api = settings.USE_EMBEDDING_API
        if not self.use_api:
            logger.info(f"Loading local dense embedding model: {settings.EMBEDDING_MODEL_NAME}")
            self.dense_model = _get_dense_model()
        else:
            logger.info(f"Using Cloud API for embeddings: {settings.EMBEDDING_API_URL}")
        
        # Vocabulary for sparse encoding (simplified)
        self.sparse_vocab_size = 10000
        
        # API速率限制：记录上次调用时间
        self._last_api_call_time = 0
        self._min_call_interval = 0.1  # 每次API调用间隔至少100ms

    def embed_dense(self, text: str) -> List[float]:
        """Generates a dense vector."""
        if self.use_api:
            # API速率限制
            elapsed = time.time() - self._last_api_call_time
            if elapsed < self._min_call_interval:
                time.sleep(self._min_call_interval - elapsed)
            
            result = self._embed_dense_api(text)
            self._last_api_call_time = time.time()
            return result
        
        embedding = self.dense_model.encode(text)
        return embedding.tolist()
    
    def embed_dense_batch(self, texts: List[str], batch_size: int = 32, show_progress: bool = True, desc: str = "Embedding") -> List[List[float]]:
        """
        批量生成dense向量，提升效率
        batch_size: 每批处理的文本数量
        show_progress: 是否显示进度条
        desc: 进度条描述
        """
        if self.use_api:
            return self._embed_dense_api_batch(texts, batch_size, show_progress, desc)
        
        # 本地模型批量处理，带进度条
        if show_progress and len(texts) > 1:
            tqdm, _ = _get_tqdm()
            if tqdm:
                all_embeddings = []
                total_batches = (len(texts) + batch_size - 1) // batch_size
                # position=1 让嵌入进度条显示在文件进度条上方
                for i in tqdm(range(0, len(texts), batch_size), desc=f"  🔤 {desc}", unit="batch", 
                             total=total_batches, position=1, leave=False, ncols=90):
                    batch = texts[i:i + batch_size]
                    batch_embeddings = self.dense_model.encode(batch)
                    all_embeddings.extend([emb.tolist() for emb in batch_embeddings])
                return all_embeddings
        else:
            # 无进度条或数据量小，直接处理
            embeddings = self.dense_model.encode(texts)
            return [emb.tolist() for emb in embeddings]

    def _embed_dense_api(self, text: str) -> List[float]:
        """Calls Cloud API for dense embedding with retry mechanism."""
        max_retries = settings.EMBEDDING_API_MAX_RETRIES if hasattr(settings, 'EMBEDDING_API_MAX_RETRIES') else 3
        retry_delay = settings.EMBEDDING_API_RETRY_DELAY if hasattr(settings, 'EMBEDDING_API_RETRY_DELAY') else 2  # 秒
        api_timeout = settings.EMBEDDING_API_TIMEOUT if hasattr(settings, 'EMBEDDING_API_TIMEOUT') else 30  # 秒
        
        for attempt in range(max_retries):
            try:
                headers = {
                    "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": settings.EMBEDDING_MODEL_NAME,
                    "input": text,
                    "encoding_format": "float"
                }
                response = requests.post(
                    settings.EMBEDDING_API_URL, 
                    json=payload, 
                    headers=headers,
                    timeout=api_timeout
                )
                response.raise_for_status()
                return response.json()["data"][0]["embedding"]
                
            except requests.exceptions.HTTPError as e:
                # 获取详细错误信息
                status_code = e.response.status_code
                try:
                    error_detail = e.response.json()
                except:
                    error_detail = e.response.text[:500]
                
                # 401/403 是认证/权限错误，重试无用
                if status_code in (401, 403):
                    logger.error(f"Embedding API 认证失败 ({status_code}): {error_detail}")
                    logger.error(f"请检查 EMBEDDING_API_KEY 是否正确，以及是否有权限访问模型 {settings.EMBEDDING_MODEL_NAME}")
                    raise e
                
                # 429 是限流，需要重试
                if status_code == 429:
                    if attempt < max_retries - 1:
                        logger.warning(f"Embedding API 限流 (429)，等待 {retry_delay} 秒后重试...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                
                # 其他 HTTP 错误
                if attempt < max_retries - 1:
                    logger.warning(f"Embedding API调用失败 (尝试 {attempt + 1}/{max_retries}): {status_code} - {error_detail}")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.error(f"Embedding API调用失败，已达最大重试次数: {status_code} - {error_detail}")
                    raise e
                    
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Embedding API调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                    logger.info(f"等待 {retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # 指数退避
                else:
                    logger.error(f"Embedding API调用失败，已达最大重试次数: {e}")
                    raise e
    
    def _embed_dense_api_batch(self, texts: List[str], batch_size: int = 32, show_progress: bool = True, desc: str = "API Embedding") -> List[List[float]]:
        """
        批量调用API进行embedding
        将大量文本分批处理，减少API调用次数
        """
        all_embeddings = []
        total_batches = (len(texts) + batch_size - 1) // batch_size
        
        # 创建进度条迭代器
        if show_progress:
            tqdm, has_tqdm = _get_tqdm()
            if has_tqdm:
                batch_iterator = tqdm(range(0, len(texts), batch_size), desc=desc, total=total_batches, unit="batch")
            else:
                batch_iterator = range(0, len(texts), batch_size)
        else:
            batch_iterator = range(0, len(texts), batch_size)
        
        for i in batch_iterator:
            batch = texts[i:i + batch_size]
            batch_num = i // batch_size + 1
            
            if not show_progress or not has_tqdm:
                logger.info(f"处理批次 {batch_num}/{total_batches}，包含 {len(batch)} 个文本")
            
            max_retries = 3
            retry_delay = 2
            
            for attempt in range(max_retries):
                try:
                    # API速率限制
                    elapsed = time.time() - self._last_api_call_time
                    if elapsed < self._min_call_interval:
                        time.sleep(self._min_call_interval - elapsed)
                    
                    headers = {
                        "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": settings.EMBEDDING_MODEL_NAME,
                        "input": batch,  # 批量输入
                        "encoding_format": "float"
                    }
                    response = requests.post(
                        settings.EMBEDDING_API_URL,
                        json=payload,
                        headers=headers,
                        timeout=60  # 批量处理需要更长超时
                    )
                    response.raise_for_status()
                    
                    # 提取所有embedding
                    batch_embeddings = [item["embedding"] for item in response.json()["data"]]
                    all_embeddings.extend(batch_embeddings)
                    
                    self._last_api_call_time = time.time()
                    break  # 成功则跳出重试循环
                    
                except requests.exceptions.RequestException as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"批次 {batch_num} API调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                        logger.info(f"等待 {retry_delay} 秒后重试...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        logger.error(f"批次 {batch_num} API调用失败，已达最大重试次数: {e}")
                        raise e
        
        return all_embeddings

    def embed_sparse(self, text: str) -> Dict[str, Any]:
        """
        Generates a sparse vector using a simplified hashing approach.
        In a production environment, you might use SPLADE or a real BM25 tokenizer.
        """
        tokens = self._tokenize(text)
        counts = Counter(tokens)
        
        # 使用字典来处理哈希冲突，合并相同index的值
        index_value_map = {}
        
        for token, count in counts.items():
            # Simple hashing to map tokens to a fixed index space
            idx = hash(token) % self.sparse_vocab_size
            # 如果index已存在，累加值（处理哈希冲突）
            if idx in index_value_map:
                index_value_map[idx] += float(count)
            else:
                index_value_map[idx] = float(count)
        
        # 转换为排序的列表（Qdrant要求indices有序）
        sorted_items = sorted(index_value_map.items())
        indices = [idx for idx, _ in sorted_items]
        values = [val for _, val in sorted_items]
            
        return {
            "indices": indices,
            "values": values
        }

    def _tokenize(self, text: str) -> List[str]:
        # Simple regex tokenizer
        text = text.lower()
        tokens = re.findall(r'\w+', text)
        # Filter short tokens
        return [t for t in tokens if len(t) > 1]

    def get_dim(self) -> int:
        if self.use_api:
            return settings.EMBEDDING_DIM
        return self.dense_model.get_sentence_embedding_dimension()

    def save_embedding_metadata(self, doc_id: str, chunks_embeddings: List[Dict[str, Any]]):
        """
        Saves embedding metadata for tuning reference.
        chunks_embeddings: List of {chunk_index, dense_dim, sparse_indices_count, content_preview}
        """
        if not settings.SAVE_INTERMEDIATE_FILES:
            return
            
        output_dir = Path(settings.DATA_EMBEDDINGS_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        output_path = output_dir / f"{Path(doc_id).stem}_embeddings.json"
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({
                "doc_id": doc_id,
                "embedding_model": settings.EMBEDDING_MODEL_NAME,
                "use_api": self.use_api,
                "dense_dim": self.get_dim(),
                "sparse_vocab_size": self.sparse_vocab_size,
                "total_chunks": len(chunks_embeddings),
                "chunks": chunks_embeddings
            }, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Saved embedding metadata to {output_path}")
        return output_path
