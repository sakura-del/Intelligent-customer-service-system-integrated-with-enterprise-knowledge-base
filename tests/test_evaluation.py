"""检索效果评测模块测试。

覆盖 EvaluationRunner 的测试集加载、各指标计算、报告持久化、列表查询，
以及空库降级、外部测试集加载失败降级等场景。

测试隔离：使用独立 chroma 目录，模块级 fixture 入库少量文档作为评测基础。

测试用例：
1. 默认测试集加载
2. 外部测试集加载
3. 外部测试集加载失败降级
4. 各指标计算（Recall/Precision/Hit/MRR/Hallucination）
5. 报告持久化
6. 报告列表查询
7. 单个报告查询
8. 不存在报告查询返回 None
9. 空库降级返回全零指标
10. 检索异常记为 miss
11. 单次评测内检索结果缓存
12. API 端点 run/reports/{report_id}
13. 内置 DEFAULT_TESTSET 至少 30 条
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 测试用独立持久化目录
TEST_PERSIST_DIR = "./tests/_chroma_data_eval"
SAMPLE_DIR = Path(__file__).parent / "sample_data"
SAMPLE_FAQ = SAMPLE_DIR / "faq.md"
SAMPLE_POLICY = SAMPLE_DIR / "return_policy.md"
SAMPLE_MANUAL = SAMPLE_DIR / "product_manual.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_ingest():
    """模块级 fixture：隔离 ChromaDB 目录并入库三份测试文档作为评测基础。"""
    from app.core.config import get_settings
    from app.knowledge import (
        bm25 as bm25_module,
    )
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        evaluation as evaluation_module,
    )
    from app.knowledge import (
        hybrid_retriever as hybrid_module,
    )
    from app.knowledge import (
        retrieval_tuner as tuner_module,
    )
    from app.knowledge import (
        reranker as reranker_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )
    from app.knowledge.pipeline import ingest_document

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_threshold = settings.SIMILARITY_THRESHOLD
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # fallback 模式下 hash 向量无语义能力，阈值降到 0 让召回阶段不过滤
    embedding_service = embeddings_module.get_embedding_service()
    if embedding_service.mode == "fallback":
        settings.SIMILARITY_THRESHOLD = 0.0

    # 重置所有相关单例
    vectorstore_module.reset_vector_store()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    reranker_module.reset_reranker()
    tuner_module.reset_retrieval_tuner()
    evaluation_module.reset_evaluation_runner()

    # 入库三份测试文档
    for sample_path, knowledge_type in [
        (SAMPLE_FAQ, "faq"),
        (SAMPLE_MANUAL, "doc"),
        (SAMPLE_POLICY, "policy"),
    ]:
        result = ingest_document(
            sample_path,
            metadata={"knowledge_type": knowledge_type},
        )
        assert result.error is None, f"入库 {sample_path.name} 失败：{result.error}"
        assert result.total_chunks > 0

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.SIMILARITY_THRESHOLD = original_threshold
    vectorstore_module.reset_vector_store()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    reranker_module.reset_reranker()
    tuner_module.reset_retrieval_tuner()
    evaluation_module.reset_evaluation_runner()

    # 重置 KnowledgeAgent 单例
    from app.agents import knowledge_agent as agent_module
    from app.agents import llm_client as llm_client_module

    agent_module.reset_knowledge_agent()
    llm_client_module.reset_llm_client()


@pytest.fixture()
def app_with_routers():
    """提供注册了 evaluation 与 tuner 路由的 FastAPI 应用。"""
    from app.api.v1.evaluation import router as evaluation_router
    from app.api.v1.tuner import router as tuner_router

    app = FastAPI()
    app.include_router(evaluation_router)
    app.include_router(tuner_router)
    return app


@pytest.fixture()
def client(app_with_routers):
    """提供 TestClient。"""
    return TestClient(app_with_routers)


# ----------------------------------------------------------------------
# 默认测试集与加载测试
# ----------------------------------------------------------------------


def test_default_testset_has_at_least_30_cases():
    """内置默认测试集应至少 30 条用例。"""
    from app.knowledge.evaluation import DEFAULT_TESTSET

    assert len(DEFAULT_TESTSET.cases) >= 30, (
        f"默认测试集应至少 30 条，实际 {len(DEFAULT_TESTSET.cases)} 条"
    )


def test_default_testset_covers_scenarios():
    """默认测试集应覆盖知识问答/订单/退货/会员/账户等场景。"""
    from app.knowledge.evaluation import DEFAULT_TESTSET

    queries_text = " ".join(c.query for c in DEFAULT_TESTSET.cases)
    # 覆盖关键场景关键词
    assert "密码" in queries_text, "应覆盖账户登录场景"
    assert "订单" in queries_text, "应覆盖订单场景"
    assert "退货" in queries_text, "应覆盖退货场景"
    assert "会员" in queries_text, "应覆盖会员场景"
    assert "积分" in queries_text, "应覆盖积分场景"
    # 应包含不应命中的用例（验证幻觉率）
    should_not_hit = [c for c in DEFAULT_TESTSET.cases if not c.should_hit]
    assert len(should_not_hit) >= 2, "应至少 2 条不应命中用例"


def test_load_testset_returns_default_when_no_path():
    """无 path 时应返回内置默认测试集。"""
    from app.knowledge.evaluation import get_evaluation_runner

    runner = get_evaluation_runner()
    testset = runner.load_testset(None)
    assert len(testset.cases) >= 30


def test_load_testset_loads_external_json(tmp_path: Path):
    """应能加载外部 JSON 测试集。"""
    from app.knowledge.evaluation import (
        TestCase,
        TestSet,
        get_evaluation_runner,
    )

    custom = TestSet(
        cases=[
            TestCase(query="测试查询1", expected_sources=["faq.md"]),
            TestCase(query="测试查询2", expected_sources=[]),
        ],
        meta={"version": "test"},
    )
    path = tmp_path / "custom_testset.json"
    path.write_text(custom.model_dump_json(), encoding="utf-8")

    runner = get_evaluation_runner()
    testset = runner.load_testset(str(path))

    assert len(testset.cases) == 2
    assert testset.cases[0].query == "测试查询1"


def test_load_testset_falls_back_on_invalid_path():
    """无效路径应降级到默认测试集。"""
    from app.knowledge.evaluation import get_evaluation_runner

    runner = get_evaluation_runner()
    testset = runner.load_testset("/nonexistent/path/testset.json")

    # 应降级到默认集
    assert len(testset.cases) >= 30


def test_load_testset_falls_back_on_invalid_json(tmp_path: Path):
    """无效 JSON 应降级到默认测试集。"""
    from app.knowledge.evaluation import get_evaluation_runner

    path = tmp_path / "invalid.json"
    path.write_text("not a valid json", encoding="utf-8")

    runner = get_evaluation_runner()
    testset = runner.load_testset(str(path))

    assert len(testset.cases) >= 30


# ----------------------------------------------------------------------
# 评测指标计算测试
# ----------------------------------------------------------------------


def test_run_evaluation_returns_report_with_metrics():
    """run 应返回包含完整指标的 EvaluationReport。"""
    from app.knowledge.evaluation import get_evaluation_runner

    runner = get_evaluation_runner()
    report = runner.run(top_k=5)

    assert report.total_queries > 0
    assert 0.0 <= report.recall_at_k <= 1.0
    assert 0.0 <= report.precision_at_k <= 1.0
    assert 0.0 <= report.hit_rate <= 1.0
    assert 0.0 <= report.mrr <= 1.0
    assert 0.0 <= report.hallucination_rate <= 1.0
    assert report.duration_seconds > 0
    assert len(report.case_details) == report.total_queries


def test_run_evaluation_with_custom_testset():
    """run 应支持传入自定义测试集。"""
    from app.knowledge.evaluation import (
        TestCase,
        TestSet,
        get_evaluation_runner,
    )

    custom = TestSet(
        cases=[
            TestCase(
                query="忘记登录密码怎么办？",
                expected_sources=["faq.md"],
                should_hit=True,
            ),
            TestCase(
                query="量子力学波函数坍缩原理",
                expected_sources=[],
                should_hit=False,
            ),
        ]
    )
    runner = get_evaluation_runner()
    report = runner.run(testset=custom, top_k=5)

    assert report.total_queries == 2
    assert len(report.case_details) == 2


def test_metrics_calculation_with_known_results():
    """用已知结果验证指标计算逻辑。"""
    from app.knowledge.evaluation import CaseDetail, EvaluationRunner

    # 构造已知结果：3 条应命中用例，2 条命中 1 条未命中
    details = [
        CaseDetail(
            query="q1",
            should_hit=True,
            hit=True,
            first_relevant_rank=1,
            relevant_count=2,
        ),
        CaseDetail(
            query="q2",
            should_hit=True,
            hit=True,
            first_relevant_rank=3,
            relevant_count=1,
        ),
        CaseDetail(
            query="q3",
            should_hit=True,
            hit=False,
            first_relevant_rank=-1,
            relevant_count=0,
        ),
        CaseDetail(
            query="q4",
            should_hit=False,
            hit=True,  # 不应命中但命中 -> 幻觉
            first_relevant_rank=-1,
            relevant_count=0,
        ),
        CaseDetail(
            query="q5",
            should_hit=False,
            hit=False,
            first_relevant_rank=-1,
            relevant_count=0,
        ),
    ]
    metrics = EvaluationRunner._compute_metrics(details, top_k=5)

    # Recall@K：应命中 3 条中 2 条命中 = 2/3
    assert abs(metrics["recall_at_k"] - 2 / 3) < 0.01
    # Hit Rate：5 条中 3 条命中（q1/q2/q4）= 3/5
    assert abs(metrics["hit_rate"] - 3 / 5) < 0.01
    # MRR：应命中用例中 (1/1 + 1/3) / 3 = 4/9 ≈ 0.4444
    assert abs(metrics["mrr"] - (1.0 / 1 + 1.0 / 3) / 3) < 0.01
    # Hallucination Rate：不应命中 2 条中 1 条命中 = 1/2
    assert abs(metrics["hallucination_rate"] - 0.5) < 0.01


def test_metrics_zero_on_empty_details():
    """无用例时指标应全零。"""
    from app.knowledge.evaluation import CaseDetail, EvaluationRunner

    metrics = EvaluationRunner._compute_metrics([], top_k=5)
    assert metrics["recall_at_k"] == 0.0
    assert metrics["precision_at_k"] == 0.0
    assert metrics["hit_rate"] == 0.0
    assert metrics["mrr"] == 0.0
    assert metrics["hallucination_rate"] == 0.0


def test_is_relevant_substring_match():
    """_is_relevant 应支持子串匹配。"""
    from app.knowledge.evaluation import EvaluationRunner
    from app.schemas.knowledge import RetrievedChunk

    # 完全匹配
    chunk = RetrievedChunk(text="x", source="faq.md")
    assert EvaluationRunner._is_relevant(chunk, ["faq.md"])
    # 子串匹配
    chunk = RetrievedChunk(text="x", source="/path/to/faq.md")
    assert EvaluationRunner._is_relevant(chunk, ["faq.md"])
    # 不匹配
    chunk = RetrievedChunk(text="x", source="other.md")
    assert not EvaluationRunner._is_relevant(chunk, ["faq.md"])
    # 空期望来源
    chunk = RetrievedChunk(text="x", source="faq.md")
    assert not EvaluationRunner._is_relevant(chunk, [])


# ----------------------------------------------------------------------
# 报告持久化与查询测试
# ----------------------------------------------------------------------


def test_report_persisted_to_disk():
    """评测后报告应持久化到 evaluation_reports/ 目录。"""
    from app.core.config import get_settings
    from app.knowledge.evaluation import (
        TestCase,
        TestSet,
        get_evaluation_runner,
    )

    runner = get_evaluation_runner()
    custom = TestSet(
        cases=[TestCase(query="忘记登录密码怎么办？", expected_sources=["faq.md"])]
    )
    report = runner.run(testset=custom, top_k=5)

    settings = get_settings()
    report_path = Path(settings.CHROMA_PERSIST_DIR) / "evaluation_reports" / f"{report.report_id}.json"
    assert report_path.exists(), "报告文件应存在"

    # 文件内容应为合法 JSON
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["report_id"] == report.report_id


def test_list_reports_returns_summaries():
    """list_reports 应返回报告摘要列表。"""
    from app.knowledge.evaluation import (
        TestCase,
        TestSet,
        get_evaluation_runner,
    )

    runner = get_evaluation_runner()
    custom = TestSet(
        cases=[TestCase(query="测试列表查询", expected_sources=["faq.md"])]
    )
    runner.run(testset=custom, top_k=3)

    summaries = runner.list_reports()
    assert len(summaries) >= 1
    # 每条摘要应包含必要字段
    first = summaries[0]
    assert "report_id" in first
    assert "created_at" in first
    assert "total_queries" in first
    assert "recall_at_k" in first


def test_get_report_returns_full_report():
    """get_report 应返回完整报告详情。"""
    from app.knowledge.evaluation import (
        TestCase,
        TestSet,
        get_evaluation_runner,
    )

    runner = get_evaluation_runner()
    custom = TestSet(
        cases=[TestCase(query="测试详情查询", expected_sources=["faq.md"])]
    )
    report = runner.run(testset=custom, top_k=3)

    fetched = runner.get_report(report.report_id)
    assert fetched is not None
    assert fetched.report_id == report.report_id
    assert fetched.total_queries == 1
    assert len(fetched.case_details) == 1


def test_get_report_returns_none_for_unknown_id():
    """查询不存在的 report_id 应返回 None。"""
    from app.knowledge.evaluation import get_evaluation_runner

    runner = get_evaluation_runner()
    assert runner.get_report("nonexistent_id_12345") is None


# ----------------------------------------------------------------------
# 降级策略测试
# ----------------------------------------------------------------------


def test_evaluation_handles_retrieval_exception_as_miss():
    """检索异常时应记为 miss，不中断整体评测。"""
    from app.knowledge.evaluation import (
        EvaluationRunner,
        TestCase,
        TestSet,
    )
    from app.schemas.knowledge import RetrievedChunk

    # 构造一个会抛异常的 mock retriever
    class FailingRunner(EvaluationRunner):
        def _retrieve(self, query: str, top_k: int):
            raise RuntimeError("模拟检索失败")

    runner = FailingRunner()
    testset = TestSet(
        cases=[
            TestCase(query="q1", expected_sources=["faq.md"], should_hit=True),
            TestCase(query="q2", expected_sources=[], should_hit=False),
        ]
    )
    report = runner.run(testset=testset, top_k=5)

    # 检索异常应记为 miss，不中断评测
    assert report.total_queries == 2
    # 所有应命中用例都应未命中
    for detail in report.case_details:
        if detail.should_hit:
            assert detail.hit is False
            assert detail.error is not None


def test_evaluation_caches_retrieval_results_within_run():
    """单次评测内相同 query 应缓存检索结果，避免重复计算。"""
    from app.knowledge.evaluation import (
        EvaluationRunner,
        TestCase,
        TestSet,
    )
    from app.schemas.knowledge import RetrievedChunk

    # 计数器追踪检索调用次数
    call_count = {"count": 0}

    class CountingRunner(EvaluationRunner):
        def _retrieve(self, query: str, top_k: int):
            call_count["count"] += 1
            return [RetrievedChunk(text="x", source="faq.md", score=0.9)]

    runner = CountingRunner()
    # 构造两条相同 query 的用例
    testset = TestSet(
        cases=[
            TestCase(query="相同查询", expected_sources=["faq.md"]),
            TestCase(query="相同查询", expected_sources=["faq.md"]),
            TestCase(query="不同查询", expected_sources=["faq.md"]),
        ]
    )
    runner.run(testset=testset, top_k=5)

    # 3 条用例但只有 2 个不同 query，检索应只调用 2 次
    assert call_count["count"] == 2


# ----------------------------------------------------------------------
# API 端点测试
# ----------------------------------------------------------------------


def test_api_run_evaluation_returns_report(client: TestClient):
    """POST /api/v1/evaluation/run 应返回评测报告。"""
    response = client.post(
        "/api/v1/evaluation/run",
        json={"top_k": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert "report_id" in body
    assert "total_queries" in body
    assert "recall_at_k" in body
    assert body["source"] == "default"
    assert body["total_queries"] >= 30  # 默认测试集


def test_api_list_reports_returns_array(client: TestClient):
    """GET /api/v1/evaluation/reports 应返回报告列表。"""
    # 先触发一次评测
    client.post("/api/v1/evaluation/run", json={"top_k": 3})

    response = client.get("/api/v1/evaluation/reports")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) >= 1


def test_api_get_report_detail(client: TestClient):
    """GET /api/v1/evaluation/reports/{report_id} 应返回报告详情。"""
    run_resp = client.post("/api/v1/evaluation/run", json={"top_k": 3})
    report_id = run_resp.json()["report_id"]

    response = client.get(f"/api/v1/evaluation/reports/{report_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["report_id"] == report_id
    assert "case_details" in body


def test_api_get_unknown_report_returns_404(client: TestClient):
    """查询不存在的 report_id 应返回 404。"""
    response = client.get("/api/v1/evaluation/reports/nonexistent_id")
    assert response.status_code == 404


def test_api_run_with_external_testset(client: TestClient, tmp_path: Path):
    """POST /run 应支持外部测试集。"""
    from app.knowledge.evaluation import TestCase, TestSet

    custom = TestSet(
        cases=[
            TestCase(query="忘记登录密码怎么办？", expected_sources=["faq.md"]),
        ]
    )
    path = tmp_path / "external_testset.json"
    path.write_text(custom.model_dump_json(), encoding="utf-8")

    response = client.post(
        "/api/v1/evaluation/run",
        json={"testset_path": str(path), "top_k": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_queries"] == 1
    assert body["source"] == "external"


# ----------------------------------------------------------------------
# 高层 API 测试
# ----------------------------------------------------------------------


def test_run_evaluation_high_level_api():
    """run_evaluation 高层 API 应能正常执行评测。"""
    from app.knowledge.evaluation import run_evaluation

    report = run_evaluation(top_k=5)
    assert report.total_queries >= 30
    assert report.source == "default"
