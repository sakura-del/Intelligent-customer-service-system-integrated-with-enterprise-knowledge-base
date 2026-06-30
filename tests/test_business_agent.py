"""业务查询 Agent 测试。

覆盖 SubTask 10.1/10.2/10.3：
- 参数提取（规则兜底 + LLM 结构化）
- 订单查询 → 模板格式化回复
- 退换货创建 → 二次确认流程（先 pending，确认后执行）
- 手机号/身份证脱敏
- 频率限制
- 未登录提示

测试隔离：每个用例独立构造 agent/api/session/llm，避免单例状态互相污染。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.agents.business_agent import BusinessAgent
from app.agents.mock_business_api import MockBusinessAPI
from app.core.session import SessionManager
from app.schemas.business import BusinessScene


class FakeLLM:
    """可控 LLM 替身。

    is_mock=True 时模拟离线 mock（走规则+模板）；
    is_mock=False 时按 responses 队列返回预设内容，模拟真实 LLM。
    """

    def __init__(self, is_mock: bool = True, responses: List[str] | None = None) -> None:
        self._is_mock = is_mock
        self.responses = list(responses or [])
        self.calls: List[List[Dict[str, Any]]] = []

    @property
    def is_mock(self) -> bool:
        return self._is_mock

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        **kwargs: Any,
    ) -> str:
        self.calls.append(messages)
        if self.responses:
            return self.responses.pop(0)
        return ""


@pytest.fixture
def env():
    """每个测试独立环境，避免单例与计数器污染。"""
    api = MockBusinessAPI()
    sm = SessionManager()
    llm = FakeLLM(is_mock=True)  # 默认走规则提取 + 模板格式化
    agent = BusinessAgent(business_api=api, session_manager_ref=sm, llm_client=llm)
    yield agent, api, sm, llm
    agent.reset_rate_limit()
    agent.clear_all_pending()


def _login_session(sm: SessionManager, user_id: str = "U001") -> str:
    """创建已登录会话。"""
    return sm.create_session(channel="web", user_id=user_id)


def _guest_session(sm: SessionManager) -> str:
    """创建未登录会话。"""
    return sm.create_session(channel="web", user_id=None)


# ----------------------------------------------------------------------
# SubTask 10.1 参数提取
# ----------------------------------------------------------------------
def test_extract_params_by_rules(env):
    """规则兜底应从对话提取订单号与查询类型。"""
    agent, *_ = env
    params = agent._extract_params("查一下订单1234567890的物流", {})
    assert params is not None
    assert params.scene == BusinessScene.ORDER
    assert params.order_id == "1234567890"
    assert params.query_type == "logistics"


def test_extract_params_phone_not_order(env):
    """11 位手机号不应被误识别为订单号。"""
    agent, *_ = env
    params = agent._extract_params("我的手机号13812341234查会员", {})
    assert params is not None
    assert params.scene == BusinessScene.MEMBER
    assert params.phone == "13812341234"
    # 手机号不应同时被当成订单号
    assert params.order_id is None


def test_extract_params_by_llm():
    """LLM 模式应解析结构化 JSON 并覆盖规则结果。"""
    api = MockBusinessAPI()
    sm = SessionManager()
    llm = FakeLLM(
        is_mock=False,
        responses=[
            '{"scene":"order","order_id":"9876543210","query_type":"amount","action":"query"}'
        ],
    )
    agent = BusinessAgent(business_api=api, session_manager_ref=sm, llm_client=llm)
    params = agent._extract_params("这个订单多少钱", {})
    assert params is not None
    assert params.scene == BusinessScene.ORDER
    assert params.order_id == "9876543210"
    assert params.query_type == "amount"
    # LLM 确实被调用一次
    assert len(llm.calls) == 1


def test_extract_params_unknown_scene(env):
    """无法识别场景时返回 None，由主流程返回引导。"""
    agent, *_ = env
    params = agent._extract_params("今天天气怎么样", {})
    assert params is None


# ----------------------------------------------------------------------
# SubTask 10.2 订单查询 → 格式化回复
# ----------------------------------------------------------------------
def test_order_query_formatted(env):
    """订单查询应返回含订单号/状态/物流/金额的格式化回复。"""
    agent, api, sm, llm = env
    sid = _login_session(sm)
    result = agent.execute("查订单1234567890的物流", sid)

    assert result.success is True
    assert result.scene == BusinessScene.ORDER
    assert result.error is None
    assert "1234567890" in result.reply
    assert "已发货" in result.reply
    assert "SF1234567890" in result.reply
    assert "199.00" in result.reply
    assert result.data["order_id"] == "1234567890"
    assert result.data["status"] == "shipped"


def test_order_query_not_found(env):
    """不存在的订单号应返回提示且 success=False。"""
    agent, _, sm, _ = env
    sid = _login_session(sm)
    result = agent.execute("查订单0000000000", sid)
    assert result.success is False
    assert result.error == "order_not_found"


def test_format_by_llm_path():
    """LLM 非 mock 时应走 LLM 格式化路径。

    execute 会调两次 LLM：参数提取 + 结果格式化，故 responses 按序提供两个。
    """
    api = MockBusinessAPI()
    sm = SessionManager()
    llm = FakeLLM(
        is_mock=False,
        responses=[
            '{"scene":"order","order_id":"1234567890","query_type":"status"}',
            "您的订单已发货，物流单号 SF1234567890。",
        ],
    )
    agent = BusinessAgent(business_api=api, session_manager_ref=sm, llm_client=llm)
    sid = _login_session(sm)
    result = agent.execute("查订单1234567890", sid)
    assert result.reply == "您的订单已发货，物流单号 SF1234567890。"
    # 一次提取 + 一次格式化
    assert len(llm.calls) == 2


# ----------------------------------------------------------------------
# SubTask 10.3 退换货二次确认流程
# ----------------------------------------------------------------------
def test_return_create_confirmation_flow(env):
    """退换货创建：先返回 need_confirmation，确认后才真正执行。"""
    agent, api, sm, llm = env
    sid = _login_session(sm)

    # 第一次：发起退货 → 等待确认
    r1 = agent.execute("我要退货订单1234567890，原因质量问题", sid)
    assert r1.need_confirmation is True
    assert r1.confirmation_token
    assert "确认" in r1.reply
    assert r1.scene == BusinessScene.RETURN
    # 确认前 API 中不应有记录
    assert api.query_return("R0001") is None

    # 第二次：确认 → 真正创建
    r2 = agent.execute("确认", sid)
    assert r2.success is True
    assert r2.scene == BusinessScene.RETURN
    assert "已创建" in r2.reply
    assert "R0001" in r2.reply
    # 确认后 API 中确有记录
    record = api.query_return("R0001")
    assert record is not None
    assert record["order_id"] == "1234567890"
    assert record["reason"] == "质量问题"


def test_return_create_cancel_flow(env):
    """用户发起退货后选择取消，不应创建记录。"""
    agent, api, sm, llm = env
    sid = _login_session(sm)

    r1 = agent.execute("我要退货订单1234567890", sid)
    assert r1.need_confirmation is True

    r2 = agent.execute("取消", sid)
    assert "取消" in r2.reply
    # 取消后不应创建退换货记录
    assert api.query_return("R0001") is None
    # pending 已清除，再次确认无效
    r3 = agent.execute("确认", sid)
    assert r3.error != "create_failed"


def test_return_query_list(env):
    """查询退换货列表应返回用户全部记录。"""
    agent, api, sm, llm = env
    sid = _login_session(sm)
    # 先创建一条退换货
    agent.execute("我要退货订单1111222233", sid)
    agent.execute("确认", sid)

    result = agent.execute("查我的退换货记录", sid)
    assert result.success is True
    assert result.scene == BusinessScene.RETURN
    assert "R0001" in result.reply


# ----------------------------------------------------------------------
# SubTask 10.3 敏感信息脱敏
# ----------------------------------------------------------------------
def test_phone_and_id_card_masking(env):
    """会员查询应脱敏手机号与身份证，且不污染原始数据。"""
    agent, api, sm, llm = env
    sid = _login_session(sm)
    result = agent.execute("查我的会员信息", sid)

    assert result.success is True
    # 手机号中间 4 位打码
    assert result.data["phone"] == "138****1234"
    assert "13812341234" not in result.reply
    # 身份证前 6 后 4，中间打码
    assert result.data["id_card"].startswith("110101")
    assert result.data["id_card"].endswith("1234")
    assert "*" in result.data["id_card"]
    # 金额/积分保留
    assert result.data["points"] == 1250
    # 原始 mock 数据未被修改
    assert api._members["U001"]["phone"] == "13812341234"
    assert api._members["U001"]["id_card"] == "110101199001011234"


def test_mask_phone_static():
    """手机号脱敏函数：11 位 → 前3后4。"""
    assert BusinessAgent._mask_phone("13812341234") == "138****1234"
    # 非 11 位不脱敏，避免破坏短号
    assert BusinessAgent._mask_phone("12345") == "12345"


def test_mask_id_card_static():
    """身份证脱敏函数：前6后4中间打码。"""
    masked = BusinessAgent._mask_id_card("110101199001011234")
    assert masked == "110101********1234"


# ----------------------------------------------------------------------
# SubTask 10.3 频率限制
# ----------------------------------------------------------------------
def test_rate_limit(env):
    """同一 session 10 秒内超过 5 次应被限流。"""
    agent, *_ = env
    sid = _login_session(sm := env[2])
    # 前 5 次正常
    for _ in range(5):
        result = agent.execute("查订单1234567890", sid)
        assert result.error != "rate_limited"
    # 第 6 次限流
    result = agent.execute("查订单1234567890", sid)
    assert result.error == "rate_limited"
    assert result.success is False
    assert "频繁" in result.reply


def test_rate_limit_independent_sessions(env):
    """不同 session 的频率计数互不影响。"""
    agent, *_ = env
    sid1 = _login_session(env[2], "U001")
    sid2 = _login_session(env[2], "U002")
    # sid1 用满 5 次
    for _ in range(5):
        agent.execute("查订单1234567890", sid1)
    # sid2 仍可调用
    result = agent.execute("查订单9876543210", sid2)
    assert result.error != "rate_limited"


# ----------------------------------------------------------------------
# SubTask 10.3 未登录提示
# ----------------------------------------------------------------------
def test_not_logged_in_member(env):
    """会员查询未登录应提示登录。"""
    agent, *_ = env
    sid = _guest_session(env[2])
    result = agent.execute("查我的会员积分", sid)
    assert result.error == "not_logged_in"
    assert result.success is False
    assert "登录" in result.reply


def test_not_logged_in_return_create(env):
    """退换货写操作未登录应提示登录。"""
    agent, *_ = env
    sid = _guest_session(env[2])
    result = agent.execute("我要退货订单1234567890", sid)
    assert result.error == "not_logged_in"


def test_order_query_no_login_required(env):
    """订单查询凭订单号即可，不强制登录。"""
    agent, *_ = env
    sid = _guest_session(env[2])
    result = agent.execute("查订单1234567890", sid)
    assert result.success is True
    assert result.scene == BusinessScene.ORDER


# ----------------------------------------------------------------------
# 业务场景演示（-s 时输出）
# ----------------------------------------------------------------------
def test_demo_scenarios(env):
    """演示：查订单→格式化+脱敏；退换货→二次确认流程。

    用 result 对象断言，-s 时 print 直接输出便于人工查看演示效果。
    """
    agent, api, sm, llm = env
    sid = _login_session(sm)

    print("\n========== 业务查询 Agent 演示 ==========")

    # 演示 1：查订单 → 格式化回复
    r = agent.execute("查订单1234567890的物流", sid)
    print(f"[查订单] reply: {r.reply}")
    print(f"[查订单] data: {r.data}")
    assert r.success and "已发货" in r.reply

    # 演示 2：查会员 → 脱敏
    r = agent.execute("查我的会员信息", sid)
    print(f"[查会员] reply: {r.reply}")
    print(
        f"[查会员] 脱敏后 data: phone={r.data.get('phone')} "
        f"id_card={r.data.get('id_card')}"
    )
    assert r.data.get("phone") == "138****1234"

    # 演示 3：退换货 → 二次确认
    r1 = agent.execute("我要退货订单1111222233，原因质量问题", sid)
    print(f"[退货-待确认] need_confirmation={r1.need_confirmation} reply: {r1.reply}")
    assert r1.need_confirmation is True
    r2 = agent.execute("确认", sid)
    print(f"[退货-已确认] reply: {r2.reply}")
    assert "已创建" in r2.reply

    print("=========================================")
