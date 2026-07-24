"""ModelRouter 竞态条件修复测试。

验证 Task：消除 chat_with_routing 中通过修改 main_client.model 属性
切换模型导致的竞态条件，改为通过 model 参数传递。

覆盖场景：
1. LLMClient.chat 接受 model 参数并使用该模型（不修改 self.model）
2. LLMClient.chat model=None 时使用 self.model（向后兼容）
3. LLMClient.stream_chat 同上
4. chat_with_routing 不修改 main_client.model 属性
5. 多线程并发调用 chat，每个使用不同 model，验证无错配

测试隔离：
- 重置性能模块与 LLMClient 单例，避免与其他测试模块相互污染
- 强制 SMALL_LLM_API_KEY="" 避免 small_client 干扰 main_client 路径测试
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest


# ----------------------------------------------------------------------
# Fake OpenAI 客户端：记录 chat.completions.create 的调用参数
# ----------------------------------------------------------------------
class _FakeDelta:
    """模拟 OpenAI stream chunk 的 delta 对象。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeStreamChoice:
    """模拟 OpenAI stream chunk 的 choice 对象。"""

    def __init__(self, content: str) -> None:
        self.delta = _FakeDelta(content)


class _FakeChunk:
    """模拟 OpenAI stream chunk。"""

    def __init__(self, content: str) -> None:
        self.choices = [_FakeStreamChoice(content)]


class _FakeMessage:
    """模拟 OpenAI response 的 message 对象。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    """模拟 OpenAI response 的 choice 对象。"""

    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    """模拟 OpenAI 非流式 response。"""

    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeStream:
    """模拟 OpenAI 流式响应迭代器。"""

    def __init__(self, model: str) -> None:
        self._model = model

    def __iter__(self):
        # 产出两个 chunk，内容包含 model 名便于断言
        yield _FakeChunk(f"[{self._model}] part1")
        yield _FakeChunk(f"[{self._model}] part2")


class _FakeCompletions:
    """模拟 OpenAI chat.completions，记录所有 create 调用参数。"""

    def __init__(self) -> None:
        self.create_calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def create(self, **kwargs: Any) -> Any:
        # 线程安全记录调用参数，便于并发场景下断言
        with self._lock:
            self.create_calls.append(dict(kwargs))
        # 流式请求返回迭代器，非流式返回 response
        if kwargs.get("stream"):
            return _FakeStream(kwargs.get("model", "unknown"))
        return _FakeResponse(f"reply-from-{kwargs.get('model', 'unknown')}")


class _FakeChat:
    """模拟 OpenAI client.chat。"""

    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeOpenAIClient:
    """模拟 OpenAI 客户端，记录所有 API 调用。"""

    def __init__(self) -> None:
        self._completions = _FakeCompletions()
        self.chat = _FakeChat(self._completions)

    @property
    def create_calls(self) -> List[Dict[str, Any]]:
        return self._completions.create_calls


# ----------------------------------------------------------------------
# 测试 fixture：隔离单例与配置
# ----------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _reset_singletons_module():
    """模块级隔离：重置性能与 LLM 单例，强制关闭 small_client 与 Langfuse。"""
    from app.core.config import get_settings
    from app.core.performance import (
        reset_concurrency_optimizer,
        reset_hot_query_cache,
        reset_model_router,
    )

    settings = get_settings()
    original_small_key = settings.SMALL_LLM_API_KEY
    # 强制小模型不可用，保证测试注入的 fake main_client 不被 small_client 分支绕过
    settings.SMALL_LLM_API_KEY = ""
    settings.LANGFUSE_ENABLED = False

    reset_model_router()
    reset_hot_query_cache()
    reset_concurrency_optimizer()

    yield

    settings.SMALL_LLM_API_KEY = original_small_key
    reset_model_router()
    reset_hot_query_cache()
    reset_concurrency_optimizer()


@pytest.fixture(autouse=True)
def _reset_per_test():
    """每个用例前后重置统计与 LLMClient 单例，避免 fake 泄漏。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import (
        get_concurrency_optimizer,
        get_hot_query_cache,
        get_model_router,
    )

    saved_llm_client = llm_client_module._llm_client
    saved_small_llm_client = llm_client_module._small_llm_client

    get_model_router().reset_stats()
    get_hot_query_cache().reset_stats()
    get_concurrency_optimizer().reset_stats()
    yield
    get_model_router().reset_stats()
    get_hot_query_cache().reset_stats()
    get_concurrency_optimizer().reset_stats()

    # 恢复 LLMClient 单例，确保不污染后续测试模块
    llm_client_module._llm_client = saved_llm_client
    llm_client_module._small_llm_client = saved_small_llm_client


