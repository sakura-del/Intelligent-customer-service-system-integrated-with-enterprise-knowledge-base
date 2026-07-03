"""坐席辅助端点与 SessionManager 扩展方法单元测试。

覆盖：
- 8 个坐席辅助端点的正常路径与边界场景（404/409/422）
- SessionManager 扩展方法（list_pending_sessions / assign_agent /
  resolve_session / mark_pending）的 CAS 行为与排序逻辑

测试隔离：模块级 fixture 强制 mock 模式，避免触发真实 LLM/Langfuse 调用；
每个用例前重置 session_manager，保证测试相互独立。
"""
from __future__ import annotations

import os
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app

# 鉴权：API_KEY 为空时开发模式免鉴权；非空时请求头携带 X-API-Key
_API_KEY = os.environ.get("API_KEY", "")
_HEADERS: Dict[str, str] = {"X-API-Key": _API_KEY} if _API_KEY else {}


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def module_isolation():
    """模块级隔离：强制 mock 模式，避免触发真实 LLM/Langfuse 调用。

    参考 tests/test_chat_stream.py:75-124 的隔离模式，
    重置所有相关单例保证测试环境干净。
    """
    from app.agents import business_agent as business_agent_module
    from app.agents import business_adapters as business_adapters_module
    from app.agents import dialog_agent as dialog_agent_module
    from app.agents import escalation as escalation_module
    from app.agents import graph as graph_module
    from app.agents import knowledge_agent as knowledge_agent_module
    from app.agents import knowledge_feedback as knowledge_feedback_module
    from app.agents import llm_client as llm_client_module
    from app.agents import orchestrator as orchestrator_module
    from app.agents import rag_agent as rag_agent_module
    from app.core.config import get_settings
    from app.core.session import session_manager
    from app.knowledge import hybrid_retriever as hybrid_module
    from app.knowledge import retriever as retriever_module
    from app.knowledge import vectorstore as vectorstore_module

    settings = get_settings()
    original_llm_key = settings.LLM_API_KEY
    original_small_key = settings.SMALL_LLM_API_KEY
    original_langfuse = settings.LANGFUSE_ENABLED
    # 强制 mock 模式：避免真实 LLM 调用导致测试不稳定
    settings.LLM_API_KEY = ""
    settings.SMALL_LLM_API_KEY = ""
    settings.LANGFUSE_ENABLED = False

    # 重置所有相关单例，保证干净初始状态
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    hybrid_module.reset_hybrid_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    escalation_module.reset_escalation_engine()
    knowledge_feedback_module.reset_knowledge_feedback()
    business_agent_module.reset_business_agent()
    business_adapters_module.reset_business_adapter()
    session_manager.reset_all()

    yield

    # 恢复配置并清理单例
    settings.LLM_API_KEY = original_llm_key
    settings.SMALL_LLM_API_KEY = original_small_key
    settings.LANGFUSE_ENABLED = original_langfuse
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    hybrid_module.reset_hybrid_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    escalation_module.reset_escalation_engine()
    knowledge_feedback_module.reset_knowledge_feedback()
    business_agent_module.reset_business_agent()
    business_adapters_module.reset_business_adapter()
    session_manager.reset_all()


@pytest.fixture(autouse=True)
def reset_session_per_test():
    """每个用例前后重置 session_manager，保证测试相互独立。

    端点测试与 SessionManager 单元测试都依赖干净的会话状态，
    避免前序用例写入的会话影响后续断言。
    """
    from app.core.session import session_manager

    session_manager.reset_all()
    yield
    session_manager.reset_all()


@pytest.fixture(scope="module")
def client() -> TestClient:
    """模块级 TestClient，端点测试复用以减少重复创建开销。"""
    return TestClient(app)


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def _create_pending_session(
    user_id: str = "test-user",
    priority: str = "high",
    reason: str = "测试转接",
) -> str:
    """构造一个 pending 状态会话，便于测试。

    创建会话后调用 mark_pending 缓存 escalation_card，
    让会话进入待接入队列。
    """
    from app.core.session import session_manager

    session_id = session_manager.create_session(channel="test", user_id=user_id)
    escalation_card = {
        "session_id": session_id,
        "user_id": user_id,
        "member_level": "normal",
        "history_ticket_count": 0,
        "turn_count": 0,
        "conversation_summary": "测试摘要",
        "attempted_solutions": [],
        "escalate_reason": reason,
        "priority": priority,
    }
    session_manager.mark_pending(session_id, escalation_card)
    return session_id


