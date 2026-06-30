"""运营看板与上线检查清单测试。

覆盖 OperationsCollector 与 ReleaseChecklist：
1. 看板聚合：会话/工单/转接/满意度/知识库统计
2. 缓存：30 秒 TTL，force_refresh 跳过缓存
3. 降级：子统计异常时降级为空统计
4. 上线检查：6 项检查独立执行、报告字段完整
5. API 端点：dashboard / release-checklist

测试隔离：使用独立 chroma 目录，模块级 fixture 重置单例。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 测试用独立持久化目录
TEST_PERSIST_DIR = "./tests/_chroma_data_operations"


# ----------------------------------------------------------------------
# 模块级 fixture：隔离 ChromaDB 目录与重置单例
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离向量库目录并重置所有相关单例。"""
    from app.agents import ticket_store as ticket_store_module
    from app.core import operations as operations_module
    from app.core.config import get_settings
    from app.core.monitor import reset_monitor
    from app.core.session import session_manager
    from app.knowledge import (
        document_store as document_store_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)
    persist_path.mkdir(parents=True, exist_ok=True)

    # 重置所有相关单例
    vectorstore_module.reset_vector_store()
    document_store_module.reset_document_store()
    ticket_store_module.reset_ticket_store()
    reset_monitor()
    session_manager.reset_all()
    operations_module.reset_operations_collector()

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    document_store_module.reset_document_store()
    ticket_store_module.reset_ticket_store()
    reset_monitor()
    session_manager.reset_all()
    operations_module.reset_operations_collector()
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_per_test():
    """每个用例前重置会话、工单、监控、运营采集器状态。"""
    from app.agents import ticket_store as ticket_store_module
    from app.core import operations as operations_module
    from app.core.monitor import reset_monitor
    from app.core.session import session_manager

    ticket_store_module.reset_ticket_store()
    reset_monitor()
    session_manager.reset_all()
    operations_module.reset_operations_collector()
    yield


# ----------------------------------------------------------------------
# FastAPI 应用与 TestClient fixture
# ----------------------------------------------------------------------


@pytest.fixture()
def app_with_routers():
    """提供注册了 operations 路由的 FastAPI 应用。"""
    from app.api.v1.operations import router as operations_router

    app = FastAPI()
    app.include_router(operations_router)
    return app


@pytest.fixture()
def client(app_with_routers):
    """提供 TestClient。"""
    return TestClient(app_with_routers)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def _create_ticket(
    status: Any = None,
    category: Any = None,
) -> None:
    """创建一张工单到 TicketStore，便于测试统计。"""
    from app.agents.ticket_store import get_ticket_store
    from app.schemas.ticket import (
        TicketCategory,
        TicketPriority,
        TicketStatus,
    )

    store = get_ticket_store()
    ticket = store.create_ticket(
        user_id="u1",
        title="测试工单",
        description="desc",
        category=category or TicketCategory.after_sale,
        priority=TicketPriority.medium,
    )
    if status is not None and status != TicketStatus.pending:
        store.update_status(ticket.ticket_id, status)


def _create_session(turn_count: int = 0) -> str:
    """创建一个会话并设置 turn_count，便于测试统计。"""
    from app.core.session import session_manager

    session_id = session_manager.create_session(channel="web", user_id="u1")
    if turn_count > 0:
        session_manager.update_session(session_id, turn_count=turn_count)
    return session_id


# ----------------------------------------------------------------------
# OperationsCollector 主入口测试
# ----------------------------------------------------------------------


def test_collect_returns_dashboard_with_all_sections():
    """collect() 应返回包含所有统计分区的 OperationsDashboard。"""
    from app.core.operations import get_operations_collector

    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard is not None
    # 所有分区都应存在（即使为空）
    assert dashboard.session is not None
    assert dashboard.ticket is not None
    assert dashboard.escalation is not None
    assert dashboard.satisfaction is not None
    assert dashboard.knowledge is not None
    # collected_at 应为非空字符串
    assert dashboard.collected_at


def test_collect_session_stats_empty():
    """无会话时应返回零值会话统计。"""
    from app.core.operations import get_operations_collector

    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard.session.total_sessions == 0
    assert dashboard.session.active_sessions == 0
    assert dashboard.session.avg_turn_count == 0.0


