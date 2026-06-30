"""Agent 消息总线测试。

覆盖 InMemoryBus 与 AgentBus 的核心行为：
1. 通道命名：request_channel / response_channel 格式正确
2. InMemoryBus：发布/订阅/取消订阅、多 handler、异常隔离
3. AgentBus：自动降级到 InMemoryBus（Redis 不可达时）
4. 单例：get_agent_bus / reset_agent_bus 行为正确

不依赖 Redis 服务，全部走内存路径，保证测试可在任意环境运行。
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from app.agents.agent_bus import (
    AgentBus,
    InMemoryBus,
    request_channel,
    response_channel,
)
from app.agents import agent_bus as agent_bus_module


# ----------------------------------------------------------------------
# 通道命名
# ----------------------------------------------------------------------

def test_request_channel_naming() -> None:
    """请求通道名应遵循 agent.{name}.request 格式。"""
    assert request_channel("knowledge_qa") == "agent.knowledge_qa.request"


def test_response_channel_naming() -> None:
    """响应通道名应遵循 agent.{name}.response 格式。"""
    assert response_channel("dialog") == "agent.dialog.response"


def test_channel_kind_validation() -> None:
    """非法 kind 应抛 ValueError，避免拼写错误静默通过。"""
    from app.agents.agent_bus import _build_channel

    with pytest.raises(ValueError):
        _build_channel("agent_x", "invalid")


# ----------------------------------------------------------------------
# InMemoryBus
# ----------------------------------------------------------------------

class _Collector:
    """收集 publish 派发的消息，便于断言。

    闭包变量在多 handler 场景下管理麻烦，用一个可变容器更清晰。
    """

    def __init__(self) -> None:
        self.received: List[Dict] = []

    def __call__(self, message: Dict) -> None:
        self.received.append(message)


def test_in_memory_bus_publish_subscribe() -> None:
    """订阅后应收到 publish 的消息，内容一致。"""
    bus = InMemoryBus()
    collector = _Collector()
    channel = "agent.knowledge_qa.request"

    bus.subscribe(channel, collector)
    bus.publish(channel, {"question": "忘记密码怎么办"})

    assert len(collector.received) == 1
    assert collector.received[0]["question"] == "忘记密码怎么办"


def test_in_memory_bus_unsubscribe() -> None:
    """取消订阅后不应再收到消息。"""
    bus = InMemoryBus()
    collector = _Collector()
    channel = "agent.chitchat.request"

    bus.subscribe(channel, collector)
    bus.unsubscribe(channel, collector)
    bus.publish(channel, {"message": "你好"})

    assert collector.received == []


def test_in_memory_bus_multiple_handlers() -> None:
    """同一 channel 多个 handler 都应收到消息。"""
    bus = InMemoryBus()
    collector_a = _Collector()
    collector_b = _Collector()
    channel = "agent.ticket.request"

    bus.subscribe(channel, collector_a)
    bus.subscribe(channel, collector_b)
    bus.publish(channel, {"issue": "退款"})

    assert len(collector_a.received) == 1
    assert len(collector_b.received) == 1


def test_in_memory_bus_handler_exception_isolated() -> None:
    """单个 handler 抛异常不应影响其他 handler。

    避免一个订阅者故障导致整条总线停摆。
    """
    bus = InMemoryBus()
    received: List[Dict] = []

    def bad_handler(message: Dict) -> None:
        raise RuntimeError("故意抛异常")

    def good_handler(message: Dict) -> None:
        received.append(message)

    channel = "agent.business_query.request"
    bus.subscribe(channel, bad_handler)
    bus.subscribe(channel, good_handler)
    # 不应抛异常
    bus.publish(channel, {"q": "查订单"})
    assert len(received) == 1


def test_in_memory_bus_unsubscribe_idempotent() -> None:
    """重复 unsubscribe 不应报错，保持接口幂等。"""
    bus = InMemoryBus()
    collector = _Collector()
    channel = "agent.test.request"

    bus.subscribe(channel, collector)
    bus.unsubscribe(channel, collector)
    # 再次 unsubscribe 不应抛异常
    bus.unsubscribe(channel, collector)


def test_in_memory_bus_mode() -> None:
    """InMemoryBus.mode 应返回 'memory'。"""
    assert InMemoryBus().mode == "memory"


# ----------------------------------------------------------------------
# AgentBus 门面（自动降级）
# ----------------------------------------------------------------------

def test_agent_bus_falls_back_to_memory_when_redis_unreachable() -> None:
    """Redis 不可达时，AgentBus 应自动降级到 InMemoryBus。

    通过指向一个不存在的 Redis 端口模拟不可达，
    验证降级后 publish/subscribe 仍可用。
    """
    from app.core.config import get_settings

    settings = get_settings()
    original_url = settings.REDIS_URL
    # 指向一个几乎必然不可达的端口，触发降级
    settings.REDIS_URL = "redis://127.0.0.1:1/0"
    try:
        bus = AgentBus()
        assert bus.mode == "memory"

        collector = _Collector()
        bus.subscribe("agent.test.request", collector)
        bus.publish("agent.test.request", {"hello": "world"})
        assert len(collector.received) == 1
    finally:
        settings.REDIS_URL = original_url


def test_agent_bus_with_injected_memory_backend() -> None:
    """注入 InMemoryBus 后端时应直接使用，跳过 Redis 探测。"""
    memory_backend = InMemoryBus()
    bus = AgentBus(backend=memory_backend)
    assert bus.mode == "memory"

    collector = _Collector()
    bus.subscribe("agent.x.request", collector)
    bus.publish("agent.x.request", {"k": "v"})
    assert collector.received[0]["k"] == "v"


# ----------------------------------------------------------------------
# 单例管理
# ----------------------------------------------------------------------

def test_get_agent_bus_singleton() -> None:
    """get_agent_bus 应返回同一实例（单例）。"""
    agent_bus_module.reset_agent_bus()
    bus_a = agent_bus_module.get_agent_bus()
    bus_b = agent_bus_module.get_agent_bus()
    assert bus_a is bus_b


def test_reset_agent_bus_clears_singleton() -> None:
    """reset_agent_bus 后下次 get 应返回新实例。"""
    agent_bus_module.reset_agent_bus()
    bus_a = agent_bus_module.get_agent_bus()
    agent_bus_module.reset_agent_bus()
    bus_b = agent_bus_module.get_agent_bus()
    assert bus_a is not bus_b


@pytest.fixture(autouse=True)
def _reset_agent_bus_after_test() -> None:
    """每个用例后重置单例，避免状态泄漏影响后续测试。"""
    yield
    agent_bus_module.reset_agent_bus()
