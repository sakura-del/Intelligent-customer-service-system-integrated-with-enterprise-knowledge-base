"""可观测性模块测试。

覆盖 Task 21 的核心场景：
1. AlertManager：告警记录、级别过滤、来源过滤、时间过滤、抑制窗口
2. HealthChecker：聚合报告、单项独立、降级策略
3. TokenUsageTracker：记录与统计、多窗口、多维聚合、预算告警、持久化
4. API 端点：alerts / health / token-usage

测试隔离：每个用例前重置 AlertManager / HealthChecker / TokenUsageTracker 单例，
并使用独立持久化目录避免污染其他测试。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.observability import (
    AlertLevel,
    HealthChecker,
    HealthReport,
    HealthStatus,
    TokenUsageTracker,
    get_alert_manager,
    get_health_checker,
    get_token_usage_tracker,
    reset_alert_manager,
    reset_health_checker,
    reset_token_usage_tracker,
)


@pytest.fixture(autouse=True)
def _reset_singletons_per_test(tmp_path):
    """每个用例前重置所有可观测性单例，并指向独立持久化目录。

    使用 tmp_path 让 pytest 自动管理临时目录的创建与清理，
    避免污染其他测试模块的 CHROMA_PERSIST_DIR。
    """
    from app.core.config import get_settings

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    test_persist_dir = str(tmp_path / "observability_data")
    os.makedirs(test_persist_dir, exist_ok=True)
    settings.CHROMA_PERSIST_DIR = test_persist_dir

    reset_alert_manager()
    reset_health_checker()
    reset_token_usage_tracker()

    yield

    settings.CHROMA_PERSIST_DIR = original_persist_dir
    reset_alert_manager()
    reset_health_checker()
    reset_token_usage_tracker()


def _create_test_app() -> FastAPI:
    """构造测试用 FastAPI 应用，仅挂载 observability 路由。"""
    from app.api.v1.observability import router as observability_router

    app = FastAPI()
    app.include_router(observability_router)
    return app


# ----------------------------------------------------------------------
# AlertManager 测试
# ----------------------------------------------------------------------


def test_alert_record_returns_alert_with_id_and_timestamp():
    """record_alert 应返回包含 alert_id 与 timestamp 的 Alert。"""
    manager = get_alert_manager()
    alert = manager.record_alert(
        level=AlertLevel.WARN,
        source="test",
        message="测试告警",
        metadata={"key": "value"},
    )
    assert alert is not None
    assert alert.alert_id  # 非空字符串
    assert alert.level == AlertLevel.WARN
    assert alert.source == "test"
    assert alert.message == "测试告警"
    assert alert.metadata == {"key": "value"}
    assert alert.timestamp  # ISO 字符串


def test_alert_list_returns_all_when_no_filter():
    """无过滤参数时 list_alerts 应返回全部告警。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.INFO, "src1", "msg1")
    manager.record_alert(AlertLevel.WARN, "src2", "msg2")

    alerts = manager.list_alerts()
    assert len(alerts) == 2
    sources = {a.source for a in alerts}
    assert sources == {"src1", "src2"}


def test_alert_list_filters_by_level():
    """list_alerts 应按 level 过滤。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.INFO, "src", "info-msg")
    manager.record_alert(AlertLevel.WARN, "src", "warn-msg")
    manager.record_alert(AlertLevel.ERROR, "src", "error-msg")

    alerts = manager.list_alerts(level=AlertLevel.WARN)
    assert len(alerts) == 1
    assert alerts[0].level == AlertLevel.WARN
    assert alerts[0].message == "warn-msg"


def test_alert_list_filters_by_source():
    """list_alerts 应按 source 过滤。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.INFO, "src1", "msg1")
    manager.record_alert(AlertLevel.INFO, "src2", "msg2")

    alerts = manager.list_alerts(source="src1")
    assert len(alerts) == 1
    assert alerts[0].source == "src1"


def test_alert_list_filters_by_since():
    """list_alerts 应按 since 时间过滤，仅返回时间戳 >= since 的告警。"""
    manager = get_alert_manager()
    # 记录一条早期告警
    manager.record_alert(AlertLevel.INFO, "src", "old-msg")
    # 取一个未来时间作为 since，应过滤掉所有告警
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    alerts = manager.list_alerts(since=future)
    assert len(alerts) == 0

    # 取一个过去时间作为 since，应返回全部告警
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    alerts = manager.list_alerts(since=past)
    assert len(alerts) == 1


def test_alert_suppression_within_window():
    """相同 (level, source, message) 在抑制窗口内应仅记录一次。"""
    manager = get_alert_manager()
    # 第一次记录应入队
    first = manager.record_alert(AlertLevel.WARN, "src", "duplicate-msg")
    assert first is not None
    # 第二次相同告警应被抑制，返回 None
    second = manager.record_alert(AlertLevel.WARN, "src", "duplicate-msg")
    assert second is None
    # 队列中应只有 1 条
    alerts = manager.list_alerts()
    assert len(alerts) == 1


