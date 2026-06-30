"""多轮对话分层上下文与意图切换检测数据模型。

为 Task 14（多轮对话与槽位填充）提供数据契约：
- `DialogContext`：分层摘要后的对话上下文，供 agent / dialog 节点使用
- `IntentSwitchResult`：意图切换检测结果，供 intent_node 决策是否重置槽位

设计要点：
- 与 `app/schemas/dialog.py:DialogContext` 区分：本文件聚焦"分层摘要结果"，
  dialog.py 的 DialogContext 聚焦"DialogAgent 润色入参"，两者职责不同。
- 所有字段均带默认值，保证向后兼容，避免破坏既有调用方。
"""
from typing import Any, Dict, List

from pydantic import BaseModel, Field


class DialogContext(BaseModel):
    """分层对话上下文。

    采用分层摘要策略以降低 token 消耗（目标 60%+）：
    - `recent_turns`：近期对话保留完整原文，承载即时上下文
    - `mid_summary`：中期对话压缩为单句摘要，保留要点
    - `early_summary`：早期对话整体压缩为一段摘要，避免丢失长期记忆
    - `full_context_text`：上述三层拼装后的完整文本，供下游直接拼接 prompt

    分层边界由 ContextManager 控制（默认近 5 轮 / 中期 10 轮 / 早期其余）。
    """

    recent_turns: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="近期对话原文，每条含 user/assistant 字段",
    )
    mid_summary: List[str] = Field(
        default_factory=list,
        description="中期对话单句摘要列表，按时间顺序排列",
    )
    early_summary: str = Field(
        "",
        description="早期对话会话级摘要，整体压缩为一段",
    )
    full_context_text: str = Field(
        "",
        description="分层上下文拼装后的完整文本，供 prompt 直接使用",
    )


class IntentSwitchResult(BaseModel):
    """意图切换检测结果。

    `switched=True` 时 intent_node 会重置 slots 并以新意图继续识别；
    `reason` 用于日志与可解释性，便于排查误判。
    """

    switched: bool = Field(False, description="是否发生意图切换")
    new_intent: str = Field("", description="推断的新意图（仅作占位，最终以识别结果为准）")
    reason: str = Field("", description="切换原因，便于日志与排查")
    similarity: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="query 与历史意图的语义相似度，0-1",
    )
