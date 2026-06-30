"""基于 LangGraph 的多 Agent 编排状态机。

将"意图识别 → 路由分发 → Agent 执行 → 对话润色"组装成一张有向图：
- intent_node：复用 OrchestratorAgent 做意图识别与拆解
- route_node：根据意图/情绪决定下一步走向
- agent_nodes：各业务 Agent 实际执行（knowledge/business/emotion/ticket/chitchat）
- dialog_node：DialogAgent 对最终回复做润色
- escalate_node：触发转人工兜底

可用性保障：LangGraph 不可用或编排构建失败时，自动降级到同步编排器
（SynchOrchestrator），复用 OrchestratorAgent + DialogAgent 串联，
保证端到端链路始终可用。

并行优化：复杂问题多子任务在 agent_node 内通过 ThreadPoolExecutor 并行执行。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, TypedDict

from app.agents.dialog_agent import get_dialog_agent
from app.agents.knowledge_agent import get_knowledge_agent
from app.agents.orchestrator import (
    ESCALATE_FAILED_THRESHOLD,
    get_orchestrator,
)
from app.agents.rag_agent import get_rag_agent
from app.core.context_manager import (
    get_context_manager,
    get_intent_detector,
)
from app.core.logging import get_logger
from app.core.monitor import get_monitor
from app.core.performance import get_hot_query_cache
from app.core.session import session_manager
from app.schemas.dialog import DialogContext
from app.schemas.orchestrator import Intent, IntentResult

logger = get_logger("app.agents.graph")

# 复杂问题并行执行子任务的线程池大小
# 取 4 兼顾并发与资源占用；IO 密集场景可调大
_AGENT_PARALLEL_WORKERS = 4

# LangGraph 节点名常量，统一管理避免拼写错误
_NODE_INTENT = "intent"
_NODE_ROUTE = "route"
_NODE_AGENT = "agent"
_NODE_DIALOG = "dialog"
_NODE_ESCALATE = "escalate"


class AgentState(TypedDict, total=False):
    """LangGraph 共享状态。

    total=False 让所有字段可选，便于各节点局部更新；
    实际使用时由入口初始化核心字段，节点按需补充。

    字段说明：
    - session_id / user_id：会话与用户标识
    - message：用户本轮输入
    - intent：识别到的意图（Intent 枚举字符串）
    - sub_tasks：拆解出的子任务列表
    - emotion_score：情绪分数（0-1，越低越负面）
    - turn_count / failed_attempts：调度状态计数
    - history：对话历史，供 DialogAgent 保持上下文
    - raw_results：各 agent 的原始输出，按 agent_name 索引
    - final_reply：最终给用户的回复
    - sources：引用来源列表
    - escalate_to_human：是否转人工
    - escalation_card：转接时生成的人工上下文卡片（dict 形式）
    """

    session_id: Optional[str]
    user_id: Optional[str]
    message: str
    intent: str
    sub_tasks: List[Dict[str, Any]]
    emotion_score: float
    turn_count: int
    failed_attempts: int
    history: List[Dict[str, Any]]
    raw_results: Dict[str, Any]
    final_reply: str
    sources: List[str]
    escalate_to_human: bool
    # 转接上下文卡片：escalate_node 生成，供人工客服接手参考
    # 以 dict 形式存储便于序列化与 API 响应直接展开
    escalation_card: Optional[Dict[str, Any]]
    # 监控追踪 ID：run_graph 入口生成，各节点埋点时回传给 Monitor
    # 测试中直接调用节点函数时可缺失，埋点自动跳过
    trace_id: Optional[str]
    # 分层摘要上下文文本（Task 14）：run_graph 入口由 ContextManager 生成
    # 供 dialog_node 注入 DialogContext.layered_summary，降低 token 消耗
    layered_context_text: Optional[str]
    # 意图切换检测结果（Task 14）：intent_node 前置检测后写入
    # 便于后续节点引用与日志追踪
    intent_switch: Optional[Dict[str, Any]]


# ----------------------------------------------------------------------
# Agent 执行器：把不同意图路由到对应 Agent
# 每个执行器签名统一为 (query, state) -> dict，返回 {result, sources} 等
# ----------------------------------------------------------------------

def _execute_knowledge(query: str, state: AgentState) -> Dict[str, Any]:
    """知识问答：调用 KnowledgeAgent（混合检索+重排序+LLM 摘要）。

    若 KnowledgeAgent 不可用或抛异常，降级到 RAGAgent 保证链路可用。
    """
    knowledge_agent = get_knowledge_agent()
    try:
        answer = knowledge_agent.answer(
            question=query,
            session_id=state.get("session_id"),
            generate_summary=True,
        )
        if answer.hit:
            return {
                "result": answer.answer or "已找到相关知识，但摘要生成失败。",
                "sources": list(answer.sources),
                "hit": True,
            }
        return {
            "result": "[未命中知识库]抱歉，知识库中未找到相关内容。",
            "sources": [],
            "hit": False,
        }
    except Exception as exc:
        # KnowledgeAgent 异常时降级到 RAGAgent，避免单点失败拖垮整条链路
        logger.warning(
            "KnowledgeAgent 执行失败，降级到 RAGAgent：%s", exc
        )
        rag_agent = get_rag_agent()
        rag_answer = rag_agent.answer(question=query, session_id=state.get("session_id"))
        if rag_answer.hit:
            return {
                "result": rag_answer.answer,
                "sources": list(rag_answer.sources),
                "hit": True,
            }
        return {
            "result": "[未命中知识库]抱歉，知识库中未找到相关内容。",
            "sources": [],
            "hit": False,
        }


def _execute_business(query: str, state: AgentState) -> Dict[str, Any]:
    """业务查询：占位实现，后续接入订单/会员等业务 agent。"""
    return {"result": "该能力开发中，暂无法处理，建议转人工或稍后重试。",
            "sources": [], "hit": False}


def _execute_emotion(query: str, state: AgentState) -> Dict[str, Any]:
    """情绪处理：先安抚用户情绪，再引导描述具体问题。"""
    return {
        "result": (
            "非常抱歉给您带来不好的体验，我能理解您的心情。"
            "请您先消消气，方便具体描述一下遇到的问题吗？"
            "我会尽快帮您处理。"
        ),
        "sources": [],
        "hit": True,
    }


def _execute_ticket(query: str, state: AgentState) -> Dict[str, Any]:
    """工单：占位实现，后续接入工单系统创建工单。"""
    return {"result": "该能力开发中，暂无法处理，建议转人工或稍后重试。",
            "sources": [], "hit": False}


def _execute_chitchat(query: str, state: AgentState) -> Dict[str, Any]:
    """闲聊：简单友好回应。"""
    if any(word in query for word in ("你好", "您好", "嗨", "在吗")):
        reply = "您好，很高兴为您服务，请问有什么可以帮您？"
    elif any(word in query for word in ("谢谢",)):
        reply = "不客气，很高兴能帮到您！"
    elif any(word in query for word in ("再见", "晚安", "早安")):
        reply = "感谢您的咨询，祝您生活愉快！"
    else:
        reply = "您好，请问有什么可以帮您？"
    return {"result": reply, "sources": [], "hit": True}


# agent_name -> 执行器映射，route_node 据此分发
_AGENT_EXECUTORS: Dict[str, Callable[[str, AgentState], Dict[str, Any]]] = {
    "knowledge_qa": _execute_knowledge,
    "business_query": _execute_business,
    "emotion_sensitive": _execute_emotion,
    "ticket": _execute_ticket,
    "chitchat": _execute_chitchat,
}


# ----------------------------------------------------------------------
# 节点实现
# ----------------------------------------------------------------------

def _record_step_safe(
    trace_id: Optional[str],
    node: str,
    input_summary: Any,
    output_summary: Any,
    duration_ms: float,
) -> None:
    """安全记录节点步骤：任何异常都不影响主链路。

    监控埋点失败时仅记录日志，避免观测系统故障拖垮业务链路。
    trace_id 缺失（如测试直接调用节点）时静默跳过。
    """
    if not trace_id:
        return
    try:
        get_monitor().record_step(
            trace_id, node, input_summary, output_summary, duration_ms
        )
    except Exception as exc:
        logger.warning("监控埋点失败 node=%s err=%s", node, exc)


def _record_agent_call_safe(
    trace_id: Optional[str],
    agent_name: str,
    input_text: Any,
    output_text: Any,
    duration_ms: float,
    success: bool,
) -> None:
    """安全记录 agent 调用：失败时仅记录日志，不中断主链路。"""
    if not trace_id:
        return
    try:
        get_monitor().record_agent_call(
            trace_id, agent_name, input_text, output_text, duration_ms, success
        )
    except Exception as exc:
        logger.warning("监控 agent 调用埋点失败 name=%s err=%s", agent_name, exc)


def intent_node(state: AgentState) -> AgentState:
    """意图识别节点：复用 OrchestratorAgent 的意图识别能力。

    不直接调用 orchestrate()，而是复用其内部的 _recognize_intent / _estimate_emotion_score，
    避免重复执行分发与整合逻辑（那些会在后续节点做）。

    Task 14：在意图识别前先做"意图切换检测"，若检测到切换则重置 slots，
    让新意图不受旧槽位污染；保留 history 用于回溯。
    """
    trace_id = state.get("trace_id")
    start = time.perf_counter()
    orchestrator = get_orchestrator()
    message = state.get("message", "")
    session_id = state.get("session_id")

    # === 前置：意图切换检测 ===
    # 在意图识别前检查是否切换话题，切换则重置槽位避免旧数据污染新意图
    switch_result_dict: Optional[Dict[str, Any]] = None
    if session_id:
        session = session_manager.get_session(session_id) or {}
        current_intent = session.get("current_intent")
        # 首轮对话 current_intent 为空，跳过切换检测避免误判
        if current_intent:
            try:
                detector = get_intent_detector()
                switch_result = detector.detect_switch(
                    message, session_id, current_intent
                )
                switch_result_dict = switch_result.model_dump()
                if switch_result.switched:
                    # 切换：重置槽位，保留 history 用于回溯
                    session_manager.reset_slots(session_id)
                    logger.info(
                        "检测到意图切换 session=%s old=%s new=%s reason=%s",
                        session_id,
                        current_intent,
                        switch_result.new_intent,
                        switch_result.reason,
                    )
            except Exception as exc:
                # 切换检测失败不影响主链路，按正常意图识别继续
                logger.warning("意图切换检测失败，跳过：%s", exc)

    intent_result: IntentResult = orchestrator._recognize_intent(message)
    emotion_score = orchestrator._estimate_emotion_score(message)

    # 情绪优先：和 OrchestratorAgent 保持一致策略，避免 graph 与单 agent 行为分裂
    if orchestrator._should_prioritize_emotion(intent_result, emotion_score):
        intent_result = orchestrator._override_to_emotion(
            intent_result, message, emotion_score
        )

    sub_tasks = orchestrator._ensure_sub_tasks(intent_result, message)

    state["intent"] = intent_result.intent.value
    state["sub_tasks"] = [task.model_dump() for task in sub_tasks]
    # DialogContext 约定：emotion_score 在 [0,1]，越低越负面
    # OrchestratorAgent 原始打分是 0-5 整数，越大越激烈，需反转并归一化
    state["emotion_score"] = max(0.0, min(1.0, 1.0 - emotion_score / 5.0))
    # 切换检测结果写入 state，便于后续节点引用与日志追踪
    if switch_result_dict is not None:
        state["intent_switch"] = switch_result_dict

    _record_step_safe(
        trace_id,
        "intent",
        message,
        f"intent={intent_result.intent.value}, emotion={state['emotion_score']:.2f}, "
        f"subtasks={len(sub_tasks)}",
        (time.perf_counter() - start) * 1000.0,
    )
    return state


def route_node(state: AgentState) -> AgentState:
    """路由节点：根据情绪/失败计数决定走向。

    纯决策节点，不修改 state 内容，仅作为 conditional edge 的锚点。
    实际分流由 _route_after_route 实现。
    """
    trace_id = state.get("trace_id")
    start = time.perf_counter()
    # 记录路由决策依据，便于排查"为何走了 escalate"等问题
    decision = _route_after_route(state)
    _record_step_safe(
        trace_id,
        "route",
        f"intent={state.get('intent')}, failed={state.get('failed_attempts', 0)}",
        f"next={decision}",
        (time.perf_counter() - start) * 1000.0,
    )
    return state


def _route_after_route(state: AgentState) -> str:
    """条件边：决定 route 之后去 agent 还是 escalate。

    转接触发条件（按优先级）：
    1. 用户主动要求转人工（"转人工"/"找客服"等关键词）→ 最高优先级
    2. 情绪敏感意图直接转人工（避免激化矛盾）
    3. 连续失败达阈值也直接转人工
    用户主动要求检查复用 EscalationEngine 的关键词规则，避免逻辑分裂。
    """
    # 用户主动要求转人工：最高优先级，无视其他条件
    message = state.get("message", "")
    if _is_user_request_human(message):
        return _NODE_ESCALATE

    intent = state.get("intent", Intent.UNKNOWN.value)
    failed = state.get("failed_attempts", 0)
    if intent == Intent.EMOTION_SENSITIVE.value:
        return _NODE_ESCALATE
    if failed >= ESCALATE_FAILED_THRESHOLD:
        return _NODE_ESCALATE
    return _NODE_AGENT


def _is_user_request_human(message: str) -> bool:
    """判断用户是否主动要求转人工。

    复用 EscalationEngine 的关键词规则，避免在 graph 中重复维护关键词表。
    引擎不可用时降级到内置关键词列表，保证路由可用。
    """
    if not message:
        return False
    try:
        from app.agents.escalation import HUMAN_REQUEST_KEYWORDS

        return any(keyword in message for keyword in HUMAN_REQUEST_KEYWORDS)
    except Exception:
        # 引擎模块不可用时降级到内置关键词，保证路由不中断
        builtin_keywords = ("转人工", "找客服", "人工客服", "人工服务")
        return any(keyword in message for keyword in builtin_keywords)


def agent_node(state: AgentState) -> AgentState:
    """Agent 执行节点：并行执行各子任务。

    复杂问题（多子任务）用线程池并行执行，串行结果整合；
    单子任务直接同步调用，省去线程池开销。
    执行后根据结果更新 failed_attempts，供后续条件边判断是否转人工。
    """
    sub_tasks = state.get("sub_tasks", [])
    raw_results: Dict[str, Any] = {}
    all_sources: List[str] = []

    tasks = [
        (task.get("agent_name", ""), task.get("input", state.get("message", "")))
        for task in sub_tasks
    ]

    if len(tasks) <= 1:
        # 单子任务直接同步执行，避免线程池开销
        for agent_name, task_input in tasks:
            result = _dispatch_to_agent(agent_name, task_input, state)
            raw_results[agent_name] = result
            all_sources.extend(result.get("sources", []))
    else:
        # 多子任务并行执行：IO 密集场景显著降低总延迟
        with ThreadPoolExecutor(max_workers=_AGENT_PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(_dispatch_to_agent, name, inp, state): name
                for name, inp in tasks
            }
            for future in futures:
                agent_name = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    # 单个子任务失败不影响其他，整体保持可用
                    logger.warning("子任务执行失败 agent=%s err=%s", agent_name, exc)
                    result = {"result": "该能力开发中，暂无法处理。", "sources": []}
                raw_results[agent_name] = result
                all_sources.extend(result.get("sources", []))

    state["raw_results"] = raw_results
    # 来源去重，避免重复展示
    state["sources"] = list(dict.fromkeys(all_sources))

    # 更新失败计数：本轮是否解决，决定下一步是否转人工
    # 与 OrchestratorAgent._is_resolved 保持一致语义
    is_resolved = _is_round_resolved(state)
    if is_resolved:
        state["failed_attempts"] = 0
    else:
        state["failed_attempts"] = int(state.get("failed_attempts", 0)) + 1
    return state


def _is_round_resolved(state: AgentState) -> bool:
    """判断本轮是否真正解决用户问题。

    任一子任务返回占位文案或未命中标记，均视为未解决，
    用于累计 failed_attempts 触发转人工。
    """
    raw_results = state.get("raw_results", {})
    if not raw_results:
        return False
    for result_data in raw_results.values():
        if not isinstance(result_data, dict):
            return False
        text = result_data.get("result", "")
        if not text:
            return False
        if "开发中" in text or "[未命中知识库]" in text:
            return False
    return True


def _route_after_agent(state: AgentState) -> str:
    """条件边：agent 执行后根据失败计数与意图决定走向。

    累计失败达阈值则转人工。
    chitchat/ticket/business_query 意图已有完整回复，跳过 DialogAgent 润色
    直接输出，省一次 LLM 调用降低响应时间。
    """
    failed = state.get("failed_attempts", 0)
    if failed >= ESCALATE_FAILED_THRESHOLD:
        return _NODE_ESCALATE
    # 非知识问答意图的回复已是完整话术，无需 LLM 润色
    intent = state.get("intent", "")
    if intent in (
        Intent.CHITCHAT.value,
        Intent.TICKET.value,
        Intent.BUSINESS_QUERY.value,
    ):
        return _NODE_DIALOG  # 仍走 dialog 但用 raw_reply 直通
    return _NODE_DIALOG


def _dispatch_to_agent(
    agent_name: str, query: str, state: AgentState
) -> Dict[str, Any]:
    """根据 agent_name 调用对应执行器。

    未注册的 agent 返回占位结果，避免 KeyError 中断流程。
    同时记录 agent 调用埋点（输入、输出、耗时、是否成功），便于监控面板统计。
    """
    trace_id = state.get("trace_id")
    start = time.perf_counter()
    executor = _AGENT_EXECUTORS.get(agent_name)
    if executor is None:
        logger.warning("未注册的 agent：%s", agent_name)
        result = {"result": "该能力开发中，暂无法处理。", "sources": []}
        _record_agent_call_safe(
            trace_id, agent_name, query, result["result"],
            (time.perf_counter() - start) * 1000.0, success=False,
        )
        # 节点级 step 也记录一次，便于 route_path 包含 agent_name
        _record_step_safe(
            trace_id, agent_name, query, result["result"],
            (time.perf_counter() - start) * 1000.0,
        )
        return result
    try:
        result = executor(query, state)
        # 成功与否由 result 内容判断：占位文案或未命中标记视为未解决
        success = bool(result.get("result")) and "开发中" not in result.get("result", "")
        _record_agent_call_safe(
            trace_id, agent_name, query, result.get("result", ""),
            (time.perf_counter() - start) * 1000.0, success=success,
        )
        _record_step_safe(
            trace_id, agent_name, query, result.get("result", ""),
            (time.perf_counter() - start) * 1000.0,
        )
        return result
    except Exception as exc:
        # 兜底：单个 agent 异常返回占位，不抛出避免拖垮整体
        logger.warning("agent 执行异常 name=%s err=%s", agent_name, exc)
        _record_agent_call_safe(
            trace_id, agent_name, query, f"异常：{exc}",
            (time.perf_counter() - start) * 1000.0, success=False,
        )
        _record_step_safe(
            trace_id, agent_name, query, f"异常：{exc}",
            (time.perf_counter() - start) * 1000.0,
        )
        return {"result": "该能力开发中，暂无法处理。", "sources": []}


def dialog_node(state: AgentState) -> AgentState:
    """对话润色节点：用 DialogAgent 把原始结果整合并润色。

    先把 raw_results 按子任务顺序拼接为 raw_reply，
    再交给 DialogAgent.generate 做风格统一与来源标注。

    性能优化：chitchat/ticket/business_query 意图的回复已是完整话术，
    跳过 LLM 润色直接使用 raw_reply，省一次 LLM 调用降低响应时间。

    Task 14：把 ContextManager 生成的分层摘要文本注入 DialogContext，
    让 LLM 模式下的 prompt 用精炼上下文替代完整 history，降低 token 消耗。
    """
    trace_id = state.get("trace_id")
    start = time.perf_counter()
    raw_reply = _aggregate_raw_results(state)
    sources = state.get("sources", [])

    # 性能优化：非知识问答意图跳过 LLM 润色，直接使用 raw_reply
    intent = state.get("intent", "")
    if intent in (
        Intent.CHITCHAT.value,
        Intent.TICKET.value,
        Intent.BUSINESS_QUERY.value,
        Intent.EMOTION_SENSITIVE.value,
    ):
        state["final_reply"] = raw_reply
        _record_step_safe(
            trace_id, "dialog", raw_reply, raw_reply,
            (time.perf_counter() - start) * 1000.0,
        )
        return state

    dialog_agent = get_dialog_agent()
    # 分层摘要优先：run_graph 入口已生成，直接复用避免重复计算
    layered_summary = state.get("layered_context_text")
    context = DialogContext(
        session_id=state.get("session_id"),
        user_id=state.get("user_id"),
        history=state.get("history", []),
        current_intent=state.get("intent"),
        emotion_score=state.get("emotion_score"),
        raw_answer=raw_reply,
        sources=list(sources),
        layered_summary=layered_summary,
    )
    try:
        dialog_result = dialog_agent.generate(
            raw_answer=raw_reply, sources=list(sources), context=context
        )
        final_reply = dialog_result.reply
    except Exception as exc:
        # 润色失败时直接用 raw_reply 兜底，保证有回复
        logger.warning("DialogAgent 润色失败，使用原始回复：%s", exc)
        final_reply = raw_reply

    state["final_reply"] = final_reply
    _record_step_safe(
        trace_id, "dialog", raw_reply, final_reply,
        (time.perf_counter() - start) * 1000.0,
    )
    return state


def _aggregate_raw_results(state: AgentState) -> str:
    """把 raw_results 按子任务顺序拼接为 raw_reply。

    单子任务直接返回；多子任务按序号标注，便于用户区分来源。
    """
    sub_tasks = state.get("sub_tasks", [])
    raw_results = state.get("raw_results", {})

    parts: List[str] = []
    for task in sub_tasks:
        agent_name = task.get("agent_name", "")
        result_data = raw_results.get(agent_name, {})
        text = result_data.get("result", "") if isinstance(result_data, dict) else ""
        if text:
            parts.append(text)

    if not parts:
        return "抱歉，我暂时无法理解您的问题。"

    if len(parts) == 1:
        return parts[0]
    # 多子任务按序号标注
    return "\n".join(f"[{i}] {p}" for i, p in enumerate(parts, start=1))


def escalate_node(state: AgentState) -> AgentState:
    """转人工节点：标记 escalate、生成上下文卡片、写入转接话术。

    卡片生成失败时降级为只标记 escalate，保证主链路不被卡片逻辑阻断。
    """
    trace_id = state.get("trace_id")
    start = time.perf_counter()
    state["escalate_to_human"] = True
    state["final_reply"] = "已为您转接人工客服，请稍候。"

    # 生成转接上下文卡片，让人工客服接手前快速了解会话上下文
    # 失败时仅记录日志，不影响转接主流程
    state["escalation_card"] = _build_escalation_card_safe(state)

    _record_step_safe(
        trace_id,
        "escalate",
        f"intent={state.get('intent')}, failed={state.get('failed_attempts', 0)}",
        state["final_reply"],
        (time.perf_counter() - start) * 1000.0,
    )
    return state


def _build_escalation_card_safe(state: AgentState) -> Optional[Dict[str, Any]]:
    """安全生成转接上下文卡片：失败时返回 None 不阻断主链路。

    复用 EscalationEngine.build_card，传入 state 中的会话信息，
    原因根据 intent 与 failed_attempts 推导，便于人工理解转接依据。
    """
    try:
        from app.agents.escalation import get_escalation_engine

        session_id = state.get("session_id") or ""
        if not session_id:
            return None
        engine = get_escalation_engine()
        # 推导转接原因：让卡片字段对人工更有意义
        reason = _derive_escalation_reason(state)
        card = engine.build_card(session_id=session_id, reason=reason)
        return card.model_dump()
    except Exception as exc:
        logger.warning("生成转接上下文卡片失败：%s", exc)
        return None


def _derive_escalation_reason(state: AgentState) -> str:
    """根据 AgentState 推导转接原因，供卡片展示。

    优先级：情绪敏感意图 > 连续失败 > 默认话术，
    让人工客服能从卡片快速理解转接依据。
    """
    intent = state.get("intent", "")
    failed = int(state.get("failed_attempts", 0))
    if intent == "emotion_sensitive":
        return "用户情绪敏感，需人工安抚"
    if failed >= 2:
        return f"连续 {failed} 轮未解决"
    return "智能客服无法处理，转人工跟进"


# ----------------------------------------------------------------------
# LangGraph 构建与运行
# ----------------------------------------------------------------------

def _build_lang_graph():
    """构建 LangGraph 状态机。

    返回编译后的 graph，invoke(state) 即可跑完整链路。
    构建失败时抛异常，由上层捕获并降级到同步编排器。
    """
    from langgraph.graph import END, StateGraph

    graph = StateGraph(AgentState)

    # 添加节点
    graph.add_node(_NODE_INTENT, intent_node)
    graph.add_node(_NODE_ROUTE, route_node)
    graph.add_node(_NODE_AGENT, agent_node)
    graph.add_node(_NODE_DIALOG, dialog_node)
    graph.add_node(_NODE_ESCALATE, escalate_node)

    # 设置入口
    graph.set_entry_point(_NODE_INTENT)

    # 添加边
    graph.add_edge(_NODE_INTENT, _NODE_ROUTE)
    # 条件边 1：route 之后根据情绪/失败计数决定走向
    graph.add_conditional_edges(
        _NODE_ROUTE,
        _route_after_route,
        {
            _NODE_AGENT: _NODE_AGENT,
            _NODE_ESCALATE: _NODE_ESCALATE,
        },
    )
    # 条件边 2：agent 执行后根据本轮失败计数决定走向
    # 累计失败达阈值则转人工，否则进入润色节点
    graph.add_conditional_edges(
        _NODE_AGENT,
        _route_after_agent,
        {
            _NODE_DIALOG: _NODE_DIALOG,
            _NODE_ESCALATE: _NODE_ESCALATE,
        },
    )
    graph.add_edge(_NODE_DIALOG, END)
    graph.add_edge(_NODE_ESCALATE, END)

    return graph.compile()


# ----------------------------------------------------------------------
# 同步编排器（fallback）
# ----------------------------------------------------------------------

class _SynchOrchestrator:
    """同步编排器：LangGraph 不可用时的 fallback。

    直接复用 graph 节点函数（intent_node / agent_node / dialog_node / escalate_node）
    按顺序调用，行为与 LangGraph 版本完全一致，只是没有图结构调度。
    这样保证 fallback 路径下的 failed_attempts 累计、转人工逻辑与主路径一致。
    """

    def run(self, initial_state: AgentState) -> AgentState:
        """同步执行编排链路，返回最终 AgentState。"""
        state = initial_state

        # 1. 意图识别
        state = intent_node(state)

        # 2. 路由决策（不修改 state，仅判断走向）
        state = route_node(state)
        next_node = _route_after_route(state)

        # 3. 转人工或执行 agent
        if next_node == _NODE_ESCALATE:
            return escalate_node(state)

        state = agent_node(state)

        # 4. agent 后根据失败计数决定走向
        next_node = _route_after_agent(state)
        if next_node == _NODE_ESCALATE:
            return escalate_node(state)

        # 5. 对话润色
        return dialog_node(state)


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------

# 模块级缓存：编译后的 graph 与 fallback 实例
_compiled_graph = None
_graph_init_error: Optional[Exception] = None
_synch_orchestrator: Optional[_SynchOrchestrator] = None


def _get_compiled_graph():
    """惰性获取编译后的 LangGraph 实例。

    首次调用时尝试构建；失败则记录错误并返回 None，
    后续调用直接复用结果，避免每次都重试。
    """
    global _compiled_graph, _graph_init_error
    if _compiled_graph is not None:
        return _compiled_graph
    if _graph_init_error is not None:
        # 之前已失败，不再重试，避免每次调用都触发异常
        return None
    try:
        _compiled_graph = _build_lang_graph()
        logger.info("LangGraph 编排器已构建成功")
        return _compiled_graph
    except Exception as exc:
        # LangGraph 不可用或构建失败：降级到同步编排器
        _graph_init_error = exc
        logger.warning(
            "LangGraph 构建失败，降级到同步编排器：%s", exc
        )
        return None


def _get_synch_orchestrator() -> _SynchOrchestrator:
    """获取同步编排器单例（fallback 路径）。"""
    global _synch_orchestrator
    if _synch_orchestrator is None:
        _synch_orchestrator = _SynchOrchestrator()
    return _synch_orchestrator


def run_graph(
    message: str, session_id: Optional[str] = None
) -> AgentState:
    """运行多 Agent 编排，返回最终 AgentState。

    主路径：LangGraph 状态机；
    fallback：同步编排器（OrchestratorAgent + DialogAgent）。

    会话状态：在执行前后更新 session_manager 中的 turn_count/failed_attempts/history，
    保证多轮对话上下文连续。

    监控：入口启动 trace，各节点埋点回写步骤，出口 finish_trace 收尾；
    监控异常不影响业务链路（埋点函数已做容错）。

    性能优化：入口检查 HotQueryCache，命中直接返回缓存结果（跳过全部 LLM 调用）；
    出口将成功结果写入缓存，后续相同查询命中即返回。
    """
    # 会话管理：复用或创建 session，并记录用户消息
    effective_session_id = session_manager.get_or_create(
        session_id=session_id, channel="api", user_id=None
    )

    # 热点缓存检查：命中则跳过全部编排，直接返回缓存结果
    try:
        cache = get_hot_query_cache()
        cached = cache.get(message, session_context=None)
        if cached is not None:
            logger.info("热点缓存命中，跳过编排：query=%r", message[:50])
            session_manager.increment_turn(effective_session_id)
            session_manager.append_history(effective_session_id, "user", message)
            session_manager.append_history(effective_session_id, "assistant", cached.answer)
            return {
                "session_id": effective_session_id,
                "user_id": None,
                "message": message,
                "intent": "cached",
                "sub_tasks": [],
                "emotion_score": 0.0,
                "turn_count": 1,
                "failed_attempts": 0,
                "history": [],
                "raw_results": {},
                "final_reply": cached.answer,
                "sources": list(cached.sources) if cached.sources else [],
                "escalate_to_human": False,
                "escalation_card": None,
                "trace_id": None,
                "layered_context_text": "",
                "intent_switch": None,
            }
    except Exception as exc:
        logger.warning("热点缓存读取失败，降级到正常编排：%s", exc)

    session_manager.increment_turn(effective_session_id)
    session_manager.append_history(effective_session_id, "user", message)

    # 取会话历史快照，供 DialogAgent 使用
    session = session_manager.get_session(effective_session_id) or {}
    history = list(session.get("history", []))
    failed_attempts = int(session.get("failed_attempts", 0))

    # Task 14：构造分层摘要上下文，供 dialog_node 注入 DialogContext
    # 在入口处一次性生成，节点直接复用，避免重复 LLM/规则摘要调用
    layered_context_text = ""
    try:
        layered_context = get_context_manager().build_context(
            effective_session_id
        )
        layered_context_text = layered_context.full_context_text
    except Exception as exc:
        # 分层上下文生成失败不影响主链路，dialog_node 仍可走原 history 路径
        logger.warning("分层上下文生成失败，降级到原 history：%s", exc)
        layered_context_text = ""

    # 启动监控 trace：trace_id 透传到各节点，便于全链路追踪
    try:
        trace_id = get_monitor().start_trace(effective_session_id, message)
    except Exception as exc:
        logger.warning("启动监控 trace 失败，本次不埋点：%s", exc)
        trace_id = None

    # 构造初始 state
    initial_state: AgentState = {
        "session_id": effective_session_id,
        "user_id": session.get("user_id"),
        "message": message,
        "intent": Intent.UNKNOWN.value,
        "sub_tasks": [],
        "emotion_score": 0.0,
        "turn_count": int(session.get("turn_count", 1)),
        "failed_attempts": failed_attempts,
        "history": history,
        "raw_results": {},
        "final_reply": "",
        "sources": [],
        "escalate_to_human": False,
        "escalation_card": None,
        "trace_id": trace_id,
        # Task 14：分层摘要上下文与意图切换结果
        "layered_context_text": layered_context_text,
        "intent_switch": None,
    }

    # 优先 LangGraph，失败则同步编排
    compiled = _get_compiled_graph()
    try:
        if compiled is not None:
            try:
                final_state = compiled.invoke(initial_state)
            except Exception as exc:
                # LangGraph 运行时异常：降级同步编排，保证可用
                logger.warning(
                    "LangGraph 运行失败，降级到同步编排器：%s", exc
                )
                final_state = _get_synch_orchestrator().run(initial_state)
        else:
            final_state = _get_synch_orchestrator().run(initial_state)
    except Exception as exc:
        # 整条链路异常：标记 trace 失败后重新抛出，由上层兜底
        if trace_id:
            try:
                get_monitor().fail_trace(trace_id, str(exc))
            except Exception:
                pass
        raise

    # 把最终状态同步回 session_manager：意图、情绪、历史、失败计数
    _sync_state_to_session(effective_session_id, final_state)

    # 热点缓存写入：仅缓存知识问答类成功结果，避免转人工/未命中污染缓存
    # 写入失败不影响主链路，仅记录日志
    try:
        cache = get_hot_query_cache()
        final_reply = final_state.get("final_reply", "")
        intent_value = final_state.get("intent", "")
        escalate = final_state.get("escalate_to_human", False)
        # 仅缓存：知识问答 + 有回复 + 未转人工 + 未命中标记的兜底文案
        is_cacheable = (
            intent_value == Intent.KNOWLEDGE_QA.value
            and bool(final_reply)
            and not escalate
            and "[未命中知识库]" not in final_reply
        )
        if is_cacheable:
            cache.set(
                message,
                final_reply,
                sources=final_state.get("sources", []),
                session_context=None,
            )
    except Exception as exc:
        logger.warning("热点缓存写入失败，不影响主链路：%s", exc)

    # 完成 trace：写入终态字段，供监控面板查询
    if trace_id:
        try:
            get_monitor().finish_trace(
                trace_id,
                intent=final_state.get("intent", ""),
                final_reply=final_state.get("final_reply", ""),
                escalate_to_human=final_state.get("escalate_to_human", False),
                turn_count=final_state.get("turn_count", 0),
                failed_attempts=final_state.get("failed_attempts", 0),
                status="success",
            )
        except Exception as exc:
            logger.warning("完成监控 trace 失败：%s", exc)

    return final_state


def _sync_state_to_session(session_id: str, state: AgentState) -> None:
    """把 AgentState 中的关键字段同步回 SessionManager。

    failed_attempts 已在 agent_node 内更新，这里直接写入 session，
    避免重复累加。history 在 append_history（user）与这里（assistant）共同维护。
    """
    # 直接用 state 中的 failed_attempts 覆盖 session，避免重复累加
    failed = int(state.get("failed_attempts", 0))
    session_manager.update_session(session_id, failed_attempts=failed)

    # 更新会话上下文：意图、情绪（归一化后的 0-1）
    session_manager.update_session(
        session_id,
        current_intent=state.get("intent"),
        emotion_score=state.get("emotion_score"),
    )

    # 追加 assistant 回复到历史
    reply = state.get("final_reply", "")
    session_manager.append_history(session_id, "assistant", reply)


def reset_graph() -> None:
    """重置 graph 模块缓存，便于测试切换配置或注入 mock。

    会清空编译后的 graph 与 fallback 实例，下次 run_graph 重新构建。
    """
    global _compiled_graph, _graph_init_error, _synch_orchestrator
    _compiled_graph = None
    _graph_init_error = None
    _synch_orchestrator = None
