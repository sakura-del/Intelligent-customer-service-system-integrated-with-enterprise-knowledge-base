"""会话超时自动清理测试。

覆盖范围：
1. 新建会话含 last_activity 时间戳（time.monotonic）
2. get_or_create 复用会话时刷新 last_activity
3. add_message 追加消息时刷新 last_activity
4. cleanup_expired_sessions 清理超过 TTL 的会话
5. 未超时会话不被清理
6. 空会话列表时 cleanup 返回 0
7. 并发清理线程安全（多线程同时清理不报错）
8. get_session_manager 返回单例
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core.session import (
    SessionManager,
    get_session_manager,
    session_manager,
)


# ==================== fixture ====================


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前重置 session_manager，避免用例间状态污染。"""
    session_manager.reset_all()
    yield
    session_manager.reset_all()


def _set_stale(session_id: str, age_seconds: float) -> None:
    """将指定会话的 last_activity 回拨 age_seconds 秒，模拟长时间未活动。

    直接操作内部字典以绕过 update_session 的字段限制，
    便于测试过期判定逻辑。
    """
    # 通过 update_session 写入过去的时间戳，模拟会话闲置
    session_manager.update_session(
        session_id, last_activity=time.monotonic() - age_seconds
    )


# ==================== last_activity 字段测试 ====================


def test_new_session_has_last_activity():
    """新建会话应包含 last_activity 时间戳，且接近当前 monotonic 时间。"""
    session_id = session_manager.create_session(channel="test")

    session = session_manager.get_session(session_id)

    assert session is not None
    assert "last_activity" in session
    assert isinstance(session["last_activity"], float)
    # last_activity 应接近当前时间，容差 1 秒避免调度抖动误判
    assert abs(time.monotonic() - session["last_activity"]) < 1.0


def test_get_session_copy_includes_last_activity():
    """get_session 返回的浅拷贝应包含 last_activity 字段。"""
    session_id = session_manager.create_session(channel="test")

    snapshot = session_manager.get_session(session_id)

    assert snapshot is not None
    assert "last_activity" in snapshot
    # 浅拷贝修改不应影响内部状态
    snapshot["last_activity"] = -1.0
    internal = session_manager.get_session(session_id)
    assert internal["last_activity"] != -1.0


# ==================== get_or_create 刷新 last_activity 测试 ====================


def test_get_or_create_reuse_updates_last_activity():
    """get_or_create 复用现有会话时应刷新 last_activity。"""
    session_id = session_manager.create_session(channel="test")
    original = session_manager.get_session(session_id)
    assert original is not None

    # 将 last_activity 回拨，模拟长时间未活动
    _set_stale(session_id, age_seconds=1000)
    stale = session_manager.get_session(session_id)
    assert stale["last_activity"] < original["last_activity"]

    # get_or_create 复用会话，应刷新 last_activity
    reused_id = session_manager.get_or_create(session_id, channel="test")

    assert reused_id == session_id
    refreshed = session_manager.get_session(session_id)
    assert refreshed["last_activity"] > stale["last_activity"]
    # 应接近当前时间
    assert abs(time.monotonic() - refreshed["last_activity"]) < 1.0


def test_get_or_create_new_session_has_last_activity():
    """get_or_create 新建会话时也应包含 last_activity。"""
    # 传入不存在的 session_id，应新建会话
    session_id = session_manager.get_or_create(None, channel="test")

    session = session_manager.get_session(session_id)

    assert session is not None
    assert "last_activity" in session
    assert abs(time.monotonic() - session["last_activity"]) < 1.0


# ==================== add_message 刷新 last_activity 测试 ====================


def test_add_message_updates_last_activity():
    """add_message 追加消息时应刷新 last_activity。"""
    session_id = session_manager.create_session(channel="test")
    _set_stale(session_id, age_seconds=1000)
    stale = session_manager.get_session(session_id)

    # add_message 应刷新 last_activity
    result = session_manager.add_message(session_id, "user", "你好")

    assert result is not None
    refreshed = session_manager.get_session(session_id)
    assert refreshed["last_activity"] > stale["last_activity"]
    # history 应包含新消息
    assert len(refreshed["history"]) == 1
    assert refreshed["history"][0]["content"] == "你好"


def test_add_message_nonexistent_returns_none():
    """add_message 对不存在的会话应返回 None。"""
    result = session_manager.add_message("nonexistent-id", "user", "test")
    assert result is None


def test_append_history_also_updates_last_activity():
    """append_history 作为 add_message 的底层，也应刷新 last_activity。"""
    session_id = session_manager.create_session(channel="test")
    _set_stale(session_id, age_seconds=1000)
    stale = session_manager.get_session(session_id)

    session_manager.append_history(session_id, "assistant", "回复")

    refreshed = session_manager.get_session(session_id)
    assert refreshed["last_activity"] > stale["last_activity"]


# ==================== cleanup_expired_sessions 测试 ====================


def test_cleanup_removes_expired_sessions():
    """cleanup_expired_sessions 应清理超过 TTL 的会话，保留活跃会话。"""
    # 创建 3 个会话：1 个活跃 + 2 个过期
    active_id = session_manager.create_session(channel="test")
    expired_id_1 = session_manager.create_session(channel="test")
    expired_id_2 = session_manager.create_session(channel="test")

    # 将 2 个会话标记为过期（回拨 2000 秒，TTL=1000）
    _set_stale(expired_id_1, age_seconds=2000)
    _set_stale(expired_id_2, age_seconds=2000)

    cleaned = session_manager.cleanup_expired_sessions(ttl_seconds=1000)

    assert cleaned == 2
    # 活跃会话保留
    assert session_manager.get_session(active_id) is not None
    # 过期会话已被清理
    assert session_manager.get_session(expired_id_1) is None
    assert session_manager.get_session(expired_id_2) is None


