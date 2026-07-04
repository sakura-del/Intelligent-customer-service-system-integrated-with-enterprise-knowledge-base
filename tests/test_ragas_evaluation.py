"""RAGAS 生成质量评估测试。

覆盖 RagasEvaluator 的测试集加载、评测执行、报告持久化、列表查询，
以及 ragas 不可用、LLM_API_KEY 未配置等降级场景。

测试隔离：
- 使用独立 chroma 目录避免污染正式环境；
- mock RAGAgent 避免真实检索与 LLM 调用；
- mock RAGAS 评分避免真实 ragas 依赖。

测试用例：
1. 数据模型：RagasTestCase / RagasEvaluationReport / RagasRunRequest
2. 测试集加载：默认集 / 外部 JSON / 失败降级
3. 评估器核心：run / list_reports / get_report（含不存在 ID）
4. 降级策略：ragas 未安装 / LLM_API_KEY 为空
5. 报告持久化：文件保存 / 反序列化
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 测试用独立持久化目录，与 test_evaluation.py 隔离避免互相污染
TEST_PERSIST_DIR = "./tests/_chroma_data_ragas"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_reset():
    """模块级 fixture：隔离 ChromaDB 目录并重置 RAGAS 评估器单例。

    与 test_evaluation.py 类似的隔离模式：替换 CHROMA_PERSIST_DIR 到
    测试专用目录，并在结束后恢复配置、清理单例，避免污染正式环境。
    """
    from app.core.config import get_settings
    from app.knowledge import ragas_evaluator as ragas_module

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留，确保测试从干净状态开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置 RAGAS 评估器单例，确保使用新的持久化目录
    ragas_module.reset_ragas_evaluator()

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    ragas_module.reset_ragas_evaluator()


@pytest.fixture()
def mock_rag_agent():
    """提供 mock RAGAgent，避免真实检索与 LLM 调用。

    mock 的 answer 方法返回固定 RAGAnswer（含 answer 文本与 retrieved_chunks），
    便于测试 _collect_samples 与 run 的完整链路而不依赖真实知识库。
    """
    from app.schemas.chat import RAGAnswer
    from app.schemas.knowledge import RetrievedChunk

    mock_agent = MagicMock()

    def mock_answer(question, top_k=None, **kwargs):
        # 返回固定结构化的 RAGAnswer，便于后续断言
        return RAGAnswer(
            answer=f"这是对「{question}」的模拟回答。",
            sources=["faq.md"],
            retrieved_chunks=[
                RetrievedChunk(
                    text="模拟上下文片段", source="faq.md", score=0.9
                )
            ],
            confidence=0.9,
            hit=True,
        )

    mock_agent.answer = mock_answer

    # patch 延迟导入入口：_collect_samples 内部 from app.agents.rag_agent import get_rag_agent
    with patch(
        "app.agents.rag_agent.get_rag_agent", return_value=mock_agent
    ):
        yield mock_agent


@pytest.fixture()
def mock_ragas_metrics():
    """mock RAGAS 评分返回固定非零指标，避免真实 ragas 调用。

    返回固定指标 0.8/0.7/0.6/0.5，便于断言聚合逻辑；
    降级场景由专门测试覆盖，此处只验证正常链路。
    """
    from app.knowledge.ragas_evaluator import RagasEvaluator

    fixed_metrics = {
        "faithfulness": 0.8,
        "answer_relevancy": 0.7,
        "context_precision": 0.6,
        "context_recall": 0.5,
    }

    def mock_compute(samples):
        # 每条样本返回相同的固定指标，聚合后仍为该值
        return [dict(fixed_metrics) for _ in samples]

    # 用 MagicMock 替换实例方法：调用 self._compute_ragas_metrics(samples)
    # 实际触发 mock_compute(samples)，不绑定 self
    mock = MagicMock(side_effect=mock_compute)
    with patch.object(RagasEvaluator, "_compute_ragas_metrics", mock):
        yield


# ----------------------------------------------------------------------
# 数据模型测试
# ----------------------------------------------------------------------


def test_ragas_test_case_creation_and_validation():
    """RagasTestCase 应正确创建并校验必填字段。"""
    from pydantic import ValidationError

    from app.schemas.ragas_evaluation import RagasTestCase

    case = RagasTestCase(question="测试问题", ground_truth="标准答案")
    assert case.question == "测试问题"
    assert case.ground_truth == "标准答案"

    # 缺失必填字段应抛 ValidationError
    with pytest.raises(ValidationError):
        RagasTestCase(question="只有问题")  # 缺 ground_truth
    with pytest.raises(ValidationError):
        RagasTestCase(ground_truth="只有答案")  # 缺 question


def test_ragas_evaluation_report_serialization():
    """RagasEvaluationReport 应支持序列化与反序列化。"""
    from app.schemas.ragas_evaluation import (
        RagasCaseDetail,
        RagasEvaluationReport,
    )

    report = RagasEvaluationReport(
        report_id="test_001",
        created_at="2026-07-04T10:00:00",
        total_queries=2,
        faithfulness=0.8,
        answer_relevancy=0.7,
        context_precision=0.6,
        context_recall=0.5,
        duration_seconds=1.23,
        source="default",
        case_details=[
            RagasCaseDetail(question="q1", ground_truth="g1", answer="a1"),
            RagasCaseDetail(question="q2", ground_truth="g2", answer="a2"),
        ],
    )

    # 序列化为 JSON 后再解析
    json_str = report.model_dump_json()
    payload = json.loads(json_str)
    assert payload["report_id"] == "test_001"
    assert payload["faithfulness"] == 0.8
    assert len(payload["case_details"]) == 2

    # 反序列化还原为模型实例
    restored = RagasEvaluationReport(**payload)
    assert restored.report_id == report.report_id
    assert restored.faithfulness == report.faithfulness
    assert len(restored.case_details) == 2
    assert restored.case_details[0].question == "q1"


def test_ragas_run_request_defaults():
    """RagasRunRequest 默认 testset_path 与 top_k 均为 None。"""
    from app.schemas.ragas_evaluation import RagasRunRequest

    req = RagasRunRequest()
    assert req.testset_path is None
    assert req.top_k is None

    # 显式传值应正常
    req2 = RagasRunRequest(testset_path="/tmp/x.json", top_k=5)
    assert req2.testset_path == "/tmp/x.json"
    assert req2.top_k == 5


# ----------------------------------------------------------------------
# 测试集加载测试
# ----------------------------------------------------------------------


def test_load_testset_returns_default_when_no_path():
    """无 path 时应返回内置默认 RAGAS 测试集。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator

    evaluator = get_ragas_evaluator()
    testset = evaluator.load_testset(None)

    # 内置默认集至少 10 条
    assert len(testset.cases) >= 10
    # 每条用例应含 question 与 ground_truth
    for case in testset.cases:
        assert case.question
        assert case.ground_truth


