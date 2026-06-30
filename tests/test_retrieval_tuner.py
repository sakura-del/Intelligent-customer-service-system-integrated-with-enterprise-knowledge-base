"""检索参数调优模块测试。

覆盖 TunerParams 模型校验、RetrievalTuner 单例的加载/更新/持久化/重置，
以及并发场景下的线程安全性。测试隔离使用独立 chroma 目录。

测试用例：
1. 默认参数加载
2. 字段范围校验（vector_top_k/bm25_top_k/rerank_top_k 等）
3. 参数更新生效
4. 部分更新（merge）保留未传字段
5. 持久化到 JSON 文件
6. 重启后恢复参数
7. reset_to_defaults 清空并恢复
8. 下游单例重置（hybrid_retriever/bm25/reranker）
9. 并发更新线程安全
10. API 端点 GET/PUT/POST
11. API 范围校验返回 422
12. 调优单例重置
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 测试用独立持久化目录，避免与其他测试模块相互干扰
TEST_PERSIST_DIR = "./tests/_chroma_data_tuner"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离 ChromaDB 目录并重置调优相关单例。"""
    from app.core.config import get_settings
    from app.knowledge import (
        bm25 as bm25_module,
    )
    from app.knowledge import (
        hybrid_retriever as hybrid_module,
    )
    from app.knowledge import (
        retrieval_tuner as tuner_module,
    )
    from app.knowledge import (
        reranker as reranker_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留，保证从零开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置所有相关单例
    tuner_module.reset_retrieval_tuner()
    hybrid_module.reset_hybrid_retriever()
    bm25_module.reset_bm25_retriever()
    reranker_module.reset_reranker()

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    tuner_module.reset_retrieval_tuner()
    hybrid_module.reset_hybrid_retriever()
    bm25_module.reset_bm25_retriever()
    reranker_module.reset_reranker()


@pytest.fixture()
def app_with_routers():
    """提供注册了 tuner 路由的 FastAPI 应用。"""
    from app.api.v1.evaluation import router as evaluation_router
    from app.api.v1.tuner import router as tuner_router

    app = FastAPI()
    app.include_router(evaluation_router)
    app.include_router(tuner_router)
    return app


@pytest.fixture()
def client(app_with_routers):
    """提供 TestClient。"""
    return TestClient(app_with_routers)


# ----------------------------------------------------------------------
# TunerParams 模型校验测试
# ----------------------------------------------------------------------


def test_tuner_params_default_values():
    """TunerParams 默认值应与 Settings 一致。"""
    from app.knowledge.retrieval_tuner import TunerParams

    params = TunerParams()
    assert params.vector_top_k == 25
    assert params.bm25_top_k == 25
    assert params.rerank_top_k == 5
    assert params.similarity_threshold == 0.6
    assert params.rrf_k == 60
    assert params.rrf_vector_weight == 0.6
    assert params.rrf_keyword_weight == 0.4


def test_tuner_params_validation_rejects_out_of_range():
    """TunerParams 应拒绝超范围字段。"""
    from pydantic import ValidationError

    from app.knowledge.retrieval_tuner import TunerParams

    # vector_top_k 超范围
    with pytest.raises(ValidationError):
        TunerParams(vector_top_k=0)
    with pytest.raises(ValidationError):
        TunerParams(vector_top_k=101)

    # similarity_threshold 超范围
    with pytest.raises(ValidationError):
        TunerParams(similarity_threshold=-0.1)
    with pytest.raises(ValidationError):
        TunerParams(similarity_threshold=1.1)

    # rrf_k 超范围
    with pytest.raises(ValidationError):
        TunerParams(rrf_k=0)
    with pytest.raises(ValidationError):
        TunerParams(rrf_k=201)

    # rrf 权重超范围
    with pytest.raises(ValidationError):
        TunerParams(rrf_vector_weight=1.5)
    with pytest.raises(ValidationError):
        TunerParams(rrf_keyword_weight=-0.1)


def test_tuner_params_merge_preserves_unset_fields():
    """merge 应保留未传字段，仅更新非空字段。"""
    from app.knowledge.retrieval_tuner import TunerParams

    base = TunerParams()
    patch = TunerParams(vector_top_k=30, similarity_threshold=0.7)
    merged = base.merge(patch)

    # 更新字段应反映新值
    assert merged.vector_top_k == 30
    assert merged.similarity_threshold == 0.7
    # 未更新字段应保持原值
    assert merged.bm25_top_k == 25
    assert merged.rerank_top_k == 5
    assert merged.rrf_k == 60


# ----------------------------------------------------------------------
# RetrievalTuner 单例测试
# ----------------------------------------------------------------------


def test_retrieval_tuner_loads_defaults_when_no_persist_file():
    """无持久化文件时应加载默认参数。"""
    from app.knowledge.retrieval_tuner import (
        get_retrieval_tuner,
        reset_retrieval_tuner,
    )

    reset_retrieval_tuner()
    tuner = get_retrieval_tuner()
    params = tuner.get_params()

    assert params.vector_top_k == 25
    assert params.bm25_top_k == 25
    assert params.rerank_top_k == 5


def test_retrieval_tuner_update_params_takes_effect():
    """update_params 应立即更新参数并返回新值。"""
    from app.knowledge.retrieval_tuner import (
        TunerParams,
        get_retrieval_tuner,
        reset_retrieval_tuner,
    )

    reset_retrieval_tuner()
    tuner = get_retrieval_tuner()

    new_params = TunerParams(
        vector_top_k=30,
        bm25_top_k=28,
        rerank_top_k=4,
        similarity_threshold=0.65,
    )
    updated = tuner.update_params(new_params)

    assert updated.vector_top_k == 30
    assert updated.bm25_top_k == 28
    assert updated.rerank_top_k == 4
    assert updated.similarity_threshold == 0.65

    # 再次读取应保持更新后的值
    assert tuner.get_params().vector_top_k == 30


def test_retrieval_tuner_persists_to_json():
    """update_params 后应持久化到 JSON 文件。"""
    from app.core.config import get_settings
    from app.knowledge.retrieval_tuner import (
        TunerParams,
        get_retrieval_tuner,
        reset_retrieval_tuner,
    )

    reset_retrieval_tuner()
    tuner = get_retrieval_tuner()
    tuner.update_params(TunerParams(vector_top_k=22, rrf_k=50))

    settings = get_settings()
    persist_path = Path(settings.CHROMA_PERSIST_DIR) / "tuner_params.json"
    assert persist_path.exists(), "持久化文件应存在"

    import json

    payload = json.loads(persist_path.read_text(encoding="utf-8"))
    assert payload["vector_top_k"] == 22
    assert payload["rrf_k"] == 50


def test_retrieval_tuner_loads_from_persist_file():
    """重启后（单例重置）应从持久化文件恢复参数。"""
    from app.knowledge.retrieval_tuner import (
        TunerParams,
        get_retrieval_tuner,
        reset_retrieval_tuner,
    )

    # 第一次：写入参数
    reset_retrieval_tuner()
    tuner = get_retrieval_tuner()
    tuner.update_params(TunerParams(rerank_top_k=3, similarity_threshold=0.7))

    # 第二次：重置单例模拟重启
    reset_retrieval_tuner()
    tuner = get_retrieval_tuner()
    params = tuner.get_params()

    assert params.rerank_top_k == 3
    assert params.similarity_threshold == 0.7


def test_retrieval_tuner_reset_to_defaults():
    """reset_to_defaults 应恢复默认值并清空持久化。"""
    from app.knowledge.retrieval_tuner import (
        TunerParams,
        get_retrieval_tuner,
        reset_retrieval_tuner,
    )

    reset_retrieval_tuner()
    tuner = get_retrieval_tuner()
    # 先改成非默认值
    tuner.update_params(TunerParams(vector_top_k=30, rrf_k=80))

    # 重置
    params = tuner.reset_to_defaults()
    assert params.vector_top_k == 25
    assert params.rrf_k == 60
    assert params.bm25_top_k == 25

    # 单例内参数应也是默认值
    assert tuner.get_params().vector_top_k == 25


def test_retrieval_tuner_resets_downstream_singletons():
    """update_params 应触发 hybrid_retriever/bm25/reranker 单例重置。"""
    from app.knowledge import (
        bm25 as bm25_module,
    )
    from app.knowledge import (
        hybrid_retriever as hybrid_module,
    )
    from app.knowledge import (
        reranker as reranker_module,
    )
    from app.knowledge import (
        retrieval_tuner as tuner_module,
    )
    from app.knowledge.retrieval_tuner import TunerParams

    tuner_module.reset_retrieval_tuner()
    tuner = tuner_module.get_retrieval_tuner()

    # 触发单例创建（延迟初始化）
    hybrid_module.get_hybrid_retriever()
    bm25_module.get_bm25_retriever()
    reranker_module.get_reranker()

    # 更新参数应重置下游单例
    tuner.update_params(TunerParams(vector_top_k=26))

    # 单例重置后内部 _hybrid_retriever 等应为 None
    assert hybrid_module._hybrid_retriever is None
    assert bm25_module._bm25_retriever is None
    assert reranker_module._reranker is None


# ----------------------------------------------------------------------
# 并发测试
# ----------------------------------------------------------------------


def test_retrieval_tuner_concurrent_update_thread_safe():
    """并发更新应线程安全，最终值为最后一次写入。"""
    from app.knowledge.retrieval_tuner import (
        get_retrieval_tuner,
        reset_retrieval_tuner,
    )

    reset_retrieval_tuner()
    tuner = get_retrieval_tuner()

    errors = []

    def updater(value: int) -> None:
        try:
            tuner.update_params(
                type(tuner.get_params())(vector_top_k=value)
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=updater, args=(v,)) for v in range(20, 30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发更新不应抛异常：{errors}"
    # 最终值应在合法范围内
    final = tuner.get_params().vector_top_k
    assert 20 <= final <= 29


# ----------------------------------------------------------------------
# API 端点测试
# ----------------------------------------------------------------------


def test_api_get_params_returns_current(client: TestClient):
    """GET /api/v1/tuner/params 应返回当前参数。"""
    response = client.get("/api/v1/tuner/params")
    assert response.status_code == 200
    body = response.json()
    assert "vector_top_k" in body
    assert "bm25_top_k" in body
    assert "rerank_top_k" in body
    assert "similarity_threshold" in body
    assert "rrf_k" in body


def test_api_put_params_updates_and_persists(client: TestClient):
    """PUT /api/v1/tuner/params 应更新参数并持久化。"""
    response = client.put(
        "/api/v1/tuner/params",
        json={"vector_top_k": 28, "similarity_threshold": 0.65},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["vector_top_k"] == 28
    assert body["similarity_threshold"] == 0.65

    # 再次 GET 应反映更新后的值
    get_resp = client.get("/api/v1/tuner/params")
    assert get_resp.json()["vector_top_k"] == 28


def test_api_put_params_rejects_out_of_range(client: TestClient):
    """PUT /api/v1/tuner/params 应拒绝超范围参数并返回 422。"""
    response = client.put(
        "/api/v1/tuner/params",
        json={"vector_top_k": 200},  # 超过 100 上限
    )
    assert response.status_code == 422


def test_api_reset_params_returns_defaults(client: TestClient):
    """POST /api/v1/tuner/reset 应返回默认参数。"""
    # 先改成非默认值
    client.put("/api/v1/tuner/params", json={"vector_top_k": 28})

    # 重置
    response = client.post("/api/v1/tuner/reset")
    assert response.status_code == 200
    body = response.json()
    assert body["vector_top_k"] == 25
    assert body["bm25_top_k"] == 25
    assert body["rerank_top_k"] == 5


def test_api_put_params_partial_update_preserves_other_fields(
    client: TestClient,
):
    """PUT /api/v1/tuner/params 部分更新应保留未传字段。"""
    # 先重置到默认
    client.post("/api/v1/tuner/reset")

    # 仅更新 vector_top_k
    response = client.put(
        "/api/v1/tuner/params",
        json={"vector_top_k": 30},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["vector_top_k"] == 30
    # 未传字段应保持默认
    assert body["bm25_top_k"] == 25
    assert body["rerank_top_k"] == 5


def test_tuner_singleton_reset_isolation():
    """reset_retrieval_tuner 后再次获取应是新实例。"""
    from app.knowledge.retrieval_tuner import (
        get_retrieval_tuner,
        reset_retrieval_tuner,
    )

    tuner1 = get_retrieval_tuner()
    reset_retrieval_tuner()
    tuner2 = get_retrieval_tuner()

    assert tuner1 is not tuner2
