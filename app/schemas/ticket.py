"""工单相关数据模型。

定义工单的分类、优先级、状态枚举与数据结构，
作为 TicketAgent 与 TicketStore 之间的数据契约，
同时供上游调度器（orchestrator/graph）后续集成时引用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class TicketCategory(str, Enum):
    """工单分类：对应不同处理部门。

    继承 str 与 Enum，便于直接序列化为 JSON 字符串，
    也方便与 HTTP 接口字段对齐。
    """

    after_sale = "after_sale"  # 售后：退换货 / 维修
    logistics = "logistics"  # 物流：发货 / 配送
    product = "product"  # 产品：质量 / 功能
    account = "account"  # 账户：登录 / 支付
    complaint = "complaint"  # 投诉：服务态度 / 体验


class TicketPriority(str, Enum):
    """工单优先级：决定处理顺序与响应时效。

    urgent 最高（需立即介入），low 最低（可延后处理）。
    """

    urgent = "urgent"  # 紧急：愤怒情绪 / VIP / 资金损失
    high = "high"  # 高：影响正常使用
    medium = "medium"  # 中：一般咨询
    low = "low"  # 低：建议 / 反馈


class TicketStatus(str, Enum):
    """工单状态：描述处理进度。

    状态流转：pending → processing → resolved → closed，
    closed 为终态，避免被回退到中间态造成审计混乱。
    """

    pending = "pending"  # 待处理：刚创建尚未分派
    processing = "processing"  # 处理中：已分派正在跟进
    resolved = "resolved"  # 已解决：处理完成待用户确认
    closed = "closed"  # 已关闭：终态


class Ticket(BaseModel):
    """工单模型。

    包含完整生命周期所需字段：身份标识、内容、分类定级、
    状态与时间戳。created_at / updated_at 由存储层维护，
    避免业务层手动管理时间引发不一致。
    """

    ticket_id: str = Field(..., description="工单唯一标识，自动生成")
    user_id: str | None = Field(None, description="用户标识，匿名场景可为空")
    title: str = Field(..., description="工单标题，由问题描述提炼")
    description: str = Field(..., description="问题描述详情")
    category: TicketCategory = Field(..., description="工单分类")
    priority: TicketPriority = Field(..., description="优先级")
    status: TicketStatus = Field(TicketStatus.pending, description="工单状态，默认待处理")
    # 关键附加信息：订单/产品/联系方式，便于人工跟进
    related_order: str | None = Field(None, description="相关订单号")
    related_product: str | None = Field(None, description="相关产品名")
    contact: str | None = Field(None, description="用户联系方式")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="创建时间（UTC）",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="最近更新时间（UTC）",
    )


class TicketResult(BaseModel):
    """工单处理 Agent 的统一返回结构。

    reply 为给用户的回复文本，其余字段透出工单关键属性，
    便于上层（DialogAgent 润色 / API 响应）复用。
    """

    reply: str = Field(..., description="给用户的回复文本")
    ticket_id: str = Field(..., description="工单 ID")
    category: TicketCategory = Field(..., description="工单分类")
    priority: TicketPriority = Field(..., description="优先级")
    status: TicketStatus = Field(..., description="工单状态")
