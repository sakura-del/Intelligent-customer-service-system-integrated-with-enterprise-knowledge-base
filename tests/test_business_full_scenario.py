"""业务系统适配器全场景测试（Task 15 / SubTask 15.1）。

覆盖目标：
- MockBusinessAdapter 包装 MockBusinessAPI 行为正确
- HttpBusinessAdapter 通过 httpx.MockTransport 验证端点映射与错误降级
- get_business_adapter() 工厂按配置返回 mock/http，http 配置缺失时降级
- BusinessAgent 经适配器接入端到端全场景：
  * 订单查询（状态/物流/金额）
  * 退换货（创建→查询→取消，含二次确认）
  * 会员信息（积分/等级/优惠券）
  * 账户查询（余额/账单/交易记录）
  * 错误场景（订单不存在、未登录、频率限制）

测试隔离：每个用例独立构造 adapter/agent/session/llm，并重置工厂单例。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

from app.agents.business_adapters import (
    BusinessSystemAdapter,
    HttpBusinessAdapter,
    MockBusinessAdapter,
    get_business_adapter,
    reset_business_adapter,
)
from app.agents.business_agent import BusinessAgent
from app.agents.mock_business_api import MockBusinessAPI
from app.core.session import SessionManager
from app.schemas.business import BusinessScene


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class FakeLLM:
    """可控 LLM 替身：is_mock=True 时模拟离线 mock，走规则+模板。"""

    def __init__(self, is_mock: bool = True) -> None:
        self._is_mock = is_mock

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        **kwargs: Any,
    ) -> str:
        return ""


# ----------------------------------------------------------------------
# 公共 fixture
# ----------------------------------------------------------------------
@pytest.fixture
def mock_adapter() -> MockBusinessAdapter:
    """每个测试独立的 mock 适配器，底层 MockBusinessAPI 为新实例。"""
    return MockBusinessAdapter(MockBusinessAPI())


@pytest.fixture
def env(mock_adapter):
    """组装独立环境：mock 适配器 + 已登录会话 + mock LLM。"""
    sm = SessionManager()
    llm = FakeLLM(is_mock=True)
    agent = BusinessAgent(
        business_adapter=mock_adapter,
        session_manager_ref=sm,
        llm_client=llm,
    )
    yield agent, mock_adapter, sm, llm
    agent.reset_rate_limit()
    agent.clear_all_pending()


@pytest.fixture
def reset_factory():
    """测试前后重置工厂单例，避免 http/mock 模式互相污染。"""
    reset_business_adapter()
    yield
    reset_business_adapter()


def _login(sm: SessionManager, user_id: str = "U001") -> str:
    return sm.create_session(channel="web", user_id=user_id)


def _guest(sm: SessionManager) -> str:
    return sm.create_session(channel="web", user_id=None)


# ======================================================================
# 一、MockBusinessAdapter 单元测试
# ======================================================================
class TestMockAdapter:
    """验证 MockBusinessAdapter 正确包装 MockBusinessAPI。"""

    def test_query_order_hit(self, mock_adapter):
        """存在的订单应返回 dict 且不污染内部状态。"""
        order = mock_adapter.query_order("1234567890")
        assert order is not None
        assert order["order_id"] == "1234567890"
        assert order["status"] == "shipped"
        # 修改返回值不影响内部数据
        order["status"] = "tampered"
        again = mock_adapter.query_order("1234567890")
        assert again["status"] == "shipped"

    def test_query_order_miss(self, mock_adapter):
        """不存在的订单返回 None。"""
        assert mock_adapter.query_order("0000000000") is None

    def test_query_member_hit(self, mock_adapter):
        """会员查询返回包含敏感字段的原始数据（脱敏由 Agent 负责）。"""
        member = mock_adapter.query_member("U001")
        assert member is not None
        assert member["level"] == "金卡"
        assert member["phone"] == "13812341234"

    def test_query_account_hit(self, mock_adapter):
        """账户查询返回包含余额/账单/交易记录的完整结构。"""
        account = mock_adapter.query_account("U001")
        assert account is not None
        assert account["balance"] == 500.00
        assert len(account["bills"]) == 1
        assert len(account["transactions"]) == 2

    def test_list_transactions_from_account(self, mock_adapter):
        """list_transactions 从账户数据抽取交易记录。"""
        txs = mock_adapter.list_transactions("U001")
        assert len(txs) == 2
        assert txs[0]["tx_id"] == "T001"
        # 用户不存在返回空列表
        assert mock_adapter.list_transactions("UNKNOWN") == []

    def test_create_and_cancel_return_flow(self, mock_adapter):
        """创建退换货 → 查询 → 取消 全流程经适配器。"""
        record = mock_adapter.create_return("1234567890", "质量问题", "U001")
        assert record is not None
        return_id = record["return_id"]
        assert record["status"] == "pending"

        # 查询命中
        got = mock_adapter.query_return(return_id)
        assert got is not None
        assert got["order_id"] == "1234567890"

        # 取消成功
        cancelled = mock_adapter.cancel_return(return_id)
        assert cancelled["status"] == "cancelled"

    def test_create_return_rejects_unknown_order(self, mock_adapter):
        """订单不存在时创建退换货返回 None。"""
        assert mock_adapter.create_return("0000000000", "x") is None

    def test_create_return_rejects_duplicate_pending(self, mock_adapter):
        """同一订单已有 pending 退换货时拒绝再次创建。"""
        first = mock_adapter.create_return("1234567890", "质量问题", "U001")
        assert first is not None
        # 同订单再次创建应被拒
        second = mock_adapter.create_return("1234567890", "其他原因", "U001")
        assert second is None

    def test_cancel_return_rejects_unknown(self, mock_adapter):
        """不存在的退换单号取消返回 None。"""
        assert mock_adapter.cancel_return("R9999") is None

    def test_list_returns_by_user_empty(self, mock_adapter):
        """无退换货记录的用户返回空列表。"""
        assert mock_adapter.list_returns_by_user("U001") == []

    def test_list_returns_by_user_after_create(self, mock_adapter):
        """创建后退换货应出现在用户列表中。"""
        mock_adapter.create_return("1111222233", "质量问题", "U001")
        records = mock_adapter.list_returns_by_user("U001")
        assert len(records) == 1
        assert records[0]["order_id"] == "1111222233"

    def test_implements_abstract_interface(self, mock_adapter):
        """MockBusinessAdapter 实现抽象基类全部方法。"""
        assert isinstance(mock_adapter, BusinessSystemAdapter)


# ======================================================================
# 二、HttpBusinessAdapter 单元测试（用 httpx.MockTransport）
# ======================================================================
class TestHttpAdapter:
    """验证 HttpBusinessAdapter 端点映射与错误降级。"""

    def _make_adapter(self, handler, api_key="test-key"):
        """构造带 MockTransport 的 HTTP 适配器。"""
        transport = httpx.MockTransport(handler)
        return HttpBusinessAdapter(
            base_url="https://biz.test",
            api_key=api_key,
            timeout=5.0,
            transport=transport,
        )

    def test_query_order_success(self):
        """GET /orders/{id} 成功返回 JSON。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["api_key"] = request.headers.get("X-API-Key")
            return httpx.Response(
                200,
                json={"order_id": "1234567890", "status": "shipped"},
            )

        adapter = self._make_adapter(handler)
        order = adapter.query_order("1234567890")
        assert order == {"order_id": "1234567890", "status": "shipped"}
        assert captured["method"] == "GET"
        assert captured["url"] == "https://biz.test/orders/1234567890"
        # API Key 写入鉴权头
        assert captured["api_key"] == "test-key"

    def test_create_return_posts_body(self):
        """POST /returns 携带 order_id/reason/user_id。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={"return_id": "R0001", "status": "pending"},
            )

        adapter = self._make_adapter(handler)
        result = adapter.create_return("1234567890", "质量问题", "U001")
        assert result == {"return_id": "R0001", "status": "pending"}
        assert captured["method"] == "POST"
        assert captured["url"] == "https://biz.test/returns"
        assert captured["body"] == {
            "order_id": "1234567890",
            "reason": "质量问题",
            "user_id": "U001",
        }

    def test_create_return_omits_empty_user_id(self):
        """user_id 为空时 body 不含 user_id 字段。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"return_id": "R0001"})

        adapter = self._make_adapter(handler)
        adapter.create_return("1234567890", "质量问题", None)
        assert "user_id" not in captured["body"]

    def test_cancel_return_posts_to_cancel_endpoint(self):
        """取消退换货走 POST /returns/{id}/cancel。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"return_id": "R0001", "status": "cancelled"})

        adapter = self._make_adapter(handler)
        result = adapter.cancel_return("R0001")
        assert result["status"] == "cancelled"
        assert captured["method"] == "POST"
        assert captured["url"] == "https://biz.test/returns/R0001/cancel"

    def test_query_return_get_single(self):
        """查询退换货走 GET /returns/{id}。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"return_id": "R0001", "status": "pending"})

        adapter = self._make_adapter(handler)
        result = adapter.query_return("R0001")
        assert result["return_id"] == "R0001"
        assert captured["url"] == "https://biz.test/returns/R0001"

    def test_list_returns_by_user_with_params(self):
        """list_returns_by_user 携带 user_id 查询参数。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={"returns": [{"return_id": "R0001", "order_id": "1234567890"}]},
            )

        adapter = self._make_adapter(handler)
        records = adapter.list_returns_by_user("U001")
        assert len(records) == 1
        assert records[0]["return_id"] == "R0001"
        # 查询参数已附加
        assert "user_id=U001" in captured["url"]

    def test_list_returns_accepts_raw_array(self):
        """list_returns 兼容后端直接返回数组的结构。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"return_id": "R0001"}])

        adapter = self._make_adapter(handler)
        records = adapter.list_returns_by_user("U001")
        assert records == [{"return_id": "R0001"}]

    def test_query_member_get(self):
        """查询会员走 GET /members/{user_id}。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"user_id": "U001", "level": "金卡"})

        adapter = self._make_adapter(handler)
        member = adapter.query_member("U001")
        assert member["level"] == "金卡"
        assert captured["url"] == "https://biz.test/members/U001"

    def test_query_account_get(self):
        """查询账户走 GET /accounts/{user_id}。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"balance": 500.0})

        adapter = self._make_adapter(handler)
        account = adapter.query_account("U001")
        assert account["balance"] == 500.0

    def test_list_transactions_get(self):
        """list_transactions 走 GET /accounts/{user_id}/transactions。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=[{"tx_id": "T001"}])

        adapter = self._make_adapter(handler)
        txs = adapter.list_transactions("U001")
        assert txs == [{"tx_id": "T001"}]
        assert captured["url"] == "https://biz.test/accounts/U001/transactions"

    def test_timeout_returns_none(self):
        """超时不抛异常，返回 None。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout")

        adapter = self._make_adapter(handler)
        assert adapter.query_order("123") is None
        # 写操作同样降级
        assert adapter.create_return("123", "x", "U001") is None
        # 列表操作降级为空列表
        assert adapter.list_returns_by_user("U001") == []
        assert adapter.list_transactions("U001") == []

    def test_network_error_returns_none(self):
        """网络异常不抛异常，返回 None。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated connection refused")

        adapter = self._make_adapter(handler)
        assert adapter.query_order("123") is None

    def test_4xx_returns_none(self):
        """4xx 视为业务失败返回 None。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        adapter = self._make_adapter(handler)
        assert adapter.query_order("xxx") is None

    def test_5xx_returns_none(self):
        """5xx 服务端错误返回 None。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal server error")

        adapter = self._make_adapter(handler)
        assert adapter.query_member("U001") is None

    def test_invalid_json_returns_none(self):
        """响应非 JSON 返回 None 不抛异常。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        adapter = self._make_adapter(handler)
        assert adapter.query_order("123") is None

    def test_no_api_key_header_when_empty(self):
        """API Key 为空时不写鉴权头。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["api_key"] = request.headers.get("X-API-Key")
            return httpx.Response(200, json={})

        adapter = self._make_adapter(handler, api_key="")
        adapter.query_order("123")
        assert captured["api_key"] is None

    def test_trailing_slash_in_base_url_normalized(self):
        """base_url 末尾斜杠被去掉，避免拼接双斜杠。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        adapter = HttpBusinessAdapter(
            base_url="https://biz.test/",
            transport=transport,
        )
        adapter.query_order("123")
        assert captured["url"] == "https://biz.test/orders/123"


