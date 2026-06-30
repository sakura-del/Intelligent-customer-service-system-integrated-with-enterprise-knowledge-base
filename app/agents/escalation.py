"""人工客服转接规则引擎。

按优先级实现六类转接规则：
1. 用户主动要求（"转人工"/"找客服"/"人工客服"）→ 最高优先级
2. 情绪激动（愤怒 score > 4）→ 高
3. 连续失败（failed_attempts >= 2，或 3 轮未解决）→ 中
4. 复杂问题（跨 3 个以上业务域，用涉及 agent 数判断）→ 中
5. VIP 用户（高等级会员优先转接）→ 低
6. 工作时间外（非人工服务时间）→ 不转接，仅记录

工作时间判断：从 Settings 读取 WORKING_HOURS_START / END，
非工作时间不因情绪/失败主动转接（避免无人接听反而降低体验），
但用户主动要求仍会转接。

设计要点：
- 规则之间互不依赖，按优先级短路返回，避免重复计算
- 卡片生成复用 SessionManager 与 TicketStore 数据，避免重复查询
- 单例模式与项目其他 Agent 风格保持一致
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.session import session_manager
from app.schemas.emotion import EmotionResult, EmotionType
from app.schemas.escalation import (
    EscalationCard,
    EscalationDecision,
    EscalationPriority,
)
from app.schemas.orchestrator import IntentResult

logger = get_logger("app.agents.escalation")

# 触发"用户主动转人工"的关键词：覆盖常见表达，避免漏识别
HUMAN_REQUEST_KEYWORDS = ("转人工", "找客服", "人工客服", "人工服务", "找人工", "转接人工")

# 情绪转接阈值：愤怒 score 严格大于该值才转，避免误触发
EMOTION_ESCALATE_THRESHOLD = 4

# 连续失败转接阈值：与 OrchestratorAgent 保持一致
FAILED_ATTEMPTS_THRESHOLD = 2

# 智能客服未解决轮数阈值：3 轮仍未解决即转人工，避免无限循环
UNRESOLVED_TURNS_THRESHOLD = 3

# 复杂问题阈值：涉及 agent 数超过该值视为跨多业务域
COMPLEX_AGENT_COUNT_THRESHOLD = 3

# VIP 会员等级关键词：会话/用户标记中含这些即视为 VIP
VIP_LEVEL_KEYWORDS = ("vip", "钻石", " platinum", "diamond", "黄金", "金牌")

# 卡片中已尝试方案的最大保留条数，避免卡片过长
MAX_ATTEMPTED_SOLUTIONS = 5

# 对话摘要最大长度，控制人工阅读成本
SUMMARY_MAX_CHARS = 300


class EscalationEngine:
    """人工客服转接规则引擎。

    持有 SessionManager 与 TicketStore 引用（均延迟取单例），
    check_escalation 是核心入口：按规则优先级短路返回决策。
    build_card 在转接发生时生成上下文卡片，供人工客服快速接手。
    """

    def __init__(self) -> None:
        # 卡片生成需要查会话与历史工单，延迟注入便于测试 mock
        self._session_manager = session_manager

    # ------------------------------------------------------------------
    # 规则引擎主入口
    # ------------------------------------------------------------------
    def check_escalation(
        self,
        query: str,
        session_id: str,
        emotion_result: Optional[EmotionResult] = None,
        intent_result: Optional[IntentResult] = None,
        session_state: Optional[Dict[str, Any]] = None,
        respect_working_hours: bool = True,
        now: Optional[datetime] = None,
    ) -> EscalationDecision:
        """按优先级检查转接规则，返回首个命中规则的决策。

        规则短路：高优先级规则命中即返回，不再检查低优先级规则，
        避免"主动转人工"被"非工作时间"覆盖等误判。
        会话状态查询失败时降级为不转接，保证主链路不中断。

        参数说明：
        - session_state：调用方已有的会话状态（如 OrchestratorAgent 自维护的 state），
          传入时优先使用，避免重复查询 session_manager
        - respect_working_hours：是否应用工作时间限制。
          OrchestratorAgent 等底层组件传 False 跳过该限制（工作时间是部署层策略），
          API/规则引擎直接调用时传 True 应用完整规则。
        """
        # 优先使用调用方传入的 state，避免重复查询 session_manager
        if session_state is not None:
            session = session_state
        else:
            session = self._session_manager.get_session(session_id) or {}
        failed_attempts = int(session.get("failed_attempts", 0))
        turn_count = int(session.get("turn_count", 0))

        # 规则 1：用户主动要求转人工 - 最高优先级，无视工作时间
        decision = self._check_user_request(query)
        if decision is not None:
            return decision

        # 工作时间外：仅记录不转接（情绪/失败等都不触发主动转接）
        # 避免无人接听导致用户体验更差
        # respect_working_hours=False 时跳过此检查，供底层组件使用
        if respect_working_hours and not self._is_within_working_hours(now):
            return EscalationDecision(
                should_escalate=False,
                reason="当前为非人工服务时间，已记录诉求待工作时间处理",
                priority=EscalationPriority.INFO,
                rule_matched="off_hours",
            )

        # 规则 2：情绪激动（愤怒 score > 4）
        decision = self._check_emotion(emotion_result)
        if decision is not None:
            return decision

        # 规则 3：连续失败或 3 轮未解决
        decision = self._check_failures(failed_attempts, turn_count)
        if decision is not None:
            return decision

        # 规则 4：复杂问题（跨 3 个以上业务域）
        decision = self._check_complex_problem(intent_result)
        if decision is not None:
            return decision

        # 规则 5：VIP 用户优先转接
        decision = self._check_vip(session)
        if decision is not None:
            return decision

        # 无规则命中：不转接
        return EscalationDecision(
            should_escalate=False,
            reason="",
            priority=EscalationPriority.INFO,
            rule_matched="",
        )

    # ------------------------------------------------------------------
    # 各规则检查：返回 None 表示未命中，返回 EscalationDecision 表示命中
    # ------------------------------------------------------------------
    @staticmethod
    def _check_user_request(query: str) -> Optional[EscalationDecision]:
        """规则 1：用户主动要求转人工。

        关键词命中即转接，优先级最高，不因非工作时间被阻断
        （用户已明确表达诉求，应保留沟通入口）。
        """
        if not query:
            return None
        if any(keyword in query for keyword in HUMAN_REQUEST_KEYWORDS):
            return EscalationDecision(
                should_escalate=True,
                reason="用户主动要求转接人工客服",
                priority=EscalationPriority.HIGHEST,
                rule_matched="user_request",
            )
        return None

    @staticmethod
    def _check_emotion(
        emotion_result: Optional[EmotionResult],
    ) -> Optional[EscalationDecision]:
        """规则 2：愤怒情绪超过阈值即转接。

        仅愤怒情绪触发，其他情绪由上游情绪处理流程应对，
        避免焦虑等可缓解情绪过度占用人工。
        """
        if emotion_result is None:
            return None
        if (
            emotion_result.emotion == EmotionType.ANGER
            and emotion_result.score > EMOTION_ESCALATE_THRESHOLD
        ):
            return EscalationDecision(
                should_escalate=True,
                reason=f"用户情绪激动（愤怒 score={emotion_result.score}），需人工安抚",
                priority=EscalationPriority.HIGH,
                rule_matched="emotion_anger",
            )
        return None

    @staticmethod
    def _check_failures(
        failed_attempts: int, turn_count: int
    ) -> Optional[EscalationDecision]:
        """规则 3：连续失败或智能客服 3 轮未解决。

        触发任一条件即转接：
        - failed_attempts >= 2：与 OrchestratorAgent 阈值对齐
        - turn_count >= 3 且仍处于失败状态：避免无限循环
        """
        if failed_attempts >= FAILED_ATTEMPTS_THRESHOLD:
            return EscalationDecision(
                should_escalate=True,
                reason=f"连续 {failed_attempts} 轮未解决问题，转人工跟进",
                priority=EscalationPriority.MEDIUM,
                rule_matched="consecutive_failures",
            )
        if turn_count >= UNRESOLVED_TURNS_THRESHOLD and failed_attempts > 0:
            return EscalationDecision(
                should_escalate=True,
                reason=f"已对话 {turn_count} 轮仍未解决，转人工跟进",
                priority=EscalationPriority.MEDIUM,
                rule_matched="unresolved_turns",
            )
        return None

    @staticmethod
    def _check_complex_problem(
        intent_result: Optional[IntentResult],
    ) -> Optional[EscalationDecision]:
        """规则 4：跨 3 个以上业务域即转接。

        用 sub_tasks 涉及的 agent_name 数判断业务域跨度，
        避免智能客服在多域问题上拼接低质量回复。
        """
        if intent_result is None or not intent_result.sub_tasks:
            return None
        # 不同 agent_name 数量反映业务域跨度
        agent_names = {
            task.agent_name for task in intent_result.sub_tasks if task.agent_name
        }
        if len(agent_names) >= COMPLEX_AGENT_COUNT_THRESHOLD:
            return EscalationDecision(
                should_escalate=True,
                reason=f"问题跨 {len(agent_names)} 个业务域，建议人工综合处理",
                priority=EscalationPriority.MEDIUM,
                rule_matched="complex_problem",
            )
        return None

    @staticmethod
    def _check_vip(session: Dict[str, Any]) -> Optional[EscalationDecision]:
        """规则 5：VIP 用户优先转接。

        仅在用户明确为 VIP 等级时触发，优先级低，
        不抢占情绪/失败类高优先级转接资源。
        """
        member_level = str(session.get("member_level", "")).lower()
        if any(kw in member_level for kw in VIP_LEVEL_KEYWORDS):
            return EscalationDecision(
                should_escalate=True,
                reason="VIP 用户优先转接人工",
                priority=EscalationPriority.LOW,
                rule_matched="vip_user",
            )
        return None

    # ------------------------------------------------------------------
    # 工作时间判断
    # ------------------------------------------------------------------
    @staticmethod
    def _is_within_working_hours(now: Optional[datetime] = None) -> bool:
        """判断当前是否在人工服务时间段内。

        使用 Settings 配置的时区与起止小时，
        区间为闭区间 [START, END)，END 即下班点不再算工作时间。
        时区加载失败时降级到本地时间，保证规则可用。

        参数 now：可选，用于测试注入固定时间；为 None 时取当前时间，
        避免测试依赖真实时间导致不稳定。
        """
        settings = get_settings()
        if now is None:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(settings.TIMEZONE)
                now = datetime.now(tz)
            except Exception as exc:
                # 时区不可用时降级到本地时间，保证规则不阻塞主链路
                logger.warning("时区加载失败，降级本地时间：%s", exc)
                now = datetime.now()

        start = settings.WORKING_HOURS_START
        end = settings.WORKING_HOURS_END
        # [start, end) 闭开区间，下班点不再计入工作时间
        return start <= now.hour < end

    # ------------------------------------------------------------------
    # 上下文卡片生成
    # ------------------------------------------------------------------
    def build_card(
        self,
        session_id: str,
        reason: str,
        priority: EscalationPriority = EscalationPriority.INFO,
    ) -> EscalationCard:
        """生成转接上下文卡片，供人工客服接手时参考。

        汇总会话状态、历史工单与对话摘要：
        - conversation_summary：取最近 history 拼接（mock 模式）
        - attempted_solutions：从 history 提取 assistant 回复
        任一查询失败时降级为空值，保证卡片总能生成。
        """
        session = self._session_manager.get_session(session_id) or {}
        history: List[Dict[str, Any]] = session.get("history", []) or []

        # 对话摘要：取最近 user 消息 + assistant 回复拼接，截断避免过长
        summary = self._build_summary(history)
        # 已尝试方案：从 history 提取 assistant 回复，限 MAX_ATTEMPTED_SOLUTIONS 条
        attempted = self._extract_attempted_solutions(history)
        # 历史工单数：复用 TicketStore 统计该用户的工单量
        ticket_count = self._count_user_tickets(session.get("user_id"))

        return EscalationCard(
            session_id=session_id,
            user_id=session.get("user_id"),
            member_level=str(session.get("member_level", "normal")),
            history_ticket_count=ticket_count,
            turn_count=int(session.get("turn_count", 0)),
            conversation_summary=summary,
            attempted_solutions=attempted,
            escalate_reason=reason,
            priority=priority,
        )

    @staticmethod
    def _build_summary(history: List[Dict[str, Any]]) -> str:
        """从对话历史拼装摘要。

        取最近若干轮的 user/assistant 内容拼接，
        mock 模式下无 LLM 摘要能力，此实现保证有可读摘要。
        """
        if not history:
            return ""
        # 取最近 4 条记录做摘要，避免过长
        recent = history[-4:]
        parts: List[str] = []
        for item in recent:
            role = item.get("role", "")
            content = str(item.get("content", ""))[:80]
            if not content:
                continue
            label = "用户" if role == "user" else "客服"
            parts.append(f"{label}：{content}")
        summary = " | ".join(parts)
        return summary[:SUMMARY_MAX_CHARS]

    @staticmethod
    def _extract_attempted_solutions(
        history: List[Dict[str, Any]]
    ) -> List[str]:
        """从历史中提取智能客服已给出的回复作为已尝试方案。

        仅取 assistant 角色内容，倒序保留最近若干条，
        避免人工重复给出已建议过的方案。
        """
        if not history:
            return []
        solutions: List[str] = []
        # 倒序遍历，优先保留最近的方案
        for item in reversed(history):
            if item.get("role") != "assistant":
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            solutions.append(content)
            if len(solutions) >= MAX_ATTEMPTED_SOLUTIONS:
                break
        return solutions

    @staticmethod
    def _count_user_tickets(user_id: Optional[str]) -> int:
        """统计用户历史工单数。

        复用 TicketStore 单例避免重复实例化，
        查询失败时返回 0，保证卡片生成不阻塞。
        """
        if not user_id:
            return 0
        try:
            from app.agents.ticket_store import get_ticket_store

            return len(get_ticket_store().list_tickets(user_id=user_id))
        except Exception as exc:
            logger.warning("查询用户工单数失败，返回 0：%s", exc)
            return 0


# 模块级单例：规则引擎无状态，进程内复用
_escalation_engine: Optional[EscalationEngine] = None


def get_escalation_engine() -> EscalationEngine:
    """获取 EscalationEngine 单例。"""
    global _escalation_engine
    if _escalation_engine is None:
        _escalation_engine = EscalationEngine()
    return _escalation_engine


def reset_escalation_engine() -> None:
    """重置单例，便于测试隔离。"""
    global _escalation_engine
    _escalation_engine = None


def generate_solution_id() -> str:
    """生成方案记录 ID，前缀 + uuid4 hex 便于识别。"""
    return f"HS-{uuid.uuid4().hex[:12]}"
