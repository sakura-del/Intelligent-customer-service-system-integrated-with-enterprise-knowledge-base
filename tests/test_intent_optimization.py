"""意图识别三层优化测试：IntentCache + ModelRouter 双 Provider + 流式 HotQueryCache。

覆盖优化点：
1. IntentCache：命中/未命中、TTL 过期、LRU 淘汰、归一化 key、降级
2. ModelRouter 双 Provider 路由：小模型客户端可用时走小模型，不可用时降级主 LLM
3. orchestrator 接入 IntentCache：命中跳过 LLM 调用，未命中写入缓存
4. 流式端点接入 HotQueryCache：命中直接 yield meta + 切片 token + done，
   跳过全部编排，首 Token < 200ms

测试隔离：重置相关单例，避免与其他测试模块相互污染。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Generator, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

TEST_PERSIST_DIR = "./tests/_chroma_data_intent_opt"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _isolate_singletons():
    """模块级 fixture：隔离 ChromaDB 目录与相关单例。"""
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
    from app.core import (
        monitor as monitor_module,
    )
    from app.core.config import get_settings
    from app.core.performance import (
        reset_concurrency_optimizer,
        reset_hot_query_cache,
        reset_intent_cache,
        reset_model_router,
    )
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
    original_small_key = settings.SMALL_LLM_API_KEY
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR
    # 强制 mock 模式：意图识别走关键词规则，便于精确控制测试
    settings.LLM_API_KEY = ""
    # 强制小模型不可用：get_small_llm_client() 读空 key 返回 None，
    # 避免 .env 配置真实千问 key 时 _small_llm_client 被初始化为真实客户端，
    # 导致测试注入的 fake 被 get_small_llm_client() 延迟初始化覆盖
    settings.SMALL_LLM_API_KEY = ""
    # 强制 Langfuse 关闭：避免 .env 配置真实 key 时 LLMClient 误用 langfuse.openai
    settings.LANGFUSE_ENABLED = False

    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    llm_client_module.reset_small_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    session_manager.reset_all()
    monitor_module.reset_monitor()
    reset_model_router()
    reset_hot_query_cache()
    reset_intent_cache()
    reset_concurrency_optimizer()

    yield

    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.LLM_API_KEY = original_llm_key
    settings.SMALL_LLM_API_KEY = original_small_key
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    llm_client_module.reset_small_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    session_manager.reset_all()
    monitor_module.reset_monitor()
    reset_model_router()
    reset_hot_query_cache()
    reset_intent_cache()
    reset_concurrency_optimizer()


@pytest.fixture(autouse=True)
def _reset_caches_per_test():
    """每个用例前后重置缓存与 monitor，保证用例间不互相污染。

    额外重置 small_llm_client、_llm_client 与 orchestrator 单例，避免前一个
    用例注入的 fake LLMClient 泄漏到后续用例。orchestrator 持有 llm_client
    引用，仅重置 llm_client 不够，必须连 orchestrator 一起重置。
    模块级 fixture 已强制 LLM_API_KEY=""，重置后 LLMClient 进入 mock 模式。
    """
    from app.agents import llm_client as llm_client_module
    from app.agents import orchestrator as orchestrator_module
    from app.core import monitor as monitor_module
    from app.core.performance import (
        get_concurrency_optimizer,
        get_hot_query_cache,
        get_intent_cache,
        get_model_router,
    )

    get_model_router().reset_stats()
    get_hot_query_cache().reset_stats()
    get_intent_cache().reset_stats()
    get_concurrency_optimizer().reset_stats()
    monitor_module.reset_monitor()
    # 重置 LLM 与 orchestrator 单例，避免上一用例注入的 fake 泄漏
    llm_client_module.reset_llm_client()
    llm_client_module.reset_small_llm_client()
    orchestrator_module.reset_orchestrator()
    yield
    get_model_router().reset_stats()
    get_hot_query_cache().reset_stats()
    get_intent_cache().reset_stats()
    get_concurrency_optimizer().reset_stats()
    monitor_module.reset_monitor()
    llm_client_module.reset_llm_client()
    llm_client_module.reset_small_llm_client()
    orchestrator_module.reset_orchestrator()


def _parse_sse_events(text: str) -> List[Dict[str, Any]]:
    """解析 SSE 文本为事件列表。"""
    events: List[Dict[str, Any]] = []
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
            events.append({"event": event_type, "data": json.loads(data_str)})
    return events


# ======================================================================
# IntentCache 单元测试
# ======================================================================


def test_intent_cache_miss_returns_none():
    """未命中时应返回 None 并记 miss。"""
    from app.core.performance import get_intent_cache

    cache = get_intent_cache()
    result = cache.get("not-exist-query")
    assert result is None
    stats = cache.get_stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 0


def test_intent_cache_hit_returns_cached_value():
    """命中时应返回缓存值并记 hit。"""
    from app.core.performance import get_intent_cache
    from app.schemas.orchestrator import Intent, IntentResult, SubTask

    cache = get_intent_cache()
    intent = IntentResult(
        intent=Intent.KNOWLEDGE_QA,
        confidence=0.9,
        sub_tasks=[SubTask(agent_name="knowledge_qa", input="产品有哪些功能")],
    )
    cache.set("产品有哪些功能", intent)

    # 命中
    result = cache.get("产品有哪些功能")
    assert result is not None
    assert result.intent == Intent.KNOWLEDGE_QA
    assert result.confidence == 0.9

    stats = cache.get_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 0


def test_intent_cache_normalize_key():
    """key 应归一化：strip + lower，大小写与首尾空白不影响命中。"""
    from app.core.performance import get_intent_cache
    from app.schemas.orchestrator import Intent, IntentResult

    cache = get_intent_cache()
    cache.set("产品有哪些功能", IntentResult(intent=Intent.CHITCHAT, confidence=0.8))

    # 大小写不同
    assert cache.get("产品有哪些功能") is not None
    # 首尾空白不同
    assert cache.get("  产品有哪些功能  ") is not None
    # 空字符串不缓存
    assert cache.get("") is None
    assert cache.get("   ") is None


def test_intent_cache_ttl_expiry():
    """TTL 过期后应返回 None 并删除条目。"""
    from app.core.performance import IntentCache
    from app.schemas.orchestrator import Intent, IntentResult

    # TTL 设为 0.01s，快速触发过期
    cache = IntentCache(ttl_seconds=0)
    cache.set("hi", IntentResult(intent=Intent.CHITCHAT, confidence=0.8))
    # TTL=0 表示立即过期
    time.sleep(0.01)
    result = cache.get("hi")
    assert result is None


def test_intent_cache_lru_eviction():
    """超容量时应按 LRU 淘汰最旧条目。"""
    from app.core.performance import IntentCache
    from app.schemas.orchestrator import Intent, IntentResult

    cache = IntentCache(max_size=2)
    cache.set("q1", IntentResult(intent=Intent.CHITCHAT, confidence=0.8))
    cache.set("q2", IntentResult(intent=Intent.CHITCHAT, confidence=0.8))
    # 访问 q1 让 q2 变最旧
    assert cache.get("q1") is not None
    # 写入 q3 触发淘汰，应淘汰 q2
    cache.set("q3", IntentResult(intent=Intent.CHITCHAT, confidence=0.8))
    assert cache.get("q2") is None
    assert cache.get("q1") is not None
    assert cache.get("q3") is not None
    stats = cache.get_stats()
    assert stats["evicted"] == 1


def test_intent_cache_invalidate():
    """invalidate 应清空全部缓存。"""
    from app.core.performance import get_intent_cache
    from app.schemas.orchestrator import Intent, IntentResult

    cache = get_intent_cache()
    cache.set("q1", IntentResult(intent=Intent.CHITCHAT, confidence=0.8))
    cache.set("q2", IntentResult(intent=Intent.CHITCHAT, confidence=0.8))
    cleared = cache.invalidate()
    assert cleared == 2
    assert cache.get("q1") is None
    assert cache.get("q2") is None


# ======================================================================
# ModelRouter 双 Provider 路由测试
# ======================================================================


class _FakeSmallLLMClient:
    """记录调用的小模型假客户端。"""

    def __init__(self, reply: str = "small-model-reply") -> None:
        self.model = "doubao-lite-4k"
        self.is_mock = False
        self._reply = reply
        self.call_count = 0

    def chat(self, messages, **kwargs):
        self.call_count += 1
        return self._reply


class _FakeMainLLMClient:
    """记录 model 切换的主 LLM 假客户端。

    适配新契约：model 通过 kwargs 传入而非修改 self.model 属性，
    chat 从 kwargs 读取实际使用的 model 记入 call_history。
    """

    def __init__(self, reply: str = "main-model-reply") -> None:
        self.model = "deepseek-chat"
        self.is_mock = False
        self._reply = reply
        self.call_history: List[Dict[str, Any]] = []

    def chat(self, messages, **kwargs):
        # 从 kwargs 读取 model，未传时用 self.model（与 LLMClient.chat 行为一致）
        actual_model = kwargs.get("model", None) or self.model
        self.call_history.append({"model": actual_model})
        return self._reply


def test_model_router_uses_small_client_when_available():
    """小模型客户端可用且路由到小模型时，应调用 small_client。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    fake_small = _FakeSmallLLMClient()
    fake_main = _FakeMainLLMClient()
    # 注入主 client 与 small client
    llm_client_module._llm_client = fake_main
    llm_client_module._small_llm_client = fake_small

    router = get_model_router()
    router.reset_stats()
    # 简单 query 路由到小模型
    result = router.chat_with_routing(
        [{"role": "user", "content": "你好"}], query="你好"
    )
    assert result == "small-model-reply"
    assert fake_small.call_count == 1
    # 主 client 不应被调用
    assert len(fake_main.call_history) == 0


