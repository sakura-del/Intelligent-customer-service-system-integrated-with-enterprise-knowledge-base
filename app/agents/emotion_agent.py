"""情感分析 Agent（心理师）。

识别用户消息中的情绪类型与激烈程度，给出应对策略与转人工建议。
支持五类情绪：愤怒、焦虑、失望、满意、中性。

LLM 不可用时（mock 模式）降级到关键词规则兜底，
保证离线环境下仍能产出可用的情绪判断。
"""

from __future__ import annotations

import json
import re

from app.agents.llm_client import LLMClient, get_llm_client
from app.core.logging import get_logger
from app.schemas.emotion import EmotionResult, EmotionType

logger = get_logger("app.agents.emotion_agent")

# ==================== 关键词规则配置 ====================
# 每类情绪对应一组触发关键词及分数区间 [base, max]：
# 命中 1 个关键词取 base，每多命中 1 个 +1，上限为 max。
# 顺序决定优先级：愤怒 > 焦虑 > 失望 > 满意，越严重的情绪越优先判定。
EMOTION_KEYWORD_RULES: list[tuple[EmotionType, list[str], int, int]] = [
    (
        EmotionType.ANGER,
        # 脏话、投诉、威胁差评、垃圾等高激烈负面表达
        [
            "垃圾",
            "投诉",
            "差评",
            "骗子",
            "恶心",
            "无耻",
            "神经病",
            "傻逼",
            "去死",
            "气死",
            "滚",
            "妈的",
            "靠",
        ],
        4,
        5,
    ),
    (
        EmotionType.ANXIETY,
        # 反复追问、着急、担心等焦虑表达
        ["着急", "担心", "怎么还", "到底", "什么时候", "还没", "多久", "赶紧", "快点", "急"],
        3,
        4,
    ),
    (
        EmotionType.DISAPPOINTMENT,
        # 不满、失望、对比预期等失落表达
        ["失望", "不满", "跟预期", "不如", "没想到", "太差", "不值", "不应该"],
        2,
        3,
    ),
    (
        EmotionType.SATISFACTION,
        # 感谢、表扬、好评等正面表达
        ["谢谢", "感谢", "好评", "太棒", "不错", "满意", "喜欢", "专业", "棒"],
        1,
        2,
    ),
]

# 情绪类型 → 应对策略文本，按 spec 定义每类情绪的标准应对方式
EMOTION_STRATEGIES: dict[EmotionType, str] = {
    EmotionType.ANGER: "先安抚用户情绪，表达理解与歉意；优先转人工客服由专人跟进处理",
    EmotionType.ANXIETY: "详细解释处理流程与原因，给出明确的时间预期，缓解用户焦虑",
    EmotionType.DISAPPOINTMENT: "诚恳道歉，承认不足，主动提供解决方案或补偿方案",
    EmotionType.SATISFACTION: "礼貌回应感谢，邀请用户评价并推荐其他服务",
    EmotionType.NEUTRAL: "按标准流程处理用户咨询，提供准确信息",
}

# 触发转人工的愤怒分数阈值：超过该值表示激烈愤怒，必须转人工
ANGER_ESCALATE_THRESHOLD = 4

# LLM 系统 prompt：约束模型只返回指定格式的 JSON
SYSTEM_PROMPT = (
    "你是一名客服情绪分析专家。请分析用户消息的情绪，"
    "只返回 JSON，不要任何额外文本或解释。\n"
    "JSON 字段：\n"
    '- emotion: 情绪类型，取值 "anger"/"anxiety"/"disappointment"/'
    '"satisfaction"/"neutral"\n'
    "- score: 激烈程度 1-5 整数，1=轻微，5=强烈\n"
    "- confidence: 置信度 0-1 浮点数\n"
    "- keywords: 触发情绪的关键词列表（字符串数组）\n"
    "判定参考：\n"
    "- anger: 脏话、投诉、威胁差评、垃圾 → score 4-5\n"
    "- anxiety: 反复追问、着急、担心 → score 3-4\n"
    "- disappointment: 不满、失望、对比预期 → score 2-3\n"
    "- satisfaction: 感谢、表扬、好评 → score 1-2\n"
    "- neutral: 正常咨询 → score 1"
)


