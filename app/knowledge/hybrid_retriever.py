"""混合检索器：向量召回 + BM25 召回 + RRF 融合。

向量检索擅长语义匹配但易遗漏关键词命中；
BM25 擅长精确关键词匹配但对同义词无能为力。
两路召回后用 RRF（Reciprocal Rank Fusion）融合排名，
取长补短提升整体召回率与排序质量。

召回链路采用 ThreadPoolExecutor 并行执行：
- 向量路与 BM25 路互不依赖，并行可显著降低端到端延迟
- 任一路失败不影响另一路，实现异常降级
- 超时后用已完成的召回结果降级，避免单路阻塞拖垮整体
"""

from __future__ import annotations

from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.knowledge.bm25 import BM25Retriever, get_bm25_retriever
from app.knowledge.embeddings import get_embedding_service
from app.knowledge.vectorstore import VectorStore, get_vector_store
from app.schemas.knowledge import RetrievedChunk, TextChunk

logger = get_logger("app.knowledge.hybrid_retriever")


class HybridRetriever:
    """混合检索器：向量 + BM25 + RRF 融合。

    BM25 索引按需构建并缓存，避免每次检索都全量重建；
    通过 _indexed_signature 标记当前索引对应的向量库状态，
    知识库变更（count 变化）时自动重建索引。
    """

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        bm25_retriever: BM25Retriever | None = None,
        vector_top_k: int | None = None,
        bm25_top_k: int | None = None,
        rrf_k: int | None = None,
        rrf_vector_weight: float | None = None,
        rrf_keyword_weight: float | None = None,
        parallel_timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._vector_store = vector_store
        self._bm25_retriever = bm25_retriever
        # 未显式传入时使用全局配置，便于 .env 调参
        self._vector_top_k = vector_top_k or settings.VECTOR_TOP_K
        self._bm25_top_k = bm25_top_k or settings.BM25_TOP_K
        self._rrf_k = rrf_k or settings.RRF_K
        self._rrf_vector_weight = rrf_vector_weight or settings.RRF_VECTOR_WEIGHT
        self._rrf_keyword_weight = rrf_keyword_weight or settings.RRF_KEYWORD_WEIGHT
        # 并行召回超时：超过该时间未完成的 future 不再等待，用已完成结果降级
        self._parallel_timeout: float = (
            parallel_timeout
            if parallel_timeout is not None
            else float(settings.RETRIEVAL_PARALLEL_TIMEOUT)
        )

        # BM25 索引缓存：避免每次检索都重建
        self._indexed_count: int = 0  # 当前 BM25 索引对应的向量库条目数

    @property
    def vector_store(self) -> VectorStore:
        """延迟初始化向量库单例。"""
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store

    @property
    def bm25_retriever(self) -> BM25Retriever:
        """延迟初始化 BM25 检索器单例。"""
        if self._bm25_retriever is None:
            self._bm25_retriever = get_bm25_retriever()
        return self._bm25_retriever

    def retrieve(
        self,
        question: str,
        top_k: int = 20,
        score_threshold: float = 0.0,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """混合检索：向量 + BM25 双路并行召回后 RRF 融合。

        流程：
        1. 向量召回与 BM25 召回并行执行（ThreadPoolExecutor）
        2. 任一路失败不影响另一路，实现异常降级
        3. 超时后用已完成的召回结果降级，避免单路阻塞拖垮整体
        4. RRF 加权融合两路排名
        5. 取 top_k 并按阈值过滤
        """
        if not question or not question.strip():
            return []

        # BM25 召回辅助函数：先确保索引构建，再执行检索
        # 不修改 _bm25_retrieve 本身；索引构建与向量召回并行进行以节省总耗时
        def _bm25_retrieve_safe(q: str) -> list[tuple[str, float]]:
            self._ensure_bm25_index()
            return self._bm25_retrieve(q)

        vector_hits: list[dict[str, Any]] = []
        bm25_hits: list[tuple[str, float]] = []

        # 双路并行召回：max_workers=2 足够，避免无谓线程开销
        with ThreadPoolExecutor(max_workers=2) as executor:
            vector_future: Future[list[dict[str, Any]]] = executor.submit(
                self._vector_retrieve, question, where
            )
            bm25_future: Future[list[tuple[str, float]]] = executor.submit(
                _bm25_retrieve_safe, question
            )

            future_to_name: dict[Future[Any], str] = {
                vector_future: "vector",
                bm25_future: "bm25",
            }

            # as_completed 在每个 future 完成时 yield；timeout 控制总等待上限
            try:
                for future in as_completed(
                    [vector_future, bm25_future],
                    timeout=self._parallel_timeout,
                ):
                    name = future_to_name[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        # 单路召回失败：记录告警，跳过该路，另一路仍可正常融合
                        logger.warning("%s 召回失败，降级跳过该路：%s", name, exc)
                        continue
                    if name == "vector":
                        vector_hits = result
                        logger.debug(
                            "向量召回：question=%r 命中=%d",
                            question[:30],
                            len(vector_hits),
                        )
                    else:
                        bm25_hits = result
                        logger.debug(
                            "BM25 召回：question=%r 命中=%d",
                            question[:30],
                            len(bm25_hits),
                        )
            except FuturesTimeoutError:
                # 超时：用已完成的结果降级，未完成的 future 在 with 退出后由 executor 清理
                logger.warning(
                    "并行召回超时（%.1fs），使用已完成的召回结果降级",
                    self._parallel_timeout,
                )
                for future, name in future_to_name.items():
                    # 仅取已完成且未抛异常的结果，避免重复覆盖
                    if not future.done() or future.cancelled():
                        continue
                    try:
                        result = future.result()
                    except Exception:
                        continue
                    if name == "vector" and not vector_hits:
                        vector_hits = result
                    elif name == "bm25" and not bm25_hits:
                        bm25_hits = result

        if not vector_hits and not bm25_hits:
            return []

        # RRF 加权融合：按 chunk_id 聚合两路排名（融合逻辑保持不变）
        fused = self._rrf_fuse(vector_hits, bm25_hits)

        # 取 top_k 并转 RetrievedChunk
        # 用 chroma id 反查元数据，避免重复查询
        metadata_map = {hit["id"]: hit for hit in vector_hits}
        bm25_text_map = {
            chunk_id: self.bm25_retriever.get_text(chunk_id) for chunk_id, _ in bm25_hits
        }

        retrieved: list[RetrievedChunk] = []
        for chunk_id, rrf_score in fused[:top_k]:
            # 优先从向量召回结果取元数据（含 similarity、source 等）
            hit = metadata_map.get(chunk_id)
            if hit is not None:
                metadata = hit.get("metadata") or {}
                text = hit.get("text", "")
                # 用 RRF 分数覆盖 similarity，反映融合后真实排序
                retrieved.append(
                    RetrievedChunk(
                        text=text,
                        score=float(rrf_score),
                        source=str(metadata.get("source", "")),
                        page_number=int(metadata.get("page_number", 1) or 1),
                        section=str(metadata.get("section", "")),
                        knowledge_type=str(metadata.get("knowledge_type", "doc")),
                    )
                )
            else:
                # 仅 BM25 命中：用 BM25 文本，元数据缺失填默认值
                text = bm25_text_map.get(chunk_id) or ""
                if not text:
                    continue
                retrieved.append(
                    RetrievedChunk(
                        text=text,
                        score=float(rrf_score),
                        source="",
                        page_number=1,
                        section="",
                        knowledge_type="doc",
                    )
                )

        # 阈值过滤：RRF 分数无统一量纲，仅在 >0 时保留
        if score_threshold > 0:
            retrieved = [c for c in retrieved if c.score >= score_threshold]

        logger.info(
            "混合检索完成：question=%r 融合后命中=%d 取 top=%d",
            question[:30],
            len(fused),
            len(retrieved),
        )
        return retrieved

    def _vector_retrieve(self, question: str, where: dict[str, Any] | None) -> list[dict[str, Any]]:
        """向量召回：embed_query → vectorstore.query。"""
        embedding_service = get_embedding_service()
        query_embedding = embedding_service.embed_query(question)
        if not query_embedding:
            logger.warning("问题向量化为空，跳过向量召回")
            return []
        return self.vector_store.query(
            query_embedding=query_embedding,
            top_k=self._vector_top_k,
            score_threshold=0.0,  # 融合阶段统一过滤，召回阶段放宽
            where=where,
        )

    def _bm25_retrieve(self, question: str) -> list[tuple[str, float]]:
        """BM25 关键词召回。"""
        return self.bm25_retriever.search(question, top_k=self._bm25_top_k)

    def _ensure_bm25_index(self) -> None:
        """确保 BM25 索引已构建且与向量库同步。

        通过比较向量库条目数判断是否需要重建：
        - 索引为空或条目数不一致时重建
        - 避免每次检索都全量重建，节省 CPU 与内存
        """
        current_count = self.vector_store.count()
        if self.bm25_retriever.size == 0 or self._indexed_count != current_count:
            logger.info(
                "BM25 索引需重建：当前=%d 已索引=%d",
                current_count,
                self._indexed_count,
            )
            self._build_bm25_index()
            self._indexed_count = current_count

    def _build_bm25_index(self) -> None:
        """从向量库拉取全部 chunks 构建 BM25 索引。"""
        all_hits = self.vector_store.get_all_chunks()
        if not all_hits:
            logger.warning("向量库为空，BM25 索引未构建")
            return

        # 用 chroma id 作为 chunk_id，便于 RRF 融合时跨路匹配
        chunks: list[TextChunk] = []
        ids: list[str] = []
        for hit in all_hits:
            chunks.append(TextChunk(text=hit.get("text", "")))
            ids.append(str(hit.get("id", "")))

        self.bm25_retriever.index(chunks, ids=ids)

    def _rrf_fuse(
        self,
        vector_hits: list[dict[str, Any]],
        bm25_hits: list[tuple[str, float]],
    ) -> list[tuple[str, float]]:
        """RRF 加权融合：score = Σ weight_i * 1/(k + rank_i)。

        rank 从 1 开始（rank=1 表示该路第 1 名）；
        k=60 是经验值，平滑排名差异避免 top-1 过度主导。
        """
        scores: dict[str, float] = {}

        # 向量路：按 similarity 降序赋 rank
        vector_ranked = sorted(vector_hits, key=lambda h: h.get("similarity", 0.0), reverse=True)
        for rank, hit in enumerate(vector_ranked, start=1):
            chunk_id = str(hit.get("id", ""))
            if not chunk_id:
                continue
            scores[chunk_id] = scores.get(chunk_id, 0.0) + self._rrf_vector_weight * (
                1.0 / (self._rrf_k + rank)
            )

        # 关键词路：BM25 已按分数降序返回，直接赋 rank
        for rank, (chunk_id, _) in enumerate(bm25_hits, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + self._rrf_keyword_weight * (
                1.0 / (self._rrf_k + rank)
            )

        # 按融合分数降序排列
        fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return fused


# 模块级单例：无状态，进程内复用
_hybrid_retriever: HybridRetriever | None = None


def get_hybrid_retriever() -> HybridRetriever:
    """获取 HybridRetriever 单例。"""
    global _hybrid_retriever
    if _hybrid_retriever is None:
        _hybrid_retriever = HybridRetriever()
    return _hybrid_retriever


def reset_hybrid_retriever() -> None:
    """重置单例，便于测试切换配置。"""
    global _hybrid_retriever
    _hybrid_retriever = None
