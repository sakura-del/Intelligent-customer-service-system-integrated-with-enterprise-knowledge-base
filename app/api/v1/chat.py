"""对话端点：多 Agent 协同的智能客服问答。

提供两套对话接口：
- POST /api/v1/chat：同步返回完整 ChatResponse
- POST /api/v1/chat/stream：SSE 流式返回 meta/token/done 事件

两套端点共用 SessionManager 与 OrchestratorAgent，
仅最终生成阶段是否流式有所差异，保持多 Agent 协同逻辑一致。
"""
import json
from typing import Any, Dict, Generator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.agents.graph import run_graph
from app.agents.knowledge_agent import get_knowledge_agent
from app.agents.orchestrator import (
    ESCALATE_FAILED_THRESHOLD,
    ESCALATE_REPLY,
    get_orchestrator,
)
from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.core.session import session_manager
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.orchestrator import Intent

logger = get_logger("app.api.v1.chat")

router = APIRouter(
    prefix="/api/v1/chat",
    tags=["对话"],
    dependencies=[Depends(verify_api_key)],
)


@router.post("", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """对话端点：鉴权 + 会话 + 多 Agent 协同。

    流程：
    1. 鉴权由依赖项完成
    2. run_graph 内部完成会话复用/创建、轮次计数与历史维护
    3. LangGraph 编排：意图识别 → 路由分发 → Agent 执行 → DialogAgent 润色
    4. 把 AgentState 展平到 ChatResponse.data，含 intent/sources/escalate 等
    5. 转人工时透出 escalation_card，供前端展示给人工客服
    """
    # 多 Agent 编排：内部会完成 session 复用、turn 自增、history 追加
    state = run_graph(message=request.message, session_id=request.session_id)

    session_id = state.get("session_id") or ""

    return ChatResponse(
        session_id=session_id,
        reply=state.get("final_reply", ""),
        status="ok",
        data={
            "intent": state.get("intent"),
            "sources": state.get("sources", []),
            "escalate_to_human": state.get("escalate_to_human", False),
            # 转接时为人工客服提供上下文卡片，未转接时为 None
            "escalation_card": state.get("escalation_card"),
            "turn_count": state.get("turn_count", 0),
            "failed_attempts": state.get("failed_attempts", 0),
            "emotion_score": state.get("emotion_score"),
            "sub_tasks": state.get("sub_tasks", []),
        },
    )


@router.post("/stream")
def chat_stream(request: ChatRequest):
    """流式对话端点：返回 SSE 事件流。

    入参与 /chat 一致，返回 text/event-stream，
    事件类型：meta（intent/sources）→ token（多次）→ done（turn_count/escalate）。
    任一阶段异常 yield error 事件后关闭流，HTTP 状态保持 200（SSE 协议约定）。

    Accept 非 text/event-stream 时也返回流，兼容非标准 SSE 客户端。
    """
    return StreamingResponse(
        _stream_generator(request),
        media_type="text/event-stream",
        headers={
            # 关闭 nginx 缓冲，保证 token 实时下发
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _stream_generator(request: ChatRequest) -> Generator[bytes, None, None]:
    """SSE 事件生成器：编排意图识别 → 路由 → 流式生成。

    yield 格式：'event: <type>\\ndata: <json>\\n\\n'，符合 SSE 规范。
    异常时 yield error 事件后 return，保证流总能正常关闭。
    """
    try:
        yield from _run_stream_pipeline(request)
    except Exception as exc:
        # 兜底：未捕获异常也发 error 事件，避免前端无响应
        logger.exception("流式对话异常：%s", exc)
        yield _format_sse("error", {"message": f"内部错误：{exc}"})


def _run_stream_pipeline(request: ChatRequest) -> Generator[bytes, None, None]:
    """执行流式编排主流程：会话 → 意图识别 → 路由 → 流式生成。

    拆分出来便于异常统一处理：本函数内任何异常都会被外层捕获并转 error 事件。
    """
    # 1. 会话管理：复用或创建 session，记录用户消息
    session_id = session_manager.get_or_create(
        session_id=request.session_id, channel=request.channel, user_id=request.user_id
    )
    session_manager.increment_turn(session_id)
    session_manager.append_history(session_id, "user", request.message)
    session = session_manager.get_session(session_id) or {}
    turn_count = int(session.get("turn_count", 1))
    failed_attempts = int(session.get("failed_attempts", 0))

    # 2. 意图识别 + 情绪评分（复用 OrchestratorAgent 的能力）
    orchestrator = get_orchestrator()
    message = request.message
    intent_result = orchestrator._recognize_intent(message)
    emotion_score = orchestrator._estimate_emotion_score(message)
    if orchestrator._should_prioritize_emotion(intent_result, emotion_score):
        intent_result = orchestrator._override_to_emotion(
            intent_result, message, emotion_score
        )

    intent_value = intent_result.intent.value
    logger.info(
        "流式对话意图：session=%s intent=%s confidence=%.2f",
        session_id,
        intent_value,
        intent_result.confidence,
    )

    # 3. 路由：判断是否转人工
    escalate = _should_escalate(message, intent_result, failed_attempts)

    if escalate:
        # 转人工：发 meta + done（escalate=true），不走 LLM
        session_manager.update_session(
            session_id,
            current_intent=intent_value,
            failed_attempts=failed_attempts + 1,
            emotion_score=max(0.0, min(1.0, 1.0 - emotion_score / 5.0)),
        )
        session_manager.append_history(
            session_id, "assistant", ESCALATE_REPLY
        )
        yield _format_sse(
            "meta",
            {"intent": intent_value, "sources": [], "escalate": True},
        )
        yield _format_sse(
            "done",
            {"turn_count": turn_count, "escalate": True, "answer": ESCALATE_REPLY},
        )
        return

    # 4. 按意图分流：knowledge_qa 走流式生成，其他意图非流式收集后放 meta
    if intent_value == Intent.KNOWLEDGE_QA.value:
        yield from _stream_knowledge_qa(
            message=message,
            session_id=session_id,
            intent_value=intent_value,
            turn_count=turn_count,
            failed_attempts=failed_attempts,
            emotion_score=emotion_score,
        )
        return

    # 非知识问答意图：非流式收集完整结果，作为单 token 放入流中
    yield from _stream_non_knowledge(
        message=message,
        session_id=session_id,
        intent_result=intent_result,
        turn_count=turn_count,
        failed_attempts=failed_attempts,
        emotion_score=emotion_score,
    )


def _should_escalate(
    message: str, intent_result: Any, failed_attempts: int
) -> bool:
    """判断是否需要转人工。

    复用 OrchestratorAgent 的转接判断逻辑，避免与 graph 路径行为分裂：
    - 用户主动要求转人工 → 直接转
    - 情绪敏感意图 → 直接转
    - 连续失败达阈值 → 直接转
    """
    # 用户主动要求转人工：复用 graph._is_user_request_human 关键词规则
    from app.agents.graph import _is_user_request_human

    if _is_user_request_human(message):
        return True
    if intent_result.intent == Intent.EMOTION_SENSITIVE:
        return True
    if failed_attempts >= ESCALATE_FAILED_THRESHOLD:
        return True
    return False


def _stream_knowledge_qa(
    message: str,
    session_id: str,
    intent_value: str,
    turn_count: int,
    failed_attempts: int,
    emotion_score: int,
) -> Generator[bytes, None, None]:
    """知识问答意图：调用 KnowledgeAgent.handle_stream 流式生成。

    先发 meta（含 intent），再透传 RAG 的事件流，最后发 done。
    RAG 已命中时 meta 含 sources；未命中时 RAG 直接发 done 不走 LLM。
    """
    knowledge_agent = get_knowledge_agent()
    sources: list = []
    answer_parts: list = []

    # 1. 流式生成：透传 KnowledgeAgent 事件
    for event in knowledge_agent.handle_stream(query=message, session_id=session_id):
        event_type = event.get("type")
        if event_type == "meta":
            sources = list(event.get("sources", []))
            # 发送 meta 事件给客户端，含 intent 与 sources
            yield _format_sse(
                "meta",
                {"intent": intent_value, "sources": sources},
            )
        elif event_type == "token":
            content = event.get("content", "")
            if content:
                answer_parts.append(content)
                yield _format_sse("token", {"content": content})
        elif event_type == "error":
            yield _format_sse("error", {"message": event.get("message", "")})
        elif event_type == "done":
            # RAG 未命中时直接发 done（无 meta/token），此时需补发 meta
            answer = event.get("answer", "")
            if not sources and not answer_parts:
                # 未命中：补发 meta 让前端知道意图
                yield _format_sse(
                    "meta", {"intent": intent_value, "sources": []}
                )
            final_answer = answer or "".join(answer_parts)
            # 同步会话状态：解决则清零失败计数，否则累加
            is_resolved = bool(final_answer) and "未找到相关内容" not in final_answer
            new_failed = 0 if is_resolved else failed_attempts + 1
            session_manager.update_session(
                session_id,
                current_intent=intent_value,
                failed_attempts=new_failed,
                emotion_score=max(0.0, min(1.0, 1.0 - emotion_score / 5.0)),
            )
            session_manager.append_history(session_id, "assistant", final_answer)
            yield _format_sse(
                "done",
                {
                    "turn_count": turn_count,
                    "escalate": False,
                    "answer": final_answer,
                },
            )
            return


def _stream_non_knowledge(
    message: str,
    session_id: str,
    intent_result: Any,
    turn_count: int,
    failed_attempts: int,
    emotion_score: int,
) -> Generator[bytes, None, None]:
    """非知识问答意图：非流式收集完整结果，作为 meta 后单 token 输出。

    business_query / emotion_sensitive / ticket / chitchat 等意图
    用 OrchestratorAgent 的 handler 同步生成完整回复，
    再以单 token 形式放入流，保持 SSE 协议一致性。
    """
    orchestrator = get_orchestrator()
    intent_value = intent_result.intent.value

    # 1. 同步执行子任务，收集完整回复
    sub_tasks = orchestrator._ensure_sub_tasks(intent_result, message)
    orchestrator._dispatch_subtasks(sub_tasks)
    reply = orchestrator._aggregate_results(sub_tasks, intent_result)

    # 2. 判断是否解决，更新失败计数
    is_resolved = orchestrator._is_resolved(intent_result, sub_tasks)
    new_failed = 0 if is_resolved else failed_attempts + 1

    # 3. 发 meta（含完整回复作为 preview）+ token + done
    yield _format_sse(
        "meta",
        {
            "intent": intent_value,
            "sources": [],
            "answer": reply,
        },
    )
    # 把完整回复作为单 token 发出，让前端能复用 token 渲染逻辑
    yield _format_sse("token", {"content": reply})

    session_manager.update_session(
        session_id,
        current_intent=intent_value,
        failed_attempts=new_failed,
        emotion_score=max(0.0, min(1.0, 1.0 - emotion_score / 5.0)),
    )
    session_manager.append_history(session_id, "assistant", reply)

    yield _format_sse(
        "done",
        {"turn_count": turn_count, "escalate": False, "answer": reply},
    )


def _format_sse(event: str, data: Dict[str, Any]) -> bytes:
    """格式化 SSE 事件：'event: <type>\\ndata: <json>\\n\\n'。

    返回 bytes 避免 StreamingResponse 编码歧义；
    data 用 ensure_ascii=False 保留中文可读性。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