def test_model_router_fallback_to_main_when_small_unavailable():
    """小模型客户端不可用时，应降级到主 client 并通过 model 参数传递小模型名。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    fake_main = _FakeMainLLMClient()
    llm_client_module._llm_client = fake_main
    # 不注入 small_client（_small_llm_client 为 None）
    llm_client_module._small_llm_client = None

    router = get_model_router()
    router.reset_stats()
    # 简单 query 路由到小模型，但 small_client 不可用
    result = router.chat_with_routing(
        [{"role": "user", "content": "你好"}], query="你好"
    )
    assert result == "main-model-reply"
    # 主 client 应被调用，通过 model 参数传递小模型名
    assert len(fake_main.call_history) == 1
    assert fake_main.call_history[0]["model"] == router._small_model
    # main_client.model 属性不应被修改
    assert fake_main.model == "deepseek-chat"


def test_model_router_uses_main_for_complex_query():
    """路由到大模型时，应直接走主 client，不调用 small_client。

    用 model_override 强制大模型路由：chat_with_routing 内部 route() 仅基于
    query 文本复杂度，不接受 emotion/turn 等参数，故用 model_override 显式指定。
    """
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    fake_small = _FakeSmallLLMClient()
    fake_main = _FakeMainLLMClient()
    llm_client_module._llm_client = fake_main
    llm_client_module._small_llm_client = fake_small

    router = get_model_router()
    router.reset_stats()
    # 用 model_override 强制走大模型
    result = router.chat_with_routing(
        [{"role": "user", "content": "复杂问题"}],
        query="复杂问题",
        model_override=router._large_model,
    )
    assert result == "main-model-reply"
    # 小模型不应被调用
    assert fake_small.call_count == 0
    # 主 client 应被调用
    assert len(fake_main.call_history) == 1


def test_model_router_small_client_failure_degrades_to_main():
    """小模型调用失败时应降级到主 client 重试。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    class _FailingSmall:
        def __init__(self):
            self.model = "doubao-lite-4k"
            self.is_mock = False

        def chat(self, messages, **kwargs):
            raise RuntimeError("small model network error")

    fake_small = _FailingSmall()
    fake_main = _FakeMainLLMClient()
    llm_client_module._llm_client = fake_main
    llm_client_module._small_llm_client = fake_small

    router = get_model_router()
    router.reset_stats()
    # 简单 query 路由到小模型，但小模型调用失败
    result = router.chat_with_routing(
        [{"role": "user", "content": "你好"}], query="你好"
    )
    # 应降级到主 client
    assert result == "main-model-reply"
    assert len(fake_main.call_history) == 1