def test_load_testset_loads_external_json(tmp_path: Path):
    """应能加载外部 JSON 测试集。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import RagasTestCase, RagasTestSet

    custom = RagasTestSet(
        cases=[
            RagasTestCase(question="外部问题1", ground_truth="外部答案1"),
            RagasTestCase(question="外部问题2", ground_truth="外部答案2"),
        ],
        meta={"version": "custom"},
    )
    path = tmp_path / "custom_ragas.json"
    path.write_text(custom.model_dump_json(), encoding="utf-8")

    evaluator = get_ragas_evaluator()
    testset = evaluator.load_testset(str(path))

    assert len(testset.cases) == 2
    assert testset.cases[0].question == "外部问题1"
    assert testset.cases[1].ground_truth == "外部答案2"
    # load_testset 应合并 source 字段到 meta
    assert "source" in testset.meta


def test_load_testset_falls_back_on_invalid_path():
    """无效路径应降级到默认测试集。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator

    evaluator = get_ragas_evaluator()
    testset = evaluator.load_testset("/nonexistent/path/ragas.json")

    # 应降级到默认集
    assert len(testset.cases) >= 10


def test_load_testset_falls_back_on_invalid_json(tmp_path: Path):
    """无效 JSON 应降级到默认测试集。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator

    path = tmp_path / "invalid.json"
    path.write_text("not a valid json", encoding="utf-8")

    evaluator = get_ragas_evaluator()
    testset = evaluator.load_testset(str(path))

    assert len(testset.cases) >= 10


# ----------------------------------------------------------------------
# 评估器核心功能测试
# ----------------------------------------------------------------------


def test_run_returns_report_with_four_metrics(
    mock_rag_agent, mock_ragas_metrics
):
    """run 应返回包含四项核心指标的报告。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import RagasTestCase, RagasTestSet

    custom = RagasTestSet(
        cases=[
            RagasTestCase(question="问题1", ground_truth="答案1"),
            RagasTestCase(question="问题2", ground_truth="答案2"),
        ]
    )
    evaluator = get_ragas_evaluator()
    report = evaluator.run(testset=custom, top_k=3)

    assert report.total_queries == 2
    # 四项指标应在 [0, 1] 范围
    assert 0.0 <= report.faithfulness <= 1.0
    assert 0.0 <= report.answer_relevancy <= 1.0
    assert 0.0 <= report.context_precision <= 1.0
    assert 0.0 <= report.context_recall <= 1.0
    # mock 链路极快，duration 经 round(,4) 可能取整为 0，断言非负即可
    assert report.duration_seconds >= 0
    assert len(report.case_details) == 2
    # 每条详情应填充 question/answer/contexts
    for detail in report.case_details:
        assert detail.question
        assert detail.answer  # mock agent 返回非空 answer
        assert len(detail.contexts) > 0  # mock agent 返回 1 条 context


