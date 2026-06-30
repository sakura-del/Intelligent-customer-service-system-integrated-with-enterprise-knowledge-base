"""Task 14：分层摘要上下文管理 + 意图切换检测 测试。

覆盖范围：
1. 分层摘要策略：
   - 近 5 轮保留完整原文
   - 中期（6-15 轮）压缩为单句摘要
   - 早期（>15 轮）整体压缩为会话级摘要
2. 摘要 mock 模式可用（无 LLM 时走规则）
3. 意图切换检测：
   - 语义相似度 < 阈值 → 切换
   - 用户明示关键词 → 切换
   - 与历史意图一致 → 不切换
4. 槽位重置：切换后 slots 清空，history 保留
5. 长对话（>15 轮）上下文不失忆：早期摘要保留要点
6. 缓存命中：相同内容不重复计算
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.core.context_manager import (
    ContextManager,
    IntentDetector,
    RECENT_TURNS,
    MID_TURNS,
    EARLY_THRESHOLD,
    SWITCH_SIMILARITY_THRESHOLD,
    _cosine_similarity,
    get_context_manager,
    get_intent_detector,
    reset_context_manager,
    reset_intent_detector,
)
from app.core.session import session_manager
from app.schemas.context import DialogContext, IntentSwitchResult


# ==================== 测试辅助：可控的 mock 组件 ====================


class _MockLLMClient:
    """Mock LLM 客户端：is_mock=True 触发规则摘要路径。

    规则摘要不依赖外部服务，保证测试稳定可复现。
    """

    is_mock = True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> str:
        return ""


class _FakeRealLLMClient:
    """模拟真实 LLM 客户端：is_mock=False，返回预设摘要。

    用于测试 LLM 模式下的摘要生成与降级容错。
    """

    def __init__(self, response: str = "LLM 生成的摘要") -> None:
        self._response = response
        self.is_mock = False
        self.call_count = 0

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> str:
        self.call_count += 1
        return self._response


class _FakeEmbeddingService:
    """可控 embedding 服务：按文本映射返回预设向量。

    用于测试意图切换的相似度判断，避免依赖真实 BGE 模型。
    """

    def __init__(self, vector_map: Dict[str, List[float]]) -> None:
        self.vector_map = vector_map

    def embed_query(self, text: str) -> List[float]:
        # 命中预设向量；未命中返回零向量，便于测试 fallback
        return list(self.vector_map.get(text, [0.0] * 4))


# ==================== fixture ====================


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前重置 session_manager 与相关单例，避免缓存污染。"""
    session_manager.reset_all()
    reset_context_manager()
    reset_intent_detector()
    yield
    session_manager.reset_all()
    reset_context_manager()
    reset_intent_detector()


def _make_history(turns: int) -> List[Dict[str, Any]]:
    """构造指定轮数的 history 列表。

    每轮含 user + assistant 两条记录，便于测试分层边界。
    """
    history: List[Dict[str, Any]] = []
    for i in range(turns):
        history.append({"role": "user", "content": f"用户第{i + 1}轮问题"})
        history.append({"role": "assistant", "content": f"客服第{i + 1}轮回复"})
    return history


def _create_session_with_history(history: List[Dict[str, Any]]) -> str:
    """创建会话并通过 update_session 写入 history。

    用 update_session 而非 append_history，可绕过 FIFO 限制，
    便于测试长对话（>MAX_HISTORY_LENGTH）场景。
    """
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(session_id, history=history)
    return session_id


# ==================== 分层摘要策略测试 ====================


