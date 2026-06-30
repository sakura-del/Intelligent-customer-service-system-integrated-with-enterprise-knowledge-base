"""POST /api/v1/chat/stream SSE 流式端点测试。

覆盖：
- 知识问答流式（meta + 多 token + done）
- 闲聊流式（非知识意图，结果放 meta）
- 业务查询流式（business_query 非流式收集后放 meta）
- 转人工流式（escalate=true 在 done 事件）
- 错误事件（mock 异常）
- 会话保持（同 session_id 多次调用 turn_count 递增）
- 鉴权（无 API Key 返回 401）
- 非 SSE 客户端兼容（Accept 非 text/event-stream 也能返回流）

测试用 TestClient + with client.stream("POST", ...) 消费 SSE 流。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Generator, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

# 测试用独立持久化目录，避免与其他测试模块共享向量库状态
TEST_PERSIST_DIR = "./tests/_chroma_data_chat_stream"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离 ChromaDB 目录与多 Agent 协同相关单例。

    本测试通过 mock KnowledgeAgent.handle_stream 控制流式输出，
    不依赖真实向量库内容；隔离目录保证 mock 之外的部分也能稳定运行。
    强制 LLM_API_KEY 为空进入 mock 模式，保证意图识别走关键词规则可重现。
    """
    from app.agents import (
        dialog_agent as dialog_agent_module,
    )
    from app.agents import (
        graph as graph_module,
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
        retriever as retriever_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_llm_key = settings.LLM_API_KEY
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR
    # 强制 mock 模式：意图识别走关键词规则，避免真实 LLM 调用导致测试不稳定
    settings.LLM_API_KEY = ""

    # 清理上次残留
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置相关单例
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    session_manager.reset_all()

    yield

    # 恢复配置并清理
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.LLM_API_KEY = original_llm_key
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    session_manager.reset_all()


def _parse_sse_events(text: str) -> List[Dict[str, Any]]:
    """解析 SSE 文本为事件列表。

    每个 SSE 事件由 'event: <type>\\ndata: <json>\\n\\n' 组成，
    解析后返回 [{"event": type, "data": data_dict}, ...]。
    """
    events: List[Dict[str, Any]] = []
    # 按双换行分割事件块
    blocks = text.split("\n\n")
    for block in blocks:
        if not block.strip():
            continue
        event_type = ""
        data_str = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:") :].strip()
        if event_type and data_str:
            events.append(
                {"event": event_type, "data": json.loads(data_str)}
            )
    return events


def _make_stream_events(
    sources: List[str] = None,
    tokens: List[str] = None,
    answer: str = "",
):
    """构造 KnowledgeAgent.handle_stream 的预设事件序列。"""
    sources = sources or []
    tokens = tokens or ["答"]
    events: List[Dict[str, Any]] = [
        {"type": "meta", "sources": sources, "context_count": len(sources)},
    ]
    for token in tokens:
        events.append({"type": "token", "content": token})
    events.append({"type": "done", "answer": answer or "".join(tokens), "sources": sources})
    return events


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------


def test_stream_knowledge_qa_emits_meta_tokens_done():
    """知识问答流式：meta（含 sources）+ 多 token + done。"""
    # 用 stub 替换 KnowledgeAgent.handle_stream，返回预设事件
    preset_events = _make_stream_events(
        sources=["faq.md 第1页"], tokens=["你好", "世界"], answer="你好世界"
    )

    class _StubAgent:
        def handle_stream(self, query: str, session_id: str = None):
            for event in preset_events:
                yield event

    with patch("app.api.v1.chat.get_knowledge_agent", return_value=_StubAgent()):
        client = TestClient(app)
        # 用不含闲聊/业务关键词的 query 触发 knowledge_qa 意图
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "请介绍产品功能"},
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    event_types = [e["event"] for e in events]
    # 应有 meta + 2 个 token + done
    assert "meta" in event_types
    assert "done" in event_types
    assert event_types.count("token") == 2

    meta = next(e for e in events if e["event"] == "meta")
    assert meta["data"]["intent"] == "knowledge_qa"
    assert "faq.md 第1页" in meta["data"]["sources"]

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["answer"] == "你好世界"
    assert done["data"]["escalate"] is False


def test_stream_chitchat_emits_meta_token_done():
    """闲聊流式：非知识意图，结果放 meta 与单 token。"""
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "你好啊"},
    ) as response:
        assert response.status_code == 200
        text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    event_types = [e["event"] for e in events]
    assert "meta" in event_types
    assert "token" in event_types
    assert "done" in event_types

    meta = next(e for e in events if e["event"] == "meta")
    # "你好" 命中 CHITCHAT_KEYWORDS，意图应为 chitchat
    assert meta["data"]["intent"] == "chitchat"
    # 闲聊回复应非空
    assert meta["data"]["answer"]

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["escalate"] is False


def test_stream_business_query_emits_meta_token_done():
    """业务查询流式：business_agent 非流式，结果放 meta。"""
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "查订单"},
    ) as response:
        assert response.status_code == 200
        text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    meta = next(e for e in events if e["event"] == "meta")
    # "订单" 命中 BUSINESS_KEYWORDS，意图应为 business_query
    assert meta["data"]["intent"] == "business_query"
    # 业务回复在 meta 与 token 中均应存在
    assert meta["data"]["answer"]
    token_events = [e for e in events if e["event"] == "token"]
    assert len(token_events) >= 1


