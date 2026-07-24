"""内容安全过滤器测试。

覆盖：
- AC 自动机匹配准确性（子串、多词、无命中）
- 三级分级：block 拦截 / warn 替换为 *** / mask 保留首尾打码
- check_input 返回格式（三元组结构、block 命中拦截、非 block 放行）
- filter_output 替换逻辑（warn 整词替换、mask 部分打码、多词混合、无命中透传）
- 降级模式：pyahocorasick 未安装时 _enabled=False，所有输入直接通过
- 单例模式：get_content_filter 双重检查 + reset_content_filter 重置
- 线程安全：多线程并发调用不崩溃
- 文件加载：从 sensitive_words.txt 解析 `词|级别` 格式

测试隔离：每个用例前重置 ContentFilter 单例，避免相互污染。
"""
from __future__ import annotations

import sys
import threading
from typing import Dict
from unittest.mock import patch

import pytest

from app.core.content_filter import (
    ContentFilter,
    get_content_filter,
    reset_content_filter,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton_per_test():
    """每个用例前重置 ContentFilter 单例，避免上一用例的实例残留。"""
    reset_content_filter()
    yield
    reset_content_filter()


def _make_filter(word_levels: Dict[str, str]) -> ContentFilter:
    """构造带指定敏感词的 ContentFilter 实例。

    通过 patch _load_words 注入测试词表，绕过文件与 settings 加载，
    保证测试可重复且不依赖外部配置。
    """
    with patch.object(ContentFilter, "_load_words", return_value=word_levels):
        return ContentFilter()


# ----------------------------------------------------------------------
# AC 自动机匹配准确性
# ----------------------------------------------------------------------


def test_ac_match_single_word_substring():
    """AC 自动机应匹配文本中的敏感词子串。"""
    cf = _make_filter({"违禁词": "block"})
    # 完整匹配
    assert cf._match("这里有违禁词") == [("违禁词", "block")]
    # 子串匹配（敏感词嵌在更长文本中）
    assert cf._match("违禁词出现在开头")
    assert cf._match("结尾是违禁词")


def test_ac_match_multiple_words():
    """AC 自动机应同时匹配多个不同的敏感词。"""
    cf = _make_filter({"词甲": "warn", "词乙": "mask", "词丙": "block"})
    hits = cf._match("这里同时有词甲和词乙还有词丙")
    hit_words = {w for w, _ in hits}
    assert hit_words == {"词甲", "词乙", "词丙"}


def test_ac_match_no_hit_returns_empty():
    """无敏感词的文本应返回空列表。"""
    cf = _make_filter({"违禁词": "block"})
    assert cf._match("这是一段正常文本") == []


def test_ac_match_deduplicates_overlapping():
    """重复出现的敏感词应在去重后只出现一次。"""
    cf = _make_filter({"测试": "warn"})
    hits = cf._match("测试测试测试")
    # _match 返回去重后的列表，同一词只出现一次
    assert hits == [("测试", "warn")]


# ----------------------------------------------------------------------
# 三级分级：block / warn / mask
# ----------------------------------------------------------------------


def test_block_level_blocks_input():
    """block 级命中：check_input 返回 (False, 拒绝原因, 命中词)。"""
    cf = _make_filter({"代开发票": "block"})
    passed, reason, hits = cf.check_input("我想代开发票")
    assert passed is False
    assert reason != ""
    assert "代开发票" in hits


def test_warn_level_does_not_block_input():
    """warn 级命中：check_input 放行，仅返回命中词。"""
    cf = _make_filter({"笨蛋": "warn"})
    passed, reason, hits = cf.check_input("你是个笨蛋")
    assert passed is True
    assert reason == ""
    assert "笨蛋" in hits


def test_mask_level_does_not_block_input():
    """mask 级命中：check_input 放行，仅返回命中词。"""
    cf = _make_filter({"手机号码": "mask"})
    passed, reason, hits = cf.check_input("请提供手机号码")
    assert passed is True
    assert reason == ""
    assert "手机号码" in hits


def test_warn_level_replaced_in_output():
    """warn 级：filter_output 将整词替换为 ***。"""
    cf = _make_filter({"笨蛋": "warn"})
    result = cf.filter_output("你是个笨蛋吧")
    assert "笨蛋" not in result
    assert "***" in result


def test_mask_level_masked_in_output():
    """mask 级：filter_output 保留首尾字符，中间用 * 替代。"""
    cf = _make_filter({"手机号码": "mask"})
    result = cf.filter_output("请提供手机号码给我")
    # mask 保留首尾：手(1) + *(2) + 码(1) -> 手**码
    assert "手机号码" not in result
    assert "手" in result
    assert "码" in result
    assert "*" in result


def test_mask_short_word_all_asterisks():
    """mask 级短词（<=2 字符）：全部替换为 *。"""
    cf = _make_filter({"号码": "mask"})
    result = cf.filter_output("这是号码")
    assert "号码" not in result
    assert "**" in result


def test_block_level_not_replaced_in_output():
    """block 级不在 filter_output 处理（仅 check_input 拦截）。"""
    cf = _make_filter({"代开发票": "block"})
    # block 级词在输出中不被替换（任务约定：filter_output 仅处理 warn/mask）
    result = cf.filter_output("代开发票文本")
    assert result == "代开发票文本"


# ----------------------------------------------------------------------
# check_input 返回格式
# ----------------------------------------------------------------------


def test_check_input_returns_three_tuple():
    """check_input 应返回 (bool, str, list) 三元组。"""
    cf = _make_filter({"违禁": "block"})
    result = cf.check_input("正常文本")
    assert isinstance(result, tuple)
    assert len(result) == 3
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)
    assert isinstance(result[2], list)


