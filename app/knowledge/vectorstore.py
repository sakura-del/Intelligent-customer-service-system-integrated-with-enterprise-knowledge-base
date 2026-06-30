"""ChromaDB 向量库封装。

提供集合初始化、批量写入、相似度检索与去重判断，
作为知识库持久层供 pipeline 与 RAG 检索调用。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.knowledge import TextChunk

logger = get_logger("app.knowledge.vectorstore")


class VectorStore:
    """ChromaDB 向量库封装。

    使用持久化 Client，进程重启后仍可读取已有数据；
    通过 collection_name 隔离不同知识库。
    """

    def __init__(
        self,
        persist_dir: str = "",
        collection_name: str = "",
        dedup_threshold: float = 0.0,
    ) -> None:
        settings = get_settings()
        self.persist_dir = persist_dir or settings.CHROMA_PERSIST_DIR
        self.collection_name = collection_name or settings.CHROMA_COLLECTION_NAME
        self.dedup_threshold = dedup_threshold or settings.DEDUP_THRESHOLD

        # 确保持久化目录存在，避免 ChromaDB 初始化报错
        Path(self.persist_dir).mkdir(parents=True, exist_ok=True)

        # 延迟导入：测试或未安装 chromadb 时仍可加载本模块
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self._client = chromadb.PersistentClient(
            path=self.persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        # get_or_create 避免重复创建抛错
        # 显式指定 cosine 距离：ChromaDB 默认 l2（L2 squared），
        # 而 BGE 向量做 L2 归一化后用 cosine 检索更准确
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={
                "description": "企业知识库向量集合",
                "hnsw:space": "cosine",
            },
        )
        logger.info(
            "VectorStore 初始化完成：persist_dir=%s collection=%s 已有条目=%d",
            self.persist_dir,
            self.collection_name,
            self._collection.count(),
        )

    def count(self) -> int:
        """返回集合中当前条目数。"""
        return self._collection.count()

    def get_all_chunks(self, batch_size: int = 500) -> List[Dict[str, Any]]:
        """拉取集合中全部 chunks，用于构建 BM25 等需要全量索引的场景。

        分批拉取避免一次性加载大集合导致内存峰值；
        返回字段与 query 一致：text/metadata/id。
        """
        total = self._collection.count()
        if total == 0:
            return []

        all_hits: List[Dict[str, Any]] = []
        offset = 0
        # 分页拉取，每批 batch_size 条，控制单次内存占用
        while offset < total:
            batch = self._collection.get(
                include=["metadatas", "documents"],
                limit=batch_size,
                offset=offset,
            )
            if not batch or not batch.get("ids"):
                break
            ids = batch["ids"]
            documents = batch.get("documents") or ["" for _ in ids]
            metadatas = batch.get("metadatas") or [{} for _ in ids]
            for idx in range(len(ids)):
                all_hits.append(
                    {
                        "id": ids[idx],
                        "text": documents[idx],
                        "metadata": metadatas[idx],
                    }
                )
            # 已拉取到末尾或本批不足 batch_size，提前退出避免空循环
            if len(ids) < batch_size:
                break
            offset += len(ids)
        return all_hits

    def add_chunks(
        self,
        chunks: List[TextChunk],
        embeddings: List[List[float]],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        skip_dedup: bool = False,
    ) -> int:
        """批量写入 chunk 与向量。

        入库前用 cosine 阈值过滤重复向量，返回实际写入条数。
        ChromaDB 要求 id 唯一，使用 uuid4 避免冲突。
        skip_dedup=True 时跳过去重检查，供版本回滚等确知无重复的场景使用。
        """
        if not chunks:
            return 0

        if len(chunks) != len(embeddings):
            raise ValueError("chunks 与 embeddings 数量不一致")

        # 用 chroma query 批量查询每个新向量的最近邻做去重判断，
        # 避免一次性 get 全部已有向量导致内存压力与 numpy 真值歧义
        # skip_dedup 时直接全 False，避免回滚重入库被误判为重复
        if skip_dedup:
            duplicate_flags = [False] * len(embeddings)
        else:
            duplicate_flags = self._batch_check_duplicate(embeddings)

        to_add_ids: List[str] = []
        to_add_texts: List[str] = []
        to_add_embeddings: List[List[float]] = []
        to_add_metadatas: List[Dict[str, Any]] = []
        deduped = 0
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            if duplicate_flags[index]:
                deduped += 1
                continue
            metadata = metadatas[index] if metadatas and index < len(metadatas) else dict(chunk.metadata)
            # ChromaDB 元数据值必须是基础类型，序列化复杂字段
            metadata = self._normalize_metadata(metadata)
            to_add_ids.append(str(uuid.uuid4()))
            to_add_texts.append(chunk.text)
            to_add_embeddings.append(embedding)
            to_add_metadatas.append(metadata)

        if to_add_ids:
            self._collection.add(
                ids=to_add_ids,
                documents=to_add_texts,
                embeddings=to_add_embeddings,
                metadatas=to_add_metadatas,
            )
            logger.info("写入 %d 条（去重 %d 条）", len(to_add_ids), deduped)
        else:
            logger.info("全部 %d 条 chunk 均判为重复，跳过写入", deduped)
        return len(to_add_ids)

    def _batch_check_duplicate(self, embeddings: List[List[float]]) -> List[bool]:
        """批量查询每个向量的最近邻判断是否重复。

        返回与 embeddings 等长的布尔列表，True 表示已存在相似向量。
        空集合时直接返回全 False，避免无意义查询。
        """
        if self._collection.count() == 0:
            return [False] * len(embeddings)

        try:
            results = self._collection.query(
                query_embeddings=embeddings,
                n_results=1,
            )
        except Exception as exc:
            # 查询失败时降级为不去重，保证写入不中断
            logger.warning("去重查询失败，本次跳过去重：%s", exc)
            return [False] * len(embeddings)

        distances_list = results.get("distances") or []
        flags: List[bool] = []
        for distances in distances_list:
            if not distances:
                flags.append(False)
                continue
            # cosine 距离：distance = 1 - similarity（hnsw:space=cosine）
            distance = distances[0]
            similarity = max(0.0, 1.0 - distance)
            flags.append(similarity >= self.dedup_threshold)
        return flags

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        score_threshold: float = 0.0,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """向量相似度检索。

        ChromaDB 默认按距离排序，返回前 top_k 中满足 score_threshold 的结果。
        where 用于元数据过滤，例如 {"knowledge_type": "faq"}。
        """
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
        )

        hits: List[Dict[str, Any]] = []
        if not results or not results.get("ids"):
            return hits

        ids = results["ids"][0]
        documents = results["documents"][0] if results.get("documents") else ["" for _ in ids]
        metadatas = results["metadatas"][0] if results.get("metadatas") else [{} for _ in ids]
        distances = results["distances"][0] if results.get("distances") else [0.0 for _ in ids]

        for idx in range(len(ids)):
            # ChromaDB 返回的是距离（越小越相似），转成相似度便于阈值过滤
            distance = distances[idx]
            similarity = max(0.0, 1.0 - distance)
            if similarity < score_threshold:
                continue
            hits.append(
                {
                    "id": ids[idx],
                    "text": documents[idx],
                    "metadata": metadatas[idx],
                    "similarity": similarity,
                }
            )
        return hits

    @staticmethod
    def _normalize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        """规范化元数据值类型，保证 ChromaDB 可存储。

        ChromaDB 仅支持 str/int/float/bool/None，
        复杂类型统一转字符串避免写入失败。
        """
        normalized: Dict[str, Any] = {}
        for key, value in metadata.items():
            if value is None or isinstance(value, (str, int, float, bool)):
                normalized[key] = value
            else:
                normalized[key] = str(value)
        return normalized


# 模块级单例，避免重复打开 PersistentClient
_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """获取 VectorStore 单例。"""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store


def reset_vector_store() -> None:
    """重置单例，便于测试切换持久化目录。"""
    global _vector_store
    _vector_store = None