def test_layered_summary_recent_only_keeps_full_text():
    """短对话（≤5 轮）：全部保留为近期原文，无中期/早期摘要。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    history = _make_history(RECENT_TURNS)
    session_id = _create_session_with_history(history)

    ctx = manager.build_context(session_id)

    assert isinstance(ctx, DialogContext)
    # 近期保留全部 5 轮原文
    assert len(ctx.recent_turns) == RECENT_TURNS
    # 短对话无中期摘要、无早期摘要
    assert ctx.mid_summary == []
    assert ctx.early_summary == ""
    # full_context_text 应包含近期原文标识
    assert "近期对话原文" in ctx.full_context_text
    assert "中期对话摘要" not in ctx.full_context_text


def test_layered_summary_mid_turns_compressed_to_single_sentence():
    """中期对话（6-15 轮）：6-15 轮压缩为单句摘要，近 5 轮保留原文。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    total_turns = 10  # 5 近期 + 5 中期
    history = _make_history(total_turns)
    session_id = _create_session_with_history(history)

    ctx = manager.build_context(session_id)

    # 近期 5 轮原文
    assert len(ctx.recent_turns) == RECENT_TURNS
    # 中期 5 轮单句摘要
    assert len(ctx.mid_summary) == total_turns - RECENT_TURNS
    # 每条摘要非空
    for summary in ctx.mid_summary:
        assert summary
    # 早期摘要为空（未达 15 轮阈值）
    assert ctx.early_summary == ""
    # full_text 应同时包含近期与中期
    assert "近期对话原文" in ctx.full_context_text
    assert "中期对话摘要" in ctx.full_context_text


def test_layered_summary_early_turns_compressed_to_session_summary():
    """长对话（>15 轮）：早期对话整体压缩为会话级摘要。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    # 构造 20 轮：5 近期 + 10 中期 + 5 早期
    total_turns = EARLY_THRESHOLD + 5
    history = _make_history(total_turns)
    session_id = _create_session_with_history(history)

    ctx = manager.build_context(session_id)

    # 近期 5 轮原文
    assert len(ctx.recent_turns) == RECENT_TURNS
    # 中期 10 轮单句摘要
    assert len(ctx.mid_summary) == MID_TURNS
    # 早期会话级摘要非空
    assert ctx.early_summary
    # full_text 应包含三层标识
    assert "早期对话摘要" in ctx.full_context_text
    assert "中期对话摘要" in ctx.full_context_text
    assert "近期对话原文" in ctx.full_context_text


def test_layered_summary_threshold_boundary():
    """分层边界：恰好 15 轮时应只有近期+中期，无早期。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    history = _make_history(EARLY_THRESHOLD)
    session_id = _create_session_with_history(history)

    ctx = manager.build_context(session_id)

    assert len(ctx.recent_turns) == RECENT_TURNS
    assert len(ctx.mid_summary) == MID_TURNS
    # 恰好 15 轮：无早期
    assert ctx.early_summary == ""


# ==================== 摘要 mock 模式测试 ====================


def test_summarize_turn_mock_mode_returns_rule_based():
    """mock 模式下 summarize_turn 应走规则摘要，取首句要点。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    turn = {
        "user": "忘记登录密码怎么办？请帮我重置。",
        "assistant": "您可以通过登录页的忘记密码链接重置。",
    }

    summary = manager.summarize_turn(turn)

    # 规则摘要应包含用户首句
    assert "忘记登录密码" in summary
    # 应包含客服首句要点
    assert "重置" in summary


def test_summarize_session_mock_mode_returns_rule_based():
    """mock 模式下 summarize_session 应按时间顺序拼接每轮要点。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    turns = [
        {"user": "查订单物流。", "assistant": "已发货。"},
        {"user": "退款流程是什么？", "assistant": "7 天内可申请。"},
    ]

    summary = manager.summarize_session(turns)

    # 应包含两轮的用户首句
    assert "查订单物流" in summary
    assert "退款流程" in summary
    # 应按时间顺序标注轮次
    assert "第1轮" in summary
    assert "第2轮" in summary


def test_summarize_turn_llm_mode_uses_llm_response():
    """LLM 模式下 summarize_turn 应调用 LLM 返回摘要。"""
    fake_llm = _FakeRealLLMClient(response="用户问密码重置，客服指引登录页操作")
    manager = ContextManager(llm_client=fake_llm)
    turn = {"user": "忘记密码", "assistant": "用登录页链接重置"}

    summary = manager.summarize_turn(turn)

    assert summary == "用户问密码重置，客服指引登录页操作"
    assert fake_llm.call_count == 1


