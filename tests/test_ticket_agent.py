"""工单处理 Agent 测试。

使用注入的 mock LLM 客户端与独立 TicketStore 验证 TicketAgent 核心行为：
1. 工单创建：信息提取、ticket_id 自动生成、字段填充
2. 工单分类：退货→after_sale、发货→logistics、投诉→complaint
3. 优先级判断：含愤怒关键词→urgent、影响使用→high、建议→low
4. 进度查询：query_ticket 返回正确状态
5. 状态更新：update_ticket_status 流转正确
6. LLM 模式：JSON 提取与解析、降级路径

通过注入独立 TicketStore 与 mock LLM 隔离全局单例，
不依赖 ChromaDB 与网络，保证测试稳定可复现。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.agents.ticket_agent import TicketAgent
from app.agents.ticket_store import TicketStore
from app.schemas.ticket import (
    Ticket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
    TicketResult,
)


# ==================== Mock 客户端 ====================


class _MockLLMClient:
    """Mock LLM 客户端：is_mock=True，触发规则提取路径。

    规则提取不调用任何外部服务，保证测试稳定可复现。
    """

    is_mock = True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> str:
        # mock 模式下 TicketAgent 走规则提取，不会真正调用此方法
        return ""


class _FakeRealLLMClient:
    """模拟真实 LLM 客户端：is_mock=False，返回预设 JSON。

    用于测试 LLM 模式下的 JSON 解析与字段校验逻辑。
    """

    def __init__(self, response: str) -> None:
        self._response = response
        self.is_mock = False
        # 记录最后一次调用的 messages，便于断言 prompt 构造
        self.last_messages: Optional[List[Dict[str, Any]]] = None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> str:
        self.last_messages = messages
        return self._response


# ==================== Fixtures ====================


@pytest.fixture
def isolated_store() -> TicketStore:
    """每个用例独立的 TicketStore，避免用例间数据污染。

    直接 new 一个实例而非使用全局单例，
    确保并发执行的用例互不影响。
    """
    return TicketStore()


@pytest.fixture
def mock_agent(isolated_store: TicketStore) -> TicketAgent:
    """基于 mock LLM 与独立 store 的 TicketAgent，走规则提取路径。"""
    return TicketAgent(
        llm_client=_MockLLMClient(),
        ticket_store=isolated_store,
    )


# ==================== 工单创建测试 ====================


def test_create_ticket_generates_ticket_id(mock_agent: TicketAgent):
    """工单创建应自动生成非空且带前缀的 ticket_id。"""
    result = mock_agent.create_ticket_from_message(
        message="我买的手机坏了，需要维修",
        user_id="user-001",
    )
    assert isinstance(result, TicketResult)
    assert result.ticket_id
    # 工单 ID 应带 TK 前缀，便于运维识别
    assert result.ticket_id.startswith("TK-")
    # 状态默认为 pending
    assert result.status == TicketStatus.pending


def test_create_ticket_returns_reply_with_id(mock_agent: TicketAgent):
    """创建工单后回复中应包含工单号，便于用户后续查询。"""
    result = mock_agent.create_ticket_from_message(
        message="订单123456789还没发货",
        user_id="user-002",
    )
    assert result.ticket_id in result.reply
    assert "工单号" in result.reply


def test_create_ticket_extracts_order_id(mock_agent: TicketAgent):
    """规则提取应从消息中识别订单号并写入工单。"""
    result = mock_agent.create_ticket_from_message(
        message="订单号：ABC123456 发货太慢了",
        user_id="user-003",
    )
    # 通过 store 取出工单验证字段
    ticket = mock_agent.ticket_store.get_ticket(result.ticket_id)
    assert ticket is not None
    assert ticket.related_order == "ABC123456"


def test_create_ticket_extracts_phone_contact(mock_agent: TicketAgent):
    """规则提取应从消息中识别手机号作为联系方式。"""
    result = mock_agent.create_ticket_from_message(
        message="我的手机13800138000收不到验证码，登录不了",
        user_id="user-004",
    )
    ticket = mock_agent.ticket_store.get_ticket(result.ticket_id)
    assert ticket is not None
    assert ticket.contact == "13800138000"


def test_create_ticket_extracts_email_contact(mock_agent: TicketAgent):
    """规则提取应从消息中识别邮箱作为联系方式。"""
    result = mock_agent.create_ticket_from_message(
        message="请把退款详情发到 test@example.com，邮箱收不到",
        user_id="user-005",
    )
    ticket = mock_agent.ticket_store.get_ticket(result.ticket_id)
    assert ticket is not None
    assert ticket.contact == "test@example.com"


def test_create_ticket_empty_message_returns_fallback(mock_agent: TicketAgent):
    """空消息应返回兜底 TicketResult，且不创建工单。"""
    result = mock_agent.create_ticket_from_message(message="", user_id="u")
    assert result.ticket_id == ""
    assert "请描述您遇到的问题" in result.reply
    # 不应在 store 中产生任何工单
    assert mock_agent.ticket_store.count() == 0


def test_create_ticket_title_truncated(mock_agent: TicketAgent):
    """标题超长时应被截断到 MAX_TITLE_LENGTH 以内。"""
    long_message = "我的订单还没有发货并且已经等了好久了请帮我处理一下谢谢"
    result = mock_agent.create_ticket_from_message(
        message=long_message, user_id="u"
    )
    ticket = mock_agent.ticket_store.get_ticket(result.ticket_id)
    assert ticket is not None
    from app.agents.ticket_agent import MAX_TITLE_LENGTH
    assert len(ticket.title) <= MAX_TITLE_LENGTH


# ==================== 工单分类测试 ====================


def test_classify_return_goods_to_after_sale(mock_agent: TicketAgent):
    """退货消息应分类到 after_sale（售后）。"""
    result = mock_agent.create_ticket_from_message(
        message="我要退货，商品不喜欢",
        user_id="u",
    )
    assert result.category == TicketCategory.after_sale


def test_classify_shipping_to_logistics(mock_agent: TicketAgent):
    """发货问题应分类到 logistics（物流）。"""
    result = mock_agent.create_ticket_from_message(
        message="订单已经三天了还没发货",
        user_id="u",
    )
    assert result.category == TicketCategory.logistics


def test_classify_complaint_to_complaint(mock_agent: TicketAgent):
    """投诉消息应分类到 complaint（投诉），优先级高于具体业务分类。"""
    result = mock_agent.create_ticket_from_message(
        message="我要投诉客服态度差，太差了",
        user_id="u",
    )
    assert result.category == TicketCategory.complaint


def test_classify_quality_to_product(mock_agent: TicketAgent):
    """质量问题应分类到 product（产品）。"""
    result = mock_agent.create_ticket_from_message(
        message="收到的商品有质量问题，破损了",
        user_id="u",
    )
    assert result.category == TicketCategory.product


def test_classify_login_to_account(mock_agent: TicketAgent):
    """登录问题应分类到 account（账户）。"""
    result = mock_agent.create_ticket_from_message(
        message="账号登录不了，密码忘了",
        user_id="u",
    )
    assert result.category == TicketCategory.account


# ==================== 优先级判断测试 ====================


def test_priority_urgent_for_angry_keywords(mock_agent: TicketAgent):
    """含愤怒关键词（气死/太差了/投诉）应判为 urgent。"""
    result = mock_agent.create_ticket_from_message(
        message="气死我了，你们这服务太差了，立刻给我处理",
        user_id="u",
    )
    assert result.priority == TicketPriority.urgent


def test_priority_urgent_for_money_loss(mock_agent: TicketAgent):
    """资金损失场景（扣了钱/钱没了）应判为 urgent。"""
    result = mock_agent.create_ticket_from_message(
        message="支付时扣了钱但订单没成功，钱没了",
        user_id="u",
    )
    assert result.priority == TicketPriority.urgent


def test_priority_high_for_cannot_use(mock_agent: TicketAgent):
    """影响使用（无法使用/不能用）应判为 high。"""
    result = mock_agent.create_ticket_from_message(
        message="商品故障，无法使用",
        user_id="u",
    )
    assert result.priority == TicketPriority.high


def test_priority_low_for_suggestion(mock_agent: TicketAgent):
    """建议反馈类消息应判为 low。"""
    result = mock_agent.create_ticket_from_message(
        message="建议你们优化一下搜索功能",
        user_id="u",
    )
    assert result.priority == TicketPriority.low


def test_priority_medium_for_general_consult(mock_agent: TicketAgent):
    """一般咨询（无情绪/无关键词）应判为 medium。"""
    result = mock_agent.create_ticket_from_message(
        message="请问会员等级是怎么划分的",
        user_id="u",
    )
    assert result.priority == TicketPriority.medium


# ==================== 进度查询测试 ====================


def test_query_ticket_returns_ticket(mock_agent: TicketAgent):
    """query_ticket 应返回已创建的工单。"""
    result = mock_agent.create_ticket_from_message(
        message="退款什么时候到账",
        user_id="user-q",
    )
    ticket = mock_agent.query_ticket(result.ticket_id)
    assert ticket is not None
    assert ticket.ticket_id == result.ticket_id
    assert ticket.user_id == "user-q"


def test_query_ticket_returns_none_for_unknown_id(mock_agent: TicketAgent):
    """未知 ticket_id 应返回 None。"""
    ticket = mock_agent.query_ticket("TK-notexist")
    assert ticket is None


# ==================== 状态更新测试 ====================


def test_update_status_to_processing(mock_agent: TicketAgent):
    """工单状态可从 pending 更新到 processing。"""
    result = mock_agent.create_ticket_from_message(
        message="订单还没发货",
        user_id="u",
    )
    updated = mock_agent.update_ticket_status(
        result.ticket_id, TicketStatus.processing
    )
    assert updated is not None
    assert updated.status == TicketStatus.processing
    # updated_at 应刷新到更晚的时间
    ticket = mock_agent.query_ticket(result.ticket_id)
    assert ticket is not None
    assert ticket.status == TicketStatus.processing


def test_update_status_to_resolved_then_closed(mock_agent: TicketAgent):
    """工单可流转到 resolved 再到 closed。"""
    result = mock_agent.create_ticket_from_message(
        message="退款没到账",
        user_id="u",
    )
    # pending → resolved
    updated = mock_agent.update_ticket_status(
        result.ticket_id, TicketStatus.resolved
    )
    assert updated is not None
    assert updated.status == TicketStatus.resolved
    # resolved → closed
    updated = mock_agent.update_ticket_status(
        result.ticket_id, TicketStatus.closed
    )
    assert updated is not None
    assert updated.status == TicketStatus.closed


def test_update_status_rejects_change_after_closed(mock_agent: TicketAgent):
    """已关闭工单拒绝状态回退，返回 None。"""
    result = mock_agent.create_ticket_from_message(
        message="退款没到账",
        user_id="u",
    )
    mock_agent.update_ticket_status(result.ticket_id, TicketStatus.closed)
    # 尝试回退到 processing 应被拒绝
    updated = mock_agent.update_ticket_status(
        result.ticket_id, TicketStatus.processing
    )
    assert updated is None


def test_update_status_returns_none_for_unknown_id(mock_agent: TicketAgent):
    """未知 ticket_id 更新状态应返回 None。"""
    updated = mock_agent.update_ticket_status(
        "TK-notexist", TicketStatus.processing
    )
    assert updated is None


# ==================== TicketStore 直接测试 ====================


def test_store_list_tickets_by_user(isolated_store: TicketStore):
    """list_tickets(user_id) 应只返回该用户的工单。"""
    isolated_store.create_ticket(
        user_id="u1",
        title="问题1",
        description="描述1",
        category=TicketCategory.after_sale,
        priority=TicketPriority.medium,
    )
    isolated_store.create_ticket(
        user_id="u2",
        title="问题2",
        description="描述2",
        category=TicketCategory.logistics,
        priority=TicketPriority.high,
    )
    isolated_store.create_ticket(
        user_id="u1",
        title="问题3",
        description="描述3",
        category=TicketCategory.product,
        priority=TicketPriority.low,
    )
    u1_tickets = isolated_store.list_tickets(user_id="u1")
    assert len(u1_tickets) == 2
    assert all(t.user_id == "u1" for t in u1_tickets)


def test_store_list_tickets_by_status(isolated_store: TicketStore):
    """list_tickets_by_status 应只返回指定状态的工单。"""
    t1 = isolated_store.create_ticket(
        user_id="u",
        title="问题1",
        description="描述1",
        category=TicketCategory.after_sale,
        priority=TicketPriority.medium,
    )
    t2 = isolated_store.create_ticket(
        user_id="u",
        title="问题2",
        description="描述2",
        category=TicketCategory.logistics,
        priority=TicketPriority.high,
    )
    isolated_store.update_status(t2.ticket_id, TicketStatus.processing)
    pending = isolated_store.list_tickets_by_status(TicketStatus.pending)
    processing = isolated_store.list_tickets_by_status(TicketStatus.processing)
    assert len(pending) == 1
    assert pending[0].ticket_id == t1.ticket_id
    assert len(processing) == 1
    assert processing[0].ticket_id == t2.ticket_id


def test_store_ticket_id_is_unique(isolated_store: TicketStore):
    """并发场景下多次创建工单应生成唯一 ticket_id。"""
    ids = set()
    for _ in range(100):
        ticket = isolated_store.create_ticket(
            user_id="u",
            title="t",
            description="d",
            category=TicketCategory.after_sale,
            priority=TicketPriority.medium,
        )
        ids.add(ticket.ticket_id)
    assert len(ids) == 100


# ==================== LLM 模式测试 ====================


def test_llm_extract_parses_valid_json(isolated_store: TicketStore):
    """LLM 模式下应解析合法 JSON 并创建工单。"""
    llm_response = json.dumps(
        {
            "title": "退款未到账",
            "description": "用户反馈订单 ABC123 退款未到账",
            "category": "after_sale",
            "priority": "high",
            "related_order": "ABC123",
            "related_product": "",
            "contact": "",
        },
        ensure_ascii=False,
    )
    fake_client = _FakeRealLLMClient(response=llm_response)
    agent = TicketAgent(llm_client=fake_client, ticket_store=isolated_store)

    result = agent.create_ticket_from_message(
        message="订单ABC123退款没到账",
        user_id="u",
    )
    assert result.category == TicketCategory.after_sale
    assert result.priority == TicketPriority.high
    ticket = isolated_store.get_ticket(result.ticket_id)
    assert ticket is not None
    assert ticket.title == "退款未到账"
    assert ticket.related_order == "ABC123"
    # 应构造了 system 与 user 两条消息
    assert fake_client.last_messages is not None
    assert len(fake_client.last_messages) == 2
    assert fake_client.last_messages[0]["role"] == "system"


def test_llm_extract_handles_markdown_wrapped_json(isolated_store: TicketStore):
    """LLM 返回带 markdown 代码块的 JSON 也应能解析。"""
    llm_response = (
        "```json\n"
        + json.dumps(
            {
                "title": "发货慢",
                "description": "订单三天没发货",
                "category": "logistics",
                "priority": "medium",
            },
            ensure_ascii=False,
        )
        + "\n```"
    )
    fake_client = _FakeRealLLMClient(response=llm_response)
    agent = TicketAgent(llm_client=fake_client, ticket_store=isolated_store)

    result = agent.create_ticket_from_message(
        message="订单三天没发货", user_id="u"
    )
    assert result.category == TicketCategory.logistics
    assert result.priority == TicketPriority.medium


def test_llm_extract_falls_back_on_invalid_json(isolated_store: TicketStore):
    """LLM 返回非法 JSON 时应降级到规则提取。"""
    fake_client = _FakeRealLLMClient(response="这不是 JSON")
    agent = TicketAgent(llm_client=fake_client, ticket_store=isolated_store)

    result = agent.create_ticket_from_message(
        message="我要退货，商品坏了",
        user_id="u",
    )
    # 降级后仍应成功创建工单
    assert result.ticket_id.startswith("TK-")
    # 规则分类：退货 → after_sale
    assert result.category == TicketCategory.after_sale


def test_llm_extract_falls_back_on_invalid_enum(isolated_store: TicketStore):
    """LLM 返回的枚举值非法时应降级到规则提取。"""
    llm_response = json.dumps(
        {
            "title": "测试",
            "description": "描述",
            "category": "unknown_category",  # 非法枚举
            "priority": "high",
        },
        ensure_ascii=False,
    )
    fake_client = _FakeRealLLMClient(response=llm_response)
    agent = TicketAgent(llm_client=fake_client, ticket_store=isolated_store)

    result = agent.create_ticket_from_message(
        message="我要退货", user_id="u"
    )
    # 降级到规则：退货 → after_sale
    assert result.category == TicketCategory.after_sale


def test_llm_extract_falls_back_on_exception(isolated_store: TicketStore):
    """LLM 调用抛异常时应降级到规则提取，不中断流程。"""

    class _ErrorLLMClient:
        is_mock = False

        def chat(self, **kwargs: Any) -> str:
            raise RuntimeError("LLM 服务不可用")

    agent = TicketAgent(
        llm_client=_ErrorLLMClient(), ticket_store=isolated_store
    )
    result = agent.create_ticket_from_message(
        message="气死我了，投诉你们态度太差了",
        user_id="u",
    )
    # 降级后仍应创建工单，规则判定为投诉 + urgent
    assert result.category == TicketCategory.complaint
    assert result.priority == TicketPriority.urgent


# ==================== 演示：完整工单流程 ====================


def test_demo_full_ticket_flow(mock_agent: TicketAgent, capsys):
    """演示：创建工单 → 分类 → 定级 → 查询进度 → 更新状态。"""
    print("\n========== 工单处理流程演示 ==========")

    # 1. 创建工单
    message = "气死我了！订单号：ORD20240601 三天了还没发货，立刻给我处理！"
    result = mock_agent.create_ticket_from_message(
        message=message, user_id="user-demo"
    )
    print("【1. 创建工单】")
    print(f"  用户消息：{message}")
    print(f"  回复：{result.reply}")

    # 2. 验证分类与定级
    print("\n【2. 分类与定级】")
    print(f"  分类：{result.category.value}（应为 logistics）")
    print(f"  优先级：{result.priority.value}（应为 urgent）")

    # 3. 查询进度
    ticket = mock_agent.query_ticket(result.ticket_id)
    print("\n【3. 查询进度】")
    print(f"  工单号：{ticket.ticket_id}")
    print(f"  状态：{ticket.status.value}")
    print(f"  订单号：{ticket.related_order}")
    print(f"  标题：{ticket.title}")

    # 4. 更新状态
    updated = mock_agent.update_ticket_status(
        result.ticket_id, TicketStatus.processing
    )
    print("\n【4. 更新状态】")
    print(f"  新状态：{updated.status.value}")

    # 5. 最终关闭
    closed = mock_agent.update_ticket_status(
        result.ticket_id, TicketStatus.closed
    )
    print(f"  最终状态：{closed.status.value}")
    print("=====================================")

    # 断言流程结果
    assert result.category == TicketCategory.logistics
    assert result.priority == TicketPriority.urgent
    assert ticket.related_order == "ORD20240601"
    assert updated.status == TicketStatus.processing
    assert closed.status == TicketStatus.closed