def _make_real_client_with_fake_openai(
    default_model: str = "default-model",
) -> "Any":
    """构造一个真实 LLMClient，注入 fake OpenAI 客户端。

    用于测试 chat/stream_chat 的 model 参数是否正确传递到 API 调用，
    同时验证 self.model 属性不被修改。
    """
    from app.agents.llm_client import LLMClient

    # 传入 api_key 避免 LLMClient 进入 mock 模式
    client = LLMClient(api_key="fake-key", model=default_model)
    # 注入 fake OpenAI 客户端，_ensure_client 检测到 _client 非空时直接返回
    fake_openai = _FakeOpenAIClient()
    client._client = fake_openai
    return client, fake_openai


# ======================================================================
# LLMClient.chat 的 model 参数测试
# ======================================================================


def test_chat_accepts_model_param_and_uses_it():
    """chat 传入 model 时应使用该模型发起请求，不修改 self.model。"""
    client, fake_openai = _make_real_client_with_fake_openai(default_model="default-model")

    reply = client.chat(
        [{"role": "user", "content": "hi"}], model="custom-model-x"
    )

    # 返回内容应包含传入的 model 名
    assert reply == "reply-from-custom-model-x"
    # API 调用应使用传入的 model
    assert len(fake_openai.create_calls) == 1
    assert fake_openai.create_calls[0]["model"] == "custom-model-x"
    # self.model 属性不应被修改
    assert client.model == "default-model"


def test_chat_model_none_uses_self_model():
    """chat model=None 时应使用 self.model（向后兼容）。"""
    client, fake_openai = _make_real_client_with_fake_openai(default_model="my-default")

    reply = client.chat([{"role": "user", "content": "hi"}])

    assert reply == "reply-from-my-default"
    assert len(fake_openai.create_calls) == 1
    assert fake_openai.create_calls[0]["model"] == "my-default"
    assert client.model == "my-default"


def test_chat_model_none_explicit_same_as_omitted():
    """显式传 model=None 与不传 model 行为一致。"""
    client, fake_openai = _make_real_client_with_fake_openai(default_model="base-model")

    client.chat([{"role": "user", "content": "hi"}], model=None)

    assert fake_openai.create_calls[0]["model"] == "base-model"


# ======================================================================
# LLMClient.stream_chat 的 model 参数测试
# ======================================================================


def test_stream_chat_accepts_model_param_and_uses_it():
    """stream_chat 传入 model 时应使用该模型，不修改 self.model。"""
    client, fake_openai = _make_real_client_with_fake_openai(default_model="default-model")

    events = list(
        client.stream_chat(
            [{"role": "user", "content": "hi"}], model="stream-model-y"
        )
    )

    # 应有一次 API 调用，使用传入的 model
    assert len(fake_openai.create_calls) == 1
    assert fake_openai.create_calls[0]["model"] == "stream-model-y"
    assert fake_openai.create_calls[0]["stream"] is True
    # self.model 属性不应被修改
    assert client.model == "default-model"
    # 流式事件应包含 token 与 done
    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    assert len(token_events) >= 1
    assert len(done_events) == 1


def test_stream_chat_model_none_uses_self_model():
    """stream_chat model=None 时应使用 self.model（向后兼容）。"""
    client, fake_openai = _make_real_client_with_fake_openai(default_model="stream-default")

    list(client.stream_chat([{"role": "user", "content": "hi"}]))

    assert len(fake_openai.create_calls) == 1
    assert fake_openai.create_calls[0]["model"] == "stream-default"
    assert client.model == "stream-default"


# ======================================================================
# chat_with_routing 不修改 main_client.model 属性
# ======================================================================