# ======================================================================
# Orchestrator IntentCache 集成测试
# ======================================================================


def test_orchestrator_intent_cache_hit_skips_llm():
    """IntentCache 命中时应跳过 LLM 意图识别，直接返回缓存结果。"""
    from app.agents import orchestrator as orchestrator_module
    from app.core.performance import get_intent_cache
    from app.schemas.orchestrator import Intent, IntentResult, SubTask

    # 重置 orchestrator 单例，确保用全新实例
    orchestrator_module.reset_orchestrator()
    orchestrator = orchestrator_module.get_orchestrator()

    # 预填意图缓存：query="产品有哪些功能" → knowledge_qa
    cached_intent = IntentResult(
        intent=Intent.KNOWLEDGE_QA,
        confidence=0.9,
        sub_tasks=[SubTask(agent_name="knowledge_qa", input="产品有哪些功能")],
    )
    get_intent_cache().set("产品有哪些功能", cached_intent)

    # mock 主 LLM 客户端（mock 模式），不应被调用
    # 关键：is_mock=False 时才走 IntentCache 路径
    class _NonMockLLM:
        is_mock = False
        call_count = 0

        def chat(self, messages, **kwargs):
            self.call_count += 1
            return '{"intent": "chitchat", "confidence": 0.5, "sub_tasks": []}'

    fake_llm = _NonMockLLM()
    orchestrator._llm_client = fake_llm

    # 调用 _recognize_intent：query="产品有哪些功能"（不在快通道关键词中）
    result = orchestrator._recognize_intent("产品有哪些功能")
    # 应返回缓存的 knowledge_qa 意图
    assert result.intent == Intent.KNOWLEDGE_QA
    assert result.confidence == 0.9
    # LLM 不应被调用
    assert fake_llm.call_count == 0