# ======================================================================
# 三、工厂方法测试
# ======================================================================
class TestFactory:
    """验证 get_business_adapter 按 Settings 返回对应实例。"""

    def test_default_returns_mock(self, reset_factory):
        """默认配置（无环境变量）返回 MockBusinessAdapter。"""
        adapter = get_business_adapter()
        assert isinstance(adapter, MockBusinessAdapter)

    def test_http_mode_uses_http_adapter(self, monkeypatch, reset_factory):
        """http 模式且 base_url 配置齐全时返回 HttpBusinessAdapter。"""
        from app.agents import business_adapters

        class FakeSettings:
            BUSINESS_ADAPTER_MODE = "http"
            BUSINESS_API_BASE_URL = "https://biz.test"
            BUSINESS_API_KEY = "k"
            BUSINESS_API_TIMEOUT = 5

        monkeypatch.setattr(business_adapters, "get_settings", lambda: FakeSettings())
        adapter = get_business_adapter()
        assert isinstance(adapter, HttpBusinessAdapter)

    def test_http_mode_falls_back_when_no_url(self, monkeypatch, reset_factory):
        """http 模式但 base_url 缺失时降级 MockBusinessAdapter。"""
        from app.agents import business_adapters

        class FakeSettings:
            BUSINESS_ADAPTER_MODE = "http"
            BUSINESS_API_BASE_URL = ""
            BUSINESS_API_KEY = ""
            BUSINESS_API_TIMEOUT = 10

        monkeypatch.setattr(business_adapters, "get_settings", lambda: FakeSettings())
        adapter = get_business_adapter()
        assert isinstance(adapter, MockBusinessAdapter)

    def test_unknown_mode_defaults_to_mock(self, monkeypatch, reset_factory):
        """非法模式值兜底为 mock。"""
        from app.agents import business_adapters

        class FakeSettings:
            BUSINESS_ADAPTER_MODE = "weird-mode"
            BUSINESS_API_BASE_URL = ""
            BUSINESS_API_KEY = ""
            BUSINESS_API_TIMEOUT = 10

        monkeypatch.setattr(business_adapters, "get_settings", lambda: FakeSettings())
        adapter = get_business_adapter()
        assert isinstance(adapter, MockBusinessAdapter)

    def test_singleton_cached(self, reset_factory):
        """工厂返回单例，重复调用拿到同一实例。"""
        a1 = get_business_adapter()
        a2 = get_business_adapter()
        assert a1 is a2


