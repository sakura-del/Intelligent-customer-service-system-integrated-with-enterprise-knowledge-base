"""Mock 业务系统 API。

无真实业务系统时，用进程内内存数据模拟订单/退换货/会员/账户查询，
供 BusinessAgent 调用。返回结构化 dict，由 Agent 负责脱敏与格式化。

线程安全：写操作经锁串行化，避免并发创建退换单号重复。
内存优化：数据量小且固定，初始化一次即可；测试可通过 reset 重建。
"""

from __future__ import annotations

import threading
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.agents.mock_business_api")


class MockBusinessAPI:
    """内存模拟业务系统。

    所有查询返回 dict 的浅拷贝，避免调用方修改污染内部数据。
    写操作（创建/取消退换货）加锁，保证退换单号生成原子。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 退换单号自增序列，加锁生成避免并发重复
        self._return_seq = 0
        self._init_data()

    def _init_data(self) -> None:
        """预置测试数据。

        覆盖多状态订单、多等级会员与账户，保证各查询分支可演示。
        """
        # 订单：状态覆盖 paid/shipped/delivered，验证不同格式化分支
        self._orders: dict[str, dict[str, Any]] = {
            "1234567890": {
                "order_id": "1234567890",
                "status": "shipped",
                "tracking_no": "SF1234567890",
                "amount": 199.00,
                "currency": "CNY",
                "items": ["无线蓝牙耳机"],
                "user_id": "U001",
                "created_at": "2026-06-25 10:00:00",
                "eta": "明天送达",
            },
            "9876543210": {
                "order_id": "9876543210",
                "status": "paid",
                "tracking_no": None,
                "amount": 599.50,
                "currency": "CNY",
                "items": ["智能手表"],
                "user_id": "U002",
                "created_at": "2026-06-28 09:30:00",
                "eta": None,
            },
            "1111222233": {
                "order_id": "1111222233",
                "status": "delivered",
                "tracking_no": "YT9876543210",
                "amount": 88.00,
                "currency": "CNY",
                "items": ["手机壳"],
                "user_id": "U001",
                "created_at": "2026-06-20 14:00:00",
                "eta": None,
            },
        }

        # 会员：含手机号/身份证等敏感字段，用于验证脱敏
        self._members: dict[str, dict[str, Any]] = {
            "U001": {
                "user_id": "U001",
                "name": "张三",
                "phone": "13812341234",
                "id_card": "110101199001011234",
                "points": 1250,
                "level": "金卡",
                "coupons": [
                    {"code": "C001", "amount": 50, "expire": "2026-12-31"},
                    {"code": "C002", "amount": 20, "expire": "2026-07-15"},
                ],
            },
            "U002": {
                "user_id": "U002",
                "name": "李四",
                "phone": "13987654321",
                "id_card": "310101198812120001",
                "points": 350,
                "level": "银卡",
                "coupons": [],
            },
        }

        # 账户：余额/账单/交易记录
        self._accounts: dict[str, dict[str, Any]] = {
            "U001": {
                "user_id": "U001",
                "balance": 500.00,
                "currency": "CNY",
                "bills": [
                    {"bill_id": "B202606", "amount": 199.00, "status": "已结清"},
                ],
                "transactions": [
                    {"tx_id": "T001", "type": "消费", "amount": -199.00, "time": "2026-06-25"},
                    {"tx_id": "T002", "type": "充值", "amount": 500.00, "time": "2026-06-20"},
                ],
            },
            "U002": {
                "user_id": "U002",
                "balance": 88.80,
                "currency": "CNY",
                "bills": [],
                "transactions": [
                    {"tx_id": "T003", "type": "消费", "amount": -599.50, "time": "2026-06-28"},
                ],
            },
        }

        # 退换货：初始为空，运行时由 create_return 写入
        self._returns: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 订单
    # ------------------------------------------------------------------
    def query_order(self, order_id: str) -> dict[str, Any] | None:
        """按订单号查询订单详情，不存在返回 None。"""
        order = self._orders.get(order_id)
        logger.info("查询订单 order_id=%s 命中=%s", order_id, order is not None)
        return dict(order) if order else None

    # ------------------------------------------------------------------
    # 会员
    # ------------------------------------------------------------------
    def query_member(self, user_id: str) -> dict[str, Any] | None:
        """按用户标识查询会员信息，不存在返回 None。"""
        member = self._members.get(user_id)
        logger.info("查询会员 user_id=%s 命中=%s", user_id, member is not None)
        return dict(member) if member else None

    # ------------------------------------------------------------------
    # 账户
    # ------------------------------------------------------------------
    def query_account(self, user_id: str) -> dict[str, Any] | None:
        """按用户标识查询账户信息，不存在返回 None。"""
        account = self._accounts.get(user_id)
        logger.info("查询账户 user_id=%s 命中=%s", user_id, account is not None)
        return dict(account) if account else None

    # ------------------------------------------------------------------
    # 退换货（写操作）
    # ------------------------------------------------------------------
    def create_return(
        self, order_id: str, reason: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """创建退换货申请。

        订单不存在时返回 None；同一订单已有进行中退换货则拒绝，
        避免重复申请造成业务数据混乱。单号原子生成防止并发重复。
        """
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                logger.warning("创建退换货失败：订单 %s 不存在", order_id)
                return None
            # 同一订单已有进行中退换货则拒绝，避免重复申请
            for existing in self._returns.values():
                if existing["order_id"] == order_id and existing["status"] == "pending":
                    logger.warning("订单 %s 已有进行中退换货 %s", order_id, existing["return_id"])
                    return None

            self._return_seq += 1
            return_id = f"R{self._return_seq:04d}"
            # pending 表示待审核，后续可由审核流程推进
            record: dict[str, Any] = {
                "return_id": return_id,
                "order_id": order_id,
                "reason": reason,
                "status": "pending",
                "user_id": user_id or order.get("user_id"),
                "created_at": "2026-06-29",
            }
            self._returns[return_id] = record
            logger.info("创建退换货 return_id=%s order_id=%s", return_id, order_id)
            return dict(record)

    def query_return(self, return_id: str) -> dict[str, Any] | None:
        """按退换单号查询退换货进度，不存在返回 None。"""
        record = self._returns.get(return_id)
        logger.info("查询退换货 return_id=%s 命中=%s", return_id, record is not None)
        return dict(record) if record else None

    def cancel_return(self, return_id: str) -> dict[str, Any] | None:
        """取消退换货申请。

        不存在或已终态（cancelled/approved）则不可取消，返回 None。
        """
        with self._lock:
            record = self._returns.get(return_id)
            if not record:
                logger.warning("取消退换货失败：%s 不存在", return_id)
                return None
            if record["status"] in ("cancelled", "approved"):
                logger.warning("取消退换货失败：%s 已终态 %s", return_id, record["status"])
                return None
            record["status"] = "cancelled"
            logger.info("取消退换货 return_id=%s", return_id)
            return dict(record)

    def list_returns_by_user(self, user_id: str) -> list[dict[str, Any]]:
        """查询用户的全部退换货记录，供取消场景回查。"""
        with self._lock:
            return [dict(r) for r in self._returns.values() if r.get("user_id") == user_id]

    def reset(self) -> None:
        """重置全部数据，便于测试隔离。"""
        with self._lock:
            self._return_seq = 0
            self._init_data()


# 模块级单例：API 无状态（除退换货写入），进程内复用
_business_api: MockBusinessAPI | None = None


def get_business_api() -> MockBusinessAPI:
    """获取 MockBusinessAPI 单例。"""
    global _business_api
    if _business_api is None:
        _business_api = MockBusinessAPI()
    return _business_api


def reset_business_api() -> None:
    """重置单例，便于测试切换数据。"""
    global _business_api
    _business_api = None
