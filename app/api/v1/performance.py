"""性能监控接口。

提供 Task 20 性能优化模块的运维可观测 API：
- GET /api/v1/performance/metrics：综合性能指标（缓存命中率、并发数、模型路由统计、平均响应时间）
- GET /api/v1/performance/cache/stats：热点缓存统计
- POST /api/v1/performance/cache/invalidate：清空热点缓存（知识库更新后调用）

设计要点：
- 不做鉴权，便于运维面板与知识库更新流水线无凭据调用（生产可加反代鉴权）
- 全部委托给 core.performance 单例，API 层保持薄壳
- 不修改 main.py：路由在测试中显式 include，生产可由部署层按需挂载
"""
from fastapi import APIRouter

from app.core.logging import get_logger
from app.core.performance import (
    get_hot_query_cache,
    get_performance_metrics,
)
from app.schemas.performance import (
    CacheStats,
    CacheStatsResponse,
    InvalidateResult,
    MetricsResponse,
    PerformanceMetrics,
)

logger = get_logger("app.api.v1.performance")

router = APIRouter(prefix="/api/v1/performance", tags=["性能监控"])


@router.get("/metrics", response_model=MetricsResponse)
def metrics() -> MetricsResponse:
    """返回综合性能指标。

    聚合缓存命中率、并发数、模型路由统计与平均响应时间，
    供监控面板顶部展示与告警判断。
    """
    performance_metrics: PerformanceMetrics = get_performance_metrics()
    return MetricsResponse(metrics=performance_metrics)


@router.get("/cache/stats", response_model=CacheStatsResponse)
def cache_stats() -> CacheStatsResponse:
    """返回热点缓存统计。

    包含命中/未命中次数、命中率、当前条目数、LRU 淘汰数等，
    便于判断缓存是否生效与容量是否需调整。
    """
    stats = CacheStats(**get_hot_query_cache().get_stats())
    return CacheStatsResponse(cache=stats)


@router.post("/cache/invalidate", response_model=InvalidateResult)
def invalidate_cache() -> InvalidateResult:
    """清空热点缓存。

    知识库更新后调用，避免缓存返回过期回复。
    返回被清除的条目数，便于审计。
    """
    cleared = get_hot_query_cache().invalidate()
    logger.info("热点缓存已清空：cleared=%d", cleared)
    return InvalidateResult(
        success=True,
        cleared=cleared,
        message=f"已清空 {cleared} 条缓存",
    )
