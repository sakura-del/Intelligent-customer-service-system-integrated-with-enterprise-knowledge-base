"""检索效果评测端点。

提供触发评测与查询历史报告的 HTTP 接口：
- POST /api/v1/evaluation/run：触发检索评测，可传 testset_path 与 top_k
- GET /api/v1/evaluation/reports：列出历史检索评测报告摘要
- GET /api/v1/evaluation/reports/{report_id}：查询单个检索评测报告详情

RAGAS 生成质量评估端点：
- POST /api/v1/evaluation/ragas/run：触发 RAGAS 评测
- GET /api/v1/evaluation/ragas/reports：列出 RAGAS 历史报告摘要
- GET /api/v1/evaluation/ragas/reports/{report_id}：查询单个 RAGAS 报告详情

评测在后台同步执行，结果分别持久化到：
- {CHROMA_PERSIST_DIR}/evaluation_reports/：检索评测报告
- {CHROMA_PERSIST_DIR}/ragas_reports/：RAGAS 生成质量评测报告
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.knowledge.evaluation import (
    EvaluationReport,
    get_evaluation_runner,
)
from app.knowledge.ragas_evaluator import (
    get_ragas_evaluator,
    is_ragas_available,
)
from app.schemas.evaluation import EvaluationRunRequest, ReportSummary
from app.schemas.ragas_evaluation import (
    RagasEvaluationReport,
    RagasReportSummary,
    RagasRunRequest,
)

logger = get_logger("app.api.v1.evaluation")

router = APIRouter(
    prefix="/api/v1/evaluation",
    tags=["检索评测"],
    dependencies=[Depends(verify_api_key)],
)


@router.post("/run", response_model=EvaluationReport)
def run_evaluation_api(request: EvaluationRunRequest) -> EvaluationReport:
    """触发评测运行。

    - testset_path 为空时使用内置默认测试集（30 条）；
    - top_k 为空时使用调优参数中的 rerank_top_k；
    - 评测结果持久化到 evaluation_reports/ 目录。
    """
    runner = get_evaluation_runner()
    testset = runner.load_testset(request.testset_path)
    if request.testset_path:
        testset.meta["source"] = f"external:{request.testset_path}"
    report = runner.run(testset=testset, top_k=request.top_k)
    # 标记测试集来源：外部 vs 内置
    report.source = "external" if request.testset_path else "default"
    logger.info(
        "评测已触发：report_id=%s total=%d source=%s",
        report.report_id,
        report.total_queries,
        report.source,
    )
    return report


@router.get("/reports", response_model=list[ReportSummary])
def list_reports_api() -> list[ReportSummary]:
    """列出历史评测报告摘要，按时间倒序返回。"""
    summaries = get_evaluation_runner().list_reports()
    return [ReportSummary(**item) for item in summaries]


@router.get("/reports/{report_id}", response_model=EvaluationReport)
def get_report_api(report_id: str) -> EvaluationReport:
    """查询单个报告详情。

    report_id 不存在时返回 404。
    """
    report = get_evaluation_runner().get_report(report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"报告不存在：{report_id}",
        )
    return report


# ----------------------------------------------------------------------
# RAGAS 生成质量评估端点
# ----------------------------------------------------------------------


@router.post("/ragas/run", response_model=RagasEvaluationReport)
def run_ragas_evaluation_api(
    request: RagasRunRequest,
) -> RagasEvaluationReport:
    """触发 RAGAS 生成质量评测。

    - testset_path 为空时使用内置 RAGAS 测试集（12 条，含 ground_truth）；
    - top_k 为空时使用 RAGAgent 默认 Top-K；
    - 评测结果持久化到 ragas_reports/ 目录；
    - ragas 未安装或 LLM_API_KEY 未配置时返回 503。
    """
    # 降级策略：ragas 不可用或 LLM_API_KEY 未配置时返回 503
    if not is_ragas_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAGAS 评估需要安装 ragas 库并配置 LLM_API_KEY",
        )

    evaluator = get_ragas_evaluator()
    testset = evaluator.load_testset(request.testset_path)
    if request.testset_path:
        testset.meta["source"] = f"external:{request.testset_path}"
    report = evaluator.run(testset=testset, top_k=request.top_k)
    # 标记测试集来源：外部 vs 内置
    report.source = "external" if request.testset_path else "default"
    logger.info(
        "RAGAS 评测已触发：report_id=%s total=%d source=%s",
        report.report_id,
        report.total_queries,
        report.source,
    )
    return report


@router.get("/ragas/reports", response_model=list[RagasReportSummary])
def list_ragas_reports_api() -> list[RagasReportSummary]:
    """列出 RAGAS 历史评测报告摘要，按时间倒序返回。"""
    summaries = get_ragas_evaluator().list_reports()
    return [RagasReportSummary(**item) for item in summaries]


@router.get("/ragas/reports/{report_id}", response_model=RagasEvaluationReport)
def get_ragas_report_api(report_id: str) -> RagasEvaluationReport:
    """查询单个 RAGAS 报告详情。

    report_id 不存在时返回 404。
    """
    report = get_ragas_evaluator().get_report(report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RAGAS 报告不存在：{report_id}",
        )
    return report
