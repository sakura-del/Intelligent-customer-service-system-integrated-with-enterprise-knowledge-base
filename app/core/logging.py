"""日志配置。

统一日志格式与级别，便于在多渠道接入下追踪请求链路。
"""
import logging
import sys

from app.core.config import get_settings


def setup_logging() -> None:
    """初始化全局日志配置。

    在应用启动时调用一次，后续模块直接使用 logging.getLogger 即可。
    DEBUG 模式下输出更详细的信息，便于本地调试。
    """
    settings = get_settings()
    level = logging.DEBUG if settings.DEBUG else logging.INFO

    # 日志格式包含时间、级别、模块、消息，便于排查问题
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt=date_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def get_logger(name: str) -> logging.Logger:
    """获取命名 logger，统一入口便于后续扩展（如文件 handler）。"""
    return logging.getLogger(name)