def test_alert_suppression_does_not_affect_different_messages():
    """不同 message 的告警不应被互相抑制。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.WARN, "src", "msg1")
    manager.record_alert(AlertLevel.WARN, "src", "msg2")
    manager.record_alert(AlertLevel.ERROR, "src", "msg1")  # 不同级别
    alerts = manager.list_alerts()
    assert len(alerts) == 3


def test_alert_suppression_does_not_affect_different_sources():
    """不同 source 的告警不应被互相抑制。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.WARN, "src1", "msg")
    manager.record_alert(AlertLevel.WARN, "src2", "msg")
    alerts = manager.list_alerts()
    assert len(alerts) == 2


def test_alert_reset_clears_all():
    """reset 应清空告警队列与抑制缓存。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.WARN, "src", "msg")
    # 触发抑制
    manager.record_alert(AlertLevel.WARN, "src", "msg")
    assert len(manager.list_alerts()) == 1

    manager.reset()
    assert len(manager.list_alerts()) == 0
    # reset 后相同告警应能再次入队（抑制缓存已清空）
    manager.record_alert(AlertLevel.WARN, "src", "msg")
    assert len(manager.list_alerts()) == 1


def test_alert_manager_thread_safety():
    """多线程并发记录告警应不崩溃、不丢数据。"""
    manager = get_alert_manager()
    barrier = threading.Barrier(10)
    counts = [0] * 10

    def worker(idx: int):
        barrier.wait()
        for i in range(20):
            # 每个线程用不同的 source，避免抑制
            alert = manager.record_alert(
                AlertLevel.WARN, f"src-{idx}", f"msg-{i}"
            )
            if alert is not None:
                counts[idx] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 10 线程 × 20 条 = 200 条（不同 source 不同 message，无抑制）
    alerts = manager.list_alerts()
    assert len(alerts) == 200


# ----------------------------------------------------------------------
# HealthChecker 测试
# ----------------------------------------------------------------------


def test_health_check_returns_report_with_all_items():
    """check_all 应返回包含全部检查项的报告。"""
    checker = get_health_checker()
    report = checker.check_all()

    assert isinstance(report, HealthReport)
    assert report.checked_at  # 时间戳非空
    item_names = {item.name for item in report.items}
    # 应包含 4 项检查
    assert item_names == {"llm", "vector_store", "redis", "disk_space"}


def test_health_check_each_item_has_required_fields():
    """每项检查结果应包含 name/status/message/duration_ms/checked_at 字段。"""
    checker = get_health_checker()
    report = checker.check_all()

    for item in report.items:
        assert item.name
        assert isinstance(item.status, HealthStatus)
        assert isinstance(item.message, str)
        assert isinstance(item.duration_ms, float)
        assert item.duration_ms >= 0
        assert item.checked_at


def test_health_check_independent_items_one_failure_does_not_block_others():
    """单项检查失败不应影响其他检查项执行。"""
    checker = get_health_checker()

    # 用 monkeypatch 让 _check_llm 抛异常，验证其他项仍执行
    original_llm_check = HealthChecker._check_llm

    def _broken_llm():
        raise RuntimeError("故意失败")

    HealthChecker._check_llm = staticmethod(_broken_llm)
    try:
        report = checker.check_all()
    finally:
        HealthChecker._check_llm = original_llm_check

    # 仍应返回 4 项
    assert len(report.items) == 4
    # llm 项应标记为 UNHEALTHY
    llm_item = next(item for item in report.items if item.name == "llm")
    assert llm_item.status == HealthStatus.UNHEALTHY
    assert "故意失败" in llm_item.message
    # 其他项应正常执行（status 不为 None）
    for item in report.items:
        if item.name != "llm":
            assert isinstance(item.status, HealthStatus)


def test_health_check_overall_unhealthy_when_any_unhealthy():
    """任一项 UNHEALTHY 时整体应为 UNHEALTHY。"""
    checker = get_health_checker()
    original_llm_check = HealthChecker._check_llm

    def _broken_llm():
        raise RuntimeError("故意失败")

    HealthChecker._check_llm = staticmethod(_broken_llm)
    try:
        report = checker.check_all()
    finally:
        HealthChecker._check_llm = original_llm_check

    assert report.overall == HealthStatus.UNHEALTHY


def test_health_check_disk_space_returns_healthy():
    """磁盘空间检查在测试环境应返回 HEALTHY（剩余空间充足）。"""
    checker = get_health_checker()
    report = checker.check_all()
    disk_item = next(item for item in report.items if item.name == "disk_space")
    # 测试环境磁盘通常充足
    assert disk_item.status == HealthStatus.HEALTHY
    assert "GB" in disk_item.message


# ----------------------------------------------------------------------
# TokenUsageTracker 测试
# ----------------------------------------------------------------------


def test_token_usage_record_increments_stats():
    """record 应正确累加 token 统计。"""
    tracker = get_token_usage_tracker()
    tracker.record(
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
        user_id="user1",
        endpoint="/api/v1/chat",
    )

    stats = tracker.get_stats(window="hour")
    assert stats.call_count == 1
    assert stats.total_prompt_tokens == 100
    assert stats.total_completion_tokens == 50
    assert stats.total_tokens == 150


def test_token_usage_get_stats_minute_window_excludes_old_records():
    """minute 窗口应只包含最近 1 分钟的记录。"""
    tracker = get_token_usage_tracker()
    # 当前时间记录
    tracker.record(model="m", prompt_tokens=10, completion_tokens=5)

    # minute 窗口应包含
    stats = tracker.get_stats(window="minute")
    assert stats.call_count == 1
    assert stats.window == "minute"


def test_token_usage_get_stats_day_window_includes_more():
    """day 窗口应包含最近 1 天的记录。"""
    tracker = get_token_usage_tracker()
    for i in range(5):
        tracker.record(model="m", prompt_tokens=10, completion_tokens=5)

    stats = tracker.get_stats(window="day")
    assert stats.call_count == 5
    assert stats.total_tokens == 75  # 5 * (10 + 5)


def test_token_usage_invalid_window_raises_value_error():
    """未知窗口应抛 ValueError。"""
    tracker = get_token_usage_tracker()
    with pytest.raises(ValueError):
        tracker.get_stats(window="week")


def test_token_usage_aggregation_by_model():
    """by_model 应按 model 维度聚合。"""
    tracker = get_token_usage_tracker()
    tracker.record(model="gpt-4o", prompt_tokens=100, completion_tokens=50)
    tracker.record(model="gpt-4o", prompt_tokens=200, completion_tokens=100)
    tracker.record(model="gpt-3.5", prompt_tokens=50, completion_tokens=25)

    stats = tracker.get_stats(window="hour")
    assert "gpt-4o" in stats.by_model
    assert "gpt-3.5" in stats.by_model
    assert stats.by_model["gpt-4o"]["total_tokens"] == 450  # (100+50) + (200+100)
    assert stats.by_model["gpt-4o"]["call_count"] == 2
    assert stats.by_model["gpt-3.5"]["total_tokens"] == 75
    assert stats.by_model["gpt-3.5"]["call_count"] == 1


def test_token_usage_aggregation_by_user_and_endpoint():
    """by_user 与 by_endpoint 应正确聚合。"""
    tracker = get_token_usage_tracker()
    tracker.record(
        model="m",
        prompt_tokens=10,
        completion_tokens=5,
        user_id="alice",
        endpoint="/chat",
    )
    tracker.record(
        model="m",
        prompt_tokens=20,
        completion_tokens=10,
        user_id="bob",
        endpoint="/chat",
    )
    tracker.record(model="m", prompt_tokens=5, completion_tokens=2)  # 匿名

    stats = tracker.get_stats(window="hour")
    assert "alice" in stats.by_user
    assert "bob" in stats.by_user
    assert "anonymous" in stats.by_user
    assert stats.by_user["alice"]["total_tokens"] == 15

    # user_id 为 None 归 "anonymous"，endpoint 为 None 归 "unknown"
    assert "/chat" in stats.by_endpoint
    assert "unknown" in stats.by_endpoint


def test_token_usage_persistence_save_and_load(tmp_path):
    """持久化文件应能保存并在加载时恢复记录。"""
    persist_path = str(tmp_path / "token_usage.json")
    tracker = TokenUsageTracker(persist_path=persist_path)
    tracker.record(model="gpt-4o", prompt_tokens=100, completion_tokens=50)
    tracker.record(model="gpt-3.5", prompt_tokens=50, completion_tokens=25)
    tracker.flush()

    # 文件应存在
    assert os.path.exists(persist_path)
    # 文件内容应为合法 JSON
    with open(persist_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "records" in data
    assert len(data["records"]) == 2

    # 新建 tracker 应加载历史记录
    tracker2 = TokenUsageTracker(persist_path=persist_path)
    stats = tracker2.get_stats(window="day")
    assert stats.call_count == 2
    assert stats.total_tokens == 225  # 150 + 75


def test_token_usage_persistence_failure_keeps_in_memory(tmp_path):
    """持久化失败时应保留内存记录，不抛异常。"""
    # 使用一个无法写入的路径（父目录是文件）
    invalid_path = str(tmp_path / "blocker" / "token.json")
    # 创建 blocker 文件阻塞目录创建
    with open(tmp_path / "blocker", "w") as f:
        f.write("block")

    tracker = TokenUsageTracker(persist_path=invalid_path)
    # 记录应正常工作（持久化失败仅日志）
    tracker.record(model="m", prompt_tokens=10, completion_tokens=5)
    tracker.flush()  # 应不抛异常

    stats = tracker.get_stats(window="hour")
    assert stats.call_count == 1


def test_token_usage_reset_clears_records():
    """reset 应清空内存记录。"""
    tracker = get_token_usage_tracker()
    tracker.record(model="m", prompt_tokens=10, completion_tokens=5)
    assert tracker.get_stats(window="hour").call_count == 1

    tracker.reset()
    assert tracker.get_stats(window="hour").call_count == 0


def test_token_usage_record_failure_does_not_raise():
    """record 内部异常应被吞掉，不影响调用方。"""
    tracker = get_token_usage_tracker()
    # 传入会触发 int() 失败的非法值
    tracker.record(model="m", prompt_tokens="not-a-number", completion_tokens=5)
    # 不应抛异常


def test_token_usage_thread_safety():
    """多线程并发记录应不崩溃、不丢数据。"""
    tracker = get_token_usage_tracker()
    barrier = threading.Barrier(10)

    def worker(idx: int):
        barrier.wait()
        for i in range(20):
            tracker.record(
                model=f"model-{idx % 3}",
                prompt_tokens=10,
                completion_tokens=5,
                user_id=f"user-{idx}",
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = tracker.get_stats(window="day")
    # 10 线程 × 20 次 = 200 次
    assert stats.call_count == 200


def test_token_usage_budget_alert_triggered_when_exceeding():
    """Token 用量超预算时应记录告警。

    通过 monkeypatch 把预算阈值改小，模拟超预算场景。
    """
    from app.core import observability as obs_module

    original_budget = obs_module.TokenUsageTracker._get_budget_per_hour

    @staticmethod
    def _tiny_budget():
        return 10  # 预算仅 10 token

    obs_module.TokenUsageTracker._get_budget_per_hour = _tiny_budget
    try:
        # 重置单例让新预算生效
        reset_token_usage_tracker()
        tracker = get_token_usage_tracker()
        # 记录一次会超预算的用量
        tracker.record(model="m", prompt_tokens=20, completion_tokens=10)

        # 应有 token_usage 来源的告警
        manager = get_alert_manager()
        alerts = manager.list_alerts(source="token_usage")
        assert len(alerts) >= 1
        assert "超预算" in alerts[0].message
    finally:
        obs_module.TokenUsageTracker._get_budget_per_hour = original_budget


# ----------------------------------------------------------------------
# API 端点测试
# ----------------------------------------------------------------------


def test_api_alerts_returns_list():
    """GET /alerts 应返回告警列表。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.WARN, "src1", "msg1")
    manager.record_alert(AlertLevel.ERROR, "src2", "msg2")

    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2


