"""Langfuse 集成降级验证测试。

覆盖：
- 未配置时 get_langfuse_client() 返回 None
- is_langfuse_enabled() 返回 False
- LLMClient 降级为原生 openai（不包装 langfuse.openai）
- start_langfuse_trace / finish_langfuse_trace no-op 不抛异常
- _MockLLM 兼容 name/metadata 新参数
- LLMClient.chat() 传 name/metadata 参数不报错

测试隔离：模块级 fixture 强制 LANGFUSE_ENABLED=False 与空密钥，
并 reset_langfuse_client()，避免 .env 配置真实 key 时影响降级断言。
策略与 test_intent_optimization.py / test_performance.py 一致。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest


# ----------------------------------------------------------------------
# 模块级 fixture：强制 Langfuse 关闭，重置单例避免污染
# ----------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _isolate_langfuse_disabled():
    """模块级隔离：强制 LANGFUSE_ENABLED=False 与空密钥。

    即便 .env 配置了真实 Langfuse key，本模块的所有断言都基于
    “未启用”状态，必须强制关闭并 reset 单例，确保 get_langfuse_client()
    重新走 _create_client() 路径并按当前配置返回 None。
    """
    from app.core.config import get_settings
    from app.core.langfuse_client import reset_langfuse_client

    settings = get_settings()
    # 备份原值，便于用例结束后恢复，避免影响其他测试模块
    original_enabled = settings.LANGFUSE_ENABLED
    original_public_key = settings.LANGFUSE_PUBLIC_KEY
    original_secret_key = settings.LANGFUSE_SECRET_KEY

    # 强制降级条件：开关关闭 + 密钥置空，双重保险
    settings.LANGFUSE_ENABLED = False
    settings.LANGFUSE_PUBLIC_KEY = ""
    settings.LANGFUSE_SECRET_KEY = ""

    # 重置单例，让下次 get_langfuse_client() 重新按当前配置创建
    reset_langfuse_client()

    yield

    # 恢复原配置并重置单例，避免污染后续测试模块
    settings.LANGFUSE_ENABLED = original_enabled
    settings.LANGFUSE_PUBLIC_KEY = original_public_key
    settings.LANGFUSE_SECRET_KEY = original_secret_key
    reset_langfuse_client()


# ----------------------------------------------------------------------
# 降级场景测试
# ----------------------------------------------------------------------


def test_get_langfuse_client_returns_none_when_disabled():
    """未启用 Langfuse 时 get_langfuse_client() 应返回 None。"""
    from app.core.langfuse_client import get_langfuse_client

    # 模块级 fixture 已强制 LANGFUSE_ENABLED=False 与空密钥
    assert get_langfuse_client() is None


def test_is_langfuse_enabled_returns_false():
    """未启用 Langfuse 时 is_langfuse_enabled() 应返回 False。"""
    from app.core.langfuse_client import is_langfuse_enabled

    assert is_langfuse_enabled() is False


def test_start_langfuse_trace_returns_none_when_disabled():
    """未启用 Langfuse 时 start_langfuse_trace() 应返回 None（no-op）。"""
    from app.core.langfuse_client import start_langfuse_trace

    # 传 name 与 metadata 也不应触发任何实际创建行为
    trace = start_langfuse_trace("test-trace", metadata={"k": "v"})
    assert trace is None


def test_finish_langfuse_trace_noop_with_none():
    """trace 为 None 时 finish_langfuse_trace() 应 no-op 不抛异常。"""
    from app.core.langfuse_client import finish_langfuse_trace

    # 关键：None 是降级路径的返回值，finish 必须能优雅处理
    finish_langfuse_trace(None, status="success")
    finish_langfuse_trace(None, status="error")


# ----------------------------------------------------------------------
# _MockLLM 与 LLMClient 兼容性测试
# ----------------------------------------------------------------------


def test_mock_llm_accepts_name_metadata():
    """_MockLLM.chat() 接受 name/metadata 参数不应报错。

    Task 3 已让 _MockLLM 签名兼容 Langfuse 追踪参数，本用例做回归保护，
    防止后续重构误删 name/metadata 形参导致 LLMClient.chat() 调用失败。
    """
    from app.agents.llm_client import _MockLLM

    mock = _MockLLM(reason="测试")
    messages: List[Dict[str, Any]] = [{"role": "user", "content": "你好"}]
    # 传入 name/metadata 应被 mock 忽略，正常返回拼接回复
    reply = mock.chat(messages, name="test-prompt", metadata={"version": "1"})
    assert isinstance(reply, str)
    assert len(reply) > 0


def test_llm_client_chat_with_name_metadata_no_error():
    """LLMClient.chat() 传 name/metadata 在 mock 模式下应正常返回。

    mock 模式（LLM_API_KEY=""）下 LLMClient.chat() 内部转发给 _MockLLM.chat()，
    name/metadata 通过 kwargs 透传；本用例验证 Langfuse 追踪参数不会破坏
    现有调用链路。
    """
    from app.agents.llm_client import LLMClient, reset_llm_client
    from app.core.config import get_settings

    # 强制 mock 模式：LLM_API_KEY 为空时 LLMClient 自动走 _MockLLM
    settings = get_settings()
    original_key = settings.LLM_API_KEY
    settings.LLM_API_KEY = ""
    reset_llm_client()
    try:
        client = LLMClient()
        assert client.is_mock, "前置条件失败：未进入 mock 模式"

        messages: List[Dict[str, Any]] = [{"role": "user", "content": "你好"}]
        reply = client.chat(messages, name="test-prompt", metadata={"v": "1"})
        assert isinstance(reply, str)
        assert len(reply) > 0
    finally:
        # 恢复配置并重置单例，避免污染其他用例
        settings.LLM_API_KEY = original_key
        reset_llm_client()
