"""可观测性 API 端点。

提供熔断器、告警、健康检查、Token 用量的运维查询接口：
- GET  /api/v1/observability/circuit-breakers：列出所有熔断器状态
- POST /api/v1/observability/circuit-breakers/{name}/reset：手动重置熔断器
- GET  /api/v1/observability/alerts：查询告警列表（支持 level/source 过滤）
- GET  /api/v1/observability/health：健康检查报告
- GET  /api/v1/observability/token-usage：Token 用量统计（支持 window 参数）

设计要点：
- 不做鉴权，便于运维面板无凭据访问（与 monitor 模块保持一致）
- 全部只读（除 reset 端点外），不修改核心业务状态
- 返回结构扁平化，便于前端直接渲染
- 异常被捕获并返回 500，避免暴露内部堆栈
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.core.circuit_breaker import get_circuit_breaker_registry
from app.core.logging import get_logger
from app.core.observability import (
    AlertLevel,
    get_alert_manager,
    get_health_checker,
    get_token_usage_tracker,
)
from app.schemas.observability import CircuitBreakerStats

logger = get_logger("app.api.v1.observability")

router = APIRouter(prefix="/api/v1/observability", tags=["可观测性"])


# ----------------------------------------------------------------------
# 熔断器端点
# ----------------------------------------------------------------------


@router.get("/circuit-breakers", response_model=Dict[str, CircuitBreakerStats])
def list_circuit_breakers() -> Dict[str, CircuitBreakerStats]:
    """列出所有熔断器的当前状态与统计。

    返回 name -> CircuitBreakerStats 的字典，便于前端按名索引。
    """
    return get_circuit_breaker_registry().list_all()


@router.post(
    "/circuit-breakers/{name}/reset",
    response_model=Dict[str, Any],
)
def reset_circuit_breaker(name: str) -> Dict[str, Any]:
    """手动重置指定熔断器到 CLOSED 状态。

    用于运维介入后强制恢复，不依赖恢复时长。
    熔断器不存在时返回 404。
    """
    success = get_circuit_breaker_registry().reset(name)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"熔断器不存在：{name}",
        )
    logger.info("熔断器 [%s] 已通过 API 手动重置", name)
    return {"name": name, "reset": True}


# ----------------------------------------------------------------------
# 告警端点
# ----------------------------------------------------------------------


@router.get("/alerts", response_model=List[Dict[str, Any]])
def list_alerts(
    level: Optional[str] = Query(
        None, description="按级别过滤：info/warn/error/critical"
    ),
    source: Optional[str] = Query(None, description="按来源过滤，如 token_usage"),
    since: Optional[str] = Query(
        None, description="起始时间（ISO8601），仅返回该时间之后的告警"
    ),
) -> List[Dict[str, Any]]:
    """查询告警列表。

    全部参数可选，未指定的维度不做过滤。
    level 字符串会转为 AlertLevel 枚举，无效值返回 400。
    """
    level_enum = _parse_alert_level(level)
    alerts = get_alert_manager().list_alerts(
        level=level_enum, source=source, since=since
    )
    return [alert.model_dump(mode="json") for alert in alerts]


def _parse_alert_level(level: Optional[str]) -> Optional[AlertLevel]:
    """把字符串级别转为枚举，None 直接返回，无效值抛 400。"""
    if level is None:
        return None
    try:
        return AlertLevel(level)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的告警级别：{level}，支持 info/warn/error/critical",
        )


# ----------------------------------------------------------------------
# 健康检查端点
# ----------------------------------------------------------------------


@router.get("/health", response_model=Dict[str, Any])
def health_check() -> Dict[str, Any]:
    """执行全部健康检查并返回聚合报告。

    每项检查独立执行，单项失败不影响其他检查项。
    """
    report = get_health_checker().check_all()
    return report.model_dump(mode="json")


# ----------------------------------------------------------------------
# Token 用量端点
# ----------------------------------------------------------------------


@router.get("/token-usage", response_model=Dict[str, Any])
def token_usage(
    window: str = Query(
        "hour", description="统计窗口：minute/hour/day"
    ),
) -> Dict[str, Any]:
    """返回指定窗口的 Token 用量统计。

    window 取值：minute / hour / day，其他值返回 400。
    """
    if window not in ("minute", "hour", "day"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的窗口：{window}，支持 minute/hour/day",
        )
    stats = get_token_usage_tracker().get_stats(window=window)
    return stats.model_dump(mode="json")