def test_check_input_empty_text_passes():
    """空文本应直接放行，返回空命中列表。"""
    cf = _make_filter({"违禁": "block"})
    passed, reason, hits = cf.check_input("")
    assert passed is True
    assert reason == ""
    assert hits == []


def test_check_input_no_hit_passes():
    """无命中时应放行，命中词列表为空。"""
    cf = _make_filter({"违禁": "block"})
    passed, reason, hits = cf.check_input("正常文本无敏感词")
    assert passed is True
    assert reason == ""
    assert hits == []


def test_check_input_mixed_levels_only_block_stops():
    """混合级别命中：仅 block 级触发拦截，warn/mask 不拦截。"""
    cf = _make_filter({"拦截词": "block", "警告词": "warn", "打码词": "mask"})
    passed, reason, hits = cf.check_input("拦截词和警告词和打码词")
    assert passed is False
    assert reason != ""
    # 所有命中词都应在列表中
    assert "拦截词" in hits
    assert "警告词" in hits
    assert "打码词" in hits


# ----------------------------------------------------------------------
# filter_output 替换逻辑
# ----------------------------------------------------------------------


def test_filter_output_no_hit_returns_original():
    """无命中时 filter_output 应返回原文。"""
    cf = _make_filter({"违禁": "warn"})
    text = "这是一段正常文本"
    assert cf.filter_output(text) == text


def test_filter_output_multiple_warn_words():
    """多个 warn 级词应全部被替换。"""
    cf = _make_filter({"笨蛋": "warn", "白痴": "warn"})
    result = cf.filter_output("笨蛋和白痴都是不当用语")
    assert "笨蛋" not in result
    assert "白痴" not in result
    assert result.count("***") == 2


def test_filter_output_mixed_warn_and_mask():
    """warn 和 mask 混合时应分别按各自策略替换。"""
    cf = _make_filter({"笨蛋": "warn", "手机号码": "mask"})
    result = cf.filter_output("笨蛋的银行卡号和手机号码")
    # warn 级整词替换
    assert "笨蛋" not in result
    assert "***" in result
    # mask 级保留首尾
    assert "手" in result
    assert "码" in result


def test_filter_output_empty_text_returns_empty():
    """空文本应直接返回空字符串。"""
    cf = _make_filter({"违禁": "warn"})
    assert cf.filter_output("") == ""


def test_filter_output_preserves_non_sensitive_text():
    """非敏感部分文本应完整保留。"""
    cf = _make_filter({"笨蛋": "warn"})
    result = cf.filter_output("前面文本笨蛋后面文本")
    assert "前面文本" in result
    assert "后面文本" in result
    assert "笨蛋" not in result


# ----------------------------------------------------------------------
# 降级模式
# ----------------------------------------------------------------------


def test_degradation_when_ahocorasick_not_installed():
    """pyahocorasick 未安装时降级为禁用模式，不阻断主链路。

    通过将 sys.modules['ahocorasick'] 设为 None 模拟模块未安装，
    ContentFilter._build 中的 `import ahocorasick` 会抛 ImportError。
    """
    saved = sys.modules.pop("ahocorasick", None)
    try:
        with patch.dict(sys.modules, {"ahocorasick": None}):
            cf = ContentFilter()
        # 降级模式：_enabled 为 False
        assert cf._enabled is False
        # check_input 直接放行
        passed, reason, hits = cf.check_input("任何敏感内容")
        assert passed is True
        assert reason == ""
        assert hits == []
        # filter_output 直接返回原文
        assert cf.filter_output("任何敏感内容") == "任何敏感内容"
    finally:
        if saved is not None:
            sys.modules["ahocorasick"] = saved


