"""熔断器模块测试。

覆盖 Task 21 的核心场景：
1. 三态机转换：CLOSED / OPEN / HALF_OPEN 之间的正确流转
2. 阈值参数：failure_threshold / recovery_timeout / success_threshold
3. 统计字段：累计成功/失败次数、最近熔断时间
4. 注册中心：get_or_create / list_all / reset
5. 线程安全：多线程并发调用熔断器不崩溃、不丢数据
6. API 端点：circuit-breakers 列表与 reset 接口

测试隔离：每个用例前重置 CircuitBreakerRegistry 单例，避免相互污染。
"""
from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerRegistry,
    get_circuit_breaker_registry,
    reset_circuit_breaker_registry,
)
from app.schemas.observability import CircuitBreakerState


@pytest.fixture(autouse=True)
def _reset_registry_per_test():
    """每个用例前重置 Registry 单例，避免上一用例的熔断器残留。"""
    reset_circuit_breaker_registry()
    yield
    reset_circuit_breaker_registry()


def _make_failure_func(exc: Exception = ValueError("boom")):
    """构造一个总是失败的函数，便于测试失败计数。"""

    def _func(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return _func


# ----------------------------------------------------------------------
# 三态机转换测试
# ----------------------------------------------------------------------


def test_circuit_breaker_starts_in_closed_state():
    """新建熔断器应处于 CLOSED 状态。"""
    breaker = CircuitBreaker(name="test")
    assert breaker.state == CircuitBreakerState.CLOSED
    stats = breaker.stats()
    assert stats.failure_count == 0
    assert stats.success_count == 0
    assert stats.total_failures == 0
    assert stats.total_successes == 0


def test_circuit_breaker_opens_after_failure_threshold():
    """连续失败达到阈值应转为 OPEN。"""
    breaker = CircuitBreaker(
        name="test", failure_threshold=3, recovery_timeout=30, success_threshold=2
    )
    # 失败 2 次仍在 CLOSED
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.CLOSED
    # 第 3 次失败触发熔断
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.OPEN
    stats = breaker.stats()
    assert stats.failure_count == 3
    assert stats.total_failures == 3
    assert stats.last_opened_at is not None


def test_circuit_breaker_open_raises_error_on_call():
    """OPEN 状态下 call 应抛 CircuitBreakerOpenError，不执行函数。"""
    breaker = CircuitBreaker(name="test", failure_threshold=1)
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.OPEN

    call_count = 0

    def _func():
        nonlocal call_count
        call_count += 1
        return "ok"

    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        breaker.call(_func)
    # 函数不应被执行
    assert call_count == 0
    # 异常携带熔断器名称
    assert exc_info.value.name == "test"


def test_circuit_breaker_recovers_to_half_open_after_timeout():
    """OPEN 状态经过 recovery_timeout 后应转为 HALF_OPEN。

    使用 recovery_timeout=0 模拟恢复时长已到：
    record_failure 进入 OPEN 后，下次访问 state 应立即转为 HALF_OPEN。
    """
    breaker = CircuitBreaker(
        name="test", failure_threshold=1, recovery_timeout=0, success_threshold=2
    )
    breaker.record_failure()
    # recovery_timeout=0 时，下次访问 state 应立即转为 HALF_OPEN
    # （OPEN -> HALF_OPEN 的延迟转换由 state 属性触发）
    assert breaker.state == CircuitBreakerState.HALF_OPEN


def test_circuit_breaker_half_open_to_closed_after_success_threshold():
    """HALF_OPEN 状态下连续成功达到阈值应回到 CLOSED。"""
    breaker = CircuitBreaker(
        name="test", failure_threshold=1, recovery_timeout=0, success_threshold=2
    )
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.HALF_OPEN

    # 第 1 次成功仍在 HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitBreakerState.HALF_OPEN
    # 第 2 次成功达到阈值，回到 CLOSED
    breaker.record_success()
    assert breaker.state == CircuitBreakerState.CLOSED
    # success_count 在回到 CLOSED 时被重置
    assert breaker.stats().success_count == 0


def test_circuit_breaker_half_open_to_open_on_failure():
    """HALF_OPEN 状态下任意失败应立即回到 OPEN。

    使用 recovery_timeout=60 保证 OPEN 状态可观测（不会立即被 state 属性转回 HALF_OPEN）。
    通过直接设置 _state 进入 HALF_OPEN 模拟恢复探测开始。
    """
    breaker = CircuitBreaker(
        name="test", failure_threshold=1, recovery_timeout=60, success_threshold=2
    )
    breaker.record_failure()
    # 此时 state 应为 OPEN（recovery_timeout=60 未到）
    assert breaker.state == CircuitBreakerState.OPEN

    # 模拟恢复时长已到，强制进入 HALF_OPEN
    # （直接设置 _state 避免依赖时间，单元测试中可控）
    breaker._state = CircuitBreakerState.HALF_OPEN
    breaker._success_count = 0
    assert breaker.state == CircuitBreakerState.HALF_OPEN

    # HALF_OPEN 下一次成功
    breaker.record_success()
    assert breaker.state == CircuitBreakerState.HALF_OPEN

    # 任意失败立即回到 OPEN
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.OPEN
    stats = breaker.stats()
    # 进入 OPEN 时 success_count 重置
    assert stats.success_count == 0


# ----------------------------------------------------------------------
# call 方法与异常透传
# ----------------------------------------------------------------------


def test_circuit_breaker_call_returns_result_on_success():
    """CLOSED 状态下 call 成功应返回函数结果并记录成功。"""
    breaker = CircuitBreaker(name="test", failure_threshold=3)
    result = breaker.call(lambda x: x * 2, 21)
    assert result == 42
    stats = breaker.stats()
    assert stats.total_successes == 1
    assert stats.last_success_at is not None


def test_circuit_breaker_call_propagates_exception_and_records_failure():
    """call 中函数抛异常应重新抛出并记录失败。"""
    breaker = CircuitBreaker(name="test", failure_threshold=3)

    with pytest.raises(ValueError, match="boom"):
        breaker.call(_make_failure_func())

    stats = breaker.stats()
    assert stats.total_failures == 1
    assert stats.failure_count == 1
    assert stats.last_failure_at is not None
    # 仍未达到阈值，状态保持 CLOSED
    assert stats.state == CircuitBreakerState.CLOSED


def test_circuit_breaker_success_resets_failure_count_in_closed():
    """CLOSED 状态下成功应重置连续失败计数，避免历史失败长期累积。"""
    breaker = CircuitBreaker(name="test", failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.stats().failure_count == 2
    # 一次成功应清零连续失败计数
    breaker.record_success()
    assert breaker.stats().failure_count == 0
    # 累计统计仍保留
    assert breaker.stats().total_failures == 2
    assert breaker.stats().total_successes == 1


# ----------------------------------------------------------------------
# reset 与参数校验
# ----------------------------------------------------------------------


def test_circuit_breaker_manual_reset_returns_to_closed():
    """手动 reset 应将熔断器强制回到 CLOSED，不依赖恢复时长。"""
    breaker = CircuitBreaker(
        name="test", failure_threshold=1, recovery_timeout=60, success_threshold=2
    )
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.OPEN
    # recovery_timeout=60，未到时间也能手动重置
    breaker.reset()
    assert breaker.state == CircuitBreakerState.CLOSED
    stats = breaker.stats()
    assert stats.failure_count == 0
    assert stats.success_count == 0


def test_circuit_breaker_invalid_params_raise_value_error():
    """无效的阈值参数应在构造时抛 ValueError。"""
    with pytest.raises(ValueError):
        CircuitBreaker(name="test", failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(name="test", recovery_timeout=-1)
    with pytest.raises(ValueError):
        CircuitBreaker(name="test", success_threshold=0)


# ----------------------------------------------------------------------
# 注册中心
# ----------------------------------------------------------------------


def test_registry_get_or_create_returns_same_instance():
    """get_or_create 同名应返回同一实例，忽略新参数。"""
    registry = CircuitBreakerRegistry()
    breaker1 = registry.get_or_create("llm", failure_threshold=5)
    breaker2 = registry.get_or_create("llm", failure_threshold=10)
    assert breaker1 is breaker2
    # 第一次创建时的参数应保留
    assert breaker1.failure_threshold == 5


def test_registry_list_all_returns_stats_snapshot():
    """list_all 应返回所有熔断器的统计快照。"""
    registry = CircuitBreakerRegistry()
    registry.get_or_create("llm", failure_threshold=5)
    registry.get_or_create("vector_store", failure_threshold=3)
    registry.get_or_create("business_api", failure_threshold=10)

    all_stats = registry.list_all()
    assert set(all_stats.keys()) == {"llm", "vector_store", "business_api"}
    # 每项都是 CircuitBreakerStats
    for name, stats in all_stats.items():
        assert stats.name == name
        assert stats.state == CircuitBreakerState.CLOSED


def test_registry_reset_existing_returns_true():
    """reset 已存在的熔断器应返回 True 并重置状态。"""
    registry = CircuitBreakerRegistry()
    breaker = registry.get_or_create("llm", failure_threshold=1)
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.OPEN

    assert registry.reset("llm") is True
    assert breaker.state == CircuitBreakerState.CLOSED


def test_registry_reset_nonexistent_returns_false():
    """reset 不存在的熔断器应返回 False。"""
    registry = CircuitBreakerRegistry()
    assert registry.reset("nonexistent") is False


def test_registry_get_returns_none_for_nonexistent():
    """get 不存在的熔断器应返回 None。"""
    registry = CircuitBreakerRegistry()
    assert registry.get("nonexistent") is None
    breaker = registry.get_or_create("llm")
    assert registry.get("llm") is breaker


def test_registry_clear_empties_all_breakers():
    """clear 应清空所有已注册的熔断器。"""
    registry = CircuitBreakerRegistry()
    registry.get_or_create("llm")
    registry.get_or_create("vector_store")
    assert len(registry.list_all()) == 2

    registry.clear()
    assert len(registry.list_all()) == 0


# ----------------------------------------------------------------------
# 线程安全
# ----------------------------------------------------------------------


def test_circuit_breaker_thread_safety_concurrent_calls():
    """多线程并发调用熔断器应不崩溃、不丢数据。"""
    breaker = CircuitBreaker(
        name="test", failure_threshold=1000, success_threshold=100
    )
    barrier = threading.Barrier(10)
    success_count = [0] * 10
    failure_count = [0] * 10

    def worker(idx: int):
        barrier.wait()
        for i in range(50):
            try:
                breaker.call(lambda: "ok")
                success_count[idx] += 1
            except CircuitBreakerOpenError:
                failure_count[idx] += 1
            except Exception:
                failure_count[idx] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = breaker.stats()
    # 10 线程 × 50 次 = 500 次调用
    total_calls = sum(success_count) + sum(failure_count)
    assert total_calls == 500
    # 成功数应等于 total_successes
    assert stats.total_successes == sum(success_count)


# ----------------------------------------------------------------------
# API 端点
# ----------------------------------------------------------------------


def _create_test_app() -> FastAPI:
    """构造测试用 FastAPI 应用，仅挂载 observability 路由。"""
    from app.api.v1.observability import router as observability_router

    app = FastAPI()
    app.include_router(observability_router)
    return app


def test_api_list_circuit_breakers_returns_empty_when_no_registered():
    """GET /circuit-breakers 在无熔断器时应返回空字典。"""
    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/circuit-breakers")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_api_list_circuit_breakers_returns_registered():
    """GET /circuit-breakers 应返回所有已注册熔断器的统计。"""
    registry = get_circuit_breaker_registry()
    registry.get_or_create("llm", failure_threshold=5)
    registry.get_or_create("vector_store", failure_threshold=3)

    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/circuit-breakers")
    assert resp.status_code == 200
    body = resp.json()
    assert "llm" in body
    assert "vector_store" in body
    assert body["llm"]["state"] == "closed"
    assert body["llm"]["failure_threshold"] == 5


def test_api_reset_circuit_breaker_returns_404_for_nonexistent():
    """reset 不存在的熔断器应返回 404。"""
    app = _create_test_app()
    client = TestClient(app)
    resp = client.post("/api/v1/observability/circuit-breakers/nonexistent/reset")
    assert resp.status_code == 404


def test_api_reset_circuit_breaker_resets_state():
    """reset 已存在的熔断器应重置状态并返回 200。"""
    registry = get_circuit_breaker_registry()
    breaker = registry.get_or_create("llm", failure_threshold=1)
    breaker.record_failure()
    assert breaker.state == CircuitBreakerState.OPEN

    app = _create_test_app()
    client = TestClient(app)
    resp = client.post("/api/v1/observability/circuit-breakers/llm/reset")
    assert resp.status_code == 200
    assert resp.json() == {"name": "llm", "reset": True}
    assert breaker.state == CircuitBreakerState.CLOSED