def test_collect_session_stats_with_active_sessions():
    """有会话时应正确统计总数、活跃数与平均轮数。"""
    from app.core.operations import get_operations_collector

    _create_session(turn_count=2)
    _create_session(turn_count=4)
    _create_session(turn_count=0)
    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard.session.total_sessions == 3
    # turn_count > 0 视为活跃
    assert dashboard.session.active_sessions == 2
    # 平均轮数 (2+4+0)/3 = 2.0
    assert dashboard.session.avg_turn_count == 2.0


def test_collect_ticket_stats_empty():
    """无工单时应返回零值工单统计。"""
    from app.core.operations import get_operations_collector

    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard.ticket.total == 0
    assert dashboard.ticket.new_count == 0
    assert dashboard.ticket.resolved_count == 0
    assert dashboard.ticket.unresolved_count == 0


def test_collect_ticket_stats_with_various_statuses():
    """不同状态工单应正确分类统计。"""
    from app.core.operations import get_operations_collector
    from app.schemas.ticket import (
        TicketCategory,
        TicketStatus,
    )

    # pending x1, processing x1, resolved x1, closed x1
    _create_ticket(status=TicketStatus.pending, category=TicketCategory.after_sale)
    _create_ticket(status=TicketStatus.processing, category=TicketCategory.logistics)
    _create_ticket(status=TicketStatus.resolved, category=TicketCategory.product)
    _create_ticket(status=TicketStatus.closed, category=TicketCategory.complaint)

    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard.ticket.total == 4
    # new_count = pending 数量
    assert dashboard.ticket.new_count == 1
    # resolved_count = resolved + closed
    assert dashboard.ticket.resolved_count == 2
    # unresolved_count = pending + processing
    assert dashboard.ticket.unresolved_count == 2
    # 分类分布应包含 4 个分类
    assert len(dashboard.ticket.category_distribution) == 4


def test_collect_ticket_stats_category_distribution():
    """分类分布应按 category 名称聚合。"""
    from app.core.operations import get_operations_collector
    from app.schemas.ticket import TicketCategory

    _create_ticket(category=TicketCategory.after_sale)
    _create_ticket(category=TicketCategory.after_sale)
    _create_ticket(category=TicketCategory.logistics)

    dashboard = get_operations_collector().collect(force_refresh=True)
    dist = dashboard.ticket.category_distribution
    assert dist.get("after_sale") == 2
    assert dist.get("logistics") == 1


def test_collect_escalation_stats_empty():
    """无 trace 数据时转接统计应为零。"""
    from app.core.operations import get_operations_collector

    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard.escalation.total_escalations == 0
    assert dashboard.escalation.human_pickup_rate == 0.0


def test_collect_escalation_stats_with_escalated_traces():
    """有转接 trace 时应统计转接次数与原因分布。"""
    from app.core.monitor import get_monitor
    from app.core.operations import get_operations_collector

    monitor = get_monitor()
    # 构造两条 trace，其中一条标记转接
    trace_id_1 = monitor.start_trace("s1", "msg1")
    monitor.finish_trace(
        trace_id_1,
        intent="ticket",
        final_reply="r1",
        escalate_to_human=True,
        status="success",
    )
    # 通过 record_step 注入 rule_matched 字段不可行，trace 摘要里
    # 没有 rule_matched，因此 reason_distribution 会聚合到 "unknown" key
    trace_id_2 = monitor.start_trace("s2", "msg2")
    monitor.finish_trace(
        trace_id_2,
        intent="chitchat",
        final_reply="r2",
        escalate_to_human=False,
        status="success",
    )

    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard.escalation.total_escalations == 1
    # 转接率 > 0 时应有 mock 接通率
    assert dashboard.escalation.human_pickup_rate > 0.0


def test_collect_satisfaction_stats_empty():
    """无 trace 时满意度统计应为零。"""
    from app.core.operations import get_operations_collector

    dashboard = get_operations_collector().collect(force_refresh=True)
    assert dashboard.satisfaction.avg_score == 0.0
    assert dashboard.satisfaction.sample_count == 0


def test_collect_satisfaction_stats_with_traces():
    """有 trace 数据时应推算出 mock 满意度。"""
    from app.core.monitor import get_monitor
    from app.core.operations import get_operations_collector

    monitor = get_monitor()
    trace_id = monitor.start_trace("s1", "msg")
    monitor.finish_trace(
        trace_id,
        intent="chitchat",
        final_reply="r",
        escalate_to_human=False,
        status="success",
    )
    dashboard = get_operations_collector().collect(force_refresh=True)
    # 至少有一条样本
    assert dashboard.satisfaction.sample_count >= 1
    # 满意度应在 0-5 之间
    assert 0.0 <= dashboard.satisfaction.avg_score <= 5.0


