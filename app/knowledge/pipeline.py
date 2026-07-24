"""知识库构建流水线编排。

串联解析 → 切分 → 元数据标注 → 向量化 → 去重校验 → 写入 ChromaDB，
对外暴露 ingest_document 与 get_stats 两个高层 API。

Task 16 扩展：可选注册文档版本到 DocumentStore，可选触发质量校验。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.knowledge.chunker import SemanticChunker
from app.knowledge.embeddings import get_embedding_service
from app.knowledge.metadata import MetadataAnnotator
from app.knowledge.parsers import parse_file
from app.knowledge.quality import run_quality_check
from app.knowledge.vectorstore import get_vector_store
from app.schemas.knowledge import IngestResult, KnowledgeStats
from app.schemas.quality import QualityReport

logger = get_logger("app.knowledge.pipeline")


def ingest_document(
    file_path: str | Path,
    metadata: dict[str, Any] | None = None,
    register_document: bool = False,
    validate_quality: bool = False,
    source_name: str | None = None,
) -> IngestResult:
    """端到端入库单文档。

    metadata 支持覆盖 product_category / applicable_version /
    published_at / knowledge_type 等字段，未提供时使用默认值。
    任一阶段异常都被捕获并写入 IngestResult.error，避免拖垮 API。

    register_document=True 时将文档注册到 DocumentStore 并回填 doc_id/version，
    便于后续版本管理与回滚；默认关闭以保持与既有调用方兼容。
    validate_quality=True 时在切分后对原始 chunks 执行质量校验，
    报告挂载到 IngestResult.quality_report，不影响入库主流程。
    """
    start_ts = time.time()
    path = Path(file_path)
    metadata = metadata or {}

    try:
        # 1. 解析：把不同格式文档统一成 ParsedDocument
        parsed = parse_file(path)

        # 2. 切分：按章节/段落/字符三级降级策略
        chunks = SemanticChunker().chunk_document(parsed)
        total_chunks = len(chunks)
        if total_chunks == 0:
            logger.warning("文档 %s 切分后无有效 chunk", path.name)
            return IngestResult(
                source=path.name,
                total_chunks=0,
                added_chunks=0,
                deduped_chunks=0,
                duration_seconds=time.time() - start_ts,
                doc_hash=parsed.doc_hash,
                embedding_mode="unknown",
                error="切分后无有效内容",
            )

        # 质量校验在标注前执行：敏感词/术语检查需基于原始文本，避免被标注阶段打码
        quality_report: QualityReport | None = None
        if validate_quality:
            quality_report = run_quality_check(chunks, existing_embeddings=None)

        # 3. 文档注册：预分配 doc_id/version，便于回填到 chunk metadata
        # source_name 优先：API 上传时传入原始文件名，避免 chunk source 为临时文件名
        effective_source = source_name or parsed.source
        doc_id, version = "", ""
        if register_document:
            doc_id, version = _prepare_document_version(parsed.doc_hash, effective_source)

        # 4. 元数据标注：补充来源/页码/章节/产品分类等
        annotator = MetadataAnnotator()
        annotated = annotator.annotate_chunks(
            chunks=chunks,
            source=effective_source,
            doc_hash=parsed.doc_hash,
            overrides=metadata,
        )
        # 注册模式下回填 doc_id/version，供后续按版本过滤检索与回滚定位
        if register_document and doc_id:
            for chunk in annotated:
                chunk.metadata["doc_id"] = doc_id
                chunk.metadata["version"] = version

        # 5. 向量化：批量 embedding，内部已分批避免 OOM
        embedding_service = get_embedding_service()
        texts = [chunk.text for chunk in annotated]
        embeddings = embedding_service.embed_texts(texts)

        # 6/7. 写入向量库：内部包含 cosine 去重判断
        store = get_vector_store()
        added = store.add_chunks(annotated, embeddings, [chunk.metadata for chunk in annotated])
        deduped = total_chunks - added

        # 注册模式下补全版本的 chunk_ids 与文本快照，供回滚恢复使用
        if register_document and doc_id:
            _finalize_document_version(doc_id, version, texts)

        duration = time.time() - start_ts
        logger.info(
            "文档 %s 入库完成：total=%d added=%d deduped=%d 耗时=%.2fs 模式=%s",
            path.name,
            total_chunks,
            added,
            deduped,
            duration,
            embedding_service.mode,
        )
        return IngestResult(
            source=path.name,
            total_chunks=total_chunks,
            added_chunks=added,
            deduped_chunks=deduped,
            duration_seconds=duration,
            doc_hash=parsed.doc_hash,
            embedding_mode=embedding_service.mode,
            doc_id=doc_id,
            version=version,
            quality_report=quality_report,
        )
    except Exception as exc:
        # 任一阶段失败都向上返回错误结果，避免 API 抛 500
        logger.exception("文档 %s 入库失败：%s", path.name, exc)
        return IngestResult(
            source=path.name,
            total_chunks=0,
            added_chunks=0,
            deduped_chunks=0,
            duration_seconds=time.time() - start_ts,
            error=str(exc),
        )


def _prepare_document_version(doc_hash: str, source: str) -> tuple[str, str]:
    """预分配文档 ID 与版本号，失败时返回空串不阻断入库。

    DocumentStore 操作异常时仅记录告警，保证入库主流程不受注册表故障影响。
    """
    try:
        from app.knowledge.document_store import get_document_store

        return get_document_store().prepare_version(doc_hash, source)
    except Exception as exc:
        logger.warning("文档注册失败，跳过版本管理：%s", exc)
        return "", ""


def _finalize_document_version(doc_id: str, version: str, chunk_texts: list[str]) -> None:
    """入库后补全版本元数据，失败时仅告警不阻断主流程。"""
    try:
        from app.knowledge.document_store import get_document_store

        get_document_store().finalize_version(doc_id, version, chunk_texts)
    except Exception as exc:
        logger.warning("文档版本 finalize 失败：%s", exc)


def get_stats() -> KnowledgeStats:
    """查询知识库统计信息。"""
    store = get_vector_store()
    return KnowledgeStats(
        collection_name=store.collection_name,
        total_documents=store.count(),
        persist_dir=store.persist_dir,
    )
