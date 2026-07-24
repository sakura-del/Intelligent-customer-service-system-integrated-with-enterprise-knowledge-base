"""Agent 监控追踪器。

采集每次 run_graph 执行的 trace 与各节点 step，提供运维可观测能力：
- trace：一次完整 run_graph 调用的全链路记录（意图、路由、各 agent 输入输出、最终回复）
- step：trace 内每个关键节点（intent/route/agent_name/dialog/escalate）的输入输出与耗时
- agent 统计：按 agent_name 聚合调用次数、平均耗时、成功率
- 活跃会话：从 SessionManager 读取当前会话列表

设计要点：
- 单例模式（get_monitor），进程内复用，避免每个请求各起一套采集器
- 线程安全：所有读写经同一把 RLock 串行化，兼容 agent_node 内 ThreadPoolExecutor 并发埋点
- 内存优化：trace 保留最近 N 条（默认 100，可配置），超限按 FIFO 丢弃
- 持久化接口：预留 save_trace 钩子，后续接入 ES/DB 时只需覆写
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.core.monitor")

# trace 保留上限：超过则按 FIFO 丢弃最旧
# 选取 100 兼顾运维可观测性与内存成本，可通过构造参数覆盖
DEFAULT_MAX_TRACES = 100

# 单步输入输出摘要截断长度，避免长文本拖爆内存
SUMMARY_MAX_LENGTH = 200


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串，统一时间格式便于序列化。"""
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: Any, max_length: int = SUMMARY_MAX_LENGTH) -> str:
    """把任意输入转为字符串并截断，避免长文本占用过多内存。

    监控面板只需展示摘要，完整内容可从日志或持久化存储查询。
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if len(s) <= max_length:
        return s
    return s[:max_length] + "..."


def _new_trace(trace_id: str, session_id: str, message: str) -> dict[str, Any]:
    """构造一条新 trace 的初始结构。

    集中在此处定义字段，便于后续接入持久化时复用序列化结构。
    """
    return {
        "trace_id": trace_id,
        "session_id": session_id,
        "message": _truncate(message),
        "start_time": _now_iso(),
        "end_time": None,
        "duration_ms": 0.0,
        "intent": "",
        "route_path": [],
        "sub_tasks": [],
        "final_reply": "",
        "escalate_to_human": False,
        "status": "running",  # running / success / failed
        "error": None,
        "steps": [],
        "turn_count": 0,
        "failed_attempts": 0,
    }


class Monitor:
    """监控追踪器。

    持有进程内 trace 双端队列与一把 RLock，所有暴露方法均加锁，
    保证多线程并发埋点时 trace 数据一致。

    使用流程：
        trace_id = monitor.start_trace(session_id, message)
        monitor.record_step(trace_id, "intent", input, output, duration_ms)
        monitor.record_agent_call(trace_id, agent_name, input, output, duration_ms, success)
        monitor.finish_trace(trace_id, intent=..., final_reply=..., ...)
        # 或失败时：
        monitor.fail_trace(trace_id, error)
    """

    def __init__(self, max_traces: int = DEFAULT_MAX_TRACES) -> None:
        # deque 配 maxlen 后超限自动丢弃最旧，无需手动维护
        self._traces: deque[dict[str, Any]] = deque(maxlen=max_traces)
        # trace_id -> trace 的索引，便于 O(1) 查询详情
        # 注意：与 deque 数据保持同步，丢弃时同步移除索引
        self._trace_index: dict[str, dict[str, Any]] = {}
        self._max_traces = max_traces
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # trace 生命周期
    # ------------------------------------------------------------------
    def start_trace(self, session_id: str, message: str) -> str:
        """开始一条新 trace，返回 trace_id。

        采用 uuid4 保证全局唯一，便于跨服务追踪。
        超过上限时 deque 自动丢弃最旧 trace，同步清理索引避免泄漏。
        """
        trace_id = str(uuid.uuid4())
        trace = _new_trace(trace_id, session_id, message)
        with self._lock:
            # deque(maxlen=...) 会自动弹出最旧元素，但不会通知我们
            # 因此先手动检查：若即将超限，弹出最旧并清理索引
            if len(self._traces) >= self._max_traces:
                self._evict_oldest_locked()
            self._traces.append(trace)
            self._trace_index[trace_id] = trace
        return trace_id

    def _evict_oldest_locked(self) -> None:
        """弹出最旧 trace 并清理索引（调用方需持锁）。"""
        if not self._traces:
            return
        oldest = self._traces.popleft()
        self._trace_index.pop(oldest.get("trace_id"), None)

    def record_step(
        self,
        trace_id: str,
        node: str,
        input_summary: Any,
        output_summary: Any,
        duration_ms: float,
    ) -> None:
        """记录一个节点的执行步骤。

        node 取值：intent / route / dialog / escalate / {agent_name}。
        agent 执行步骤直接用 agent_name 作为 node，便于 route_path 直观展示。
        """
        step = {
            "node": node,
            "input_summary": _truncate(input_summary),
            "output_summary": _truncate(output_summary),
            "duration_ms": round(float(duration_ms), 2),
            "timestamp": _now_iso(),
        }
        with self._lock:
            trace = self._trace_index.get(trace_id)
            if trace is None:
                # trace 已被淘汰或不存在：静默丢弃，避免影响主链路
                return
            trace["steps"].append(step)
            # route_path 实时维护，便于无需重算即可返回摘要
            trace["route_path"].append(node)

    def record_agent_call(
        self,
        trace_id: str,
        agent_name: str,
        input_text: Any,
        output_text: Any,
        duration_ms: float,
        success: bool,
    ) -> None:
        """记录一次 agent 调用，用于 agent 维度统计。

        与 record_step 分离：record_step 记录图节点级别，
        record_agent_call 记录 agent 维度，便于按 agent_name 聚合统计。
        """
        agent_call = {
            "agent_name": agent_name,
            "input": _truncate(input_text),
            "output": _truncate(output_text),
            "duration_ms": round(float(duration_ms), 2),
            "success": bool(success),
        }
        with self._lock:
            trace = self._trace_index.get(trace_id)
            if trace is None:
                return
            trace["sub_tasks"].append(agent_call)

    def finish_trace(
        self,
        trace_id: str,
        *,
        intent: str = "",
        final_reply: str = "",
        escalate_to_human: bool = False,
        turn_count: int = 0,
        failed_attempts: int = 0,
        status: str = "success",
        error: str | None = None,
    ) -> None:
        """完成一条 trace，写入最终字段。

        显式接收终态字段，避免依赖外部 state 结构变化。
        """
        with self._lock:
            trace = self._trace_index.get(trace_id)
            if trace is None:
                return
            trace["end_time"] = _now_iso()
            # duration_ms 取 start_time 到 end_time 的实际差值
            try:
                start = datetime.fromisoformat(trace["start_time"])
                end = datetime.fromisoformat(trace["end_time"])
                trace["duration_ms"] = round((end - start).total_seconds() * 1000.0, 2)
            except (ValueError, TypeError):
                # 时间解析失败时降级为 0，避免影响 trace 落库
                trace["duration_ms"] = 0.0
            trace["intent"] = intent
            trace["final_reply"] = _truncate(final_reply, 500)
            trace["escalate_to_human"] = escalate_to_human
            trace["turn_count"] = int(turn_count)
            trace["failed_attempts"] = int(failed_attempts)
            trace["status"] = status
            trace["error"] = error
            # 预留持久化钩子：子类可覆写 _persist_trace 接入 ES/DB
            self._persist_trace_locked(trace)

    def fail_trace(self, trace_id: str, error: str) -> None:
        """标记 trace 失败并记录错误信息。"""
        with self._lock:
            trace = self._trace_index.get(trace_id)
            if trace is None:
                return
            trace["end_time"] = _now_iso()
            try:
                start = datetime.fromisoformat(trace["start_time"])
                end = datetime.fromisoformat(trace["end_time"])
                trace["duration_ms"] = round((end - start).total_seconds() * 1000.0, 2)
            except (ValueError, TypeError):
                trace["duration_ms"] = 0.0
            trace["status"] = "failed"
            trace["error"] = _truncate(error, 500)
            self._persist_trace_locked(trace)

    def _persist_trace_locked(self, trace: dict[str, Any]) -> None:
        """持久化钩子，默认空实现。

        预留接口：后续接入 ES/DB 时覆写此方法即可，无需改动上层调用。
        调用方需持锁。
        """
        # 默认不持久化，仅内存存储
        pass

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def get_traces(self, limit: int = 50) -> list[dict[str, Any]]:
        """返回最近 trace 列表（摘要，不含 steps 详情）。

        按时间倒序返回（最新在前），limit 控制最多返回条数。
        返回浅拷贝避免外部修改内部状态。
        """
        with self._lock:
            # deque 默认按插入顺序（最旧在前），反转为最新在前
            ordered = list(reversed(self._traces))
            return [self._trace_summary(t) for t in ordered[:limit]]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """返回单条 trace 详情（含 steps 与 sub_tasks）。

        不存在则返回 None。
        """
        with self._lock:
            trace = self._trace_index.get(trace_id)
            if trace is None:
                return None
            # 深拷贝避免外部修改内部状态
            return {
                "trace_id": trace["trace_id"],
                "session_id": trace["session_id"],
                "message": trace["message"],
                "start_time": trace["start_time"],
                "end_time": trace["end_time"],
                "duration_ms": trace["duration_ms"],
                "intent": trace["intent"],
                "route_path": list(trace["route_path"]),
                "sub_tasks": [dict(st) for st in trace["sub_tasks"]],
                "final_reply": trace["final_reply"],
                "escalate_to_human": trace["escalate_to_human"],
                "status": trace["status"],
                "error": trace["error"],
                "steps": [dict(s) for s in trace["steps"]],
                "turn_count": trace["turn_count"],
                "failed_attempts": trace["failed_attempts"],
            }

    def get_agent_stats(self) -> list[dict[str, Any]]:
        """返回各 Agent 当前状态。

        从 OrchestratorAgent 注册表获取所有已注册 agent 名（保证未调用的 agent 也展示），
        再扫描 traces 聚合每个 agent 的调用次数、平均耗时与成功率。
        """
        # 懒导入避免循环依赖：orchestrator 不应在模块加载阶段被引入
        try:
            from app.agents.orchestrator import get_orchestrator

            registered_names = list(get_orchestrator()._agent_registry.keys())
        except Exception as exc:
            # orchestrator 未初始化或导入失败：降级为仅展示已采集到的 agent
            logger.debug("获取已注册 agent 列表失败，仅展示已采集 agent：%s", exc)
            registered_names = []

        # 初始化所有已注册 agent 的统计骨架，未调用过的 agent 也展示 0
        stats: dict[str, dict[str, Any]] = {
            name: {
                "name": name,
                "total_calls": 0,
                "success_count": 0,
                "total_duration_ms": 0.0,
            }
            for name in registered_names
        }

        with self._lock:
            for trace in self._traces:
                for agent_call in trace.get("sub_tasks", []):
                    name = agent_call.get("agent_name")
                    if not name:
                        continue
                    if name not in stats:
                        stats[name] = {
                            "name": name,
                            "total_calls": 0,
                            "success_count": 0,
                            "total_duration_ms": 0.0,
                        }
                    stats[name]["total_calls"] += 1
                    if agent_call.get("success"):
                        stats[name]["success_count"] += 1
                    stats[name]["total_duration_ms"] += agent_call.get("duration_ms", 0.0)

        # 计算平均耗时与成功率
        result: list[dict[str, Any]] = []
        for stat in stats.values():
            calls = stat["total_calls"]
            avg_duration = stat["total_duration_ms"] / calls if calls > 0 else 0.0
            success_rate = stat["success_count"] / calls if calls > 0 else 0.0
            result.append(
                {
                    "name": stat["name"],
                    "total_calls": calls,
                    "success_count": stat["success_count"],
                    "total_duration_ms": round(stat["total_duration_ms"], 2),
                    "avg_duration_ms": round(avg_duration, 2),
                    "success_rate": round(success_rate, 4),
                }
            )
        return result

    def get_sessions(self) -> list[dict[str, Any]]:
        """返回活跃会话列表。

        委托给 SessionManager.list_sessions，避免在 Monitor 中重复维护会话状态。
        """
        try:
            from app.core.session import session_manager

            return session_manager.list_sessions()
        except Exception as exc:
            logger.warning("获取活跃会话列表失败：%s", exc)
            return []

    def get_overview(self) -> dict[str, Any]:
        """返回系统概览统计：总 trace 数、成功率、平均耗时、活跃会话数。"""
        with self._lock:
            total = len(self._traces)
            success = sum(1 for t in self._traces if t.get("status") == "success")
            failed = sum(1 for t in self._traces if t.get("status") == "failed")
            durations = [t.get("duration_ms", 0.0) for t in self._traces]
            avg_duration = sum(durations) / len(durations) if durations else 0.0
            success_rate = success / total if total > 0 else 0.0
        sessions_count = len(self.get_sessions())
        return {
            "total_traces": total,
            "success_count": success,
            "failed_count": failed,
            "success_rate": round(success_rate, 4),
            "avg_duration_ms": round(avg_duration, 2),
            "active_sessions": sessions_count,
        }

    def get_stream_first_token_durations(self) -> list[float]:
        """返回所有 trace 中 stream_first_token 步骤的耗时列表（毫秒）。

        每条 trace 最多取一次首 Token 耗时（break），
        供 performance.py 聚合 avg/p95，避免多次记录导致统计偏置。
        """
        durations: list[float] = []
        with self._lock:
            for trace in self._traces:
                # 每条 trace 只取首个 stream_first_token 步骤
                for step in trace.get("steps", []):
                    if step.get("node") == "stream_first_token":
                        durations.append(float(step.get("duration_ms", 0.0)))
                        break
        return durations

    # ------------------------------------------------------------------
    # 维护与测试辅助
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空所有 trace，主要用于测试隔离。"""
        with self._lock:
            self._traces.clear()
            self._trace_index.clear()

    @property
    def max_traces(self) -> int:
        """trace 保留上限，便于外部诊断。"""
        return self._max_traces

    @staticmethod
    def _trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
        """构造 trace 摘要（不含 steps 详情），用于列表展示。

        列表场景只需关键字段，详情通过 get_trace 单独查询，减少传输量。
        """
        return {
            "trace_id": trace["trace_id"],
            "session_id": trace["session_id"],
            "message": trace["message"],
            "start_time": trace["start_time"],
            "end_time": trace["end_time"],
            "duration_ms": trace["duration_ms"],
            "intent": trace["intent"],
            "route_path": list(trace["route_path"]),
            "escalate_to_human": trace["escalate_to_human"],
            "status": trace["status"],
            "turn_count": trace["turn_count"],
        }


# 模块级单例：进程内复用一个 Monitor，避免每个请求各起一套采集器
_monitor: Monitor | None = None
_monitor_lock = threading.Lock()


def get_monitor() -> Monitor:
    """获取 Monitor 单例。

    首次调用时按默认配置创建，后续复用同一实例。
    """
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = Monitor()
    return _monitor


def reset_monitor() -> None:
    """重置单例，便于测试切换配置或注入 mock。"""
    global _monitor
    with _monitor_lock:
        _monitor = None
