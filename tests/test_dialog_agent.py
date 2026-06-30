"""对话润色 Agent 测试。

使用注入的 mock LLM 客户端验证 DialogAgent 的核心行为：
1. 生硬答案润色后含亲切开头与引导结尾
2. 含禁用词的输入被校验/过滤
3. 多条来源正确标注
4. 有历史对话时回复连贯
5. 话术校验合规/不合规判定
6. 演示：生硬答案 → 人性化回复的前后对比

通过注入自定义 LLM 客户端隔离全局单例，不依赖 ChromaDB 与网络。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.agents.dialog_agent import DialogAgent
from app.schemas.dialog import DialogContext, DialogResult


class _MockLLMClient:
    """Mock LLM 客户端：is_mock=True，触发规则拼装路径。

    规则拼装不调用任何外部服务，保证测试稳定可复现。
    """

    is_mock = True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> str:
        # mock 模式下 DialogAgent 走规则拼装，不会真正调用此方法
        return ""


class _FakeRealLLMClient:
    """模拟真实 LLM 客户端：is_mock=False，返回预设内容。

    用于测试 LLM 模式下的 prompt 构造与响应处理逻辑。
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


@pytest.fixture
def mock_agent() -> DialogAgent:
    """基于 mock LLM 的 DialogAgent，走规则拼装路径。"""
    return DialogAgent(llm_client=_MockLLMClient())


@pytest.fixture
def empty_context() -> DialogContext:
    """无历史对话的空上下文。"""
    return DialogContext(session_id="test-session", user_id="test-user")


# ==================== 润色行为测试 ====================


def test_polish_adds_greeting_and_closing(mock_agent, empty_context):
    """生硬答案润色后应含亲切开头与引导结尾。"""
    raw = "退款需在7天内申请。"
    polished = mock_agent.polish(raw, empty_context)

    # 开头必须含亲切称呼
    assert polished.startswith("您好~") or polished.startswith("亲~")
    # 结尾必须含引导语
    assert "还有什么可以帮您的吗？" in polished
    # 原始信息应保留
    assert "退款" in polished
    assert "7天" in polished


def test_polish_segments_long_answer(mock_agent, empty_context):
    """多句答案应分段展示，重要信息分行。"""
    raw = "退款需在7天内申请。商品需保持原包装。退款将原路返回。"
    polished = mock_agent.polish(raw, empty_context)

    # 多句时应出现换行分段
    assert polished.count("\n") >= 3
    # 三条要点都应保留
    assert "7天" in polished
    assert "原包装" in polished
    assert "原路返回" in polished


def test_polish_empty_answer_returns_compliant_reply(mock_agent, empty_context):
    """空答案应返回合规的兜底回复。"""
    polished = mock_agent.polish("", empty_context)
    assert polished.startswith("您好~")
    assert "还有什么可以帮您的吗？" in polished
    assert mock_agent.validate_tone(polished) is True


# ==================== 禁用词校验/过滤测试 ====================


def test_validate_tone_rejects_forbidden_phrases(mock_agent):
    """含禁用词的文本应校验失败。"""
    bad_text = "亲，这边建议您退款需要在7天内申请。还有什么可以帮您的吗？"
    # 以合规称呼开头但含禁用词，应判不合规
    bad_text = "您好~\n" + bad_text
    assert mock_agent.validate_tone(bad_text) is False


def test_validate_tone_rejects_missing_opening(mock_agent):
    """缺少开头称呼应校验失败。"""
    text = "退款需在7天内申请。还有什么可以帮您的吗？"
    assert mock_agent.validate_tone(text) is False


def test_validate_tone_rejects_missing_closing(mock_agent):
    """缺少结尾引导应校验失败。"""
    text = "您好~\n退款需在7天内申请。"
    assert mock_agent.validate_tone(text) is False


def test_validate_tone_accepts_compliant_text(mock_agent):
    """合规文本应校验通过。"""
    text = "您好~\n退款需在7天内申请。\n还有什么可以帮您的吗？"
    assert mock_agent.validate_tone(text) is True


