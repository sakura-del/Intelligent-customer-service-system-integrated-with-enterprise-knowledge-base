"""熔断器模块。

为外部依赖（LLM / 向量库 / 业务 API）提供熔断降级能力：
- 三态机：CLOSED（正常） / OPEN（熔断，快速失败） / HALF_OPEN（半开探测）
- 按依赖名独立熔断（如 llm / vector_store / business_api）
- 线程安全：所有共享状态由 RLock 保护
- 降级策略：失败超阈值即熔断，恢复时长后允许探测，连续成功后关闭

状态机说明：
- CLOSED → OPEN：连续失败次数达到 failure_threshold
- OPEN → HALF_OPEN：recovery_timeout 时长后，下次访问时延迟转换
- HALF_OPEN → CLOSED：连续成功次数达到 success_threshold
- HALF_OPEN → OPEN：任意一次失败立即回到 OPEN，重新计时
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.schemas.observability import CircuitBreakerState, CircuitBreakerStats

logger = get_logger("app.core.circuit_breaker")


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串，统一时间格式便于序列化。"""
    return datetime.now(timezone.utc).isoformat()


class CircuitBreakerOpenError(Exception):
    """熔断器处于 OPEN 状态时抛出，提示调用方快速失败。

    携带熔断器名称与最近熔断时间，便于调用方记录与降级处理。
    """

    def __init__(self, name: str, message: str | None = None) -> None:
        self.name = name
        super().__init__(message or f"熔断器 [{name}] 处于 OPEN 状态，快速失败")


