"""业务系统适配器框架（Task 15）。

将 BusinessAgent 与具体业务系统实现解耦：通过抽象接口 + 工厂模式，
让 Agent 可在内存 mock 与真实 HTTP API 之间按配置切换，无需改代码。

模块构成：
- BusinessSystemAdapter：抽象基类，定义 Agent 调用业务系统的统一接口
- MockBusinessAdapter：包装现有 MockBusinessAPI，零成本接入 mock 数据
- HttpBusinessAdapter：通过 httpx 调用真实业务系统 REST API
- get_business_adapter()：工厂，按 Settings.BUSINESS_ADAPTER_MODE 返回实例

设计要点：
- 工厂在 http 模式下若 base_url 缺失自动降级 mock 并告警，保证启动不失败
- HTTP 适配器统一捕获超时/网络异常/非 2xx 响应，返回 None/[] 不抛异常，
  让 Agent 走"未找到/失败"分支给出友好提示，避免单次调用拖垮整条业务流程
- 适配器实例无状态（mock 共享底层 MockBusinessAPI 单例），进程内复用
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.agents.mock_business_api import MockBusinessAPI, get_business_api
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.agents.business_adapters")


class BusinessSystemAdapter(ABC):
    """业务系统适配器抽象基类。

    定义 BusinessAgent 调用业务系统的最小接口。子类负责对接具体后端
    （内存 mock / 真实 HTTP API），实现细节对 Agent 透明。
    所有方法返回结构化 dict 或 None/[]，由 Agent 负责脱敏与格式化。
    """

    # ----- 订单 -----
    @abstractmethod
    def query_order(self, order_id: str) -> dict[str, Any] | None:
        """按订单号查询订单详情，不存在返回 None。"""

    # ----- 退换货 -----
    @abstractmethod
    def create_return(
        self,
        order_id: str,
        reason: str,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """创建退换货申请，订单不存在或已有进行中申请时返回 None。"""

    @abstractmethod
    def cancel_return(self, return_id: str) -> dict[str, Any] | None:
        """取消退换货，不存在或已终态返回 None。"""

    @abstractmethod
    def query_return(self, return_id: str) -> dict[str, Any] | None:
        """按退换单号查询退换货详情，不存在返回 None。"""

    @abstractmethod
    def list_returns_by_user(self, user_id: str) -> list[dict[str, Any]]:
        """列出用户全部退换货记录，无记录返回空列表。"""

    # ----- 会员 -----
    @abstractmethod
    def query_member(self, user_id: str) -> dict[str, Any] | None:
        """按用户标识查询会员信息，不存在返回 None。"""

    # ----- 账户 -----
    @abstractmethod
    def query_account(self, user_id: str) -> dict[str, Any] | None:
        """按用户标识查询账户信息，不存在返回 None。"""

    @abstractmethod
    def list_transactions(self, user_id: str) -> list[dict[str, Any]]:
        """列出用户全部交易记录，无记录返回空列表。"""


class MockBusinessAdapter(BusinessSystemAdapter):
    """基于 MockBusinessAPI 的适配器。

    包装现有 mock 实现，使 BusinessAgent 通过统一接口调用 mock 数据。
    不持有独立状态，所有调用透传到底层 MockBusinessAPI 实例，
    保证测试注入的 mock 实例与 Agent 内部使用的是同一份共享状态。
    """

    def __init__(self, mock_api: MockBusinessAPI | None = None) -> None:
        # 允许注入自定义 mock 实例，便于测试隔离；默认用全局单例
        self._mock_api = mock_api or get_business_api()

    def query_order(self, order_id: str) -> dict[str, Any] | None:
        return self._mock_api.query_order(order_id)

    def create_return(
        self,
        order_id: str,
        reason: str,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._mock_api.create_return(order_id, reason, user_id)

    def cancel_return(self, return_id: str) -> dict[str, Any] | None:
        return self._mock_api.cancel_return(return_id)

    def query_return(self, return_id: str) -> dict[str, Any] | None:
        return self._mock_api.query_return(return_id)

    def list_returns_by_user(self, user_id: str) -> list[dict[str, Any]]:
        return self._mock_api.list_returns_by_user(user_id)

    def query_member(self, user_id: str) -> dict[str, Any] | None:
        return self._mock_api.query_member(user_id)

    def query_account(self, user_id: str) -> dict[str, Any] | None:
        return self._mock_api.query_account(user_id)

    def list_transactions(self, user_id: str) -> list[dict[str, Any]]:
        """从账户数据中抽取交易记录。

        mock 没有独立的交易接口，交易记录是账户数据的子字段，
        这里复用 query_account 提取，保持与 HTTP 适配器接口一致。
        """
        account = self._mock_api.query_account(user_id)
        if not account:
            return []
        # 返回浅拷贝列表避免调用方修改污染 mock 内部账户数据
        transactions = account.get("transactions") or []
        return [dict(tx) if isinstance(tx, dict) else tx for tx in transactions]


class HttpBusinessAdapter(BusinessSystemAdapter):
    """HTTP 真实业务系统适配器。

    通过 httpx 调用真实业务系统 REST API，端点映射约定：
      GET    {base}/orders/{order_id}                  → query_order
      POST   {base}/returns                            → create_return
      POST   {base}/returns/{return_id}/cancel         → cancel_return
      GET    {base}/returns/{return_id}                → query_return
      GET    {base}/returns?user_id={user_id}          → list_returns_by_user
      GET    {base}/members/{user_id}                  → query_member
      GET    {base}/accounts/{user_id}                 → query_account
      GET    {base}/accounts/{user_id}/transactions    → list_transactions

    错误处理策略：
    - 4xx/5xx 响应、超时、网络异常、JSON 解析失败统一返回 None/[]
    - 不抛异常中断业务流程，日志记录详细错误便于运维排查
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # 去掉末尾斜杠避免拼接出双斜杠导致路由 404
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        # transport 仅用于测试注入 MockTransport；生产为 None 走默认真实传输
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        """构造请求头。API Key 非空时携带鉴权头。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """统一发送 HTTP 请求并解析 JSON 响应。

        网络错误/超时/非 2xx 状态码统一返回 None，避免业务流程被异常打断。
        日志记录详细原因便于排查，调用方按 None 走"未找到/失败"分支。
        """
        url = f"{self._base_url}{path}"
        try:
            # 每次请求新建 Client 以避免长连接复用导致的连接老化问题；
            # httpx 内部会管理连接池，开销可控
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.warning("业务系统 HTTP 调用失败 %s %s: %s", method, url, exc)
            return None
        except Exception as exc:
            # 兜底捕获未知异常（如 SSL 握手失败），避免任何意外中断流程
            logger.warning("业务系统 HTTP 异常 %s %s: %s", method, url, exc)
            return None

        # 4xx/5xx 视为业务失败，返回 None 让 Agent 走"未找到/失败"分支
        if response.status_code >= 400:
            logger.warning(
                "业务系统 HTTP %s %s 返回 %s: %s",
                method,
                url,
                response.status_code,
                response.text[:200],
            )
            return None

        try:
            return response.json()
        except Exception as exc:
            logger.warning("业务系统响应 JSON 解析失败 %s %s: %s", method, url, exc)
            return None

    # ----- 订单 -----
    def query_order(self, order_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/orders/{order_id}")

    # ----- 退换货 -----
    def create_return(
        self,
        order_id: str,
        reason: str,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {"order_id": order_id, "reason": reason}
        if user_id:
            body["user_id"] = user_id
        return self._request("POST", "/returns", json_body=body)

    def cancel_return(self, return_id: str) -> dict[str, Any] | None:
        return self._request("POST", f"/returns/{return_id}/cancel")

    def query_return(self, return_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/returns/{return_id}")

    def list_returns_by_user(self, user_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/returns", params={"user_id": user_id})
        if not data:
            return []
        # 兼容两种返回结构：{returns: [...]} 或直接数组
        if isinstance(data, list):
            return data
        return list(data.get("returns") or [])

    # ----- 会员 -----
    def query_member(self, user_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/members/{user_id}")

    # ----- 账户 -----
    def query_account(self, user_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/accounts/{user_id}")

    def list_transactions(self, user_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/accounts/{user_id}/transactions")
        if not data:
            return []
        if isinstance(data, list):
            return data
        return list(data.get("transactions") or [])


# 模块级单例：适配器无状态，进程内复用
_business_adapter: BusinessSystemAdapter | None = None


def get_business_adapter() -> BusinessSystemAdapter:
    """工厂方法：按 Settings.BUSINESS_ADAPTER_MODE 返回对应适配器实例。

    - mock（默认）：返回 MockBusinessAdapter
    - http：返回 HttpBusinessAdapter；若 base_url 未配置则降级 mock 并告警

    单例缓存，进程内只创建一次；测试可通过 reset_business_adapter 重建。
    """
    global _business_adapter
    if _business_adapter is not None:
        return _business_adapter

    settings = get_settings()
    mode = (settings.BUSINESS_ADAPTER_MODE or "mock").lower().strip()

    if mode == "http":
        if not settings.BUSINESS_API_BASE_URL:
            # 配置缺失自动降级 mock，保证服务可用，避免启动失败
            logger.warning(
                "BUSINESS_ADAPTER_MODE=http 但 BUSINESS_API_BASE_URL 未配置，"
                "自动降级到 MockBusinessAdapter"
            )
            _business_adapter = MockBusinessAdapter()
        else:
            logger.info(
                "使用 HttpBusinessAdapter base_url=%s timeout=%s",
                settings.BUSINESS_API_BASE_URL,
                settings.BUSINESS_API_TIMEOUT,
            )
            _business_adapter = HttpBusinessAdapter(
                base_url=settings.BUSINESS_API_BASE_URL,
                api_key=settings.BUSINESS_API_KEY,
                timeout=float(settings.BUSINESS_API_TIMEOUT),
            )
    else:
        # 默认 mock 模式，开箱即用
        _business_adapter = MockBusinessAdapter()

    return _business_adapter


def reset_business_adapter() -> None:
    """重置单例，便于测试切换配置后重建适配器。"""
    global _business_adapter
    _business_adapter = None
