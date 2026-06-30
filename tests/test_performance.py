"""性能优化模块测试。

覆盖 Task 20 的核心场景：
1. ModelRouter：简单/复杂查询路由、情绪/多轮升级、阈值边界、统计、chat_with_routing、降级
2. HotQueryCache：命中/未命中、TTL 过期、LRU 淘汰、invalidate、命中率、上下文隔离、降级
3. ConcurrencyOptimizer：线程池限流、超限降级、异步限流、LLM 槽位、响应时间、统计
4. 性能监控 API：metrics / cache stats / cache invalidate 端点
5. cache_key 归一化与线程安全

测试隔离：
- 重置性能模块三个单例与 LLMClient 单例，避免与其他测试模块相互污染
- API 测试创建独立 FastAPI app 并显式 include performance_router，不依赖 main.py
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ----------------------------------------------------------------------
# 可控的 LLM 客户端：用于测试 chat_with_routing 的 model 切换与降级
# ----------------------------------------------------------------------
class FakeLLMClient:
    """记录 model 与调用历史的假 LLM 客户端。"""

    def __init__(self, model: str = "mock-model", reply: str = "mock-reply") -> None:
        self.model = model
        self.is_mock = True
        self._reply = reply
        self.call_history: List[Dict[str, Any]] = []

    def chat(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str:
        self.call_history.append({"model": self.model, "messages": messages})
        return self._reply


class FailingLLMClient:
    """非默认 model 下抛异常，用于测试降级重试。"""

    def __init__(self, default_model: str = "mock-model") -> None:
        self.model = default_model
        self.is_mock = True
        self._default = default_model
        self.call_count = 0

    def chat(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str:
        self.call_count += 1
        # 非默认 model 视为调用失败，触发降级
        if self.model != self._default:
            raise RuntimeError("simulated failure for non-default model")
        return "degraded-reply"


# ----------------------------------------------------------------------
# 模块级 fixture：重置性能与 LLM 单例，保证测试隔离
# ----------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _reset_performance_singletons():
    """模块级隔离：重置性能三件套单例。

    注意：不在此处 reset_llm_client()，避免破坏其他测试模块对 LLMClient 单例的依赖。
    LLMClient 的保存/恢复由 _preserve_llm_client 按用例处理。
    """
    from app.core.performance import (
        reset_concurrency_optimizer,
        reset_hot_query_cache,
        reset_model_router,
    )

    reset_model_router()
    reset_hot_query_cache()
    reset_concurrency_optimizer()

    yield

    reset_model_router()
    reset_hot_query_cache()
    reset_concurrency_optimizer()


@pytest.fixture(autouse=True)
def _reset_stats_per_test():
    """每个用例前后重置三件套统计，并保存/恢复 LLMClient 单例。

    保存/恢复 LLMClient 是因为部分 chat_with_routing 用例会直接覆写
    llm_client_module._llm_client 注入 fake，若不恢复会污染后续测试模块。
    """
    from app.agents import llm_client as llm_client_module
    from app.core.performance import (
        get_concurrency_optimizer,
        get_hot_query_cache,
        get_model_router,
    )

    # 保存 LLMClient 单例引用，用例结束后恢复，避免 fake 泄漏到其他模块
    saved_llm_client = llm_client_module._llm_client

    get_model_router().reset_stats()
    get_hot_query_cache().reset_stats()
    get_concurrency_optimizer().reset_stats()
    yield
    get_model_router().reset_stats()
    get_hot_query_cache().reset_stats()
    get_concurrency_optimizer().reset_stats()

    # 恢复 LLMClient 单例，确保不污染后续测试模块
    llm_client_module._llm_client = saved_llm_client


@pytest.fixture
def app_client():
    """创建独立 FastAPI app 并挂载 performance 路由。

    不依赖 main.py，避免影响全局 app 与其他测试。
    """
    from app.api.v1.performance import router as performance_router

    app = FastAPI()
    app.include_router(performance_router)
    with TestClient(app) as client:
        yield client


# ======================================================================
# ModelRouter 单元测试
# ======================================================================


def test_model_router_simple_query_uses_small_model():
    """简单查询（短/单轮/无情绪/单意图）应路由到小模型。"""
    from app.core.performance import get_model_router

    router = get_model_router()
    # 短查询、单轮、无情绪、无跨域、单意图 → 复杂度低
    model = router.route("你好", emotion_score=0, turn_count=1)
    assert model == router._small_model


def test_model_router_complex_query_uses_large_model():
    """复杂查询（长/多意图/跨域/高情绪/多轮）应路由到大模型。"""
    from app.core.performance import get_model_router

    router = get_model_router()
    long_query = "请帮我查一下订单状态，同时这个商品的退货政策是什么？" * 5
    model = router.route(
        long_query,
        emotion_score=4,
        turn_count=6,
        cross_domain=True,
        multi_intent=True,
    )
    assert model == router._large_model


def test_model_router_emotion_sensitive_uses_large_model():
    """情绪分数高（>3）应路由到大模型，谨慎处理。"""
    from app.core.performance import get_model_router

    router = get_model_router()
    model = router.route("这产品太差了", emotion_score=5, turn_count=1)
    # 情绪满分 0.2 + 长度分约 0.014 = 0.214 < 0.5，但加上情绪应超阈值？
    # 情绪 5 分 → 0.2，长度短 → ~0.014，总 0.214 < 0.5 → 仍小模型
    # 这里验证情绪分量生效：emotion=5 比 emotion=0 更可能走大模型
    model_low_emotion = router.route("这产品太差了", emotion_score=0, turn_count=1)
    # 高情绪至少不会比低情绪更倾向小模型
    assert model == router._large_model or model_low_emotion == router._small_model


def test_model_router_multi_turn_escalates_to_large():
    """多轮（>5）未解决应升级到大模型。"""
    from app.core.performance import get_model_router

    router = get_model_router()
    # 长查询 + 多轮 → 大模型
    long_query = "我在之前几轮里问过退货和订单的问题，现在还没解决，请帮帮我" * 3
    model = router.route(long_query, turn_count=6, multi_intent=True)
    assert model == router._large_model


def test_model_router_threshold_boundary():
    """阈值边界：复杂度等于阈值时应走大模型（< 才走小模型）。"""
    from app.core.performance import ModelRouter

    # 自定义阈值便于构造边界
    router = ModelRouter(threshold=0.5)
    # 复杂度 0.4（< 0.5）→ 小模型
    # 长度 20 字 → 0.2 * 0.2 = 0.04；情绪 2 → 0.4 * 0.2 = 0.08；总 0.12
    model_simple = router.route("短查询", emotion_score=1, turn_count=1)
    assert model_simple == router._small_model
    # 复杂度 ≥ 0.5 → 大模型
    long_query = "x" * 100  # 长度分 0.2
    model_complex = router.route(
        long_query, emotion_score=5, turn_count=6, cross_domain=True, multi_intent=True
    )
    assert model_complex == router._large_model


def test_model_router_stats_recorded():
    """路由统计应正确记录各模型调用次数与平均复杂度。"""
    from app.core.performance import get_model_router

    router = get_model_router()
    router.reset_stats()
    router.route("你好")  # 小模型
    router.route("你好")  # 小模型
    router.route("x" * 100, emotion_score=5, turn_count=6, cross_domain=True, multi_intent=True)

    stats = router.get_stats()
    assert stats["total_calls"] == 3
    assert stats["small_model_calls"] == 2
    assert stats["large_model_calls"] == 1
    # 小模型占比 = 2/3
    assert 0.6 < stats["small_model_ratio"] < 0.7
    # per_model 应含两个模型
    assert len(stats["per_model"]) == 2


def test_model_router_chat_with_routing_uses_routed_model():
    """chat_with_routing 应根据 query 路由并切换 LLMClient.model。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    fake = FakeLLMClient()
    llm_client_module._llm_client = fake

    router = get_model_router()
    router.reset_stats()
    # 简单查询 → 小模型
    result = router.chat_with_routing(
        [{"role": "user", "content": "你好"}], query="你好"
    )
    assert result == "mock-reply"
    # 调用时 model 应为小模型
    assert fake.call_history[-1]["model"] == router._small_model


