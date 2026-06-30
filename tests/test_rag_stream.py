"""RAGAgent.answer_stream 与 KnowledgeAgent.handle_stream 流式生成测试。

覆盖：
- 检索命中流式正常
- 检索未命中直接返回兜底
- LLM 异常 yield error
- mock 模式流式
- 来源标注正确
- 上下文衔接（多轮查询 session_id 一致）

测试隔离：注入 fake LLMClient / RAGAgent / 检索器，避免依赖真实向量库。
"""
from __future__ import annotations

from typing import Any, Dict, Generator, List, Optional
from unittest.mock import MagicMock

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.llm_client import LLMClient
from app.agents.rag_agent import RAGAgent
from app.schemas.knowledge import RetrievedChunk


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeStreamLLM:
    """可控的 LLMClient 替身：stream_chat 按预设事件序列 yield。

    用于测试 RAGAgent.answer_stream 对 LLM 事件的透传与聚合逻辑。
    """

    def __init__(
        self,
        events: List[Dict[str, Any]],
        is_mock: bool = False,
    ) -> None:
        self._events = list(events)
        self.is_mock = is_mock

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Generator[Dict[str, Any], None, None]:
        for event in self._events:
            yield event


def _make_chunk(
    text: str,
    source: str = "faq.md",
    page_number: int = 1,
    score: float = 0.9,
) -> RetrievedChunk:
    """构造 RetrievedChunk 测试夹具。"""
    return RetrievedChunk(
        text=text,
        score=score,
        source=source,
        page_number=page_number,
        section="测试章节",
        knowledge_type="faq",
    )


# ----------------------------------------------------------------------
# RAGAgent.answer_stream 用例
# ----------------------------------------------------------------------


def test_rag_answer_stream_yields_meta_tokens_done():
    """检索命中：先 meta（含 sources/context_count），再多个 token，最后 done。"""
    chunks = [_make_chunk("知识片段内容", source="faq.md", page_number=3)]
    fake_llm = _FakeStreamLLM(
        events=[
            {"type": "token", "content": "你好"},
            {"type": "token", "content": "世界"},
            {"type": "done", "content": "你好世界"},
        ]
    )
    agent = RAGAgent(llm_client=fake_llm)

    events = list(agent.answer_stream(query="测试问题", context_chunks=chunks))

    # 1. 首事件为 meta，含 sources 与 context_count
    assert events[0]["type"] == "meta"
    assert events[0]["context_count"] == 1
    assert "faq.md 第3页" in events[0]["sources"]

    # 2. 中间事件为 token
    token_events = [e for e in events if e["type"] == "token"]
    assert [e["content"] for e in token_events] == ["你好", "世界"]

    # 3. 末事件为 done，含完整答案与 sources
    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    assert done_events[0]["answer"] == "你好世界"
    assert "faq.md 第3页" in done_events[0]["sources"]


def test_rag_answer_stream_propagates_llm_error():
    """LLM 异常：yield error 事件，且后续不再有 token 事件。"""
    chunks = [_make_chunk("知识片段")]
    fake_llm = _FakeStreamLLM(
        events=[
            {"type": "token", "content": "部分"},
            {"type": "error", "message": "LLM 异常"},
            {"type": "done", "content": "部分"},
        ]
    )
    agent = RAGAgent(llm_client=fake_llm)

    events = list(agent.answer_stream(query="问题", context_chunks=chunks))

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert "LLM 异常" in error_events[0]["message"]
    # error 后 done 仍透传，保证调用方能收尾
    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1


def test_rag_answer_stream_handles_missing_done_event():
    """LLM 未发 done 事件：RAGAgent 兜底发一次 done，含已累积文本。"""
    chunks = [_make_chunk("片段")]
    fake_llm = _FakeStreamLLM(
        events=[
            {"type": "token", "content": "ABC"},
            {"type": "token", "content": "DEF"},
            # 没有 done 事件
        ]
    )
    agent = RAGAgent(llm_client=fake_llm)

    events = list(agent.answer_stream(query="问题", context_chunks=chunks))

    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    assert done_events[0]["answer"] == "ABCDEF"


def test_rag_answer_stream_sources_dedup_and_truncated():
    """来源标注：多 chunk 同源去重，最多展示 MAX_SOURCE_COUNT 条。"""
    # 构造 5 个 chunk，其中 2 个同源（faq.md 第1页），验证去重
    chunks = [
        _make_chunk("片段1", source="faq.md", page_number=1),
        _make_chunk("片段2", source="faq.md", page_number=1),  # 重复
        _make_chunk("片段3", source="manual.md", page_number=2),
        _make_chunk("片段4", source="policy.md", page_number=5),
    ]
    fake_llm = _FakeStreamLLM(events=[{"type": "done", "content": "答案"}])
    agent = RAGAgent(llm_client=fake_llm)

    events = list(agent.answer_stream(query="问题", context_chunks=chunks))

    meta_event = next(e for e in events if e["type"] == "meta")
    sources = meta_event["sources"]
    # 去重后应剩 3 条
    assert len(sources) == 3
    assert "faq.md 第1页" in sources
    assert "manual.md 第2页" in sources
    assert "policy.md 第5页" in sources


# ----------------------------------------------------------------------
# KnowledgeAgent.handle_stream 用例
# ----------------------------------------------------------------------


