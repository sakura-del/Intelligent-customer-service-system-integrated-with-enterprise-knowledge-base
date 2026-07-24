"""发布与运营接口。

提供灰度实验管理、运营看板、上线检查清单的 HTTP 接口：
- POST   /api/v1/operations/experiments：创建实验
- GET    /api/v1/operations/experiments：列出实验
- GET    /api/v1/operations/experiments/{name}/results：查询实验结果
- POST   /api/v1/operations/experiments/{name}/metrics：记录指标
- GET    /api/v1/operations/dashboard：运营看板数据
- GET    /api/v1/operations/release-checklist：上线检查报告

设计要点：
- 通过 API Key 鉴权保护运维接口，未配置 API_KEY 时进入开发免鉴权模式
- 实验名重复时返回 409，未找到时返回 404
- 看板数据复用 30 秒缓存，避免高频请求重复聚合
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.experiment import get_experiment_manager
from app.core.logging import get_logger
from app.core.operations import (
    ReleaseChecklist,
    get_operations_collector,
)
from app.core.security import verify_api_key
from app.schemas.operations import (
    ChecklistReport,
    CreateExperimentRequest,
    Experiment,
    ExperimentResults,
    OperationsDashboard,
    RecordMetricRequest,
)

logger = get_logger("app.api.v1.operations")

router = APIRouter(
    prefix="/api/v1/operations",
    tags=["发布与运营"],
    dependencies=[Depends(verify_api_key)],
)


# ----------------------------------------------------------------------
# 灰度实验接口
# ----------------------------------------------------------------------


@router.post(
    "/experiments",
    response_model=Experiment,
    status_code=status.HTTP_201_CREATED,
)
def create_experiment(request: CreateExperimentRequest) -> Experiment:
    """创建实验。

    若实验名已存在则覆盖并清空历史指标，便于重新启动实验。
    """
    manager = get_experiment_manager()
    existing = manager.get_experiment(request.name)
    if existing is not None:
        logger.info("实验已存在，覆盖重建：name=%s", request.name)
    return manager.create_experiment_from_request(request)


@router.get("/experiments", response_model=list[Experiment])
def list_experiments() -> list[Experiment]:
    """列出全部实验。"""
    return get_experiment_manager().list_experiments()


@router.get(
    "/experiments/{name}/results",
    response_model=ExperimentResults,
)
def get_experiment_results(name: str) -> ExperimentResults:
    """查询实验结果（各分组指标统计）。

    实验不存在时返回 404。
    """
    results = get_experiment_manager().get_results(name)
    if results is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实验不存在：{name}",
        )
    return results


@router.post(
    "/experiments/{name}/metrics",
    status_code=status.HTTP_200_OK,
)
def record_metric(name: str, request: RecordMetricRequest) -> None:
    """记录一条实验指标。

    即使实验不存在也允许记录，便于回放与离线分析。
    """
    get_experiment_manager().record_metric(
        experiment_name=name,
        variant=request.variant,
        metric_name=request.metric_name,
        value=request.value,
    )


# ----------------------------------------------------------------------
# 运营看板接口
# ----------------------------------------------------------------------


@router.get("/dashboard", response_model=OperationsDashboard)
def get_dashboard(
    force_refresh: bool = Query(
        False,
        description="是否强制刷新缓存，跳过 30 秒缓存窗口",
    ),
) -> OperationsDashboard:
    """返回运营看板聚合数据。

    30 秒内重复调用返回缓存结果，避免重复聚合。
    force_refresh=True 时跳过缓存强制重新聚合。
    """
    return get_operations_collector().collect(force_refresh=force_refresh)


# ----------------------------------------------------------------------
# 上线检查清单接口
# ----------------------------------------------------------------------


@router.get("/release-checklist", response_model=ChecklistReport)
def get_release_checklist() -> ChecklistReport:
    """执行上线检查清单并返回报告。

    每项检查独立执行，失败不中断其他检查。
    """
    return ReleaseChecklist().run_all_checks()
