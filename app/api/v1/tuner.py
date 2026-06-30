"""检索参数调优端点。

提供运行时调整检索参数的 HTTP 接口：
- GET /api/v1/tuner/params：查询当前调优参数
- PUT /api/v1/tuner/params：更新参数（立即生效）
- POST /api/v1/tuner/reset：重置为默认参数

调参范围校验在 TunerParams 模型内统一执行，
超范围请求返回 422 与具体错误信息。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.knowledge.retrieval_tuner import (
    TunerParams,
    get_retrieval_tuner,
)
from app.schemas.evaluation import TunerParamsUpdateRequest

logger = get_logger("app.api.v1.tuner")

router = APIRouter(
    prefix="/api/v1/tuner",
    tags=["检索调优"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("/params", response_model=TunerParams)
def get_params() -> TunerParams:
    """查询当前调优参数。"""
    return get_retrieval_tuner().get_params()


@router.put("/params", response_model=TunerParams)
def update_params(request: TunerParamsUpdateRequest) -> TunerParams:
    """更新调优参数，立即生效。

    仅更新请求中非空字段，其余保持原值。
    范围校验失败返回 422，附带详细错误信息。
    """
    tuner = get_retrieval_tuner()
    current = tuner.get_params()
    # 合并请求中的非空字段
    patch_data = request.model_dump(exclude_none=True)
    merged = current.model_dump()
    merged.update(patch_data)
    try:
        new_params = TunerParams(**merged)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"参数校验失败：{exc.errors()}",
        )
    return tuner.update_params(new_params)


@router.post("/reset", response_model=TunerParams)
def reset_params() -> TunerParams:
    """重置为默认参数并立即生效。"""
    return get_retrieval_tuner().reset_to_defaults()