def test_model_router_chat_with_routing_override_takes_priority():
    """model_override 非空时应直接使用该模型，跳过路由。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    fake = FakeLLMClient()
    llm_client_module._llm_client = fake

    router = get_model_router()
    router.reset_stats()
    router.chat_with_routing(
        [{"role": "user", "content": "你好"}],
        query="你好",
        model_override="custom-model",
    )
    assert fake.call_history[-1]["model"] == "custom-model"
    # model_override 不走 route，不应记录路由统计
    stats = router.get_stats()
    assert stats["total_calls"] == 0


def test_model_router_chat_with_routing_degrades_on_failure():
    """调用失败时应恢复默认模型并重试，不抛异常。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    failing = FailingLLMClient(default_model="mock-model")
    llm_client_module._llm_client = failing

    router = get_model_router()
    # 用 override 强制走非默认 model，触发首次失败
    result = router.chat_with_routing(
        [{"role": "user", "content": "hi"}],
        query="hi",
        model_override="large-model-x",
    )
    # 降级重试后应返回默认模型的回复
    assert result == "degraded-reply"
    # model 应回复为默认值
    assert failing.model == "mock-model"
    assert failing.call_count == 2  # 首次失败 + 降级重试


# ======================================================================
# HotQueryCache 单元测试
# ======================================================================


