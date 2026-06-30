"""对话润色相关数据模型。

定义对话上下文与润色结果的结构，
作为 DialogAgent 与上游调用方之间的数据契约。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DialogContext(BaseModel):
    """对话上下文。

    承载会话历史、当前意图、情绪分数等信息，
    供 DialogAgent 在润色时保持上下文连贯与风格一致。
    raw_answer 与 sources 由 generate 在调用前注入，
    便于 polish 方法统一从 context 取用。
    """

    session_id: Optional[str] = Field(None, description="会话 ID")
    user_id: Optional[str] = Field(None, description="用户标识")
    history: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="历史对话列表，每条含 role 与 content",
    )
    current_intent: Optional[str] = Field(None, description="当前识别意图")
    emotion_score: Optional[float] = Field(
        None, description="用户情绪分数，0-1，越低表示越负面"
    )
    raw_answer: str = Field("", description="待润色的原始答案")
    sources: List[str] = Field(
        default_factory=list, description="引用来源列表"
    )
    # 分层摘要上下文文本（Task 14）：由 ContextManager.build_context 生成
    # 非空时 DialogAgent 优先用它替代 history 摘要，降低 token 消耗
    layered_summary: Optional[str] = Field(
        None,
        description="分层摘要后的上下文文本，供 LLM 模式优先使用",
    )


class DialogResult(BaseModel):
    """对话润色结果。

    reply 为最终给用户的回复文本，
    tone_valid 标识话术是否合规，
    suggestions 提供引导追问建议以减少用户二次提问。
    """

    reply: str = Field(..., description="润色后的客服回复")
    sources: List[str] = Field(
        default_factory=list, description="引用来源列表"
    )
    tone_valid: bool = Field(True, description="话术是否合规")
    suggestions: List[str] = Field(
        default_factory=list,
        description="引导追问建议，减少用户二次提问",
    )
