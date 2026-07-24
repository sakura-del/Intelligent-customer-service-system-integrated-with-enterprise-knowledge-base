"""Agent 监控接口。

提供运维可观测 API：
- GET /api/v1/monitor/overview：系统概览（总 trace 数、成功率、平均耗时、活跃会话数）
- GET /api/v1/monitor/traces：最近 trace 列表（摘要）
- GET /api/v1/monitor/traces/{trace_id}：单条 trace 详情（含每步）
- GET /api/v1/monitor/agents：各 Agent 当前状态（调用次数、平均耗时、成功率）
- GET /api/v1/monitor/sessions：活跃会话列表

设计要点：
- 不做鉴权，便于运维面板无凭据访问（生产环境可加 IP 白名单或反代鉴权）
- 全部只读，不修改 Monitor 内部状态
- 返回结构扁平化，便于前端直接渲染
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.core.monitor import get_monitor

router = APIRouter(prefix="/api/v1/monitor", tags=["监控"])


@router.get("/overview")
def overview() -> dict[str, Any]:
    """返回系统概览统计。

    用于监控面板顶部展示：总 trace 数、成功率、平均耗时、活跃会话数。
    """
    return get_monitor().get_overview()


@router.get("/traces")
def list_traces(
    limit: int = Query(50, ge=1, le=200, description="返回的最多 trace 条数"),
) -> list[dict[str, Any]]:
    """返回最近 trace 列表（摘要，不含 steps 详情）。

    按时间倒序返回（最新在前）。点击单条 trace 后通过
    /traces/{trace_id} 查询详情。
    """
    return get_monitor().get_traces(limit=limit)


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    """返回单条 trace 详情（含 steps 与 sub_tasks）。

    trace_id 不存在时返回 404。
    """
    trace = get_monitor().get_trace(trace_id)
    if trace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"trace 不存在：{trace_id}",
        )
    return trace


@router.get("/agents")
def agent_stats() -> list[dict[str, Any]]:
    """返回各 Agent 当前状态。

    包含已注册但未调用的 agent（调用次数为 0），
    便于面板展示完整 agent 列表。
    """
    return get_monitor().get_agent_stats()


@router.get("/sessions")
def active_sessions() -> list[dict[str, Any]]:
    """返回活跃会话列表。

    按最后活跃时间倒序返回，便于面板关注最近活跃的会话。
    """
    return get_monitor().get_sessions()
