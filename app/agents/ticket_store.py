"""工单存储。

负责工单的增删改查与状态流转，当前采用进程内字典存储，
接口设计已预留持久化扩展点（_persist / _load），
后续接入数据库或 Redis 时只需覆写这两个钩子即可。

线程安全：所有读写经同一把 RLock 串行化，
保证多线程并发创建/更新工单时 ID 唯一与状态一致。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from app.core.logging import get_logger
from app.schemas.ticket import (
    Ticket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)

logger = get_logger("app.agents.ticket_store")

# 工单 ID 前缀：便于在日志与监控中识别业务类型
TICKET_ID_PREFIX = "TK"


class TicketStore:
    """工单存储：内存实现 + 持久化扩展点。

    持有进程内工单字典与一把读写锁，所有暴露方法均加锁。
    _persist / _load 为预留钩子，子类可覆写以接入 DB / Redis，
    默认空实现保持内存语义，便于单测与离线开发。
    """

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 工单创建与查询
    # ------------------------------------------------------------------
    def create_ticket(
        self,
        user_id: str | None,
        title: str,
        description: str,
        category: TicketCategory,
        priority: TicketPriority,
        related_order: str | None = None,
        related_product: str | None = None,
        contact: str | None = None,
    ) -> Ticket:
        """创建并持久化工单，返回新建的 Ticket。

        ticket_id 由前缀 + uuid4 生成，保证全局唯一且可读；
        状态默认 pending，等待后续分派。
        """
        ticket_id = self._generate_ticket_id()
        now = datetime.now(timezone.utc)
        ticket = Ticket(
            ticket_id=ticket_id,
            user_id=user_id,
            title=title,
            description=description,
            category=category,
            priority=priority,
            status=TicketStatus.pending,
            related_order=related_order,
            related_product=related_product,
            contact=contact,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._tickets[ticket_id] = ticket
            # 预留钩子：子类可在此写入持久化存储
            self._persist(ticket)
        logger.info(
            "工单创建成功：ticket_id=%s category=%s priority=%s user=%s",
            ticket_id,
            ticket.category.value,
            ticket.priority.value,
            user_id or "-",
        )
        # 返回副本避免外部突变内部状态，与 SessionManager 风格一致
        return ticket.model_copy()

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        """根据 ticket_id 获取工单，不存在返回 None。

        返回副本避免外部直接修改内部状态。
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            return ticket.model_copy() if ticket is not None else None

    def list_tickets(self, user_id: str | None = None) -> list[Ticket]:
        """列出工单，可按 user_id 过滤。

        未传 user_id 时返回全部工单（供运维监控）；
        返回结果按创建时间倒序，便于优先查看最新工单。
        返回副本避免外部直接修改内部状态。
        """
        with self._lock:
            tickets = [t.model_copy() for t in self._tickets.values()]
        if user_id is not None:
            tickets = [t for t in tickets if t.user_id == user_id]
        # 倒序：最新的在前，便于人工优先处理
        tickets.sort(key=lambda t: t.created_at, reverse=True)
        return tickets

    def list_tickets_by_status(self, status: TicketStatus) -> list[Ticket]:
        """按状态过滤工单，便于值班人员分派处理。

        返回副本避免外部直接修改内部状态。
        """
        with self._lock:
            tickets = [
                ticket.model_copy() for ticket in self._tickets.values() if ticket.status == status
            ]
        tickets.sort(key=lambda t: t.created_at, reverse=True)
        return tickets

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------
    def update_status(self, ticket_id: str, status: TicketStatus) -> Ticket | None:
        """更新工单状态并刷新 updated_at。

        closed 为终态，已关闭工单拒绝状态回退，避免审计混乱。
        返回更新后的 Ticket；不存在或已关闭返回 None。
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return None
            # 已关闭工单不允许再变更状态，保证终态语义
            if ticket.status == TicketStatus.closed:
                logger.warning(
                    "工单已关闭，拒绝状态变更：ticket_id=%s target=%s",
                    ticket_id,
                    status.value,
                )
                return None
            ticket.status = status
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist(ticket)
            # 返回副本：避免外部持有引用后再次更新被覆盖
            return ticket.model_copy()

    # ------------------------------------------------------------------
    # 维护与诊断
    # ------------------------------------------------------------------
    def reset_all(self) -> None:
        """清空所有工单，主要用于测试隔离。"""
        with self._lock:
            self._tickets.clear()

    def count(self) -> int:
        """返回当前工单总数，供监控面板使用。"""
        with self._lock:
            return len(self._tickets)

    # ------------------------------------------------------------------
    # 内部工具与持久化钩子
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_ticket_id() -> str:
        """生成唯一工单 ID：前缀 + uuid4 十六进制。

        使用 hex 形式避免连字符，缩短长度同时保证唯一性。
        """
        return f"{TICKET_ID_PREFIX}-{uuid.uuid4().hex[:12]}"

    def _persist(self, ticket: Ticket) -> None:
        """持久化钩子：默认空实现，子类可覆写以写入 DB / Redis。

        在锁内调用，子类实现应避免长耗时 IO 阻塞其他请求。
        """
        # 默认内存存储：无额外动作
        return None

    def _load(self) -> None:
        """加载钩子：默认空实现，子类可在初始化时调用以恢复历史工单。"""
        return None


# 模块级单例：工单存储进程内复用，避免多处实例导致数据不一致
_ticket_store: TicketStore | None = None
# 单例创建锁：保证多线程首次获取单例时只创建一次
_singleton_lock = threading.Lock()


def get_ticket_store() -> TicketStore:
    """获取 TicketStore 单例。"""
    global _ticket_store
    if _ticket_store is None:
        with _singleton_lock:
            if _ticket_store is None:
                _ticket_store = TicketStore()
    return _ticket_store


def reset_ticket_store() -> None:
    """重置单例，便于测试隔离。"""
    global _ticket_store
    with _singleton_lock:
        _ticket_store = None