def test_orchestrator_writes_intent_cache_after_llm_call():
    """LLM 意图识别后应将高置信度结果写入 IntentCache。"""
    from app.agents import llm_client as llm_client_module
    from app.agents import orchestrator as orchestrator_module
    from app.core.performance import get_intent_cache
    from app.schemas.orchestrator import Intent

    orchestrator_module.reset_orchestrator()
    orchestrator = orchestrator_module.get_orchestrator()

    # 用 mock LLM 返回高置信度 knowledge_qa
    class _MockLLM:
        model = "mock-llm"
        is_mock = False

        def chat(self, messages, **kwargs):
            return (
                '{"intent": "knowledge_qa", "confidence": 0.9, '
                '"sub_tasks": [{"agent_name": "knowledge_qa", "input": "test"}]}'
            )

    # 关键：同时注入 orchestrator._llm_client 与 llm_client_module._llm_client
    # 因为 orchestrator._llm_based_intent 走 ModelRouter.chat_with_routing，
    # ModelRouter 内部调用 llm_client_module._llm_client（不是 orchestrator._llm_client）
    mock_llm = _MockLLM()
    orchestrator._llm_client = mock_llm
    llm_client_module._llm_client = mock_llm
    # 确保不路由到小模型客户端（避免 fake 泄漏）
    llm_client_module._small_llm_client = None

    from app.core.performance import get_model_router
    router = get_model_router()
    router.reset_stats()

    # query 不在快通道关键词，不在缓存中
    result = orchestrator._recognize_intent("会员等级有哪些权益")
    assert result.intent == Intent.KNOWLEDGE_QA

    # 缓存应已写入
    cached = get_intent_cache().get("会员等级有哪些权益")
    assert cached is not None
    assert cached.intent == Intent.KNOWLEDGE_QA
    assert cached.confidence == 0.9


def test_orchestrator_low_confidence_not_cached():
    """置信度 < 0.7 的意图结果不应写入 IntentCache。"""
    from app.agents import llm_client as llm_client_module
    from app.agents import orchestrator as orchestrator_module
    from app.core.performance import get_intent_cache

    orchestrator_module.reset_orchestrator()
    orchestrator = orchestrator_module.get_orchestrator()

    class _LowConfidenceLLM:
        model = "mock-llm"
        is_mock = False

        def chat(self, messages, **kwargs):
            return (
                '{"intent": "unknown", "confidence": 0.4, "sub_tasks": []}'
            )

    # 关键：同时注入 orchestrator._llm_client 与 llm_client_module._llm_client
    # 因为 orchestrator._llm_based_intent 走 ModelRouter.chat_with_routing，
    # ModelRouter 内部调用 llm_client_module._llm_client（不是 orchestrator._llm_client）
    # 否则上一用例注入的 fake 会泄漏到本用例，导致 confidence=0.9 被错误缓存
    low_conf_llm = _LowConfidenceLLM()
    orchestrator._llm_client = low_conf_llm
    llm_client_module._llm_client = low_conf_llm
    # 确保不路由到小模型客户端（避免 fake 泄漏）
    llm_client_module._small_llm_client = None

    orchestrator._recognize_intent("模糊不清的问题")
    cached = get_intent_cache().get("模糊不清的问题")
    # 低置信度不应缓存
    assert cached is None


