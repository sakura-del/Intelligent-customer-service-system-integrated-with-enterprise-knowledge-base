"""FastAPI 应用入口。

负责创建应用实例、注册路由、配置 CORS 与日志，
并挂载前端静态资源，作为 Uvicorn 启动的 ASGI 入口。
"""
import os

from fastapi import FastAPI
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

# 静态资源目录：与 main.py 同级的 static 目录
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


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
    )

    # CORS 配置：开发环境放开全部来源，生产环境需收紧白名单
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.DEBUG else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
