"""坐席辅助端点。

补齐人机协同短板：转接发生后坐席侧的工作台 API 支撑。
- GET  /sessions/pending：待接入会话列表
- GET  /sessions/{session_id}：会话详情（含 EscalationCard + history）
- POST /sessions/{session_id}/accept：坐席接手
- POST /sessions/{session_id}/messages：坐席发消息
- POST /sessions/{session_id}/knowledge-recommend：知识推荐辅助
- POST /sessions/{session_id}/business-assist：业务查询辅助
- POST /sessions/{session_id}/resolve：标记已解决
- POST /sessions/{session_id}/solution：录入方案沉淀回库

设计要点：
- 复用 SessionManager / EscalationEngine / HybridRetriever / BusinessAgent / KnowledgeFeedback
- 鉴权统一通过 verify_api_key 依赖
- 端点实现见 Task 4-7
"""
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.agents.business_agent import get_business_agent
from app.agents.escalation import get_escalation_engine
from app.agents.knowledge_feedback import get_knowledge_feedback
from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.core.session import session_manager
from app.knowledge.hybrid_retriever import get_hybrid_retriever
from app.schemas.agent import (
    AcceptRequest,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentSessionDetail,
    AgentSessionSummary,
    BusinessAssistRequest,
    BusinessAssistResponse,
    KnowledgeChunk,
    KnowledgeRecommendRequest,
    KnowledgeRecommendResponse,
    ResolveRequest,
    ResolveResponse,
    SolutionSubmitRequest,
)
from app.schemas.business import BusinessResult
from app.schemas.escalation import (
    EscalationCard,
    EscalationPriority,
    HumanSolutionRecord,
)

logger = get_logger("app.api.v1.agent")

router = APIRouter(
    prefix="/api/v1/agent",
    tags=["坐席辅助"],
    dependencies=[Depends(verify_api_key)],
)


# =============================================================================
# Task 4: 待接入会话列表与会话详情
# =============================================================================


@router.get("/sessions/pending", response_model=List[AgentSessionSummary])
def list_pending_agent_sessions() -> List[AgentSessionSummary]:
    """列出所有待接入会话，按 EscalationPriority 降序。

    供坐席工作台首屏展示待接入队列，
    SessionManager.list_pending_sessions 已实现排序逻辑。
    """
    # list_pending_sessions 返回的字典 key 与 AgentSessionSummary 字段已对齐，
    # 无需额外字段映射，直接解包构造即可
    pending = session_manager.list_pending_sessions()
    return [AgentSessionSummary(**item) for item in pending]


@router.get("/sessions/{session_id}", response_model=AgentSessionDetail)
def get_agent_session(session_id: str) -> AgentSessionDetail:
    """查看会话详情，含 EscalationCard 与完整 history。

    若 escalation_card 缓存为空则即时调用 EscalationEngine.build_card 重建，
    并写回 session 缓存避免重复构建。
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    escalation_card_dict = session.get("escalation_card")
    escalation_card: Optional[EscalationCard] = None
    if escalation_card_dict:
        # 缓存命中：从 dict 重建 EscalationCard，避免重复构建卡片
        try:
            escalation_card = EscalationCard(**escalation_card_dict)
        except Exception as exc:
            logger.warning("缓存 EscalationCard 解析失败，将重建：%s", exc)
            escalation_card = None

    if escalation_card is None and session.get("agent_status") in (
        "pending",
        "assigned",
        "resolved",
    ):
        # 缓存缺失但有转接记录：即时重建并写回缓存，避免下次查询再重建
        try:
            engine = get_escalation_engine()
            reason = (escalation_card_dict or {}).get("escalate_reason", "用户转接")
            priority = (escalation_card_dict or {}).get(
                "priority", EscalationPriority.INFO
            )
            if isinstance(priority, str):
                priority = EscalationPriority(priority)
            escalation_card = engine.build_card(
                session_id, reason=reason, priority=priority
            )
            # 写回缓存避免重复构建
            session_manager.update_session(
                session_id, escalation_card=escalation_card.model_dump()
            )
        except Exception as exc:
            logger.warning("重建 EscalationCard 失败：%s", exc)

    return AgentSessionDetail(
        session_id=session_id,
        user_id=session.get("user_id"),
        channel=session.get("channel", ""),
        agent_status=session.get("agent_status"),
        assigned_agent_id=session.get("assigned_agent_id"),
        turn_count=session.get("turn_count", 0),
        emotion_score=session.get("emotion_score"),
        escalation_card=escalation_card,
        history=session.get("history", []),
        created_at=session.get("created_at", ""),
    )


# =============================================================================
# Task 5: 坐席接手与发消息
# =============================================================================


@router.post("/sessions/{session_id}/accept", response_model=AgentSessionDetail)
def accept_agent_session(
    session_id: str,
    request: AcceptRequest = AcceptRequest(),
) -> AgentSessionDetail:
    """坐席接手会话，CAS 判断 pending → assigned。

    多坐席并发接手同一会话时只有一个成功，避免重复接手。
    已 assigned/resolved 返回 409。
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    # assign_agent 内部 CAS 保证并发接手只有一个成功
    success = session_manager.assign_agent(session_id, request.agent_id)
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Session already {session.get('agent_status')} by agent {session.get('assigned_agent_id')}",
        )
    logger.info("坐席接手成功：session=%s agent=%s", session_id, request.agent_id)
    # 复用详情端点逻辑返回更新后的会话
    return get_agent_session(session_id)


