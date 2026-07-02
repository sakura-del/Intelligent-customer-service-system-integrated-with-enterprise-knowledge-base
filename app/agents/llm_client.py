"""LLM 客户端封装。

对 OpenAI 兼容接口（DeepSeek-V3 / GPT-4o-mini 等）做统一封装，
提供 chat 与 stream_chat 两套接口供 RAG Agent 调用。

当 LLM_API_KEY 为空或调用失败时降级到 _MockLLM，
基于检索片段拼接简单回复，保证无网络/无 Key 也能跑通端到端流程。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Generator, List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.agents.llm_client")


class _MockLLM:
    """Mock LLM：无 API Key 或调用失败时的兜底实现。

    不调用任何外部服务，从 messages 中提取最后一条 user 内容，
    并优先拼接传入的检索片段上下文，给出一个可读的回复，
    让 RAG 流程在离线环境下也能产出可验证结果。
    """

    def __init__(self, reason: str = "") -> None:
        # reason 仅用于日志，便于排查为何走了 mock
        self.reason = reason

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """根据 messages 与检索片段拼一个简单回复。

        context_chunks 由 RAGAgent 在调用前注入，避免 mock 时还要重新解析 prompt。
        name/metadata 仅用于 Langfuse 追踪，mock 模式下直接忽略，保证签名兼容。
        """
        # 取最后一条 user 消息作为问题，找不到时用兜底文案
        user_question = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_question = str(message.get("content", ""))
                break

        if context_chunks:
            # 取最相关片段（已按相似度排序），截断避免回复过长
            top_chunk = context_chunks[0][:300]
            reply = f"根据知识库：{top_chunk}"
        else:
            reply = f"抱歉，知识库中未找到与「{user_question[:30]}」相关的内容。"

        logger.warning(
            "使用 _MockLLM 生成回复（原因=%s），无真实 LLM 调用", self.reason or "未指定"
        )
        return reply

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Generator[Dict[str, Any], None, None]:
        """Mock 流式生成：按字符切片 yield token，模拟流式体验。

        每 2 个字符一组，间隔 10ms，让前端能看到打字效果，
        便于在没有真实 LLM 时验证 SSE 链路。
        name/metadata 仅用于 Langfuse 追踪，mock 模式下直接忽略，保证签名兼容。
        """
        # 复用 chat 拿到完整回复，再切片 yield，避免逻辑重复
        full_reply = self.chat(
            messages=messages,
            temperature=temperature,
            context_chunks=context_chunks,
            **kwargs,
        )
        yield from _slice_text_to_stream(full_reply)


def _inject_langfuse_tracing(
    kwargs: Dict[str, Any],
    name: Optional[str],
    metadata: Optional[Dict[str, Any]],
) -> None:
    """当 Langfuse 启用且 name/metadata 非空时，向 kwargs 注入 extra_body。

    langfuse.openai 包装器会自动识别请求体中的 name/metadata 字段，用于在
    Langfuse 面板标记 prompt name 与版本等元信息；未启用或无 name/metadata
    时直接返回，保持原生 OpenAI 调用行为不变，确保向后兼容。
    """
    # name/metadata 均为空时无需注入，避免无意义字段污染请求体
    if not name and not metadata:
        return
    try:
        from app.core.langfuse_client import is_langfuse_enabled

        if not is_langfuse_enabled():
            return
    except Exception as exc:
        # Langfuse 状态检查异常：跳过注入，不影响主链路
        logger.warning("Langfuse 状态检查失败，跳过 extra_body 注入：%s", exc)
        return
    # 合并已有 extra_body，避免覆盖调用方传入的其他自定义字段
    extra_body: Dict[str, Any] = dict(kwargs.get("extra_body") or {})
    if metadata:
        extra_body["metadata"] = metadata
    if name:
        extra_body["name"] = name
    kwargs["extra_body"] = extra_body


class LLMClient:
    """OpenAI 兼容 LLM 客户端。

    按 Settings.LLM_API_KEY 是否存在决定走真实接口还是 Mock：
    - 有 Key：用 openai SDK 调用 chat.completions
    - 无 Key 或调用异常：降级到 _MockLLM，保证流程不中断

    客户端实例按需创建，避免在未使用时即建立网络连接。
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """构造 LLM 客户端。

        默认从 Settings 读取主 LLM 配置；传入 api_key/base_url/model 时
        使用自定义配置（用于小模型客户端等场景），保持与默认客户端行为一致。
        """
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.LLM_API_KEY
        self.base_url = base_url if base_url is not None else settings.LLM_BASE_URL
        self.model = model if model is not None else settings.LLM_MODEL
        self._client = None
        self._mock: Optional[_MockLLM] = None

        # 提前判断：无 Key 直接走 mock，避免每次调用都重试
        if not self.api_key:
            self._mock = _MockLLM(reason="LLM_API_KEY 为空")
            logger.warning(
                "LLM_API_KEY 未配置，LLMClient 将使用 _MockLLM 兜底；"
                "如需真实生成请在 .env 配置 LLM_API_KEY"
            )

    @property
    def is_mock(self) -> bool:
        """是否处于 mock 模式，便于上层调整期望与断言。"""
        return self._mock is not None

    def _ensure_client(self) -> None:
        """延迟创建 OpenAI 客户端，未配置 Key 时跳过。

        Langfuse 启用时改用 langfuse.openai.OpenAI 包装器，与原生 SDK 接口
        完全兼容，无需改动其他调用代码；包装器加载失败时降级原生 OpenAI，
        保证主链路不受 Langfuse 影响。此处分发逻辑同时覆盖主模型与小模型客户端。
        """
        if self._mock is not None or self._client is not None:
            return
        try:
            # 延迟导入：未安装 openai 或无 Key 时仍可加载本模块
            from openai import OpenAI

            client_cls = OpenAI
            # Langfuse 启用时替换为 langfuse.openai 包装器，自动上报 LLM trace
            try:
                from app.core.langfuse_client import is_langfuse_enabled

                if is_langfuse_enabled():
                    from langfuse.openai import OpenAI as LangfuseOpenAI

                    client_cls = LangfuseOpenAI
                    logger.info("Langfuse 已启用，LLMClient 使用 langfuse.openai 包装器")
            except Exception as exc:
                # langfuse 包缺失或导入失败：降级原生 OpenAI，不阻断初始化
                logger.warning("Langfuse 包装器加载失败，使用原生 OpenAI：%s", exc)

            self._client = client_cls(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        except Exception as exc:
            # SDK 缺失或初始化失败：降级 mock，保证链路可用
            logger.warning("OpenAI 客户端初始化失败，降级到 _MockLLM：%s", exc)
            self._mock = _MockLLM(reason=f"客户端初始化失败：{exc}")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """调用 LLM 生成回复。

        messages 遵循 OpenAI Chat 格式：[{role, content}, ...]。
        context_chunks 用于 mock 模式拼接，真实模式下忽略以节省 token。
        name/metadata 用于 Langfuse 追踪（标记 prompt name 与版本等），
        Langfuse 未启用或为空时忽略，保证现有调用行为不变。
        """
        # 1. mock 模式直接返回拼接结果
        if self._mock is not None:
            return self._mock.chat(
                messages=messages,
                temperature=temperature,
                context_chunks=context_chunks,
                **kwargs,
            )

        # 2. 真实模式：确保 client 就绪后调用 chat.completions
        self._ensure_client()
        if self._mock is not None:
            # _ensure_client 内部可能因初始化失败切到 mock
            return self._mock.chat(
                messages=messages,
                temperature=temperature,
                context_chunks=context_chunks,
                **kwargs,
            )

        # Langfuse 启用且 name/metadata 非空时注入 extra_body，否则 no-op
        _inject_langfuse_tracing(kwargs, name, metadata)

        try:
            assert self._client is not None  # 仅供类型检查器收敛
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            )
            # 取首个候选回复；空回复时降级到 mock 保证有输出
            if response.choices and response.choices[0].message.content:
                return response.choices[0].message.content
            logger.warning("LLM 返回空回复，降级到 _MockLLM")
            return _MockLLM(reason="LLM 返回空回复").chat(
                messages=messages,
                temperature=temperature,
                context_chunks=context_chunks,
                **kwargs,
            )
        except Exception as exc:
            # 调用失败（限流/网络/鉴权）时降级，避免拖垮整个对话
            logger.warning("LLM 调用失败，降级到 _MockLLM：%s", exc)
            return _MockLLM(reason=f"调用失败：{exc}").chat(
                messages=messages,
                temperature=temperature,
                context_chunks=context_chunks,
                **kwargs,
            )

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Generator[Dict[str, Any], None, None]:
        """流式调用 LLM，逐 token yield。

        协议：
        - {"type": "token", "content": "..."} 多次：每个生成片段
        - {"type": "error", "message": "..."}：异常时
        - {"type": "done", "content": "完整文本"}：生成结束

        mock 模式按字符切片模拟流式；真实模式调用 OpenAI SDK 的 stream=True。
        出错时降级 yield error 事件，保证调用方拿到统一协议。
        name/metadata 用于 Langfuse 追踪，未启用或为空时忽略，保证向后兼容。
        """
        # 1. mock 模式：直接走 _MockLLM.stream_chat 切片 yield
        if self._mock is not None:
            yield from self._mock.stream_chat(
                messages=messages,
                temperature=temperature,
                context_chunks=context_chunks,
                **kwargs,
            )
            return

        # 2. 真实模式：确保 client 就绪，初始化失败则降级 mock
        self._ensure_client()
        if self._mock is not None:
            yield from self._mock.stream_chat(
                messages=messages,
                temperature=temperature,
                context_chunks=context_chunks,
                **kwargs,
            )
            return

        # Langfuse 启用且 name/metadata 非空时注入 extra_body，否则 no-op
        _inject_langfuse_tracing(kwargs, name, metadata)

        yield from self._stream_from_openai(
            messages=messages,
            temperature=temperature,
            context_chunks=context_chunks,
            **kwargs,
        )

    def _stream_from_openai(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.3,
        context_chunks: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Generator[Dict[str, Any], None, None]:
        """调用 OpenAI SDK 的流式接口并转换为统一协议。

        SDK 异常时 yield error 事件，避免抛出中断整个流；
        空响应时降级到 _MockLLM 保证有内容输出。
        """
        try:
            assert self._client is not None
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
                **kwargs,
            )
        except Exception as exc:
            # 流式创建失败：yield error 后让上层优雅关闭流
            logger.warning("LLM 流式调用初始化失败：%s", exc)
            yield {"type": "error", "message": f"流式调用失败：{exc}"}
            return

        # 逐 chunk 提取 delta.content，累积成完整文本
        full_text_parts: List[str] = []
        try:
            for chunk in response:
                content = _extract_delta_content(chunk)
                if not content:
                    continue
                full_text_parts.append(content)
                yield {"type": "token", "content": content}
        except Exception as exc:
            # 流中途异常：先 yield error 再 yield done，保证调用方能收尾
            logger.warning("LLM 流式生成中断：%s", exc)
            yield {"type": "error", "message": f"流式生成中断：{exc}"}

        full_text = "".join(full_text_parts)
        if not full_text:
            # 空响应降级 mock：保证前端能拿到可读回复
            logger.warning("LLM 流式返回空回复，降级到 _MockLLM")
            mock = _MockLLM(reason="LLM 流式返回空回复")
            full_text = mock.chat(
                messages=messages,
                temperature=temperature,
                context_chunks=context_chunks,
                **kwargs,
            )
            yield {"type": "token", "content": full_text}

        yield {"type": "done", "content": full_text}


