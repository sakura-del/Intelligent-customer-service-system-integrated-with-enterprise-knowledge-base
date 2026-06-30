"""EmbeddingService 加载健壮性测试。

验证 BGE 模型加载的四级回退链路：
1. HuggingFace 主源加载成功
2. 主源失败 → 镜像源成功
3. 主源 + 镜像源失败 → 本地缓存命中
4. 全部失败 → hash fallback
5. 维度校验失败 → 降级 fallback
6. 诊断函数返回结构正确
7. 并发加载不出错
8. 配置切换后重新加载

测试隔离：每个用例独立 mock sentence_transformers 与配置，
调用 reset_embedding_service() 重置单例，避免污染其他测试。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# 预导入 sentence_transformers：避免测试中重复 import 导致 torch C 扩展在
# Windows 下垃圾回收时触发 access violation；模块常驻 sys.modules 后再 patch
import sentence_transformers  # noqa: F401

from app.knowledge.embeddings import (
    BGE_LARGE_ZH_DIMENSION,
    EmbeddingService,
    get_embedding_diagnostics,
    reset_embedding_service,
)


# ----------------------------------------------------------------------
# Mock 工厂：构造可控的 SentenceTransformer 替身
# ----------------------------------------------------------------------


class _FakeModel:
    """可控的 SentenceTransformer 替身。

    支持设置返回维度与 encode 行为，便于测试不同加载场景。
    """

    def __init__(self, dim: int = BGE_LARGE_ZH_DIMENSION) -> None:
        self._dim = dim

    def encode(self, texts: List[str], normalize_embeddings: bool = False) -> List[List[float]]:
        # 返回固定维度向量，仅用于 warmup 与 embed 测试
        return [[0.0] * self._dim for _ in texts]

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim


def _make_st_factory(
    main_raises: Optional[Exception] = None,
    mirror_raises: Optional[Exception] = None,
    local_raises: Optional[Exception] = None,
    dim: int = BGE_LARGE_ZH_DIMENSION,
):
    """构造 SentenceTransformer 替身工厂。

    根据调用时环境变量区分主源/镜像源/本地缓存场景，
    各场景可独立配置抛出异常或返回 fake model。
    """

    def _factory(model_name_or_path: str, *args: Any, **kwargs: Any) -> _FakeModel:
        # 本地缓存路径：包含路径分隔符且非 HuggingFace repo id
        is_local_path = os.path.isdir(model_name_or_path)
        endpoint = os.environ.get("HF_ENDPOINT", "")
        offline = os.environ.get("HF_HUB_OFFLINE", "0") == "1"

        if is_local_path:
            if local_raises is not None:
                raise local_raises
            return _FakeModel(dim=dim)
        if offline:
            # 本地缓存路径已在上分支处理；这里通常是异常路径
            if local_raises is not None:
                raise local_raises
            return _FakeModel(dim=dim)
        if endpoint:
            # 镜像源：endpoint 已被设置
            if mirror_raises is not None:
                raise mirror_raises
            return _FakeModel(dim=dim)
        # 主源：endpoint 未设置
        if main_raises is not None:
            raise main_raises
        return _FakeModel(dim=dim)

    return _factory


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_embedding_singleton():
    """每个用例前后重置 EmbeddingService 单例，避免状态污染。"""
    reset_embedding_service()
    yield
    reset_embedding_service()


@pytest.fixture(autouse=True)
def _restore_env():
    """保留并恢复 HF 相关环境变量，避免测试间相互影响。"""
    snapshot = {
        key: os.environ.get(key)
        for key in ("HF_HUB_OFFLINE", "HF_ENDPOINT")
    }
    yield
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------


def test_load_from_huggingface_main_source_success():
    """主源加载成功：mode=bge，诊断含一次成功尝试。"""
    factory = _make_st_factory()
    with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory):
        service = EmbeddingService()
    assert service.mode == "bge"
    assert service.dimension == BGE_LARGE_ZH_DIMENSION
    # 主源成功后不应再尝试镜像源/本地缓存
    sources = [attempt["source"] for attempt in service._diagnostics]
    assert "huggingface_main" in sources
    assert "hf_mirror" not in sources


def test_load_from_hf_mirror_when_main_fails():
    """主源失败 → 镜像源成功：mode=bge，诊断含主源失败记录。"""
    factory = _make_st_factory(
        main_raises=RuntimeError("main source unavailable")
    )
    with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory):
        service = EmbeddingService()
    assert service.mode == "bge"
    main_attempt = next(
        (a for a in service._diagnostics if a["source"] == "huggingface_main"),
        None,
    )
    assert main_attempt is not None
    assert main_attempt["success"] is False
    assert "main source unavailable" in main_attempt["error"]
    mirror_attempt = next(
        (a for a in service._diagnostics if a["source"] == "hf_mirror"),
        None,
    )
    assert mirror_attempt is not None
    assert mirror_attempt["success"] is True


def test_fallback_when_all_sources_fail():
    """全部加载源失败 → 降级 hash fallback。"""
    factory = _make_st_factory(
        main_raises=RuntimeError("main fail"),
        mirror_raises=RuntimeError("mirror fail"),
        local_raises=RuntimeError("local fail"),
    )
    with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory):
        service = EmbeddingService()
    assert service.mode == "fallback"
    assert service.dimension == BGE_LARGE_ZH_DIMENSION
    # fallback 模式下仍能正常向量化
    vectors = service.embed_texts(["hello", "world"])
    assert len(vectors) == 2
    assert all(len(v) == BGE_LARGE_ZH_DIMENSION for v in vectors)


def test_load_from_local_cache_when_remote_fails(tmp_path):
    """主源 + 镜像源失败 → 本地缓存命中：mode=bge。"""
    # 构造本地缓存目录：包含 config.json + 权重文件
    cache_dir = tmp_path / "bge_cache"
    cache_dir.mkdir()
    (cache_dir / "config.json").write_text(json.dumps({"hidden_size": 1024}))
    (cache_dir / "pytorch_model.bin").write_bytes(b"fake weights")

    factory = _make_st_factory(
        main_raises=RuntimeError("main fail"),
        mirror_raises=RuntimeError("mirror fail"),
    )

    from app.core.config import get_settings

    settings = get_settings()
    original_cache_dir = settings.EMBEDDING_LOCAL_CACHE_DIR
    settings.EMBEDDING_LOCAL_CACHE_DIR = str(cache_dir)
    try:
        with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory):
            service = EmbeddingService()
        assert service.mode == "bge"
        local_attempt = next(
            (a for a in service._diagnostics if a["source"] == "local_cache"),
            None,
        )
        assert local_attempt is not None
        assert local_attempt["success"] is True
    finally:
        settings.EMBEDDING_LOCAL_CACHE_DIR = original_cache_dir


def test_dimension_mismatch_falls_back_to_hash():
    """加载成功但维度非 1024 → 降级 fallback 避免 schema 冲突。"""
    # 构造维度为 768 的 fake model，模拟加载到非 BGE-large 模型
    factory = _make_st_factory(dim=768)
    with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory):
        service = EmbeddingService()
    # 所有源都返回 768 维 → 全部维度校验失败 → 降级 fallback
    assert service.mode == "fallback"
    assert service.dimension == BGE_LARGE_ZH_DIMENSION


def test_diagnostics_returns_complete_structure():
    """get_embedding_diagnostics 返回结构含 mode/dim/attempts/model_name。"""
    factory = _make_st_factory(
        main_raises=RuntimeError("main fail"),
        mirror_raises=RuntimeError("mirror fail"),
        local_raises=RuntimeError("local fail"),
    )
    with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory):
        # 触发单例创建
        from app.knowledge.embeddings import get_embedding_service

        get_embedding_service()
        diagnostics = get_embedding_diagnostics()
    assert diagnostics["mode"] == "fallback"
    assert diagnostics["embedding_dim"] == BGE_LARGE_ZH_DIMENSION
    assert diagnostics["model_name"] == "BAAI/bge-large-zh-v1.5"
    assert isinstance(diagnostics["attempts"], list)
    assert len(diagnostics["attempts"]) >= 3
    for attempt in diagnostics["attempts"]:
        assert "source" in attempt
        assert "success" in attempt
        assert "elapsed_ms" in attempt


def test_concurrent_get_embedding_service_safe():
    """多线程并发 get_embedding_service 不抛错且单例唯一。"""
    # 让所有源都失败，避免触发真实网络请求
    factory = _make_st_factory(
        main_raises=RuntimeError("main fail"),
        mirror_raises=RuntimeError("mirror fail"),
        local_raises=RuntimeError("local fail"),
    )
    results: List[EmbeddingService] = []
    errors: List[Exception] = []
    barrier = threading.Barrier(8)

    def _worker():
        try:
            barrier.wait(timeout=5)
            from app.knowledge.embeddings import get_embedding_service

            results.append(get_embedding_service())
        except Exception as exc:
            errors.append(exc)

    with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory):
        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    assert errors == [], f"并发加载出错：{errors}"
    # 所有线程应拿到同一单例
    assert len(results) == 8
    assert all(r is results[0] for r in results)


def test_config_change_reloads_with_new_cache_dir(tmp_path):
    """修改 EMBEDDING_LOCAL_CACHE_DIR 后重新加载，新配置生效。"""
    from app.core.config import get_settings

    settings = get_settings()
    original_cache_dir = settings.EMBEDDING_LOCAL_CACHE_DIR

    # 第一次：所有源失败 → fallback
    factory_fail = _make_st_factory(
        main_raises=RuntimeError("main fail"),
        mirror_raises=RuntimeError("mirror fail"),
        local_raises=RuntimeError("local fail"),
    )
    with patch.object(sentence_transformers, "SentenceTransformer", side_effect=factory_fail):
        service1 = EmbeddingService()
    assert service1.mode == "fallback"

    # 第二次：配置本地缓存目录 + 主源镜像源失败 + 本地缓存成功
    cache_dir = tmp_path / "new_cache"
    cache_dir.mkdir()
    (cache_dir / "config.json").write_text(json.dumps({"hidden_size": 1024}))
    (cache_dir / "model.safetensors").write_bytes(b"fake weights")
    settings.EMBEDDING_LOCAL_CACHE_DIR = str(cache_dir)

    factory_local_ok = _make_st_factory(
        main_raises=RuntimeError("main fail"),
        mirror_raises=RuntimeError("mirror fail"),
    )
    try:
        with patch.object(
            sentence_transformers, "SentenceTransformer", side_effect=factory_local_ok
        ):
            service2 = EmbeddingService()
        assert service2.mode == "bge"
        # 不同实例，新实例命中本地缓存
        local_attempt = next(
            (a for a in service2._diagnostics if a["source"] == "local_cache"),
            None,
        )
        assert local_attempt is not None
        assert local_attempt["success"] is True
    finally:
        settings.EMBEDDING_LOCAL_CACHE_DIR = original_cache_dir
