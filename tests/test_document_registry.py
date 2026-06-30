"""文档注册表（DocumentStore）测试。

覆盖 Task 16 SubTask 16.1 的文档管理能力：
- 注册表 CRUD：prepare_version / finalize_version / list / get / delete
- 版本递增：同 doc_hash 再次上传自动 +1 版本号
- 版本回滚：回滚到旧版本并验证状态切换
- 并发安全：多线程并发注册不损坏 JSON 持久化

测试隔离：使用独立 chroma 目录 + 模块级 fixture 重置相关单例。
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

# 测试用独立持久化目录
TEST_PERSIST_DIR = "./tests/_chroma_data_doc_registry"
SAMPLE_FAQ = Path(__file__).parent / "sample_data" / "faq.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离向量库目录并重置 document_store 等单例。"""
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

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留，保证注册表与向量库从零开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置单例，确保新配置生效
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()


def _ingest_with_registration():
    """辅助：入库样例文档并注册到文档注册表，返回 (doc_id, version)。"""
    from app.knowledge.pipeline import ingest_document

    result = ingest_document(
        SAMPLE_FAQ,
        metadata={"knowledge_type": "faq"},
        register_document=True,
    )
    assert result.error is None, f"入库失败：{result.error}"
    assert result.doc_id, "应返回 doc_id"
    assert result.version, "应返回 version"
    return result.doc_id, result.version


# ----------------------------------------------------------------------
# 注册表 CRUD
# ----------------------------------------------------------------------


def test_prepare_version_creates_new_document():
    """首次注册应创建新文档，版本号为 v1。"""
    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    doc_id, version = store.prepare_version("hash_test_create", "test_create.md")
    assert version == "v1"
    doc = store.get_document(doc_id)
    assert doc is not None
    assert doc["source"] == "test_create.md"
    assert doc["status"] == "active"
    assert doc["current_version"] == "v1"


def test_prepare_version_increments_for_same_hash():
    """同 doc_hash 再次注册应追加新版本，版本号递增。"""
    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    doc_hash = "hash_increment_test"
    doc_id, v1 = store.prepare_version(doc_hash, "doc.md")
    assert v1 == "v1"
    # 同 hash 再次注册，应追加 v2
    doc_id2, v2 = store.prepare_version(doc_hash, "doc.md")
    assert doc_id2 == doc_id, "同 hash 应复用同一 doc_id"
    assert v2 == "v2"
    # 旧版本应被归档
    versions = store.list_versions(doc_id)
    assert len(versions) == 2
    v1_status = next(v["status"] for v in versions if v["version"] == "v1")
    assert v1_status == "archived"
    v2_status = next(v["status"] for v in versions if v["version"] == "v2")
    assert v2_status == "active"


def test_list_documents_returns_summaries():
    """list_documents 应返回所有已注册文档的摘要。"""
    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    store.prepare_version("hash_list_test", "list_test.md")
    docs = store.list_documents()
    assert len(docs) > 0
    # 摘应包含必要字段
    last = docs[-1]
    assert "doc_id" in last
    assert "current_version" in last
    assert "status" in last


def test_get_document_returns_detail_with_versions():
    """get_document 应返回含完整版本历史的详情。"""
    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    doc_id, _ = store.prepare_version("hash_detail_test", "detail.md")
    doc = store.get_document(doc_id)
    assert doc is not None
    assert doc["doc_id"] == doc_id
    assert "versions" in doc
    assert len(doc["versions"]) == 1


def test_get_document_returns_none_for_unknown():
    """查询不存在的 doc_id 应返回 None。"""
    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    assert store.get_document("doc-nonexistent") is None


# ----------------------------------------------------------------------
# 入库注册集成
# ----------------------------------------------------------------------


def test_ingest_with_register_populates_chunk_ids():
    """带注册的入库应将 chunk_ids 与文本快照写入注册表。"""
    from app.knowledge.document_store import get_document_store

    doc_id, version = _ingest_with_registration()
    store = get_document_store()
    doc = store.get_document(doc_id)
    assert doc is not None
    ver = next(v for v in doc["versions"] if v["version"] == version)
    # finalize 后 chunk_ids 应非空（实际写入向量库的 chunks）
    assert len(ver["chunk_ids"]) > 0
    assert len(ver["chunk_texts"]) > 0


def test_reingest_same_doc_creates_new_version():
    """同文档再次注册入库应产生新版本，旧版本归档。"""
    doc_id1, v1 = _ingest_with_registration()
    doc_id2, v2 = _ingest_with_registration()
    # 同源文件同内容，doc_hash 相同，应复用 doc_id
    assert doc_id1 == doc_id2
    assert v2 != v1, "应产生新版本号"


# ----------------------------------------------------------------------
# 删除与回滚
# ----------------------------------------------------------------------


def test_delete_document_removes_chunks_and_marks_deleted():
    """删除文档应移除向量库 chunks 并标记状态为 deleted。"""
    from app.knowledge.document_store import get_document_store
    from app.knowledge.vectorstore import get_vector_store

    doc_id, _ = _ingest_with_registration()
    store = get_document_store()
    before_count = get_vector_store().count()

    deleted = store.delete_document(doc_id)
    assert deleted > 0, "应删除至少 1 个 chunk"

    # 向量库条目应减少
    after_count = get_vector_store().count()
    assert after_count < before_count

    # 文档状态应标记为 deleted
    doc = store.get_document(doc_id)
    assert doc["status"] == "deleted"


def test_rollback_restores_previous_version():
    """回滚到旧版本应将旧版本设为 active 并切换 current_version。"""
    from app.knowledge.document_store import get_document_store

    doc_id, v2 = _ingest_with_registration()
    # v2 是当前版本，回滚到 v1
    store = get_document_store()
    versions = store.list_versions(doc_id)
    v1 = next(v["version"] for v in versions if v["version"] == "v1")

    success = store.rollback_document(doc_id, v1)
    assert success is True

    doc = store.get_document(doc_id)
    assert doc["current_version"] == v1
    v1_ver = next(v for v in doc["versions"] if v["version"] == v1)
    assert v1_ver["status"] == "active"


def test_rollback_unknown_version_returns_false():
    """回滚到不存在的版本应返回 False。"""
    from app.knowledge.document_store import get_document_store

    doc_id, _ = _ingest_with_registration()
    store = get_document_store()
    assert store.rollback_document(doc_id, "v999") is False


def test_rollback_unknown_doc_returns_false():
    """回滚不存在的文档应返回 False。"""
    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    assert store.rollback_document("doc-nonexistent", "v1") is False


# ----------------------------------------------------------------------
# 并发安全
# ----------------------------------------------------------------------


def test_concurrent_prepare_version_is_thread_safe():
    """多线程并发注册不同文档应全部成功且无数据损坏。"""
    from app.knowledge.document_store import get_document_store

    store = get_document_store()
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    def register(index: int) -> None:
        doc_hash = f"hash_concurrent_{index}"
        doc_id, version = store.prepare_version(doc_hash, f"concurrent_{index}.md")
        with lock:
            results.append((doc_id, version))

    threads = [threading.Thread(target=register, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 全部 8 个线程应成功注册
    assert len(results) == 8
    # doc_id 应全部唯一
    doc_ids = {doc_id for doc_id, _ in results}
    assert len(doc_ids) == 8
    # 持久化文件应可正常加载（无损坏）
    store2 = type(store)()
    for doc_id, _ in results:
        assert store2.get_document(doc_id) is not None
