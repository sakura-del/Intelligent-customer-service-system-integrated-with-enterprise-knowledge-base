"""重排序器（Reranker）。

混合检索后用 CrossEncoder 对 query-chunk 对做精排：
- 优先加载 sentence-transformers 的 CrossEncoder（BGE-reranker-base）
- 模型下载失败/无网络时降级到 cosine 相似度重排序（复用 embedding 向量）

为什么需要重排序：双路召回侧重召回率，排序质量有限；
CrossEncoder 直接建模 (query, doc) 交互，比双塔向量更精准。
"""

from __future__ import annotations

import math

from app.core.config import get_settings
from app.core.logging import get_logger
from app.knowledge.embeddings import get_embedding_service
from app.schemas.knowledge import RetrievedChunk

logger = get_logger("app.knowledge.reranker")

# cosine fallback 时向量缓存上限：避免大 chunk 集合重复 embed 占内存
_FALLBACK_CACHE_LIMIT = 200


class Reranker:
    """重排序器：CrossEncoder 优先，cosine 兜底。

    模型延迟加载：首次 rerank 时才尝试加载，避免导入阶段拖慢启动。
    加载失败后标记 _use_fallback，后续不再重试，节省开销。
    """

    def __init__(self, model_name: str | None = None) -> None:
        settings = get_settings()
        self._model_name = model_name or settings.RERANKER_MODEL
        self._model = None
        self._loaded = False
        self._use_fallback = False
        # 缓存 chunk 向量避免重复 embed（fallback 模式专用）
        self._embedding_cache: dict[str, list[float]] = {}

    @property
    def is_fallback(self) -> bool:
        """是否处于 fallback 模式（cosine 相似度重排序）。"""
        # 未加载时不算 fallback；加载尝试后才确定
        return self._loaded and self._use_fallback

    @property
    def mode(self) -> str:
        """当前 reranker 模式：unknown / cross_encoder / fallback。

        unknown 表示尚未尝试加载；加载后根据结果返回具体模式。
        """
        if not self._loaded:
            return "unknown"
        return "fallback" if self._use_fallback else "cross_encoder"

    def _ensure_model(self) -> None:
        """延迟加载 CrossEncoder，仅首次调用时执行。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            from sentence_transformers import CrossEncoder

            logger.info("加载 Reranker 模型：%s", self._model_name)
            self._model = CrossEncoder(self._model_name)
            # warmup：触发一次小请求验证模型可用
            _ = self._model.predict([("warmup", "warmup")])
            logger.info("CrossEncoder 加载成功：%s", self._model_name)
        except Exception as exc:
            # 无网络/模型下载失败时降级，打印警告便于排查
            logger.warning(
                "Reranker 模型 %s 加载失败，降级到 cosine 相似度重排序：%s",
                self._model_name,
                exc,
            )
            self._model = None
            self._use_fallback = True

    def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        """对 chunks 重排序，返回 top_k 结果。

        CrossEncoder 模式：直接预测 (query, chunk) 对分数；
        fallback 模式：用 embedding cosine 相似度作为分数。
        分数归一化到 [0, 1] 便于阈值过滤。
        """
        if not query or not chunks:
            return []

        self._ensure_model()

        if self._use_fallback or self._model is None:
            ranked = self._rerank_with_cosine(query, chunks)
        else:
            ranked = self._rerank_with_cross_encoder(query, chunks)

        # 取 top_k 并返回
        return ranked[:top_k]

    def _rerank_with_cross_encoder(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """用 CrossEncoder 预测分数并重排序。

        CrossEncoder 输出 logits，通过 sigmoid 归一化到 [0, 1]，
        便于与 similarity 阈值（如 0.6）配合过滤。
        """
        assert self._model is not None
        # 构造 (query, chunk) 对，批量预测节省开销
        pairs = [(query, chunk.text) for chunk in chunks]
        try:
            scores = self._model.predict(pairs)
        except Exception as exc:
            # 预测失败时降级到 cosine，保证链路不中断
            logger.warning("CrossEncoder 预测失败，降级到 cosine 重排序：%s", exc)
            return self._rerank_with_cosine(query, chunks)

        # sigmoid 归一化：logits -> [0, 1]
        ranked: list[RetrievedChunk] = []
        for chunk, score in zip(chunks, scores):
            normalized = 1.0 / (1.0 + math.exp(-float(score)))
            ranked.append(chunk.model_copy(update={"score": normalized}))
        # 按分数降序
        ranked.sort(key=lambda c: c.score, reverse=True)
        return ranked

    def _rerank_with_cosine(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """用 embedding cosine 相似度作为重排序分数。

        复用 EmbeddingService：query 向量与每个 chunk 文本向量计算 cosine；
        chunk 向量缓存避免重复 embed（同会话多次 rerank 时受益）。
        """
        embedding_service = get_embedding_service()
        query_vec = embedding_service.embed_query(query)
        if not query_vec:
            # query 向量化失败：保持原顺序，分数不变
            return list(chunks)

        ranked: list[RetrievedChunk] = []
        for chunk in chunks:
            chunk_vec = self._get_cached_embedding(chunk, embedding_service)
            if not chunk_vec:
                # chunk 向量化失败：保留原分数
                ranked.append(chunk)
                continue
            cosine = self._cosine_similarity(query_vec, chunk_vec)
            # cosine 已在 [-1, 1]，归一化到 [0, 1] 便于阈值过滤
            normalized = (cosine + 1.0) / 2.0
            ranked.append(chunk.model_copy(update={"score": normalized}))
        ranked.sort(key=lambda c: c.score, reverse=True)
        return ranked

    def _get_cached_embedding(self, chunk: RetrievedChunk, embedding_service) -> list[float]:
        """获取 chunk 向量，带缓存避免重复 embed。

        缓存键用 chunk 文本哈希：相同文本不同 RetrievedChunk 实例可复用。
        缓存上限 _FALLBACK_CACHE_LIMIT 防止内存膨胀。
        """
        # 用文本前 100 字符作为缓存键，平衡碰撞率与内存
        cache_key = chunk.text[:100]
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        # 缓存超限时清空，避免内存持续增长
        if len(self._embedding_cache) >= _FALLBACK_CACHE_LIMIT:
            self._embedding_cache.clear()

        vec = embedding_service.embed_query(chunk.text)
        if vec:
            self._embedding_cache[cache_key] = vec
        return vec

    @staticmethod
    def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        """计算两个向量的 cosine 相似度。

        空向量或零向量返回 0，避免除零异常。
        """
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


# 模块级单例：模型加载昂贵，进程内复用
_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    """获取 Reranker 单例。"""
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


def reset_reranker() -> None:
    """重置单例，便于测试切换模型配置。"""
    global _reranker
    _reranker = None
