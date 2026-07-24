"""Langfuse 客户端单例。

未配置或初始化失败时降级 no-op（返回 None），保证主链路不受 Langfuse 影响。
单例风格与 app/core/monitor.py 一致：模块级变量 + Lock + 双重检查。
"""

from __future__ import annotations

import threading

from app.core.logging import get_logger

logger = get_logger("app.core.langfuse_client")

# 模块级单例与锁：进程内复用同一 Langfuse 客户端
_langfuse_client: Langfuse | None = None
_langfuse_lock = threading.Lock()


def get_langfuse_client() -> Langfuse | None:
    """获取 Langfuse 客户端单例。

    未启用/密钥缺失/初始化失败时返回 None，调用方据此走 no-op 分支。
    """
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client
    with _langfuse_lock:
        if _langfuse_client is not None:
            return _langfuse_client
        _langfuse_client = _create_client()
    return _langfuse_client


def _create_client() -> Langfuse | None:
    """按当前配置创建 Langfuse 客户端，失败时降级返回 None。"""
    from app.core.config import get_settings

    settings = get_settings()
    # 降级条件：开关关闭或密钥缺失，避免构造无效实例
    if (
        not settings.LANGFUSE_ENABLED
        or not settings.LANGFUSE_PUBLIC_KEY
        or not settings.LANGFUSE_SECRET_KEY
    ):
        return None
    try:
        # 延迟导入：未安装 langfuse 时本模块其余函数仍可被 import
        from langfuse import Langfuse

        # flush_at=1 保证开发期即时上报，不丢数据
        return Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
            flush_at=1,
        )
    except Exception as exc:
        logger.warning("Langfuse 客户端初始化失败，降级 no-op：%s", exc)
        return None


def reset_langfuse_client() -> None:
    """重置单例为 None 便于测试隔离；已存在客户端先 flush 再重置避免丢失埋点。"""
    global _langfuse_client
    with _langfuse_lock:
        client = _langfuse_client
        if client is not None:
            try:
                client.flush()
            except Exception as exc:
                logger.warning("Langfuse flush 失败，忽略：%s", exc)
        _langfuse_client = None


def is_langfuse_enabled() -> bool:
    """返回 Langfuse 是否可用，避免调用方重复判 None。"""
    return get_langfuse_client() is not None


def start_langfuse_trace(name: str, metadata: dict = None):
    """创建 Langfuse trace 并设为当前上下文，供 langfuse.openai 自动挂载 generation。

    Langfuse v4 基于 OpenTelemetry：start_as_current_observation 返回上下文管理器，
    __enter__ 后激活 contextvar，后续 langfuse.openai 调用会自动挂载到该 trace。

    未启用或创建失败时返回 None，调用方据此走 no-op 分支，不影响主链路。
    启用时返回 (context_manager, observation) 元组，交由 finish_langfuse_trace 释放。
    """
    client = get_langfuse_client()
    if client is None:
        return None
    try:
        context_manager = client.start_as_current_observation(name=name, metadata=metadata or {})
        observation = context_manager.__enter__()
        return (context_manager, observation)
    except Exception as exc:
        logger.warning("创建 Langfuse trace 失败，降级 no-op：%s", exc)
        return None


def finish_langfuse_trace(trace, status: str = "success") -> None:
    """结束 Langfuse trace：标记状态、结束 observation、释放上下文、flush 上报。

    trace 为 None 时直接返回（未启用或创建失败的降级场景）。
    v4 用 level/status_message 标记成功/失败，无 status 字段：
      success → level=DEFAULT；error → level=ERROR。
    __exit__ 会结束 span 并 detach contextvar，避免线程内上下文泄漏。
    异常时记 warning，不抛出，保证不影响主链路。
    """
    if trace is None:
        return
    try:
        context_manager, observation = trace
        level = "ERROR" if status == "error" else "DEFAULT"
        observation.update(level=level)
        context_manager.__exit__(None, None, None)
        client = get_langfuse_client()
        if client is not None:
            client.flush()
    except Exception as exc:
        logger.warning("结束 Langfuse trace 失败，忽略：%s", exc)