def test_filter_forbidden_removes_outdated_phrases(mock_agent, empty_context):
    """含禁用词的输入经润色后应被过滤掉。"""
    raw = "亲，这边建议您退款需要在7天内申请。"
    polished = mock_agent.polish(raw, empty_context)

    # 禁用词应被移除
    assert "亲，这边建议您" not in polished
    # 过滤后应通过话术校验
    assert mock_agent.validate_tone(polished) is True


# ==================== 来源标注测试 ====================


def test_generate_annotates_multiple_sources(mock_agent, empty_context):
    """多条来源应在回复末尾正确标注。"""
    raw = "退款需在7天内申请。"
    sources = ["退款政策.md 第1页", "服务条款.md 第3页", "FAQ.md 第5页"]

    result = mock_agent.generate(raw, sources, empty_context)

    assert isinstance(result, DialogResult)
    # 每条来源都应出现在回复中
    for source in sources:
        assert source in result.reply
    # 来源区块应以「参考来源」标识开头
    assert "参考来源：" in result.reply
    # 返回的 sources 应与传入一致
    assert result.sources == sources


def test_generate_without_sources_skips_annotation(mock_agent, empty_context):
    """无来源时不应追加来源区块。"""
    result = mock_agent.generate("退款需在7天内申请。", [], empty_context)
    assert "参考来源：" not in result.reply


def test_generate_strips_existing_source_line(mock_agent, empty_context):
    """原始答案自带的「来源：」行应被移除，避免与统一标注重复。"""
    raw = "退款需在7天内申请。\n来源：旧文档.md 第1页"
    result = mock_agent.generate(
        raw, ["新文档.md 第2页"], empty_context
    )
    # 旧来源行不应出现
    assert "旧文档.md" not in result.reply
    # 新来源应被标注
    assert "新文档.md 第2页" in result.reply


# ==================== 上下文衔接测试 ====================


def test_polish_with_history_adds_transition(mock_agent):
    """有历史对话时回复应含过渡语，保持连贯。"""
    context = DialogContext(
        session_id="s1",
        history=[
            {"role": "user", "content": "我昨天下的订单还没发货"},
            {"role": "assistant", "content": "已为您加急处理"},
        ],
    )
    polished = mock_agent.polish("订单已发货，预计明天送达。", context)
    # 应出现上下文过渡语
    assert "关于您提到的问题" in polished


def test_polish_with_low_emotion_shows_empathy(mock_agent):
    """用户情绪偏低时回复应表达理解与安抚。"""
    context = DialogContext(
        session_id="s1",
        emotion_score=0.2,
        history=[{"role": "user", "content": "等了好久还没处理"}],
    )
    polished = mock_agent.polish("已为您加急处理。", context)
    assert "理解您的着急" in polished


def test_polish_without_history_skips_transition(mock_agent, empty_context):
    """无历史对话时不应出现过渡语。"""
    polished = mock_agent.polish("退款需在7天内申请。", empty_context)
    assert "关于您提到的问题" not in polished


# ==================== 完整 generate 流程测试 ====================


def test_generate_returns_suggestions(mock_agent, empty_context):
    """generate 应返回引导追问建议。"""
    result = mock_agent.generate(
        "退款需在7天内申请。",
        ["退款政策.md 第1页"],
        empty_context,
    )
    assert len(result.suggestions) > 0
    # 有来源时建议应引用来源
    assert any("退款政策.md" in s for s in result.suggestions)


def test_generate_tone_valid_in_mock_mode(mock_agent, empty_context):
    """mock 模式下润色结果应通过话术校验。"""
    result = mock_agent.generate(
        "退款需在7天内申请。", ["退款政策.md 第1页"], empty_context
    )
    # 来源标注追加在结尾引导之后，不影响开头与引导语判定
    assert result.tone_valid is True


# ==================== LLM 模式测试 ====================


