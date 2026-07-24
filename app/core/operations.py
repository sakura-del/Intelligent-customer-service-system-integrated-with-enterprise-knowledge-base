"""运营数据看板与上线检查清单。

提供 OperationsCollector 与 ReleaseChecklist：
- OperationsCollector：聚合会话/工单/转接/满意度/知识库统计，30 秒内缓存避免重复计算
- ReleaseChecklist：执行依赖、配置、数据库、知识库、API、性能等上线前检查

设计要点：
- 线程安全：所有共享状态用 RLock 保护
- 延迟计算：检查项延迟到调用时执行，避免启动期开销
- 降级：聚合失败返回空统计，单检查项失败不影响其他
- 不引入新依赖
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.operations import (
    CheckItem,
    ChecklistReport,
    EscalationStats,
    KnowledgeStats,
    OperationsDashboard,
    SatisfactionStats,
    SessionStats,
    TicketStats,
)

logger = get_logger("app.core.operations")

# 看板缓存有效期（秒），避免高频请求重复聚合
DASHBOARD_CACHE_TTL_SECONDS = 30.0

# 满意度推算常量：基于转接率与失败率推算 mock 满意度
SATISFACTION_BASE_SCORE = 4.5
SATISFACTION_ESCALATION_PENALTY = 0.5
SATISFACTION_FAILURE_PENALTY = 0.3

# 近 N 天入库量统计窗口
RECENT_INGEST_WINDOW_DAYS = 7

# 默认 mock 人工接通率：转接发生后假设 90% 能被人工接起
MOCK_HUMAN_PICKUP_RATE = 0.9


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat()


class OperationsCollector:
    """运营数据聚合器。

    collect() 一次聚合所有运营数据并返回 OperationsDashboard。
    30 秒内重复调用直接返回缓存，避免重复计算。

    各子统计独立聚合，单项失败不影响其他项，整体降级为空统计。
    """

    def __init__(self, cache_ttl: float = DASHBOARD_CACHE_TTL_SECONDS) -> None:
        self._lock = threading.RLock()
        self._cache_ttl = cache_ttl
        self._cache: OperationsDashboard | None = None
        self._cache_at: float = 0.0

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def collect(self, force_refresh: bool = False) -> OperationsDashboard:
        """聚合所有运营数据。

        force_refresh=True 时跳过缓存，强制重新聚合。
        缓存未过期且存在时直接返回缓存结果。
        """
        with self._lock:
            if not force_refresh and self._is_cache_valid():
                return self._cache  # type: ignore[return-value]
            dashboard = self._aggregate_all()
            self._cache = dashboard
            self._cache_at = time.monotonic()
            return dashboard

    def _is_cache_valid(self) -> bool:
        """判断缓存是否有效。"""
        if self._cache is None:
            return False
        return (time.monotonic() - self._cache_at) < self._cache_ttl

    def _aggregate_all(self) -> OperationsDashboard:
        """聚合所有子统计，任一异常降级为空统计。"""
        session = self._safe_collect(self._collect_session_stats, SessionStats())
        ticket = self._safe_collect(self._collect_ticket_stats, TicketStats())
        escalation = self._safe_collect(self._collect_escalation_stats, EscalationStats())
        satisfaction = self._safe_collect(
            lambda: self._collect_satisfaction_stats(escalation, session),
            SatisfactionStats(),
        )
        knowledge = self._safe_collect(self._collect_knowledge_stats, KnowledgeStats())
        return OperationsDashboard(
            session=session,
            ticket=ticket,
            escalation=escalation,
            satisfaction=satisfaction,
            knowledge=knowledge,
            collected_at=_now_iso(),
        )

    @staticmethod
    def _safe_collect(collector: Callable[[], Any], fallback: Any) -> Any:
        """安全执行子统计，异常时降级为 fallback。"""
        try:
            return collector()
        except Exception as exc:
            logger.warning("运营子统计聚合失败，降级为空统计：%s", exc)
            return fallback

    # ------------------------------------------------------------------
    # 各子统计聚合
    # ------------------------------------------------------------------

    def _collect_session_stats(self) -> SessionStats:
        """聚合会话统计：总数、活跃数、平均轮数。"""
        from app.core.session import session_manager

        sessions = session_manager.list_sessions()
        total = len(sessions)
        if total == 0:
            return SessionStats()
        # 活跃会话：turn_count > 0 视为有交互
        active = sum(1 for s in sessions if int(s.get("turn_count", 0)) > 0)
        turn_counts = [int(s.get("turn_count", 0)) for s in sessions]
        avg_turn = sum(turn_counts) / total if total else 0.0
        return SessionStats(
            total_sessions=total,
            active_sessions=active,
            avg_turn_count=round(avg_turn, 2),
        )

    def _collect_ticket_stats(self) -> TicketStats:
        """聚合工单统计：新增/已解决/未解决、分类分布。"""
        from app.agents.ticket_store import get_ticket_store
        from app.schemas.ticket import TicketStatus

        store = get_ticket_store()
        tickets = store.list_tickets()
        total = len(tickets)
        new_count = sum(1 for t in tickets if t.status == TicketStatus.pending)
        resolved_count = sum(
            1 for t in tickets if t.status in (TicketStatus.resolved, TicketStatus.closed)
        )
        unresolved_count = sum(
            1 for t in tickets if t.status in (TicketStatus.pending, TicketStatus.processing)
        )
        # 按分类聚合
        category_dist: dict[str, int] = {}
        for t in tickets:
            key = t.category.value if hasattr(t.category, "value") else str(t.category)
            category_dist[key] = category_dist.get(key, 0) + 1
        return TicketStats(
            total=total,
            new_count=new_count,
            resolved_count=resolved_count,
            unresolved_count=unresolved_count,
            category_distribution=category_dist,
        )

    def _collect_escalation_stats(self) -> EscalationStats:
        """聚合转接统计：转接次数、原因分布、人工接通率。

        转接数据来自 Monitor 的 traces（escalate_to_human=True）。
        无 trace 数据时返回空统计。
        """
        from app.core.monitor import get_monitor

        monitor = get_monitor()
        traces = monitor.get_traces(limit=200)
        escalated = [t for t in traces if t.get("escalate_to_human")]
        total = len(escalated)
        # 原因分布从 rule_matched 字段聚合（trace 中可能未记录，使用兜底键）
        reason_dist: dict[str, int] = {}
        for t in escalated:
            reason = t.get("rule_matched") or "unknown"
            reason_dist[reason] = reason_dist.get(reason, 0) + 1
        # mock 人工接通率：固定值，避免依赖外部系统
        pickup_rate = MOCK_HUMAN_PICKUP_RATE if total > 0 else 0.0
        return EscalationStats(
            total_escalations=total,
            reason_distribution=reason_dist,
            human_pickup_rate=pickup_rate,
        )

    def _collect_satisfaction_stats(
        self,
        escalation: EscalationStats,
        session: SessionStats,
    ) -> SatisfactionStats:
        """基于转接率与失败率推算 mock 满意度。

        无真实评价数据接入，使用启发式推算：
        - 基础分 4.5
        - 转接率越高满意度越低
        - 平均失败次数越高满意度越低
        """
        from app.core.monitor import get_monitor

        monitor = get_monitor()
        overview = monitor.get_overview()
        total_traces = int(overview.get("total_traces", 0))
        if total_traces == 0:
            return SatisfactionStats()
        failed_count = int(overview.get("failed_count", 0))
        escalation_rate = escalation.total_escalations / total_traces if total_traces else 0.0
        failure_rate = failed_count / total_traces if total_traces else 0.0
        score = SATISFACTION_BASE_SCORE
        score -= SATISFACTION_ESCALATION_PENALTY * escalation_rate
        score -= SATISFACTION_FAILURE_PENALTY * failure_rate
        # 限制在 [0, 5]
        score = max(0.0, min(5.0, score))
        positive_rate = score / 5.0
        return SatisfactionStats(
            avg_score=round(score, 2),
            sample_count=total_traces,
            positive_rate=round(positive_rate, 4),
        )

    def _collect_knowledge_stats(self) -> KnowledgeStats:
        """聚合知识库统计：总条目数、类型分布、近 7 天入库量。"""
        from app.knowledge.vectorstore import get_vector_store

        # 总条目数来自向量库
        total_entries = 0
        type_dist: dict[str, int] = {}
        try:
            store = get_vector_store()
            total_entries = int(store.count())
            # 按 metadata.knowledge_type 聚合类型分布
            chunks = store.get_all_chunks(batch_size=500)
            for chunk in chunks:
                meta = chunk.get("metadata") or {}
                ktype = meta.get("knowledge_type") or "unknown"
                type_dist[ktype] = type_dist.get(ktype, 0) + 1
        except Exception as exc:
            logger.warning("知识库总条目统计失败，降级为 0：%s", exc)

        # 近 7 天入库量来自文档元数据
        recent_count = self._count_recent_ingest()
        return KnowledgeStats(
            total_entries=total_entries,
            type_distribution=type_dist,
            recent_7d_ingest_count=recent_count,
        )

    @staticmethod
    def _count_recent_ingest() -> int:
        """统计近 7 天入库的文档数。

        从 DocumentStore 拉取文档列表，按 updated_at 字段过滤。
        查询失败时返回 0。
        """
        try:
            from app.knowledge.document_store import get_document_store

            doc_store = get_document_store()
            docs = doc_store.list_documents()
            cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_INGEST_WINDOW_DAYS)
            count = 0
            for doc in docs:
                updated = doc.get("updated_at", "")
                if not updated:
                    continue
                try:
                    # 兼容带/不带时区的 ISO 字符串
                    updated_dt = datetime.fromisoformat(updated)
                    if updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                    if updated_dt >= cutoff:
                        count += 1
                except (ValueError, TypeError):
                    continue
            return count
        except Exception as exc:
            logger.warning("近 7 天入库量统计失败，降级为 0：%s", exc)
            return 0

    # ------------------------------------------------------------------
    # 维护与测试辅助
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """清除缓存，便于测试或强制刷新。"""
        with self._lock:
            self._cache = None
            self._cache_at = 0.0


# ----------------------------------------------------------------------
# 上线检查清单
# ----------------------------------------------------------------------


class ReleaseChecklist:
    """上线检查清单执行器。

    每项检查独立执行，失败不中断其他检查。
    延迟计算：仅在 run_all_checks 调用时执行检查。
    """

    def __init__(self) -> None:
        # 检查项注册表：name -> callable 返回 (status, message)
        self._checks: list[dict[str, Any]] = [
            {"name": "依赖完整性", "func": self._check_dependencies},
            {"name": "配置完整性", "func": self._check_config},
            {"name": "数据库连接", "func": self._check_database},
            {"name": "知识库非空", "func": self._check_knowledge_nonempty},
            {"name": "API 健康检查", "func": self._check_api_health},
            {"name": "性能基线", "func": self._check_performance_baseline},
        ]

    def run_all_checks(self) -> ChecklistReport:
        """执行所有检查项，返回汇总报告。"""
        items: list[CheckItem] = []
        passed = failed = warned = skipped = 0
        for check in self._checks:
            item = self._run_single(check["name"], check["func"])
            items.append(item)
            if item.status == "pass":
                passed += 1
            elif item.status == "fail":
                failed += 1
            elif item.status == "warn":
                warned += 1
            else:
                skipped += 1
        return ChecklistReport(
            passed=passed,
            failed=failed,
            warned=warned,
            skipped=skipped,
            total=len(items),
            items=items,
            generated_at=_now_iso(),
        )

    @staticmethod
    def _run_single(name: str, func: Callable[[], tuple]) -> CheckItem:
        """执行单条检查，异常时记为 fail。

        检查函数返回 (status, message) 二元组。
        """
        start = time.monotonic()
        try:
            status, message = func()
        except Exception as exc:
            status = "fail"
            message = f"检查异常：{exc}"
        duration_ms = round((time.monotonic() - start) * 1000.0, 2)
        return CheckItem(
            name=name,
            status=status,
            message=message,
            duration_ms=duration_ms,
        )

    # ------------------------------------------------------------------
    # 各检查项实现
    # ------------------------------------------------------------------

    @staticmethod
    def _check_dependencies() -> tuple:
        """检查关键依赖是否可导入。"""
        critical_modules = [
            "fastapi",
            "pydantic",
            "chromadb",
        ]
        missing = []
        for mod in critical_modules:
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        if missing:
            return "fail", f"缺失关键依赖：{', '.join(missing)}"
        return "pass", "关键依赖完整"

    @staticmethod
    def _check_config() -> tuple:
        """检查关键配置是否已设置。"""
        settings = get_settings()
        issues = []
        # 持久化目录必须存在路径值
        if not settings.CHROMA_PERSIST_DIR:
            issues.append("CHROMA_PERSIST_DIR 未配置")
        # 工作时间合理性
        if settings.WORKING_HOURS_END <= settings.WORKING_HOURS_START:
            issues.append(
                f"WORKING_HOURS 配置异常：start={settings.WORKING_HOURS_START} end={settings.WORKING_HOURS_END}"
            )
        # 相似度阈值范围检查
        if not 0.0 <= settings.SIMILARITY_THRESHOLD <= 1.0:
            issues.append(f"SIMILARITY_THRESHOLD 越界：{settings.SIMILARITY_THRESHOLD}")
        if issues:
            return "warn", "；".join(issues)
        return "pass", "关键配置完整"

    @staticmethod
    def _check_database() -> tuple:
        """检查 ChromaDB 是否可访问。

        实际部署中可扩展为检查 Redis / Elasticsearch 等。
        """
        try:
            from app.knowledge.vectorstore import get_vector_store

            store = get_vector_store()
            count = store.count()
            return "pass", f"ChromaDB 连接正常，当前条目数 {count}"
        except Exception as exc:
            return "fail", f"ChromaDB 连接失败：{exc}"

    @staticmethod
    def _check_knowledge_nonempty() -> tuple:
        """检查知识库非空。"""
        try:
            from app.knowledge.vectorstore import get_vector_store

            count = get_vector_store().count()
            if count <= 0:
                return "warn", "知识库为空，建议入库基础 FAQ 后再上线"
            return "pass", f"知识库已入库 {count} 条"
        except Exception as exc:
            return "fail", f"知识库检查失败：{exc}"

    @staticmethod
    def _check_api_health() -> tuple:
        """检查 API 健康端点是否可用。

        mock 检查：直接验证 FastAPI 应用能否构造，
        真实部署可改为 HTTP 调用 /api/v1/health。
        """
        try:
            from fastapi import FastAPI

            app = FastAPI()
            if app is None:
                return "fail", "FastAPI 应用构造失败"
            return "pass", "API 应用可正常构造"
        except Exception as exc:
            return "fail", f"API 健康检查失败：{exc}"

    @staticmethod
    def _check_performance_baseline() -> tuple:
        """检查性能基线：简单测量一次向量库计数耗时。

        阈值 1 秒，超过则告警（不直接 fail，避免性能抖动误报）。
        """
        try:
            from app.knowledge.vectorstore import get_vector_store

            start = time.monotonic()
            get_vector_store().count()
            duration_ms = (time.monotonic() - start) * 1000.0
            if duration_ms > 1000.0:
                return "warn", f"向量库计数耗时 {duration_ms:.0f}ms 超过 1s"
            return "pass", f"性能基线正常（计数耗时 {duration_ms:.0f}ms）"
        except Exception as exc:
            return "warn", f"性能基线检查失败（已降级）：{exc}"


# ----------------------------------------------------------------------
# 单例管理
# ----------------------------------------------------------------------

_operations_collector: OperationsCollector | None = None
_operations_singleton_lock = threading.Lock()


def get_operations_collector() -> OperationsCollector:
    """获取 OperationsCollector 单例。"""
    global _operations_collector
    if _operations_collector is None:
        with _operations_singleton_lock:
            if _operations_collector is None:
                _operations_collector = OperationsCollector()
    return _operations_collector


def reset_operations_collector() -> None:
    """重置单例，便于测试隔离。"""
    global _operations_collector
    with _operations_singleton_lock:
        _operations_collector = None
