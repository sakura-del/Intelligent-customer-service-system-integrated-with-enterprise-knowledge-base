"""灰度发布与 A/B 测试模块测试。

覆盖 ExperimentManager 的核心能力：
1. 实验创建、查询、列表、删除
2. 确定性分流：同一 user_id 始终进同组
3. 流量分配比例：按权重近似分布
4. 灰度渐进：5%/10%/50%/100% 配置
5. 指标记录与聚合统计（均值/标准差/样本数）
6. 降级：未启用/未配置/权重为 0 时进 control
7. 持久化：配置与指标写入 experiments.json
8. API 端点：创建/列表/结果/记录指标

测试隔离：使用独立 chroma 目录，模块级 fixture 重置单例。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 测试用独立持久化目录
TEST_PERSIST_DIR = "./tests/_chroma_data_experiment"


# ----------------------------------------------------------------------
# 模块级 fixture：隔离持久化目录与重置单例
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _isolate_persist_dir():
    """模块级 fixture：隔离实验配置文件目录并重置单例。"""
    from app.core import experiment as experiment_module
    from app.core.config import get_settings

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)
    persist_path.mkdir(parents=True, exist_ok=True)

    experiment_module.reset_experiment_manager()
    yield

    settings.CHROMA_PERSIST_DIR = original_persist_dir
    experiment_module.reset_experiment_manager()
    # 清理测试目录
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_per_test():
    """每个用例前重置实验单例，避免用例间状态污染。"""
    from app.core import experiment as experiment_module

    experiment_module.reset_experiment_manager()
    # 重置后再次清理目录残留文件
    store_path = Path(TEST_PERSIST_DIR) / "experiments.json"
    if store_path.exists():
        store_path.unlink()
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
# 工具函数与基础测试
# ----------------------------------------------------------------------


def _make_variants():
    """构造标准 control / treatment 分组。"""
    from app.schemas.operations import Variant

    return [
        Variant(name="control", weight=9),
        Variant(name="treatment", weight=1),
    ]


def test_create_experiment_returns_config():
    """创建实验应返回完整配置且默认启用。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    exp = manager.create_experiment(
        name="exp_basic",
        description="基础实验",
        variants=_make_variants(),
    )
    assert exp.name == "exp_basic"
    assert exp.enabled is True
    assert len(exp.variants) == 2
    assert exp.variants[0].name == "control"


def test_get_experiment_returns_none_when_not_exists():
    """查询不存在的实验应返回 None。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    assert manager.get_experiment("not_exists") is None


def test_list_experiments_returns_all():
    """列表应返回所有已创建实验。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("exp_a", "a", _make_variants())
    manager.create_experiment("exp_b", "b", _make_variants())
    names = [e.name for e in manager.list_experiments()]
    assert "exp_a" in names
    assert "exp_b" in names


def test_delete_experiment_removes_config_and_metrics():
    """删除实验应同时清理配置与指标。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("exp_del", "del", _make_variants())
    manager.record_metric("exp_del", "control", "ctr", 0.5)
    assert manager.delete_experiment("exp_del") is True
    assert manager.get_experiment("exp_del") is None
    # 删除已不存在的实验返回 False
    assert manager.delete_experiment("exp_del") is False


# ----------------------------------------------------------------------
# 确定性分流测试
# ----------------------------------------------------------------------


def test_assign_user_is_deterministic():
    """同一 user_id 多次分流应返回相同分组。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("exp_det", "确定性测试", _make_variants())
    first = manager.assign_user("exp_det", "user-001")
    for _ in range(10):
        assert manager.assign_user("exp_det", "user-001") == first


def test_assign_user_distributes_across_variants():
    """大量用户分流应同时覆盖 control 与 treatment 两个分组。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    # 50/50 分组保证覆盖两边
    from app.schemas.operations import Variant

    manager.create_experiment(
        "exp_dist",
        "分布测试",
        [Variant(name="control", weight=1), Variant(name="treatment", weight=1)],
    )
    counts = {"control": 0, "treatment": 0}
    for i in range(1000):
        variant = manager.assign_user("exp_dist", f"user-{i}")
        counts[variant] += 1
    # 两个分组都应有流量
    assert counts["control"] > 0
    assert counts["treatment"] > 0
    # 比例近似 50/50，允许 ±10% 误差
    assert 400 < counts["control"] < 600


def test_assign_user_respects_weight_ratio():
    """9:1 权重应使 control 流量约占 90%。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("exp_weight", "权重测试", _make_variants())
    counts = {"control": 0, "treatment": 0}
    for i in range(1000):
        counts[manager.assign_user("exp_weight", f"u-{i}")] += 1
    # 9:1 分组下 control 应占 85%~95%
    ratio = counts["control"] / 1000
    assert 0.85 < ratio < 0.95