def _extract_delta_content(chunk: Any) -> str:
    """从 OpenAI stream chunk 中安全提取 delta.content。

    不同 SDK 版本字段访问路径偶有差异，统一在此容错，
    避免 chunk 结构异常时整个流中断。
    """
    try:
        choices = getattr(chunk, "choices", None)
        if not choices:
            return ""
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return ""
        content = getattr(delta, "content", None)
        return content or ""
    except Exception:
        # 任何结构异常都视为空内容，跳过该 chunk
        return ""


def _slice_text_to_stream(
    text: str, chunk_size: int = 2, sleep_seconds: float = 0.01
) -> Generator[Dict[str, Any], None, None]:
    """把完整文本切片成 token 流，模拟流式生成。

    每 chunk_size 个字符一组，组间 sleep sleep_seconds 秒，
    让前端能感受到打字效果；空文本直接 yield done 避免无内容输出。
    """
    if not text:
        yield {"type": "done", "content": ""}
        return

    for start in range(0, len(text), chunk_size):
        piece = text[start : start + chunk_size]
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        yield {"type": "token", "content": piece}
    yield {"type": "done", "content": text}


# 模块级单例：LLM 客户端无状态，进程内复用
_llm_client: Optional[LLMClient] = None
# 小模型客户端单例：未配置 SMALL_LLM_API_KEY 时为 None，调用方降级到主 LLM
_small_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取 LLMClient 单例。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def reset_llm_client() -> None:
    """重置单例，便于测试切换配置。"""
    global _llm_client
    _llm_client = None


def get_small_llm_client() -> Optional[LLMClient]:
    """获取小模型 LLMClient 单例。

    未配置 SMALL_LLM_API_KEY 时返回 None，调用方应降级到主 LLMClient，
    保证未配置小模型时主链路仍可用。

    Langfuse 包装由 LLMClient._ensure_client 统一分发：启用时小模型客户端
    同样使用 langfuse.openai.OpenAI，无需在此重复检查 is_langfuse_enabled()。
    """
    global _small_llm_client
    if _small_llm_client is not None:
        return _small_llm_client
    settings = get_settings()
    if not settings.SMALL_LLM_API_KEY:
        return None
    _small_llm_client = LLMClient(
        api_key=settings.SMALL_LLM_API_KEY,
        base_url=settings.SMALL_LLM_BASE_URL,
        model=settings.SMALL_LLM_MODEL,
    )
    return _small_llm_client


def reset_small_llm_client() -> None:
    """重置小模型单例，便于测试切换配置。"""
    global _small_llm_client
    _small_llm_client = None