# ======================================================================
# 四、BusinessAgent 全场景（经适配器接入）
# ======================================================================
class TestFullScenario:
    """端到端验证 BusinessAgent 通过适配器完成全场景。"""

    # ----- 订单查询：状态/物流/金额 -----
    def test_order_status(self, env):
        """订单状态查询返回 shipped 状态。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查订单1234567890状态", sid)
        assert result.success is True
        assert result.scene == BusinessScene.ORDER
        assert result.data["status"] == "shipped"
        assert "1234567890" in result.reply
        assert "已发货" in result.reply

    def test_order_logistics(self, env):
        """订单物流查询返回物流单号与预计送达。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查订单1234567890的物流", sid)
        assert result.success is True
        assert "SF1234567890" in result.reply
        assert "明天送达" in result.reply

    def test_order_amount(self, env):
        """订单金额查询返回订单金额。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("订单1234567890多少钱", sid)
        assert result.success is True
        assert "199.00" in result.reply

    def test_order_paid_status(self, env):
        """paid 状态订单格式化为"已支付，待发货"。"""
        agent, _, sm, _ = env
        sid = _login(sm, "U002")
        result = agent.execute("查订单9876543210", sid)
        assert result.success is True
        assert "已支付，待发货" in result.reply
        assert "599.50" in result.reply

    # ----- 退换货：创建→查询→取消，含二次确认 -----
    def test_return_full_flow_with_confirmation(self, env):
        """创建→确认→查询→取消 全流程含二次确认。"""
        agent, adapter, sm, _ = env
        sid = _login(sm)

        # 1. 发起退货 → 待确认
        r1 = agent.execute("我要退货订单1111222233，原因质量问题", sid)
        assert r1.need_confirmation is True
        assert r1.confirmation_token
        assert "确认" in r1.reply
        # 确认前适配器中无记录
        assert adapter.query_return("R0001") is None

        # 2. 确认 → 真正创建
        r2 = agent.execute("确认", sid)
        assert r2.success is True
        assert "已创建" in r2.reply
        assert "R0001" in r2.reply
        record = adapter.query_return("R0001")
        assert record is not None
        assert record["order_id"] == "1111222233"
        assert record["reason"] == "质量问题"
        assert record["status"] == "pending"

        # 3. 查询退换货
        r3 = agent.execute("查退换货单R0001", sid)
        assert r3.success is True
        assert r3.scene == BusinessScene.RETURN
        assert "R0001" in r3.reply

        # 4. 取消退换货 → 待确认
        r4 = agent.execute("取消退换货R0001", sid)
        assert r4.need_confirmation is True

        # 5. 确认取消
        r5 = agent.execute("确认", sid)
        assert r5.success is True
        cancelled = adapter.query_return("R0001")
        assert cancelled["status"] == "cancelled"

    def test_return_create_then_user_cancels(self, env):
        """发起退货后用户回复"取消"放弃操作，不应创建记录。"""
        agent, adapter, sm, _ = env
        sid = _login(sm)

        r1 = agent.execute("我要退货订单1111222233", sid)
        assert r1.need_confirmation is True

        r2 = agent.execute("取消", sid)
        assert "取消" in r2.reply
        assert adapter.query_return("R0001") is None

    def test_return_query_list(self, env):
        """查询退换货列表返回用户全部记录。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        # 先创建一条
        agent.execute("我要退货订单1111222233", sid)
        agent.execute("确认", sid)

        result = agent.execute("查我的退换货记录", sid)
        assert result.success is True
        assert "R0001" in result.reply

    def test_return_cancel_unknown(self, env):
        """取消不存在的退换货单：二次确认后失败提示。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        r1 = agent.execute("取消退换货R9999", sid)
        assert r1.need_confirmation is True
        r2 = agent.execute("确认", sid)
        assert r2.success is False
        assert r2.error == "cancel_failed"

    # ----- 会员信息：积分/等级/优惠券 -----
    def test_member_points(self, env):
        """会员积分查询返回积分值。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查我的会员积分", sid)
        assert result.success is True
        assert "1250" in result.reply
        # 手机号脱敏后展示
        assert result.data["phone"] == "138****1234"
        assert "13812341234" not in result.reply

    def test_member_level(self, env):
        """会员等级查询返回等级。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("我的会员等级", sid)
        assert result.success is True
        assert "金卡" in result.reply

    def test_member_coupons(self, env):
        """会员优惠券查询返回优惠券列表。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("我有哪些优惠券", sid)
        assert result.success is True
        assert "C001" in result.reply
        assert "50" in result.reply  # 优惠券金额

    def test_member_id_card_masked(self, env):
        """身份证字段被脱敏，原始 mock 数据不受影响。"""
        agent, adapter, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查我的会员信息", sid)
        assert result.data["id_card"].startswith("110101")
        assert result.data["id_card"].endswith("1234")
        assert "*" in result.data["id_card"]
        # 原始数据未被污染
        assert adapter._mock_api._members["U001"]["id_card"] == "110101199001011234"

    # ----- 账户查询：余额/账单/交易记录 -----
    def test_account_balance(self, env):
        """账户余额查询返回余额。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查我的账户余额", sid)
        assert result.success is True
        assert "500.00" in result.reply

    def test_account_bill(self, env):
        """账户账单查询返回账单列表。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查我的账单", sid)
        assert result.success is True
        assert "B202606" in result.reply
        assert "已结清" in result.reply

    def test_account_transactions(self, env):
        """账户交易记录查询返回交易明细。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查我的交易记录", sid)
        assert result.success is True
        # 模板按 type/amount/time 拼装，tx_id 仅在 data 中
        assert "消费" in result.reply
        assert "充值" in result.reply
        assert "2026-06-25" in result.reply
        assert result.data["transactions"][0]["tx_id"] == "T001"

    # ----- 错误场景 -----
    def test_order_not_found(self, env):
        """订单不存在返回友好提示且 success=False。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("查订单0000000000", sid)
        assert result.success is False
        assert result.error == "order_not_found"
        assert "0000000000" in result.reply

    def test_not_logged_in_member(self, env):
        """会员查询未登录提示先登录。"""
        agent, _, sm, _ = env
        sid = _guest(sm)
        result = agent.execute("查我的会员积分", sid)
        assert result.success is False
        assert result.error == "not_logged_in"
        assert "登录" in result.reply

    def test_not_logged_in_return_create(self, env):
        """退换货写操作未登录提示先登录。"""
        agent, _, sm, _ = env
        sid = _guest(sm)
        result = agent.execute("我要退货订单1234567890", sid)
        assert result.success is False
        assert result.error == "not_logged_in"

    def test_not_logged_in_account(self, env):
        """账户查询未登录提示先登录。"""
        agent, _, sm, _ = env
        sid = _guest(sm)
        result = agent.execute("查我的账户余额", sid)
        assert result.success is False
        assert result.error == "not_logged_in"

    def test_rate_limit_triggers(self, env):
        """同一 session 10 秒内超过 5 次调用被限流。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        for _ in range(5):
            r = agent.execute("查订单1234567890", sid)
            assert r.error != "rate_limited"
        # 第 6 次限流
        r = agent.execute("查订单1234567890", sid)
        assert r.error == "rate_limited"
        assert r.success is False
        assert "频繁" in r.reply

    def test_rate_limit_isolated_sessions(self, env):
        """不同 session 频率计数互不影响。"""
        agent, _, sm, _ = env
        sid1 = _login(sm, "U001")
        sid2 = _login(sm, "U002")
        for _ in range(5):
            agent.execute("查订单1234567890", sid1)
        # sid2 仍可调用
        r = agent.execute("查订单9876543210", sid2)
        assert r.error != "rate_limited"

    def test_unknown_scene_returns_guide(self, env):
        """无法识别场景返回引导提示。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("今天天气怎么样", sid)
        assert result.success is False
        assert result.error == "unknown_scene"

    def test_empty_query(self, env):
        """空查询返回提示。"""
        agent, _, sm, _ = env
        sid = _login(sm)
        result = agent.execute("", sid)
        assert result.success is False
        assert result.error == "empty_query"


