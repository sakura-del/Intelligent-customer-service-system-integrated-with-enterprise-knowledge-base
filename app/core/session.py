"""会话管理。

负责会话创建、查询与状态维护。当前采用进程内字典存储，
接口设计已与 Redis 对齐，后续接入共享存储时只需替换实现。

会话状态字段：
- session_id / channel / user_id：基础身份信息
- current_intent：当前轮识别到的意图，便于上下文衔接
- slots：槽位 dict（如订单号、商品名），供业务 agent 复用
- history：对话历史（role/content），用于多轮上下文
- emotion_score：用户情绪分数（0-1，越低越负面）
- turn_count / failed_attempts：调度状态计数

线程安全：所有读写经同一把锁串行化，保证多线程环境下状态一致。
内存优化：history 限制最大长度，避免长会话无限增长。
"""
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 历史对话保留上限：超过则按 FIFO 丢弃最旧条目
# 选取 20 兼顾上下文充分与内存成本，可按业务调整
MAX_HISTORY_LENGTH = 20


class SessionManager:
    """会话管理器。

    持有进程内会话字典与一把读写锁，所有暴露方法均加锁，
    保证多线程并发调用时状态一致。
    字段初始化与原有 create_session 保持兼容，
    新增字段在 _default_session_state 内统一构造。
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 会话创建与查询
    # ------------------------------------------------------------------
    def create_session(
        self,
        channel: str,
        user_id: Optional[str] = None,
    ) -> str:
        """创建新会话并返回 session_id。

        使用 uuid4 保证全局唯一性，避免多渠道会话冲突。
        新会话字段含原始基础字段 + 调度状态字段，向后兼容旧调用。
        """
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = self._default_session_state(
                session_id=session_id,
                channel=channel,
                user_id=user_id,
            )
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """根据 session_id 获取会话上下文，不存在则返回 None。"""
        with self._lock:
            # 返回浅拷贝避免外部直接修改内部状态
            session = self._sessions.get(session_id)
            return dict(session) if session is not None else None

    def get_or_create(
        self,
        session_id: Optional[str],
        channel: str,
        user_id: Optional[str] = None,
    ) -> str:
        """获取或创建会话，返回有效 session_id。

        传入的 session_id 有效则复用，否则新建会话，
        保证多轮对话连续性。
        """
        with self._lock:
            if session_id and session_id in self._sessions:
                return session_id
        return self.create_session(channel=channel, user_id=user_id)

    # ------------------------------------------------------------------
    # 会话状态更新
    # ------------------------------------------------------------------
    def update_session(
        self, session_id: str, **fields: Any
    ) -> Optional[Dict[str, Any]]:
        """部分更新会话字段。

        未知字段也会写入，便于扩展；但内部保留字段（如 session_id）
        不会被覆盖，避免主键被破坏。
        返回更新后的会话快照；session_id 不存在则返回 None。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            for key, value in fields.items():
                # session_id 是主键，禁止覆盖避免索引失联
                if key == "session_id":
                    continue
                session[key] = value
            return dict(session)

    def append_history(
        self, session_id: str, role: str, content: str
    ) -> Optional[List[Dict[str, Any]]]:
        """追加一条对话历史。

        history 超过 MAX_HISTORY_LENGTH 时按 FIFO 丢弃最旧条目，
        防止长会话内存无限增长。
        返回追加后的历史快照；session_id 不存在则返回 None。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            history: List[Dict[str, Any]] = session.setdefault("history", [])
            history.append(
                {
                    "role": role,
                    "content": content,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            # 超长时丢弃最旧的若干条，一次切片到上限避免频繁触发
            if len(history) > MAX_HISTORY_LENGTH:
                del history[: len(history) - MAX_HISTORY_LENGTH]
            return list(history)

    def increment_turn(self, session_id: str) -> Optional[int]:
        """轮次自增，返回新值；session_id 不存在则返回 None。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session["turn_count"] = int(session.get("turn_count", 0)) + 1
            return session["turn_count"]

    def increment_failed(self, session_id: str) -> Optional[int]:
        """连续失败计数自增，返回新值；session_id 不存在则返回 None。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session["failed_attempts"] = (
                int(session.get("failed_attempts", 0)) + 1
            )
            return session["failed_attempts"]

    def reset_failed(self, session_id: str) -> bool:
        """重置连续失败计数；session_id 不存在返回 False。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session["failed_attempts"] = 0
            return True

    def reset_slots(self, session_id: str) -> bool:
        """重置会话槽位，用于意图切换后清理旧槽位。

        保留 history 与 turn_count，仅清空 slots，
        便于新意图重新填充槽位，同时保留历史用于回溯。
        session_id 不存在返回 False。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            # 用新 dict 替换避免外部持有旧引用造成脏读
            session["slots"] = {}
            return True

    # ------------------------------------------------------------------
    # 维护与诊断
    # ------------------------------------------------------------------
    def reset_session(self, session_id: str) -> None:
        """删除指定会话，便于测试隔离与手动重置。"""
        with self._lock:
            self._sessions.pop(session_id, None)

    def reset_all(self) -> None:
        """清空所有会话，主要用于测试。"""
        with self._lock:
            self._sessions.clear()

    def list_sessions(self) -> List[Dict[str, Any]]:
        """返回所有活跃会话的摘要列表。

        供监控面板使用：按最后活跃时间倒序返回，
        每条仅含运维关注字段（session_id/turn_count/failed_attempts/current_intent/最后活跃时间），
        避免把完整 history 等大字段透出造成内存与带宽浪费。
        """
        with self._lock:
            items = []
            for session in self._sessions.values():
                # history 中最后一条的 timestamp 作为"最后活跃时间"
                history = session.get("history") or []
                last_active = (
                    history[-1].get("timestamp")
                    if history and isinstance(history[-1], dict)
                    else session.get("created_at")
                )
                items.append(
                    {
                        "session_id": session.get("session_id"),
                        "channel": session.get("channel"),
                        "user_id": session.get("user_id"),
                        "turn_count": int(session.get("turn_count", 0)),
                        "failed_attempts": int(
                            session.get("failed_attempts", 0)
                        ),
                        "current_intent": session.get("current_intent"),
                        "emotion_score": session.get("emotion_score"),
                        "created_at": session.get("created_at"),
                        "last_active_at": last_active,
                        "history_count": len(history),
                    }
                )
            # 最后活跃时间缺失的排到末尾，便于面板按活跃度排序
            items.sort(key=lambda x: x.get("last_active_at") or "", reverse=True)
            return items

    @staticmethod
    def _default_session_state(
        session_id: str,
        channel: str,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造新会话的默认状态字典。

        统一在此处定义字段，便于后续接入 Redis 时复用序列化结构。
        messages 字段保留以兼容旧调用方；新调度逻辑用 history。
        """
        return {
            "session_id": session_id,
            "channel": channel,
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "messages": [],
            # 以下为 Task 8 新增字段
            "current_intent": None,
            "slots": {},
            "history": [],
            "emotion_score": None,
            "turn_count": 0,
            "failed_attempts": 0,
        }


# 全局会话管理单例
session_manager = SessionManager()
