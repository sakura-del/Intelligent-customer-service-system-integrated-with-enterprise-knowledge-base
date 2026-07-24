"""全局限流中间件：基于 IP 维度的限流。

提供两类限流能力：
1. 全局限流：默认 60 req/min/IP，通过 HTTP 中间件自动覆盖所有接口
2. 端点级限流：通过 @rate_limit 装饰器对重操作端点施加更严格限制

设计要点：
- 线程安全：使用 threading.Lock 保护共享状态，避免并发计数错乱
- 降级策略：RATE_LIMIT_ENABLED=False 时全部降级为放行，不影响主链路
- 滑动窗口：使用 deque + 时间戳实现，比固定窗口更平滑、更精确
- 进程内实现：当前为单进程内存限流，多进程部署需替换为 Redis 等共享存储

依赖说明：
- 优先尝试导入 slowapi（任务要求的依赖），未安装时降级为纯自实现
- slowapi 需在路由上显式添加 @limiter.limit() 装饰器才生效，
  为避免修改路由文件，主链路使用自定义中间件实现全局限流
"""

import functools
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.core.config import get_settings

# 优先尝试导入 slowapi，未安装时降级到纯自实现
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address

    _SLOWAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖未安装时的降级路径
    _SLOWAPI_AVAILABLE = False
    Limiter = None  # type: ignore[assignment, misc]
    get_remote_address = None  # type: ignore[assignment]


# 全局限流阈值：60 req/min/IP（普通业务接口默认值）
GLOBAL_RATE_LIMIT: int = 60
# 重操作限流阈值：10 req/min（知识库入库、文档删除等）
HEAVY_RATE_LIMIT: int = 10
# 时间窗口：60 秒，与「每分钟」语义对齐
RATE_LIMIT_WINDOW: int = 60


class SlidingWindowLimiter:
    """基于滑动窗口的线程安全限流器。

    使用 deque 保存每个 key（通常是客户端 IP）的请求时间戳，
    超出窗口的旧时间戳在每次请求时惰性清理，
    既保证限流精度又避免内存无限增长。
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        """初始化限流器。

        Args:
            max_requests: 窗口内允许的最大请求数
            window_seconds: 时间窗口大小（秒）
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # key -> 该 key 在窗口内的请求时间戳队列
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """检查 key 是否在限流窗口内允许通过。

        Args:
            key: 限流维度标识，通常是客户端 IP

        Returns:
            (allowed, retry_after) 二元组：
            - allowed=True 表示放行，并已记录本次请求时间戳
            - allowed=False 表示被限流，retry_after 提示客户端多久后重试（秒）
        """
        now = time.monotonic()
        window_start = now - self.window_seconds

        with self._lock:
            bucket = self._buckets[key]
            # 惰性清理：弹出超出窗口的旧时间戳，保持 bucket 内都是有效记录
            while bucket and bucket[0] <= window_start:
                bucket.popleft()

            # 已达阈值：拒绝并计算最早请求出窗时间作为重试等待
            if len(bucket) >= self.max_requests:
                retry_after = int(bucket[0] - window_start) + 1
                return False, max(retry_after, 1)

            # 未达阈值：记录本次请求时间戳并放行
            bucket.append(now)
            return True, 0

    def reset(self) -> None:
        """清空所有计数（测试与运维重置场景使用）。"""
        with self._lock:
            self._buckets.clear()


# 全局限流器实例：单进程内共享，所有请求共用 60 req/min/IP 的桶
_global_limiter = SlidingWindowLimiter(GLOBAL_RATE_LIMIT, RATE_LIMIT_WINDOW)

# slowapi Limiter 实例：暴露给需要路由级精细化控制的场景
# 当前主链路使用自定义中间件，此实例仅作为可选扩展点保留，
# 便于未来在特定路由上添加 @limiter.limit() 装饰器
if _SLOWAPI_AVAILABLE:
    limiter: Limiter | None = Limiter(key_func=get_remote_address)  # type: ignore[assignment]
else:
    limiter: Limiter | None = None  # type: ignore[assignment]


def get_client_ip(request: Request) -> str:
    """提取客户端真实 IP，兼容反向代理场景。

    优先级：X-Forwarded-For > X-Real-IP > 连接远端地址。
    X-Forwarded-For 取链中第一个值（最原始的客户端 IP），
    注意：生产环境应在反向代理层覆盖该头以防客户端伪造。

    Args:
        request: FastAPI 请求对象

    Returns:
        客户端 IP 字符串，无法识别时返回 "unknown"
    """
    # X-Forwarded-For: 链式 IP，取第一个（最原始的客户端 IP）
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    # X-Real-IP: 部分反向代理（如 Nginx）会设置
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    # 兜底：使用连接的远端地址
    return request.client.host if request.client else "unknown"


async def rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """全局 IP 限流中间件。

    - RATE_LIMIT_ENABLED=False 时直接放行（降级模式，不影响主链路）
    - 其他情况下按 IP 维度做 60 req/min 滑动窗口限流
    - 超出限流返回 429，并带 Retry-After 头提示客户端重试时机

    Args:
        request: FastAPI 请求对象
        call_next: 下一个中间件/路由处理函数

    Returns:
        正常请求的响应，或 429 限流响应
    """
    settings = get_settings()
    if not settings.RATE_LIMIT_ENABLED:
        return await call_next(request)

    client_ip = get_client_ip(request)
    allowed, retry_after = _global_limiter.check(client_ip)

    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "detail": "请求过于频繁，请稍后再试",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)


def rate_limit(max_requests: int, window_seconds: int = 60) -> Callable:
    """端点级限流装饰器：对特定端点应用更严格限制。

    用于知识库入库、文档删除等重操作端点，
    与全局限流叠加生效（取两者中更严格的限制）。

    用法：
        @router.post("/ingest")
        @rate_limit(10, 60)  # 10 req/min，比全局 60 更严格
        async def ingest(...):
            ...

    Args:
        max_requests: 窗口内允许的最大请求数
        window_seconds: 时间窗口大小（秒），默认 60

    Returns:
        装饰器函数
    """
    local_limiter = SlidingWindowLimiter(max_requests, window_seconds)

    def decorator(func: Callable) -> Callable:
        """装饰目标函数，注入限流检查逻辑。"""

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            settings = get_settings()
            if not settings.RATE_LIMIT_ENABLED:
                return await func(*args, **kwargs)

            # 从位置参数与关键字参数中查找 Request 对象
            request: Request | None = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break
            if request is None:
                request = kwargs.get("request")

            if request is None:
                # 无法识别请求来源，降级放行避免误杀
                return await func(*args, **kwargs)

            client_ip = get_client_ip(request)
            allowed, retry_after = local_limiter.check(client_ip)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "请求过于频繁，请稍后再试",
                        "retry_after": retry_after,
                    },
                    headers={"Retry-After": str(retry_after)},
                )
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def reset_limiters() -> None:
    """重置所有限流器状态（测试辅助函数）。

    在测试间清理全局计数，避免用例间相互污染。
    """
    _global_limiter.reset()
