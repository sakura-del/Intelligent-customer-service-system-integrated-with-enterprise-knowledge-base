"""LangGraph 多 Agent 编排端到端测试。

覆盖 Task 8 的核心场景：
1. 知识问答 → 检索 → 润色端到端：返回命中回复与来源
2. 闲聊意图：路由到 chitchat agent
3. 情绪敏感意图：直接转人工
4. 连续失败转人工：业务查询占位"开发中"两轮后 escalate
5. 会话状态流转：turn_count / failed_attempts / history 正确累计

测试隔离：使用独立 chroma 目录，模块级 fixture 入库三份文档。
fallback embedding 模式下注入低阈值 KnowledgeAgent 单例，保证 rerank 后能命中。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# 测试用独立持久化目录，与其他测试模块隔离
TEST_PERSIST_DIR = "./tests/_chroma_data_graph"
SAMPLE_DIR = Path(__file__).parent / "sample_data"
SAMPLE_FAQ = SAMPLE_DIR / "faq.md"
SAMPLE_MANUAL = SAMPLE_DIR / "product_manual.md"
SAMPLE_POLICY = SAMPLE_DIR / "return_policy.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_ingest():
    """模块级 fixture：隔离 ChromaDB 目录并入库三份测试文档。

    重置所有相关单例让新配置生效；
    fallback embedding 模式下把 KnowledgeAgent 单例替换为低阈值版本，
    避免 rerank 后 cosine 分数低于 0.6 阈值导致误判未命中。
    """
    from app.agents import (
        dialog_agent as dialog_agent_module,
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
    from app.knowledge.pipeline import ingest_document

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_threshold = settings.SIMILARITY_THRESHOLD
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次测试残留
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # fallback 模式下 hash 向量无语义能力，阈值降到 0 让召回阶段不过滤
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
    # 显式注入 mock LLM 单例，保证 run_graph 走关键词规则路径，
    # 意图识别结果稳定可断言，不受真实 LLM_API_KEY 环境影响
    from app.agents.llm_client import LLMClient, _MockLLM

    mock_client = LLMClient()
    mock_client._mock = _MockLLM(reason="测试强制 mock")
    llm_client_module._llm_client = mock_client
    dialog_agent_module.reset_dialog_agent()
    orchestrator_module.reset_orchestrator()

    # 重置 graph 模块缓存与 session_manager 状态
    from app.agents import graph as graph_module

    graph_module.reset_graph()
    session_manager.reset_all()

    # 入库三份文档供 knowledge_qa 测试检索
    for sample_path, knowledge_type in [
        (SAMPLE_FAQ, "faq"),
        (SAMPLE_MANUAL, "doc"),
        (SAMPLE_POLICY, "policy"),
    ]:
        result = ingest_document(
            sample_path,
            metadata={"knowledge_type": knowledge_type},
        )
        assert result.error is None, f"入库 {sample_path.name} 失败：{result.error}"
        assert result.total_chunks > 0, f"{sample_path.name} 切分后无 chunk"

    # fallback 模式下注入低阈值 KnowledgeAgent 单例
    # 否则 rerank 后 cosine 分数 < 0.6 默认阈值，所有 chunk 被过滤为未命中
    if embedding_service.mode == "fallback":
        knowledge_agent_module._knowledge_agent = (
            knowledge_agent_module.KnowledgeAgent(score_threshold=0.0)
        )

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.SIMILARITY_THRESHOLD = original_threshold
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    rewriter_module.reset_query_rewriter()
    reranker_module.reset_reranker()
    knowledge_agent_module.reset_knowledge_agent()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    dialog_agent_module.reset_dialog_agent()
    orchestrator_module.reset_orchestrator()
    graph_module.reset_graph()
    session_manager.reset_all()


@pytest.fixture(autouse=True)
def _reset_session_per_test():
    """每个用例前重置 session_manager 与 OrchestratorAgent 状态。

    避免上一用例的 failed_attempts 累计污染下一用例的转人工判断。
    同时清理 HotQueryCache，避免热点缓存跨用例污染意图/回复断言。
    """
    from app.agents import orchestrator as orchestrator_module
    from app.core.performance import get_hot_query_cache
    from app.core.session import session_manager

    session_manager.reset_all()
    orchestrator_module.reset_orchestrator()
    # 清空热点缓存，避免上一用例写入的回复影响下一用例的编排走向
    try:
        get_hot_query_cache().invalidate()
    except Exception:
        pass
    yield
    session_manager.reset_all()
    orchestrator_module.reset_orchestrator()
    try:
        get_hot_query_cache().invalidate()
    except Exception:
        pass


# ----------------------------------------------------------------------
# 端到端：知识问答
# ----------------------------------------------------------------------


def test_knowledge_qa_end_to_end_returns_polished_reply():
    """知识问答应经过 检索 → 润色 后返回含来源的友好回复。

    断言：
    - 意图为 knowledge_qa
    - final_reply 非空且经过润色（含合规开头称呼）
    - sources 非空（命中知识库）
    - 未触发转人工
    """
    from app.agents.graph import run_graph

    state = run_graph("忘记登录密码怎么办？")

    assert state["intent"] == "knowledge_qa"
    assert state["final_reply"], "润色后回复不应为空"
    # 润色后应含合规开头（DialogAgent 规则拼装的默认开头）
    assert "您好" in state["final_reply"] or "亲" in state["final_reply"]
    # 命中知识库应返回来源
    assert state["sources"], "知识问答命中时应返回来源列表"
    assert state["escalate_to_human"] is False
    assert state["turn_count"] >= 1


def test_knowledge_qa_reply_not_placeholder():
    """知识问答回复不应是占位文案或未命中标记。"""
    from app.agents.graph import run_graph

    state = run_graph("忘记登录密码怎么办？")
    reply = state["final_reply"]
    assert "开发中" not in reply
    assert "[未命中知识库]" not in reply


# ----------------------------------------------------------------------
# 端到端：闲聊
# ----------------------------------------------------------------------


def test_chitchat_routes_to_chitchat_agent():
    """闲聊意图应路由到 chitchat agent 并返回友好回应。"""
    from app.agents.graph import run_graph

    state = run_graph("你好")

    assert state["intent"] == "chitchat"
    assert state["final_reply"]
    # chitchat 润色后应含问候语
    assert "您好" in state["final_reply"] or "高兴" in state["final_reply"]
    assert state["escalate_to_human"] is False
    # 闲聊无来源
    assert state["sources"] == []


# ----------------------------------------------------------------------
# 端到端：情绪敏感 → 转人工
# ----------------------------------------------------------------------


def test_emotion_sensitive_escalates_to_human():
    """含脏话应识别为情绪敏感意图并直接转人工。

    路由策略：情绪敏感意图直接走 escalate_node，避免激化矛盾。
    """
    from app.agents.graph import run_graph

    state = run_graph("你们这垃圾产品真差劲")

    assert state["intent"] == "emotion_sensitive"
    assert state["escalate_to_human"] is True
    # 转人工话术应包含"转接人工"或"人工客服"
    assert "转接人工" in state["final_reply"] or "人工客服" in state["final_reply"]


# ----------------------------------------------------------------------
# 端到端：连续失败 → 转人工
# ----------------------------------------------------------------------


def test_consecutive_failures_escalate_to_human():
    """连续 2 轮未解决应自动转人工。

    业务查询（business_query）当前为占位实现返回"开发中"，
    视为未解决，failed_attempts 累计达阈值后触发 escalate。
    """
    from app.agents.graph import run_graph

    # 用占位 session_id，run_graph 内部会创建实际 session
    # 通过返回的 state["session_id"] 在第二轮复用
    state1 = run_graph("查我的订单物流状态", session_id="test-graph-escalate")
    session_id = state1["session_id"]
    assert state1["intent"] == "business_query"
    assert state1["failed_attempts"] >= 1
    assert state1["escalate_to_human"] is False

    # 第二轮：用第一轮返回的 session_id 复用会话，仍走业务查询占位
    state2 = run_graph("还是查不到订单物流", session_id=session_id)
    assert state2["failed_attempts"] >= 2
    assert state2["escalate_to_human"] is True
    assert "转接人工" in state2["final_reply"] or "人工客服" in state2["final_reply"]


def test_resolved_resets_failed_attempts():
    """解决后应清零失败计数，避免历史失败累计误触发转人工。

    第一轮业务查询未解决（failed=1），第二轮切换为知识问答命中（解决），
    验证 failed_attempts 清零。
    """
    from app.agents.graph import run_graph

    # 第一轮：业务查询占位，未解决
    state1 = run_graph("查我的订单物流", session_id="test-graph-reset")
    session_id = state1["session_id"]
    assert state1["failed_attempts"] >= 1
    assert state1["escalate_to_human"] is False

    # 第二轮：知识问答命中，视为解决，failed_attempts 应清零
    state2 = run_graph("忘记登录密码怎么办？", session_id=session_id)
    assert state2["intent"] == "knowledge_qa"
    assert state2["failed_attempts"] == 0
    assert state2["escalate_to_human"] is False


# ----------------------------------------------------------------------
# 会话状态流转
# ----------------------------------------------------------------------


def test_session_turn_count_increments():
    """同一会话多轮调用 turn_count 应自增。

    注意：传入占位 session_id 时，run_graph 内部会调用 get_or_create
    创建实际 session（生成 uuid），需用返回的 state["session_id"]
    在后续轮次与 session_manager 查询中复用，才能命中同一会话。
    """
    from app.agents.graph import run_graph
    from app.core.session import session_manager

    # 第一轮：用占位 session_id，run_graph 内部创建实际 session
    state1 = run_graph("你好", session_id="test-graph-turn-session")
    actual_session_id = state1["session_id"]
    turn1 = state1["turn_count"]
    assert turn1 >= 1

    # 第二轮：复用第一轮返回的实际 session_id，turn_count 应自增
    state2 = run_graph("谢谢", session_id=actual_session_id)
    turn2 = state2["turn_count"]
    assert turn2 == turn1 + 1

    # session_manager 中的 turn_count 应与 state 一致
    session = session_manager.get_session(actual_session_id)
    assert session is not None
    assert session["turn_count"] == turn2


def test_session_history_appended():
    """每轮对话应在 session.history 中追加 user 与 assistant 两条记录。

    复用 run_graph 返回的实际 session_id 查询 session_manager，
    避免占位 id 不命中导致 session 为 None。
    """
    from app.agents.graph import run_graph
    from app.core.session import session_manager

    # 用占位 session_id，run_graph 内部创建实际 session
    state = run_graph("你好", session_id="test-graph-history-session")
    actual_session_id = state["session_id"]

    session = session_manager.get_session(actual_session_id)
    assert session is not None
    history = session["history"]
    # 第一轮应有 user + assistant 两条
    assert len(history) >= 2
    roles = [h["role"] for h in history]
    assert "user" in roles
    assert "assistant" in roles


def test_session_history_capped():
    """history 长度超过上限时应按 FIFO 丢弃最旧条目，避免无限增长。

    循环中复用第一轮返回的实际 session_id，确保所有轮次命中同一会话，
    才能验证 history FIFO 截断逻辑生效。
    """
    from app.agents.graph import run_graph
    from app.core.session import MAX_HISTORY_LENGTH, session_manager

    # 第一轮：用占位 session_id 取得实际 session_id
    state1 = run_graph("第 0 轮对话", session_id="test-graph-history-cap")
    actual_session_id = state1["session_id"]

    # 后续轮次复用实际 session_id，累计超过 MAX_HISTORY_LENGTH
    for i in range(1, MAX_HISTORY_LENGTH + 5):
        run_graph(f"第 {i} 轮对话", session_id=actual_session_id)

    session = session_manager.get_session(actual_session_id)
    assert session is not None
    # history 长度不应超过上限 * 2（每轮 user + assistant）
    assert len(session["history"]) <= MAX_HISTORY_LENGTH * 2


def test_session_failed_attempts_synced():
    """state 中的 failed_attempts 应同步到 session_manager。

    复用 run_graph 返回的实际 session_id 查询 session_manager，
    避免占位 id 不命中导致 session 为 None。
    """
    from app.agents.graph import run_graph
    from app.core.session import session_manager

    # 用占位 session_id，run_graph 内部创建实际 session
    state = run_graph("查我的订单物流", session_id="test-graph-failed-sync")
    actual_session_id = state["session_id"]

    session = session_manager.get_session(actual_session_id)
    assert session is not None
    assert session["failed_attempts"] == state["failed_attempts"]


# ----------------------------------------------------------------------
# AgentState 结构
# ----------------------------------------------------------------------


def test_agent_state_required_fields_present():
    """run_graph 返回的 AgentState 应包含所有关键字段。"""
    from app.agents.graph import run_graph

    state = run_graph("你好")
    required_fields = [
        "session_id",
        "message",
        "intent",
        "sub_tasks",
        "emotion_score",
        "turn_count",
        "failed_attempts",
        "history",
        "raw_results",
        "final_reply",
        "sources",
        "escalate_to_human",
    ]
    for field in required_fields:
        assert field in state, f"AgentState 缺少字段：{field}"


# ----------------------------------------------------------------------
# fallback 路径
# ----------------------------------------------------------------------


def test_synch_orchestrator_runs_without_langgraph():
    """LangGraph 不可用时，同步编排器应能跑通端到端。

    通过直接调用 _SynchOrchestrator.run 验证 fallback 路径。
    """
    from app.agents.graph import _SynchOrchestrator

    initial_state = {
        "session_id": "test-synch-session",
        "user_id": None,
        "message": "你好",
        "intent": "unknown",
        "sub_tasks": [],
        "emotion_score": 1.0,
        "turn_count": 1,
        "failed_attempts": 0,
        "history": [],
        "raw_results": {},
        "final_reply": "",
        "sources": [],
        "escalate_to_human": False,
    }
    orchestrator = _SynchOrchestrator()
    state = orchestrator.run(initial_state)

    assert state["intent"] == "chitchat"
    assert state["final_reply"]
    assert "您好" in state["final_reply"] or "高兴" in state["final_reply"]
