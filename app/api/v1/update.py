"""文档自动更新机制 HTTP 端点。

提供 Task 17 三种更新策略的对外接口：
- POST /api/v1/update/full：触发全量更新（月度）
- POST /api/v1/update/incremental：触发增量更新（周度）
- POST /api/v1/update/file：单文件路径触发实时更新
- GET /api/v1/update/status：查询最近一次更新结果

所有端点复用全局 verify_api_key 依赖鉴权，
结果通过 UpdateResultResponse 返回，便于前端展示与监控。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.knowledge.update_mechanism import get_update_scheduler
from app.schemas.update import (
    UpdateRequest,
    UpdateResultResponse,
    UpdateSingleFileRequest,
    UpdateStatusResponse,
)

logger = get_logger("app.api.v1.update")

router = APIRouter(
    prefix="/api/v1/update",
    tags=["文档更新"],
    dependencies=[Depends(verify_api_key)],
)


def _to_response(result) -> UpdateResultResponse:
    """将内部 UpdateResult 转为 API 响应模型。

    字段一一对应，单独抽出便于后续字段扩展时统一调整。
    """
    return UpdateResultResponse(
        mode=result.mode,
        scanned=result.scanned,
        added=result.added,
        updated=result.updated,
        skipped=result.skipped,
        deleted=result.deleted,
        failed=result.failed,
        duration_seconds=result.duration_seconds,
        errors=list(result.errors),
    )


@router.post("/full", response_model=UpdateResultResponse)
def trigger_full_update(request: UpdateRequest) -> UpdateResultResponse:
    """触发全量更新。

    扫描 dir_path 下所有支持格式文档，逐个入库；
    与 document_store 比对 doc_hash，已存在且未变更的跳过；
    删除 document_store 中已不存在文件的记录与对应 chunks。
    """
    scheduler = get_update_scheduler()
    result = scheduler.run_full_update(
        dir_path=request.dir_path,
        extensions=request.extensions,
    )
    return _to_response(result)


@router.post("/incremental", response_model=UpdateResultResponse)
def trigger_incremental_update(request: UpdateRequest) -> UpdateResultResponse:
    """触发增量更新。

    扫描 dir_path，仅处理新增或 doc_hash 变化的文件；
    不删除已不存在的文件记录。
    """
    scheduler = get_update_scheduler()
    result = scheduler.run_incremental_update(
        dir_path=request.dir_path,
        extensions=request.extensions,
    )
    return _to_response(result)


@router.post("/file", response_model=UpdateResultResponse)
def trigger_single_file_update(
    request: UpdateSingleFileRequest,
) -> UpdateResultResponse:
    """单文件路径触发实时更新。

    复用 pipeline.ingest_document 完成入库与版本注册，
    适用于 API 触发的实时更新场景。
    """
    scheduler = get_update_scheduler()
    result = scheduler.update_single_file(
        file_path=request.file_path,
        metadata=request.metadata,
    )
    return _to_response(result)


@router.get("/status", response_model=UpdateStatusResponse)
def get_update_status() -> UpdateStatusResponse:
    """查询最近一次更新结果。

    结果缓存在调度器内存中，进程重启后清空。
    未执行过更新时 last_update 为空。
    """
    scheduler = get_update_scheduler()
    last = scheduler.get_last_result()
    if last is None:
        return UpdateStatusResponse(
            last_update=None,
            message="尚未执行过更新",
        )
    return UpdateStatusResponse(
        last_update=_to_response(last),
        message=f"最近一次 {last.mode.value} 更新于 {last.duration_seconds:.2f}s 内完成",
    )