# ----------------------------------------------------------------------
# 灰度渐进测试
# ----------------------------------------------------------------------


def test_grayscale_progression_5_percent():
    """5% 灰度：treatment 流量约占 5%。"""
    from app.core.experiment import get_experiment_manager
    from app.schemas.operations import Variant

    manager = get_experiment_manager()
    # 95:5 权重实现 5% 放量
    manager.create_experiment(
        "exp_gray_5",
        "5% 灰度",
        [Variant(name="control", weight=95), Variant(name="treatment", weight=5)],
    )
    treatment_count = 0
    for i in range(2000):
        if manager.assign_user("exp_gray_5", f"u-{i}") == "treatment":
            treatment_count += 1
    ratio = treatment_count / 2000
    # 允许 ±3% 误差
    assert 0.02 < ratio < 0.08


# ----------------------------------------------------------------------
# 降级测试
# ----------------------------------------------------------------------


def test_assign_user_returns_control_when_experiment_not_exists():
    """实验不存在时所有用户进 control。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    assert manager.assign_user("not_exists", "u1") == "control"


def test_assign_user_returns_control_when_disabled():
    """实验未启用时所有用户进 control。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment(
        "exp_disabled", "停用实验", _make_variants(), enabled=False
    )
    for i in range(50):
        assert manager.assign_user("exp_disabled", f"u-{i}") == "control"


def test_assign_user_returns_control_when_zero_weight():
    """所有分组权重为 0 时进 control。"""
    from app.core.experiment import get_experiment_manager
    from app.schemas.operations import Variant

    manager = get_experiment_manager()
    manager.create_experiment(
        "exp_zero",
        "零权重",
        [Variant(name="control", weight=0), Variant(name="treatment", weight=0)],
    )
    assert manager.assign_user("exp_zero", "u1") == "control"


def test_normalize_variants_ensures_control():
    """传入不含 control 的分组列表应自动补全 control。"""
    from app.core.experiment import ExperimentManager
    from app.schemas.operations import Variant

    normalized = ExperimentManager._normalize_variants(
        [Variant(name="treatment", weight=1)]
    )
    names = [v.name for v in normalized]
    assert "control" in names


# ----------------------------------------------------------------------
# 指标记录与统计测试
# ----------------------------------------------------------------------


def test_record_and_get_results():
    """记录指标后应能聚合返回均值/标准差/样本数。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("exp_metric", "指标测试", _make_variants())
    # control 组记录 ctr 指标
    for value in [0.1, 0.2, 0.3]:
        manager.record_metric("exp_metric", "control", "ctr", value)
    results = manager.get_results("exp_metric")
    assert results is not None
    assert "control" in results.metrics
    stats = results.metrics["control"]["ctr"]
    assert stats.sample_count == 3
    assert abs(stats.mean - 0.2) < 1e-6
    # 总体标准差：sqrt(((0.1-0.2)^2 + 0 + (0.3-0.2)^2) / 3) ≈ 0.0816
    assert stats.std > 0


def test_get_results_returns_none_when_not_exists():
    """查询不存在实验的结果应返回 None。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    assert manager.get_results("not_exists") is None


