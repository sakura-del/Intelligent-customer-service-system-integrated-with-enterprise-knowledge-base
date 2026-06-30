"""知识库管理后台 HTTP 端点端到端测试。

覆盖 Task 16 三个子任务通过 HTTP 接口的端到端行为：
- SubTask 16.1：/ingest（register=true）、/documents、/documents/{doc_id}、DELETE
- SubTask 16.2：/quality/check（库内巡检）
- SubTask 16.3：/documents/{doc_id}/rollback、/canary/ingest、/canary/compare

测试隔离：使用独立 chroma 目录 + 模块级 fixture 重置所有相关单例，
避免与其他测试模块共享状态导致用例间相互污染。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 测试用独立持久化目录，每个测试模块独占一份避免相互干扰
TEST_PERSIST_DIR = "./tests/_chroma_data_admin_api"
SAMPLE_FAQ = Path(__file__).parent / "sample_data" / "faq.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离向量库目录并重置知识库管理相关单例。

    覆盖范围：vectorstore / document_store / embeddings / canary_manager，
    保证模块内全部用例共享同一份干净的向量库与注册表状态。
    """
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

    # 清理上次残留，保证入库从零开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置所有相关单例，确保新配置生效
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()
    versioning_module.reset_canary_manager()

    yield

    # 测试结束恢复配置并清理单例，避免影响后续测试模块
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()
    versioning_module.reset_canary_manager()


@pytest.fixture()
def client():
    """提供 FastAPI TestClient。

    每个用例独立创建，避免跨用例共享 client 状态。
    """
    from app.main import app

    return TestClient(app)


def _ingest_via_api(
    client: TestClient,
    register: bool = True,
    validate_quality: bool = False,
    filename: str = "faq.md",
) -> dict:
    """辅助：通过 HTTP /ingest 端点上传样例文档，返回响应 JSON。"""
    with open(SAMPLE_FAQ, "rb") as fp:
        response = client.post(
            "/api/v1/knowledge/ingest",
            files={"file": (filename, fp, "text/markdown")},
            data={
                "knowledge_type": "faq",
                "product_category": "customer_service",
                "register": "true" if register else "false",
                "validate_quality": "true" if validate_quality else "false",
            },
        )
    assert response.status_code == 200, f"入库失败：{response.text}"
    return response.json()


# ----------------------------------------------------------------------
# SubTask 16.1：文档管理 HTTP 端点
# ----------------------------------------------------------------------


def test_ingest_with_register_returns_doc_id_and_version(client: TestClient):
    """/ingest register=true 应返回 doc_id 与 version。"""
    body = _ingest_via_api(client, register=True)
    assert body["error"] is None, f"入库错误：{body.get('error')}"
    assert body["total_chunks"] > 0
    assert body["doc_id"], "register=true 时应返回 doc_id"
    assert body["version"], "register=true 时应返回 version"
    assert body["version"].startswith("v")


def test_ingest_without_register_returns_empty_doc_id(client: TestClient):
    """/ingest register=false 应返回空 doc_id 与 version，保持向后兼容。"""
    body = _ingest_via_api(client, register=False)
    assert body["error"] is None
    assert body["doc_id"] == "", "register=false 时 doc_id 应为空串"
    assert body["version"] == "", "register=false 时 version 应为空串"


def test_ingest_with_validate_quality_returns_quality_report(client: TestClient):
    """/ingest validate_quality=true 应在响应中携带质量校验报告。"""
    body = _ingest_via_api(client, register=True, validate_quality=True)
    assert body["error"] is None
    # quality_report 字段应非空且包含 total_chunks
    assert body["quality_report"] is not None, "validate_quality=true 应返回质量报告"
    assert body["quality_report"]["total_chunks"] > 0
    assert "summary" in body["quality_report"]


