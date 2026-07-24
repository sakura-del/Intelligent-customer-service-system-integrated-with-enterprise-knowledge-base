"""内容安全过滤器：基于 AC 自动机的高效敏感词匹配，支持三级分级。

运行时双向过滤：
- 用户输入：check_input 检测 block 级命中，命中则拦截不进入 LLM 链路
- LLM 输出：filter_output 替换 warn/mask 级词，避免敏感内容外泄

三级分级：
- block：拦截，check_input 返回拒绝回复，不进入 LLM
- warn：替换为 ***，整词遮蔽
- mask：打码部分字符，保留首尾便于上下文识别

降级策略：pyahocorasick 未安装或初始化异常时 _enabled=False，
所有输入直接通过，不阻断主链路。
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.core.content_filter")

# 敏感词文件路径：与 quality.py 共用，不修改路径
_SENSITIVE_WORDS_PATH = Path(__file__).parent.parent / "knowledge" / "sensitive_words.txt"

# 合法级别集合，非法级别回退为 warn
_VALID_LEVELS = {"block", "warn", "mask"}

# block 级命中时的统一拒绝回复
_BLOCK_REPLY = "抱歉，您的输入包含违规内容，无法继续处理，请调整后重试。"


class ContentFilter:
    """内容安全过滤器：基于 AC 自动机的多模式敏感词匹配。

    线程安全：所有共享状态（_automaton / _word_levels）由 _lock 保护，
    check_input / filter_output 均可安全并发调用。

    降级策略：pyahocorasick 未安装或构建失败时 _enabled=False，
    check_input 直接放行，filter_output 直接返回原文。
    """

    def __init__(self) -> None:
        # RLock 允许同线程在持锁期间再次 acquire，便于内部方法互调
        self._lock = threading.RLock()
        self._enabled: bool = False
        # 敏感词 -> 级别 映射，用于命中后判定处理策略
        self._word_levels: dict[str, str] = {}
        # AC 自动机实例，None 表示无可用自动机（降级或无敏感词）
        self._automaton = None
        self._build()

    # ------------------------------------------------------------------
    # 构建与加载
    # ------------------------------------------------------------------
    def _build(self) -> None:
        """构建 AC 自动机，失败时降级为禁用模式。

        任何异常都不抛出，保证主链路启动不受影响。
        """
        try:
            import ahocorasick
        except ImportError:
            # pyahocorasick 未安装：降级为禁用模式，不阻断主链路
            logger.warning("pyahocorasick 未安装，内容过滤器降级为禁用模式")
            self._enabled = False
            return

        try:
            word_levels = self._load_words()
            if not word_levels:
                # 无敏感词也标记启用，但 automaton 为 None，匹配直接返回空
                logger.info("未加载到敏感词，内容过滤器启用但无匹配规则")
                self._enabled = True
                self._word_levels = {}
                self._automaton = None
                return

            automaton = ahocorasick.Automaton()
            for word, level in word_levels.items():
                # value 携带 (词, 级别)，匹配时直接取用无需二次查表
                automaton.add_word(word, (word, level))
            automaton.make_automaton()

            self._word_levels = word_levels
            self._automaton = automaton
            self._enabled = True
            logger.info("内容过滤器已启用，加载敏感词 %d 个", len(word_levels))
        except Exception as exc:
            # 构建过程任何异常均降级，避免拖垮启动链路
            logger.warning("内容过滤器初始化失败，降级为禁用模式：%s", exc)
            self._enabled = False

    def _load_words(self) -> dict[str, str]:
        """加载敏感词，合并文件与 settings 配置。

        文件格式：每行 `词|级别`，无 `|` 时默认 warn。
        settings.SENSITIVE_WORDS：逗号分隔，默认 warn 级。

        返回 {词: 级别} 字典，已去重。
        """
        word_levels: dict[str, str] = {}

        # 1. 从文件加载：每行 `词|级别`，# 开头为注释
        try:
            if _SENSITIVE_WORDS_PATH.exists():
                lines = _SENSITIVE_WORDS_PATH.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    word, level = self._parse_line(line)
                    if word and word not in word_levels:
                        word_levels[word] = level
        except Exception as exc:
            logger.warning("敏感词文件加载失败：%s", exc)

        # 2. 从 settings.SENSITIVE_WORDS 加载（逗号分隔，默认 warn）
        # 便于运行时动态注入，无需修改文件
        try:
            configured = get_settings().SENSITIVE_WORDS
            if configured:
                for word in configured.split(","):
                    word = word.strip()
                    if word and word not in word_levels:
                        word_levels[word] = "warn"
        except Exception as exc:
            logger.warning("settings.SENSITIVE_WORDS 加载失败：%s", exc)

        return word_levels

    @staticmethod
    def _parse_line(line: str) -> tuple[str, str]:
        """解析单行 `词|级别`，返回 (词, 级别)。

        无 `|` 时级别默认 warn；非法级别回退 warn。
        """
        if "|" in line:
            word, _, level = line.partition("|")
            word = word.strip()
            level = level.strip().lower()
            if level not in _VALID_LEVELS:
                level = "warn"
        else:
            word = line.strip()
            level = "warn"
        return word, level

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def check_input(self, text: str) -> tuple[bool, str, list[str]]:
        """检查用户输入，返回 (是否通过, 拒绝原因, 命中词列表)。

        - block 级命中：返回 (False, 拒绝回复, [所有命中词])
        - warn/mask 命中或无命中：返回 (True, "", [命中词])

        降级模式（_enabled=False）或空文本直接返回 (True, "", [])。
        """
        if not self._enabled or not text:
            return True, "", []

        with self._lock:
            hits = self._match(text)

        if not hits:
            return True, "", []

        hit_words = [w for w, _ in hits]
        # 仅 block 级命中才拦截，warn/mask 在输入侧仅记录不拦截
        block_hits = [w for w, lvl in hits if lvl == "block"]
        if block_hits:
            return False, _BLOCK_REPLY, hit_words

        return True, "", hit_words

    def filter_output(self, text: str) -> str:
        """过滤 LLM 输出，替换 warn/mask 级词。

        - warn：整词替换为 ***
        - mask：保留首尾字符，中间用 * 替代
        - block：不在 filter_output 处理（仅 check_input 拦截）

        降级模式或无自动机直接返回原文。
        """
        if not self._enabled or not text or self._automaton is None:
            return text

        with self._lock:
            matches = self._match_with_positions(text, levels=("warn", "mask"))

        if not matches:
            return text

        # 按起始位置排序，相同起点取最长匹配
        matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))

        # 贪心选取非重叠区间，避免替换后索引错乱
        selected: list[tuple[int, int, str, str]] = []
        last_end = 0
        for start, end, word, level in matches:
            if start >= last_end:
                selected.append((start, end, word, level))
                last_end = end

        # 拼接结果：原片段 + 替换文本
        result: list[str] = []
        pos = 0
        for start, end, word, level in selected:
            result.append(text[pos:start])
            if level == "warn":
                result.append("*" * 3)
            else:  # mask
                result.append(self._mask_word(word))
            pos = end
        result.append(text[pos:])
        return "".join(result)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _mask_word(word: str) -> str:
        """打码：保留首尾字符，中间用 * 替代。

        长度 <= 2 时全部替换为 *，避免短词泄露过多信息。
        """
        if len(word) <= 2:
            return "*" * len(word)
        return word[0] + "*" * (len(word) - 2) + word[-1]

    def _match(self, text: str) -> list[tuple[str, str]]:
        """AC 自动机匹配，返回去重后的 [(词, 级别), ...]。"""
        if self._automaton is None:
            return []
        hits: set = set()
        for _, (word, level) in self._automaton.iter(text):
            hits.add((word, level))
        return list(hits)

    def _match_with_positions(
        self, text: str, levels: tuple[str, ...] | None = None
    ) -> list[tuple[int, int, str, str]]:
        """AC 自动机匹配并返回带位置的区间列表。

        返回 [(start, end_exclusive, 词, 级别), ...]。
        levels 不为 None 时仅保留指定级别的命中。
        """
        if self._automaton is None:
            return []
        matches: list[tuple[int, int, str, str]] = []
        for end_idx, (word, level) in self._automaton.iter(text):
            if levels is not None and level not in levels:
                continue
            # ahocorasick 的 end_idx 是 inclusive，转成 exclusive 便于切片
            start = end_idx - len(word) + 1
            matches.append((start, end_idx + 1, word, level))
        return matches


# 模块级单例：进程内复用，避免每个请求重建自动机
_filter: ContentFilter | None = None
_filter_lock = threading.Lock()


def get_content_filter() -> ContentFilter:
    """获取 ContentFilter 单例（双重检查锁定）。

    首次调用时构建自动机，后续直接返回缓存实例。
    线程安全：双重检查 + Lock，避免多线程并发构建多个实例。
    """
    global _filter
    if _filter is None:
        with _filter_lock:
            if _filter is None:
                _filter = ContentFilter()
    return _filter


def reset_content_filter() -> None:
    """重置单例，便于测试切换配置或注入 mock。

    下次调用 get_content_filter 会重新构建。
    """
    global _filter
    with _filter_lock:
        _filter = None