def test_llm_polish_uses_prompt_and_response():
    """LLM 模式下应构造 system+user 消息并返回 LLM 响应。"""
    fake_response = "您好~\n退款需要在7天内申请哦~\n还有什么可以帮您的吗？"
    fake_client = _FakeRealLLMClient(response=fake_response)
    agent = DialogAgent(llm_client=fake_client)
    context = DialogContext(session_id="s1")

    polished = agent.polish("退款需在7天内申请。", context)

    # 应使用 LLM 返回的内容
    assert polished == fake_response
    # 应构造了 system 与 user 两条消息
    assert fake_client.last_messages is not None
    assert len(fake_client.last_messages) == 2
    assert fake_client.last_messages[0]["role"] == "system"
    assert fake_client.last_messages[1]["role"] == "user"
    # user 消息应包含原始答案
    assert "退款需在7天内申请" in fake_client.last_messages[1]["content"]


def test_llm_polish_fallback_on_empty_response():
    """LLM 返回空回复时应降级到规则拼装。"""
    fake_client = _FakeRealLLMClient(response="")
    agent = DialogAgent(llm_client=fake_client)
    polished = agent.polish("退款需在7天内申请。", DialogContext())
    # 降级后应含规则拼装的开头与结尾
    assert polished.startswith("您好~")
    assert "还有什么可以帮您的吗？" in polished


def test_llm_polish_includes_context_summary():
    """LLM 模式下 user 消息应包含上下文摘要。"""
    fake_client = _FakeRealLLMClient(response="您好~\n已处理。\n还有什么可以帮您的吗？")
    agent = DialogAgent(llm_client=fake_client)
    context = DialogContext(
        session_id="s1",
        history=[{"role": "user", "content": "订单没发货"}],
        current_intent="查询物流",
        emotion_score=0.3,
    )
    agent.polish("已加急处理。", context)

    user_content = fake_client.last_messages[1]["content"]
    # 上下文摘要应包含历史对话、意图、情绪
    assert "订单没发货" in user_content
    assert "查询物流" in user_content
    assert "0.3" in user_content


# ==================== 可配置性测试 ====================


def test_validate_tone_is_configurable():
    """话术校验规则应支持自定义配置。"""
    custom_forbidden = ["不知道", "没办法"]
    custom_openings = ["哈喽~"]
    custom_closings = ["随时找我哦~"]

    agent = DialogAgent(
        llm_client=_MockLLMClient(),
        forbidden_phrases=custom_forbidden,
        opening_greetings=custom_openings,
        closing_phrases=custom_closings,
    )

    # 自定义开头+结尾且无禁用词 → 通过
    assert agent.validate_tone("哈喽~\n已处理。\n随时找我哦~") is True
    # 含自定义禁用词 → 不通过
    assert agent.validate_tone("哈喽~\n不知道。\n随时找我哦~") is False
    # 用默认开头（不在自定义列表）→ 不通过
    assert agent.validate_tone("您好~\n已处理。\n随时找我哦~") is False


# ==================== 演示：润色前后对比 ====================


def test_demo_polish_before_after(mock_agent, empty_context, capsys):
    """演示：生硬答案 → 人性化回复的前后对比。"""
    raw_answer = "退款需在7天内申请。商品需保持原包装。退款将原路返回。"
    sources = ["退款政策.md 第1页", "FAQ.md 第3页"]

    result = mock_agent.generate(raw_answer, sources, empty_context)

    print("\n========== 对话润色演示 ==========")
    print("【润色前 - 原始答案】")
    print(raw_answer)
    print("\n【润色后 - 人性化回复】")
    print(result.reply)
    print("\n【话术校验】", "通过" if result.tone_valid else "不通过")
    print("【来源】", result.sources)
    print("【追问建议】", result.suggestions)
    print("==================================")

    # 断言润色后符合规范
    assert result.tone_valid is True
    assert result.reply.startswith("您好~")
    assert "还有什么可以帮您的吗？" in result.reply
    # 原始信息保留
    assert "7天" in result.reply
    assert "原包装" in result.reply
    assert "原路返回" in result.reply
    # 来源标注完整
    for source in sources:
        assert source in result.reply
