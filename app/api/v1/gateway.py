"""统一网关入口。

接收多渠道请求（web/app/wechat/dingtalk/api），
统一完成鉴权与会话管理，后续可按渠道做分发处理。
"""
from fastapi import APIRouter, Depends

from app.core.security import verify_api_key
from app.core.session import session_manager
from app.schemas.chat import ChatResponse, GatewayRequest

router = APIRouter(
    prefix="/api/v1/gateway",
    tags=["统一网关"],
    dependencies=[Depends(verify_api_key)],
)


@router.post("", response_model=ChatResponse)
def handle_gateway_request(request: GatewayRequest) -> ChatResponse:
    """处理多渠道接入请求。

    网关层职责：鉴权（通过依赖项完成）+ 会话管理 + 渠道分发。
    当前为骨架实现，后续接入 Agent 与 RAG 后替换占位响应。
    """
    # 复用已有会话或创建新会话，保证多轮对话上下文连续
    session_id = session_manager.get_or_create(
        session_id=request.session_id,
        channel=request.channel,
        user_id=request.user_id,
    )

    # 占位响应：后续替换为 Agent 协同处理结果
    return ChatResponse(
        session_id=session_id,
        reply=f"已接收 [{request.channel}] 渠道消息，功能开发中",
        status="ok",
        data={"channel": request.channel, "original_message": request.message},
    )
