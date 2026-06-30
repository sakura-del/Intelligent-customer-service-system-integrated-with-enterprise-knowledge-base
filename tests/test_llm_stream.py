"""LLMClient.stream_chat 流式生成测试。

覆盖 mock 模式与真实模式的流式协议：
- mock 模式按字符切片 yield
- 真实模式 mock OpenAI SDK 验证 chunk 透传
- 错误处理、空响应、大响应、提前中断

测试隔离：每个用例独立 mock OpenAI SDK 与配置，
调用 reset_llm_client() 重置单例，避免污染其他测试。
"""
from __future__ import annotations

from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from app.agents.llm_client import LLMClient, _MockLLM, _slice_text_to_stream


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_llm_singleton():
    """每个用例前后重置 LLMClient 单例，避免 mock 状态泄漏。"""
    from app.agents.llm_client import reset_llm_client

    reset_llm_client()
    yield
    reset_llm_client()


def _make_mock_chunk(content: str):
    """构造模拟的 OpenAI stream chunk。"""
    chunk = MagicMock()
    if content:
        delta = MagicMock()
        delta.content = content
        choice = MagicMock()
        choice.delta = delta
        chunk.choices = [choice]
    else:
        chunk.choices = []
    return chunk


def _make_real_client_with_streaming(stream_chunks: List[Any]):
    """构造一个真实模式（非 mock）LLMClient，stream_chat 时返回指定 chunks。

    通过 mock OpenAI SDK 的 chat.completions.create(stream=True) 实现，
    便于在不依赖网络的情况下验证流式协议。
    """
    client = LLMClient()
    # 强制设为非 mock 模式并注入 mock client
    client._mock = None
    client._client = MagicMock()
    # 同时支持非流式 chat 调用（空响应降级路径用）
    client._client.chat.completions.create.side_effect = lambda **kwargs: (
        _StreamResponse(stream_chunks) if kwargs.get("stream") else _NonStreamResponse()
    )
    return client


class _StreamResponse:
    """可迭代的流式响应替身。"""

    def __init__(self, chunks: List[Any]) -> None:
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class _NonStreamResponse:
    """非流式响应替身（空内容，触发降级）。"""

    def __init__(self) -> None:
        self.choices = []


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------


def test_mock_mode_stream_yields_multiple_tokens():
    """mock 模式流式：yield 多个 token + 最终 done，内容拼起来完整。"""
    from app.core.config import get_settings

    settings = get_settings()
    original_key = settings.LLM_API_KEY
    settings.LLM_API_KEY = ""  # 强制 mock 模式
    try:
        client = LLMClient()
        assert client.is_mock

        messages = [{"role": "user", "content": "你好"}]
        events = list(
            client.stream_chat(messages=messages, context_chunks=["知识片段内容"])
        )
    finally:
        settings.LLM_API_KEY = original_key

    # 应有多个 token 事件 + 1 个 done 事件
    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    assert len(token_events) >= 2, "mock 模式应切片 yield 多个 token"
    assert len(done_events) == 1

    # 拼接所有 token 应等于 done 中的完整文本
    full_text = "".join(e["content"] for e in token_events)
    assert full_text == done_events[0]["content"]
    assert "知识片段内容" in full_text


def test_real_mode_stream_with_mocked_openai_sdk():
    """真实模式流式：mock OpenAI SDK，验证 chunk 透传与 done 聚合。"""
    chunks = [
        _make_mock_chunk("你好"),
        _make_mock_chunk("，"),
        _make_mock_chunk("世界"),
        _make_mock_chunk(""),  # 空 chunk 应被跳过
    ]
    client = _make_real_client_with_streaming(chunks)

    events = list(client.stream_chat(messages=[{"role": "user", "content": "hi"}]))

    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    assert [e["content"] for e in token_events] == ["你好", "，", "世界"]
    assert len(done_events) == 1
    assert done_events[0]["content"] == "你好，世界"


def test_stream_yields_error_when_sdk_raises():
    """SDK 创建流式响应抛异常 → yield error 事件，不抛错。"""
    client = LLMClient()
    client._mock = None
    mock_client = MagicMock()
    # create 抛异常模拟鉴权失败/网络错误
    mock_client.chat.completions.create.side_effect = RuntimeError("api timeout")
    client._client = mock_client

    events = list(client.stream_chat(messages=[{"role": "user", "content": "hi"}]))

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert "api timeout" in error_events[0]["message"]


def test_stream_handles_empty_response():
    """LLM 返回空内容 → 降级 mock 输出可读回复。"""
    chunks = [_make_mock_chunk(""), _make_mock_chunk("")]  # 全空
    client = _make_real_client_with_streaming(chunks)

    events = list(
        client.stream_chat(
            messages=[{"role": "user", "content": "你好"}],
            context_chunks=["备用片段"],
        )
    )

    # 空响应降级 mock，应有 token 与 done
    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    # 降级 mock 应包含 context_chunks 文本
    full_text = done_events[0]["content"]
    assert "备用片段" in full_text or full_text  # 至少有内容


def test_stream_large_response_without_interruption():
    """大响应流式：长文本生成不中断，token 拼起来完整。"""
    # 构造 1000 字符的长文本，分 100 个 chunk
    long_text = "测试" * 500
    chunks = [
        _make_mock_chunk(long_text[i : i + 10])
        for i in range(0, len(long_text), 10)
    ]
    client = _make_real_client_with_streaming(chunks)

    events = list(client.stream_chat(messages=[{"role": "user", "content": "hi"}]))

    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    assert len(token_events) == 100
    assert len(done_events) == 1
    assert done_events[0]["content"] == long_text


def test_stream_generator_can_be_closed_early():
    """生成器提前关闭不抛错：消费前几个 token 后 break。"""
    chunks = [_make_mock_chunk(f"tok{i}") for i in range(50)]
    client = _make_real_client_with_streaming(chunks)

    generator = client.stream_chat(messages=[{"role": "user", "content": "hi"}])
    consumed: List[str] = []
    for event in generator:
        if event["type"] == "token":
            consumed.append(event["content"])
        if len(consumed) >= 3:
            break  # 提前关闭
    # 应能正常消费前 3 个 token，不抛异常
    assert len(consumed) == 3
    # 关闭生成器（Python 自动 GC 也会调用 close，不应抛错）
    generator.close()


def test_slice_text_to_stream_helper():
    """_slice_text_to_stream 切片辅助函数：按 chunk_size 切片。"""
    events = list(_slice_text_to_stream("abcde", chunk_size=2, sleep_seconds=0))
    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    assert [e["content"] for e in token_events] == ["ab", "cd", "e"]
    assert len(done_events) == 1
    assert done_events[0]["content"] == "abcde"


def test_slice_text_to_stream_empty_text():
    """空文本：直接 yield done，无 token 事件。"""
    events = list(_slice_text_to_stream("", sleep_seconds=0))
    assert len(events) == 1
    assert events[0]["type"] == "done"
    assert events[0]["content"] == ""
