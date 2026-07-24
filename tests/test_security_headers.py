"""CORS 白名单与安全响应头测试。

覆盖场景：
1. 白名单内的源被允许（预检请求返回 CORS 头）
2. 白名单外的源被拒绝（无 CORS 头）
3. 安全响应头存在（X-Content-Type-Options/X-Frame-Options/Referrer-Policy）
4. DEBUG 模式下 allow_origins=["*"] 但 allow_credentials=False（修复通配符 + 凭据隐患）
5. HTTPS 请求存在 HSTS 头

说明：为隔离不同 CORS 配置，测试中使用最小化 FastAPI app，
复现 app/main.py 中 create_app 的 CORS 与安全头逻辑，避免依赖全局 Settings 单例。
"""
from __future__ import annotations

from typing import Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient


def _build_app(allowed_origins_str: str = "", debug: bool = False) -> FastAPI:
    """构建复现 main.py 逻辑的最小化 FastAPI 应用。

    参数与生产配置一致：
    - allowed_origins_str：对应 Settings.ALLOWED_ORIGINS（逗号分隔字符串）
    - debug：对应 Settings.DEBUG

    通过参数化构造，避免污染全局 Settings 单例，保证各用例相互独立。
    """
    app = FastAPI(debug=debug)

    # 与 app/main.py create_app 中的 CORS 逻辑保持一致
    allowed_origins = [
        o.strip() for o in allowed_origins_str.split(",") if o.strip()
    ] if allowed_origins_str else []

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins if allowed_origins else (["*"] if debug else []),
        allow_credentials=bool(allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 与 app/main.py 中的安全响应头中间件保持一致
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        """添加安全响应头（与生产逻辑保持一致）。"""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    @app.get("/ping")
    def ping() -> dict:
        """简单探测端点，供测试校验响应头。"""
        return {"ok": True}

    return app


def _preflight(
    client: TestClient,
    origin: str,
    method: str = "GET",
) -> dict:
    """发送 CORS 预检请求并返回响应头。

    预检（OPTIONS）是浏览器执行跨域请求前的标准行为，
    CORS 头由 CORSMiddleware 在预检阶段决定是否回写。
    """
    response = client.options(
        "/ping",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
        },
    )
    return dict(response.headers)


# ============================================================
# 1. 白名单内的源被允许
# ============================================================


def test_whitelisted_origin_allowed() -> None:
    """白名单内的源发起预检请求，应返回对应的 CORS 头。"""
    app = _build_app(allowed_origins_str="https://example.com", debug=False)
    client = TestClient(app)

    headers = _preflight(client, origin="https://example.com")

    # allow_origin 应回显请求源
    assert headers.get("access-control-allow-origin") == "https://example.com"
    # 白名单模式应允许携带凭据
    assert headers.get("access-control-allow-credentials") == "true"


def test_whitelisted_origin_multiple_entries() -> None:
    """多个白名单源中，请求其中一个应被允许。"""
    app = _build_app(
        allowed_origins_str="https://a.example.com,https://b.example.com",
        debug=False,
    )
    client = TestClient(app)

    headers = _preflight(client, origin="https://b.example.com")
    assert headers.get("access-control-allow-origin") == "https://b.example.com"
    assert headers.get("access-control-allow-credentials") == "true"


def test_whitelist_with_spaces_is_trimmed() -> None:
    """白名单字符串中含空格时应被正确 trim，不污染匹配。"""
    app = _build_app(
        allowed_origins_str=" https://example.com , https://app.example.com ",
        debug=False,
    )
    client = TestClient(app)

    headers = _preflight(client, origin="https://example.com")
    assert headers.get("access-control-allow-origin") == "https://example.com"


# ============================================================
# 2. 白名单外的源被拒绝
# ============================================================


def test_non_whitelisted_origin_rejected() -> None:
    """白名单外的源发起预检请求，不应返回 allow-origin 头。

    说明：Starlette 的 CORSMiddleware 会预构建 preflight_headers，
    其中 access-control-allow-credentials 等头在中间件初始化时确定，
    可能对所有预检请求都存在。但 access-control-allow-origin 是在
    每次请求时按源匹配后条件回写的，是浏览器判定跨域是否被允许的关键。
    因此以 access-control-allow-origin 缺失作为拒绝的判定依据。
    """
    app = _build_app(allowed_origins_str="https://example.com", debug=False)
    client = TestClient(app)

    headers = _preflight(client, origin="https://evil.example.com")

    # 不在白名单内的源不应回写 allow-origin，浏览器据此判定跨域被拒绝
    assert "access-control-allow-origin" not in headers


def test_empty_whitelist_in_production_rejects_all() -> None:
    """生产环境（DEBUG=False）且未配置白名单时，应拒绝所有跨域请求。"""
    app = _build_app(allowed_origins_str="", debug=False)
    client = TestClient(app)

    headers = _preflight(client, origin="https://example.com")
    # allow_origins=[] 时任何源都不会回写 allow-origin
    assert "access-control-allow-origin" not in headers


# ============================================================
# 3. 安全响应头存在
# ============================================================


def test_security_headers_present() -> None:
    """所有响应应包含基础安全响应头。"""
    app = _build_app(allowed_origins_str="", debug=False)
    client = TestClient(app)

    response = client.get("/ping")
    assert response.status_code == 200

    headers = response.headers
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_hsts_absent_on_http() -> None:
    """HTTP 请求不应添加 HSTS 头，避免在调试环境下误锁协议。"""
    app = _build_app(allowed_origins_str="", debug=False)
    # 默认 base_url 为 http://testserver
    client = TestClient(app)

    response = client.get("/ping")
    assert "strict-transport-security" not in response.headers


# ============================================================
# 4. DEBUG 模式下 allow_origins=["*"] 但 allow_credentials=False
# ============================================================


def test_debug_mode_allows_wildcard_without_credentials() -> None:
    """DEBUG=True 且未配置白名单时，应允许通配符但不允许凭据。

    修复说明：原配置 allow_origins=["*"] + allow_credentials=True
    在浏览器规范中是被禁止的组合（会被浏览器忽略凭据），
    且存在安全隐患。现统一为通配符模式下不携带凭据。
    """
    app = _build_app(allowed_origins_str="", debug=True)
    client = TestClient(app)

    headers = _preflight(client, origin="https://anything.example.com")

    # 通配符模式应回写 *
    assert headers.get("access-control-allow-origin") == "*"
    # 关键安全校验：通配符模式下不应允许凭据
    assert headers.get("access-control-allow-credentials") != "true"


def test_whitelist_overrides_debug_wildcard() -> None:
    """配置白名单后即便 DEBUG=True 也应使用白名单模式（允许凭据）。"""
    app = _build_app(
        allowed_origins_str="https://example.com",
        debug=True,
    )
    client = TestClient(app)

    headers = _preflight(client, origin="https://example.com")
    # 白名单优先级高于 DEBUG 通配符
    assert headers.get("access-control-allow-origin") == "https://example.com"
    assert headers.get("access-control-allow-credentials") == "true"


# ============================================================
# 5. HTTPS 请求有 HSTS 头
# ============================================================


def test_hsts_present_on_https() -> None:
    """HTTPS 请求应包含 HSTS 头，强制后续访问走 HTTPS。"""
    app = _build_app(allowed_origins_str="", debug=False)
    # 通过 base_url 指定 https 协议，模拟 HTTPS 请求
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/ping")
    assert response.status_code == 200

    hsts = response.headers.get("strict-transport-security")
    assert hsts is not None
    assert "max-age=31536000" in hsts
    assert "includeSubDomains" in hsts
