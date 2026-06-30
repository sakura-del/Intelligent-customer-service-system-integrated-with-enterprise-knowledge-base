"""情感分析 Agent 测试。

使用注入的 mock LLM 客户端验证 EmotionAgent 的核心行为：
1. 五类情绪（愤怒/焦虑/失望/满意/中性）的规则兜底识别
2. 情绪分级（1-5 分）与激烈程度判定
3. 应对策略与情绪类型匹配
4. 人工转接判断（愤怒 score>4 必须转人工）
5. LLM 模式下的 JSON 解析与降级
6. 演示：5 类情绪各一个示例的识别结果

通过注入自定义 LLM 客户端隔离全局单例，不依赖网络与真实模型。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.agents.emotion_agent import EmotionAgent
from app.schemas.emotion import EmotionResult, EmotionType


class _MockLLMClient:
    """Mock LLM 客户端：is_mock=True，触发规则兜底路径。

    规则兜底不调用任何外部服务，保证测试稳定可复现。
    """

    is_mock = True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> str:
        # mock 模式下 EmotionAgent 走规则兜底，不会真正调用此方法
        return ""


class _FakeRealLLMClient:
    """模拟真实 LLM 客户端：is_mock=False，返回预设 JSON。

    用于测试 LLM 模式下的 JSON 解析与异常降级逻辑。
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
def mock_agent() -> EmotionAgent:
    """基于 mock LLM 的 EmotionAgent，走规则兜底路径。"""
    return EmotionAgent(llm_client=_MockLLMClient())


# ==================== 情绪识别测试 ====================


def test_anger_with_profanity_identified_and_escalated(mock_agent):
    """愤怒（含脏话/投诉/威胁差评）应识别为 anger，score>4，建议转人工。"""
    query = "你们这什么垃圾服务，我要投诉，给差评！"
    result = mock_agent.analyze(query, session_id="s1")

    assert isinstance(result, EmotionResult)
    assert result.emotion == EmotionType.ANGER
    # 愤怒且激烈程度高必须建议转人工
    assert result.score > 4
    assert result.suggest_escalate is True
    # 应识别出关键词
    assert len(result.keywords) > 0


def test_anxiety_with_repeated_questioning_identified(mock_agent):
    """焦虑（反复追问/着急/担心）应识别为 anxiety。"""
    query = "怎么还没回复？到底什么时候能处理？我着急啊，担心出问题！"
    result = mock_agent.analyze(query, session_id="s2")

    assert result.emotion == EmotionType.ANXIETY
    # 焦虑分数应在 3-4 区间
    assert 3 <= result.score <= 4
    # 焦虑不主动转人工
    assert result.suggest_escalate is False


def test_disappointment_identified(mock_agent):
    """失望（不满/对比预期）应识别为 disappointment。"""
    query = "太失望了，跟预期完全不一样，没想到会这么差。"
    result = mock_agent.analyze(query, session_id="s3")

    assert result.emotion == EmotionType.DISAPPOINTMENT
    # 失望分数应在 2-3 区间
    assert 2 <= result.score <= 3
    assert result.suggest_escalate is False


def test_satisfaction_with_thanks_identified(mock_agent):
    """满意（感谢/表扬/好评）应识别为 satisfaction。"""
    query = "太感谢了，服务很好，给你们好评！"
    result = mock_agent.analyze(query, session_id="s4")

    assert result.emotion == EmotionType.SATISFACTION
    # 满意分数应在 1-2 区间
    assert 1 <= result.score <= 2
    assert result.suggest_escalate is False


def test_neutral_normal_consultation_identified(mock_agent):
    """中性（正常咨询）应识别为 neutral，score=1。"""
    query = "请问退款流程是什么？"
    result = mock_agent.analyze(query, session_id="s5")

    assert result.emotion == EmotionType.NEUTRAL
    assert result.score == 1
    assert result.suggest_escalate is False


# ==================== 应对策略测试 ====================


def test_strategy_for_anger_contains_appease_and_escalation(mock_agent):
    """愤怒策略应包含安抚与转人工提示。"""
    result = mock_agent.analyze("垃圾服务，我要投诉！", session_id="s6")
    assert result.emotion == EmotionType.ANGER
    strategy = result.strategy
    # 策略应包含安抚相关表述
    assert "安抚" in strategy or "理解" in strategy
    # 愤怒策略应提及转人工
    assert "转人工" in strategy or "人工" in strategy


def test_strategy_for_anxiety_contains_explanation_and_timeline(mock_agent):
    """焦虑策略应包含详细解释与时间预期。"""
    result = mock_agent.analyze("到底什么时候能处理？我着急！", session_id="s7")
    assert result.emotion == EmotionType.ANXIETY
    strategy = result.strategy
    assert "解释" in strategy or "说明" in strategy
    assert "时间" in strategy or "预期" in strategy


def test_strategy_for_disappointment_contains_apology_and_solution(mock_agent):
    """失望策略应包含道歉与解决方案。"""
    result = mock_agent.analyze("太失望了，跟预期不一样。", session_id="s8")
    assert result.emotion == EmotionType.DISAPPOINTMENT
    strategy = result.strategy
    assert "道歉" in strategy or "抱歉" in strategy
    assert "解决" in strategy or "方案" in strategy


def test_strategy_for_satisfaction_contains_polite_and_review_invite(mock_agent):
    """满意策略应包含礼貌回应与邀请评价。"""
    result = mock_agent.analyze("太感谢了，服务很好！", session_id="s9")
    assert result.emotion == EmotionType.SATISFACTION
    strategy = result.strategy
    assert "礼貌" in strategy or "感谢" in strategy
    assert "评价" in strategy or "好评" in strategy


