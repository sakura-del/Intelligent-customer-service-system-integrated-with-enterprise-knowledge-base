"""知识库检索器。

封装 embed_query → vectorstore.query → 阈值过滤的完整召回链路，
向 RAG Agent 提供统一 retrieve 接口，屏蔽底层向量库细节。
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.knowledge.embeddings import get_embedding_service
from app.knowledge.vectorstore import VectorStore, get_vector_store
from app.schemas.knowledge import RetrievedChunk

logger = get_logger("app.knowledge.retriever")


class KnowledgeRetriever:
    """单 Agent RAG 检索器。

    持有 Embedding 服务与 VectorStore 引用，
    通过默认相似度阈值过滤弱相关结果，避免噪声进入 prompt。
    """

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        similarity_threshold: float | None = None,
    ) -> None:
        # 延迟取单例，避免在导入阶段触发模型加载
        self._vector_store = vector_store
        settings = get_settings()
        # 未显式传入时使用全局配置，保证阈值可由 .env 调整
        self._similarity_threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else settings.SIMILARITY_THRESHOLD
        )

    @property
    def vector_store(self) -> VectorStore:
        """延迟初始化向量库单例，避免导入时即建立连接。"""
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store

    def retrieve(
        self,
        question: str,
        top_k: int = 5,
        score_threshold: float | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """检索与 question 最相关的知识片段。

        流程：向量化查询 → 向量库召回 → 阈值过滤 → 转 RetrievedChunk。
        score_threshold 为 None 时使用实例默认阈值，便于调用方按场景精细控制。
        """
        if not question or not question.strip():
            return []

        threshold = score_threshold if score_threshold is not None else self._similarity_threshold

        # 1. 把问题向量化；失败时返回空列表，避免拖垮后续生成
        embedding_service = get_embedding_service()
        query_embedding = embedding_service.embed_query(question)
        if not query_embedding:
            logger.warning("问题向量化为空，跳过检索：%s", question[:50])
            return []

        # 2. 向量库召回：top_k 内置在 vectorstore.query 中，再按阈值过滤
        raw_hits = self.vector_store.query(
            query_embedding=query_embedding,
            top_k=top_k,
            score_threshold=threshold,
            where=where,
        )

        # 3. 仅保留 RAG 必要字段，丢弃 id 等内部结构，控制下游 prompt token
        retrieved: list[RetrievedChunk] = []
        for hit in raw_hits:
            metadata = hit.get("metadata") or {}
            retrieved.append(
                RetrievedChunk(
                    text=hit.get("text", ""),
                    score=float(hit.get("similarity", 0.0)),
                    source=str(metadata.get("source", "")),
                    page_number=int(metadata.get("page_number", 1) or 1),
                    section=str(metadata.get("section", "")),
                    knowledge_type=str(metadata.get("knowledge_type", "doc")),
                )
            )

        logger.info(
            "检索完成：question=%r top_k=%d 命中=%d 阈值=%.2f",
            question[:30],
            top_k,
            len(retrieved),
            threshold,
        )
        return retrieved


# 模块级单例：检索器无状态，进程内复用即可
_retriever: KnowledgeRetriever | None = None


def get_retriever() -> KnowledgeRetriever:
    """获取 KnowledgeRetriever 单例。"""
    global _retriever
    if _retriever is None:
        _retriever = KnowledgeRetriever()
    return _retriever


def reset_retriever() -> None:
    """重置单例，便于测试切换配置。"""
    global _retriever
    _retriever = None
