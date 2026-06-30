"""版本管理与灰度验证测试。

覆盖 Task 16 SubTask 16.3：
- rollback_version：版本回滚（含 chunks 重建场景）
- CanaryManager.ingest_to_canary：灰度集合写入
- CanaryManager.compare：主集合与灰度集合 A/B 对比

测试隔离：独立 chroma 目录 + 重置 vector_store / document_store / canary_manager 单例。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TEST_PERSIST_DIR = "./tests/_chroma_data_versioning"
SAMPLE_FAQ = Path(__file__).parent / "sample_data" / "faq.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离向量库目录并重置版本管理相关单例。"""
    from app.core.config import get_settings
    from app.knowledge import (
        document_store as document_store_module,
    )
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )
    from app.knowledge import (
        versioning as versioning_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()
    versioning_module.reset_canary_manager()

    yield

    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()
    versioning_module.reset_canary_manager()


def _ingest_registered():
    """辅助：注册入库样例文档，返回 (doc_id, version)。"""
    from app.knowledge.pipeline import ingest_document

    result = ingest_document(
        SAMPLE_FAQ,
        metadata={"knowledge_type": "faq"},
        register_document=True,
    )
    assert result.error is None, f"入库失败：{result.error}"
    return result.doc_id, result.version


# ----------------------------------------------------------------------
# 版本回滚
# ----------------------------------------------------------------------


def test_rollback_to_previous_version():
    """回滚到前一版本应成功并切换 current_version。"""
    doc_id, v1 = _ingest_registered()
    _, v2 = _ingest_registered()  # 同内容再入库，产生 v2
    assert v2 != v1

    from app.knowledge.versioning import rollback_version

    result = rollback_version(doc_id, v1)
    assert result.success is True
    assert result.target_version == v1
    assert result.current_version == v1


def test_rollback_unknown_doc_returns_error():
    """回滚不存在的文档应返回失败结果。"""
    from app.knowledge.versioning import rollback_version

    result = rollback_version("doc-nonexistent", "v1")
    assert result.success is False
    assert result.error is not None


def test_rollback_unknown_version_returns_error():
    """回滚到不存在的版本应返回失败结果。"""
    doc_id, _ = _ingest_registered()
    from app.knowledge.versioning import rollback_version

    result = rollback_version(doc_id, "v999")
    assert result.success is False
    assert "不存在" in result.error


def test_rollback_reingests_deleted_chunks():
    """删除文档后回滚应重新入库 chunks（restored_chunks > 0）。"""
    doc_id, v1 = _ingest_registered()

    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    # 先删除文档，移除全部 chunks
    deleted = store.delete_document(doc_id)
    assert deleted > 0
    # 回滚到 v1，chunks 已被删除，应触发重新入库
    from app.knowledge.versioning import rollback_version

    result = rollback_version(doc_id, v1)
    assert result.success is True
    assert result.restored_chunks > 0, "删除后回滚应重新入库 chunks"
    assert result.current_version == v1


# ----------------------------------------------------------------------
# 灰度集合写入
# ----------------------------------------------------------------------


def test_canary_ingest_writes_to_canary_collection():
    """灰度入库应将版本 chunks 写入灰度集合。"""
    doc_id, v1 = _ingest_registered()

    from app.knowledge.versioning import get_canary_manager

    manager = get_canary_manager()
    added = manager.ingest_to_canary(doc_id, v1)
    assert added > 0, "灰度集合应写入至少 1 个 chunk"

    # 灰度集合计数应大于 0
    canary_store = manager.get_canary_store()
    assert canary_store.count() > 0


def test_canary_ingest_unknown_doc_returns_zero():
    """灰度入库不存在的文档应返回 0。"""
    from app.knowledge.versioning import get_canary_manager

    manager = get_canary_manager()
    added = manager.ingest_to_canary("doc-nonexistent", "v1")
    assert added == 0


def test_canary_ingest_unknown_version_returns_zero():
    """灰度入库不存在的版本应返回 0。"""
    doc_id, _ = _ingest_registered()
    from app.knowledge.versioning import get_canary_manager

    manager = get_canary_manager()
    added = manager.ingest_to_canary(doc_id, "v999")
    assert added == 0


# ----------------------------------------------------------------------
# 灰度对比
# ----------------------------------------------------------------------


def test_canary_compare_returns_report():
    """灰度对比应返回包含查询结果的结构化报告。"""
    doc_id, v1 = _ingest_registered()
    # 同内容再入库产生 v2（chunk_texts 存储但 chunks 去重不入主库）
    _, v2 = _ingest_registered()

    from app.knowledge.versioning import get_canary_manager

    manager = get_canary_manager()
    # 将 v2 chunks 写入灰度集合
    manager.ingest_to_canary(doc_id, v2)
    # 对比：主集合 v1 vs 灰度集合 v2
    report = manager.compare(
        doc_id=doc_id,
        target_version=v2,
        current_version=v1,
    )
    assert report.doc_id == doc_id
    assert report.target_version == v2
    assert report.current_version == v1
    assert len(report.query_results) > 0
    # 同内容对比，相似度差值应接近 0
    assert abs(report.avg_similarity_diff) < 0.5


def test_canary_compare_with_explicit_queries():
    """灰度对比支持显式样本查询。"""
    doc_id, v1 = _ingest_registered()

    from app.knowledge.versioning import get_canary_manager

    manager = get_canary_manager()
    manager.ingest_to_canary(doc_id, v1)
    report = manager.compare(
        doc_id=doc_id,
        target_version=v1,
        current_version=v1,
        sample_queries=["退款", "客服"],
    )
    assert len(report.query_results) == 2
    # 每条查询结果应包含 target 与 current 结果列表
    for qr in report.query_results:
        assert qr.query in ("退款", "客服")


def test_canary_compare_no_sample_queries_derives_from_chunks():
    """无显式查询时从版本 chunks 派生样本查询。"""
    doc_id, v1 = _ingest_registered()

    from app.knowledge.versioning import get_canary_manager

    manager = get_canary_manager()
    manager.ingest_to_canary(doc_id, v1)
    report = manager.compare(
        doc_id=doc_id,
        target_version=v1,
        current_version=v1,
    )
    # 应自动派生查询，query_results 非空
    assert len(report.query_results) > 0


def test_canary_compare_summary_not_empty():
    """对比报告应包含非空的整体结论。"""
    doc_id, v1 = _ingest_registered()
    from app.knowledge.versioning import get_canary_manager

    manager = get_canary_manager()
    manager.ingest_to_canary(doc_id, v1)
    report = manager.compare(
        doc_id=doc_id,
        target_version=v1,
        current_version=v1,
    )
    assert report.summary, "summary 不应为空"