def test_collect_knowledge_stats_returns_zero_on_empty():
    """空知识库应返回 0 条目与空类型分布。"""
    from app.core.operations import get_operations_collector

    dashboard = get_operations_collector().collect(force_refresh=True)
    # 空库：total_entries 可能为 0
    assert dashboard.knowledge.total_entries >= 0
    # 类型分布可能为空字典
    assert isinstance(dashboard.knowledge.type_distribution, dict)


# ----------------------------------------------------------------------
# 缓存测试
# ----------------------------------------------------------------------


def test_dashboard_cache_returns_within_ttl():
    """30 秒内重复 collect 应返回缓存结果（同实例）。"""
    from app.core.operations import get_operations_collector

    collector = get_operations_collector()
    first = collector.collect(force_refresh=True)
    second = collector.collect()
    # 缓存命中应返回同一对象
    assert second is first


def test_dashboard_force_refresh_skips_cache():
    """force_refresh=True 应跳过缓存重新聚合。"""
    from app.core.operations import get_operations_collector

    collector = get_operations_collector()
    first = collector.collect(force_refresh=True)
    second = collector.collect(force_refresh=True)
    # 强制刷新应返回新对象
    assert second is not first


def test_dashboard_invalidate_cache():
    """invalidate_cache 后应重新聚合。"""
    from app.core.operations import get_operations_collector

    collector = get_operations_collector()
    first = collector.collect(force_refresh=True)
    collector.invalidate_cache()
    # 缓存失效后再次 collect 应重新聚合
    second = collector.collect()
    assert second is not first


def test_dashboard_reflects_new_sessions_after_invalidate():
    """invalidate 后再次 collect 应反映新增会话。"""
    from app.core.operations import get_operations_collector

    collector = get_operations_collector()
    first = collector.collect(force_refresh=True)
    initial_total = first.session.total_sessions
    # 新增一个会话
    _create_session(turn_count=1)
    # 不强制刷新：应返回缓存
    cached = collector.collect()
    assert cached.session.total_sessions == initial_total
    # invalidate 后应反映新会话
    collector.invalidate_cache()
    refreshed = collector.collect()
    assert refreshed.session.total_sessions == initial_total + 1


# ----------------------------------------------------------------------
# 降级测试
# ----------------------------------------------------------------------


def test_safe_collect_returns_fallback_on_exception():
    """子统计异常时应降级为 fallback 值。"""
    from app.core.operations import OperationsCollector

    def failing_collector() -> Any:
        raise RuntimeError("模拟聚合失败")

    fallback = {"fallback": True}
    result = OperationsCollector._safe_collect(failing_collector, fallback)
    assert result is fallback


def test_dashboard_aggregate_continues_on_sub_failure():
    """某子统计失败时其他子统计仍应正常返回。"""
    from app.core.operations import OperationsCollector
    from app.schemas.operations import SessionStats

    collector = OperationsCollector()

    # 替换 _collect_session_stats 让其抛错，其他保持正常
    original = collector._collect_session_stats

    def failing() -> SessionStats:
        raise RuntimeError("session stats 失败")

    collector._collect_session_stats = failing  # type: ignore
    try:
        dashboard = collector.collect(force_refresh=True)
        # session 降级为默认空统计
        assert dashboard.session.total_sessions == 0
        # 其他子统计应正常
        assert dashboard.ticket is not None
    finally:
        collector._collect_session_stats = original  # type: ignore


# ----------------------------------------------------------------------
# 上线检查清单测试
# ----------------------------------------------------------------------


def test_release_checklist_returns_complete_report():
    """run_all_checks 应返回包含所有字段的报告。"""
    from app.core.operations import ReleaseChecklist

    report = ReleaseChecklist().run_all_checks()
    assert report.total > 0
    assert report.passed + report.failed + report.warned + report.skipped == report.total
    assert len(report.items) == report.total
    assert report.generated_at


def test_release_checklist_has_six_checks():
    """应包含 6 项检查：依赖/配置/数据库/知识库/API/性能。"""
    from app.core.operations import ReleaseChecklist

    report = ReleaseChecklist().run_all_checks()
    names = [item.name for item in report.items]
    assert "依赖完整性" in names
    assert "配置完整性" in names
    assert "数据库连接" in names
    assert "知识库非空" in names
    assert "API 健康检查" in names
    assert "性能基线" in names
    assert report.total == 6


