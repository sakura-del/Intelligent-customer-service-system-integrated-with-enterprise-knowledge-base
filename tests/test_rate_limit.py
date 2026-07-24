"""全局限流中间件测试。

覆盖场景：
1. 正常请求通过（< 60 req/min）
2. 超出限流返回 429
3. RATE_LIMIT_ENABLED=False 时不限流
4. 不同 IP 独立计数

说明：为隔离测试，使用最小化 FastAPI app 复现 main.py 的限流中间件逻辑，
并通过 reset_limiters() 在用例间清理全局计数状态，避免相互污染。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.rate_limit import (
    rate_limit_middleware,
    reset_limiters,
)


@pytest.fixture(autouse=True)
def _reset_limiter_state() -> Iterator[None]:
    """每个测试前后重置全局限流器状态，避免用例间相互污染。"""
    reset_limiters()
    yield
    reset_limiters()


def _build_app() -> FastAPI:
    """构建挂载限流中间件的最小化 FastAPI 应用。

    复用生产环境中的 rate_limit_middleware，确保测试行为与生产一致。
    """
    app = FastAPI()

    @app.middleware("http")
    async def _limit(request: Request, call_next):
        return await rate_limit_middleware(request, call_next)

    @app.get("/ping")
    def ping() -> dict:
        """简单探测端点，供测试校验限流行为。"""
        return {"ok": True}

    return app


def _get(client: TestClient, client_ip: str, path: str = "/ping"):
    """以指定客户端 IP 发起 GET 请求。

    通过 X-Forwarded-For 头模拟不同客户端 IP，
    避免依赖底层连接的真实远端地址，便于测试多 IP 隔离场景。
    """
    return client.get(path, headers={"X-Forwarded-For": client_ip})


# ============================================================
# 1. 限流阈值内的请求全部放行
# ============================================================


def test_normal_requests_pass_under_limit() -> None:
    """限流阈值内的请求应全部放行（< 60 req/min）。"""
    app = _build_app()
    client = TestClient(app)

    # 发送 60 个请求（恰好达到阈值但未超出），全部应返回 200
    for i in range(60):
        response = _get(client, "1.1.1.1")
        assert response.status_code == 200, f"第 {i + 1} 个请求不应被限流"


# ============================================================
# 2. 超出限流返回 429
# ============================================================


def test_exceeding_limit_returns_429() -> None:
    """超出限流阈值的请求应返回 429，并带 Retry-After 头。"""
    app = _build_app()
    client = TestClient(app)

    # 前 60 个请求放行
    for _ in range(60):
        _get(client, "2.2.2.2")

    # 第 61 个请求应被限流
    response = _get(client, "2.2.2.2")
    assert response.status_code == 429
    body = response.json()
    assert "retry_after" in body
    # Retry-After 头应存在，提示客户端重试时机
    assert "retry-after" in response.headers


def test_429_response_body_format() -> None:
    """429 响应体应包含可读的错误信息与 retry_after 字段。"""
    app = _build_app()
    client = TestClient(app)

    # 用完配额
    for _ in range(60):
        _get(client, "2.2.2.3")

    response = _get(client, "2.2.2.3")
    assert response.status_code == 429
    body = response.json()
    assert "detail" in body
    assert isinstance(body["retry_after"], int)
    assert body["retry_after"] >= 1


# ============================================================
# 3. RATE_LIMIT_ENABLED=False 时不限流
# ============================================================


def test_rate_limit_disabled_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """RATE_LIMIT_ENABLED=False 时不应限流，即便超过阈值也全部放行。"""
    # 构造关闭限流的轻量 settings mock，避免加载完整 Settings 单例
    disabled_settings = SimpleNamespace(RATE_LIMIT_ENABLED=False)
    monkeypatch.setattr(
        "app.middleware.rate_limit.get_settings",
        lambda: disabled_settings,
    )

    app = _build_app()
    client = TestClient(app)

    # 即便超过 60 次请求也应全部放行
    for i in range(70):
        response = _get(client, "3.3.3.3")
        assert response.status_code == 200, f"限流关闭时第 {i + 1} 个请求应放行"


# ============================================================
# 4. 不同 IP 独立计数
# ============================================================


def test_different_ips_count_independently() -> None:
    """不同 IP 的请求计数应相互独立，一个 IP 触发限流不影响另一个。"""
    app = _build_app()
    client = TestClient(app)

    # IP-A 用满 60 次配额
    for _ in range(60):
        assert _get(client, "10.0.0.1").status_code == 200

    # IP-A 第 61 次被限流
    assert _get(client, "10.0.0.1").status_code == 429

    # IP-B 仍可正常访问（独立计数，互不影响）
    for i in range(60):
        assert _get(client, "10.0.0.2").status_code == 200, (
            f"IP-B 第 {i + 1} 个请求应放行"
        )

    # IP-B 第 61 次同样被限流
    assert _get(client, "10.0.0.2").status_code == 429


def test_unknown_ip_fallback_does_not_crash() -> None:
    """缺少 client 信息时不应崩溃，应走 unknown 兜底桶。"""
    app = _build_app()
    # 不带 X-Forwarded-For 头，使用连接远端地址（TestClient 通常是 testclient）
    client = TestClient(app)

    response = client.get("/ping")
    assert response.status_code == 200