@router.post("/sessions/{session_id}/messages", response_model=AgentMessageResponse)
def send_agent_message(
    session_id: str,
    request: AgentMessageRequest,
) -> AgentMessageResponse:
    """坐席在原会话上下文中发消息，追加到 history。

    仅 assigned 状态允许发送，pending/resolved 返回 409，
    避免未接手就发送或已解决后继续发送导致状态错乱。
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    agent_status = session.get("agent_status")
    if agent_status != "assigned":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot send message in status '{agent_status}', must be 'assigned'",
        )

    session_manager.append_history(session_id, role="assistant", content=request.content)
    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info("坐席消息已追加：session=%s message_id=%s", session_id, message_id)
    return AgentMessageResponse(
        message_id=message_id, timestamp=timestamp, role="assistant"
    )


# =============================================================================
# Task 6: 知识推荐与业务辅助
# =============================================================================


@router.post(
    "/sessions/{session_id}/knowledge-recommend",
    response_model=KnowledgeRecommendResponse,
)
def recommend_knowledge(
    session_id: str,
    request: KnowledgeRecommendRequest,
) -> KnowledgeRecommendResponse:
    """知识推荐辅助，复用 HybridRetriever.retrieve。

    坐席接手后可输入查询快速获取相关知识片段，
    未命中时返回空 chunks 列表，不报错。
    """
    # 校验会话存在（即使未接手也允许知识查询，便于坐席接手前预览）
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    try:
        retriever = get_hybrid_retriever()
        chunks = retriever.retrieve(question=request.query, top_k=request.top_k)
    except Exception as exc:
        # 检索失败降级为空列表，保证坐席工作台不中断
        logger.warning("知识推荐检索失败，降级空列表：%s", exc)
        chunks = []

    return KnowledgeRecommendResponse(
        chunks=[
            KnowledgeChunk(
                content=c.text,
                score=c.score,
                source=c.source,
            )
            for c in chunks
        ],
        total=len(chunks),
    )


@router.post(
    "/sessions/{session_id}/business-assist", response_model=BusinessAssistResponse
)
def assist_business_query(
    session_id: str,
    request: BusinessAssistRequest,
) -> BusinessAssistResponse:
    """业务查询辅助，复用 BusinessAgent.execute（含脱敏）。

    业务异常不抛 5xx，降级为 result.error 字段，
    保证坐席工作台不被业务系统故障阻塞。
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    try:
        agent = get_business_agent()
        result: BusinessResult = agent.execute(
            query=request.query, session_id=session_id
        )
        # BusinessResult.data 已脱敏，从 data 中提取敏感字段名供前端标识
        masked_fields = [
            k
            for k in result.data.keys()
            if k.endswith("_masked")
            or "phone" in k.lower()
            or "id_card" in k.lower()
        ]
        return BusinessAssistResponse(
            result={
                "reply": result.reply,
                "data": result.data,
                "error": result.error,
                "need_confirmation": result.need_confirmation,
                "scene": str(result.scene) if result.scene else None,
            },
            masked_fields=masked_fields,
        )
    except Exception as exc:
        # 业务系统故障降级返回错误信息，不阻塞坐席工作台
        logger.warning("业务辅助查询失败，降级返回：%s", exc)
        return BusinessAssistResponse(
            result={"error": f"business_assist_failed: {exc}"},
            masked_fields=[],
        )


# =============================================================================
# Task 7: 标记已解决与方案沉淀
# =============================================================================


@router.post("/sessions/{session_id}/resolve", response_model=ResolveResponse)
def resolve_agent_session(
    session_id: str,
    request: ResolveRequest = ResolveRequest(),
) -> ResolveResponse:
    """标记会话已解决，CAS 判断 assigned → resolved。

    仅 assigned 状态可标记已解决，pending 返回 409，
    避免未接手直接关闭导致坐席遗漏处理。
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    # resolve_session 内部 CAS 保证仅 assigned 状态可流转到 resolved
    success = session_manager.resolve_session(session_id, request.note)
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resolve session in status '{session.get('agent_status')}', must be 'assigned'",
        )
    logger.info("会话已标记解决：session=%s note=%s", session_id, request.note)
    return ResolveResponse(
        session_id=session_id,
        agent_status="resolved",
        resolved_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/sessions/{session_id}/solution", response_model=HumanSolutionRecord)
def submit_agent_solution(
    session_id: str,
    request: SolutionSubmitRequest,
) -> HumanSolutionRecord:
    """录入人工方案，沉淀为 FAQ 候选。

    复用 KnowledgeFeedback.record_human_solution 入队，
    进入 pending 审核队列，审核通过后入库为 FAQ。
    question/solution 为空时由 FastAPI 自动返回 422（min_length=1 约束）。
    """
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    feedback = get_knowledge_feedback()
    record = feedback.record_human_solution(
        session_id=session_id,
        question=request.question,
        solution=request.solution,
        intent=request.intent,
    )
    logger.info(
        "坐席方案已录入：session=%s solution_id=%s intent=%s",
        session_id,
        record.solution_id,
        record.intent,
    )
    return record