def test_list_reports_returns_summaries(mock_rag_agent, mock_ragas_metrics):
    """list_reports 应返回历史报告摘要列表。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import RagasTestCase, RagasTestSet

    evaluator = get_ragas_evaluator()
    custom = RagasTestSet(
        cases=[RagasTestCase(question="列表查询", ground_truth="答案")]
    )
    evaluator.run(testset=custom, top_k=3)

    summaries = evaluator.list_reports()
    assert len(summaries) >= 1
    # 每条摘要应包含必要字段
    first = summaries[0]
    assert "report_id" in first
    assert "created_at" in first
    assert "total_queries" in first
    assert "faithfulness" in first
    assert "answer_relevancy" in first
    assert "context_precision" in first
    assert "context_recall" in first


def test_get_report_returns_full_report(
    mock_rag_agent, mock_ragas_metrics
):
    """get_report 应返回完整报告详情。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import RagasTestCase, RagasTestSet

    evaluator = get_ragas_evaluator()
    custom = RagasTestSet(
        cases=[RagasTestCase(question="详情查询", ground_truth="答案")]
    )
    report = evaluator.run(testset=custom, top_k=3)

    fetched = evaluator.get_report(report.report_id)
    assert fetched is not None
    assert fetched.report_id == report.report_id
    assert fetched.total_queries == 1
    assert len(fetched.case_details) == 1


