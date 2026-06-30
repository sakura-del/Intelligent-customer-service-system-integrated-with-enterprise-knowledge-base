"""调度 Agent（Orchestrator）：多 Agent 架构的大脑。

负责意图识别、任务拆解、路由分发、结果整合与兜底处理，
将用户 query 路由到合适的下游 agent 并汇总最终回复。

调度策略：
- 简单问题（单一意图、高置信）：单轮派发
- 复杂问题（多子任务）：同步遍历模拟并行（后续接 Redis Pub/Sub）
- 情绪优先：检测到情绪敏感意图或用户情绪激动时优先走情绪处理
- 连续 2 轮未解决：自动标记转人工
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from app.agents.knowledge_agent import KnowledgeAgent, get_knowledge_agent
from app.agents.llm_client import LLMClient, get_llm_client
from app.agents.rag_agent import RAGAgent, get_rag_agent
from app.core.logging import get_logger
from app.schemas.orchestrator import (
    Intent,
    IntentResult,
    OrchestratorResult,
    SubTask,
)

logger = get_logger("app.agents.orchestrator")

# 触发转人工的连续失败阈值：达到即升级，避免用户在自动回复里反复兜圈子
ESCALATE_FAILED_THRESHOLD = 2
# 高置信度门槛：超过该值视为简单问题，走单轮派发简化路径
HIGH_CONFIDENCE_THRESHOLD = 0.7
# 情绪激动阈值：emotion_score 超过该值优先走情绪处理
EMOTION_AGGITATED_THRESHOLD = 4

# 关键词规则表：mock 模式与情绪评分共用
# 顺序敏感：先匹配情绪敏感词，避免被业务词抢路由
BUSINESS_KEYWORDS = ("订单", "查订单", "物流", "发货", "会员", "账户", "余额", "积分")
# 退货/退款同时触发业务查询与工单，需在拆解阶段生成两个子任务
TICKET_KEYWORDS = ("退货", "退款", "换货", "退换货", "投诉", "售后")
CHITCHAT_KEYWORDS = ("你好", "您好", "谢谢", "嗨", "在吗", "再见", "早安", "晚安")
# 脏话/激烈词：命中即视为情绪敏感，分值较高
OFFENSIVE_KEYWORDS = ("脏话", "傻逼", "滚", "垃圾", "骗子", "操", "妈的", "草泥马")

# 意图识别系统提示：约束 LLM 只返回 JSON，避免自由发挥导致解析失败
INTENT_SYSTEM_PROMPT = (
    "你是客服意图识别模块。请将用户 query 分类为以下意图之一：\n"
    "knowledge_qa(知识问答)、business_query(业务查询:订单/会员/退换货/账户)、"
    "emotion_sensitive(情绪敏感)、ticket(工单)、chitchat(闲聊/问候)、unknown(无法识别)。\n"
    "仅返回 JSON，不要包含任何解释或 markdown 标记，字段：\n"
    '{"intent": "<意图>", "confidence": <0-1>, '
    '"sub_tasks": [{"agent_name": "<agent>", "input": "<输入>"}], '
    '"need_emotion_check": <true/false>}\n'
    "sub_tasks 中 agent_name 必须是以下之一："
    "knowledge_qa, business_query, emotion_sensitive, ticket, chitchat。"
)

# 占位回复：未注册的 agent 统一返回该文案，便于上层识别"未解决"
PLACEHOLDER_REPLY = "该能力开发中，暂无法处理，建议转人工或稍后重试。"
# 兜底引导语：unknown 意图时给用户的引导
FALLBACK_REPLY = (
    "抱歉，我暂时无法理解您的问题。您可以尝试：\n"
    "1) 描述更具体一些（如订单号、商品名）；\n"
    "2) 直接说「转人工」联系客服。"
)
# 转人工话术
ESCALATE_REPLY = "已为您转接人工客服，请稍候。"


class OrchestratorAgent:
    """调度 Agent。

    维护 agent 注册表与各会话的轮次/失败计数，
    通过 orchestrate 方法串联意图识别→拆解→分发→整合→兜底完整链路。
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        rag_agent: Optional[RAGAgent] = None,
        knowledge_agent: Optional[KnowledgeAgent] = None,
    ) -> None:
        # 延迟取单例：未在构造时即建立 LLM/检索连接，降低启动开销
        self._llm_client = llm_client
        # 保留 RAGAgent 注入入口：旧测试通过注入 fake RAGAgent 控制行为
        # 默认走 KnowledgeAgent（混合检索+重排序），检索质量优于基础 RAG
        self._rag_agent = rag_agent
        self._knowledge_agent = knowledge_agent
        # 会话状态：session_id -> {turn_count, failed_attempts}
        # 进程内字典存储，多实例部署需替换为 Redis
        self._session_states: Dict[str, Dict[str, int]] = {}
        self._agent_registry = self._build_agent_registry()

    @property
    def llm_client(self) -> LLMClient:
        """延迟获取 LLM 客户端单例，便于测试注入。"""
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    @property
    def rag_agent(self) -> RAGAgent:
        """延迟获取 RAGAgent 单例，避免导入阶段触发向量库初始化。"""
        if self._rag_agent is None:
            self._rag_agent = get_rag_agent()
        return self._rag_agent

    @property
    def knowledge_agent(self) -> KnowledgeAgent:
        """延迟获取 KnowledgeAgent 单例。

        用混合检索+重排序替代基础 RAG，提升知识问答召回与排序质量。
        """
        if self._knowledge_agent is None:
            self._knowledge_agent = get_knowledge_agent()
        return self._knowledge_agent

    def _build_agent_registry(self) -> Dict[str, Callable[[str], str]]:
        """构建 agent 路由表。

        knowledge_qa 默认走 KnowledgeAgent（混合检索+重排序），
        若调用方注入了 RAGAgent（测试场景）则改走 RAGAgent 保持兼容；
        其余能力暂用占位 handler，后续各业务 agent 实现后在此注册即可接入。
        """
        return {
            "knowledge_qa": self._handle_knowledge_qa,
            "business_query": self._handle_business_query,
            "emotion_sensitive": self._handle_emotion_sensitive,
            "ticket": self._handle_ticket,
            "chitchat": self._handle_chitchat,
        }

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def orchestrate(
        self,
        query: str,
        session_id: Optional[str] = None,
    ) -> OrchestratorResult:
        """调度主入口：意图识别 → 拆解 → 分发 → 整合 → 兜底。

        返回的 OrchestratorResult 含最终回复、子任务结果与转人工标记，
        调用方据此决定是否继续自动回复或转人工。
        """
        # 1. 会话状态：复用或初始化，便于触发连续失败兜底
        session_id, state = self._get_or_init_session(session_id)
        state["turn_count"] += 1
        turn_count = state["turn_count"]

        # 2. 意图识别：mock 模式走关键词规则，真实模式走 LLM
        intent_result = self._recognize_intent(query)

        # 3. 情绪优先：检测到情绪敏感或情绪激动时强制改走情绪处理
        # 避免用户在愤怒状态下还收到冷冰冰的业务答复，激化矛盾
        emotion_score = self._estimate_emotion_score(query)
        if self._should_prioritize_emotion(intent_result, emotion_score):
            intent_result = self._override_to_emotion(intent_result, query, emotion_score)

        # 4. 任务拆解：意图识别阶段已产出 sub_tasks，为空时按意图补默认子任务
        sub_tasks = self._ensure_sub_tasks(intent_result, query)

        # 5. 路由分发：复杂问题多子任务，本任务用同步遍历模拟并行
        # TODO: 后续接 Redis Pub/Sub 实现真正并行派发，提升复杂问题响应速度
        self._dispatch_subtasks(sub_tasks)

        # 6. 结果整合：汇总各子任务结果为最终回复
        reply = self._aggregate_results(sub_tasks, intent_result)

        # 7. 失败计数与转人工判断
        is_resolved = self._is_resolved(intent_result, sub_tasks)
        self._update_session_state(state, is_resolved)

        # 转接规则引擎：综合用户主动要求/情绪/失败/复杂/VIP 等规则决策
        # 替代纯 failed_attempts 判断，覆盖更多转接场景
        # respect_working_hours=False：工作时间策略由部署层决定，底层调度不阻断
        escalate = self._check_escalation_with_engine(
            query=query,
            session_id=session_id,
            state=state,
            intent_result=intent_result,
        )

        # 转人工时覆盖回复为转接话术，让用户明确感知
        if escalate:
            reply = ESCALATE_REPLY

        logger.info(
            "调度完成：session=%s turn=%d intent=%s confidence=%.2f "
            "subtasks=%d resolved=%s escalate=%s",
            session_id,
            turn_count,
            intent_result.intent.value,
            intent_result.confidence,
            len(sub_tasks),
            is_resolved,
            escalate,
        )

        return OrchestratorResult(
            reply=reply,
            intent=intent_result.intent,
            sub_tasks=sub_tasks,
            escalate_to_human=escalate,
            session_id=session_id,
            turn_count=turn_count,
            failed_attempts=state["failed_attempts"],
            metadata={"emotion_score": emotion_score},
        )

    # ------------------------------------------------------------------
    # 意图识别
    # ------------------------------------------------------------------
    def _recognize_intent(self, query: str) -> IntentResult:
        """意图识别分发：mock 模式走关键词规则，真实模式走 LLM。

        性能优化：闲聊/问候/感谢等高频简单查询先用关键词快通道识别，
        跳过 LLM 调用降低响应时间。未命中关键词再走 LLM。
        LLM 返回解析失败时降级到关键词规则，保证链路不中断。
        """
        # 快通道：闲聊关键词命中直接返回，省一次 LLM 调用
        quick = self._try_quick_intent(query)
        if quick is not None:
            return quick
        if self.llm_client.is_mock:
            return self._keyword_based_intent(query)
        return self._llm_based_intent(query)

    def _try_quick_intent(self, query: str) -> Optional[IntentResult]:
        """关键词快通道：闲聊/转人工等高频意图直接识别，跳过 LLM。

        命中返回 IntentResult，未命中返回 None 走 LLM 路径。
        """
        q = query.strip()
        if not q:
            return None
        # 闲聊：问候/感谢/告别
        if any(w in q for w in CHITCHAT_KEYWORDS):
            return IntentResult(
                intent=Intent.CHITCHAT,
                confidence=0.95,
                sub_tasks=[{
                    "agent_name": "chitchat",
                    "input": query,
                }],
                need_emotion_check=False,
            )
        # 转人工
        if any(w in q for w in ("转人工", "人工客服", "联系人工", "找人工")):
            return IntentResult(
                intent=Intent.TICKET,
                confidence=0.9,
                sub_tasks=[{
                    "agent_name": "ticket",
                    "input": query,
                }],
                need_emotion_check=False,
            )
        return None

    def _llm_based_intent(self, query: str) -> IntentResult:
        """用 LLM 做结构化意图识别。

        要求 LLM 返回 JSON，解析失败时降级到关键词规则，
        避免模型偶发输出格式错误导致整个调度链路崩溃。
        """
        messages = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        raw = self.llm_client.chat(messages, temperature=0.0)
        try:
            data = self._parse_intent_json(raw)
            return self._build_intent_result_from_dict(data, query)
        except (ValueError, KeyError, TypeError) as exc:
            # LLM 返回非 JSON 或字段缺失：降级关键词规则，保证可用
            logger.warning("LLM 意图识别解析失败，降级关键词规则：%s", exc)
            return self._keyword_based_intent(query)

    @staticmethod
    def _parse_intent_json(raw: str) -> Dict[str, Any]:
        """从 LLM 输出中提取 JSON。

        LLM 偶尔会包裹 markdown 代码块或附加说明，
        用正则提取首个 JSON 对象，避免直接 json.loads 失败。
        """
        # 优先尝试直接解析，常见于严格遵循 prompt 的模型
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 退化：提取首个 {...} 块，兼容带 markdown 包裹的输出
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"未在 LLM 输出中找到 JSON：{raw[:120]}")
        return json.loads(match.group(0))

    @staticmethod
    def _build_intent_result_from_dict(
        data: Dict[str, Any], fallback_query: str
    ) -> IntentResult:
        """把 LLM 返回的 dict 映射为 IntentResult。

        sub_tasks 缺失时按意图补一个默认子任务，保证下游分发有输入；
        unknown 意图无对应 handler，兜底用 chitchat agent 避免路由失败。
        """
        intent_str = str(data.get("intent", "unknown"))
        try:
            intent = Intent(intent_str)
        except ValueError:
            # LLM 返回了未知意图枚举值，降级 unknown
            intent = Intent.UNKNOWN

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        raw_sub_tasks = data.get("sub_tasks") or []
        sub_tasks: List[SubTask] = []
        for item in raw_sub_tasks:
            if not isinstance(item, dict):
                continue
            sub_tasks.append(
                SubTask(
                    agent_name=str(item.get("agent_name", intent.value)),
                    input=str(item.get("input", fallback_query)),
                )
            )
        if not sub_tasks:
            # unknown 无注册 handler，兜底用 chitchat 让分发有去处
            default_agent = (
                "chitchat" if intent == Intent.UNKNOWN else intent.value
            )
            sub_tasks.append(
                SubTask(agent_name=default_agent, input=fallback_query)
            )

        return IntentResult(
            intent=intent,
            confidence=confidence,
            sub_tasks=sub_tasks,
            need_emotion_check=bool(data.get("need_emotion_check", False)),
        )

    def _keyword_based_intent(self, query: str) -> IntentResult:
        """关键词规则兜底：mock 模式或 LLM 解析失败时使用。

        按情绪→工单→业务→闲聊→知识问答顺序匹配，
        退货/退款类同时产出 business_query + ticket 两个子任务。
        """
        # 1. 情绪敏感词优先级最高，命中即标记
        if any(word in query for word in OFFENSIVE_KEYWORDS):
            return IntentResult(
                intent=Intent.EMOTION_SENSITIVE,
                confidence=0.9,
                sub_tasks=[
                    SubTask(agent_name="emotion_sensitive", input=query)
                ],
                need_emotion_check=True,
            )

        # 2. 退货/退款：本质属于业务诉求，同时需要工单跟进
        # 因此命中工单关键词时同时视为业务查询，生成双子任务
        is_ticket = any(word in query for word in TICKET_KEYWORDS)
        is_business = any(word in query for word in BUSINESS_KEYWORDS) or is_ticket
        if is_ticket and is_business:
            return IntentResult(
                intent=Intent.BUSINESS_QUERY,
                confidence=0.8,
                sub_tasks=[
                    SubTask(agent_name="business_query", input=query),
                    SubTask(agent_name="ticket", input=query),
                ],
            )
        if is_business:
            return IntentResult(
                intent=Intent.BUSINESS_QUERY,
                confidence=0.8,
                sub_tasks=[
                    SubTask(agent_name="business_query", input=query)
                ],
            )

        # 3. 闲聊/问候
        if any(word in query for word in CHITCHAT_KEYWORDS):
            return IntentResult(
                intent=Intent.CHITCHAT,
                confidence=0.85,
                sub_tasks=[SubTask(agent_name="chitchat", input=query)],
            )

        # 4. 默认走知识问答：让 RAGAgent 检索，命中与否由 hit 字段决定
        return IntentResult(
            intent=Intent.KNOWLEDGE_QA,
            confidence=0.5,
            sub_tasks=[SubTask(agent_name="knowledge_qa", input=query)],
        )

    # ------------------------------------------------------------------
    # 情绪处理
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_emotion_score(query: str) -> int:
        """关键词情绪评分：脏话 +5，工单类投诉词 +3。

        本任务用关键词粗估，后续可接入情感分析模型细化。
        """
        if any(word in query for word in OFFENSIVE_KEYWORDS):
            return 5
        if any(word in query for word in TICKET_KEYWORDS):
            return 3
        return 0

    @staticmethod
    def _should_prioritize_emotion(
        intent_result: IntentResult, emotion_score: int
    ) -> bool:
        """判断是否需要优先走情绪处理。

        意图已识别为情绪敏感，或情绪分超过阈值，都触发优先处理。
        """
        if intent_result.intent == Intent.EMOTION_SENSITIVE:
            return False  # 已是情绪意图，无需再 override
        return emotion_score > EMOTION_AGGITATED_THRESHOLD

    @staticmethod
    def _override_to_emotion(
        intent_result: IntentResult, query: str, emotion_score: int
    ) -> IntentResult:
        """将原意图覆盖为情绪敏感，并在子任务前置情绪处理。

        保留原子任务保证业务诉求不丢失，只是先安抚再处理。
        """
        emotion_task = SubTask(agent_name="emotion_sensitive", input=query)
        # 情绪处理优先，原业务子任务排在后面
        new_sub_tasks = [emotion_task] + list(intent_result.sub_tasks)
        return IntentResult(
            intent=Intent.EMOTION_SENSITIVE,
            confidence=max(intent_result.confidence, 0.9),
            sub_tasks=new_sub_tasks,
            need_emotion_check=True,
        )

    # ------------------------------------------------------------------
    # 任务拆解
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_sub_tasks(intent_result: IntentResult, query: str) -> List[SubTask]:
        """确保子任务列表非空。

        意图识别阶段通常已产出 sub_tasks，unknown 时补一个兜底子任务。
        """
        if intent_result.sub_tasks:
            return list(intent_result.sub_tasks)
        # unknown 兜底：路由到 chitchat handler 给引导语
        return [SubTask(agent_name="chitchat", input=query)]

    # ------------------------------------------------------------------
    # 路由分发
    # ------------------------------------------------------------------
    def _dispatch_subtasks(self, sub_tasks: List[SubTask]) -> None:
        """按 agent_name 分发子任务并回填 result。

        简单问题（单一子任务）直接派发；
        复杂问题（多子任务）同步遍历模拟并行，后续可替换为异步。
        """
        for task in sub_tasks:
            handler = self._agent_registry.get(task.agent_name)
            if handler is None:
                # 未注册的 agent：返回占位文案，便于上层识别"未解决"
                task.result = PLACEHOLDER_REPLY
                logger.warning("未注册的 agent：%s", task.agent_name)
                continue
            try:
                task.result = handler(task.input)
            except Exception as exc:
                # 单个子任务失败不影响其他子任务，整体链路保持可用
                task.result = PLACEHOLDER_REPLY
                logger.warning(
                    "子任务执行失败 agent=%s input=%r err=%s",
                    task.agent_name,
                    task.input[:50],
                    exc,
                )

    # ------------------------------------------------------------------
    # Agent handlers
    # ------------------------------------------------------------------
    def _handle_knowledge_qa(self, query: str) -> str:
        """知识问答：默认调用 KnowledgeAgent（混合检索+重排序+LLM 摘要）。

        若调用方注入了 RAGAgent（多用于测试场景）则改走 RAGAgent，
        保持旧测试用 fake RAGAgent 控制路径的能力。
        """
        # 注入了 RAGAgent 时走旧路径，便于测试隔离
        if self._rag_agent is not None:
            rag_answer = self.rag_agent.answer(question=query)
            if not rag_answer.hit:
                # 未命中知识库：返回标记串，便于上层判定未解决
                return "[未命中知识库]抱歉，知识库中未找到相关内容。"
            return rag_answer.answer

        # 默认路径：KnowledgeAgent 已含混合检索+重排序，开启 LLM 摘要生成最终答复
        knowledge_answer = self.knowledge_agent.answer(
            question=query, generate_summary=True
        )
        if not knowledge_answer.hit:
            return "[未命中知识库]抱歉，知识库中未找到相关内容。"
        # answer 在 generate_summary=True 时非空；空兜底防止 LLM 异常返回空串
        return knowledge_answer.answer or "已找到相关知识，但摘要生成失败。"

    @staticmethod
    def _handle_business_query(query: str) -> str:
        """业务查询：占位实现，后续接入订单/会员等业务 agent。"""
        return PLACEHOLDER_REPLY

    @staticmethod
    def _handle_emotion_sensitive(query: str) -> str:
        """情绪处理：先安抚用户情绪，再引导描述具体问题。"""
        return (
            "非常抱歉给您带来不好的体验，我能理解您的心情。"
            "请您先消消气，方便具体描述一下遇到的问题吗？"
            "我会尽快帮您处理。"
        )

    @staticmethod
    def _handle_ticket(query: str) -> str:
        """工单：占位实现，后续接入工单系统创建工单。"""
        return PLACEHOLDER_REPLY

    @staticmethod
    def _handle_chitchat(query: str) -> str:
        """闲聊：简单回应，保持友好。"""
        if any(word in query for word in ("你好", "您好", "嗨", "在吗")):
            return "您好，很高兴为您服务，请问有什么可以帮您？"
        if any(word in query for word in ("谢谢",)):
            return "不客气，很高兴能帮到您！"
        if any(word in query for word in ("再见", "晚安", "早安")):
            return "感谢您的咨询，祝您生活愉快！"
        return "您好，请问有什么可以帮您？"

    # ------------------------------------------------------------------
    # 结果整合
    # ------------------------------------------------------------------
    def _aggregate_results(
        self,
        sub_tasks: List[SubTask],
        intent_result: IntentResult,
    ) -> str:
        """汇总子任务结果为最终回复。

        单子任务直接返回；多子任务按顺序拼接并标注来源；
        unknown 意图返回兜底引导语。
        """
        if intent_result.intent == Intent.UNKNOWN:
            return FALLBACK_REPLY

        # 过滤掉空结果，避免拼接出多余换行
        results = [task.result for task in sub_tasks if task.result]
        if not results:
            return FALLBACK_REPLY

        if len(results) == 1:
            return results[0]

        # 多子任务：按序号拼接，让用户能区分各部分来源
        parts = [
            f"[{index}] {result}" for index, result in enumerate(results, start=1)
        ]
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 会话状态与兜底
    # ------------------------------------------------------------------
    @staticmethod
    def _is_resolved(
        intent_result: IntentResult, sub_tasks: List[SubTask]
    ) -> bool:
        """判断本轮是否真正解决用户问题。

        unknown 意图、子任务结果为占位文案或未命中知识库，均视为未解决，
        用于累计 failed_attempts 触发转人工。
        """
        if intent_result.intent == Intent.UNKNOWN:
            return False
        for task in sub_tasks:
            if task.result is None:
                return False
            # 占位文案或未命中知识库：能力未就绪或检索失败
            if task.result == PLACEHOLDER_REPLY:
                return False
            if "[未命中知识库]" in task.result:
                return False
        return True

    def _get_or_init_session(
        self, session_id: Optional[str]
    ) -> tuple[str, Dict[str, int]]:
        """获取或初始化会话状态。

        未传 session_id 时生成一个，便于无会话上下文的单次调用也能计数。
        """
        if not session_id:
            import uuid

            session_id = str(uuid.uuid4())
        state = self._session_states.get(session_id)
        if state is None:
            state = {"turn_count": 0, "failed_attempts": 0}
            self._session_states[session_id] = state
        return session_id, state

    @staticmethod
    def _update_session_state(
        state: Dict[str, int], is_resolved: bool
    ) -> None:
        """更新会话状态：解决则清零失败计数，否则累加。"""
        if is_resolved:
            state["failed_attempts"] = 0
        else:
            state["failed_attempts"] += 1

    @staticmethod
    def _check_escalation_with_engine(
        query: str,
        session_id: str,
        state: Dict[str, int],
        intent_result: IntentResult,
    ) -> bool:
        """调用 EscalationEngine 判断是否转人工。

        传入 OrchestratorAgent 自维护的 state（含 failed_attempts/turn_count），
        避免重复查询 session_manager；respect_working_hours=False 让底层调度
        不受工作时间阻断（工作时间策略由部署层/API 层决定）。
        引擎不可用时降级到 failed_attempts 阈值判断，保证链路可用。
        """
        try:
            from app.agents.escalation import get_escalation_engine

            engine = get_escalation_engine()
            decision = engine.check_escalation(
                query=query,
                session_id=session_id,
                intent_result=intent_result,
                session_state=state,
                respect_working_hours=False,
            )
            return decision.should_escalate
        except Exception as exc:
            # 引擎异常时降级到纯 failed_attempts 判断，保证主链路不中断
            logger.warning(
                "EscalationEngine 调用失败，降级 failed_attempts 判断：%s", exc
            )
            return state.get("failed_attempts", 0) >= ESCALATE_FAILED_THRESHOLD

    # ------------------------------------------------------------------
    # 测试与调试辅助
    # ------------------------------------------------------------------
    def reset_session(self, session_id: str) -> None:
        """重置指定会话状态，便于测试隔离。"""
        self._session_states.pop(session_id, None)

    def reset_all_sessions(self) -> None:
        """清空所有会话状态。"""
        self._session_states.clear()


# 模块级单例：调度器本身无状态（会话状态在实例内），进程内复用
_orchestrator: Optional[OrchestratorAgent] = None


def get_orchestrator() -> OrchestratorAgent:
    """获取 OrchestratorAgent 单例。"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = OrchestratorAgent()
    return _orchestrator


def reset_orchestrator() -> None:
    """重置单例，便于测试切换配置。"""
    global _orchestrator
    _orchestrator = None
