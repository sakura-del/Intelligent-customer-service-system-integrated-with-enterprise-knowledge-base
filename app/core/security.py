"""鉴权骨架。

提供基于 API Key 的依赖项，校验请求头中的 X-API-Key。
当 API_KEY 未配置时（开发环境）放行，便于本地调试；
生产环境必须配置以保障安全。
"""

import secrets

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings


def verify_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> str:
    """校验请求头中的 API Key。

    设计为 FastAPI 依赖项，可复用到任意路由。
    未配置服务端 API_KEY 时视为开发模式，直接放行，
    避免本地调试时频繁填写鉴权信息。
    """
    # 开发模式：服务端未配置 API Key 时放行所有请求
    if not settings.api_key_configured:
        return "dev-mode"

    # 生产模式：使用恒定时间比较防止时序攻击
    # secrets.compare_digest 在两个字符串不一致时仍保持相近耗时，
    # 避免攻击者通过响应时间差逐字符猜测 API Key
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或缺失的 API Key，请在请求头中提供 X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    return x_api_key
