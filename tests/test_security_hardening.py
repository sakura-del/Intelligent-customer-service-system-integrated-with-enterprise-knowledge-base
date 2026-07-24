"""API Key 安全加固测试。

覆盖 Task：API Key 安全加固的核心场景：
1. verify_api_key 使用 secrets.compare_digest 进行恒定时间比较
   - 正确 Key 放行
   - 错误 Key 拒绝（401）
   - 缺失 Key 拒绝（401）
2. Settings.insecure_mode 属性：
   - API_KEY 为空时返回 True
   - API_KEY 非空时返回 False
3. 健康检查响应包含 insecure_mode 字段
4. dev-mode 向后兼容：API_KEY 为空时放行所有请求

说明：为避免 app.main 中其他路由（如 operations.py 的 204 兼容性问题）
导致的预存导入错误影响本测试，健康检查端点测试使用只挂载 health 路由的
最小化 FastAPI 应用，与生产路由配置保持一致。
"""
from __future__ import annotations

import secrets
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.security import verify_api_key


def _build_minimal_app() -> FastAPI:
    """构建只挂载 health 路由的最小化 FastAPI 应用。

    避免导入 app.main 触发其他模块的预存问题，
    同时 health 路由的 Depends(get_settings) 仍使用全局 Settings 单例，
    保证测试中修改配置能被路由感知。
    """
    app = FastAPI()
    app.include_router(health_router)
    return app


@pytest.fixture
def settings_singleton() -> Settings:
    """获取全局 Settings 单例，便于测试中临时修改配置。"""
    return get_settings()


@pytest.fixture
def reset_api_key(settings_singleton: Settings) -> Iterator[None]:
    """每个用例前后保存/恢复 API_KEY，避免污染其他测试。"""
    original_key = settings_singleton.API_KEY
    yield
    settings_singleton.API_KEY = original_key


# ============================================================
# 1. verify_api_key：secrets.compare_digest 使用验证
# ============================================================


def test_verify_api_key_uses_secrets_compare_digest() -> None:
    """verify_api_key 内部应调用 secrets.compare_digest，而非直接使用 == 比较。"""
    settings = get_settings()
    original_key = settings.API_KEY
    settings.API_KEY = "prod-secret-key"
    try:
        # 使用 patch 验证 secrets.compare_digest 被调用
        with patch(
            "app.core.security.secrets.compare_digest", wraps=secrets.compare_digest
        ) as mocked:
            # 正确 Key 应放行
            result = verify_api_key(
                x_api_key="prod-secret-key", settings=settings
            )
            assert result == "prod-secret-key"
            assert mocked.called, "verify_api_key 应调用 secrets.compare_digest"
    finally:
        settings.API_KEY = original_key


def test_verify_api_key_correct_key_passes(reset_api_key: None) -> None:
    """配置 API_KEY 后，携带正确 Key 的请求应放行。"""
    settings = get_settings()
    settings.API_KEY = "my-secret-key-123"

    result = verify_api_key(x_api_key="my-secret-key-123", settings=settings)
    assert result == "my-secret-key-123"


def test_verify_api_key_wrong_key_rejected(reset_api_key: None) -> None:
    """配置 API_KEY 后，携带错误 Key 的请求应被拒绝（返回 401）。"""
    from fastapi import HTTPException

    settings = get_settings()
    settings.API_KEY = "correct-key"

    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(x_api_key="wrong-key", settings=settings)

    assert exc_info.value.status_code == 401
    assert "API Key" in exc_info.value.detail


def test_verify_api_key_missing_key_rejected(reset_api_key: None) -> None:
    """配置 API_KEY 后，未携带 Key 的请求应被拒绝（返回 401）。"""
    from fastapi import HTTPException

    settings = get_settings()
    settings.API_KEY = "correct-key"

    with pytest.raises(HTTPException) as exc_info:
        verify_api_key(x_api_key=None, settings=settings)

    assert exc_info.value.status_code == 401


def test_verify_api_key_dev_mode_bypasses_when_empty(reset_api_key: None) -> None:
    """API_KEY 为空时应进入 dev-mode 放行，保持向后兼容。"""
    settings = get_settings()
    settings.API_KEY = ""

    # 即便不传 Key 也应放行
    result = verify_api_key(x_api_key=None, settings=settings)
    assert result == "dev-mode"

    # 传任意 Key 也应放行（dev-mode 不校验）
    result = verify_api_key(x_api_key="anything", settings=settings)
    assert result == "dev-mode"


# ============================================================
# 2. Settings.insecure_mode 属性验证
# ============================================================


def test_insecure_mode_true_when_api_key_empty() -> None:
    """API_KEY 为空时 insecure_mode 应为 True。"""
    settings = Settings(API_KEY="")
    assert settings.insecure_mode is True


def test_insecure_mode_false_when_api_key_configured() -> None:
    """API_KEY 非空时 insecure_mode 应为 False。"""
    settings = Settings(API_KEY="some-secret-key")
    assert settings.insecure_mode is False


def test_insecure_mode_consistent_with_api_key_configured() -> None:
    """insecure_mode 应与 api_key_configured 互为反值。"""
    # 空配置
    settings_empty = Settings(API_KEY="")
    assert settings_empty.insecure_mode is True
    assert settings_empty.api_key_configured is False

    # 非空配置
    settings_set = Settings(API_KEY="abc")
    assert settings_set.insecure_mode is False
    assert settings_set.api_key_configured is True


# ============================================================
# 3. 健康检查响应包含 insecure_mode 字段
# ============================================================


def test_health_check_returns_insecure_mode_field(reset_api_key: None) -> None:
    """健康检查响应应包含 insecure_mode 字段，反映当前安全模式。"""
    settings = get_settings()
    settings.API_KEY = ""
    client = TestClient(_build_minimal_app())

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()

    # 字段存在且类型正确
    assert "insecure_mode" in body
    assert isinstance(body["insecure_mode"], bool)
    # API_KEY 为空时 insecure_mode 应为 True
    assert body["insecure_mode"] is True


def test_health_check_insecure_mode_false_when_api_key_set(
    reset_api_key: None,
) -> None:
    """配置 API_KEY 后，健康检查的 insecure_mode 应为 False。"""
    settings = get_settings()
    settings.API_KEY = "configured-key"
    client = TestClient(_build_minimal_app())

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()

    assert "insecure_mode" in body
    assert body["insecure_mode"] is False
