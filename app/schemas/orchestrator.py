"""调度 Agent 数据模型。

定义意图识别、子任务拆解、调度结果等数据契约，
作为 OrchestratorAgent 内部各环节之间以及对外交互的统一结构。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Intent(str, Enum):
    """用户意图枚举。

    继承 str 便于直接序列化与比较，避免在路由表中再做类型转换。
    """

    KNOWLEDGE_QA = "knowledge_qa"  # 知识问答：命中知识库的常见问题
    BUSINESS_QUERY = "business_query"  # 业务查询：订单/会员/退换货/账户
    EMOTION_SENSITIVE = "emotion_sensitive"  # 情绪敏感：用户带脏话或投诉，需安抚
    TICKET = "ticket"  # 工单：需创建工单跟进
    CHITCHAT = "chitchat"  # 闲聊/问候
    UNKNOWN = "unknown"  # 无法识别：兜底引导或转人工


class SubTask(BaseModel):
    """子任务结构。

    复杂问题会被拆解为多个子任务，每个子任务绑定一个 agent 处理。
    result 在路由分发前为空，分发后回填 agent 输出。
    """

    agent_name: str = Field(..., description="负责处理的 agent 名称")
    input: str = Field(..., description="子任务输入，通常是原始问题或其切片")
    result: str | None = Field(
        None,
        description="agent 处理结果，未执行时为空",
    )


class IntentResult(BaseModel):
    """意图识别结果。

    confidence 用于决定是否走简化调度路径；
    need_emotion_check 提示调度器是否需追加情绪检测。
    """

    intent: Intent = Field(..., description="识别出的意图")
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="意图置信度，0-1 之间",
    )
    sub_tasks: list[SubTask] = Field(
        default_factory=list,
        description="拆解出的子任务列表，简单问题仅含一项",
    )
    need_emotion_check: bool = Field(
        False,
        description="是否需要追加情绪检测",
    )


class OrchestratorResult(BaseModel):
    """调度 Agent 最终输出。

    escalate_to_human=True 表示本轮已标记转人工，调用方应停止自动回复；
    turn_count 与 failed_attempts 反映会话进度，用于触发连续失败兜底。
    """

    reply: str = Field(..., description="给用户的最终回复文本")
    intent: Intent = Field(Intent.UNKNOWN, description="本轮主意图")
    sub_tasks: list[SubTask] = Field(
        default_factory=list,
        description="本轮执行的子任务及其结果",
    )
    escalate_to_human: bool = Field(
        False,
        description="是否标记转人工",
    )
    session_id: str | None = Field(None, description="会话 ID")
    turn_count: int = Field(0, description="当前会话轮数")
    failed_attempts: int = Field(
        0,
        description="连续未解决次数，达到阈值触发转人工",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="附加信息，便于扩展字段（如情绪分等）",
    )
