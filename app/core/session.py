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
- agent_status / assigned_agent_id / escalation_card / resolve_note：
  坐席辅助字段，跟踪转接后会话的接管状态与备注
- last_activity：最后活动时间戳（time.monotonic），用于超时清理判定

线程安全：所有读写经同一把锁串行化，保证多线程环境下状态一致。
内存优化：history 限制最大长度，避免长会话无限增长；
         后台线程定期清理超时会话，释放闲置内存。
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# 历史对话保留上限：超过则按 FIFO 丢弃最旧条目
# 选取 20 兼顾上下文充分与内存成本，可按业务调整
MAX_HISTORY_LENGTH = 20

# 模块级 logger：复用 root logger 配置，setup_logging 后自动生效
logger = logging.getLogger(__name__)


class SessionManager:
    """会话管理器。

    持有进程内会话字典与一把读写锁，所有暴露方法均加锁，
    保证多线程并发调用时状态一致。
    字段初始化与原有 create_session 保持兼容，
    新增字段在 _default_session_state 内统一构造。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 会话创建与查询
    # ------------------------------------------------------------------
    def create_session(
        self,
        channel: str,
        user_id: str | None = None,
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

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """根据 session_id 获取会话上下文，不存在则返回 None。"""
        with self._lock:
            # 返回浅拷贝避免外部直接修改内部状态
            session = self._sessions.get(session_id)
            return dict(session) if session is not None else None

    def get_or_create(
        self,
        session_id: str | None,
        channel: str,
        user_id: str | None = None,
    ) -> str:
        """获取或创建会话，返回有效 session_id。

        传入的 session_id 有效则复用并刷新最后活跃时间，
        否则新建会话，保证多轮对话连续性。
        """
        with self._lock:
            if session_id and session_id in self._sessions:
                # 复用会话时刷新活跃时间，避免活跃会话被误清理
                self._sessions[session_id]["last_activity"] = time.monotonic()
                return session_id
        return self.create_session(channel=channel, user_id=user_id)

    # ------------------------------------------------------------------
    # 会话状态更新
    # ------------------------------------------------------------------
    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any] | None:
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
    ) -> list[dict[str, Any]] | None:
        """追加一条对话历史。

        history 超过 MAX_HISTORY_LENGTH 时按 FIFO 丢弃最旧条目，
        防止长会话内存无限增长。
        追加同时刷新 last_activity，使活跃会话免于超时清理。
        返回追加后的历史快照；session_id 不存在则返回 None。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            history: list[dict[str, Any]] = session.setdefault("history", [])
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
            # 追加消息即用户活动，刷新活跃时间戳
            session["last_activity"] = time.monotonic()
            return list(history)

    def add_message(self, session_id: str, role: str, content: str) -> list[dict[str, Any]] | None:
        """向会话追加一条消息并刷新活跃时间。

        作为 append_history 的语义化别名，便于调用方按"添加消息"
        的直觉使用；内部复用 append_history 逻辑避免重复实现。
        返回追加后的历史快照；session_id 不存在则返回 None。
        """
        return self.append_history(session_id, role, content)

    def increment_turn(self, session_id: str) -> int | None:
        """轮次自增，返回新值；session_id 不存在则返回 None。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session["turn_count"] = int(session.get("turn_count", 0)) + 1
            return session["turn_count"]

    def increment_failed(self, session_id: str) -> int | None:
        """连续失败计数自增，返回新值；session_id 不存在则返回 None。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session["failed_attempts"] = int(session.get("failed_attempts", 0)) + 1
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
    # 坐席辅助管理
    # ------------------------------------------------------------------
    # 优先级到数值的映射：数值越大越紧急，便于 list_pending_sessions
    # 按数值降序排序，让坐席优先看到最紧急的会话
    _PRIORITY_RANK: dict[str, int] = {
        "highest": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
        "info": 0,
    }

    def list_pending_sessions(self) -> list[dict[str, Any]]:
        """返回所有 agent_status='pending' 的会话摘要，按 EscalationPriority 降序排列。

        排序映射：highest=4 > high=3 > medium=2 > low=1 > info=0
        便于坐席按优先级接手最紧急的会话。
        返回字段：session_id / user_id / priority / escalate_reason / turn_count / created_at / agent_status / assigned_agent_id
        """
        with self._lock:
            pending: list[dict[str, Any]] = []
            for session in self._sessions.values():
                # 仅关注待接入会话，已接手/已解决的不进入待办列表
                if session.get("agent_status") != "pending":
                    continue
                # 无卡片的视为 info，避免排序时缺失值导致 KeyError
                card = session.get("escalation_card") or {}
                priority = card.get("priority", "info")
                pending.append(
                    {
                        "session_id": session.get("session_id"),
                        "user_id": session.get("user_id"),
                        "priority": priority,
                        "escalate_reason": card.get("escalate_reason"),
                        "turn_count": int(session.get("turn_count", 0)),
                        "created_at": session.get("created_at"),
                        "agent_status": session.get("agent_status"),
                        "assigned_agent_id": session.get("assigned_agent_id"),
                    }
                )
            # 按优先级数值降序：最紧急的排在前，未知优先级视作 info(0)
            pending.sort(
                key=lambda x: self._PRIORITY_RANK.get(x["priority"], 0),
                reverse=True,
            )
            return pending

    def assign_agent(self, session_id: str, agent_id: str) -> bool:
        """原子化接手会话：CAS 判断 pending → assigned。

        利用 RLock 保证多坐席并发接手同一会话时只有一个成功，
        避免重复接手导致会话状态错乱。
        成功返回 True，会话不存在或状态非 pending 返回 False。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            # CAS：再次校验状态仍为 pending，避免并发接手时重复写入
            if session.get("agent_status") != "pending":
                return False
            session["agent_status"] = "assigned"
            session["assigned_agent_id"] = agent_id
            return True

    def resolve_session(self, session_id: str, note: str | None = None) -> bool:
        """原子化标记会话已解决：CAS 判断 assigned → resolved。

        仅允许 assigned 状态的会话被标记已解决，
        避免 pending 状态被直接关闭导致坐席遗漏处理。
        成功返回 True，会话不存在或状态非 assigned 返回 False。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            # CAS：仅 assigned 状态可流转到 resolved，防止 pending 被跳过处理
            if session.get("agent_status") != "assigned":
                return False
            session["agent_status"] = "resolved"
            session["resolve_note"] = note
            return True

    def mark_pending(self, session_id: str, escalation_card: dict[str, Any]) -> bool:
        """转接触发时将会话置为 pending 并缓存 EscalationCard。

        由 EscalationEngine.check_escalation() 调用方在转接决策通过后调用，
        把转接卡片缓存到 session 中，避免坐席查询时重复构建卡片。
        成功返回 True，会话不存在返回 False。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session["agent_status"] = "pending"
            session["escalation_card"] = escalation_card
            return True

    # ------------------------------------------------------------------
    # 维护与诊断
    # ------------------------------------------------------------------
    def cleanup_expired_sessions(self, ttl_seconds: int = 1800) -> int:
        """清理超过 TTL 未活动的会话，返回被清理的会话数。

        以 last_activity（time.monotonic 时间戳）为基准，
        距当前时间超过 ttl_seconds 的会话视为过期并被删除。
        整个扫描与删除过程持同一把 RLock，保证与并发读写互斥。
        日志在锁外记录，避免日志 IO 拖慢锁持有时间。
        """
        now = time.monotonic()
        expired_ids: list[str] = []
        with self._lock:
            # 先扫描收集过期 session_id，再统一删除，避免边遍历边修改字典
            for session_id, session in self._sessions.items():
                last_activity = session.get("last_activity")
                # last_activity 缺失视为数据异常，主动清理避免脏数据驻留
                if last_activity is None or (now - last_activity) > ttl_seconds:
                    expired_ids.append(session_id)
            for session_id in expired_ids:
                # pop 避免 KeyError：理论上不会发生，防御性编程
                self._sessions.pop(session_id, None)
        # 锁外记录日志，减少锁持有时间
        if expired_ids:
            logger.info("清理过期会话：%d 个", len(expired_ids))
        return len(expired_ids)

    def reset_session(self, session_id: str) -> None:
        """删除指定会话，便于测试隔离与手动重置。"""
        with self._lock:
            self._sessions.pop(session_id, None)

    def reset_all(self) -> None:
        """清空所有会话，主要用于测试。"""
        with self._lock:
            self._sessions.clear()

    def list_sessions(self) -> list[dict[str, Any]]:
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
                        "failed_attempts": int(session.get("failed_attempts", 0)),
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
        user_id: str | None = None,
    ) -> dict[str, Any]:
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
            # 以下为坐席辅助端点（add-agent-assist-endpoints）新增字段：
            # 跟踪转接后会话的接管状态，避免坐席遗漏处理或重复接手
            # None=未触发转接；pending=已转接待接入；assigned=坐席已接手；resolved=已解决
            "agent_status": None,
            # 坐席接手后写入，便于审计与多坐席协作时定位责任人
            "assigned_agent_id": None,
            # 缓存转接时生成的 EscalationCard，坐席查询时直接读取避免重复构建
            "escalation_card": None,
            # 标记已解决时坐席可写入备注，便于事后回溯处理结论
            "resolve_note": None,
            # 最后活动时间戳：time.monotonic()，用于超时清理判定；
            # 选择 monotonic 而非 wall clock，避免系统时钟回拨导致误判
            "last_activity": time.monotonic(),
        }


# 全局会话管理单例
session_manager = SessionManager()


def get_session_manager() -> SessionManager:
    """获取全局 SessionManager 单例。

    作为工厂函数入口，便于 main.py 启动后台清理线程时获取实例，
    也为后续接入 Redis 等共享存储时替换实现提供统一注入点。
    """
    return session_manager