def test_stats_returns_collection_info(client: TestClient):
    """GET /stats 应返回集合名与文档总数。"""
    _ingest_via_api(client, register=False)
    response = client.get("/api/v1/knowledge/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["collection_name"] == "knowledge_base"
    assert body["total_documents"] > 0, "入库后应有数据"


def test_list_documents_returns_paginated_response(client: TestClient):
    """GET /documents 应返回分页结构（items/total/limit/offset）。"""
    _ingest_via_api(client, register=True)
    response = client.get("/api/v1/knowledge/documents", params={"limit": 10, "offset": 0})
    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert "total" in body
    assert body["total"] > 0, "注册入库后应至少有 1 个文档"
    assert body["limit"] == 10
    assert body["offset"] == 0
    # 每条 item 应包含必要字段
    first = body["items"][0]
    assert "doc_id" in first
    assert "current_version" in first
    assert "status" in first


def test_list_documents_pagination(client: TestClient):
    """分页参数 limit/offset 应正确生效。"""
    # 入库两次注册产生两个文档或同文档新版本
    _ingest_via_api(client, register=True)
    _ingest_via_api(client, register=True)

    # limit=1 应只返回 1 条
    response = client.get("/api/v1/knowledge/documents", params={"limit": 1, "offset": 0})
    body = response.json()
    assert len(body["items"]) <= 1, "limit=1 应限制返回条数"
    assert body["limit"] == 1


def test_get_document_detail_returns_versions(client: TestClient):
    """GET /documents/{doc_id} 应返回含版本历史的详情。"""
    body = _ingest_via_api(client, register=True)
    doc_id = body["doc_id"]

    response = client.get(f"/api/v1/knowledge/documents/{doc_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["doc_id"] == doc_id
    assert detail["source"], "详情应包含 source 字段"
    assert detail["current_version"], "详情应包含当前版本号"
    assert "versions" in detail
    assert len(detail["versions"]) >= 1
    # 版本结构应含必要字段
    first_ver = detail["versions"][0]
    assert "version" in first_ver
    assert "status" in first_ver


def test_get_document_detail_unknown_returns_not_found_status(client: TestClient):
    """查询不存在的 doc_id 应返回 status=not_found 的详情（不抛 500）。"""
    response = client.get("/api/v1/knowledge/documents/doc-nonexistent")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_found"


def test_delete_document_removes_chunks(client: TestClient):
    """DELETE /documents/{doc_id} 应删除文档并返回 DeleteResult。"""
    body = _ingest_via_api(client, register=True)
    doc_id = body["doc_id"]

    response = client.delete(f"/api/v1/knowledge/documents/{doc_id}")
    assert response.status_code == 200
    result = response.json()
    assert result["doc_id"] == doc_id
    assert result["success"] is True
    assert result["deleted_chunks"] > 0, "应删除至少 1 个 chunk"

    # 删除后再次查询应返回 status=deleted
    detail = client.get(f"/api/v1/knowledge/documents/{doc_id}").json()
    assert detail["status"] == "deleted"


def test_delete_unknown_document_returns_failure(client: TestClient):
    """删除不存在的文档应返回 success=false。"""
    response = client.delete("/api/v1/knowledge/documents/doc-nonexistent")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["deleted_chunks"] == 0


# ----------------------------------------------------------------------
# SubTask 16.2：质量校验 HTTP 端点
# ----------------------------------------------------------------------


def test_quality_check_returns_report(client: TestClient):
    """POST /quality/check 应对已入库内容返回质量巡检报告。"""
    _ingest_via_api(client, register=False)

    response = client.post("/api/v1/knowledge/quality/check", json={})
    assert response.status_code == 200
    body = response.json()
    assert "total_chunks" in body
    assert body["total_chunks"] > 0, "已入库内容应能被巡检"
    assert "summary" in body
    # 三类问题列表字段应存在
    assert "duplicate_issues" in body
    assert "term_issues" in body
    assert "sensitive_issues" in body


def test_quality_check_with_non_matching_filter_returns_zero(client: TestClient):
    """使用不存在的 source 过滤时 /quality/check 应返回 total_chunks=0。"""
    response = client.post(
        "/api/v1/knowledge/quality/check",
        json={"source": "nonexistent_source.md"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_chunks"] == 0


# ----------------------------------------------------------------------
# SubTask 16.3：版本回滚与灰度验证 HTTP 端点
# ----------------------------------------------------------------------


def test_rollback_endpoint_restores_previous_version(client: TestClient):
    """POST /documents/{doc_id}/rollback 应回滚到指定版本。"""
    body1 = _ingest_via_api(client, register=True)
    doc_id = body1["doc_id"]
    v1 = body1["version"]
    # 同文档再次注册入库产生 v2
    body2 = _ingest_via_api(client, register=True)
    assert body2["doc_id"] == doc_id, "同内容应复用 doc_id"
    v2 = body2["version"]
    assert v2 != v1

    # 回滚到 v1
    response = client.post(
        f"/api/v1/knowledge/documents/{doc_id}/rollback",
        json={"target_version": v1},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True
    assert result["target_version"] == v1
    assert result["current_version"] == v1


def test_rollback_unknown_version_returns_failure(client: TestClient):
    """回滚到不存在的版本应返回 success=false 与错误信息。"""
    body = _ingest_via_api(client, register=True)
    doc_id = body["doc_id"]

    response = client.post(
        f"/api/v1/knowledge/documents/{doc_id}/rollback",
        json={"target_version": "v999"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is False
    assert result["error"] is not None


def test_canary_ingest_endpoint_writes_to_canary(client: TestClient):
    """POST /canary/ingest 应将版本 chunks 写入灰度集合。"""
    body = _ingest_via_api(client, register=True)
    doc_id = body["doc_id"]
    version = body["version"]

    response = client.post(
        "/api/v1/knowledge/canary/ingest",
        json={"doc_id": doc_id, "version": version, "sample_queries": []},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["doc_id"] == doc_id
    assert result["version"] == version
    assert result["added_chunks"] > 0, "灰度集合应写入至少 1 个 chunk"
    assert result["success"] is True


def test_canary_ingest_unknown_doc_returns_zero(client: TestClient):
    """灰度入库不存在的文档应返回 added_chunks=0。"""
    response = client.post(
        "/api/v1/knowledge/canary/ingest",
        json={"doc_id": "doc-nonexistent", "version": "v1", "sample_queries": []},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["added_chunks"] == 0
    assert body["success"] is False


def test_canary_compare_endpoint_returns_report(client: TestClient):
    """POST /canary/compare 应返回灰度对比结构化报告。"""
    body1 = _ingest_via_api(client, register=True)
    doc_id = body1["doc_id"]
    # 产生新版本（同内容再次入库，版本递增）
    body2 = _ingest_via_api(client, register=True)
    v2 = body2["version"]

    # 先将 v2 写入灰度集合
    client.post(
        "/api/v1/knowledge/canary/ingest",
        json={"doc_id": doc_id, "version": v2, "sample_queries": []},
    )

    # 对比：target=v2（灰度集合），current=文档当前版本（两次入库后为 v2）
    response = client.post(
        "/api/v1/knowledge/canary/compare",
        json={"doc_id": doc_id, "version": v2, "sample_queries": ["退款", "客服"]},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["doc_id"] == doc_id
    assert report["target_version"] == v2
    # current_version 是文档当前活跃版本（两次入库后为 v2）
    assert report["current_version"] == v2
    assert len(report["query_results"]) == 2, "应返回 2 条样本查询的对比结果"
    assert "summary" in report
    assert report["summary"], "summary 不应为空"


def test_canary_compare_unknown_doc_returns_error_report(client: TestClient):
    """对比不存在的文档应返回含 error 字段的报告，不抛 500。"""
    response = client.post(
        "/api/v1/knowledge/canary/compare",
        json={
            "doc_id": "doc-nonexistent",
            "version": "v1",
            "sample_queries": ["测试"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    # 文档未注册时应返回结构化错误报告而非异常
    assert body["doc_id"] == "doc-nonexistent"
    assert body.get("error") is not None or body.get("summary")
