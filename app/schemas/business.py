"""业务查询 Agent 数据模型。

定义业务场景枚举、参数提取结果与执行结果的数据契约，
作为 BusinessAgent 与 mock 业务系统 API 之间的统一结构。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class BusinessScene(str, Enum):
    """业务场景枚举。

    继承 str 便于直接序列化与比较，避免在路由表中再做类型转换。
    """

    ORDER = "order"  # 订单查询：状态/物流/金额
    RETURN = "return"  # 退换货：创建/查询/取消
    MEMBER = "member"  # 会员信息：积分/等级/优惠券
    ACCOUNT = "account"  # 账户查询：余额/账单/交易记录


class ExtractedParams(BaseModel):
    """从用户对话中提取的业务参数。

    所有标识字段均为 Optional：用户未提供的字段留空，
    由 Agent 决定是否继续交互追问或用会话上下文补齐。
    action 用于区分读写操作，写操作需走二次确认流程。
    """

    scene: BusinessScene = Field(
        ...,
        description="业务场景，决定路由到哪类处理逻辑",
    )
    action: str = Field(
        "query",
        description="操作类型：query(查询)/create(创建)/cancel(取消)",
    )
    order_id: str | None = Field(None, description="订单号")
    return_id: str | None = Field(None, description="退换货单号")
    user_id: str | None = Field(None, description="用户标识")
    phone: str | None = Field(None, description="手机号")
    query_type: str | None = Field(
        None,
        description="查询细分类型，如 status/logistics/amount/points/level 等",
    )
    reason: str | None = Field(None, description="退换货原因")


class BusinessResult(BaseModel):
    """业务 Agent 执行结果。

    need_confirmation=True 表示写操作等待用户确认，
    调用方应将 confirmation_token 原样回传或让用户回复确认；
    error 非 None 时表示本次执行未成功（鉴权/限流/数据缺失等），
    reply 仍给出可读提示便于前端直接展示。
    """

    reply: str = Field(..., description="给用户的最终回复文本")
    scene: BusinessScene | None = Field(None, description="命中的业务场景，便于上层路由统计")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="结构化结果数据，已脱敏，供前端展示或调试",
    )
    need_confirmation: bool = Field(
        False,
        description="是否需要用户二次确认（写操作）",
    )
    confirmation_token: str | None = Field(
        None,
        description="待确认操作的令牌，确认时回传或由 Agent 内部匹配",
    )
    error: str | None = Field(
        None,
        description="错误码/错误信息，None 表示执行成功",
    )
    success: bool = Field(
        True,
        description="是否执行成功，便于程序化判断",
    )
