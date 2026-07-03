"""坐席辅助端点相关数据模型。

为 spec: add-agent-assist-endpoints 提供请求/响应 Pydantic 模型，
覆盖待接入列表、会话详情、坐席发消息、知识推荐、业务辅助、
方案录入、标记已解决、接手等端点。复用 schemas/escalation.py 中的
EscalationCard / EscalationPriority / HumanSolutionRecord，避免重复定义。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.schemas.escalation import EscalationCard, EscalationPriority


class AgentSessionSummary(BaseModel):
    """待接入会话摘要，供坐席列表展示。

    仅暴露关键字段，避免完整 history 拖慢列表响应。
    """

    session_id: str = Field(..., description="会话 ID")
    user_id: Optional[str] = Field(None, description="用户标识")
    priority: EscalationPriority = Field(
        EscalationPriority.INFO, description="转接优先级"
    )
    escalate_reason: str = Field("", description="转接原因")
    turn_count: int = Field(0, description="本次对话已进行轮数")
    created_at: str = Field("", description="会话创建时间")
    agent_status: Optional[str] = Field(None, description="坐席侧状态")
    assigned_agent_id: Optional[str] = Field(None, description="已分配坐席 ID")


class AgentSessionDetail(BaseModel):
    """会话详情，含 EscalationCard 与完整 history。

    坐席接手前查看，便于快速理解用户诉求与机器人处理历程。
    """

    session_id: str = Field(..., description="会话 ID")
    user_id: Optional[str] = Field(None, description="用户标识")
    channel: str = Field("", description="接入渠道")
    agent_status: Optional[str] = Field(None, description="坐席侧状态")
    assigned_agent_id: Optional[str] = Field(None, description="已分配坐席 ID")
    turn_count: int = Field(0, description="本次对话已进行轮数")
    emotion_score: Optional[float] = Field(None, description="用户情绪得分")
    escalation_card: Optional[EscalationCard] = Field(
        None, description="转接上下文卡片"
    )
    history: List[Dict[str, Any]] = Field(
        default_factory=list, description="完整对话历史"
    )
    created_at: str = Field("", description="会话创建时间")


class AgentMessageRequest(BaseModel):
    """坐席发消息请求体。

    content 最小长度 1，避免空消息污染 history。
    """

    content: str = Field(..., min_length=1, description="坐席发送的消息内容")


class AgentMessageResponse(BaseModel):
    """坐席发消息响应。"""

    message_id: str = Field(..., description="消息唯一 ID")
    timestamp: str = Field(..., description="消息时间戳")
    role: str = Field("assistant", description="消息角色，默认 assistant")


class KnowledgeRecommendRequest(BaseModel):
    """知识推荐请求体。"""

    query: str = Field(..., min_length=1, description="坐席输入的查询")
    top_k: int = Field(5, ge=1, le=20, description="返回片段数，1-20 之间")


class KnowledgeChunk(BaseModel):
    """单个知识片段。"""

    content: str = Field(..., description="片段内容")
    score: float = Field(..., description="相关性得分")
    source: str = Field("", description="片段来源标识")


class KnowledgeRecommendResponse(BaseModel):
    """知识推荐响应。"""

    chunks: List[KnowledgeChunk] = Field(
        default_factory=list, description="知识片段列表"
    )
    total: int = Field(0, description="命中的片段总数")


class BusinessAssistRequest(BaseModel):
    """业务辅助查询请求体。"""

    query: str = Field(..., min_length=1, description="坐席自然语言业务查询")


class BusinessAssistResponse(BaseModel):
    """业务辅助查询响应。

    业务异常不抛 5xx，降级为 result.error 字段。
    """

    result: Dict[str, Any] = Field(
        default_factory=dict, description="业务查询结果"
    )
    masked_fields: List[str] = Field(
        default_factory=list, description="已脱敏字段名列表"
    )


class SolutionSubmitRequest(BaseModel):
    """坐席录入方案请求体。

    复用 KnowledgeFeedback.record_human_solution 入队，
    intent 不传时由系统自动识别。
    """

    question: str = Field(..., min_length=1, description="用户原始问题")
    solution: str = Field(..., min_length=1, description="人工给出的解决方案")
    intent: Optional[str] = Field(
        None, description="标注意图；不传时由系统自动识别"
    )


class ResolveRequest(BaseModel):
    """标记已解决请求体。"""

    note: Optional[str] = Field(None, description="解决备注，可选")


class ResolveResponse(BaseModel):
    """标记已解决响应。"""

    session_id: str = Field(..., description="会话 ID")
    agent_status: str = Field(..., description="解决后的坐席侧状态")
    resolved_at: str = Field(..., description="解决时间戳")


class AcceptRequest(BaseModel):
    """坐席接手请求体。

    agent_id 可选，默认 agent-default，便于无身份系统场景下使用。
    """

    agent_id: str = Field("agent-default", description="坐席标识")