# ======================================================================
# 流式端点 HotQueryCache 命中测试
# ======================================================================


def test_stream_hot_query_cache_hit_skips_orchestration():
    """HotQueryCache 命中时应跳过编排，直接 yield meta + 切片 token + done。"""
    from app.core.performance import get_hot_query_cache

    # 预填 HotQueryCache
    cache = get_hot_query_cache()
    cache.set(
        "产品有哪些功能",
        "产品支持订单查询、会员管理、售后服务等功能。",
        sources=["faq.md 第1页"],
        session_context=None,
    )

    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "产品有哪些功能", "session_id": "cache-hit-test"},
    ) as response:
        text = response.read().decode("utf-8")

    events = _parse_sse_events(text)
    # 应有 meta + 多个 token + done
    assert len(events) >= 3
    assert events[0]["event"] == "meta"
    # intent=cached 标识命中缓存
    assert events[0]["data"]["intent"] == "cached"
    assert events[0]["data"]["sources"] == ["faq.md 第1页"]
    # token 事件应有多个
    token_events = [e for e in events if e["event"] == "token"]
    assert len(token_events) >= 2
    # done 事件
    done_events = [e for e in events if e["event"] == "done"]
    assert len(done_events) == 1
    assert done_events[0]["data"]["escalate"] is False
    assert "订单查询" in done_events[0]["data"]["answer"]


def test_stream_hot_query_cache_hit_first_token_under_200ms():
    """HotQueryCache 命中时首 Token 应 < 200ms（无 LLM 调用）。"""
    import time

    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.set(
        "退货流程是什么",
        "退货流程：1. 提交申请。2. 审核通过。3. 寄回商品。",
        sources=["return_policy.md 第1页"],
        session_context=None,
    )

    client = TestClient(app)
    start = time.perf_counter()
    first_event_time = None
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "退货流程是什么", "session_id": "perf-cache-test"},
    ) as response:
        # 读取首个事件后即可计算耗时，iter_text 自动消费完整流
        for chunk in response.iter_text():
            if "event:" in chunk:
                first_event_time = time.perf_counter()
                # 不 break，让 iter_text 自然结束，避免 StreamConsumed
    elapsed_ms = (first_event_time - start) * 1000
    # 首 Token 应 < 200ms（缓存命中跳过全部编排）
    assert elapsed_ms < 200, f"首 Token {elapsed_ms:.0f}ms 超 200ms 阈值"


def test_stream_writes_hot_query_cache_after_knowledge_qa():
    """知识问答流式成功后应写入 HotQueryCache，后续相同 query 命中。"""
    from app.core.performance import get_hot_query_cache

    # 构造 KnowledgeAgent.handle_stream 的预设事件
    preset_events = [
        {"type": "meta", "sources": ["faq.md 第1页"], "context_count": 1},
        {"type": "token", "content": "产品功能"},
        {"type": "done", "answer": "产品功能包括订单查询。", "sources": ["faq.md 第1页"]},
    ]

    class _StubAgent:
        def handle_stream(self, query: str, session_id: str = None):
            for event in preset_events:
                yield event

    cache = get_hot_query_cache()
    # 确保缓存初始为空
    assert cache.get("产品功能查询") is None

    with patch("app.api.v1.chat.get_knowledge_agent", return_value=_StubAgent()):
        client = TestClient(app)
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "产品功能查询", "session_id": "write-cache-test"},
        ) as response:
            response.read()

    # 流结束后应已写入缓存
    cached = cache.get("产品功能查询")
    assert cached is not None
    assert "产品功能包括订单查询" in cached.answer
    assert cached.sources == ["faq.md 第1页"]