def test_get_results_empty_metrics_when_no_records():
    """已创建但无指标记录的实验应返回空 metrics。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("exp_empty_metric", "空指标", _make_variants())
    results = manager.get_results("exp_empty_metric")
    assert results is not None
    assert results.metrics == {}


def test_stats_single_sample_has_zero_std():
    """单样本标准差应为 0。"""
    from app.core.experiment import ExperimentManager

    stats = ExperimentManager._compute_stats([0.5])
    assert stats.sample_count == 1
    assert stats.mean == 0.5
    assert stats.std == 0.0


def test_stats_empty_list_returns_zero():
    """空列表应返回零值统计。"""
    from app.core.experiment import ExperimentManager

    stats = ExperimentManager._compute_stats([])
    assert stats.sample_count == 0
    assert stats.mean == 0.0
    assert stats.std == 0.0


# ----------------------------------------------------------------------
# 持久化测试
# ----------------------------------------------------------------------


def test_experiments_persisted_to_json():
    """创建实验后应写入 experiments.json 文件。"""
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("exp_persist", "持久化测试", _make_variants())
    store_path = Path(TEST_PERSIST_DIR) / "experiments.json"
    assert store_path.exists()
    import json

    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert "exp_persist" in data.get("experiments", {})


def test_experiments_loaded_from_json():
    """新实例应能从 experiments.json 加载已存实验。"""
    from app.core import experiment as experiment_module
    from app.core.experiment import ExperimentManager

    # 先写入一个实验
    manager1 = ExperimentManager(persist_dir=TEST_PERSIST_DIR)
    manager1.create_experiment("exp_load", "加载测试", _make_variants())
    # 模拟进程重启：新建实例应加载已有配置
    manager2 = ExperimentManager(persist_dir=TEST_PERSIST_DIR)
    exp = manager2.get_experiment("exp_load")
    assert exp is not None
    assert exp.name == "exp_load"


def test_corrupted_config_file_degrades_to_empty():
    """配置文件损坏时应降级为空字典而非抛错。"""
    from app.core.experiment import ExperimentManager

    store_path = Path(TEST_PERSIST_DIR) / "experiments.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    # 写入非法 JSON
    store_path.write_text("{ invalid json", encoding="utf-8")
    # 不应抛错，降级为空字典
    manager = ExperimentManager(persist_dir=TEST_PERSIST_DIR)
    assert manager.list_experiments() == []


# ----------------------------------------------------------------------
# API 端点测试
# ----------------------------------------------------------------------


def test_api_create_experiment():
    """POST /experiments 应创建实验并返回 201。"""
    from app.api.v1.operations import router as operations_router

    payload = {
        "name": "api_exp",
        "description": "API 创建",
        "variants": [
            {"name": "control", "weight": 9, "description": ""},
            {"name": "treatment", "weight": 1, "description": ""},
        ],
        "target_metrics": ["ctr"],
        "enabled": True,
    }
    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    response = client.post("/api/v1/operations/experiments", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "api_exp"


def test_api_list_experiments():
    """GET /experiments 应返回实验列表。"""
    from app.api.v1.operations import router as operations_router
    from app.core.experiment import get_experiment_manager

    get_experiment_manager().create_experiment("api_list_exp", "list", _make_variants())
    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    response = client.get("/api/v1/operations/experiments")
    assert response.status_code == 200
    names = [e["name"] for e in response.json()]
    assert "api_list_exp" in names


def test_api_get_results_returns_404_when_not_exists():
    """GET /experiments/{name}/results 不存在时应返回 404。"""
    from app.api.v1.operations import router as operations_router

    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    response = client.get("/api/v1/operations/experiments/not_exists/results")
    assert response.status_code == 404


def test_api_record_metric_returns_204():
    """POST /experiments/{name}/metrics 应返回 204。"""
    from app.api.v1.operations import router as operations_router
    from app.core.experiment import get_experiment_manager

    get_experiment_manager().create_experiment("api_metric_exp", "metric", _make_variants())
    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    response = client.post(
        "/api/v1/operations/experiments/api_metric_exp/metrics",
        json={"variant": "control", "metric_name": "ctr", "value": 0.42},
    )
    assert response.status_code == 204


def test_api_get_results_returns_aggregated():
    """GET /experiments/{name}/results 应返回聚合后的指标统计。"""
    from app.api.v1.operations import router as operations_router
    from app.core.experiment import get_experiment_manager

    manager = get_experiment_manager()
    manager.create_experiment("api_results_exp", "results", _make_variants())
    manager.record_metric("api_results_exp", "control", "ctr", 0.1)
    manager.record_metric("api_results_exp", "control", "ctr", 0.3)
    app = FastAPI()
    app.include_router(operations_router)
    client = TestClient(app)
    response = client.get(
        "/api/v1/operations/experiments/api_results_exp/results"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "api_results_exp"
    assert "control" in body["metrics"]
    assert body["metrics"]["control"]["ctr"]["sample_count"] == 2