def test_chat_with_routing_does_not_mutate_main_client_model():
    """chat_with_routing 通过 model 参数传递，不修改 main_client.model 属性。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    client, fake_openai = _make_real_client_with_fake_openai(default_model="default-model")
    # 注入到 llm_client 模块单例，让 chat_with_routing 拿到该 client
    llm_client_module._llm_client = client

    router = get_model_router()
    router.reset_stats()

    # 用 model_override 强制走 main_client 路径（small_client 为 None）
    router.chat_with_routing(
        [{"role": "user", "content": "hi"}],
        query="hi",
        model_override="override-model-z",
    )

    # 关键断言：main_client.model 不被修改
    assert client.model == "default-model"
    # API 调用应使用 override 的 model
    assert len(fake_openai.create_calls) == 1
    assert fake_openai.create_calls[0]["model"] == "override-model-z"


def test_chat_with_routing_routed_model_does_not_mutate_main_client():
    """chat_with_routing 路由到小模型（small_client 不可用）时也不修改 main_client.model。"""
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    client, fake_openai = _make_real_client_with_fake_openai(default_model="default-model")
    llm_client_module._llm_client = client

    router = get_model_router()
    router.reset_stats()

    # 简单查询路由到小模型，small_client 为 None 时走 main_client + model 参数
    router.chat_with_routing(
        [{"role": "user", "content": "你好"}],
        query="你好",
    )

    # main_client.model 不被修改
    assert client.model == "default-model"
    # API 调用应使用路由到的小模型
    assert len(fake_openai.create_calls) == 1
    assert fake_openai.create_calls[0]["model"] == router._small_model


# ======================================================================
# 并发测试：多线程同时调用，每个使用不同 model，验证无错配
# ======================================================================


class _RecordingFakeClient:
    """并发测试用 fake：记录每次调用的 model，引入延迟放大竞态窗口。

    模拟旧设计中 main_client.model 被修改后其他线程读到错误值的场景：
    - chat 从 kwargs 读取 model（新契约）
    - 记录 (调用序号, model) 供主线程校验
    """

    def __init__(self, model: str = "default-model") -> None:
        self.model = model
        self.is_mock = True
        self._records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._barrier: threading.Barrier = None  # 由测试设置

    def chat(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        # 从 kwargs 读取 model（新契约：不修改 self.model）
        actual_model = kwargs.get("model", None) or self.model
        # 屏障同步：让所有线程同时进入，最大化竞态窗口
        if self._barrier is not None:
            self._barrier.wait()
            # 短暂 sleep 放大旧设计下 self.model 被其他线程覆盖的窗口
            time.sleep(0.01)
        with self._lock:
            self._records.append({"model": actual_model, "messages": messages})
        return f"reply-{actual_model}"


def test_concurrent_chat_with_different_models_no_mismatch():
    """多线程并发调用 chat_with_routing，每个用不同 model_override，验证无错配。

    旧设计（修改 main_client.model 属性）下，线程间会互相覆盖 model，
    导致部分调用读到错误的 model。新设计通过 model 参数传递，
    每次调用独立，无竞态。
    """
    from app.agents import llm_client as llm_client_module
    from app.core.performance import get_model_router

    fake = _RecordingFakeClient(model="default-model")
    llm_client_module._llm_client = fake

    router = get_model_router()
    router.reset_stats()

    num_threads = 8
    # 每个线程使用不同的 model_override
    models = [f"concurrent-model-{i}" for i in range(num_threads)]
    # 屏障让所有线程同时开始，最大化竞态暴露概率
    fake._barrier = threading.Barrier(num_threads)

    results: Dict[int, str] = {}
    errors: List[Exception] = []

    def worker(idx: int, model_name: str) -> None:
        try:
            reply = router.chat_with_routing(
                [{"role": "user", "content": f"query-{idx}"}],
                query=f"query-{idx}",
                model_override=model_name,
            )
            results[idx] = reply
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i, models[i]))
        for i in range(num_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 不应有异常
    assert errors == [], f"并发调用出现异常：{errors}"

    # 每个线程的回复应包含自己传入的 model 名
    for i in range(num_threads):
        assert results[i] == f"reply-{models[i]}", (
            f"线程 {i} 期望 reply-{models[i]}，实际 {results[i]}（模型错配）"
        )

    # 记录数应等于线程数
    assert len(fake._records) == num_threads

    # 每条记录的 model 应与对应线程传入的一致（无错配）
    # 按记录顺序收集 model，与传入的 models 集合比较
    recorded_models = {r["model"] for r in fake._records}
    expected_models = set(models)
    assert recorded_models == expected_models, (
        f"记录的 model 集合与预期不符："
        f"期望 {expected_models}，实际 {recorded_models}"
    )

    # main_client.model 应始终为默认值（从未被修改）
    assert fake.model == "default-model"


def test_concurrent_chat_direct_no_mismatch():
    """多线程直接调用 LLMClient.chat 传不同 model，验证无错配。

    直接测试 LLMClient.chat 层，确认 model 参数在并发下正确隔离。
    """
    client, fake_openai = _make_real_client_with_fake_openai(default_model="default-model")

    num_threads = 8
    models = [f"direct-model-{i}" for i in range(num_threads)]
    barrier = threading.Barrier(num_threads)

    def worker(model_name: str) -> None:
        # 屏障同步后立即调用，最大化并发
        barrier.wait()
        client.chat([{"role": "user", "content": "hi"}], model=model_name)

    threads = [
        threading.Thread(target=worker, args=(models[i],))
        for i in range(num_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 所有 API 调用的 model 集合应与传入的一致
    assert len(fake_openai.create_calls) == num_threads
    recorded_models = {call["model"] for call in fake_openai.create_calls}
    assert recorded_models == set(models), (
        f"并发下 model 错配：期望 {set(models)}，实际 {recorded_models}"
    )
    # self.model 不被修改
    assert client.model == "default-model"