class CircuitBreaker:
    """熔断器实例。

    每个 CircuitBreaker 保护一个外部依赖，独立维护状态与计数。
    所有可变状态由 _lock 保护，call/record_*/reset 均可安全并发调用。

    注意：call 方法内部会释放锁后再执行被保护函数，避免长时间持锁
    阻塞其他线程的快速失败判断。
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 30,
        success_threshold: int = 2,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout 必须 >= 0")
        if success_threshold < 1:
            raise ValueError("success_threshold 必须 >= 1")

        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        # 状态字段：所有读写都经 _lock 串行化
        self._state = CircuitBreakerState.CLOSED
        # 当前窗口内的连续失败/成功次数
        self._failure_count = 0
        self._success_count = 0
        # 历史累计统计
        self._total_failures = 0
        self._total_successes = 0
        # 最近一次进入 OPEN 的单调时间戳（用于恢复判断，不受系统时钟调整影响）
        self._opened_at_monotonic: float | None = None
        # ISO 字符串版本，便于对外展示
        self._last_opened_at_iso: str | None = None
        self._last_failure_at_iso: str | None = None
        self._last_success_at_iso: str | None = None

        self._lock = threading.RLock()

    @property
    def state(self) -> CircuitBreakerState:
        """返回当前状态。

        访问时会延迟触发 OPEN → HALF_OPEN 的转换，
        避免依赖外部定时器，简化实现。
        """
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            return self._state

    # ------------------------------------------------------------------
    # 核心调用入口
    # ------------------------------------------------------------------
    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """通过熔断器调用函数。

        - OPEN 状态：直接抛 CircuitBreakerOpenError，不执行 func
        - HALF_OPEN / CLOSED 状态：执行 func，按结果更新计数与状态

        func 抛异常时记录失败并重新抛出，让调用方感知错误。
        """
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            if self._state == CircuitBreakerState.OPEN:
                raise CircuitBreakerOpenError(self.name)

        # 执行被保护函数时不持锁，避免外部 IO 阻塞其他线程的快速失败判断
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            # 重新抛出前先记录失败，确保状态及时更新
            self.record_failure()
            raise exc

        self.record_success()
        return result

    # ------------------------------------------------------------------
    # 状态记录
    # ------------------------------------------------------------------
    def record_success(self) -> None:
        """记录一次成功调用。

        - HALF_OPEN：累加 success_count，达到阈值则关闭熔断器
        - CLOSED：重置当前连续失败计数，让计数从下次失败重新累计
        """
        with self._lock:
            self._total_successes += 1
            self._last_success_at_iso = _now_iso()

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._transition_to_closed_locked()
            elif self._state == CircuitBreakerState.CLOSED:
                # 成功重置连续失败计数，避免历史失败长期累积导致误熔断
                self._failure_count = 0

    def record_failure(self) -> None:
        """记录一次失败调用。

        - HALF_OPEN：任意失败立即回到 OPEN，重新计时
        - CLOSED：累加 failure_count，达到阈值则进入 OPEN
        """
        with self._lock:
            self._total_failures += 1
            self._failure_count += 1
            self._last_failure_at_iso = _now_iso()

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._transition_to_open_locked(reason="HALF_OPEN 探测失败，回到 OPEN")
            elif self._state == CircuitBreakerState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition_to_open_locked(
                        reason=f"CLOSED 连续失败 {self._failure_count} 次，转为 OPEN"
                    )

    def reset(self) -> None:
        """手动重置熔断器到 CLOSED 状态。

        用于运维介入后强制恢复，不依赖恢复时长。
        """
        with self._lock:
            self._transition_to_closed_locked(manual=True)

    # ------------------------------------------------------------------
    # 状态转换（调用方需持锁）
    # ------------------------------------------------------------------
    def _maybe_transition_to_half_open_locked(self) -> None:
        """检查是否到了半开探测时间，是则转换。

        使用 time.monotonic() 计算耗时，避免系统时钟调整导致恢复时长不准。
        """
        if self._state != CircuitBreakerState.OPEN:
            return
        if self._opened_at_monotonic is None:
            return

        elapsed = time.monotonic() - self._opened_at_monotonic
        if elapsed >= self.recovery_timeout:
            self._state = CircuitBreakerState.HALF_OPEN
            self._success_count = 0
            logger.info("熔断器 [%s] OPEN → HALF_OPEN（恢复时长已到）", self.name)

    def _transition_to_open_locked(self, reason: str) -> None:
        """进入 OPEN 状态并记录时间戳。"""
        self._state = CircuitBreakerState.OPEN
        self._opened_at_monotonic = time.monotonic()
        self._last_opened_at_iso = _now_iso()
        # 进入 OPEN 时重置半开探测的成功计数
        self._success_count = 0
        logger.warning("熔断器 [%s] → OPEN：%s", self.name, reason)

    def _transition_to_closed_locked(self, manual: bool = False) -> None:
        """回到 CLOSED 状态并清理计数。"""
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at_monotonic = None
        action = "手动重置" if manual else "探测成功"
        logger.info("熔断器 [%s] → CLOSED（%s）", self.name, action)

    # ------------------------------------------------------------------
    # 统计与查询
    # ------------------------------------------------------------------
    def stats(self) -> CircuitBreakerStats:
        """返回当前统计快照。

        访问时同样会延迟触发 OPEN → HALF_OPEN 转换，保证返回的状态与现实一致。
        """
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            return CircuitBreakerStats(
                name=self.name,
                state=self._state,
                failure_threshold=self.failure_threshold,
                recovery_timeout=self.recovery_timeout,
                success_threshold=self.success_threshold,
                failure_count=self._failure_count,
                success_count=self._success_count,
                total_failures=self._total_failures,
                total_successes=self._total_successes,
                last_opened_at=self._last_opened_at_iso,
                last_failure_at=self._last_failure_at_iso,
                last_success_at=self._last_success_at_iso,
            )


class CircuitBreakerRegistry:
    """熔断器注册中心。

    进程内单例，按 name 维护各依赖的独立熔断器：
    - get_or_create：按名取，不存在则用默认参数创建
    - get：仅查询，不创建
    - list_all：返回所有已注册熔断器的统计快照
    - reset：按名重置单个熔断器
    """

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def get_or_create(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 30,
        success_threshold: int = 2,
    ) -> CircuitBreaker:
        """获取或创建熔断器。

        已存在的熔断器忽略新参数，保证同一依赖的熔断器配置稳定。
        """
        with self._lock:
            breaker = self._breakers.get(name)
            if breaker is not None:
                return breaker
            breaker = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                success_threshold=success_threshold,
            )
            self._breakers[name] = breaker
            logger.info("注册熔断器 [%s]", name)
            return breaker

    def get(self, name: str) -> CircuitBreaker | None:
        """按名查询熔断器，不存在返回 None。"""
        with self._lock:
            return self._breakers.get(name)

    def list_all(self) -> dict[str, CircuitBreakerStats]:
        """返回所有熔断器的统计快照，按 name 索引。"""
        with self._lock:
            return {name: breaker.stats() for name, breaker in self._breakers.items()}

    def reset(self, name: str) -> bool:
        """重置指定熔断器，返回是否找到并重置成功。"""
        with self._lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                return False
            breaker.reset()
            return True

    def clear(self) -> None:
        """清空所有熔断器，主要用于测试隔离。"""
        with self._lock:
            self._breakers.clear()


# 模块级单例：进程内复用，避免每个调用点各起一套注册中心
_registry: CircuitBreakerRegistry | None = None
_registry_lock = threading.Lock()


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """获取 CircuitBreakerRegistry 单例。"""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CircuitBreakerRegistry()
    return _registry


def reset_circuit_breaker_registry() -> None:
    """重置单例，便于测试切换配置或注入 mock。"""
    global _registry
    with _registry_lock:
        _registry = None
