"""检索效果评测端点。

提供触发评测与查询历史报告的 HTTP 接口：
- POST /api/v1/evaluation/run：触发评测，可传 testset_path 与 top_k
- GET /api/v1/evaluation/reports：列出历史评测报告摘要
- GET /api/v1/evaluation/reports/{report_id}：查询单个报告详情

评测在后台同步执行，结果持久化到 {CHROMA_PERSIST_DIR}/evaluation_reports/。
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.knowledge.evaluation import (
    EvaluationReport,
    get_evaluation_runner,
)
from app.schemas.evaluation import EvaluationRunRequest, ReportSummary

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


@router.get("/reports", response_model=List[ReportSummary])
def list_reports_api() -> List[ReportSummary]:
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
