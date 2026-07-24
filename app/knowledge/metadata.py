"""元数据标注与质量校验。

为每个 chunk 补充来源/页码/章节/产品分类/版本/知识类型等元数据，
并提供去重、术语一致性、敏感词过滤的质量校验占位接口。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.knowledge import TextChunk

logger = get_logger("app.knowledge.metadata")

# 简单敏感词列表：覆盖政治/辱骂常见样例，命中后做打码处理
DEFAULT_SENSITIVE_WORDS: list[str] = [
    "敏感词1",
    "敏感词2",
    "政治敏感",
    "辱骂",
]

# 知识类型枚举：faq=常见问题 / policy=政策 / doc=普通文档 / tutorial=教程 / ticket=工单
KNOWLEDGE_TYPES = {"faq", "policy", "doc", "tutorial", "ticket"}


class MetadataAnnotator:
    """元数据标注器。

    集中维护元数据字段与默认值，避免散落在各调用点造成不一致。
    """

    def __init__(self, sensitive_words: list[str] | None = None) -> None:
        self.sensitive_words = (
            sensitive_words if sensitive_words is not None else DEFAULT_SENSITIVE_WORDS
        )
        settings = get_settings()
        self.dedup_threshold = settings.DEDUP_THRESHOLD

    def annotate_chunks(
        self,
        chunks: list[TextChunk],
        source: str,
        doc_hash: str,
        overrides: dict[str, Any] | None = None,
    ) -> list[TextChunk]:
        """为一批 chunk 标注完整元数据。

        overrides 允许调用方按文档级覆盖默认值（如指定 product/版本/知识类型），
        字段未提供时使用默认值，保证流水线对未标注文档也能跑通。
        """
        overrides = overrides or {}
        product_category = overrides.get("product_category", "unknown")
        applicable_version = overrides.get("applicable_version", "latest")
        published_at = overrides.get("published_at", datetime.now(timezone.utc).isoformat())
        knowledge_type = self._normalize_knowledge_type(overrides.get("knowledge_type", "doc"))

        for index, chunk in enumerate(chunks):
            # 敏感词过滤：在写入前对文本打码，避免污染向量库
            cleaned_text, hit_words = self._filter_sensitive(chunk.text)
            if hit_words:
                logger.warning("chunk %d 命中敏感词：%s", index, hit_words)
                chunk.text = cleaned_text

            chunk.metadata = {
                "source": source,
                "page_number": chunk.page_number,
                "section": chunk.section,
                "product_category": product_category,
                "applicable_version": applicable_version,
                "published_at": published_at,
                "knowledge_type": knowledge_type,
                "doc_hash": doc_hash,
                "chunk_index": index,
            }
        return chunks

    def _normalize_knowledge_type(self, value: str) -> str:
        """规范化知识类型，未知值回退为 doc。"""
        value = (value or "doc").strip().lower()
        return value if value in KNOWLEDGE_TYPES else "doc"

    def _filter_sensitive(self, text: str) -> tuple:
        """对敏感词做星号打码，返回 (清洗后文本, 命中词列表)。

        仅做简单字符串匹配，性能敏感场景可换 Aho-Corasick 等多模式匹配。
        """
        hit: list[str] = []
        cleaned = text
        for word in self.sensitive_words:
            if word and word in cleaned:
                hit.append(word)
                cleaned = cleaned.replace(word, "*" * len(word))
        return cleaned, hit


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """计算两个向量的余弦相似度。

    余弦相似度对向量长度不敏感，适合度量语义相似性。
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def is_duplicate(
    new_embedding: list[float],
    existing_embeddings: list[list[float]],
    threshold: float = 0.95,
) -> bool:
    """判断新向量是否与已存在向量重复。

    入库前调用，cosine 高于阈值视为同一内容，跳过写入以节省存储。
    本任务先实现逻辑，实际场景下 existing_embeddings 应来自向量库查询。
    """
    if not new_embedding or not existing_embeddings:
        return False
    for existing in existing_embeddings:
        if cosine_similarity(new_embedding, existing) >= threshold:
            return True
    return False


def check_term_consistency(text: str, term_map: dict[str, str] | None = None) -> list[str]:
    """术语一致性检查占位：返回命中的不一致术语。

    term_map 形如 {"旧称": "标准称"}，本任务仅提供接口，便于后续接入术语表。
    """
    term_map = term_map or {}
    hits: list[str] = []
    for alias, standard in term_map.items():
        if alias in text and standard not in text:
            hits.append(f"{alias} -> {standard}")
    return hits


def annotate_chunks(
    chunks: list[TextChunk],
    source: str,
    doc_hash: str,
    overrides: dict[str, Any] | None = None,
) -> list[TextChunk]:
    """便捷入口：使用默认敏感词列表标注一批 chunk。"""
    return MetadataAnnotator().annotate_chunks(chunks, source, doc_hash, overrides)