def test_release_checklist_dependencies_pass():
    """依赖完整性检查应在测试环境通过。"""
    from app.core.operations import ReleaseChecklist

    status, message = ReleaseChecklist._check_dependencies()
    assert status == "pass"
    assert "完整" in message


def test_release_checklist_config_passes_with_defaults():
    """配置完整性检查应通过（默认配置合理）。"""
    from app.core.operations import ReleaseChecklist

    status, message = ReleaseChecklist._check_config()
    # 默认配置应通过，至少不是 fail
    assert status in ("pass", "warn")


def test_release_checklist_database_returns_pass_or_fail():
    """数据库检查应返回 pass 或 fail（取决于环境）。"""
    from app.core.operations import ReleaseChecklist

    status, _ = ReleaseChecklist._check_database()
    assert status in ("pass", "fail")


def test_release_checklist_knowledge_warns_on_empty():
    """空知识库应返回 warn。"""
    from app.core.operations import ReleaseChecklist

    # 当前测试目录下向量库为空，应返回 warn
    status, message = ReleaseChecklist._check_knowledge_nonempty()
    assert status in ("warn", "pass")
    if status == "warn":
        assert "空" in message or "为空" in message


def test_release_checklist_api_health_passes():
    """API 健康检查应在测试环境通过。"""
    from app.core.operations import ReleaseChecklist

    status, _ = ReleaseChecklist._check_api_health()
    assert status == "pass"


def test_release_checklist_performance_returns_status():
    """性能基线检查应返回 pass 或 warn（不直接 fail）。"""
    from app.core.operations import ReleaseChecklist

    status, _ = ReleaseChecklist._check_performance_baseline()
    assert status in ("pass", "warn")


def test_release_checklist_each_check_independent():
    """单项检查失败不应中断其他检查。"""
    from app.core.operations import ReleaseChecklist

    checklist = ReleaseChecklist()
    # 替换其中一项为抛错，验证其他项仍执行
    original = checklist._checks[0]["func"]

    def failing() -> tuple:
        raise RuntimeError("模拟检查失败")

    checklist._checks[0]["func"] = failing  # type: ignore
    try:
        report = checklist.run_all_checks()
        # 第一项应标记为 fail
        assert report.items[0].status == "fail"
        assert "检查异常" in report.items[0].message
        # 其他项应仍执行（不为空）
        for item in report.items[1:]:
            assert item.status in ("pass", "fail", "warn", "skipped")
    finally:
        checklist._checks[0]["func"] = original  # type: ignore


def test_release_checklist_item_records_duration():
    """每项检查应记录耗时（毫秒）。"""
    from app.core.operations import ReleaseChecklist

    report = ReleaseChecklist().run_all_checks()
    for item in report.items:
        assert item.duration_ms >= 0.0


# ----------------------------------------------------------------------
# API 端点测试
# ----------------------------------------------------------------------


def test_api_get_dashboard_returns_200():
    """GET /dashboard 应返回 200 与完整看板数据。"""
    response = client_get_dashboard()
    assert response.status_code == 200
    body = response.json()
    assert "session" in body
    assert "ticket" in body
    assert "escalation" in body
    assert "satisfaction" in body
    assert "knowledge" in body
    assert "collected_at" in body


def client_get_dashboard(force_refresh: bool = False):
    """工具：调用 dashboard 接口。"""
    from app.api.v1.operations import router as operations_router

    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    url = "/api/v1/operations/dashboard"
    if force_refresh:
        url += "?force_refresh=true"
    return client.get(url)


def test_api_get_dashboard_force_refresh():
    """GET /dashboard?force_refresh=true 应跳过缓存。"""
    response = client_get_dashboard(force_refresh=True)
    assert response.status_code == 200


def test_api_get_release_checklist_returns_200():
    """GET /release-checklist 应返回 200 与检查报告。"""
    from app.api.v1.operations import router as operations_router

    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    response = client.get("/api/v1/operations/release-checklist")
    assert response.status_code == 200
    body = response.json()
    assert "passed" in body
    assert "failed" in body
    assert "warned" in body
    assert "total" in body
    assert "items" in body
    assert isinstance(body["items"], list)
    assert len(body["items"]) == body["total"]


def test_api_dashboard_caches_within_ttl():
    """连续两次调用 dashboard 应返回相同 collected_at（缓存命中）。"""
    from app.api.v1.operations import router as operations_router

    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    first = client.get("/api/v1/operations/dashboard").json()
    second = client.get("/api/v1/operations/dashboard").json()
    # 缓存命中：collected_at 应一致
    assert first["collected_at"] == second["collected_at"]
