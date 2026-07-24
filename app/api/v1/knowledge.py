"""知识库管理端点。

提供文档入库、统计、文档管理（列表/详情/删除）、质量校验、
版本回滚与灰度验证的 HTTP 接口，作为知识库管理后台的 API 入口。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.knowledge.document_store import get_document_store
from app.knowledge.pipeline import get_stats, ingest_document
from app.knowledge.quality import run_quality_check_on_existing
from app.knowledge.vectorstore import get_vector_store
from app.knowledge.versioning import get_canary_manager, rollback_version
from app.schemas.canary import CanaryReport
from app.schemas.document import (
    CanaryRequest,
    DeleteResult,
    DocumentDetail,
    DocumentListResponse,
    DocumentSummary,
    DocumentVersion,
    RollbackRequest,
    RollbackResult,
)
from app.schemas.knowledge import IngestResult, KnowledgeStats, TextChunk
from app.schemas.quality import QualityCheckRequest, QualityReport

logger = get_logger("app.api.v1.knowledge")

router = APIRouter(
    prefix="/api/v1/knowledge",
    tags=["知识库"],
    dependencies=[Depends(verify_api_key)],
)


# ----------------------------------------------------------------------
# 入库与统计（既有端点，扩展可选参数）
# ----------------------------------------------------------------------

# 文件上传安全限制：白名单后缀与最大文件大小
ALLOWED_FILE_TYPES = {".md", ".txt", ".pdf", ".docx"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


@router.post("/ingest", response_model=IngestResult)
def ingest_document_api(
    file: UploadFile = File(..., description="待入库的文档文件"),
    product_category: str | None = Form(None, description="产品分类，默认 unknown"),
    applicable_version: str | None = Form(None, description="适用版本，默认 latest"),
    knowledge_type: str | None = Form(None, description="知识类型：faq/policy/doc/tutorial/ticket"),
    published_at: str | None = Form(None, description="发布时间，ISO8601 字符串"),
    register: bool = Form(False, description="是否注册到文档注册表以启用版本管理"),
    validate_quality: bool = Form(False, description="是否在入库时执行质量校验"),
) -> IngestResult:
    """上传文档并入库。

    通过 multipart 上传文件，落盘到临时目录后调用流水线处理，
    处理完成自动清理临时文件，避免磁盘泄漏。
    register=true 时注册到文档注册表，便于后续版本管理与回滚。
    """
    # 文件类型白名单校验：在解析前拦截不支持的类型，避免无效处理
    file_ext = Path(file.filename or "").suffix.lower()
    if file_ext not in ALLOWED_FILE_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型：{file_ext}，仅支持 {', '.join(ALLOWED_FILE_TYPES)}",
        )

    # 读取文件内容并校验大小：超过上限直接拒绝，避免占用过多内存与存储
    content = file.file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(content)} 字节），最大允许 {MAX_FILE_SIZE} 字节（10MB）",
        )

    # 元数据覆盖项：未提供的字段由流水线使用默认值
    metadata = {}
    if product_category:
        metadata["product_category"] = product_category
    if applicable_version:
        metadata["applicable_version"] = applicable_version
    if knowledge_type:
        metadata["knowledge_type"] = knowledge_type
    if published_at:
        metadata["published_at"] = published_at

    # UploadFile 默认在内存中，落盘到临时目录后由 parsers 读取
    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as buffer:
        buffer.write(content)
        temp_path = buffer.name

    try:
        result = ingest_document(
            temp_path,
            metadata=metadata,
            register_document=register,
            validate_quality=validate_quality,
            source_name=file.filename,
        )
        # 用原始文件名覆盖 source，便于调用方识别
        result.source = file.filename or result.source
        return result
    finally:
        # 无论成功失败都清理临时文件，避免占用磁盘
        Path(temp_path).unlink(missing_ok=True)


@router.get("/stats", response_model=KnowledgeStats)
def get_stats_api() -> KnowledgeStats:
    """查询知识库统计信息。"""
    return get_stats()


# ----------------------------------------------------------------------
# 文档管理（列表/详情/删除）
# ----------------------------------------------------------------------


@router.get("/documents", response_model=DocumentListResponse)
def list_documents_api(
    limit: int = Query(20, ge=1, le=200, description="分页大小"),
    offset: int = Query(0, ge=0, description="起始偏移"),
) -> DocumentListResponse:
    """分页查询已注册文档列表。"""
    store = get_document_store()
    all_docs = store.list_documents()
    total = len(all_docs)
    # 切片实现分页，避免一次性返回大量文档
    page = all_docs[offset : offset + limit]
    items = [
        DocumentSummary(
            doc_id=d["doc_id"],
            source=d.get("source", ""),
            current_version=d.get("current_version", ""),
            status=d.get("status", "active"),
            version_count=d.get("version_count", 0),
            updated_at=d.get("updated_at", ""),
        )
        for d in page
    ]
    return DocumentListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/documents/{doc_id}", response_model=DocumentDetail)
def get_document_api(doc_id: str) -> DocumentDetail:
    """查询单个文档详情，含完整版本历史。"""
    store = get_document_store()
    doc = store.get_document(doc_id)
    if doc is None:
        # 文档不存在时返回 404 语义的空详情，前端据此提示
        return DocumentDetail(doc_id=doc_id, status="not_found")
    versions = [
        DocumentVersion(
            version=v.get("version", ""),
            doc_hash=v.get("doc_hash", ""),
            status=v.get("status", "active"),
            chunk_count=len(v.get("chunk_ids", [])),
            created_at=v.get("created_at", ""),
        )
        for v in doc.get("versions", [])
    ]
    return DocumentDetail(
        doc_id=doc["doc_id"],
        source=doc.get("source", ""),
        current_version=doc.get("current_version", ""),
        status=doc.get("status", "active"),
        created_at=doc.get("created_at", ""),
        updated_at=doc.get("updated_at", ""),
        versions=versions,
    )


@router.delete("/documents/{doc_id}", response_model=DeleteResult)
def delete_document_api(doc_id: str) -> DeleteResult:
    """按 doc_id 删除文档，移除向量库中该文档全部 chunks。"""
    store = get_document_store()
    deleted = store.delete_document(doc_id)
    if deleted == 0:
        # 可能文档不存在或已无 chunks，仍返回成功避免重复删除报错
        doc = store.get_document(doc_id)
        if doc is None:
            return DeleteResult(
                doc_id=doc_id,
                deleted_chunks=0,
                success=False,
                error="文档不存在",
            )
    return DeleteResult(doc_id=doc_id, deleted_chunks=deleted, success=True)


# ----------------------------------------------------------------------
# 质量校验
# ----------------------------------------------------------------------


@router.post("/quality/check", response_model=QualityReport)
def quality_check_api(request: QualityCheckRequest) -> QualityReport:
    """对已入库内容执行批量质量巡检。

    支持按 source 或 doc_id 过滤，均未提供时巡检全量内容。
    去重检测基于库内 chunks 两两比对，发现内部重复片段。
    """
    chunks, embeddings = _fetch_existing_chunks(source=request.source, doc_id=request.doc_id)
    if not chunks:
        return QualityReport(total_chunks=0, summary="无已入库内容可巡检")
    return run_quality_check_on_existing(chunks, embeddings)


def _fetch_existing_chunks(
    source: str | None = None,
    doc_id: str | None = None,
) -> tuple[list[TextChunk], list[list[float]] | None]:
    """从向量库拉取 chunks 及其向量，供质量巡检使用。

    过滤条件通过 ChromaDB where 子句下推，避免全量拉取后再过滤。
    拉取失败时返回空列表，调用方按"无内容"处理。
    """
    try:
        collection = get_vector_store()._collection
        where: dict = {}
        if source:
            where["source"] = source
        if doc_id:
            where["doc_id"] = doc_id
        result = collection.get(
            where=where if where else None,
            include=["documents", "metadatas", "embeddings"],
        )
        ids = result.get("ids") or []
        if not ids:
            return [], None
        documents = result.get("documents") or ["" for _ in ids]
        metadatas = result.get("metadatas") or [{} for _ in ids]
        raw_embeddings = result.get("embeddings")
        chunks: list[TextChunk] = []
        embeddings: list[list[float]] | None = []
        for index in range(len(ids)):
            metadata = metadatas[index] if index < len(metadatas) else {}
            chunks.append(
                TextChunk(
                    text=documents[index] if index < len(documents) else "",
                    metadata=dict(metadata) if metadata else {},
                )
            )
            if raw_embeddings is not None and index < len(raw_embeddings):
                # chroma 可能返回 numpy 数组，统一转 list 便于 cosine 计算
                emb = raw_embeddings[index]
                embeddings.append(list(emb) if hasattr(emb, "__iter__") else [])
        if not embeddings:
            embeddings = None
        return chunks, embeddings
    except Exception as exc:
        logger.warning("拉取已入库 chunks 失败：%s", exc)
        return [], None


# ----------------------------------------------------------------------
# 版本回滚
# ----------------------------------------------------------------------


@router.post("/documents/{doc_id}/rollback", response_model=RollbackResult)
def rollback_document_api(doc_id: str, request: RollbackRequest) -> RollbackResult:
    """回滚文档到指定版本。

    目标版本 chunks 已删时自动用存储的文本快照重新入库。
    """
    return rollback_version(doc_id, request.target_version)


# ----------------------------------------------------------------------
# 灰度验证
# ----------------------------------------------------------------------


@router.post("/canary/ingest")
def canary_ingest_api(request: CanaryRequest) -> dict:
    """将指定版本 chunks 写入灰度集合，供后续对比验证。"""
    manager = get_canary_manager()
    added = manager.ingest_to_canary(request.doc_id, request.version)
    return {
        "doc_id": request.doc_id,
        "version": request.version,
        "added_chunks": added,
        "success": added > 0,
    }


@router.post("/canary/compare", response_model=CanaryReport)
def canary_compare_api(request: CanaryRequest) -> CanaryReport:
    """对比主集合（当前版本）与灰度集合（目标版本）的检索结果。"""
    store = get_document_store()
    current_version = store.get_current_version(request.doc_id)
    if not current_version:
        # 文档不存在或未注册时仍返回结构化报告，前端可据此提示
        return CanaryReport(
            doc_id=request.doc_id,
            target_version=request.version,
            current_version="",
            summary="文档未注册，无法获取当前版本",
            error="文档未注册",
        )
    manager = get_canary_manager()
    return manager.compare(
        doc_id=request.doc_id,
        target_version=request.version,
        current_version=current_version,
        sample_queries=request.sample_queries,
    )
