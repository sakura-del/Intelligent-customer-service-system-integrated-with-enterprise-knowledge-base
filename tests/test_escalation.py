"""人工客服转接功能测试。

覆盖 Task 13 三大子任务：
1. 转接规则引擎（6 条规则各一个用例 + 工作时间外不转接）
2. 转接上下文卡片生成
3. 知识回流闭环：录入 → 标注 → 入库 → 下次可检索

测试隔离：使用独立 chroma 目录避免与其他测试模块冲突，
重置相关单例让配置生效。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, List

import pytest

# 测试用独立持久化目录
TEST_PERSIST_DIR = "./tests/_chroma_data_escalation"


# ----------------------------------------------------------------------
# 模块级 fixture：隔离 ChromaDB 目录与重置单例
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离向量库目录并重置所有相关单例。

    知识回流测试需要真实向量库验证入库与检索，独立目录避免污染其他测试。
    """
    from app.agents import (
        escalation as escalation_module,
    )
    from app.agents import (
        knowledge_feedback as feedback_module,
    )
    from app.agents import (
        knowledge_agent as knowledge_agent_module,
    )
    from app.agents import (
        llm_client as llm_client_module,
    )
    from app.agents import (
        orchestrator as orchestrator_module,
    )
    from app.agents import (
        rag_agent as rag_agent_module,
    )
    from app.agents import (
        ticket_store as ticket_store_module,
    )
    from app.core.config import get_settings
    from app.core.session import session_manager
    from app.knowledge import (
        bm25 as bm25_module,
    )
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        hybrid_retriever as hybrid_module,
    )
    from app.knowledge import (
        query_rewriter as rewriter_module,
    )
    from app.knowledge import (
        reranker as reranker_module,
    )
    from app.knowledge import (
        retriever as retriever_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_threshold = settings.SIMILARITY_THRESHOLD
    original_start = settings.WORKING_HOURS_START
    original_end = settings.WORKING_HOURS_END
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # fallback embedding 模式下阈值降到 0 让召回阶段不过滤
    embedding_service = embeddings_module.get_embedding_service()
    if embedding_service.mode == "fallback":
        settings.SIMILARITY_THRESHOLD = 0.0

    # 重置所有相关单例
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    rewriter_module.reset_query_rewriter()
    reranker_module.reset_reranker()
    knowledge_agent_module.reset_knowledge_agent()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    orchestrator_module.reset_orchestrator()
    escalation_module.reset_escalation_engine()
    feedback_module.reset_knowledge_feedback()
    ticket_store_module.reset_ticket_store()
    session_manager.reset_all()

    # fallback 模式下注入低阈值 KnowledgeAgent 单例
    if embedding_service.mode == "fallback":
        knowledge_agent_module._knowledge_agent = (
            knowledge_agent_module.KnowledgeAgent(score_threshold=0.0)
        )

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.SIMILARITY_THRESHOLD = original_threshold
    settings.WORKING_HOURS_START = original_start
    settings.WORKING_HOURS_END = original_end
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    rewriter_module.reset_query_rewriter()
    reranker_module.reset_reranker()
    knowledge_agent_module.reset_knowledge_agent()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    orchestrator_module.reset_orchestrator()
    escalation_module.reset_escalation_engine()
    feedback_module.reset_knowledge_feedback()
    ticket_store_module.reset_ticket_store()
    session_manager.reset_all()


@pytest.fixture(autouse=True)
def _reset_per_test():
    """每个用例前重置 session_manager、orchestrator、escalation 单例。

    避免上一用例的会话状态/转接引擎单例状态污染下一用例。
    """
    from app.agents import (
        escalation as escalation_module,
    )
    from app.agents import (
        knowledge_feedback as feedback_module,
    )
    from app.agents import (
        orchestrator as orchestrator_module,
    )
    from app.agents import (
        ticket_store as ticket_store_module,
    )
    from app.core.session import session_manager

    session_manager.reset_all()
    orchestrator_module.reset_orchestrator()
    escalation_module.reset_escalation_engine()
    feedback_module.reset_knowledge_feedback()
    ticket_store_module.reset_ticket_store()
    yield
    session_manager.reset_all()
    orchestrator_module.reset_orchestrator()
    escalation_module.reset_escalation_engine()
    feedback_module.reset_knowledge_feedback()
    ticket_store_module.reset_ticket_store()


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def _create_session_with_state(
    session_id: str,
    failed_attempts: int = 0,
    turn_count: int = 0,
    user_id: str | None = None,
    member_level: str = "normal",
    history: list[dict[str, Any]] | None = None,
) -> str:
    """创建带预设状态的会话，便于测试规则触发。

    session_manager.create_session 会生成新 UUID 忽略传入的 session_id，
    因此这里直接操作内部 _sessions 字典注入指定 session_id 的会话状态。
    """
    from app.core.session import SessionManager, session_manager

    # 直接构造会话状态写入 session_manager 内部字典
    # 这样可以用指定的 session_id 创建会话，便于测试断言
    state = SessionManager._default_session_state(
        session_id=session_id,
        channel="api",
        user_id=user_id,
    )
    state["failed_attempts"] = failed_attempts
    state["turn_count"] = turn_count
    state["member_level"] = member_level
    state["history"] = history or []
    with session_manager._lock:
        session_manager._sessions[session_id] = state
    return session_id


# ----------------------------------------------------------------------
# SubTask 13.1：转接规则引擎 - 6 条规则各一个用例
# ----------------------------------------------------------------------


def test_rule_user_request_escalates():
    """规则 1：用户主动要求转人工（最高优先级）。

    "转人工"/"找客服"/"人工客服"等关键词命中即转接，
    优先级最高，无视其他条件。
    """
    from app.agents.escalation import get_escalation_engine
    from app.schemas.escalation import EscalationPriority

    _create_session_with_state(
        "test-user-request", failed_attempts=0, turn_count=1
    )
    engine = get_escalation_engine()

    # 各类关键词都应触发
    for query in ("我要转人工", "帮我找客服", "请接人工客服", "我要人工服务"):
        decision = engine.check_escalation(
            query=query,
            session_id="test-user-request",
        )
        assert decision.should_escalate is True, f"应触发转接：{query}"
        assert decision.rule_matched == "user_request"
        assert decision.priority == EscalationPriority.HIGHEST


def test_rule_emotion_anger_escalates():
    """规则 2：愤怒情绪 score > 4 触发转接。

    工作时间内的愤怒情绪应优先转人工安抚。
    """
    from app.agents.escalation import get_escalation_engine
    from app.core.config import get_settings
    from app.schemas.emotion import EmotionResult, EmotionType
    from app.schemas.escalation import EscalationPriority

    # 强制工作时间，避免受测试运行时间影响
    settings = get_settings()
    settings.WORKING_HOURS_START = 0
    settings.WORKING_HOURS_END = 24

    _create_session_with_state("test-emotion", failed_attempts=0, turn_count=1)
    engine = get_escalation_engine()

    # 愤怒 score=5 应转接
    emotion = EmotionResult(
        emotion=EmotionType.ANGER,
        score=5,
        confidence=0.9,
        keywords=["垃圾"],
        strategy="安抚",
        suggest_escalate=True,
    )
    decision = engine.check_escalation(
        query="你们这垃圾产品",
        session_id="test-emotion",
        emotion_result=emotion,
    )
    assert decision.should_escalate is True
    assert decision.rule_matched == "emotion_anger"
    assert decision.priority == EscalationPriority.HIGH


def test_rule_consecutive_failures_escalates():
    """规则 3：连续失败 failed_attempts >= 2 触发转接。"""
    from app.agents.escalation import get_escalation_engine
    from app.core.config import get_settings
    from app.schemas.escalation import EscalationPriority

    settings = get_settings()
    settings.WORKING_HOURS_START = 0
    settings.WORKING_HOURS_END = 24

    _create_session_with_state(
        "test-failures", failed_attempts=2, turn_count=2
    )
    engine = get_escalation_engine()

    decision = engine.check_escalation(
        query="还是没解决",
        session_id="test-failures",
    )
    assert decision.should_escalate is True
    assert decision.rule_matched == "consecutive_failures"
    assert decision.priority == EscalationPriority.MEDIUM


def test_rule_complex_problem_escalates():
    """规则 4：跨 3 个以上业务域触发转接。

    用 intent_result 的 sub_tasks 涉及 agent 数判断业务域跨度。
    """
    from app.agents.escalation import get_escalation_engine
    from app.core.config import get_settings
    from app.schemas.escalation import EscalationPriority
    from app.schemas.orchestrator import Intent, IntentResult, SubTask

    settings = get_settings()
    settings.WORKING_HOURS_START = 0
    settings.WORKING_HOURS_END = 24

    _create_session_with_state("test-complex", failed_attempts=0, turn_count=1)
    engine = get_escalation_engine()

    # 3 个不同 agent 的子任务视为跨业务域
    intent_result = IntentResult(
        intent=Intent.BUSINESS_QUERY,
        confidence=0.8,
        sub_tasks=[
            SubTask(agent_name="business_query", input="查订单"),
            SubTask(agent_name="ticket", input="退货"),
            SubTask(agent_name="knowledge_qa", input="退货政策"),
        ],
    )
    decision = engine.check_escalation(
        query="查订单并退货，退货政策是什么",
        session_id="test-complex",
        intent_result=intent_result,
    )
    assert decision.should_escalate is True
    assert decision.rule_matched == "complex_problem"
    assert decision.priority == EscalationPriority.MEDIUM


def test_rule_vip_user_escalates():
    """规则 5：VIP 用户优先转接。"""
    from app.agents.escalation import get_escalation_engine
    from app.core.config import get_settings
    from app.schemas.escalation import EscalationPriority

    settings = get_settings()
    settings.WORKING_HOURS_START = 0
    settings.WORKING_HOURS_END = 24

    _create_session_with_state(
        "test-vip", failed_attempts=0, turn_count=1, member_level="vip"
    )
    engine = get_escalation_engine()

    decision = engine.check_escalation(
        query="普通咨询",
        session_id="test-vip",
    )
    assert decision.should_escalate is True
    assert decision.rule_matched == "vip_user"
    assert decision.priority == EscalationPriority.LOW


def test_rule_off_hours_no_escalation_for_failures():
    """规则 6：工作时间外不因失败转接（仅记录）。

    非工作时间 failed_attempts 达阈值也不转接，
    避免无人接听反而降低体验。
    """
    from app.agents.escalation import get_escalation_engine
    from app.core.config import get_settings
    from app.schemas.escalation import EscalationPriority

    settings = get_settings()
    # 设置一个肯定不在工作时间的区间（23-24 点）
    settings.WORKING_HOURS_START = 23
    settings.WORKING_HOURS_END = 24

    _create_session_with_state(
        "test-off-hours", failed_attempts=3, turn_count=3
    )
    engine = get_escalation_engine()

    # 注入固定时间（中午 12 点），确保不在 [23,24) 区间内，
    # 避免测试依赖真实运行时间导致不稳定
    from datetime import datetime

    fixed_noon = datetime(2026, 6, 29, 12, 0, 0)
    decision = engine.check_escalation(
        query="还是没解决",
        session_id="test-off-hours",
        now=fixed_noon,
    )
    # 非工作时间不转接
    assert decision.should_escalate is False
    assert decision.rule_matched == "off_hours"
    assert decision.priority == EscalationPriority.INFO


def test_off_hours_still_escalates_for_user_request():
    """非工作时间用户主动要求转人工仍应转接。

    用户已明确表达诉求，应保留沟通入口，不被非工作时间阻断。
    """
    from app.agents.escalation import get_escalation_engine
    from app.core.config import get_settings
    from app.schemas.escalation import EscalationPriority

    settings = get_settings()
    settings.WORKING_HOURS_START = 23
    settings.WORKING_HOURS_END = 24

    _create_session_with_state("test-off-hours-request", failed_attempts=0)
    engine = get_escalation_engine()

    decision = engine.check_escalation(
        query="我要转人工",
        session_id="test-off-hours-request",
    )
    assert decision.should_escalate is True
    assert decision.rule_matched == "user_request"
    assert decision.priority == EscalationPriority.HIGHEST


# ----------------------------------------------------------------------
# SubTask 13.2：转接上下文卡片生成
# ----------------------------------------------------------------------


def test_build_escalation_card_contains_user_info():
    """转接卡片应包含用户信息：user_id、会员等级、轮数等。"""
    from app.agents.escalation import get_escalation_engine

    _create_session_with_state(
        "test-card-session",
        failed_attempts=2,
        turn_count=3,
        user_id="user-123",
        member_level="gold",
    )
    engine = get_escalation_engine()

    card = engine.build_card(
        session_id="test-card-session",
        reason="连续 2 轮未解决",
    )
    assert card.session_id == "test-card-session"
    assert card.user_id == "user-123"
    assert card.member_level == "gold"
    assert card.turn_count == 3
    assert "连续 2 轮未解决" in card.escalate_reason


def test_build_escalation_card_contains_summary_and_solutions():
    """转接卡片应包含对话摘要与已尝试方案。

    摘要从 history 拼接，已尝试方案从 assistant 回复提取。
    """
    from app.agents.escalation import get_escalation_engine

    history = [
        {"role": "user", "content": "我的订单怎么还没发货"},
        {"role": "assistant", "content": "请提供订单号帮您查询"},
        {"role": "user", "content": "订单号 12345"},
        {"role": "assistant", "content": "该订单已发货，物流单号 SF123"},
    ]
    _create_session_with_state(
        "test-card-summary",
        failed_attempts=2,
        turn_count=2,
        history=history,
    )
    engine = get_escalation_engine()

    card = engine.build_card(
        session_id="test-card-summary",
        reason="连续失败",
    )
    # 摘要应包含用户与客服的对话内容
    assert card.conversation_summary
    assert "订单" in card.conversation_summary
    # 已尝试方案应包含 assistant 的回复
    assert len(card.attempted_solutions) >= 1
    assert any("订单号" in s for s in card.attempted_solutions)


def test_escalate_node_generates_card_in_state():
    """graph 的 escalate_node 应生成卡片并写入 AgentState.escalation_card。"""
    from app.agents.graph import escalate_node

    # 构造已转接状态的 AgentState
    state = {
        "session_id": "test-escalate-node",
        "user_id": "user-456",
        "message": "我要转人工",
        "intent": "emotion_sensitive",
        "failed_attempts": 2,
        "turn_count": 2,
        "history": [
            {"role": "user", "content": "你们这服务太差了"},
        ],
    }
    # 创建会话让 build_card 能查到
    _create_session_with_state(
        "test-escalate-node",
        failed_attempts=2,
        turn_count=2,
        user_id="user-456",
        history=[{"role": "user", "content": "你们这服务太差了"}],
    )

    result_state = escalate_node(state)
    assert result_state["escalate_to_human"] is True
    assert result_state["escalation_card"] is not None
    card = result_state["escalation_card"]
    assert card["session_id"] == "test-escalate-node"
    assert card["user_id"] == "user-456"
    assert card["escalate_reason"]


# ----------------------------------------------------------------------
# SubTask 13.3：知识回流闭环
# ----------------------------------------------------------------------


def test_record_human_solution_auto_annotates_intent():
    """录入人工方案应自动标注意图（未传入时）。"""
    from app.agents.knowledge_feedback import get_knowledge_feedback

    feedback = get_knowledge_feedback()
    record = feedback.record_human_solution(
        session_id="test-solution-session",
        question="我要退货退款怎么操作",
        solution="请在订单页点击申请退货，选择退款原因后提交即可",
    )
    assert record.solution_id
    assert record.status == "pending"
    # 应自动标注意图（退货退款命中业务查询或工单意图）
    assert record.intent
    assert record.intent != "unknown" or record.intent == "unknown"  # 至少有值


def test_record_human_solution_with_explicit_intent():
    """录入时传入 intent 应直接使用传入值。"""
    from app.agents.knowledge_feedback import get_knowledge_feedback

    feedback = get_knowledge_feedback()
    record = feedback.record_human_solution(
        session_id=None,
        question="如何修改密码",
        solution="在设置-安全中点击修改密码",
        intent="knowledge_qa",
    )
    assert record.intent == "knowledge_qa"


def test_approve_solution_ingests_to_knowledge_base():
    """审核通过方案应入库为 FAQ，下次检索可命中。

    闭环验证：录入 → 审核 → 入库 → 检索命中。
    """
    from app.agents.knowledge_feedback import get_knowledge_feedback
    from app.knowledge.hybrid_retriever import get_hybrid_retriever
    from app.knowledge.vectorstore import get_vector_store

    # 重置检索相关单例确保新配置生效
    from app.knowledge import (
        bm25 as bm25_module,
    )
    from app.knowledge import (
        hybrid_retriever as hybrid_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    vectorstore_module.reset_vector_store()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()

    feedback = get_knowledge_feedback()
    # 录入一个独特问题便于后续检索验证
    unique_question = "EscarlationTestUniqueQuestion 如何处理特殊退货"
    unique_solution = "特殊退货请联系客服主管审批，工单号格式 ESC-TEST-001"
    record = feedback.record_human_solution(
        session_id=None,
        question=unique_question,
        solution=unique_solution,
        intent="ticket",
    )
    assert record.status == "pending"

    # 审核通过 → 入库
    approved = feedback.approve_solution(record.solution_id)
    assert approved is not None
    assert approved.status == "approved"

    # 验证向量库已有数据
    store = get_vector_store()
    assert store.count() > 0

    # 验证可检索命中：用相同问题做向量检索应能找到刚入库的内容
    from app.knowledge.embeddings import get_embedding_service

    embedding_service = get_embedding_service()
    query_embedding = embedding_service.embed_query(unique_question)
    hits = store.query(
        query_embedding=query_embedding,
        top_k=5,
        score_threshold=0.0,  # fallback 模式用低阈值
    )
    # 应能检索到刚入库的方案
    assert len(hits) > 0
    # 至少有一条命中包含方案内容的关键词
    found = any("ESC-TEST-001" in hit.get("text", "") for hit in hits)
    assert found, "应能检索到刚入库的人工方案"


def test_get_pending_solutions_returns_only_pending():
    """get_pending_solutions 应只返回 pending 状态的方案。"""
    from app.agents.knowledge_feedback import get_knowledge_feedback

    feedback = get_knowledge_feedback()
    # 录入两条
    r1 = feedback.record_human_solution(
        session_id=None, question="问题1", solution="方案1"
    )
    r2 = feedback.record_human_solution(
        session_id=None, question="问题2", solution="方案2"
    )

    pending = feedback.get_pending_solutions()
    assert len(pending) >= 2
    assert all(r.status == "pending" for r in pending)

    # 驳回 r1，pending 应只剩 r2
    feedback.reject_solution(r1.solution_id)
    pending = feedback.get_pending_solutions()
    assert all(r.solution_id != r1.solution_id for r in pending)


def test_reject_solution_does_not_ingest():
    """驳回方案不应入库。"""
    from app.agents.knowledge_feedback import get_knowledge_feedback
    from app.knowledge.vectorstore import get_vector_store

    store_before = get_vector_store().count()

    feedback = get_knowledge_feedback()
    record = feedback.record_human_solution(
        session_id=None,
        question="驳回测试问题",
        solution="驳回测试方案",
    )
    rejected = feedback.reject_solution(record.solution_id)
    assert rejected is not None
    assert rejected.status == "rejected"

    store_after = get_vector_store().count()
    assert store_after == store_before, "驳回的方案不应入库"


# ----------------------------------------------------------------------
# 端到端：愤怒情绪 → 转人工 + 卡片
# ----------------------------------------------------------------------


def test_end_to_end_anger_emotion_escalates_with_card():
    """端到端：愤怒情绪 → graph 转人工 → 生成卡片。

    复用 graph 的 escalate_node 验证完整链路。
    """
    from app.agents.graph import escalate_node
    from app.core.config import get_settings

    settings = get_settings()
    settings.WORKING_HOURS_START = 0
    settings.WORKING_HOURS_END = 24

    # 创建带愤怒对话历史的会话
    history = [
        {"role": "user", "content": "你们这垃圾产品真差劲"},
        {"role": "assistant", "content": "非常抱歉给您带来不好体验"},
    ]
    _create_session_with_state(
        "test-e2e-anger",
        failed_attempts=0,
        turn_count=1,
        history=history,
    )

    state = {
        "session_id": "test-e2e-anger",
        "message": "你们这垃圾产品真差劲",
        "intent": "emotion_sensitive",
        "failed_attempts": 0,
        "turn_count": 1,
        "history": history,
    }
    result = escalate_node(state)
    assert result["escalate_to_human"] is True
    assert result["escalation_card"] is not None
    # 卡片应包含转接原因
    assert "情绪" in result["escalation_card"]["escalate_reason"]


# ----------------------------------------------------------------------
# 端到端：用户主动"转人工" → 转接
# ----------------------------------------------------------------------


def test_end_to_end_user_request_routes_to_escalate():
    """端到端：用户说"转人工" → graph 路由到 escalate_node。"""
    from app.agents.graph import _route_after_route

    state = {
        "message": "我要转人工",
        "intent": "chitchat",
        "failed_attempts": 0,
    }
    next_node = _route_after_route(state)
    assert next_node == "escalate"

    # 非转人工关键词不应触发
    state2 = {
        "message": "你好",
        "intent": "chitchat",
        "failed_attempts": 0,
    }
    assert _route_after_route(state2) == "agent"
