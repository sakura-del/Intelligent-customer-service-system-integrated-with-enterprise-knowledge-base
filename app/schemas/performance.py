"""性能优化相关数据模型。

定义 Task 20 性能优化模块对外的数据契约：
- CacheStats：热点缓存命中率与容量统计
- ConcurrencyStats：并发限流与连接池监控统计
- ModelRoutingStats：大小模型路由分布统计
- PerformanceMetrics：综合性能指标聚合，供 /performance/metrics 返回
- InvalidateResult / CacheStatsResponse / MetricsResponse：API 响应封装

设计目标：把核心模块的内部 dict 统一收敛为 Pydantic 模型，
便于 API 层直接 response_model 校验输出，同时作为前后端数据契约。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CacheStats(BaseModel):
    """热点缓存统计。

    hit_rate = hits / (hits + misses)，未发生任何访问时为 0.0。
    size 为当前缓存条目数，max_size 为容量上限，evicted 为 LRU 淘汰累计计数。
    """

    hits: int = Field(0, description="缓存命中累计次数")
    misses: int = Field(0, description="缓存未命中累计次数")
    hit_rate: float = Field(0.0, description="命中率，0-1 之间")
    size: int = Field(0, description="当前缓存条目数")
    max_size: int = Field(0, description="缓存容量上限")
    evicted: int = Field(0, description="LRU 淘汰累计条目数")
    ttl_seconds: int = Field(0, description="单条缓存 TTL，单位秒")


class ConcurrencyStats(BaseModel):
    """并发限流与连接池监控统计。

    active_retrieval / active_llm 为当前各自在途并发数；
    peak 记录历史峰值，rejected 为超限被拒绝（降级同步执行）的累计次数。
    """

    max_concurrent_retrieval: int = Field(0, description="检索最大并发上限")
    max_concurrent_llm: int = Field(0, description="LLM 调用最大并发上限")
    active_retrieval: int = Field(0, description="当前在途检索并发数")
    active_llm: int = Field(0, description="当前在途 LLM 调用并发数")
    peak_retrieval: int = Field(0, description="检索并发历史峰值")
    peak_llm: int = Field(0, description="LLM 调用并发历史峰值")
    rejected_retrieval: int = Field(0, description="检索超限降级为同步执行的累计次数")
    rejected_llm: int = Field(0, description="LLM 调用超限降级为同步执行的累计次数")


class ModelRouteStat(BaseModel):
    """单个模型的路由统计。"""

    model: str = Field(..., description="模型名称")
    calls: int = Field(0, description="路由到该模型的累计次数")
    avg_complexity: float = Field(0.0, description="路由到该模型的平均复杂度评分，0-1")


class ModelRoutingStats(BaseModel):
    """模型路由统计聚合。"""

    small_model: str = Field("", description="小模型名称")
    large_model: str = Field("", description="大模型名称")
    small_model_calls: int = Field(0, description="路由到小模型的累计次数")
    large_model_calls: int = Field(0, description="路由到大模型的累计次数")
    total_calls: int = Field(0, description="路由决策总次数")
    small_model_ratio: float = Field(0.0, description="小模型占比，0-1，越高越省成本")
    per_model: list[ModelRouteStat] = Field(
        default_factory=list,
        description="按模型名聚合的统计列表，便于细粒度分析",
    )


class PerformanceMetrics(BaseModel):
    """综合性能指标。

    聚合缓存、并发、模型路由与平均响应时间，
    作为 GET /api/v1/performance/metrics 的统一返回结构。
    """

    cache: CacheStats = Field(default_factory=CacheStats, description="热点缓存统计")
    concurrency: ConcurrencyStats = Field(
        default_factory=ConcurrencyStats, description="并发限流统计"
    )
    model_routing: ModelRoutingStats = Field(
        default_factory=ModelRoutingStats, description="模型路由统计"
    )
    avg_response_ms: float = Field(0.0, description="最近请求平均响应时间，单位毫秒")
    total_response_samples: int = Field(0, description="响应时间采样总数")
    stream_first_token_ms_avg: float = Field(0.0, description="流式对话首 Token 平均耗时，单位毫秒")
    stream_first_token_ms_p95: float = Field(0.0, description="流式对话首 Token P95 耗时，单位毫秒")


class InvalidateResult(BaseModel):
    """缓存失效操作结果。"""

    success: bool = Field(..., description="是否成功清空")
    cleared: int = Field(0, description="被清除的条目数")
    message: str = Field("", description="补充说明")


class CacheStatsResponse(BaseModel):
    """缓存统计响应封装。"""

    cache: CacheStats = Field(..., description="缓存统计")


class MetricsResponse(BaseModel):
    """性能指标响应封装。"""

    metrics: PerformanceMetrics = Field(..., description="综合性能指标")


# 缓存条目内部结构（仅供 core 模块使用，不对外暴露）
class CacheEntry(BaseModel):
    """单条缓存条目。

    内部使用，存储最终回复、来源与过期时间戳，
    模型化便于序列化与扩展（后续可落库或跨进程共享）。
    """

    answer: str = Field(..., description="缓存回复文本")
    sources: list[str] = Field(default_factory=list, description="来源列表")
    expires_at: float = Field(0.0, description="过期时间戳（time.monotonic 基准）")
    created_at: float = Field(0.0, description="写入时间戳，便于诊断 TTL 与命中新鲜度")

    model_config = {"arbitrary_types_allowed": True}
