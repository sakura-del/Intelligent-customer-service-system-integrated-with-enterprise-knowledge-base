"""日志 PII 自动脱敏测试。

覆盖：
- PIIMaskingFilter.mask 对四类 PII（手机号/身份证/邮箱/银行卡）的脱敏准确性
- filter 方法对 record.msg 和 record.args 的处理
- 多 PII 同时存在的脱敏
- 无 PII 日志保持不变
- 短数字不被误匹配
- 身份证末位 X 的兼容
- setup_logging 将 Filter 注册到 root logger handler
"""
from __future__ import annotations

import logging

import pytest

from app.core.logging import PIIMaskingFilter, setup_logging


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def _make_record(msg, args=None) -> logging.LogRecord:
    """构造 LogRecord，用于测试 filter 方法。"""
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )


# ----------------------------------------------------------------------
# 单类 PII 脱敏
# ----------------------------------------------------------------------


def test_mask_phone_number():
    """手机号脱敏：前3后4，中间4位打码。"""
    assert PIIMaskingFilter.mask("用户手机号13812345678") == "用户手机号138****5678"


def test_mask_id_card():
    """身份证脱敏：前6后4，中间8位打码。"""
    assert (
        PIIMaskingFilter.mask("身份证110101199001011234")
        == "身份证110101********1234"
    )


def test_mask_id_card_with_x():
    """身份证末位为 X 时也应正确脱敏。"""
    assert (
        PIIMaskingFilter.mask("身份证11010119900101123X")
        == "身份证110101********123X"
    )


def test_mask_email():
    """邮箱脱敏：保留用户名首字母，域名完整保留。"""
    assert PIIMaskingFilter.mask("邮箱user@example.com") == "邮箱u***@example.com"


def test_mask_bank_card():
    """银行卡脱敏：前4后4，中间打码。"""
    assert (
        PIIMaskingFilter.mask("卡号622202123456789012")
        == "卡号6222****9012"
    )


# ----------------------------------------------------------------------
# 多 PII 同时存在
# ----------------------------------------------------------------------


def test_mask_multiple_pii():
    """多种 PII 同时存在时应全部脱敏，且原始 PII 不残留。"""
    text = (
        "手机13812345678邮箱user@example.com"
        "身份证110101199001011234卡号622202123456789012"
    )
    result = PIIMaskingFilter.mask(text)
    # 脱敏后的片段均存在
    assert "138****5678" in result
    assert "u***@example.com" in result
    assert "110101********1234" in result
    assert "6222****9012" in result
    # 原始 PII 不应残留
    assert "13812345678" not in result
    assert "user@example.com" not in result
    assert "110101199001011234" not in result
    assert "622202123456789012" not in result


# ----------------------------------------------------------------------
# 无 PII 日志不变
# ----------------------------------------------------------------------


def test_mask_no_pii_unchanged():
    """无 PII 的日志应保持原样。"""
    text = "这是一条普通日志，没有任何敏感信息"
    assert PIIMaskingFilter.mask(text) == text


# ----------------------------------------------------------------------
# 短数字不被误匹配
# ----------------------------------------------------------------------


def test_mask_short_number_not_matched():
    """短数字（如订单号、验证码）不应被误匹配为 PII。"""
    assert PIIMaskingFilter.mask("订单号12345") == "订单号12345"
    assert PIIMaskingFilter.mask("验证码123456") == "验证码123456"
    assert PIIMaskingFilter.mask("端口号8080") == "端口号8080"


def test_mask_three_digit_not_matched():
    """3 位数字不应匹配手机号正则。"""
    assert PIIMaskingFilter.mask("错误码123") == "错误码123"


# ----------------------------------------------------------------------
# filter 方法
# ----------------------------------------------------------------------


def test_filter_masks_record_msg():
    """filter 应对 record.msg 中的 PII 进行脱敏。"""
    record = _make_record("用户手机号13812345678")
    PIIMaskingFilter().filter(record)
    assert record.msg == "用户手机号138****5678"


def test_filter_masks_record_args_tuple():
    """filter 应对 record.args（tuple 形式）中的 PII 进行脱敏。"""
    record = _make_record("用户手机号%s邮箱%s", ("13812345678", "user@example.com"))
    PIIMaskingFilter().filter(record)
    assert record.args == ("138****5678", "u***@example.com")
    assert record.getMessage() == "用户手机号138****5678邮箱u***@example.com"


def test_filter_masks_record_args_dict():
    """filter 应对 record.args（dict 形式）中的 PII 进行脱敏。"""
    # LogRecord 要求 dict 参数包裹在 tuple 中：(dict,)
    record = _make_record("用户%(phone)s", ({"phone": "13812345678"},))
    PIIMaskingFilter().filter(record)
    assert record.args == {"phone": "138****5678"}
    assert record.getMessage() == "用户138****5678"


def test_filter_returns_true():
    """filter 应始终返回 True（放行日志记录）。"""
    record = _make_record("任何内容")
    assert PIIMaskingFilter().filter(record) is True


def test_filter_handles_non_string_msg():
    """record.msg 为非字符串时不应报错。"""
    record = _make_record(12345)
    assert PIIMaskingFilter().filter(record) is True
    assert record.msg == 12345


def test_filter_handles_empty_args():
    """record.args 为 None 时不应报错。"""
    record = _make_record("普通消息")
    assert PIIMaskingFilter().filter(record) is True
    assert record.msg == "普通消息"


def test_filter_preserves_non_string_args():
    """record.args 中的非字符串参数应原样保留。"""
    record = _make_record("计数%d", (42,))
    PIIMaskingFilter().filter(record)
    assert record.args == (42,)


# ----------------------------------------------------------------------
# Filter 注册到 handler
# ----------------------------------------------------------------------


@pytest.fixture()
def _restore_root_logger():
    """保存并恢复 root logger 的 handlers 与 filters，避免测试间污染。"""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_filters = {id(h): list(h.filters) for h in original_handlers}
    yield
    # 移除测试中新增的 handler
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
    # 恢复原始 handler 的 filters
    for handler in original_handlers:
        handler.filters = list(original_filters.get(id(handler), []))


def test_setup_logging_registers_pii_filter(_restore_root_logger):
    """setup_logging 应将 PIIMaskingFilter 注册到 root logger 的 handler。"""
    setup_logging()
    root = logging.getLogger()
    # root logger 至少有一个 handler
    assert len(root.handlers) > 0
    # 至少一个 handler 上存在 PIIMaskingFilter 实例
    has_pii_filter = any(
        isinstance(f, PIIMaskingFilter)
        for handler in root.handlers
        for f in handler.filters
    )
    assert has_pii_filter


def test_pii_filter_masks_in_actual_logging(_restore_root_logger):
    """端到端验证：通过 logging 输出的 PII 应被自动脱敏。"""
    setup_logging()
    # 用自定义 handler 捕获日志输出
    captured: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    capture_handler = _CaptureHandler(level=logging.DEBUG)
    # 同步注册 PIIMaskingFilter，模拟生产 handler 的行为
    capture_handler.addFilter(PIIMaskingFilter())
    root = logging.getLogger()
    root.addHandler(capture_handler)
    try:
        logging.getLogger("pii_test").info("用户手机号13812345678")
    finally:
        root.removeHandler(capture_handler)

    assert len(captured) == 1
    assert "138****5678" in captured[0]
    assert "13812345678" not in captured[0]