class _StubKnowledgeAgent(KnowledgeAgent):
    """重写 _retrieve_filter 与 rag_agent，便于隔离测试。

    通过预设检索结果与 fake RAGAgent 控制流式输出，
    避免依赖真实向量库与 reranker。
    """

    def __init__(
        self,
        filtered_chunks: List[RetrievedChunk],
        rag_agent: RAGAgent,
    ) -> None:
        super().__init__()
        self._filtered_chunks = filtered_chunks
        self._rag_agent = rag_agent

    def _retrieve_filter(self, query: str) -> List[RetrievedChunk]:
        return list(self._filtered_chunks)


class _FakeRAGAgent:
    """可控的 RAGAgent 替身：answer_stream 按预设事件序列 yield。"""

    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self._events = list(events)
        # 记录调用参数，便于断言上下文衔接
        self.last_query: Optional[str] = None
        self.last_session_id: Optional[str] = None

    def answer_stream(
        self,
        query: str,
        context_chunks: List[RetrievedChunk],
        session_id: Optional[str] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        self.last_query = query
        self.last_session_id = session_id
        for event in self._events:
            yield event


def test_knowledge_handle_stream_hit_transfers_rag_events():
    """检索命中：透传 RAGAgent.answer_stream 的 meta/token/done 事件。"""
    chunks = [_make_chunk("片段", source="faq.md", page_number=1)]
    fake_rag = _FakeRAGAgent(
        events=[
            {"type": "meta", "sources": ["faq.md 第1页"], "context_count": 1},
            {"type": "token", "content": "答"},
            {"type": "done", "answer": "答", "sources": ["faq.md 第1页"]},
        ]
    )
    agent = _StubKnowledgeAgent(filtered_chunks=chunks, rag_agent=fake_rag)

    events = list(agent.handle_stream(query="问题", session_id="sess-1"))

    types = [e["type"] for e in events]
    assert types == ["meta", "token", "done"]
    assert fake_rag.last_query == "问题"
    assert fake_rag.last_session_id == "sess-1"


def test_knowledge_handle_stream_miss_returns_fallback_directly():
    """检索未命中：直接 yield done 兜底，不走 LLM。"""
    fake_rag = _FakeRAGAgent(events=[])
    agent = _StubKnowledgeAgent(filtered_chunks=[], rag_agent=fake_rag)

    events = list(agent.handle_stream(query="未知问题"))

    # 应只发一个 done 事件，answer 为兜底文案，sources 为空
    assert len(events) == 1
    assert events[0]["type"] == "done"
    assert "未找到相关内容" in events[0]["answer"]
    assert events[0]["sources"] == []
    # fake_rag 不应被调用
    assert fake_rag.last_query is None


def test_knowledge_handle_stream_empty_query_returns_fallback():
    """空问题：直接 yield done 兜底。"""
    fake_rag = _FakeRAGAgent(events=[])
    agent = _StubKnowledgeAgent(filtered_chunks=[], rag_agent=fake_rag)

    events = list(agent.handle_stream(query=""))

    assert len(events) == 1
    assert events[0]["type"] == "done"
    assert "问题不能为空" in events[0]["answer"]


def test_knowledge_handle_stream_propagates_llm_error():
    """LLM 异常：透传 RAGAgent 的 error 事件。"""
    chunks = [_make_chunk("片段")]
    fake_rag = _FakeRAGAgent(
        events=[
            {"type": "meta", "sources": ["faq.md 第1页"], "context_count": 1},
            {"type": "error", "message": "LLM 调用失败"},
        ]
    )
    agent = _StubKnowledgeAgent(filtered_chunks=chunks, rag_agent=fake_rag)

    events = list(agent.handle_stream(query="问题"))

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert "LLM 调用失败" in error_events[0]["message"]


def test_knowledge_handle_stream_session_id_passed_through():
    """上下文衔接：多次调用 handle_stream 时 session_id 透传到 RAGAgent。"""
    chunks = [_make_chunk("片段")]
    fake_rag = _FakeRAGAgent(
        events=[{"type": "done", "answer": "答", "sources": []}]
    )
    agent = _StubKnowledgeAgent(filtered_chunks=chunks, rag_agent=fake_rag)

    # 第一次调用
    list(agent.handle_stream(query="问题1", session_id="sess-123"))
    assert fake_rag.last_session_id == "sess-123"

    # 第二次调用同一 session，session_id 应一致透传
    list(agent.handle_stream(query="问题2", session_id="sess-123"))
    assert fake_rag.last_session_id == "sess-123"
    assert fake_rag.last_query == "问题2"


def test_rag_answer_stream_mock_mode_works():
    """mock 模式：LLMClient.stream_chat 切片 yield，RAGAgent 透传正常。"""
    from app.core.config import get_settings

    settings = get_settings()
    original_key = settings.LLM_API_KEY
    settings.LLM_API_KEY = ""  # 强制 mock 模式
    try:
        mock_llm = LLMClient()
        assert mock_llm.is_mock
        agent = RAGAgent(llm_client=mock_llm)
        chunks = [_make_chunk("Mock 测试片段内容", source="mock.md", page_number=1)]

        events = list(agent.answer_stream(query="问题", context_chunks=chunks))

        # mock 模式应切片 yield 多个 token + done
        token_events = [e for e in events if e["type"] == "token"]
        done_events = [e for e in events if e["type"] == "done"]
        assert len(token_events) >= 2
        assert len(done_events) == 1
        # mock 回复应包含 context_chunks 文本
        assert "Mock 测试片段内容" in done_events[0]["answer"]
    finally:
        settings.LLM_API_KEY = original_key
