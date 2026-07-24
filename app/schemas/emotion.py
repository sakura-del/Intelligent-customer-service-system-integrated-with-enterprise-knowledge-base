"""情感分析相关数据模型。

定义情绪类型枚举与情感分析结果的结构，
作为 EmotionAgent 与上游调用方之间的数据契约。
"""

from enum import Enum

from pydantic import BaseModel, Field


class EmotionType(str, Enum):
    """用户情绪类型。

    覆盖客服场景下五类典型情绪，分别对应不同的应对策略：
    - ANGER：愤怒，需安抚并可能转人工
    - ANXIETY：焦虑，需详细解释与时间预期
    - DISAPPOINTMENT：失望，需道歉与解决方案
    - SATISFACTION：满意，需礼貌回应并邀请评价
    - NEUTRAL：中性，按标准流程处理
    """

    ANGER = "anger"
    ANXIETY = "anxiety"
    DISAPPOINTMENT = "disappointment"
    SATISFACTION = "satisfaction"
    NEUTRAL = "neutral"


class EmotionResult(BaseModel):
    """情感分析结果。

    emotion/score/confidence/keywords 由 LLM 或规则兜底产出，
    strategy 与 suggest_escalate 由 EmotionAgent 根据情绪类型与分数推导。
    score 取值 1-5：1=轻微，5=强烈；confidence 取值 0-1。
    """

    emotion: EmotionType = Field(..., description="识别出的情绪类型")
    score: int = Field(..., ge=1, le=5, description="情绪激烈程度 1-5 分")
    confidence: float = Field(..., ge=0.0, le=1.0, description="识别置信度 0-1")
    keywords: list[str] = Field(default_factory=list, description="触发情绪识别的关键词列表")
    strategy: str = Field(..., description="针对该情绪的应对策略")
    suggest_escalate: bool = Field(False, description="是否建议转人工客服")
