"""版本管理与灰度验证。

提供版本回滚与灰度集合（canary collection）能力：
- rollback_version：包装 DocumentStore 的回滚，返回结构化 RollbackResult
- CanaryManager：管理独立的灰度 VectorStore（集合名 = 主集合名 + 后缀），
  支持将指定版本 chunks 写入灰度集合，并在主/灰度集合间做检索 A/B 对比

降级策略：
灰度集合初始化或查询失败时标记 canary_unavailable=True，仅返回主集合结果，
不抛异常阻断对比流程。
"""

from __future__ import annotations

from threading import RLock

from app.core.config import get_settings
from app.core.logging import get_logger
from app.knowledge.document_store import DocumentStore, get_document_store
from app.knowledge.embeddings import get_embedding_service
from app.knowledge.vectorstore import VectorStore, get_vector_store
from app.schemas.canary import CanaryHitItem, CanaryQueryResult, CanaryReport
from app.schemas.document import RollbackResult
from app.schemas.knowledge import TextChunk

logger = get_logger("app.knowledge.versioning")


def rollback_version(doc_id: str, target_version: str) -> RollbackResult:
    """回滚文档到指定版本，返回结构化结果。

    委托 DocumentStore.rollback_document 完成实际回滚，
    额外计算重建 chunk 数量：对比回滚前后目标版本在向量库中的 chunk 数差值。
    """
    store = get_document_store()
    doc = store.get_document(doc_id)
    if doc is None:
        return RollbackResult(
            doc_id=doc_id,
            target_version=target_version,
            success=False,
            error="文档不存在",
        )
    if DocumentStore._find_version(doc, target_version) is None:
        return RollbackResult(
            doc_id=doc_id,
            target_version=target_version,
            success=False,
            error="目标版本不存在",
        )

    # 记录回滚前目标版本在向量库中的 chunk 数，用于判断是否触发重建
    before_ids = store._query_chunk_ids_by_version(doc_id, target_version)
    success = store.rollback_document(doc_id, target_version)
    if not success:
        return RollbackResult(
            doc_id=doc_id,
            target_version=target_version,
            success=False,
            error="回滚失败",
        )

    after_ids = store._query_chunk_ids_by_version(doc_id, target_version)
    # 重建数 = 回滚后新增的 chunk 数（目标版本 chunks 此前已被删除的情况）
    restored = max(0, len(after_ids) - len(before_ids))
    current = store.get_current_version(doc_id)
    logger.info(
        "回滚完成 doc_id=%s target=%s restored=%d",
        doc_id,
        target_version,
        restored,
    )
    return RollbackResult(
        doc_id=doc_id,
        target_version=target_version,
        success=True,
        restored_chunks=restored,
        current_version=current,
    )


