"""对话生成 Agent（回复润色师）。

作为最终回复的"化妆师"，把 RAGAgent 等其他 Agent 产出的原始答案
润色为友好、人性化的客服回复：统一话术风格、保证上下文连贯、
主动引导追问，并校验话术规范。

LLM 不可用时（mock 模式）降级到规则拼装，保证输出仍符合话术规范。
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.agents.llm_client import LLMClient, get_llm_client
from app.core.logging import get_logger
from app.schemas.dialog import DialogContext, DialogResult

logger = get_logger("app.agents.dialog_agent")

# 系统 prompt：设定客服角色、话术规范与风格要求
SYSTEM_PROMPT = (
    "你是一名亲切专业的电商客服代表。请把提供的原始答案润色为"
    "友好、人性化的客服回复。\n"
    "【话术规范】\n"
    "1. 开头必须使用亲切称呼，如\"您好~\"、\"亲~\"。\n"
    "2. 结尾必须添加引导语，如\"还有什么可以帮您的吗？\"。\n"
    "3. 重要信息分段展示，用换行分隔不同要点。\n"
    "4. 严禁使用过时话术：\"亲，这边建议您\"、"
    "\"亲，给您带来的不便深表歉意\"、\"这边为您\"、\"您反馈的问题\"等。\n"
    "5. 避免机械重复和生硬的事实陈述，保持自然对话感。\n"
    "6. 主动提供相关信息，减少用户追问。\n"
    "【风格要求】\n"
    "- 语气亲切专业，把生硬事实转化为友好对话。\n"
    "- 结合历史对话保持上下文连贯。\n"
    "- 不要暴露原始答案、上下文摘要等内部信息。\n"
    "- 只输出最终的客服回复，不要加任何解释或标记。"
)

# === 话术规范默认配置（可在实例化时覆盖以适配不同业务场景）===

# 禁用话术：过时或机械的表达，出现即视为不合规
DEFAULT_FORBIDDEN_PHRASES: List[str] = [
    "亲，这边建议您",
    "亲，给您带来的不便深表歉意",
    "这边建议您",
    "这边为您",
    "这边给您",
    "您反馈的问题",
]

# 合规开头称呼：回复必须以其中之一开头
DEFAULT_OPENING_GREETINGS: List[str] = [
    "您好~",
    "亲~",
    "您好！",
    "亲爱的用户",
]

# 合规结尾引导：回复必须包含其中之一
DEFAULT_CLOSING_PHRASES: List[str] = [
    "还有什么可以帮您的吗？",
    "如有其他问题随时告诉我哦~",
    "还有其他需要了解的吗？",
    "希望能帮到您，有问题随时找我~",
]

# 上下文摘要中保留的最近对话轮数，避免 prompt 过长
MAX_HISTORY_TURNS = 3
# 单条历史消息的截断长度，控制 token 成本
MAX_HISTORY_CHARS = 50


class DialogAgent:
    """对话润色 Agent。

    持有 LLM 客户端与话术规范配置，polish 方法负责核心润色，
    generate 方法串联润色 + 来源标注 + 引导语 + 话术校验。
    LLM 为 mock 时走规则拼装，保证离线环境下输出仍合规。
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        forbidden_phrases: Optional[List[str]] = None,
        opening_greetings: Optional[List[str]] = None,
        closing_phrases: Optional[List[str]] = None,
    ) -> None:
        # 延迟取单例，便于测试注入自定义实现
        self._llm_client = llm_client
        # 使用传入配置或默认值，复制一份避免共享可变默认参数
        self.forbidden_phrases = (
            list(forbidden_phrases)
            if forbidden_phrases is not None
            else list(DEFAULT_FORBIDDEN_PHRASES)
        )
        self.opening_greetings = (
            list(opening_greetings)
            if opening_greetings is not None
            else list(DEFAULT_OPENING_GREETINGS)
        )
        self.closing_phrases = (
            list(closing_phrases)
            if closing_phrases is not None
            else list(DEFAULT_CLOSING_PHRASES)
        )

    @property
    def llm_client(self) -> LLMClient:
        """延迟初始化 LLM 客户端，未注入时取全局单例。"""
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    # ==================== 对外核心方法 ====================

    def polish(self, raw_answer: str, context: DialogContext) -> str:
        """核心润色方法。

        风格统一 + 人性化表达 + 上下文衔接 + 引导追问，
        LLM 不可用时走规则拼装兜底，保证输出符合话术规范。
        """
        # 空答案直接返回合规的兜底回复，避免无意义润色
        if not raw_answer or not raw_answer.strip():
            return self._build_empty_reply()

        # 移除原始答案中已有的来源标注，避免与 generate 阶段重复
        cleaned_answer = self._strip_existing_sources(raw_answer)

        if self.llm_client.is_mock:
            reply = self._rule_based_polish(cleaned_answer, context)
        else:
            reply = self._llm_polish(cleaned_answer, context)

        # 过滤禁用话术，保证最终输出合规
        reply = self._filter_forbidden(reply)
        return reply

    def generate(
        self,
        raw_answer: str,
        sources: List[str],
        context: DialogContext,
    ) -> DialogResult:
        """完整生成：润色 + 来源标注 + 引导语 + 话术校验。

        sources 与 raw_answer 注入 context，便于 polish 统一取用；
        最终返回 DialogResult，包含回复、来源、话术校验结果与追问建议。
        """
        # 把入参同步到 context，保持上下文一致
        context.raw_answer = raw_answer
        context.sources = list(sources) if sources else []

        # 1. 润色
        reply = self.polish(raw_answer, context)

        # 2. 来源标注：在回复末尾追加参考来源区块
        reply = self._annotate_sources(reply, context.sources)

        # 3. 话术校验
        tone_valid = self.validate_tone(reply)

        # 4. 引导追问建议
        suggestions = self._build_suggestions(context)

        logger.info(
            "对话润色完成：session=%s tone_valid=%s sources=%d mock=%s",
            context.session_id or "-",
            tone_valid,
            len(context.sources),
            self.llm_client.is_mock,
        )

        return DialogResult(
            reply=reply,
            sources=list(context.sources),
            tone_valid=tone_valid,
            suggestions=suggestions,
        )

    def validate_tone(self, text: str) -> bool:
        """话术校验：检查禁用词、开头称呼、结尾引导。

        任一条件不满足即返回 False，规则可在实例化时配置。
        """
        if not text:
            return False
        # 1. 含禁用话术直接不合规
        for phrase in self.forbidden_phrases:
            if phrase in text:
                return False
        # 2. 必须以合规称呼开头
        if not any(text.startswith(greeting) for greeting in self.opening_greetings):
            return False
        # 3. 必须包含合规结尾引导
        return any(phrase in text for phrase in self.closing_phrases)

    # ==================== LLM 模式 ====================

    def _llm_polish(self, raw_answer: str, context: DialogContext) -> str:
        """调用 LLM 进行润色，构造 system + user 消息。"""
        context_summary = self._build_context_summary(context)
        user_content = (
            f"原始答案：\n{raw_answer}\n\n"
            f"上下文信息：\n{context_summary}\n\n"
            "请按话术规范润色为最终客服回复。"
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        try:
            # name/metadata 标记 prompt name=dialog_polish，便于 Langfuse 聚合分析
            reply = self.llm_client.chat(
                messages=messages,
                temperature=0.4,
                name="dialog_polish",
                metadata={"prompt_version": "v1"},
            )
            # LLM 返回空时降级到规则拼装，保证有输出
            if not reply or not reply.strip():
                logger.warning("LLM 返回空回复，降级到规则拼装")
                return self._rule_based_polish(raw_answer, context)
            return reply.strip()
        except Exception as exc:
            # 调用异常时降级，避免拖垮整个对话流程
            logger.warning("LLM 润色失败，降级到规则拼装：%s", exc)
            return self._rule_based_polish(raw_answer, context)

    # ==================== 规则拼装（mock 兜底）====================

    def _rule_based_polish(
        self, raw_answer: str, context: DialogContext
    ) -> str:
        """规则拼装：mock 模式下基于规则生成符合话术规范的回复。

        结构：开头称呼 + 上下文过渡 + 分段主体 + 结尾引导，
        保证输出始终通过 validate_tone 校验。
        """
        parts: List[str] = [self.opening_greetings[0]]

        # 上下文衔接：有历史对话时加过渡语保持连贯
        transition = self._build_transition(context)
        if transition:
            parts.append(transition)

        # 主体：把原始答案分段展示，重要信息分行便于阅读
        body = self._segment_answer(raw_answer)
        if body:
            parts.append(body)

        # 结尾引导
        parts.append(self.closing_phrases[0])
        return "\n".join(parts)

    def _build_transition(self, context: DialogContext) -> str:
        """根据上下文构建过渡语，保证回复与历史对话连贯。

        有历史对话时引用上文，情绪偏低时表达理解，无历史时返回空。
        """
        if not context.history:
            return ""
        # 情绪偏低时先安抚，体现人性化关怀
        if context.emotion_score is not None and context.emotion_score < 0.4:
            return "理解您的着急，我来为您说明~"
        return "关于您提到的问题，"

    def _segment_answer(self, raw_answer: str) -> str:
        """把原始答案分段展示，重要信息分行便于阅读。

        已有换行时保留原格式；单段过长时按句末标点切分。
        """
        text = raw_answer.strip()
        if not text:
            return ""
        # 保留原有换行结构，避免破坏作者意图的分段
        if "\n" in text:
            return text
        # 按句末标点切分（保留标点），多句时分段展示
        sentences = re.split(r"(?<=[。！？])", text)
        sentences = [s.strip() for s in sentences if s.strip()]
        if len(sentences) <= 1:
            return text
        return "\n".join(sentences)

    # ==================== 话术处理工具 ====================

    def _filter_forbidden(self, text: str) -> str:
        """过滤禁用话术，替换为中性表达并清理多余空白。

        确保最终输出不含过时或机械的话术，提升自然度。
        """
        for phrase in self.forbidden_phrases:
            if phrase in text:
                text = text.replace(phrase, "")
        # 清理替换后可能出现的连续空格与行首空格
        text = re.sub(r" +", " ", text)
        text = re.sub(r"\n +", "\n", text)
        return text.strip()

    def _annotate_sources(self, reply: str, sources: List[str]) -> str:
        """在回复末尾追加参考来源区块。

        来源放在结尾引导之后，作为独立区块展示，
        不影响对话部分的自然感。
        """
        if not sources:
            return reply
        source_lines = "\n".join(f"- {s}" for s in sources)
        return f"{reply}\n\n参考来源：\n{source_lines}"

    def _build_suggestions(self, context: DialogContext) -> List[str]:
        """生成引导追问建议，减少用户二次提问。

        基于来源与原始答案关键词生成通用建议，
        主动提供相关信息入口。
        """
        suggestions: List[str] = []
        if context.sources:
            suggestions.append(f"如需查看完整说明，可参考：{context.sources[0]}")
        suggestions.append("如果您需要更详细的操作步骤，请随时告诉我~")
        return suggestions

    def _build_context_summary(self, context: DialogContext) -> str:
        """从上下文中提取关键信息供 LLM 参考。

        优先使用分层摘要（Task 14）：ContextManager 预先生成的精炼文本，
        可显著降低 token 消耗；未提供时回退到取最近若干轮原始历史。
        """
        parts: List[str] = []
        # 分层摘要优先：减少 token、保留关键信息
        if context.layered_summary:
            parts.append(context.layered_summary)
        elif context.history:
            # 只取最近 MAX_HISTORY_TURNS 轮，控制 token 成本
            recent = context.history[-MAX_HISTORY_TURNS:]
            history_text = " | ".join(
                f"{turn.get('role', '?')}: "
                f"{str(turn.get('content', ''))[:MAX_HISTORY_CHARS]}"
                for turn in recent
            )
            parts.append(f"最近对话：{history_text}")
        if context.current_intent:
            parts.append(f"当前意图：{context.current_intent}")
        if context.emotion_score is not None:
            parts.append(f"用户情绪：{context.emotion_score:.1f}")
        return "；".join(parts) if parts else "无历史上下文"

    def _strip_existing_sources(self, text: str) -> str:
        """移除原始答案中已有的来源标注行。

        RAGAgent 产出的答案可能自带「来源：」行，
        移除后由 generate 统一标注，避免重复。
        """
        lines = text.split("\n")
        cleaned = [
            line
            for line in lines
            if not line.strip().startswith("来源：")
        ]
        return "\n".join(cleaned).strip()

    def _build_empty_reply(self) -> str:
        """空答案的合规兜底回复。"""
        return (
            f"{self.opening_greetings[0]}\n"
            "抱歉，暂时没有找到相关信息呢~\n"
            f"{self.closing_phrases[0]}"
        )


# 模块级单例：Agent 编排无状态，进程内复用
_dialog_agent: Optional[DialogAgent] = None


def get_dialog_agent() -> DialogAgent:
    """获取 DialogAgent 单例。"""
    global _dialog_agent
    if _dialog_agent is None:
        _dialog_agent = DialogAgent()
    return _dialog_agent


def reset_dialog_agent() -> None:
    """重置单例，便于测试切换配置。"""
    global _dialog_agent
    _dialog_agent = None