# ----------------------------------------------------------------------
# 端点 1：GET /sessions/pending
# ----------------------------------------------------------------------


class TestListPendingSessions:
    """GET /api/v1/agent/sessions/pending 端点测试。"""

    def test_list_pending_empty(self, client: TestClient) -> None:
        """无 pending 会话时返回空列表。"""
        response = client.get("/api/v1/agent/sessions/pending", headers=_HEADERS)
        assert response.status_code == 200
        assert response.json() == []

    def test_list_pending_with_sessions(self, client: TestClient) -> None:
        """有 pending 会话时返回摘要列表，字段与 AgentSessionSummary 对齐。"""
        session_id = _create_pending_session(priority="high", reason="用户情绪激动")
        response = client.get("/api/v1/agent/sessions/pending", headers=_HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["session_id"] == session_id
        assert body[0]["priority"] == "high"
        assert body[0]["escalate_reason"] == "用户情绪激动"
        assert body[0]["agent_status"] == "pending"


# ----------------------------------------------------------------------
# 端点 2：GET /sessions/{session_id}
# ----------------------------------------------------------------------


class TestGetSessionDetail:
    """GET /api/v1/agent/sessions/{session_id} 端点测试。"""

    def test_get_session_detail_success(self, client: TestClient) -> None:
        """存在会话返回 AgentSessionDetail，含 escalation_card 与 history。"""
        session_id = _create_pending_session(user_id="user-1")
        response = client.get(
            f"/api/v1/agent/sessions/{session_id}", headers=_HEADERS
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["user_id"] == "user-1"
        assert body["agent_status"] == "pending"
        assert body["escalation_card"] is not None
        assert body["escalation_card"]["escalate_reason"] == "测试转接"
        assert body["history"] == []

    def test_get_session_detail_not_found(self, client: TestClient) -> None:
        """不存在的 session_id 返回 404。"""
        response = client.get(
            "/api/v1/agent/sessions/nonexistent-session", headers=_HEADERS
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# 端点 3：POST /sessions/{session_id}/accept
# ----------------------------------------------------------------------


class TestAcceptSession:
    """POST /api/v1/agent/sessions/{session_id}/accept 端点测试。"""

    def test_accept_success(self, client: TestClient) -> None:
        """pending 状态接手成功，返回 assigned 状态的 AgentSessionDetail。"""
        session_id = _create_pending_session()
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/accept",
            json={"agent_id": "agent-001"},
            headers=_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["agent_status"] == "assigned"
        assert body["assigned_agent_id"] == "agent-001"

    def test_accept_conflict(self, client: TestClient) -> None:
        """已 assigned 的会话再次接手返回 409（CAS 冲突）。"""
        session_id = _create_pending_session()
        # 第一次接手成功
        client.post(
            f"/api/v1/agent/sessions/{session_id}/accept",
            json={"agent_id": "agent-001"},
            headers=_HEADERS,
        )
        # 第二次接手应冲突
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/accept",
            json={"agent_id": "agent-002"},
            headers=_HEADERS,
        )
        assert response.status_code == 409

    def test_accept_not_found(self, client: TestClient) -> None:
        """不存在的 session_id 返回 404。"""
        response = client.post(
            "/api/v1/agent/sessions/nonexistent-session/accept",
            json={"agent_id": "agent-001"},
            headers=_HEADERS,
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# 端点 4：POST /sessions/{session_id}/messages
# ----------------------------------------------------------------------


class TestSendMessage:
    """POST /api/v1/agent/sessions/{session_id}/messages 端点测试。"""

    def test_send_message_success(self, client: TestClient) -> None:
        """assigned 状态发送消息成功，返回 AgentMessageResponse。"""
        session_id = _create_pending_session()
        client.post(
            f"/api/v1/agent/sessions/{session_id}/accept",
            json={"agent_id": "agent-001"},
            headers=_HEADERS,
        )
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/messages",
            json={"content": "您好，请问有什么可以帮您？"},
            headers=_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["message_id"]
        assert body["timestamp"]
        assert body["role"] == "assistant"

    def test_send_message_not_assigned(self, client: TestClient) -> None:
        """pending 状态发送消息返回 409（未接手不允许发送）。"""
        session_id = _create_pending_session()
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/messages",
            json={"content": "您好"},
            headers=_HEADERS,
        )
        assert response.status_code == 409

    def test_send_message_empty_content(self, client: TestClient) -> None:
        """content 为空返回 422（FastAPI min_length=1 约束自动校验）。"""
        session_id = _create_pending_session()
        client.post(
            f"/api/v1/agent/sessions/{session_id}/accept",
            json={"agent_id": "agent-001"},
            headers=_HEADERS,
        )
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/messages",
            json={"content": ""},
            headers=_HEADERS,
        )
        assert response.status_code == 422

    def test_send_message_not_found(self, client: TestClient) -> None:
        """不存在的 session_id 返回 404。"""
        response = client.post(
            "/api/v1/agent/sessions/nonexistent-session/messages",
            json={"content": "您好"},
            headers=_HEADERS,
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# 端点 5：POST /sessions/{session_id}/knowledge-recommend
# ----------------------------------------------------------------------


class TestKnowledgeRecommend:
    """POST /api/v1/agent/sessions/{session_id}/knowledge-recommend 端点测试。"""

    def test_knowledge_recommend_success(self, client: TestClient) -> None:
        """知识推荐返回 KnowledgeRecommendResponse，空知识库下 chunks 可为空。"""
        session_id = _create_pending_session()
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/knowledge-recommend",
            json={"query": "退货流程", "top_k": 5},
            headers=_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body["chunks"], list)
        assert body["total"] == len(body["chunks"])

    def test_knowledge_recommend_not_found(self, client: TestClient) -> None:
        """不存在的 session_id 返回 404。"""
        response = client.post(
            "/api/v1/agent/sessions/nonexistent-session/knowledge-recommend",
            json={"query": "退货流程"},
            headers=_HEADERS,
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# 端点 6：POST /sessions/{session_id}/business-assist
# ----------------------------------------------------------------------


class TestBusinessAssist:
    """POST /api/v1/agent/sessions/{session_id}/business-assist 端点测试。"""

    def test_business_assist_success(self, client: TestClient) -> None:
        """业务辅助返回 BusinessAssistResponse，result 含 reply 字段。

        BUSINESS_ADAPTER_MODE 默认 mock，mock 适配器可正常返回结果；
        业务异常时降级为 result.error 字段，不抛 5xx。
        """
        session_id = _create_pending_session()
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/business-assist",
            json={"query": "查询订单 12345678"},
            headers=_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert "result" in body
        assert isinstance(body["masked_fields"], list)
        # result 中应含 reply 或 error 字段（mock 模式下均能正常返回）
        result = body["result"]
        assert "reply" in result or "error" in result

    def test_business_assist_not_found(self, client: TestClient) -> None:
        """不存在的 session_id 返回 404。"""
        response = client.post(
            "/api/v1/agent/sessions/nonexistent-session/business-assist",
            json={"query": "查询订单"},
            headers=_HEADERS,
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# 端点 7：POST /sessions/{session_id}/resolve
# ----------------------------------------------------------------------


class TestResolveSession:
    """POST /api/v1/agent/sessions/{session_id}/resolve 端点测试。"""

    def test_resolve_success(self, client: TestClient) -> None:
        """assigned 状态标记已解决成功，返回 ResolveResponse。"""
        session_id = _create_pending_session()
        client.post(
            f"/api/v1/agent/sessions/{session_id}/accept",
            json={"agent_id": "agent-001"},
            headers=_HEADERS,
        )
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/resolve",
            json={"note": "已解决"},
            headers=_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["agent_status"] == "resolved"
        assert body["resolved_at"]

    def test_resolve_not_assigned(self, client: TestClient) -> None:
        """pending 状态直接标记已解决返回 409（未接手不允许关闭）。"""
        session_id = _create_pending_session()
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/resolve",
            json={"note": "已解决"},
            headers=_HEADERS,
        )
        assert response.status_code == 409

    def test_resolve_not_found(self, client: TestClient) -> None:
        """不存在的 session_id 返回 404。"""
        response = client.post(
            "/api/v1/agent/sessions/nonexistent-session/resolve",
            json={"note": "已解决"},
            headers=_HEADERS,
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# 端点 8：POST /sessions/{session_id}/solution
# ----------------------------------------------------------------------


class TestSubmitSolution:
    """POST /api/v1/agent/sessions/{session_id}/solution 端点测试。"""

    def test_submit_solution_success(self, client: TestClient) -> None:
        """录入方案成功，返回 HumanSolutionRecord（status=pending）。"""
        session_id = _create_pending_session()
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/solution",
            json={
                "question": "如何退货？",
                "solution": "请在订单页申请退换货。",
                "intent": "return",
            },
            headers=_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["solution_id"]
        assert body["session_id"] == session_id
        assert body["question"] == "如何退货？"
        assert body["solution"] == "请在订单页申请退换货。"
        assert body["intent"] == "return"
        assert body["status"] == "pending"

    def test_submit_solution_empty_question(self, client: TestClient) -> None:
        """question 为空返回 422（min_length=1 约束自动校验）。"""
        session_id = _create_pending_session()
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/solution",
            json={"question": "", "solution": "方案"},
            headers=_HEADERS,
        )
        assert response.status_code == 422

    def test_submit_solution_not_found(self, client: TestClient) -> None:
        """不存在的 session_id 返回 404。"""
        response = client.post(
            "/api/v1/agent/sessions/nonexistent-session/solution",
            json={"question": "问题", "solution": "方案"},
            headers=_HEADERS,
        )
        assert response.status_code == 404


# ----------------------------------------------------------------------
# SessionManager 扩展方法单元测试
# ----------------------------------------------------------------------


class TestSessionManagerExtensions:
    """SessionManager 坐席辅助扩展方法单元测试。

    直接调用 session_manager 方法，验证 CAS 行为与排序逻辑，
    不经过 HTTP 层，聚焦于状态机正确性。
    """

    def test_list_pending_sessions_order(self) -> None:
        """多个 pending 会话按 priority 降序排列（highest > high > medium > low）。"""
        from app.core.session import session_manager

        # 按非递增顺序创建，验证排序不依赖创建顺序
        _create_pending_session(user_id="u-low", priority="low")
        _create_pending_session(user_id="u-highest", priority="highest")
        _create_pending_session(user_id="u-medium", priority="medium")
        _create_pending_session(user_id="u-high", priority="high")

        pending = session_manager.list_pending_sessions()
        priorities = [item["priority"] for item in pending]
        # 期望降序：highest > high > medium > low
        assert priorities == ["highest", "high", "medium", "low"]

    def test_assign_agent_cas_success(self) -> None:
        """pending → assigned 接手成功，状态与 assigned_agent_id 正确写入。"""
        from app.core.session import session_manager

        session_id = _create_pending_session()
        success = session_manager.assign_agent(session_id, "agent-001")
        assert success is True
        session = session_manager.get_session(session_id)
        assert session["agent_status"] == "assigned"
        assert session["assigned_agent_id"] == "agent-001"

    def test_assign_agent_cas_conflict(self) -> None:
        """已 assigned 再次接手返回 False，状态保持第一次接手结果。"""
        from app.core.session import session_manager

        session_id = _create_pending_session()
        # 第一次接手成功
        assert session_manager.assign_agent(session_id, "agent-001") is True
        # 第二次接手应失败（CAS 冲突）
        assert session_manager.assign_agent(session_id, "agent-002") is False
        # 状态保持第一次的接手结果，不被第二次覆盖
        session = session_manager.get_session(session_id)
        assert session["assigned_agent_id"] == "agent-001"

    def test_resolve_session_cas_success(self) -> None:
        """assigned → resolved 标记已解决成功，resolve_note 正确写入。"""
        from app.core.session import session_manager

        session_id = _create_pending_session()
        session_manager.assign_agent(session_id, "agent-001")
        success = session_manager.resolve_session(session_id, "已解决")
        assert success is True
        session = session_manager.get_session(session_id)
        assert session["agent_status"] == "resolved"
        assert session["resolve_note"] == "已解决"

    def test_resolve_session_cas_conflict(self) -> None:
        """pending 状态直接标记已解决返回 False，状态不变。"""
        from app.core.session import session_manager

        session_id = _create_pending_session()
        # 未接手直接解决应失败（CAS 冲突）
        success = session_manager.resolve_session(session_id, "已解决")
        assert success is False
        session = session_manager.get_session(session_id)
        assert session["agent_status"] == "pending"

    def test_mark_pending_success(self) -> None:
        """成功置为 pending 并缓存 escalation_card。"""
        from app.core.session import session_manager

        session_id = session_manager.create_session(channel="test", user_id="u-1")
        card = {
            "session_id": session_id,
            "user_id": "u-1",
            "priority": "high",
            "escalate_reason": "情绪激动",
        }
        success = session_manager.mark_pending(session_id, card)
        assert success is True
        session = session_manager.get_session(session_id)
        assert session["agent_status"] == "pending"
        assert session["escalation_card"] == card

    def test_mark_pending_not_found(self) -> None:
        """不存在的会话调用 mark_pending 返回 False。"""
        from app.core.session import session_manager

        success = session_manager.mark_pending(
            "nonexistent-session", {"priority": "high"}
        )
        assert success is False