# ======================================================================
# 五、BusinessAgent 经 HTTP 适配器接入的端到端（带 MockTransport）
# ======================================================================
class TestAgentWithHttpAdapter:
    """验证 BusinessAgent 可无缝切换到 HttpBusinessAdapter。

    用 httpx.MockTransport 模拟真实业务系统响应，证明 Agent 代码无需
    任何改动即可工作在 http 模式下，验证框架的解耦能力。
    """

    def test_agent_via_http_adapter_order_query(self):
        """Agent 通过 HttpBusinessAdapter 完成订单查询。"""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "/orders/" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "order_id": "1234567890",
                        "status": "shipped",
                        "tracking_no": "SF1234567890",
                        "amount": 199.00,
                        "eta": "明天送达",
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = HttpBusinessAdapter(
            base_url="https://biz.test",
            api_key="k",
            timeout=5.0,
            transport=transport,
        )
        sm = SessionManager()
        llm = FakeLLM(is_mock=True)
        agent = BusinessAgent(
            business_adapter=adapter,
            session_manager_ref=sm,
            llm_client=llm,
        )
        sid = sm.create_session(channel="web", user_id="U001")
        result = agent.execute("查订单1234567890", sid)
        assert result.success is True
        assert result.data["order_id"] == "1234567890"
        assert "已发货" in result.reply

    def test_agent_via_http_adapter_order_not_found(self):
        """HTTP 适配器返回 None 时 Agent 走"未找到"分支。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        transport = httpx.MockTransport(handler)
        adapter = HttpBusinessAdapter(
            base_url="https://biz.test",
            transport=transport,
        )
        sm = SessionManager()
        agent = BusinessAgent(
            business_adapter=adapter,
            session_manager_ref=sm,
            llm_client=FakeLLM(is_mock=True),
        )
        sid = sm.create_session(channel="web", user_id="U001")
        result = agent.execute("查订单0000000000", sid)
        assert result.success is False
        assert result.error == "order_not_found"

    def test_agent_via_http_adapter_timeout_degrades(self):
        """HTTP 适配器超时时 Agent 返回 order_not_found，不抛异常中断。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout")

        transport = httpx.MockTransport(handler)
        adapter = HttpBusinessAdapter(
            base_url="https://biz.test",
            transport=transport,
        )
        sm = SessionManager()
        agent = BusinessAgent(
            business_adapter=adapter,
            session_manager_ref=sm,
            llm_client=FakeLLM(is_mock=True),
        )
        sid = sm.create_session(channel="web", user_id="U001")
        # 不应抛异常，应返回友好失败结果
        result = agent.execute("查订单1234567890", sid)
        assert result.success is False
        assert result.error == "order_not_found"


