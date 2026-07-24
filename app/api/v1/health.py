"""健康检查接口。

供运维监控与负载均衡探活使用，返回应用基础信息。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.schemas.chat import HealthResponse

router = APIRouter(prefix="/api/v1", tags=["健康检查"])


@router.get("/health", response_model=HealthResponse)
def health_check(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """返回服务健康状态。

    不做鉴权，便于探活服务无凭据访问。
    同时返回 insecure_mode 字段，提示运维当前是否处于不安全模式。
    """
    return HealthResponse(
        status="ok",
        app=settings.APP_NAME,
        version="0.1.0",
        timestamp=datetime.now(timezone.utc).isoformat(),
        insecure_mode=settings.insecure_mode,
    )