def test_degradation_when_build_exception():
    """_build 过程异常时降级为禁用模式，不抛出异常。"""
    # 让 _load_words 抛异常，模拟文件加载失败场景
    with patch.object(
        ContentFilter, "_load_words", side_effect=RuntimeError("模拟加载失败")
    ):
        cf = ContentFilter()
    assert cf._enabled is False
    # 降级模式下所有输入直接通过
    assert cf.check_input("任何内容")[0] is True
    assert cf.filter_output("任何内容") == "任何内容"


def test_enabled_with_empty_word_list():
    """无敏感词时 _enabled=True 但 automaton=None，匹配返回空。"""
    with patch.object(ContentFilter, "_load_words", return_value={}):
        cf = ContentFilter()
    assert cf._enabled is True
    assert cf._automaton is None
    # 无规则时所有输入通过
    passed, reason, hits = cf.check_input("任何内容")
    assert passed is True
    assert hits == []
    assert cf.filter_output("任何内容") == "任何内容"


# ----------------------------------------------------------------------
# 单例模式
# ----------------------------------------------------------------------


def test_singleton_returns_same_instance():
    """get_content_filter 多次调用应返回同一实例。"""
    cf1 = get_content_filter()
    cf2 = get_content_filter()
    assert cf1 is cf2


def test_reset_singleton_creates_new_instance():
    """reset_content_filter 后应创建新实例。"""
    cf1 = get_content_filter()
    reset_content_filter()
    cf2 = get_content_filter()
    assert cf1 is not cf2


def test_singleton_is_enabled_with_real_file():
    """单例应从真实 sensitive_words.txt 加载并启用。

    验证文件加载链路：sensitive_words.txt -> _load_words -> _build -> _enabled=True
    """
    cf = get_content_filter()
    # 真实文件含敏感词，应成功启用
    assert cf._enabled is True
    assert cf._automaton is not None
    assert len(cf._word_levels) > 0


# ----------------------------------------------------------------------
# 文件格式解析
# ----------------------------------------------------------------------


def test_parse_line_with_level():
    """_parse_line 应正确解析 `词|级别` 格式。"""
    assert ContentFilter._parse_line("违禁词|block") == ("违禁词", "block")
    assert ContentFilter._parse_line("警告词|warn") == ("警告词", "warn")
    assert ContentFilter._parse_line("打码词|mask") == ("打码词", "mask")


def test_parse_line_without_level_defaults_warn():
    """无 `|` 的行应默认 warn 级。"""
    assert ContentFilter._parse_line("普通词") == ("普通词", "warn")


def test_parse_line_invalid_level_defaults_warn():
    """非法级别应回退为 warn。"""
    assert ContentFilter._parse_line("测试词|invalid") == ("测试词", "warn")


def test_load_words_from_real_file():
    """从真实 sensitive_words.txt 加载应包含三级示例词。"""
    cf = get_content_filter()
    levels = set(cf._word_levels.values())
    # 真实文件含 block / warn / mask 三级
    assert "block" in levels
    assert "warn" in levels
    assert "mask" in levels


def test_load_words_merges_settings_config():
    """settings.SENSITIVE_WORDS 配置的词应被合并加载（默认 warn）。"""
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.SENSITIVE_WORDS
    settings.SENSITIVE_WORDS = "动态敏感词测试"
    try:
        reset_content_filter()
        cf = get_content_filter()
        assert "动态敏感词测试" in cf._word_levels
        assert cf._word_levels["动态敏感词测试"] == "warn"
    finally:
        settings.SENSITIVE_WORDS = original
        reset_content_filter()


# ----------------------------------------------------------------------
# 线程安全
# ----------------------------------------------------------------------


def test_concurrent_check_input_thread_safe():
    """多线程并发调用 check_input 应不崩溃、结果一致。"""
    cf = _make_filter({"拦截词": "block", "警告词": "warn"})
    barrier = threading.Barrier(10)
    results: list = [None] * 10

    def worker(idx: int):
        barrier.wait()
        # 交替使用不同文本
        text = "拦截词" if idx % 2 == 0 else "警告词"
        results[idx] = cf.check_input(text)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 偶数线程命中 block，应被拦截
    for i in range(0, 10, 2):
        assert results[i][0] is False
    # 奇数线程命中 warn，应放行
    for i in range(1, 10, 2):
        assert results[i][0] is True


def test_concurrent_filter_output_thread_safe():
    """多线程并发调用 filter_output 应不崩溃、替换正确。"""
    cf = _make_filter({"笨蛋": "warn", "手机号码": "mask"})
    barrier = threading.Barrier(10)
    results: list = [None] * 10

    def worker(idx: int):
        barrier.wait()
        results[idx] = cf.filter_output("笨蛋的手机号码")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 所有线程的过滤结果应一致且正确
    for r in results:
        assert "笨蛋" not in r
        assert "手机号码" not in r
        assert "***" in r