def test_summarize_turn_llm_empty_response_falls_back_to_rule():
    """LLM 返回空时应降级到规则摘要，保证可用。"""
    fake_llm = _FakeRealLLMClient(response="")
    manager = ContextManager(llm_client=fake_llm)
    turn = {"user": "忘记密码怎么办", "assistant": "走重置流程"}

    summary = manager.summarize_turn(turn)

    # 降级规则应包含用户首句
    assert "忘记密码" in summary


# ==================== 缓存测试 ====================


def test_summarize_turn_cache_avoids_repeated_llm_calls():
    """相同 turn 内容应命中缓存，不重复调用 LLM。"""
    fake_llm = _FakeRealLLMClient(response="LLM 摘要")
    manager = ContextManager(llm_client=fake_llm)
    turn = {"user": "问题A", "assistant": "回答A"}

    # 第一次调用：LLM 被调用一次
    summary1 = manager.summarize_turn(turn)
    assert fake_llm.call_count == 1

    # 第二次相同内容：应命中缓存，LLM 不再被调用
    summary2 = manager.summarize_turn(turn)
    assert fake_llm.call_count == 1
    assert summary1 == summary2


def test_clear_cache_forces_recompute():
    """clear_cache 后再次调用应重新计算。"""
    fake_llm = _FakeRealLLMClient(response="LLM 摘要")
    manager = ContextManager(llm_client=fake_llm)
    turn = {"user": "问题A", "assistant": "回答A"}

    manager.summarize_turn(turn)
    assert fake_llm.call_count == 1

    manager.clear_cache()
    manager.summarize_turn(turn)
    # 清缓存后应重新调用 LLM
    assert fake_llm.call_count == 2


# ==================== 意图切换检测测试 ====================


def test_intent_switch_explicit_keyword_triggers_switch():
    """用户明示"换个问题"应直接判定切换。"""
    # 先建一个有历史的会话
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="knowledge_qa",
        history=[{"role": "user", "content": "忘记密码怎么办"}],
    )
    detector = IntentDetector(
        embedding_service=_FakeEmbeddingService({})
    )

    result = detector.detect_switch(
        "换个问题，我想查订单物流", session_id, "knowledge_qa"
    )

    assert result.switched is True
    assert "明示" in result.reason
    # 新意图应识别为 business_query（含"订单"关键词）
    assert result.new_intent == "business_query"


def test_intent_switch_zero_similarity_does_not_switch():
    """similarity=0 视为 embedding 不可用，不应误判为切换。

    设计考量：embedding 服务故障时所有相似度都为 0，
    若此时判定切换会导致所有 query 都被误判为切换。
    因此 similarity=0 时仅在 current_intent 非空且 similarity>0 时才切换。
    """
    # 构造 embedding：query 与历史主题正交，相似度 0
    vector_map = {
        "忘记密码": [1.0, 0.0, 0.0, 0.0],
        "推荐一款手机": [0.0, 1.0, 0.0, 0.0],
    }
    detector = IntentDetector(
        embedding_service=_FakeEmbeddingService(vector_map)
    )
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="knowledge_qa",
        history=[{"role": "user", "content": "忘记密码"}],
    )

    result = detector.detect_switch(
        "推荐一款手机", session_id, "knowledge_qa"
    )

    # similarity=0 视为不可用，不触发切换
    assert result.switched is False
    assert result.similarity == 0.0


def test_intent_switch_low_similarity_with_positive_score_triggers_switch():
    """相似度 > 0 但低于阈值应触发切换。"""
    # 构造 embedding：相似度 0.3（< 0.6 阈值）
    # 用归一化向量：cosine = 0.3
    import math
    angle = math.acos(0.3)
    v1 = [1.0, 0.0, 0.0, 0.0]
    v2 = [math.cos(angle), math.sin(angle), 0.0, 0.0]
    vector_map = {
        "忘记密码怎么办": v1,
        "推荐一款手机": v2,
    }
    detector = IntentDetector(
        embedding_service=_FakeEmbeddingService(vector_map)
    )
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="knowledge_qa",
        history=[{"role": "user", "content": "忘记密码怎么办"}],
    )

    result = detector.detect_switch(
        "推荐一款手机", session_id, "knowledge_qa"
    )

    assert result.switched is True
    assert result.similarity < SWITCH_SIMILARITY_THRESHOLD
    assert "语义相似度" in result.reason


