import requests
from typing import List, Dict, Any
from loguru import logger
from app.core.config import settings

# FlashRank 懒加载（仅本地模式需要，服务器用 API 模式无需安装）
_rank_model = None


def _get_ranker_and_types():
    global _rank_model
    if _rank_model is None:
        from flashrank import Ranker, RerankRequest
        _rank_model = Ranker(model_name=settings.RERANK_MODEL_NAME, cache_dir="./models")
        return _rank_model, RerankRequest
    from flashrank import RerankRequest
    return _rank_model, RerankRequest

class HybridReranker:
    """
    智能 Reranker：当检索结果数量超过 top_k 时自动启用 rerank
    无需配置，自动根据结果数量决定是否精排
    """
    
    def __init__(self):
        # 优先使用 API，如果配置完整则使用云 API，否则使用本地模型
        self.use_api = bool(settings.RERANK_API_KEY and settings.RERANK_API_URL 
                           and settings.RERANK_API_KEY != "your_api_key_here")
        
        if self.use_api:
            logger.info(f"Using Cloud API for Rerank: {settings.RERANK_API_URL}")
            self.ranker = None
            self._RerankRequest = None
        else:
            logger.info(f"Loading local Reranker model: {settings.RERANK_MODEL_NAME}")
            # FlashRank will download the model automatically
            self.ranker, self._RerankRequest = _get_ranker_and_types()

    def rerank(self, query: str, passages: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Reranks the retrieved passages based on the query.
        智能启用策略：当 passages 数量超过 top_k 时才启用 rerank
        """
        if not passages:
            return []
        
        # 智能启用判断：结果数量超过 top_k 时才需要 rerank
        if len(passages) <= top_k:
            logger.debug(f"Rerank 跳过：仅 {len(passages)} 个结果，未超过 top_k={top_k}")
            return passages[:top_k]

        if self.use_api:
            return self._rerank_api(query, passages, top_k)

        # Prepare passages for FlashRank
        flash_passages = [
            {
                "id": p.get("id", i),
                "text": p["payload"].get("content", ""),
                "meta": p["payload"]
            }
            for i, p in enumerate(passages)
        ]

        rerank_request = self._RerankRequest(query=query, passages=flash_passages)
        results = self.ranker.rerank(rerank_request)

        # FlashRank returns a list of results with scores, map back to Qdrant format
        final_results = []
        for res in results[:top_k]:
            # Find original passage by id
            original_p = next((p for p in passages if p.get("id") == res.get("id")), None)
            if original_p:
                final_results.append({
                    "id": original_p.get("id"),
                    "payload": original_p["payload"],
                    "score": res.get("score", 0)
                })
        
        logger.info(f"Reranked {len(passages)} passages down to {len(final_results)} (Local)")
        return final_results

    def _rerank_api(self, query: str, passages: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
        """Calls Cloud API for reranking."""
        try:
            headers = {
                "Authorization": f"Bearer {settings.RERANK_API_KEY}",
                "Content-Type": "application/json"
            }
            # SiliconFlow rerank format
            payload = {
                "model": settings.RERANK_MODEL_NAME,
                "query": query,
                "documents": [p["payload"].get("content", "") for p in passages],
                "top_n": top_k
            }
            response = requests.post(settings.RERANK_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            
            api_results = response.json()["results"]
            
            # Map back to Qdrant format (保持和原始检索一致的格式)
            final_results = []
            for res in api_results:
                idx = res["index"]
                original_p = passages[idx]
                final_results.append({
                    "id": original_p.get("id"),
                    "payload": original_p["payload"],
                    "score": res["relevance_score"]
                })
            
            logger.info(f"Reranked {len(passages)} passages down to {top_k} (API)")
            return final_results
            
        except Exception as e:
            logger.error(f"Error calling Rerank API: {e}")
            # Fallback to simple slice if API fails
            return passages[:top_k]
