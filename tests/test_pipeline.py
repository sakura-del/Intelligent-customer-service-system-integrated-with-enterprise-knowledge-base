"""知识库流水线端到端测试。

验证 ingest_document 能完成解析→切分→向量化→写入 ChromaDB 全流程，
并校验 stats 接口能正确返回入库数量。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# 测试用独立持久化目录，避免污染正式环境
TEST_PERSIST_DIR = "./tests/_chroma_data_test"
SAMPLE_FAQ = Path(__file__).parent / "sample_data" / "faq.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_dir(monkeypatch_module):
    """模块级 fixture：把 ChromaDB 持久化目录重定向到测试专用路径。

    使用 module 作用域避免多次重置单例，所有用例共享同一向量库实例。
    setup 阶段清理旧目录，保证重复运行测试时入库从零开始，
    避免「重复入库全部判重」导致 added_chunks=0 的断言失败。
    """
    from app.core.config import get_settings
    from app.knowledge import embeddings as embeddings_module
    from app.knowledge import vectorstore as vectorstore_module

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次测试残留，保证入库从零开始；失败时忽略（目录可能不存在）
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置单例，确保新配置生效
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()

    yield

    # 测试结束恢复原配置并清理单例，避免影响后续用例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()


@pytest.fixture(scope="module")
def monkeypatch_module():
    """模块级 monkeypatch 替代品。

    pytest 的 monkeypatch 是 function 级，无法跨用例共享，
    这里仅作为占位以便 fixture 语义清晰，实际配置修改通过 settings 完成。
    """
    return None


def test_ingest_document_returns_chunks():
    """调用 ingest_document 入库 FAQ 文档，应返回 chunk 数 > 0。"""
    from app.knowledge.pipeline import ingest_document

    result = ingest_document(
        SAMPLE_FAQ,
        metadata={
            "product_category": "customer_service",
            "knowledge_type": "faq",
            "applicable_version": "v1.0",
        },
    )

    assert result.error is None, f"入库失败：{result.error}"
    assert result.total_chunks > 0, "切分后 chunk 数应大于 0"
    assert result.added_chunks > 0, "实际写入数应大于 0"
    assert result.embedding_mode in {"bge", "fallback"}
    assert result.doc_hash, "应返回文档哈希"
    assert result.duration_seconds > 0


def test_stats_returns_count():
    """入库后 stats 接口应返回非零条目数。"""
    from app.knowledge.pipeline import get_stats

    stats = get_stats()
    assert stats.collection_name == "knowledge_base"
    assert stats.total_documents > 0, "向量库应有数据"
    assert stats.persist_dir == TEST_PERSIST_DIR


def test_dedup_skips_repeated_ingest():
    """重复入库同一文档应触发去重，写入数为 0。"""
    from app.knowledge.pipeline import ingest_document

    result = ingest_document(SAMPLE_FAQ, metadata={"knowledge_type": "faq"})
    assert result.error is None
    # 重复入库：fallback 向量完全相同，应全部判重
    assert result.added_chunks == 0
    assert result.deduped_chunks == result.total_chunks


def test_ingest_api_via_test_client():
    """通过 TestClient 调用 /api/v1/knowledge/ingest 上传文件。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with open(SAMPLE_FAQ, "rb") as fp:
        response = client.post(
            "/api/v1/knowledge/ingest",
            files={"file": ("faq.md", fp, "text/markdown")},
            data={"knowledge_type": "faq", "product_category": "customer_service"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_chunks"] > 0
    assert body["source"] == "faq.md"


def test_stats_api_via_test_client():
    """通过 TestClient 调用 /api/v1/knowledge/stats 查询统计。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.get("/api/v1/knowledge/stats")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["collection_name"] == "knowledge_base"
    assert body["total_documents"] > 0