def test_intent_switch_high_similarity_no_switch():
    """query 与历史主题高度相似时不切换。"""
    # 完全相同向量 → 相似度 1.0
    vector_map = {
        "忘记密码怎么办": [1.0, 0.0, 0.0, 0.0],
        "密码忘了怎么重置": [1.0, 0.0, 0.0, 0.0],
    }
    detector = IntentDetector(
        embedding_service=_FakeEmbeddingService(vector_map)
    )
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="knowledge_qa",
        history=[{"role": "user", "content": "忘记密码怎么办"}],
    )

    result = detector.detect_switch(
        "密码忘了怎么重置", session_id, "knowledge_qa"
    )

    assert result.switched is False
    assert result.similarity == 1.0
    assert "保持一致" in result.reason


def test_intent_switch_first_turn_no_switch():
    """首轮对话（无 current_intent）不应触发切换。"""
    detector = IntentDetector(
        embedding_service=_FakeEmbeddingService({})
    )
    session_id = session_manager.create_session(channel="test")
    # 首轮：current_intent 为 None，history 为空

    result = detector.detect_switch("你好", session_id, None)

    assert result.switched is False


def test_intent_switch_empty_query_no_switch():
    """空 query 不应触发切换。"""
    detector = IntentDetector(
        embedding_service=_FakeEmbeddingService({})
    )
    session_id = session_manager.create_session(channel="test")

    result = detector.detect_switch("", session_id, "knowledge_qa")

    assert result.switched is False
    assert "为空" in result.reason


def test_intent_switch_embedding_cache_hit():
    """相同文本应命中 embedding 缓存，不重复调用 embed_query。"""
    call_count = {"count": 0}

    class _CountingEmbeddingService:
        def embed_query(self, text: str) -> List[float]:
            call_count["count"] += 1
            return [1.0, 0.0, 0.0, 0.0]

    detector = IntentDetector(
        embedding_service=_CountingEmbeddingService()
    )
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="knowledge_qa",
        history=[{"role": "user", "content": "相同问题"}],
    )

    # 第一次：embed_query 被调用 2 次（query + history）
    detector.detect_switch("相同问题", session_id, "knowledge_qa")
    first_count = call_count["count"]

    # 第二次相同文本：应命中缓存，调用次数不增加
    detector.detect_switch("相同问题", session_id, "knowledge_qa")
    assert call_count["count"] == first_count


# ==================== 槽位重置测试 ====================


def test_reset_slots_clears_slots_keeps_history():
    """reset_slots 应清空 slots 但保留 history 与 turn_count。"""
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="business_query",
        slots={"order_id": "12345", "product": "手机"},
        history=[
            {"role": "user", "content": "查订单"},
            {"role": "assistant", "content": "已查到"},
        ],
        turn_count=3,
    )

    success = session_manager.reset_slots(session_id)

    assert success is True
    session = session_manager.get_session(session_id)
    assert session["slots"] == {}
    # history 与 turn_count 应保留
    assert len(session["history"]) == 2
    assert session["turn_count"] == 3


def test_reset_slots_nonexistent_session_returns_false():
    """不存在的 session_id 应返回 False。"""
    assert session_manager.reset_slots("nonexistent-id") is False