def test_strategy_for_neutral_contains_standard_flow(mock_agent):
    """中性策略应包含标准流程处理。"""
    result = mock_agent.analyze("请问退款流程是什么？", session_id="s10")
    assert result.emotion == EmotionType.NEUTRAL
    strategy = result.strategy
    assert "标准" in strategy or "流程" in strategy


# ==================== 转接判断边界测试 ====================


def test_anger_low_score_does_not_escalate(mock_agent):
    """愤怒但 score<=4 时不应强制转人工（仅强烈愤怒才转）。"""
    # 仅含一个轻度愤怒词，分数应<=4
    result = mock_agent.analyze("有点不满。", session_id="s11")
    # 不会判为 anger 高分；若判为 disappointment 则不应转人工
    assert result.suggest_escalate is False


def test_score_always_in_valid_range(mock_agent):
    """分数应始终在 1-5 区间内。"""
    queries = [
        "请问退款流程？",
        "谢谢，好评！",
        "着急，到底什么时候处理好？",
        "垃圾！投诉！差评！",
        "太失望了。",
    ]
    for query in queries:
        result = mock_agent.analyze(query)
        assert 1 <= result.score <= 5, f"分数越界：{query} -> {result.score}"


def test_confidence_in_valid_range(mock_agent):
    """置信度应始终在 0-1 区间内。"""
    queries = ["谢谢！", "投诉！", "请问一下。"]
    for query in queries:
        result = mock_agent.analyze(query)
        assert 0.0 <= result.confidence <= 1.0


def test_empty_query_returns_neutral(mock_agent):
    """空查询应返回中性兜底，不抛异常。"""
    result = mock_agent.analyze("", session_id="s12")
    assert result.emotion == EmotionType.NEUTRAL
    assert result.score == 1


# ==================== LLM 模式测试 ====================


def test_llm_mode_parses_json_response():
    """LLM 模式下应解析返回的 JSON 并填充 EmotionResult。"""
    llm_response = json.dumps(
        {
            "emotion": "anger",
            "score": 5,
            "confidence": 0.95,
            "keywords": ["投诉", "差评"],
        },
        ensure_ascii=False,
    )
    fake_client = _FakeRealLLMClient(response=llm_response)
    agent = EmotionAgent(llm_client=fake_client)

    result = agent.analyze("我要投诉！", session_id="s13")

    assert result.emotion == EmotionType.ANGER
    assert result.score == 5
    assert result.confidence == 0.95
    assert "投诉" in result.keywords
    # LLM 返回 anger score=5 时也应触发转人工
    assert result.suggest_escalate is True
    # 应构造 system + user 两条消息
    assert fake_client.last_messages is not None
    assert len(fake_client.last_messages) == 2
    assert fake_client.last_messages[0]["role"] == "system"


def test_llm_mode_invalid_json_falls_back_to_rules():
    """LLM 返回非法 JSON 时应降级到规则兜底，不抛异常。"""
    fake_client = _FakeRealLLMClient(response="这不是JSON")
    agent = EmotionAgent(llm_client=fake_client)

    result = agent.analyze("垃圾服务，投诉！", session_id="s14")

    # 降级后仍应识别出愤怒
    assert result.emotion == EmotionType.ANGER
    assert result.score > 4


def test_llm_mode_out_of_range_score_clamped():
    """LLM 返回的 score 越界时应被 clamp 到 1-5。"""
    llm_response = json.dumps(
        {
            "emotion": "anxiety",
            "score": 99,
            "confidence": 0.8,
            "keywords": ["着急"],
        },
        ensure_ascii=False,
    )
    fake_client = _FakeRealLLMClient(response=llm_response)
    agent = EmotionAgent(llm_client=fake_client)

    result = agent.analyze("着急啊！")
    assert result.score == 5  # clamp 到上界


# ==================== 演示：5 类情绪识别结果 ====================


def test_demo_five_emotions_recognition(mock_agent, capsys):
    """演示：5 类情绪各一个示例的识别结果。"""
    samples = [
        ("垃圾服务！我要投诉，必须给差评！", "愤怒"),
        ("怎么还没人回复？到底什么时候处理？我着急啊！", "焦虑"),
        ("太失望了，跟宣传的完全不一样。", "失望"),
        ("太感谢了，服务很专业，必须好评！", "满意"),
        ("请问一下退货流程是怎样的？", "中性"),
    ]

    print("\n========== 情感分析演示 ==========")
    for query, expected_label in samples:
        result = mock_agent.analyze(query, session_id="demo")
        print(f"\n【输入】{query}")
        print(f"【预期】{expected_label}")
        print(
            f"【结果】emotion={result.emotion.value} "
            f"score={result.score} confidence={result.confidence:.2f} "
            f"escalate={result.suggest_escalate}"
        )
        print(f"【关键词】{result.keywords}")
        print(f"【策略】{result.strategy}")
    print("\n==================================")

    # 断言五类情绪都被正确识别
    emotions = [mock_agent.analyze(q).emotion for q, _ in samples]
    assert EmotionType.ANGER in emotions
    assert EmotionType.ANXIETY in emotions
    assert EmotionType.DISAPPOINTMENT in emotions
    assert EmotionType.SATISFACTION in emotions
    assert EmotionType.NEUTRAL in emotions
