"""可观测性数据契约。

定义熔断器、告警、健康检查、Token 用量等可观测性模块的数据结构，
作为 core 模块与 API 路由之间的统一契约。

设计要点：
- 所有数据模型使用 Pydantic BaseModel，便于 FastAPI 自动序列化与校验
- 枚举使用 str + Enum，方便 JSON 序列化与 OpenAPI 文档展示
- 字段默认值与描述统一在 schema 层声明，core 与 api 层只引用不复述
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ----------------------------------------------------------------------
# 熔断器数据契约
# ----------------------------------------------------------------------


class CircuitBreakerState(str, Enum):
    """熔断器状态枚举。

    使用 str + Enum 便于 JSON 序列化与 OpenAPI 文档展示。
    """

    CLOSED = "closed"  # 正常放行
    OPEN = "open"  # 熔断中，快速失败
    HALF_OPEN = "half_open"  # 半开探测，允许有限请求


class CircuitBreakerStats(BaseModel):
    """熔断器运行统计。

    用于 API 返回与日志记录，包含当前状态、阈值参数与累计计数，
    便于运维面板快速判断外部依赖健康状况。
    """

    name: str = Field(..., description="熔断器名称，如 llm/vector_store/business_api")
    state: CircuitBreakerState = Field(..., description="当前状态")
    failure_threshold: int = Field(..., description="连续失败阈值，达到即熔断")
    recovery_timeout: int = Field(..., description="熔断恢复时长（秒）")
    success_threshold: int = Field(..., description="半开转关闭所需成功数")
    failure_count: int = Field(..., description="当前连续失败次数（CLOSED 窗口内）")
    success_count: int = Field(..., description="当前连续成功次数（HALF_OPEN 窗口内）")
    total_failures: int = Field(..., description="历史累计失败次数")
    total_successes: int = Field(..., description="历史累计成功次数")
    last_opened_at: Optional[str] = Field(
        None, description="最近一次进入 OPEN 的时间（ISO8601 UTC）"
    )
    last_failure_at: Optional[str] = Field(
        None, description="最近一次失败时间（ISO8601 UTC）"
    )
    last_success_at: Optional[str] = Field(
        None, description="最近一次成功时间（ISO8601 UTC）"
    )