def test_intent_switch_triggers_slot_reset_in_graph():
    """graph 中检测到意图切换应触发 reset_slots。"""
    from app.agents.graph import intent_node
    from app.core.context_manager import reset_intent_detector

    # 注入可控 IntentDetector：明示切换
    from app.core import context_manager as ctx_module

    class _AlwaysSwitchDetector:
        def detect_switch(self, query, session_id, current_intent):
            return IntentSwitchResult(
                switched=True,
                new_intent="business_query",
                reason="测试切换",
                similarity=0.0,
            )

    ctx_module._intent_detector = _AlwaysSwitchDetector()

    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="knowledge_qa",
        slots={"old_slot": "value"},
        history=[{"role": "user", "content": "上一轮问题"}],
    )

    state = {
        "session_id": session_id,
        "message": "查我的订单物流",
        "intent": "unknown",
        "sub_tasks": [],
        "emotion_score": 0.0,
        "turn_count": 1,
        "failed_attempts": 0,
        "history": [],
        "raw_results": {},
        "final_reply": "",
        "sources": [],
        "escalate_to_human": False,
        "escalation_card": None,
        "trace_id": None,
        "layered_context_text": "",
        "intent_switch": None,
    }

    try:
        intent_node(state)
        # 切换后 slots 应被清空
        session = session_manager.get_session(session_id)
        assert session["slots"] == {}
        # intent_switch 应写入 state
        assert state.get("intent_switch") is not None
        assert state["intent_switch"]["switched"] is True
    finally:
        reset_intent_detector()


# ==================== 长对话不失忆测试 ====================


def test_long_dialog_early_summary_preserves_key_info():
    """长对话（>15 轮）的早期要点应保留在 early_summary 中。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    # 构造 20 轮，早期 5 轮包含特殊关键词
    history: List[Dict[str, Any]] = []
    for i in range(EARLY_THRESHOLD + 5):
        # 早期 5 轮注入特殊关键词，验证规则摘要保留
        if i < 5:
            user = f"第{i + 1}轮特殊关键词VIP用户投诉"
        else:
            user = f"第{i + 1}轮普通问题"
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": f"回复{i + 1}"})

    session_id = _create_session_with_history(history)
    ctx = manager.build_context(session_id)

    # 早期摘要应包含特殊关键词（规则摘要取首句）
    assert "VIP用户投诉" in ctx.early_summary or "特殊关键词" in ctx.early_summary
    # 近期 5 轮应保留原文
    assert len(ctx.recent_turns) == RECENT_TURNS
    # 中期 10 轮单句摘要
    assert len(ctx.mid_summary) == MID_TURNS


def test_long_dialog_token_reduction():
    """LLM 模式下分层摘要后 full_context_text 应显著短于原始 history。

    使用 LLM 模式（mock LLM 返回短摘要）验证分层策略的 token 降低效果，
    目标降低率 60%+。mock 模式下规则摘要压缩率有限，此处用 LLM 模式
    更贴近生产场景。原文采用真实长对话风格（每条 80+ 字符），
    让近期原文层占比合理，凸显中期/早期摘要的压缩效果。
    """
    # LLM 返回短摘要，模拟真实 LLM 的压缩能力
    fake_llm = _FakeRealLLMClient(response="本轮要点摘要")
    manager = ContextManager(llm_client=fake_llm)
    # 构造 20 轮长对话，每轮内容较长（贴近真实客服场景）
    history: List[Dict[str, Any]] = []
    for i in range(EARLY_THRESHOLD + 5):
        history.append({
            "role": "user",
            "content": (
                f"第{i + 1}轮：您好，我之前下单买的商品想要咨询一下具体的"
                f"使用方法和注意事项，另外想了解退换货政策，能否详细说明一下？"
            ),
        })
        history.append({
            "role": "assistant",
            "content": (
                f"第{i + 1}轮回复：您好~ 关于您提到的商品使用方法，"
                f"建议您先阅读说明书，按照步骤操作。退换货方面，"
                f"7 天内可申请，需保持商品原包装完整。"
            ),
        })

    session_id = _create_session_with_history(history)
    ctx = manager.build_context(session_id)

    # 原始 history 总字符数
    original_chars = sum(
        len(h.get("content", "")) for h in history
    )
    # 分层后字符数
    layered_chars = len(ctx.full_context_text)

    # token 消耗应降低 60%+（字符数近似 token 数）
    reduction = (original_chars - layered_chars) / original_chars
    assert reduction >= 0.6, (
        f"token 降低率 {reduction:.2%} 未达 60% 目标 "
        f"(original={original_chars}, layered={layered_chars})"
    )


# ==================== 单例与工具函数测试 ====================


def test_get_context_manager_returns_singleton():
    """get_context_manager 应返回同一单例。"""
    m1 = get_context_manager()
    m2 = get_context_manager()
    assert m1 is m2


def test_get_intent_detector_returns_singleton():
    """get_intent_detector 应返回同一单例。"""
    d1 = get_intent_detector()
    d2 = get_intent_detector()
    assert d1 is d2


def test_cosine_similarity_orthogonal_vectors_returns_zero():
    """正交向量相似度应为 0。"""
    v1 = [1.0, 0.0]
    v2 = [0.0, 1.0]
    assert _cosine_similarity(v1, v2) == 0.0


def test_cosine_similarity_identical_vectors_returns_one():
    """相同向量相似度应为 1。"""
    v = [1.0, 2.0, 3.0]
    assert _cosine_similarity(v, v) == 1.0


def test_cosine_similarity_empty_vectors_returns_zero():
    """空向量或长度不一致应返回 0，避免除零。"""
    assert _cosine_similarity([], [1.0]) == 0.0
    assert _cosine_similarity([1.0, 2.0], [1.0]) == 0.0


def test_build_context_nonexistent_session_returns_empty():
    """不存在的 session_id 应返回空 DialogContext。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    ctx = manager.build_context("nonexistent-session")
    assert ctx.recent_turns == []
    assert ctx.mid_summary == []
    assert ctx.early_summary == ""
    assert ctx.full_context_text == ""