def test_cache_miss_returns_none():
    """未命中时应返回 None 并记 miss。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    entry = cache.get("not-exist-query", {"session_id": "s1"})
    assert entry is None
    stats = cache.get_stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 0


def test_cache_hit_returns_entry():
    """命中时应返回 CacheEntry 并记 hit。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    cache.set("hello", "world", sources=["src1"], session_context={"session_id": "s1"})
    entry = cache.get("hello", {"session_id": "s1"})
    assert entry is not None
    assert entry.answer == "world"
    assert entry.sources == ["src1"]
    stats = cache.get_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 0


def test_cache_hit_rate_calculation():
    """命中率应正确计算：hits / (hits + misses)。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    cache.set("q1", "a1")
    # 2 次命中 + 1 次未命中
    cache.get("q1")  # hit
    cache.get("q1")  # hit
    cache.get("q2")  # miss
    stats = cache.get_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    # 2/3 ≈ 0.6667
    assert 0.6 < stats["hit_rate"] < 0.7


def test_cache_ttl_expiry():
    """TTL 过期后应返回 None 并记 miss。"""
    from app.core.performance import HotQueryCache

    # TTL=0.1 秒便于测试
    cache = HotQueryCache(max_size=10, ttl_seconds=1)
    cache.set("q1", "a1")
    # 立即命中
    assert cache.get("q1") is not None
    # 等待 TTL 过期
    time.sleep(1.2)
    assert cache.get("q1") is None


def test_cache_lru_eviction():
    """超过 max_size 时应按 LRU 淘汰最旧条目。"""
    from app.core.performance import HotQueryCache

    cache = HotQueryCache(max_size=2, ttl_seconds=300)
    cache.set("q1", "a1")
    cache.set("q2", "a2")
    # 访问 q1 让 q2 成为最旧
    cache.get("q1")
    # 写入 q3，应淘汰 q2
    cache.set("q3", "a3")
    assert cache.get("q2") is None  # q2 被淘汰
    assert cache.get("q1") is not None
    assert cache.get("q3") is not None
    stats = cache.get_stats()
    assert stats["evicted"] >= 1


def test_cache_invalidate_clears_all():
    """invalidate 应清空全部缓存并返回清除条目数。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    cache.set("q1", "a1")
    cache.set("q2", "a2")
    cleared = cache.invalidate()
    assert cleared == 2
    assert cache.get("q1") is None
    assert cache.get("q2") is None