# ======================================================================
# 六、全场景演示（-s 时输出）
# ======================================================================
def test_demo_full_scenario(env, capsys):
    """演示：mock 模式下完整业务流程，含格式化回复样例。

    演示分多个场景段落，每段前重置频率限制计数器避免累计触发限流，
    真实生产中不同段落相当于不同用户会话，限流本应独立计数。
    """
    agent, _, sm, _ = env
    sid = _login(sm)

    print("\n========== 业务系统适配器全场景演示 ==========")

    # 演示 1：订单查询（状态/物流/金额）
    agent.reset_rate_limit()
    r = agent.execute("查订单1234567890的物流", sid)
    print(f"[订单-物流] reply: {r.reply}")
    print(f"[订单-物流] data: {r.data}")
    assert r.success and "已发货" in r.reply

    # 演示 2：退换货（创建→确认→查询→取消）
    agent.reset_rate_limit()
    r1 = agent.execute("我要退货订单1111222233，原因质量问题", sid)
    print(f"[退货-待确认] reply: {r1.reply}")
    assert r1.need_confirmation is True
    r2 = agent.execute("确认", sid)
    print(f"[退货-已创建] reply: {r2.reply}")
    r3 = agent.execute("查退换货单R0001", sid)
    print(f"[退货-查询] reply: {r3.reply}")
    assert "R0001" in r3.reply

    # 演示 3：会员信息（积分/等级/优惠券）
    agent.reset_rate_limit()
    r = agent.execute("查我的会员积分", sid)
    print(f"[会员-积分] reply: {r.reply} phone={r.data.get('phone')}")
    assert r.data["phone"] == "138****1234"

    # 演示 4：账户查询（余额/账单/交易记录）
    agent.reset_rate_limit()
    r = agent.execute("查我的账户余额", sid)
    print(f"[账户-余额] reply: {r.reply}")
    assert "500.00" in r.reply
    r = agent.execute("查我的交易记录", sid)
    print(f"[账户-交易] reply: {r.reply}")

    # 演示 5：错误场景
    agent.reset_rate_limit()
    r = agent.execute("查订单0000000000", sid)
    print(f"[错误-订单不存在] reply: {r.reply}")
    assert r.error == "order_not_found"

    print("=============================================")
