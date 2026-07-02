"""性能优化核心模块。

提供 Task 20 的三大能力：
- ModelRouter：根据查询复杂度路由到小/大模型，降低成本
- HotQueryCache：高频 query 最终回复缓存，LRU + TTL 淘汰
- ConcurrencyOptimizer：并发限流与连接池监控，避免 OOM

设计要点：
- 三个组件均为线程安全（RLock 保护共享状态）
- 降级优先：缓存/限流异常时透明降级，不阻断主链路
- 延迟初始化：LLMClient 等重资源按需获取，避免启动开销
- 阈值可通过环境变量配置，无需修改 config.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Deque, Dict, List, Optional

from app.core.logging import get_logger
from app.schemas.performance import (
    CacheEntry,
    CacheStats,
    ConcurrencyStats,
    ModelRouteStat,
    ModelRoutingStats,
    PerformanceMetrics,
)

logger = get_logger("app.core.performance")

# ===== 可配置阈值（环境变量覆盖，避免修改 config.py）=====
# 小模型名：处理简单查询，省成本
SMALL_MODEL = os.environ.get("SMALL_MODEL", "gpt-4o-mini")
# 大模型名：处理复杂查询，保质量
LARGE_MODEL = os.environ.get("LARGE_MODEL", "gpt-4o")
# 小模型路由阈值：复杂度评分低于该值走小模型
SMALL_MODEL_THRESHOLD = float(os.environ.get("SMALL_MODEL_THRESHOLD", "0.5"))
# 检索最大并发：避免向量库与 BM25 并发过高导致 OOM
MAX_CONCURRENT_RETRIEVAL = int(os.environ.get("MAX_CONCURRENT_RETRIEVAL", "20"))
# LLM 调用最大并发：避免 API 限流与内存峰值
MAX_CONCURRENT_LLM_CALLS = int(os.environ.get("MAX_CONCURRENT_LLM_CALLS", "10"))
# 热点缓存容量与 TTL
HOT_CACHE_MAX_SIZE = int(os.environ.get("HOT_CACHE_MAX_SIZE", "1000"))
HOT_CACHE_TTL = int(os.environ.get("HOT_CACHE_TTL", "300"))
# 意图识别结果缓存：TTL 较长（意图稳定），容量较大（覆盖更多 query 变体）
INTENT_CACHE_MAX_SIZE = int(os.environ.get("INTENT_CACHE_MAX_SIZE", "5000"))
INTENT_CACHE_TTL = int(os.environ.get("INTENT_CACHE_TTL", "1800"))
# 响应时间采样上限：超过则 FIFO 丢弃，控制内存
RESPONSE_TIME_SAMPLE_LIMIT = 100


def cache_key(
    query: str, session_context: Optional[Dict[str, Any]] = None
) -> str:
    """生成热点缓存键：归一化 query + 上下文关键信息哈希。

    归一化：strip + lower，消除大小写与首尾空白差异。
    上下文哈希：仅取影响回复的关键字段（session_id/intent/turn_count/user_id），
    用 md5 摘要避免长 key 占用内存，同时避免无关元数据导致缓存失效。
    """
    normalized = (query or "").strip().lower()
    context = session_context or {}
    # 仅取影响回复的关键字段，避免无关元数据导致缓存失效
    key_fields = {
        "session_id": context.get("session_id", ""),
        "intent": context.get("intent", ""),
        "turn_count": context.get("turn_count", 0),
        "user_id": context.get("user_id", ""),
    }
    context_str = json.dumps(key_fields, sort_keys=True, ensure_ascii=False)
    context_hash = hashlib.md5(context_str.encode("utf-8")).hexdigest()
    return f"{normalized}|{context_hash}"


# ----------------------------------------------------------------------
# ModelRouter：大小模型分层路由
# ----------------------------------------------------------------------
class ModelRouter:
    """大小模型路由器。

    根据查询复杂度（长度/多意图/跨域/情绪/轮次）计算评分，
    低于阈值路由到小模型（省成本），否则路由到大模型（保质量）。

    双 Provider 路由：
    - 小模型走独立 SmallLLMClient（豆包/千问，OpenAI 兼容接口）
    - 大模型走主 LLMClient（DeepSeek 等）
    - 未配置 SMALL_LLM_API_KEY 时 small_client 为 None，自动降级到主 LLMClient，
      保持兼容性

    ModelRouter 不直接持有 small_client 引用，每次调用时延迟获取，
    避免 ModelRouter 单例初始化早于 LLMClient 配置加载。
    """

    def __init__(
        self,
        small_model: str = SMALL_MODEL,
        large_model: str = LARGE_MODEL,
        threshold: float = SMALL_MODEL_THRESHOLD,
    ) -> None:
        self._small_model = small_model
        self._large_model = large_model
        self._threshold = threshold
        # 路由统计：model -> {calls, total_complexity}
        self._stats: Dict[str, Dict[str, float]] = {}
        self._lock = threading.RLock()

    def route(
        self,
        query: str,
        *,
        emotion_score: int = 0,
        turn_count: int = 1,
        cross_domain: bool = False,
        multi_intent: bool = False,
    ) -> str:
        """路由决策：返回应使用的模型名。

        复杂度评分维度（各 0-0.2，总分 0-1）：
        - 长度：query 越长越可能复杂
        - 多意图：含多个子意图 → 复杂
        - 跨域：跨业务域 → 复杂
        - 情绪：情绪分高 → 需大模型谨慎处理
        - 轮次：多轮未解决 → 升级大模型
        """
        complexity = self._compute_complexity(
            query=query,
            emotion_score=emotion_score,
            turn_count=turn_count,
            cross_domain=cross_domain,
            multi_intent=multi_intent,
        )
        model = self._select_model(complexity)
        self._record_routing(model, complexity)
        logger.debug(
            "模型路由：query=%r complexity=%.2f model=%s",
            query[:30],
            complexity,
            model,
        )
        return model

    @staticmethod
    def _compute_complexity(
        query: str,
        emotion_score: int,
        turn_count: int,
        cross_domain: bool,
        multi_intent: bool,
    ) -> float:
        """计算查询复杂度评分（0-1）。

        各维度独立打分后求和，避免单维度过度主导。
        """
        # 长度分：100 字以上视为最长，线性归一
        length_component = min(len(query) / 100.0, 1.0) * 0.2
        # 多意图：是 → 0.2
        intent_component = 0.2 if multi_intent else 0.0
        # 跨域：是 → 0.2
        domain_component = 0.2 if cross_domain else 0.0
        # 情绪分：5 分制归一
        emotion_norm = min(max(emotion_score, 0) / 5.0, 1.0)
        emotion_component = emotion_norm * 0.2
        # 轮次分：第 1 轮为 0，5 轮以上满分
        turn_norm = min(max(turn_count - 1, 0) / 5.0, 1.0)
        turn_component = turn_norm * 0.2
        return (
            length_component
            + intent_component
            + domain_component
            + emotion_component
            + turn_component
        )

    def _select_model(self, complexity: float) -> str:
        """根据复杂度与阈值选择模型。"""
        if complexity < self._threshold:
            return self._small_model
        return self._large_model

    def _record_routing(self, model: str, complexity: float) -> None:
        """记录路由统计（线程安全）。"""
        with self._lock:
            stat = self._stats.setdefault(
                model, {"calls": 0, "total_complexity": 0.0}
            )
            stat["calls"] += 1
            stat["total_complexity"] += complexity

    @staticmethod
    def _get_small_client() -> Optional[Any]:
        """延迟获取小模型 LLMClient 单例。

        未配置 SMALL_LLM_API_KEY 时返回 None，调用方降级到主 LLMClient。
        每次调用都从 llm_client 模块获取，保证单例创建后能被复用。
        """
        try:
            from app.agents.llm_client import get_small_llm_client

            return get_small_llm_client()
        except Exception as exc:
            logger.warning("获取小模型客户端失败，降级主 LLM：%s", exc)
            return None

    def chat_with_routing(
        self,
        messages: List[Dict[str, Any]],
        *,
        query: str,
        model_override: Optional[str] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """根据 query 路由到合适模型并调用 LLMClient 生成回复。

        双 Provider 路由策略：
        - model_override 非空：直接用主 client 临时切换 model（兼容旧逻辑）
        - 路由到小模型且 small_client 可用：用 small_client（独立 Provider）
        - 路由到小模型但 small_client 不可用：降级主 client 临时切换 model
        - 路由到大模型：用主 client

        异常时降级到主 client 重试，保证链路可用。

        name/metadata 透传给底层 LLMClient.chat，便于 Langfuse 平台
        按 prompt name 聚合分析调用情况。
        """
        # 延迟导入避免循环依赖与启动开销
        from app.agents.llm_client import get_llm_client

        # model_override 优先，否则按 query 路由
        target_model = model_override or self.route(query)
        main_client = get_llm_client()

        # 透传 name/metadata 给底层 LLMClient，便于 Langfuse 聚合分析
        # 合并到 kwargs 后随 small_client.chat 与 _chat_with_main_fallback 统一透传
        if name is not None:
            kwargs["name"] = name
        if metadata is not None:
            kwargs["metadata"] = metadata

        # 双 Provider 路由：路由到小模型且 small_client 可用时走独立客户端
        # small_client 是独立的 LLMClient 实例，有自己的 base_url/model/api_key
        if not model_override and target_model == self._small_model:
            small_client = self._get_small_client()
            if small_client is not None:
                try:
                    return small_client.chat(messages, **kwargs)
                except Exception as exc:
                    # 小模型调用失败：降级主 client 重试，保证链路可用
                    logger.warning(
                        "小模型 %s 调用失败，降级主 LLM 重试：%s",
                        self._small_model,
                        exc,
                    )
                    # 走主 client 路径，注意不再走 small_client 分支
                    return self._chat_with_main_fallback(
                        main_client, messages, target_model, **kwargs
                    )

        # 主 client 路径：临时切换 model 实现 model_override
        return self._chat_with_main_fallback(
            main_client, messages, target_model, **kwargs
        )

    @staticmethod
    def _chat_with_main_fallback(
        main_client: Any,
        messages: List[Dict[str, Any]],
        target_model: str,
        **kwargs: Any,
    ) -> str:
        """主 client 调用：临时切换 model，异常时降级到原 model 重试。

        临时切换 model 在高并发下存在竞态（best-effort），
        mock 模式下不影响结果；生产场景偶发模型错配会触发降级重试。
        """
        original_model = main_client.model
        try:
            main_client.model = target_model
            return main_client.chat(messages, **kwargs)
        except Exception as exc:
            # 调用失败：恢复默认模型并重试一次，保证链路可用
            logger.warning(
                "模型 %s 调用失败，降级默认模型重试：%s", target_model, exc
            )
            main_client.model = original_model
            return main_client.chat(messages, **kwargs)
        finally:
            # 确保 model 总能恢复，避免污染后续调用
            main_client.model = original_model

    def get_stats(self) -> Dict[str, Any]:
        """返回路由统计（线程安全）。"""
        with self._lock:
            small_calls = int(
                self._stats.get(self._small_model, {}).get("calls", 0)
            )
            large_calls = int(
                self._stats.get(self._large_model, {}).get("calls", 0)
            )
            total = small_calls + large_calls
            per_model: List[Dict[str, Any]] = []
            for model_name, stat in self._stats.items():
                calls = int(stat["calls"])
                avg = stat["total_complexity"] / calls if calls > 0 else 0.0
                per_model.append(
                    {
                        "model": model_name,
                        "calls": calls,
                        "avg_complexity": round(avg, 4),
                    }
                )
            return {
                "small_model": self._small_model,
                "large_model": self._large_model,
                "small_model_calls": small_calls,
                "large_model_calls": large_calls,
                "total_calls": total,
                "small_model_ratio": (
                    round(small_calls / total, 4) if total > 0 else 0.0
                ),
                "per_model": per_model,
            }

    def reset_stats(self) -> None:
        """重置统计，便于测试隔离。"""
        with self._lock:
            self._stats.clear()


# ----------------------------------------------------------------------
# HotQueryCache：热点查询缓存（LRU + TTL）
# ----------------------------------------------------------------------
class HotQueryCache:
    """热点查询缓存：LRU + TTL 淘汰。

    缓存高频 query 的最终回复，命中时跳过检索+生成，降低响应时间。
    key = cache_key(query, session_context)，value = CacheEntry。
    初始化失败时降级为不缓存（透明透传），不阻断主链路。
    """

    def __init__(
        self,
        max_size: int = HOT_CACHE_MAX_SIZE,
        ttl_seconds: int = HOT_CACHE_TTL,
    ) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._enabled = True
        try:
            self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        except Exception as exc:
            # 初始化失败：降级为不缓存，保证主链路可用
            logger.warning("热点缓存初始化失败，降级为不缓存：%s", exc)
            self._enabled = False
            self._store = OrderedDict()
        # 统计
        self._hits = 0
        self._misses = 0
        self._evicted = 0
        self._lock = threading.RLock()

    def get(
        self, query: str, session_context: Optional[Dict[str, Any]] = None
    ) -> Optional[CacheEntry]:
        """获取缓存条目，命中且未过期返回 CacheEntry，否则 None。"""
        if not self._enabled:
            return None
        key = cache_key(query, session_context)
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            # TTL 过期：删除并记 miss
            if now >= entry.expires_at:
                self._store.pop(key, None)
                self._misses += 1
                return None
            # 命中：移到末尾（LRU 最近使用）
            self._store.move_to_end(key)
            self._hits += 1
            return entry

    def set(
        self,
        query: str,
        answer: str,
        sources: Optional[List[str]] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """写入缓存条目，超限时按 LRU 淘汰最旧。"""
        if not self._enabled:
            return
        key = cache_key(query, session_context)
        now = time.monotonic()
        entry = CacheEntry(
            answer=answer,
            sources=list(sources) if sources else [],
            expires_at=now + self._ttl,
            created_at=now,
        )
        with self._lock:
            # 已存在则更新并移到末尾
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = entry
            # LRU 淘汰：超限时弹出头部（最旧）
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)
                self._evicted += 1

    def invalidate(self) -> int:
        """清空全部缓存，返回被清除的条目数。"""
        with self._lock:
            cleared = len(self._store)
            self._store.clear()
            return cleared

    def get_stats(self) -> Dict[str, Any]:
        """返回缓存统计（线程安全）。"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
                "size": len(self._store),
                "max_size": self._max_size,
                "evicted": self._evicted,
                "ttl_seconds": self._ttl,
            }

    def reset_stats(self) -> None:
        """重置统计与缓存，便于测试隔离。"""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._evicted = 0