def test_build_context_empty_history_returns_empty():
    """空 history 应返回空 DialogContext。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    session_id = session_manager.create_session(channel="test")
    ctx = manager.build_context(session_id)
    assert ctx.recent_turns == []
    assert ctx.full_context_text == ""


# ==================== 演示：分层摘要与意图切换 ====================


def test_demo_layered_summary_output(capsys):
    """演示：构造 20 轮对话，展示分层摘要输出。"""
    manager = ContextManager(llm_client=_MockLLMClient())
    # 构造 20 轮真实风格对话
    history: List[Dict[str, Any]] = []
    demo_turns = [
        ("忘记登录密码怎么办？", "您可以通过登录页的忘记密码链接重置。"),
        ("怎么修改收货地址？", "在订单详情页可以修改收货地址。"),
        ("订单什么时候发货？", "普通订单 24 小时内发货。"),
        ("支持哪些支付方式？", "支持微信、支付宝、银行卡。"),
        ("会员等级怎么提升？", "消费累计达标即可升级。"),
        ("退款多久到账？", "原路返回 3-5 个工作日。"),
        ("怎么申请发票？", "订单完成后可在详情页申请。"),
        ("商品有质量问题怎么办？", "可申请退换货，我们会安排处理。"),
        ("库存什么时候补？", "一般 3-5 天补货，可设置到货提醒。"),
        ("怎么联系人工客服？", "工作时间可拨打客服热线。"),
        ("优惠券怎么用？", "下单时在结算页选择使用。"),
        ("积分能兑换什么？", "积分商城可兑换优惠券和礼品。"),
        ("怎么取消订单？", "发货前可在订单详情页取消。"),
        ("配送范围有哪些？", "全国大部分地区都支持配送。"),
        ("怎么查看物流？", "订单详情页有实时物流跟踪。"),
        ("商品保质期多久？", "不同商品保质期不同，详见页面说明。"),
        ("怎么注册账号？", "手机号一键注册即可。"),
        ("能修改手机号吗？", "在账户设置里可以修改绑定手机号。"),
        ("怎么注销账号？", "联系客服可协助注销。"),
        ("最后问一下会员积分规则", "消费 1 元累计 1 积分，等级越高倍数越大。"),
    ]
    for user, asst in demo_turns:
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": asst})

    session_id = _create_session_with_history(history)
    ctx = manager.build_context(session_id)

    print("\n========== 分层摘要演示 ==========")
    print(f"原始对话：{len(demo_turns)} 轮，{len(history)} 条 history")
    print(f"\n【早期对话摘要】({len(ctx.early_summary)} 字)")
    print(ctx.early_summary)
    print(f"\n【中期对话摘要】({len(ctx.mid_summary)} 条)")
    for i, s in enumerate(ctx.mid_summary, 1):
        print(f"  {i}. {s}")
    print(f"\n【近期对话原文】({len(ctx.recent_turns)} 轮)")
    for t in ctx.recent_turns:
        print(f"  用户：{t.get('user', '')}")
        print(f"  客服：{t.get('assistant', '')}")
    print(f"\n完整上下文文本长度：{len(ctx.full_context_text)} 字符")
    print("==================================")

    # 断言分层结构正确
    assert len(ctx.recent_turns) == RECENT_TURNS
    assert len(ctx.mid_summary) == MID_TURNS
    assert ctx.early_summary
    # 早期摘要应包含首轮关键词
    assert "登录密码" in ctx.early_summary or "忘记密码" in ctx.early_summary


def test_demo_intent_switch(capsys):
    """演示：意图切换检测场景。"""
    # 场景 1：用户明示切换
    session_id = session_manager.create_session(channel="test")
    session_manager.update_session(
        session_id,
        current_intent="knowledge_qa",
        history=[
            {"role": "user", "content": "忘记密码怎么办"},
            {"role": "assistant", "content": "通过登录页重置"},
        ],
    )
    detector = IntentDetector(
        embedding_service=_FakeEmbeddingService({})
    )

    print("\n========== 意图切换演示 ==========")
    print("场景 1：用户明示切换")
    print("当前意图：knowledge_qa（密码重置）")
    print("用户输入：换个问题，我想查订单物流")
    result1 = detector.detect_switch(
        "换个问题，我想查订单物流", session_id, "knowledge_qa"
    )
    print(f"切换结果：switched={result1.switched}, "
          f"new_intent={result1.new_intent}, reason={result1.reason}")
    assert result1.switched is True

    # 场景 2：语义相似度低触发切换
    import math
    angle = math.acos(0.3)
    vector_map = {
        "忘记密码怎么办": [1.0, 0.0, 0.0, 0.0],
        "推荐一款性价比高的手机": [math.cos(angle), math.sin(angle), 0.0, 0.0],
    }
    detector2 = IntentDetector(
        embedding_service=_FakeEmbeddingService(vector_map)
    )
    print("\n场景 2：语义相似度低触发切换")
    print("当前意图：knowledge_qa（密码重置）")
    print("用户输入：推荐一款性价比高的手机")
    result2 = detector2.detect_switch(
        "推荐一款性价比高的手机", session_id, "knowledge_qa"
    )
    print(f"切换结果：switched={result2.switched}, "
          f"similarity={result2.similarity:.2f}, reason={result2.reason}")
    assert result2.switched is True

    # 场景 3：与当前主题一致，不切换
    vector_map3 = {
        "忘记密码怎么办": [1.0, 0.0, 0.0, 0.0],
        "密码忘了怎么重置": [1.0, 0.0, 0.0, 0.0],
    }
    detector3 = IntentDetector(
        embedding_service=_FakeEmbeddingService(vector_map3)
    )
    print("\n场景 3：与当前主题一致，不切换")
    print("当前意图：knowledge_qa（密码重置）")
    print("用户输入：密码忘了怎么重置")
    result3 = detector3.detect_switch(
        "密码忘了怎么重置", session_id, "knowledge_qa"
    )
    print(f"切换结果：switched={result3.switched}, "
          f"similarity={result3.similarity:.2f}, reason={result3.reason}")
    assert result3.switched is False

    print("\n场景 4：切换后槽位重置")
    session_manager.update_session(
        session_id,
        slots={"old_intent_slot": "password_reset_token"},
    )
    print(f"切换前 slots：{session_manager.get_session(session_id)['slots']}")
    session_manager.reset_slots(session_id)
    print(f"切换后 slots：{session_manager.get_session(session_id)['slots']}")
    print(f"history 保留：{len(session_manager.get_session(session_id)['history'])} 条")
    print("==================================")