def test_get_report_returns_none_for_unknown_id():
    """查询不存在的 report_id 应返回 None。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator

    evaluator = get_ragas_evaluator()
    assert evaluator.get_report("nonexistent_ragas_id_12345") is None


# ----------------------------------------------------------------------
# 降级策略测试
# ----------------------------------------------------------------------


def test_is_ragas_available_returns_false_when_ragas_not_installed(
    monkeypatch,
):
    """ragas 未安装时 is_ragas_available 应返回 False。"""
    from app.core.config import get_settings
    from app.knowledge.ragas_evaluator import is_ragas_available

    settings = get_settings()
    # 确保 LLM_API_KEY 已配置，使返回值仅受 ragas 安装状态影响
    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key", raising=False)

    # sys.modules['ragas'] = None 会让 import ragas 抛 ImportError
    monkeypatch.setitem(sys.modules, "ragas", None)

    assert is_ragas_available() is False


def test_is_ragas_available_returns_false_when_llm_key_missing(
    monkeypatch,
):
    """LLM_API_KEY 未配置时 is_ragas_available 应返回 False。"""
    from app.core.config import get_settings
    from app.knowledge.ragas_evaluator import is_ragas_available

    settings = get_settings()
    monkeypatch.setattr(settings, "LLM_API_KEY", "", raising=False)

    assert is_ragas_available() is False


def test_run_returns_zero_metrics_when_ragas_unavailable(
    monkeypatch, mock_rag_agent
):
    """ragas 不可用时 run() 仍应返回报告，指标全零。"""
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import RagasTestCase, RagasTestSet

    # 模拟 ragas 未安装：让 import ragas 抛 ImportError
    monkeypatch.setitem(sys.modules, "ragas", None)

    custom = RagasTestSet(
        cases=[RagasTestCase(question="降级测试", ground_truth="答案")]
    )
    evaluator = get_ragas_evaluator()
    report = evaluator.run(testset=custom, top_k=3)

    # ragas 不可用时降级返回全零指标
    assert report.total_queries == 1
    assert report.faithfulness == 0.0
    assert report.answer_relevancy == 0.0
    assert report.context_precision == 0.0
    assert report.context_recall == 0.0
    # 但报告结构应完整
    assert len(report.case_details) == 1


def test_run_degrades_when_llm_api_key_empty(monkeypatch, mock_rag_agent):
    """LLM_API_KEY 为空时 run() 应降级返回全零指标报告。"""
    from app.core.config import get_settings
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import RagasTestCase, RagasTestSet

    settings = get_settings()
    monkeypatch.setattr(settings, "LLM_API_KEY", "", raising=False)

    custom = RagasTestSet(
        cases=[RagasTestCase(question="空 key 降级", ground_truth="答案")]
    )
    evaluator = get_ragas_evaluator()
    report = evaluator.run(testset=custom, top_k=3)

    # LLM_API_KEY 为空时 _build_ragas_llm 返回 None，触发降级
    assert report.faithfulness == 0.0
    assert report.answer_relevancy == 0.0
    assert report.context_precision == 0.0
    assert report.context_recall == 0.0
    assert report.total_queries == 1


# ----------------------------------------------------------------------
# 报告持久化测试
# ----------------------------------------------------------------------


def test_report_persisted_to_ragas_reports_dir(
    mock_rag_agent, mock_ragas_metrics
):
    """评测后报告应持久化到 ragas_reports/ 目录。"""
    from app.core.config import get_settings
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import RagasTestCase, RagasTestSet

    custom = RagasTestSet(
        cases=[RagasTestCase(question="持久化测试", ground_truth="答案")]
    )
    evaluator = get_ragas_evaluator()
    report = evaluator.run(testset=custom, top_k=3)

    settings = get_settings()
    report_path = (
        Path(settings.CHROMA_PERSIST_DIR)
        / "ragas_reports"
        / f"{report.report_id}.json"
    )
    assert report_path.exists(), "RAGAS 报告文件应存在"


def test_persisted_report_can_be_deserialized(
    mock_rag_agent, mock_ragas_metrics
):
    """持久化的报告 JSON 文件应能正确反序列化为 RagasEvaluationReport。"""
    from app.core.config import get_settings
    from app.knowledge.ragas_evaluator import get_ragas_evaluator
    from app.schemas.ragas_evaluation import (
        RagasEvaluationReport,
        RagasTestCase,
        RagasTestSet,
    )

    custom = RagasTestSet(
        cases=[
            RagasTestCase(question="反序列化测试1", ground_truth="答案1"),
            RagasTestCase(question="反序列化测试2", ground_truth="答案2"),
        ]
    )
    evaluator = get_ragas_evaluator()
    report = evaluator.run(testset=custom, top_k=3)

    settings = get_settings()
    report_path = (
        Path(settings.CHROMA_PERSIST_DIR)
        / "ragas_reports"
        / f"{report.report_id}.json"
    )

    # 读取并反序列化
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    restored = RagasEvaluationReport(**payload)

    assert restored.report_id == report.report_id
    assert restored.total_queries == 2
    assert len(restored.case_details) == 2
    assert restored.case_details[0].question == "反序列化测试1"
    # 指标字段应存在且为合法值
    assert hasattr(restored, "faithfulness")
    assert hasattr(restored, "answer_relevancy")
    assert hasattr(restored, "context_precision")
    assert hasattr(restored, "context_recall")
    assert 0.0 <= restored.faithfulness <= 1.0
