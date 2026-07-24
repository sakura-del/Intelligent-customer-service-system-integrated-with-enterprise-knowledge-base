"""Agent 间消息总线。

提供基于 Redis Pub/Sub 的异步通信能力，让多 Agent 解耦协作：
- 发布方只关心 channel，不关心谁订阅；
- 订阅方注册 handler 到 channel，新消息到来时被回调。

可用性保障：Redis 不可达或 redis 库缺失时，自动降级到 InMemoryBus
（进程内回调模拟 Pub/Sub），保证开发/测试环境无 Redis 也能运行。

通道命名约定：
- agent.{agent_name}.request  下游 agent 的请求消息
- agent.{agent_name}.response 下游 agent 的响应消息
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.agents.agent_bus")

# 通道名前缀：与文档约定的 agent.{name}.request/response 保持一致
CHANNEL_PREFIX = "agent"


def _build_channel(agent_name: str, kind: str) -> str:
    """构造通道名。

    kind 必须是 request / response 之一，
    统一在入口处构造，避免散落拼接造成命名漂移。
    """
    if kind not in ("request", "response"):
        raise ValueError(f"通道类型必须是 request/response，收到：{kind}")
    return f"{CHANNEL_PREFIX}.{agent_name}.{kind}"


def request_channel(agent_name: str) -> str:
    """构造请求通道名。"""
    return _build_channel(agent_name, "request")


def response_channel(agent_name: str) -> str:
    """构造响应通道名。"""
    return _build_channel(agent_name, "response")


class InMemoryBus:
    """内存版 Pub/Sub 总线。

    用 dict[channel] -> list[handler] 维护订阅关系，
    publish 时同步遍历调用各 handler，模拟 Redis Pub/Sub 的语义。

    适用场景：单机开发、单测、Redis 不可用时的兜底。
    线程安全：所有读写经同一把锁串行化，避免并发发布导致 handler 列表损坏。
    """

    def __init__(self) -> None:
        # channel -> handler 列表
        self._subscribers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._lock = threading.RLock()

    def publish(self, channel: str, message: dict[str, Any]) -> None:
        """同步派发消息到所有订阅者。

        拷贝一份 handler 列表后再遍历，避免回调中 unsubscribe 导致并发修改异常。
        单个 handler 抛异常时不影响其他 handler，保证链路稳定。
        """
        with self._lock:
            handlers = list(self._subscribers.get(channel, []))
        for handler in handlers:
            try:
                handler(message)
            except Exception as exc:
                # 单个订阅者异常不应中断其他订阅者
                logger.warning("InMemoryBus 订阅者回调异常 channel=%s err=%s", channel, exc)

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """注册订阅者到指定 channel。"""
        with self._lock:
            handlers = self._subscribers.setdefault(channel, [])
            if handler not in handlers:
                handlers.append(handler)

    def unsubscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """移除指定 channel 上的 handler，不存在则静默忽略。"""
        with self._lock:
            handlers = self._subscribers.get(channel)
            if not handlers:
                return
            try:
                handlers.remove(handler)
            except ValueError:
                # 未注册过的 handler 静默忽略，保持接口幂等
                pass

    @property
    def mode(self) -> str:
        """标识当前总线类型，便于上层日志与诊断。"""
        return "memory"


class RedisBus:
    """基于 Redis Pub/Sub 的消息总线。

    publish 用普通 Redis 连接向 channel 发送 JSON 串；
    subscribe 在独立线程上跑 pubsub.pget_message() 长轮询，
    收到消息后回调对应 handler。

    设计要点：
    - 发布连接与订阅连接分离：Redis 中订阅连接不能用于 publish；
    - 订阅线程为守护线程，主进程退出时自动结束；
    - Redis 不可达时由 get_agent_bus() 上层捕获并降级到 InMemoryBus。
    """

    def __init__(self, redis_url: str) -> None:
        # 延迟导入 redis 库：未安装时让上层降级到内存版
        import json
        import time

        import redis as redis_lib

        self._json = json
        self._time = time
        self._redis_lib = redis_lib

        # 发布用连接：普通客户端即可
        self._publish_client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        # 订阅用连接：独立 client，避免与 publish 共享导致状态错乱
        self._subscribe_client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._pubsub = self._subscribe_client.pubsub()
        # channel -> handler 集合，便于 unsubscribe 时管理
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        # 后台轮询线程：daemon=True 让进程退出时不阻塞
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="redis-bus-poll", daemon=True
        )
        self._poll_thread.start()

    def publish(self, channel: str, message: dict[str, Any]) -> None:
        """向 channel 发布 JSON 消息。"""
        payload = self._json.dumps(message, ensure_ascii=False)
        self._publish_client.publish(channel, payload)

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """注册 handler 并向 Redis 订阅 channel。"""
        with self._lock:
            handlers = self._handlers.setdefault(channel, [])
            if handler in handlers:
                return
            handlers.append(handler)
            # Redis 侧订阅：同一 channel 多个 handler 只订阅一次
            if len(handlers) == 1:
                self._pubsub.subscribe(channel)

    def unsubscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """移除 handler；最后一个 handler 移除时取消 Redis 订阅。"""
        with self._lock:
            handlers = self._handlers.get(channel)
            if not handlers:
                return
            try:
                handlers.remove(handler)
            except ValueError:
                return
            if not handlers:
                self._handlers.pop(channel, None)
                self._pubsub.unsubscribe(channel)

    def _poll_loop(self) -> None:
        """后台轮询 pubsub 消息并分发到 handler。

        Redis Pub/Sub 是阻塞读取，单线程即可处理所有 channel；
        网络异常时退避重试，避免狂打日志。
        """
        while not self._stop_event.is_set():
            try:
                message = self._pubsub.get_message(timeout=1.0)
                if message and message.get("type") == "message":
                    self._dispatch(message)
            except Exception as exc:
                logger.warning("Redis Pub/Sub 轮询异常：%s", exc)
                # 退避避免异常风暴打爆 CPU/日志
                self._time.sleep(0.5)

    def _dispatch(self, message: Any) -> None:
        """将 Redis 消息分发到注册的 handler。

        data 是 JSON 字符串，需反序列化为 dict；解析失败时记录日志跳过。
        """
        channel = message.get("channel", "")
        raw_data = message.get("data", "")
        try:
            payload = self._json.loads(raw_data)
        except (ValueError, TypeError) as exc:
            logger.warning("RedisBus 消息反序列化失败 channel=%s err=%s", channel, exc)
            return
        with self._lock:
            handlers = list(self._handlers.get(channel, []))
        for handler in handlers:
            try:
                handler(payload)
            except Exception as exc:
                logger.warning("RedisBus 订阅者回调异常 channel=%s err=%s", channel, exc)

    @property
    def mode(self) -> str:
        """标识当前总线类型，便于上层日志与诊断。"""
        return "redis"

    def close(self) -> None:
        """关闭总线：停止轮询线程、关闭 pubsub 与客户端连接。"""
        self._stop_event.set()
        try:
            self._pubsub.close()
        except Exception:
            pass
        try:
            self._publish_client.close()
        except Exception:
            pass
        try:
            self._subscribe_client.close()
        except Exception:
            pass


class AgentBus:
    """对外统一的 Agent 消息总线门面。

    内部按可用性自动选择 RedisBus 或 InMemoryBus：
    - 优先尝试 Redis（连接 REDIS_URL）；
    - Redis 库缺失或连接失败时降级到 InMemoryBus。

    通过 mode 属性可观测当前实际使用的总线类型，
    便于测试断言与运维诊断。
    """

    def __init__(self, backend: Any | None = None) -> None:
        # 允许测试时直接注入 backend，跳过自动选择逻辑
        self._backend = backend or self._create_backend()
        logger.info("AgentBus 已初始化，后端类型=%s", self._backend.mode)

    @staticmethod
    def _create_backend() -> Any:
        """按可用性选择后端：先 Redis，失败则降级内存。"""
        try:
            import redis as redis_lib  # noqa: F401
        except ImportError:
            logger.warning("未安装 redis 库，AgentBus 降级到 InMemoryBus")
            return InMemoryBus()

        redis_url = get_settings().REDIS_URL
        try:
            backend = RedisBus(redis_url)
            # 简单 ping 一次确认连通；不通则降级
            backend._publish_client.ping()
            return backend
        except Exception as exc:
            # Redis 服务未启动或网络不可达：降级内存版，保证主流程可用
            logger.warning("Redis 不可达（%s），AgentBus 降级到 InMemoryBus", exc)
            try:
                # 创建失败的 RedisBus 已起轮询线程，需 close 释放
                backend.close()  # type: ignore[union-attr]
            except Exception:
                pass
            return InMemoryBus()

    def publish(self, channel: str, message: dict[str, Any]) -> None:
        """向 channel 发布消息。"""
        self._backend.publish(channel, message)

    def subscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """订阅 channel，注册 handler。"""
        self._backend.subscribe(channel, handler)

    def unsubscribe(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """取消订阅。"""
        self._backend.unsubscribe(channel, handler)

    @property
    def mode(self) -> str:
        """返回后端类型：redis / memory。"""
        return self._backend.mode


# 模块级单例：进程内复用一个总线，避免每个 Agent 各起一套 Redis 连接
_agent_bus: AgentBus | None = None
_agent_bus_lock = threading.Lock()


def get_agent_bus() -> AgentBus:
    """获取 AgentBus 单例。

    首次调用时按 Redis 可用性决定后端类型，
    后续复用同一实例，避免重复建连。
    """
    global _agent_bus
    if _agent_bus is None:
        with _agent_bus_lock:
            if _agent_bus is None:
                _agent_bus = AgentBus()
    return _agent_bus


def reset_agent_bus() -> None:
    """重置单例，便于测试切换配置或注入 mock。

    会调用 backend 的 close（Redis 后端需要释放连接与线程）。
    """
    global _agent_bus
    if _agent_bus is not None:
        backend = _agent_bus._backend
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                logger.warning("关闭 AgentBus backend 异常：%s", exc)
    _agent_bus = None