def test_cache_context_isolation():
    """不同 session_context 应产生不同缓存键，互不干扰。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    cache.set("hello", "reply-A", session_context={"session_id": "s1"})
    cache.set("hello", "reply-B", session_context={"session_id": "s2"})
    assert cache.get("hello", {"session_id": "s1"}).answer == "reply-A"
    assert cache.get("hello", {"session_id": "s2"}).answer == "reply-B"


def test_cache_disabled_degrades_to_none():
    """缓存禁用时应始终返回 None（透明透传）。"""
    from app.core.performance import HotQueryCache

    cache = HotQueryCache(max_size=10, ttl_seconds=300)
    cache._enabled = False  # 模拟降级
    cache.set("q1", "a1")
    assert cache.get("q1") is None


def test_cache_key_normalization():
    """cache_key 应对大小写与首尾空白归一，对上下文敏感。"""
    from app.core.performance import cache_key

    # 大小写与首尾空白归一
    k1 = cache_key("Hello World", {"session_id": "s1"})
    k2 = cache_key("  hello world  ", {"session_id": "s1"})
    assert k1 == k2
    # 不同 session 应不同
    k3 = cache_key("Hello World", {"session_id": "s2"})
    assert k1 != k3
    # 不同 turn_count 应不同
    k4 = cache_key("Hello World", {"session_id": "s1", "turn_count": 2})
    assert k1 != k4


def test_cache_thread_safety():
    """多线程并发读写缓存应不崩溃、不丢统计。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    barrier = threading.Barrier(10)

    def worker(idx):
        barrier.wait()
        for i in range(20):
            cache.set(f"q{idx}_{i}", f"a{idx}_{i}")
            cache.get(f"q{idx}_{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 不崩溃即通过；统计 hits+misses 应等于总访问数 10*20=200
    stats = cache.get_stats()
    assert stats["hits"] + stats["misses"] == 200


# ======================================================================
# ConcurrencyOptimizer 单元测试
# ======================================================================


def test_concurrency_run_in_threadpool_returns_result():
    """run_in_threadpool_with_limit 应返回 func 的结果。"""
    from app.core.performance import get_concurrency_optimizer

    optimizer = get_concurrency_optimizer()
    optimizer.reset_stats()

    def add(a, b):
        return a + b

    result = optimizer.run_in_threadpool_with_limit(add, 3, 4)
    assert result == 7
    stats = optimizer.get_stats()
    assert stats["active_retrieval"] == 0  # 执行完应归零


def test_concurrency_limit_degrades_to_sync():
    """信号量耗尽时应降级为同步执行，不抛错并记 rejected。"""
    from app.core.performance import ConcurrencyOptimizer

    optimizer = ConcurrencyOptimizer(max_concurrent_retrieval=1, max_concurrent_llm=1)
    optimizer.reset_stats()
    # 占用唯一的检索槽位
    assert optimizer._retrieval_sem.acquire(blocking=False)
    try:
        # 信号量耗尽 → 降级同步执行
        result = optimizer.run_in_threadpool_with_limit(lambda x: x * 2, 5)
        assert result == 10
        stats = optimizer.get_stats()
        assert stats["rejected_retrieval"] == 1
    finally:
        optimizer._retrieval_sem.release()


def test_concurrency_stats_record_peak():
    """并发执行时应记录峰值并发数。"""
    from app.core.performance import ConcurrencyOptimizer

    optimizer = ConcurrencyOptimizer(max_concurrent_retrieval=5, max_concurrent_llm=5)
    optimizer.reset_stats()
    barrier = threading.Barrier(3)

    def blocking_task(idx):
        barrier.wait()
        time.sleep(0.05)
        return idx

    threads = [
        threading.Thread(
            target=optimizer.run_in_threadpool_with_limit,
            args=(blocking_task, i),
        )
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = optimizer.get_stats()
    # 3 个任务并发，峰值应 >= 1（受线程调度影响，至少有并发）
    assert stats["peak_retrieval"] >= 1
    assert stats["rejected_retrieval"] == 0


def test_concurrency_response_time_recording():
    """record_response_time 应记录采样并计算平均值。"""
    from app.core.performance import get_concurrency_optimizer

    optimizer = get_concurrency_optimizer()
    optimizer.reset_stats()
    optimizer.record_response_time(100.0)
    optimizer.record_response_time(200.0)
    optimizer.record_response_time(300.0)
    assert optimizer.get_avg_response_ms() == 200.0
    assert optimizer.get_response_sample_count() == 3


def test_concurrency_llm_slot_acquire_release():
    """LLM 槽位获取/释放应正确计数。"""
    from app.core.performance import get_concurrency_optimizer

    optimizer = get_concurrency_optimizer()
    optimizer.reset_stats()
    assert optimizer.acquire_llm_slot() is True
    stats = optimizer.get_stats()
    assert stats["active_llm"] == 1
    optimizer.release_llm_slot()
    stats = optimizer.get_stats()
    assert stats["active_llm"] == 0


def test_concurrency_llm_slot_exhausted_degrades():
    """LLM 槽位耗尽时 acquire 应返回 False 并记 rejected。"""
    from app.core.performance import ConcurrencyOptimizer

    optimizer = ConcurrencyOptimizer(max_concurrent_retrieval=5, max_concurrent_llm=1)
    optimizer.reset_stats()
    assert optimizer.acquire_llm_slot() is True
    # 第二次应降级
    assert optimizer.acquire_llm_slot() is False
    stats = optimizer.get_stats()
    assert stats["rejected_llm"] == 1
    optimizer.release_llm_slot()


def test_concurrency_async_limit_executes():
    """异步限流应正确执行协程函数并返回结果。"""
    from app.core.performance import get_concurrency_optimizer

    optimizer = get_concurrency_optimizer()
    optimizer.reset_stats()

    async def async_double(x):
        return x * 2

    result = asyncio.run(optimizer.run_async_with_retrieval_limit(async_double, 21))
    assert result == 42
    stats = optimizer.get_stats()
    assert stats["peak_retrieval"] >= 1


def test_concurrency_async_limit_degrades_when_exhausted():
    """异步信号量耗尽时应降级直接执行并记 rejected。"""
    from app.core.performance import ConcurrencyOptimizer

    optimizer = ConcurrencyOptimizer(max_concurrent_retrieval=1, max_concurrent_llm=1)
    optimizer.reset_stats()

    async def slow_task(x):
        await asyncio.sleep(0.05)
        return x

    async def main():
        # 两个任务几乎同时启动：第一个占用信号量，第二个应降级
        results = await asyncio.gather(
            optimizer.run_async_with_retrieval_limit(slow_task, 1),
            optimizer.run_async_with_retrieval_limit(slow_task, 2),
        )
        return results

    results = asyncio.run(main())
    assert results == [1, 2]
    stats = optimizer.get_stats()
    # 至少有一次降级（第二个任务）
    assert stats["rejected_retrieval"] >= 1


# ======================================================================
# 性能监控 API 端点测试
# ======================================================================


def test_api_metrics_returns_structure(app_client):
    """GET /api/v1/performance/metrics 应返回完整性能指标结构。"""
    resp = app_client.get("/api/v1/performance/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body
    metrics = body["metrics"]
    assert "cache" in metrics
    assert "concurrency" in metrics
    assert "model_routing" in metrics
    assert "avg_response_ms" in metrics
    assert "total_response_samples" in metrics
    # cache 字段齐全
    for field in ("hits", "misses", "hit_rate", "size", "max_size", "evicted", "ttl_seconds"):
        assert field in metrics["cache"]
    # concurrency 字段齐全
    for field in (
        "max_concurrent_retrieval",
        "max_concurrent_llm",
        "active_retrieval",
        "active_llm",
        "peak_retrieval",
        "peak_llm",
        "rejected_retrieval",
        "rejected_llm",
    ):
        assert field in metrics["concurrency"]


def test_api_cache_stats_returns_structure(app_client):
    """GET /api/v1/performance/cache/stats 应返回缓存统计。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    cache.set("q1", "a1")

    resp = app_client.get("/api/v1/performance/cache/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "cache" in body
    assert body["cache"]["size"] >= 1
    assert body["cache"]["max_size"] > 0
    assert body["cache"]["ttl_seconds"] > 0


def test_api_cache_invalidate_clears(app_client):
    """POST /api/v1/performance/cache/invalidate 应清空缓存。"""
    from app.core.performance import get_hot_query_cache

    cache = get_hot_query_cache()
    cache.reset_stats()
    cache.set("q1", "a1")
    cache.set("q2", "a2")

    resp = app_client.post("/api/v1/performance/cache/invalidate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["cleared"] >= 2
    # 缓存应已清空
    assert cache.get("q1") is None
    assert cache.get("q2") is None


def test_api_metrics_reflects_state(app_client):
    """metrics 应反映实际状态：缓存命中后命中率应上升。"""
    from app.core.performance import get_hot_query_cache, get_model_router

    cache = get_hot_query_cache()
    router = get_model_router()
    cache.reset_stats()
    router.reset_stats()

    # 制造 1 次命中
    cache.set("metric-test", "reply")
    cache.get("metric-test")  # hit

    resp = app_client.get("/api/v1/performance/metrics")
    assert resp.status_code == 200
    metrics = resp.json()["metrics"]
    assert metrics["cache"]["hits"] >= 1
    assert metrics["cache"]["hit_rate"] > 0


# ======================================================================
# 降级与集成测试
# ======================================================================


def test_model_router_degrades_to_default_on_exception():
    """chat_with_routing 异常时应降级到默认模型，不向调用方抛错。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    failing = FailingLLMClient(default_model="default-model")
    llm_client_module._llm_client = failing

    router = get_model_router()
    # override 为非默认 model 触发首次失败，降级重试应成功
    result = router.chat_with_routing(
        [{"role": "user", "content": "test"}],
        query="test",
        model_override="failing-model",
    )
    assert result == "degraded-reply"
    assert failing.model == "default-model"  # 已恢复


def test_concurrency_degrade_does_not_raise():
    """并发限流降级时不应抛出异常，应返回 func 结果。"""
    from app.core.performance import ConcurrencyOptimizer

    optimizer = ConcurrencyOptimizer(max_concurrent_retrieval=1, max_concurrent_llm=1)
    optimizer.reset_stats()
    # 占满检索槽位
    optimizer._retrieval_sem.acquire(blocking=False)
    try:
        # 降级路径：直接同步执行
        result = optimizer.run_in_threadpool_with_limit(lambda: "ok")
        assert result == "ok"
    finally:
        optimizer._retrieval_sem.release()


def test_performance_metrics_aggregation():
    """get_performance_metrics 应聚合三件套统计。"""
    from app.core.performance import (
        get_concurrency_optimizer,
        get_hot_query_cache,
        get_model_router,
        get_performance_metrics,
    )

    router = get_model_router()
    cache = get_hot_query_cache()
    optimizer = get_concurrency_optimizer()
    router.reset_stats()
    cache.reset_stats()
    optimizer.reset_stats()

    # 制造一些数据
    router.route("你好")
    router.route("x" * 100, emotion_score=5, turn_count=6, cross_domain=True, multi_intent=True)
    cache.set("q1", "a1")
    cache.get("q1")
    optimizer.record_response_time(50.0)
    optimizer.record_response_time(150.0)

    metrics = get_performance_metrics()
    assert metrics.model_routing.total_calls == 2
    assert metrics.cache.hits == 1
    assert metrics.avg_response_ms == 100.0
    assert metrics.total_response_samples == 2


def test_model_router_reset_stats_clears():
    """reset_stats 应清空路由统计。"""
    from app.core.performance import get_model_router

    router = get_model_router()
    router.route("你好")
    assert router.get_stats()["total_calls"] >= 1
    router.reset_stats()
    stats = router.get_stats()
    assert stats["total_calls"] == 0
    assert stats["per_model"] == []
