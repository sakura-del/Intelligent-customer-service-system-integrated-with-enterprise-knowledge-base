"""日志配置。

统一日志格式与级别，便于在多渠道接入下追踪请求链路。
"""

import logging
import re
import sys

from app.core.config import get_settings


class PIIMaskingFilter(logging.Filter):
    """日志 PII 自动脱敏过滤器：手机号/身份证/邮箱/银行卡自动打码。

    通过预编译正则匹配日志文本中的敏感信息并替换为打码形式。
    Filter 本身无状态，正则对象只读，可在多线程下安全使用。
    """

    # 预编译正则（re.ASCII 使 \b 仅识别 ASCII 单词边界，
    # 避免中文字符被当作 \w 导致边界判断失效）
    _PHONE_PATTERN = re.compile(r"1[3-9]\d{9}", re.ASCII)
    _ID_CARD_PATTERN = re.compile(
        r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
        re.ASCII,
    )
    _EMAIL_PATTERN = re.compile(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        re.ASCII,
    )
    _BANK_CARD_PATTERN = re.compile(r"\b6[0-9]{15,18}\b", re.ASCII)

    @classmethod
    def _mask_phone(cls, match: re.Match) -> str:
        """手机号脱敏：保留前3后4，中间4位用 * 替代。"""
        phone = match.group(0)
        return f"{phone[:3]}****{phone[-4:]}"

    @classmethod
    def _mask_id_card(cls, match: re.Match) -> str:
        """身份证脱敏：保留前6后4，中间8位用 * 替代。"""
        id_card = match.group(0)
        return f"{id_card[:6]}********{id_card[-4:]}"

    @classmethod
    def _mask_email(cls, match: re.Match) -> str:
        """邮箱脱敏：保留用户名首字母，域名完整保留。"""
        email = match.group(0)
        at_index = email.index("@")
        return f"{email[0]}***{email[at_index:]}"

    @classmethod
    def _mask_bank_card(cls, match: re.Match) -> str:
        """银行卡脱敏：保留前4后4，中间用 * 替代。"""
        card = match.group(0)
        return f"{card[:4]}****{card[-4:]}"

    @classmethod
    def mask(cls, text: str) -> str:
        """对文本依次应用全部脱敏规则。

        替换顺序为邮箱 → 身份证 → 银行卡 → 手机号。
        身份证与银行卡优先于手机号，防止手机号正则
        误匹配长数字串内部的 11 位子串。
        """
        text = cls._EMAIL_PATTERN.sub(cls._mask_email, text)
        text = cls._ID_CARD_PATTERN.sub(cls._mask_id_card, text)
        text = cls._BANK_CARD_PATTERN.sub(cls._mask_bank_card, text)
        text = cls._PHONE_PATTERN.sub(cls._mask_phone, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        """对日志记录进行 PII 脱敏，始终返回 True 放行。"""
        # 处理消息模板中的 PII
        if isinstance(record.msg, str):
            record.msg = self.mask(record.msg)

        # 处理格式化参数中的 PII（tuple 或 dict 两种形式）
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(self.mask(arg) if isinstance(arg, str) else arg for arg in args)
        elif isinstance(args, dict):
            record.args = {
                key: self.mask(val) if isinstance(val, str) else val for key, val in args.items()
            }

        return True


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
    # basicConfig 在 root logger 已有 handler 时为 no-op，
    # 显式设置级别确保多次调用也能生效
    logging.getLogger().setLevel(level)

    # 给 root logger 的所有 handler 注册 PII 脱敏过滤器
    pii_filter = PIIMaskingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(pii_filter)


def get_logger(name: str) -> logging.Logger:
    """获取命名 logger，统一入口便于后续扩展（如文件 handler）。"""
    return logging.getLogger(name)
