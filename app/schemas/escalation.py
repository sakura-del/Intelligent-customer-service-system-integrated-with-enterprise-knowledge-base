"""人工客服转接相关数据模型。

定义转接决策（EscalationDecision）与转接上下文卡片（EscalationCard）：
- EscalationDecision：规则引擎产出的"是否转接 + 原因 + 优先级 + 命中规则"
- EscalationCard：转接发生时给人工客服的"用户画像 + 对话摘要 + 已尝试方案"

设计目标：让人工客服接手前能快速理解用户诉求与机器人的处理历程，
减少重复询问，提升转接体验与解决效率。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class EscalationPriority(str, Enum):
    """转接优先级。

    数值含义：
    - HIGHEST：用户主动要求，必须立即转接
    - HIGH：用户情绪激动，优先处理避免激化
    - MEDIUM：连续失败/复杂问题，按队列处理
    - LOW：VIP 用户优先权，但非强制
    - INFO：仅记录（如非工作时间），不实际转接
    """

    HIGHEST = "highest"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class EscalationDecision(BaseModel):
    """转接规则引擎的决策结果。

    should_escalate=False 时其他字段仅作记录，不应触发转接流程。
    rule_matched 用于审计与回放，便于排查"为什么转接了/没转接"。
    """

    should_escalate: bool = Field(False, description="是否应当转接人工")
    reason: str = Field("", description="转接原因（人类可读）")
    priority: EscalationPriority = Field(EscalationPriority.INFO, description="转接优先级")
    rule_matched: str = Field("", description="命中的规则名，便于审计")


class EscalationCard(BaseModel):
    """转接上下文卡片。

    在用户被转接到人工客服时，把会话上下文压缩为一张卡片，
    让人工客服接手前快速了解：用户身份、对话摘要、机器人已尝试方案、转接原因。
    避免人工重复询问已知信息，提升首次响应效率。
    """

    session_id: str = Field(..., description="会话 ID")
    user_id: str | None = Field(None, description="用户标识")
    member_level: str = Field("normal", description="会员等级")
    history_ticket_count: int = Field(0, description="用户历史工单数，反映用户过往诉求量")
    turn_count: int = Field(0, description="本次对话已进行轮数")
    conversation_summary: str = Field("", description="对话摘要：用户咨询的核心问题与当前状态")
    attempted_solutions: list[str] = Field(
        default_factory=list,
        description="智能客服已给出的建议列表，便于人工避免重复方案",
    )
    escalate_reason: str = Field("", description="本次转接的原因")
    priority: EscalationPriority = Field(EscalationPriority.INFO, description="转接优先级")


class HumanSolutionRecord(BaseModel):
    """人工处理方案记录。

    用于知识回流闭环：人工录入解决方案后，
    系统标注意图并入库为 FAQ，下次智能客服可直接检索命中。
    status 表示是否已审核入库，避免未审核内容污染知识库。
    """

    solution_id: str = Field(..., description="方案记录唯一 ID")
    session_id: str | None = Field(None, description="关联会话 ID")
    question: str = Field(..., description="用户原始问题")
    solution: str = Field(..., description="人工给出的解决方案")
    intent: str = Field("", description="标注意图，便于后续归类")
    status: str = Field(
        "pending",
        description="状态：pending(待审核) / approved(已入库) / rejected(已驳回)",
    )


class HumanSolutionRequest(BaseModel):
    """人工录入解决方案的请求体。

    供 POST /api/v1/escalation/solution 端点接收人工录入的方案，
    intent 可不传由系统自动标注。
    """

    session_id: str | None = Field(None, description="关联会话 ID")
    question: str = Field(..., description="用户原始问题")
    solution: str = Field(..., description="人工给出的解决方案")
    intent: str | None = Field(None, description="标注意图；不传时由系统自动识别")