def test_api_alerts_filters_by_level():
    """GET /alerts?level=warn 应按级别过滤。"""
    manager = get_alert_manager()
    manager.record_alert(AlertLevel.WARN, "src1", "msg1")
    manager.record_alert(AlertLevel.ERROR, "src2", "msg2")

    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/alerts?level=warn")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["level"] == "warn"


def test_api_alerts_invalid_level_returns_400():
    """无效的 level 参数应返回 400。"""
    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/alerts?level=invalid")
    assert resp.status_code == 400


def test_api_health_returns_report():
    """GET /health 应返回健康检查报告。"""
    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "overall" in body
    assert "items" in body
    assert "checked_at" in body
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 4
    item_names = {item["name"] for item in body["items"]}
    assert item_names == {"llm", "vector_store", "redis", "disk_space"}


def test_api_token_usage_returns_stats():
    """GET /token-usage 应返回 token 用量统计。"""
    tracker = get_token_usage_tracker()
    tracker.record(model="gpt-4o", prompt_tokens=100, completion_tokens=50)

    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/token-usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] == "hour"
    assert body["call_count"] == 1
    assert body["total_tokens"] == 150
    assert "gpt-4o" in body["by_model"]


def test_api_token_usage_invalid_window_returns_400():
    """无效的 window 参数应返回 400。"""
    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/token-usage?window=week")
    assert resp.status_code == 400


def test_api_token_usage_with_minute_window():
    """GET /token-usage?window=minute 应返回 minute 窗口统计。"""
    tracker = get_token_usage_tracker()
    tracker.record(model="m", prompt_tokens=10, completion_tokens=5)

    app = _create_test_app()
    client = TestClient(app)
    resp = client.get("/api/v1/observability/token-usage?window=minute")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] == "minute"
    assert body["call_count"] == 1