# ----------------------------------------------------------------------
# IntentCache：意图识别结果缓存（LRU + TTL）
# ----------------------------------------------------------------------
class IntentCache:
    """意图识别结果缓存：LRU + TTL 淘汰。

    缓存 query → IntentResult，命中时跳过 LLM 意图识别调用，
    把首 Token 时间从 2.7s 降到 ~800ms（仅检索+生成）。
    意图稳定（同一 query 的意图通常不变），TTL 设较长（30 分钟）。
    key = normalized query，value = IntentResult + expires_at。
    """

    def __init__(
        self,
        max_size: int = INTENT_CACHE_MAX_SIZE,
        ttl_seconds: int = INTENT_CACHE_TTL,
    ) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._enabled = True
        try:
            self._store: "OrderedDict[str, tuple[Any, float]]" = OrderedDict()
        except Exception as exc:
            logger.warning("意图缓存初始化失败，降级为不缓存：%s", exc)
            self._enabled = False
            self._store = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evicted = 0
        self._lock = threading.RLock()

    @staticmethod
    def _normalize(query: str) -> str:
        """归一化 query 作为缓存键：strip + lower，消除大小写与首尾空白差异。"""
        return (query or "").strip().lower()

    def get(self, query: str) -> Optional[Any]:
        """获取意图缓存，命中且未过期返回 IntentResult，否则 None。"""
        if not self._enabled:
            return None
        key = self._normalize(query)
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            intent_result, expires_at = entry
            if now >= expires_at:
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return intent_result

    def set(self, query: str, intent_result: Any) -> None:
        """写入意图缓存，超限时按 LRU 淘汰最旧。"""
        if not self._enabled:
            return
        key = self._normalize(query)
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (intent_result, now + self._ttl)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)
                self._evicted += 1

    def invalidate(self) -> int:
        """清空全部缓存，返回被清除的条目数。"""
        with self._lock:
            cleared = len(self._store)
            self._store.clear()
            return cleared

    def get_stats(self) -> Dict[str, Any]:
        """返回缓存统计（线程安全）。"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
                "size": len(self._store),
                "max_size": self._max_size,
                "evicted": self._evicted,
                "ttl_seconds": self._ttl,
            }

    def reset_stats(self) -> None:
        """重置统计与缓存，便于测试隔离。"""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._evicted = 0


# ----------------------------------------------------------------------
# ConcurrencyOptimizer：并发限流与连接池监控
# ----------------------------------------------------------------------
class ConcurrencyOptimizer:
    """并发优化器：限流 + 线程池 + 连接池监控。

    提供 asyncio.Semaphore 包装器限制并发检索数，避免 OOM；
    提供 run_in_threadpool_with_limit 把阻塞任务线程池化 + 限流；
    超限时降级为同步执行，不抛错。
    """

    def __init__(
        self,
        max_concurrent_retrieval: int = MAX_CONCURRENT_RETRIEVAL,
        max_concurrent_llm: int = MAX_CONCURRENT_LLM_CALLS,
    ) -> None:
        self._max_retrieval = max_concurrent_retrieval
        self._max_llm = max_concurrent_llm
        # 同步限流信号量（线程安全，不绑定 event loop）
        self._retrieval_sem = threading.Semaphore(max_concurrent_retrieval)
        self._llm_sem = threading.Semaphore(max_concurrent_llm)
        # 异步信号量按 loop 缓存，避免跨 loop 复用报错
        self._async_retrieval_cache: Dict[Any, asyncio.Semaphore] = {}
        self._async_llm_cache: Dict[Any, asyncio.Semaphore] = {}
        # 线程池：复用避免反复创建销毁
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_retrieval + max_concurrent_llm,
            thread_name_prefix="perf-worker",
        )
        # 统计
        self._active_retrieval = 0
        self._active_llm = 0
        self._peak_retrieval = 0
        self._peak_llm = 0
        self._rejected_retrieval = 0
        self._rejected_llm = 0
        # 响应时间采样（最近 N 次，FIFO 自动丢弃）
        self._response_times: Deque[float] = deque(
            maxlen=RESPONSE_TIME_SAMPLE_LIMIT
        )
        self._lock = threading.RLock()

    # ---------- 异步限流 ----------
    def _get_async_retrieval_sem(self) -> asyncio.Semaphore:
        """获取当前 loop 的检索信号量（延迟创建，按 loop 缓存）。

        不同 event loop 不能复用同一个 asyncio.Semaphore，
        因此按 loop 缓存，避免跨 loop 复用报错。
        """
        loop = asyncio.get_running_loop()
        if loop not in self._async_retrieval_cache:
            self._async_retrieval_cache[loop] = asyncio.Semaphore(
                self._max_retrieval
            )
        return self._async_retrieval_cache[loop]

    @property
    def retrieval_semaphore(self) -> asyncio.Semaphore:
        """暴露 asyncio.Semaphore 包装器，供外部 async with 使用。"""
        return self._get_async_retrieval_sem()

    async def run_async_with_retrieval_limit(
        self, func: Callable, *args: Any, **kwargs: Any
    ) -> Any:
        """异步限流执行：信号量耗尽时降级为直接执行。

        func 可为协程函数或同步函数（同步函数将放到线程池执行）。
        采用 threading.Semaphore 非阻塞获取：不依赖 event loop、不阻塞协程，
        且与同步路径共享同一信号量，保证 async/sync 总并发不超上限；
        同时避免 asyncio.Semaphore 在不同 Python 版本下私有属性差异。
        """
        # 非阻塞获取：失败则降级直接执行，避免请求堆积
        if not self._retrieval_sem.acquire(blocking=False):
            with self._lock:
                self._rejected_retrieval += 1
            return await self._execute_async(func, *args, **kwargs)
        try:
            with self._lock:
                self._active_retrieval += 1
                if self._active_retrieval > self._peak_retrieval:
                    self._peak_retrieval = self._active_retrieval
            return await self._execute_async(func, *args, **kwargs)
        finally:
            with self._lock:
                self._active_retrieval -= 1
            self._retrieval_sem.release()

    async def _execute_async(
        self, func: Callable, *args: Any, **kwargs: Any
    ) -> Any:
        """执行 async/sync 函数：协程直接 await，同步函数放线程池。"""
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: func(*args, **kwargs)
        )

    # ---------- 同步限流 ----------
    def run_in_threadpool_with_limit(
        self, func: Callable, *args: Any, **kwargs: Any
    ) -> Any:
        """同步线程池限流：信号量耗尽时降级为当前线程同步执行。

        保证请求不丢失，仅记录 rejected 计数。
        """
        # 非阻塞获取：失败则降级同步执行
        if not self._retrieval_sem.acquire(blocking=False):
            with self._lock:
                self._rejected_retrieval += 1
            return func(*args, **kwargs)
        try:
            with self._lock:
                self._active_retrieval += 1
                if self._active_retrieval > self._peak_retrieval:
                    self._peak_retrieval = self._active_retrieval
            future = self._executor.submit(func, *args, **kwargs)
            return future.result()
        finally:
            with self._lock:
                self._active_retrieval -= 1
            self._retrieval_sem.release()

    def acquire_llm_slot(self) -> bool:
        """获取 LLM 调用槽位，成功返回 True，失败（降级）返回 False。"""
        if not self._llm_sem.acquire(blocking=False):
            with self._lock:
                self._rejected_llm += 1
            return False
        with self._lock:
            self._active_llm += 1
            if self._active_llm > self._peak_llm:
                self._peak_llm = self._active_llm
        return True

    def release_llm_slot(self) -> None:
        """释放 LLM 调用槽位。"""
        with self._lock:
            self._active_llm = max(0, self._active_llm - 1)
        self._llm_sem.release()

    # ---------- 响应时间 ----------
    def record_response_time(self, duration_ms: float) -> None:
        """记录一次请求响应时间（毫秒），用于计算平均响应时间。"""
        with self._lock:
            self._response_times.append(float(duration_ms))

    def get_avg_response_ms(self) -> float:
        """计算最近采样平均响应时间（毫秒）。"""
        with self._lock:
            if not self._response_times:
                return 0.0
            return sum(self._response_times) / len(self._response_times)

    def get_response_sample_count(self) -> int:
        """返回当前响应时间采样数。"""
        with self._lock:
            return len(self._response_times)

    # ---------- 统计 ----------
    def get_stats(self) -> Dict[str, Any]:
        """返回并发与响应时间统计（线程安全）。"""
        with self._lock:
            return {
                "max_concurrent_retrieval": self._max_retrieval,
                "max_concurrent_llm": self._max_llm,
                "active_retrieval": self._active_retrieval,
                "active_llm": self._active_llm,
                "peak_retrieval": self._peak_retrieval,
                "peak_llm": self._peak_llm,
                "rejected_retrieval": self._rejected_retrieval,
                "rejected_llm": self._rejected_llm,
            }

    def reset_stats(self) -> None:
        """重置统计，便于测试隔离。不重建信号量（保持限流能力）。"""
        with self._lock:
            self._active_retrieval = 0
            self._active_llm = 0
            self._peak_retrieval = 0
            self._peak_llm = 0
            self._rejected_retrieval = 0
            self._rejected_llm = 0
            self._response_times.clear()

    def shutdown(self) -> None:
        """关闭线程池，便于测试清理。"""
        self._executor.shutdown(wait=False)


# ----------------------------------------------------------------------
# 单例管理（进程内复用，测试可 reset）
# ----------------------------------------------------------------------
_model_router: Optional[ModelRouter] = None
_hot_query_cache: Optional[HotQueryCache] = None
_intent_cache: Optional[IntentCache] = None
_concurrency_optimizer: Optional[ConcurrencyOptimizer] = None
_singleton_lock = threading.Lock()


def get_model_router() -> ModelRouter:
    """获取 ModelRouter 单例。"""
    global _model_router
    if _model_router is None:
        with _singleton_lock:
            if _model_router is None:
                _model_router = ModelRouter()
    return _model_router


def reset_model_router() -> None:
    """重置单例，便于测试切换配置。"""
    global _model_router
    with _singleton_lock:
        _model_router = None


def get_hot_query_cache() -> HotQueryCache:
    """获取 HotQueryCache 单例。"""
    global _hot_query_cache
    if _hot_query_cache is None:
        with _singleton_lock:
            if _hot_query_cache is None:
                _hot_query_cache = HotQueryCache()
    return _hot_query_cache


def reset_hot_query_cache() -> None:
    """重置单例，便于测试切换配置。"""
    global _hot_query_cache
    with _singleton_lock:
        _hot_query_cache = None


def get_intent_cache() -> IntentCache:
    """获取 IntentCache 单例。"""
    global _intent_cache
    if _intent_cache is None:
        with _singleton_lock:
            if _intent_cache is None:
                _intent_cache = IntentCache()
    return _intent_cache


def reset_intent_cache() -> None:
    """重置单例，便于测试切换配置。"""
    global _intent_cache
    with _singleton_lock:
        _intent_cache = None


def get_concurrency_optimizer() -> ConcurrencyOptimizer:
    """获取 ConcurrencyOptimizer 单例。"""
    global _concurrency_optimizer
    if _concurrency_optimizer is None:
        with _singleton_lock:
            if _concurrency_optimizer is None:
                _concurrency_optimizer = ConcurrencyOptimizer()
    return _concurrency_optimizer


def reset_concurrency_optimizer() -> None:
    """重置单例，便于测试切换配置。"""
    global _concurrency_optimizer
    with _singleton_lock:
        optimizer = _concurrency_optimizer
        _concurrency_optimizer = None
    if optimizer is not None:
        optimizer.shutdown()


# ----------------------------------------------------------------------
# 性能指标聚合
# ----------------------------------------------------------------------
def _percentile(values: List[float], p: float) -> float:
    """计算分位数（线性插值法），空列表返回 0.0。

    p 取 0-1 之间，如 0.95 表示 P95。
    使用线性插值保证小样本下分位数稳定，避免阶跃抖动。
    """
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    # 线性插值：k 为浮点索引，f 为整数下界，c 为上界
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def get_performance_metrics() -> PerformanceMetrics:
    """聚合三个组件统计，返回 PerformanceMetrics。

    供 GET /api/v1/performance/metrics 端点调用，
    避免端点层分别访问三个单例。
    """
    cache = get_hot_query_cache()
    router = get_model_router()
    optimizer = get_concurrency_optimizer()

    cache_stats = CacheStats(**cache.get_stats())
    concurrency_stats = ConcurrencyStats(**optimizer.get_stats())
    routing_dict = router.get_stats()
    # 构建 per_model 列表
    per_model = [
        ModelRouteStat(**item) for item in routing_dict.get("per_model", [])
    ]
    routing_stats = ModelRoutingStats(
        small_model=routing_dict["small_model"],
        large_model=routing_dict["large_model"],
        small_model_calls=routing_dict["small_model_calls"],
        large_model_calls=routing_dict["large_model_calls"],
        total_calls=routing_dict["total_calls"],
        small_model_ratio=routing_dict["small_model_ratio"],
        per_model=per_model,
    )

    # 聚合流式首 Token 耗时：从 Monitor 的 trace steps 中提取
    # 延迟导入避免循环依赖（monitor 不应在模块加载阶段被引入）
    from app.core.monitor import get_monitor

    first_token_durations = get_monitor().get_stream_first_token_durations()
    first_token_avg = (
        sum(first_token_durations) / len(first_token_durations)
        if first_token_durations
        else 0.0
    )
    first_token_p95 = _percentile(first_token_durations, 0.95)

    return PerformanceMetrics(
        cache=cache_stats,
        concurrency=concurrency_stats,
        model_routing=routing_stats,
        avg_response_ms=round(optimizer.get_avg_response_ms(), 2),
        total_response_samples=optimizer.get_response_sample_count(),
        stream_first_token_ms_avg=round(first_token_avg, 2),
        stream_first_token_ms_p95=round(first_token_p95, 2),
    )
