"""FastAPI 应用入口。

负责创建应用实例、注册路由、配置 CORS 与日志，
并挂载前端静态资源，作为 Uvicorn 启动的 ASGI 入口。
"""

import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import (
    agent,
    chat,
    escalation,
    evaluation,
    gateway,
    health,
    knowledge,
    mining,
    monitor,
    observability,
    operations,
    performance,
    tuner,
    update,
)
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.session import get_session_manager
from app.middleware.rate_limit import limiter as slowapi_limiter
from app.middleware.rate_limit import rate_limit_middleware

# 静态资源目录：与 main.py 同级的 static 目录
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _cleanup_session_loop() -> None:
    """后台定时清理过期会话。

    以 SESSION_CLEANUP_INTERVAL 为周期循环扫描，清理超过
    SESSION_TTL 未活动的会话。异常时仅记录 warning 不退出循环，
    保证清理线程在单次异常后仍能继续运行。
    清理日志由 cleanup_expired_sessions 内部记录，此处不重复输出。
    """
    settings = get_settings()
    logger = get_logger("app.main.session_cleanup")
    while True:
        # 先休眠再清理：启动时短暂等待，避免在应用尚未就绪时立即扫描
        time.sleep(settings.SESSION_CLEANUP_INTERVAL)
        try:
            session_manager = get_session_manager()
            # cleanup_expired_sessions 内部已记录 info 日志，此处不重复
            session_manager.cleanup_expired_sessions(settings.SESSION_TTL)
        except Exception as e:
            # 捕获所有异常避免线程意外退出，导致会话泄漏
            logger.warning("会话清理异常：%s", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理。

    在启动阶段执行安全配置检查，提醒运维人员关注鉴权配置，
    避免生产环境因遗漏 API_KEY 配置而暴露接口。
    同时启动后台 daemon 线程定时清理过期会话，防止内存泄漏。
    """
    settings = get_settings()
    logger = get_logger("app.main")

    # 启动时检查 API_KEY 配置：为空说明系统处于不安全模式
    if settings.insecure_mode:
        logger.warning("⚠️ API_KEY 未配置，系统运行在不安全模式！生产环境必须配置 API_KEY")

    # 启动会话清理后台线程：daemon=True 确保进程退出时线程自动终止
    cleanup_thread = threading.Thread(
        target=_cleanup_session_loop,
        daemon=True,
        name="session-cleanup",
    )
    cleanup_thread.start()
    logger.info(
        "会话清理后台线程已启动，扫描间隔 %ds，TTL %ds",
        settings.SESSION_CLEANUP_INTERVAL,
        settings.SESSION_TTL,
    )

    yield


def create_app() -> FastAPI:
    """应用工厂函数。

    集中完成应用初始化，便于测试与多环境部署时复用。
    """
    settings = get_settings()
    setup_logging()
    logger = get_logger("app.main")

    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        description="整合企业知识库的智能客服系统",
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    # CORS 配置：优先使用 ALLOWED_ORIGINS 白名单，未配置时按 DEBUG 模式回退
    # 从 ALLOWED_ORIGINS 配置读取白名单（逗号分隔）
    allowed_origins = (
        [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
        if settings.ALLOWED_ORIGINS
        else []
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins if allowed_origins else (["*"] if settings.DEBUG else []),
        allow_credentials=bool(allowed_origins),  # 白名单模式才允许携带凭据
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        """添加安全响应头。

        为所有响应附加通用的安全头以降低常见 Web 攻击面：
        - X-Content-Type-Options：阻止 MIME 嗅探
        - X-Frame-Options：禁止被任意站点 iframe 嵌套（防点击劫持）
        - Referrer-Policy：限制 Referer 泄露
        - HSTS：仅在 HTTPS 下启用，强制后续访问走 HTTPS
        """
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # 仅 HTTPS 时添加 HSTS，避免在 HTTP 调试环境下误锁
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # 全局 IP 限流中间件：默认 60 req/min/IP，超限返回 429
    # RATE_LIMIT_ENABLED=False 时自动降级为放行，不影响主链路
    @app.middleware("http")
    async def _global_rate_limit(request: Request, call_next):
        """全局 IP 限流中间件，基于滑动窗口实现。"""
        return await rate_limit_middleware(request, call_next)

    # 注册 slowapi Limiter 实例到 app.state，便于未来在特定路由上
    # 添加 @limiter.limit() 装饰器做精细化控制（当前主链路使用上面的自定义中间件）
    if slowapi_limiter is not None:
        app.state.limiter = slowapi_limiter
        try:
            from slowapi import _rate_limit_exceeded_handler
            from slowapi.errors import RateLimitExceeded

            app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        except ImportError:  # pragma: no cover - slowapi 版本差异时的降级
            pass

    # 注册各业务路由
    app.include_router(health.router)
    app.include_router(gateway.router)
    app.include_router(chat.router)
    app.include_router(knowledge.router)
    app.include_router(monitor.router)
    app.include_router(escalation.router)
    # 坐席辅助：与 escalation 同属人机协同闭环，紧随其后注册便于关联查阅
    app.include_router(agent.router)
    # 阶段四：知识库体系完善 - 新增路由
    app.include_router(update.router)
    app.include_router(mining.router)
    app.include_router(tuner.router)
    app.include_router(evaluation.router)
    # 阶段五：优化与上线 - 新增路由
    app.include_router(performance.router)
    app.include_router(observability.router)
    app.include_router(operations.router)

    # 挂载静态资源：/static/* 直接映射到 app/static 目录
    # 仅在目录存在时挂载，避免测试或精简部署时启动失败
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            """根路径返回前端对话界面 index.html。"""
            return FileResponse(os.path.join(STATIC_DIR, "index.html"))

        @app.get("/monitor", include_in_schema=False)
        def monitor_page() -> FileResponse:
            """监控面板入口，返回 monitor.html。"""
            return FileResponse(os.path.join(STATIC_DIR, "monitor.html"))

        @app.get("/operations", include_in_schema=False)
        def operations_page() -> FileResponse:
            """运营看板入口，返回 operations.html。"""
            return FileResponse(os.path.join(STATIC_DIR, "operations.html"))

    logger.info("应用 %s 已完成初始化", settings.APP_NAME)
    return app


# ASGI 入口对象，供 uvicorn 直接引用
app = create_app()