class EmotionAgent:
    """情感分析 Agent（心理师）。

    持有 LLM 客户端，analyze 方法对外提供情绪识别 + 策略建议 + 转人工判断。
    LLM 为 mock 时走关键词规则兜底，保证离线环境可用。
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        # 延迟取单例，便于测试注入自定义实现
        self._llm_client = llm_client

    @property
    def llm_client(self) -> LLMClient:
        """延迟初始化 LLM 客户端，未注入时取全局单例。"""
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    # ==================== 对外核心方法 ====================

    def analyze(self, query: str, session_id: str | None = None) -> EmotionResult:
        """分析用户消息的情绪并给出应对策略。

        空查询直接返回中性兜底；mock 模式走规则；LLM 模式解析 JSON，
        解析失败时降级到规则兜底，保证不抛异常。
        """
        # 空查询：返回中性兜底，避免无意义分析
        if not query or not query.strip():
            logger.info("空查询，返回中性兜底：session=%s", session_id or "-")
            return self._build_result(EmotionType.NEUTRAL, score=1, confidence=0.5, keywords=[])

        if self.llm_client.is_mock:
            result = self._rule_based_analyze(query)
        else:
            result = self._llm_analyze(query)

        logger.info(
            "情感分析完成：session=%s emotion=%s score=%d escalate=%s mock=%s",
            session_id or "-",
            result.emotion.value,
            result.score,
            result.suggest_escalate,
            self.llm_client.is_mock,
        )
        return result

    # ==================== 规则兜底（mock 模式）====================

    def _rule_based_analyze(self, query: str) -> EmotionResult:
        """基于关键词规则识别情绪。

        按优先级遍历情绪规则，首个命中关键词的情绪类型胜出；
        全部未命中时判定为中性。
        """
        emotion, matched_keywords = self._match_emotion_by_rules(query)

        if emotion is None:
            # 未命中任何关键词：中性咨询
            return self._build_result(EmotionType.NEUTRAL, score=1, confidence=0.5, keywords=[])

        match_count = len(matched_keywords)
        score = self._compute_score(emotion, match_count)
        confidence = self._compute_confidence(match_count)
        return self._build_result(
            emotion,
            score=score,
            confidence=confidence,
            keywords=matched_keywords,
        )

    @staticmethod
    def _match_emotion_by_rules(
        query: str,
    ) -> tuple[EmotionType | None, list[str]]:
        """按优先级匹配情绪关键词，返回命中的情绪类型与关键词列表。

        遍历 EMOTION_KEYWORD_RULES（已按严重程度排序），
        首个命中关键词的情绪胜出，避免轻度情绪覆盖严重情绪。
        """
        for emotion, keywords, _, _ in EMOTION_KEYWORD_RULES:
            matched = [kw for kw in keywords if kw in query]
            if matched:
                return emotion, matched
        return None, []

    @staticmethod
    def _compute_score(emotion: EmotionType, match_count: int) -> int:
        """根据情绪类型与命中数计算激烈程度分数。

        base 为该情绪的基础分，每多命中一个关键词 +1，上限为 max。
        """
        for emo, _, base, max_score in EMOTION_KEYWORD_RULES:
            if emo == emotion:
                # 命中数越多分数越高，但不超过该情绪的上限
                return min(max_score, base + (match_count - 1))
        # 中性情绪固定 1 分
        return 1

    @staticmethod
    def _compute_confidence(match_count: int) -> float:
        """根据关键词命中数估算置信度。

        命中越多置信度越高，上限 0.9；
        离线规则判定本身可信度有限，不设为 1.0。
        """
        return min(0.9, 0.65 + 0.1 * max(0, match_count - 1))

    # ==================== LLM 模式 ====================

    def _llm_analyze(self, query: str) -> EmotionResult:
        """调用 LLM 进行情绪分析，解析返回的 JSON。

        构造 system + user 消息，要求模型返回指定 JSON；
        解析失败或字段非法时降级到规则兜底，保证链路不中断。
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        try:
            # name 标识情绪分析 prompt，metadata 记录 prompt 版本
            raw_response = self.llm_client.chat(
                messages=messages,
                temperature=0.2,
                name="emotion_analyze",
                metadata={"prompt_version": "v1"},
            )
        except Exception as exc:
            # LLM 调用异常：降级到规则兜底
            logger.warning("LLM 调用失败，降级到规则兜底：%s", exc)
            return self._rule_based_analyze(query)

        parsed = self._parse_llm_response(raw_response)
        if parsed is None:
            # JSON 解析失败或字段非法：降级到规则兜底
            logger.warning("LLM 返回非法 JSON，降级到规则兜底：%r", raw_response[:100])
            return self._rule_based_analyze(query)

        emotion, score, confidence, keywords = parsed
        return self._build_result(
            emotion,
            score=score,
            confidence=confidence,
            keywords=keywords,
        )

    @staticmethod
    def _parse_llm_response(
        raw: str,
    ) -> tuple[EmotionType, int, float, list[str]] | None:
        """解析 LLM 返回的 JSON，校验字段并 clamp 到合法区间。

        解析失败或 emotion 非法时返回 None，由调用方决定降级策略。
        """
        if not raw or not raw.strip():
            return None
        try:
            # 容忍模型在 JSON 外包裹 ```json 等标记，提取首个 {...}
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            return None

        # emotion 必须是合法枚举值
        emotion_str = str(data.get("emotion", "")).lower()
        try:
            emotion = EmotionType(emotion_str)
        except ValueError:
            return None

        # score clamp 到 1-5，confidence clamp 到 0-1
        score = max(1, min(5, int(data.get("score", 1))))
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        keywords_raw = data.get("keywords", [])
        keywords = [str(k) for k in keywords_raw] if isinstance(keywords_raw, list) else []
        return emotion, score, confidence, keywords

    # ==================== 结果组装 ====================

    def _build_result(
        self,
        emotion: EmotionType,
        score: int,
        confidence: float,
        keywords: list[str],
    ) -> EmotionResult:
        """组装 EmotionResult，统一推导策略与转人工建议。

        策略按情绪类型查表；转人工仅在愤怒且 score 超过阈值时触发，
        其他情绪不主动转（持续负面场景由上游结合会话历史判断）。
        """
        strategy = EMOTION_STRATEGIES.get(emotion, EMOTION_STRATEGIES[EmotionType.NEUTRAL])
        suggest_escalate = self._should_escalate(emotion, score)
        return EmotionResult(
            emotion=emotion,
            score=score,
            confidence=confidence,
            keywords=list(keywords),
            strategy=strategy,
            suggest_escalate=suggest_escalate,
        )

    @staticmethod
    def _should_escalate(emotion: EmotionType, score: int) -> bool:
        """判断是否建议转人工。

        仅愤怒且激烈程度超过阈值时强制转人工；
        其他情绪不主动转，避免过度转接增加人工成本。
        """
        return emotion == EmotionType.ANGER and score > ANGER_ESCALATE_THRESHOLD


# 模块级单例：Agent 无状态，进程内复用
_emotion_agent: EmotionAgent | None = None


def get_emotion_agent() -> EmotionAgent:
    """获取 EmotionAgent 单例。"""
    global _emotion_agent
    if _emotion_agent is None:
        _emotion_agent = EmotionAgent()
    return _emotion_agent


def reset_emotion_agent() -> None:
    """重置单例，便于测试切换配置。"""
    global _emotion_agent
    _emotion_agent = None