class CanaryManager:
    """灰度集合管理器。

    维护独立的灰度 VectorStore（集合名 = 主集合名 + CANARY_COLLECTION_SUFFIX），
    将新版本 chunks 写入灰度集合后，可在主集合（当前版本）与灰度集合（目标版本）
    间做同一查询的检索 A/B 对比，辅助决策是否切换版本。

    线程安全：灰度 store 的延迟初始化用 RLock 保护，避免并发首查重复创建。
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._canary_store: VectorStore | None = None

    def _canary_collection_name(self) -> str:
        """根据主集合名与配置后缀拼接灰度集合名。"""
        settings = get_settings()
        return f"{settings.CHROMA_COLLECTION_NAME}{settings.CANARY_COLLECTION_SUFFIX}"

    def get_canary_store(self) -> VectorStore:
        """延迟初始化灰度集合 VectorStore，首次调用时创建。

        双重检查锁避免并发场景下重复创建灰度集合。
        """
        if self._canary_store is None:
            with self._lock:
                if self._canary_store is None:
                    settings = get_settings()
                    self._canary_store = VectorStore(
                        persist_dir=settings.CHROMA_PERSIST_DIR,
                        collection_name=self._canary_collection_name(),
                    )
                    logger.info("灰度集合已初始化：%s", self._canary_collection_name())
        return self._canary_store

    def reset(self) -> None:
        """重置灰度集合单例，便于测试切换持久化目录。"""
        with self._lock:
            self._canary_store = None

    def ingest_to_canary(self, doc_id: str, version: str) -> int:
        """将指定版本的 chunks 写入灰度集合。

        从 DocumentStore 读取该版本存储的 chunk 文本快照，重新向量化后写入灰度集合。
        若版本不存在或无文本则返回 0，不抛异常。
        """
        store = get_document_store()
        doc = store.get_document(doc_id)
        if doc is None:
            logger.warning("灰度入库失败，文档不存在：%s", doc_id)
            return 0
        target_ver = DocumentStore._find_version(doc, version)
        if target_ver is None:
            logger.warning("灰度入库失败，版本不存在：%s/%s", doc_id, version)
            return 0
        texts: list[str] = list(target_ver.get("chunk_texts", []))
        if not texts:
            logger.warning("灰度入库跳过，版本无文本快照：%s/%s", doc_id, version)
            return 0

        try:
            embedding_service = get_embedding_service()
            embeddings = embedding_service.embed_texts(texts)
            chunks = [
                TextChunk(text=text, metadata={"doc_id": doc_id, "version": version})
                for text in texts
            ]
            metadatas = [{"doc_id": doc_id, "version": version} for _ in texts]
            canary = self.get_canary_store()
            added = canary.add_chunks(chunks, embeddings, metadatas)
            logger.info("灰度集合写入 doc_id=%s version=%s added=%d", doc_id, version, added)
            return added
        except Exception as exc:
            logger.warning("灰度入库异常 doc_id=%s version=%s：%s", doc_id, version, exc)
            return 0

    def _derive_sample_queries(self, doc_id: str, target_version: str, limit: int) -> list[str]:
        """从目标版本的 chunk 文本派生样本查询，供无显式查询时使用。"""
        store = get_document_store()
        doc = store.get_document(doc_id)
        if doc is None:
            return []
        ver = DocumentStore._find_version(doc, target_version)
        if ver is None:
            return []
        texts = list(ver.get("chunk_texts", []))
        # 取前 limit 条作为占位查询，长度截断避免过长查询拖慢检索
        return [t[:64] for t in texts[:limit] if t]

    @staticmethod
    def _to_hit_items(hits: list[dict], top_k: int) -> list[CanaryHitItem]:
        """将向量库原始命中转换为对外契约 CanaryHitItem，截断文本避免响应过大。"""
        items: list[CanaryHitItem] = []
        for hit in hits[:top_k]:
            metadata = hit.get("metadata") or {}
            items.append(
                CanaryHitItem(
                    text=(hit.get("text") or "")[:80],
                    score=float(hit.get("similarity", 0.0)),
                    source=str(metadata.get("source", "")),
                )
            )
        return items

    def compare(
        self,
        doc_id: str,
        target_version: str,
        current_version: str,
        sample_queries: list[str] | None = None,
        top_k: int | None = None,
    ) -> CanaryReport:
        """主集合与灰度集合的检索 A/B 对比。

        对每条样本查询分别在主集合（当前版本）与灰度集合（目标版本）检索，
        计算 Top-1 相似度差值，正值表示目标版本更相关。
        灰度集合不可用时降级为主集合结果并标记 canary_unavailable。
        """
        settings = get_settings()
        effective_top_k = top_k or settings.CANARY_TOP_K
        queries = list(sample_queries or [])
        if not queries:
            # 无显式查询时从目标版本文本派生，保证对比有数据
            queries = self._derive_sample_queries(
                doc_id, target_version, settings.CANARY_DEFAULT_SAMPLE_SIZE
            )
        if not queries:
            return CanaryReport(
                doc_id=doc_id,
                target_version=target_version,
                current_version=current_version,
                summary="无样本查询可对比",
            )

        embedding_service = get_embedding_service()
        main_store = get_vector_store()

        # 灰度集合延迟获取，失败时降级标记不可用
        canary_store: VectorStore | None = None
        canary_unavailable = False
        try:
            canary_store = self.get_canary_store()
        except Exception as exc:
            logger.warning("灰度集合不可用，降级为主集合结果：%s", exc)
            canary_unavailable = True

        query_results: list[CanaryQueryResult] = []
        diff_sum = 0.0
        for query in queries:
            query_embedding = embedding_service.embed_query(query)
            # 主集合按当前版本过滤，对比同一文档新旧版本
            main_hits = main_store.query(
                query_embedding,
                top_k=effective_top_k,
                where={"$and": [{"doc_id": doc_id}, {"version": current_version}]},
            )
            # 灰度集合按目标版本过滤
            canary_hits: list[dict] = []
            if canary_store is not None:
                try:
                    canary_hits = canary_store.query(
                        query_embedding,
                        top_k=effective_top_k,
                        where={"$and": [{"doc_id": doc_id}, {"version": target_version}]},
                    )
                except Exception as exc:
                    logger.warning("灰度集合查询失败，降级：%s", exc)
                    canary_unavailable = True

            main_score = main_hits[0]["similarity"] if main_hits else 0.0
            canary_score = canary_hits[0]["similarity"] if canary_hits else 0.0
            diff = canary_score - main_score
            diff_sum += diff
            query_results.append(
                CanaryQueryResult(
                    query=query,
                    target_version=target_version,
                    current_version=current_version,
                    target_results=self._to_hit_items(canary_hits, effective_top_k),
                    current_results=self._to_hit_items(main_hits, effective_top_k),
                    similarity_diff=diff,
                )
            )

        avg_diff = diff_sum / len(query_results) if query_results else 0.0
        summary = self._build_compare_summary(avg_diff, canary_unavailable)
        return CanaryReport(
            doc_id=doc_id,
            target_version=target_version,
            current_version=current_version,
            query_results=query_results,
            avg_similarity_diff=avg_diff,
            summary=summary,
            canary_unavailable=canary_unavailable,
        )

    @staticmethod
    def _build_compare_summary(avg_diff: float, canary_unavailable: bool) -> str:
        """生成对比结论：正值表示目标版本更优，灰度不可用时追加降级提示。"""
        parts: list[str] = []
        if avg_diff > 0.01:
            parts.append(f"目标版本平均相似度更高 {avg_diff:.3f}，建议切换")
        elif avg_diff < -0.01:
            parts.append(f"目标版本平均相似度更低 {abs(avg_diff):.3f}，建议保留当前版本")
        else:
            parts.append("两版本检索质量接近")
        if canary_unavailable:
            parts.append("灰度集合不可用，已降级为主集合结果")
        return "；".join(parts)


# 模块级单例，避免重复创建灰度集合
_canary_manager: CanaryManager | None = None


def get_canary_manager() -> CanaryManager:
    """获取 CanaryManager 单例。"""
    global _canary_manager
    if _canary_manager is None:
        _canary_manager = CanaryManager()
    return _canary_manager


def reset_canary_manager() -> None:
    """重置单例，便于测试切换持久化目录。"""
    global _canary_manager
    _canary_manager = None
