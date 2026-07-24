"""对话相关数据模型。

定义多渠道请求、对话请求与响应的统一结构，
作为网关与对话端点的数据契约。
"""

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.knowledge import RetrievedChunk


class GatewayRequest(BaseModel):
    """统一网关请求模型。

    支持多渠道接入，通过 channel 字段区分来源，
    便于后续按渠道做差异化处理（如微信需被动回复）。
    """

    channel: str = Field(
        ...,
        description="接入渠道：web/app/wechat/dingtalk/api",
        pattern="^(web|app|wechat|dingtalk|api)$",
    )
    message: str = Field(..., description="用户消息内容")
    session_id: str | None = Field(None, description="会话 ID，首次对话可不传")
    user_id: str | None = Field(None, description="用户标识")
    metadata: dict[str, Any] | None = Field(
        default_factory=dict,
        description="附加元数据，用于渠道扩展字段",
    )


class ChatRequest(BaseModel):
    """对话端点请求模型。"""

    # 限制消息长度，防止超长 prompt 攻击占用 LLM 资源
    message: str = Field(..., max_length=2000, description="用户消息内容")
    session_id: str | None = Field(None, description="会话 ID")
    # 渠道白名单与 GatewayRequest 保持一致，避免非法渠道绕过网关
    channel: str = Field(
        "api",
        description="接入渠道",
        pattern="^(web|app|wechat|dingtalk|api)$",
    )
    user_id: str | None = Field(None, description="用户标识")


class ChatResponse(BaseModel):
    """对话端点响应模型。"""

    session_id: str = Field(..., description="会话 ID")
    reply: str = Field(..., description="客服回复内容")
    status: str = Field("ok", description="处理状态")
    data: dict[str, Any] | None = Field(
        default_factory=dict,
        description="扩展数据，后续承接 RAG 引用来源等",
    )


class HealthResponse(BaseModel):
    """健康检查响应模型。"""

    status: str
    app: str
    version: str
    timestamp: str
    # 是否运行在不安全模式（API_KEY 未配置），便于运维监控识别风险
    insecure_mode: bool = Field(
        default=False,
        description="是否处于不安全模式（未配置 API_KEY）",
    )


class RAGAnswer(BaseModel):
    """RAG 单 Agent 问答结果。

    hit=False 表示知识库未命中，调用方据此决定兜底策略；
    sources 与 retrieved_chunks 用于在 UI 上展示引用来源，
    confidence 反映回答的可信度（综合检索得分与是否命中）。
    """

    answer: str = Field(..., description="最终给用户的回答文本")
    sources: list[str] = Field(
        default_factory=list,
        description="来源列表，格式如 '产品FAQ.md 第3页'",
    )
    retrieved_chunks: list[RetrievedChunk] = Field(
        default_factory=list,
        description="命中的知识片段，便于前端展示与调试",
    )
    confidence: float = Field(0.0, description="回答置信度，0-1 之间")
    hit: bool = Field(False, description="是否检索到相关知识")