def test_cleanup_keeps_active_sessions():
    """未超时的会话不应被清理。"""
    session_id = session_manager.create_session(channel="test")

    # TTL 设为很大，不应清理任何会话
    cleaned = session_manager.cleanup_expired_sessions(ttl_seconds=3600)

    assert cleaned == 0
    assert session_manager.get_session(session_id) is not None


def test_cleanup_empty_sessions_returns_zero():
    """空会话列表时 cleanup 应返回 0。"""
    # fixture 已 reset_all，会话列表为空
    cleaned = session_manager.cleanup_expired_sessions(ttl_seconds=1000)

    assert cleaned == 0


def test_cleanup_boundary_not_expired():
    """边界：last_activity 距今略小于 TTL 时不视为过期（使用 > 而非 >=）。

    回拨 999 秒、TTL=1000 秒：(now - last_activity) 约 999+ε < 1000，应保留。
    用 999 而非 1000 是为了避开时间抖动导致的边界不稳定。
    """
    session_id = session_manager.create_session(channel="test")
    _set_stale(session_id, age_seconds=999)

    cleaned = session_manager.cleanup_expired_sessions(ttl_seconds=1000)

    # 距今小于 TTL，不应清理
    assert cleaned == 0
    assert session_manager.get_session(session_id) is not None


def test_cleanup_missing_last_activity_treated_as_expired():
    """last_activity 缺失的会话应视为数据异常并被清理。"""
    session_id = session_manager.create_session(channel="test")
    # 手动删除 last_activity 字段，模拟数据异常
    session_manager.update_session(session_id, last_activity=None)
    # update_session 写入 None 后，cleanup 时 last_activity is None 触发清理

    cleaned = session_manager.cleanup_expired_sessions(ttl_seconds=1000)

    assert cleaned == 1
    assert session_manager.get_session(session_id) is None


def test_cleanup_returns_count_matches_removed():
    """cleanup 返回值应与实际清理的会话数一致。"""
    for _ in range(5):
        sid = session_manager.create_session(channel="test")
        _set_stale(sid, age_seconds=2000)

    cleaned = session_manager.cleanup_expired_sessions(ttl_seconds=1000)

    assert cleaned == 5
    # 验证全部清理
    assert session_manager.list_sessions() == []


def test_cleanup_default_ttl_1800():
    """未传 TTL 时应使用默认值 1800 秒。"""
    session_id = session_manager.create_session(channel="test")
    # 回拨 1900 秒，超过默认 1800 秒 TTL
    _set_stale(session_id, age_seconds=1900)

    cleaned = session_manager.cleanup_expired_sessions()

    assert cleaned == 1
    assert session_manager.get_session(session_id) is None


# ==================== 线程安全测试 ====================


def test_cleanup_concurrent_no_error():
    """并发清理不应报错，最终过期会话全部被清理。"""
    # 创建 20 个会话，10 个过期（偶数索引）、10 个活跃（奇数索引）
    active_count = 0
    for i in range(20):
        sid = session_manager.create_session(channel="test")
        if i % 2 == 0:
            _set_stale(sid, age_seconds=5000)
        else:
            active_count += 1

    errors: list = []

    def _cleanup_repeatedly():
        """每个线程重复清理 10 次。"""
        try:
            for _ in range(10):
                session_manager.cleanup_expired_sessions(ttl_seconds=1000)
        except Exception as exc:  # noqa: BLE001 - 测试需捕获所有异常
            errors.append(exc)

    # 启动 5 个并发清理线程
    threads = [threading.Thread(target=_cleanup_repeatedly) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 不应有异常
    assert errors == []
    # 所有过期会话被清理，活跃会话全部保留
    remaining = session_manager.list_sessions()
    assert len(remaining) == active_count


def test_cleanup_concurrent_with_append_no_error():
    """并发清理与追加消息同时进行不应报错。"""
    session_id = session_manager.create_session(channel="test")
    errors: list = []

    def _cleanup_loop():
        try:
            for _ in range(20):
                session_manager.cleanup_expired_sessions(ttl_seconds=1000)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _append_loop():
        try:
            for i in range(20):
                session_manager.add_message(session_id, "user", f"msg-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    cleanup_thread = threading.Thread(target=_cleanup_loop)
    append_thread = threading.Thread(target=_append_loop)
    cleanup_thread.start()
    append_thread.start()
    cleanup_thread.join()
    append_thread.join()

    # 不应有异常
    assert errors == []
    # 会话应仍存在（追加消息会刷新 last_activity，不会过期）
    session = session_manager.get_session(session_id)
    assert session is not None


# ==================== get_session_manager 单例测试 ====================


def test_get_session_manager_returns_singleton():
    """get_session_manager 应返回同一单例。"""
    m1 = get_session_manager()
    m2 = get_session_manager()
    assert m1 is m2
    assert m1 is session_manager


def test_session_manager_is_session_manager_instance():
    """全局单例应为 SessionManager 实例。"""
    assert isinstance(session_manager, SessionManager)