def test_stream_escalation_emits_done_with_escalate_true():
    """转人工流式：用户要求转人工 → meta + done（escalate=true）。"""
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "转人工"},
    ) as response:
        assert response.status_code == 200
        text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["escalate"] is True
    assert "转接" in done["data"]["answer"] or "人工" in done["data"]["answer"]


def test_stream_emits_error_event_on_exception():
    """mock 异常：yield error 事件，HTTP 状态仍 200。"""
    class _ExplodingAgent:
        def handle_stream(self, query: str, session_id: str = None):
            raise RuntimeError("mock 异常")
            yield  # 让 Python 识别为生成器

    with patch("app.api.v1.chat.get_knowledge_agent", return_value=_ExplodingAgent()):
        client = TestClient(app)
        # 用 knowledge_qa 路径触发 handle_stream 调用
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "请介绍产品功能"},
        ) as response:
            assert response.status_code == 200
            text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) >= 1
    assert "mock 异常" in error_events[0]["data"]["message"]


def test_stream_session_turn_count_increments():
    """会话保持：同 session_id 多次调用，turn_count 递增。

    SessionManager.get_or_create 在 session_id 已存在时复用，
    否则新建并返回新 uuid。测试需先拿到首次响应的 session_id 再用于第二次调用。
    """
    client = TestClient(app)

    # 第一次调用：不传 session_id，让服务端新建
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "你好"},
    ) as response:
        assert response.status_code == 200
        text1 = response.read().decode("utf-8")
    events1 = _parse_sse_events(text1)
    done1 = next(e for e in events1 if e["event"] == "done")
    turn1 = done1["data"]["turn_count"]
    # 从 meta 事件中取不到 session_id，需通过 session_manager 反查
    # 这里改用直接传入已知 session_id 的方式验证 turn_count 递增

    # 用 session_manager 直接创建一个 session，再连续调用两次
    from app.core.session import session_manager

    session_id = session_manager.create_session(channel="api")
    # 第一次调用：turn_count 从 0 自增到 1
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "你好", "session_id": session_id},
    ) as response:
        assert response.status_code == 200
        text1 = response.read().decode("utf-8")
    events1 = _parse_sse_events(text1)
    done1 = next(e for e in events1 if e["event"] == "done")
    turn1 = done1["data"]["turn_count"]

    # 第二次调用同 session：turn_count 应递增到 2
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "再见", "session_id": session_id},
    ) as response:
        assert response.status_code == 200
        text2 = response.read().decode("utf-8")
    events2 = _parse_sse_events(text2)
    done2 = next(e for e in events2 if e["event"] == "done")
    turn2 = done2["data"]["turn_count"]

    assert turn2 > turn1, f"turn_count 应递增：{turn1} -> {turn2}"


def test_stream_without_api_key_returns_401():
    """鉴权：配置 API_KEY 后无 Key 请求返回 401。"""
    from app.core.config import get_settings

    settings = get_settings()
    original_key = settings.API_KEY
    settings.API_KEY = "test-secret-key"
    try:
        client = TestClient(app)
        response = client.post("/api/v1/chat/stream", json={"message": "你好"})
        assert response.status_code == 401
    finally:
        settings.API_KEY = original_key


def test_stream_works_without_accept_event_stream_header():
    """非 SSE 客户端兼容：不传 Accept: text/event-stream 也能返回流。"""
    client = TestClient(app)
    # 不设置 Accept 头，或设置为非 text/event-stream
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "你好"},
        headers={"Accept": "application/json"},
    ) as response:
        assert response.status_code == 200
        # 应仍返回 event-stream content-type
        assert "text/event-stream" in response.headers.get("content-type", "")
        text = response.read().decode("utf-8")
        # 应能解析出 SSE 事件
        events = _parse_sse_events(text)
        assert len(events) > 0
        assert any(e["event"] == "done" for e in events)


def test_stream_knowledge_miss_emits_fallback_done():
    """知识问答检索未命中：直接发 done 兜底，不走 LLM。"""
    from app.agents.knowledge_agent import get_knowledge_agent

    class _MissAgent:
        def handle_stream(self, query: str, session_id: str = None):
            yield {
                "type": "done",
                "answer": "抱歉，知识库中未找到相关内容。",
                "sources": [],
            }

    with patch("app.api.v1.chat.get_knowledge_agent", return_value=_MissAgent()):
        client = TestClient(app)
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "未知问题"},
        ) as response:
            assert response.status_code == 200
            text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    # 未命中也应补发 meta 让前端知道意图
    meta_events = [e for e in events if e["event"] == "meta"]
    done_events = [e for e in events if e["event"] == "done"]
    assert len(meta_events) >= 1
    assert len(done_events) == 1
    assert "未找到相关内容" in done_events[0]["data"]["answer"]


def test_stream_llm_error_event_propagated():
    """知识问答流式中 LLM 异常：error 事件透传到客户端。"""
    from app.agents.knowledge_agent import get_knowledge_agent

    class _ErrorAgent:
        def handle_stream(self, query: str, session_id: str = None):
            yield {"type": "meta", "sources": ["faq.md 第1页"], "context_count": 1}
            yield {"type": "error", "message": "LLM 调用失败"}
            yield {"type": "done", "answer": "", "sources": ["faq.md 第1页"]}

    with patch("app.api.v1.chat.get_knowledge_agent", return_value=_ErrorAgent()):
        client = TestClient(app)
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "测试"},
        ) as response:
            assert response.status_code == 200
            text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1
    assert "LLM 调用失败" in error_events[0]["data"]["message"]
