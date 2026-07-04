"""RAGAS 生成质量评估数据模型。

定义 RAGAS 评估链路所需的请求体、测试集、单条详情与聚合报告，
作为 /api/v1/evaluation/ragas/* 路由与 RagasEvaluator 之间的数据契约。

设计要点：
- 测试用例仅包含 question 与 ground_truth，无需人工标注期望来源；
- 单条详情记录 answer/contexts/四项指标，便于排查单条用例质量；
- 报告摘要与详情分离：列表接口返回摘要，详情接口返回完整报告。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class RagasTestCase(BaseModel):
    """RAGAS 单条测试用例。

    与检索评测的 TestCase 区别：RAGAS 关注生成质量，
    只需 question 与 ground_truth（标准答案），由 LLM 自动评分，
    无需人工标注期望来源。
    """

    question: str = Field(..., description="测试问题文本")
    ground_truth: str = Field(..., description="标准答案，用于 RAGAS 评分对比")


class RagasTestSet(BaseModel):
    """RAGAS 测试集：用例列表 + 元信息。"""

    cases: List[RagasTestCase] = Field(
        default_factory=list, description="用例列表"
    )
    meta: Dict[str, str] = Field(
        default_factory=dict, description="元信息，如版本、来源"
    )


class RagasCaseDetail(BaseModel):
    """RAGAS 单条用例评测详情。

    记录每条用例的完整链路数据（question/answer/contexts）
    与四项核心指标得分，便于排查单条用例质量。
    """

    question: str = Field("", description="测试问题")
    ground_truth: str = Field("", description="标准答案")
    answer: str = Field("", description="RAG Agent 生成的答案")
    contexts: List[str] = Field(
        default_factory=list, description="检索召回的上下文片段文本列表"
    )
    faithfulness: float = Field(0.0, description="忠实度：答案是否基于上下文")
    answer_relevancy: float = Field(0.0, description="答案相关性：是否回答了问题")
    context_precision: float = Field(
        0.0, description="上下文精确度：相关上下文占比"
    )
    context_recall: float = Field(
        0.0, description="上下文召回率：标准答案是否被上下文覆盖"
    )
    error: Optional[str] = Field(
        None, description="异常信息，正常时为空"
    )


class RagasEvaluationReport(BaseModel):
    """RAGAS 评测聚合报告，含四项聚合指标与单条详情。"""

    report_id: str = Field(..., description="报告唯一 ID")
    created_at: str = Field(..., description="生成时间，ISO8601 字符串")
    total_queries: int = Field(0, description="测试集查询总数")
    faithfulness: float = Field(0.0, description="忠实度聚合得分（0-1）")
    answer_relevancy: float = Field(0.0, description="答案相关性聚合得分（0-1）")
    context_precision: float = Field(
        0.0, description="上下文精确度聚合得分（0-1）"
    )
    context_recall: float = Field(
        0.0, description="上下文召回率聚合得分（0-1）"
    )
    duration_seconds: float = Field(0.0, description="评测耗时（秒）")
    source: str = Field(
        "default", description="测试集来源：default / external"
    )
    case_details: List[RagasCaseDetail] = Field(
        default_factory=list, description="单条用例详情列表"
    )


class RagasRunRequest(BaseModel):
    """RAGAS 评测运行请求体。

    testset_path 为空时使用内置默认测试集；
    top_k 为空时使用 RAGAgent 默认 Top-K。
    """

    testset_path: Optional[str] = Field(
        None, description="外部测试集 JSON 路径，为空时使用内置默认集"
    )
    top_k: Optional[int] = Field(
        None, ge=1, le=50, description="检索 Top-K，为空时使用默认值"
    )


class RagasReportSummary(BaseModel):
    """RAGAS 报告摘要，用于列表展示。

    仅保留概要信息，详情通过 /ragas/reports/{report_id} 查询。
    """

    report_id: str = Field(..., description="报告唯一 ID")
    created_at: str = Field(..., description="报告生成时间，ISO8601 字符串")
    total_queries: int = Field(0, description="测试集查询总数")
    faithfulness: float = Field(0.0, description="忠实度聚合得分")
    answer_relevancy: float = Field(0.0, description="答案相关性聚合得分")
    context_precision: float = Field(0.0, description="上下文精确度聚合得分")
    context_recall: float = Field(0.0, description="上下文召回率聚合得分")
    duration_seconds: float = Field(0.0, description="评测耗时（秒）")
    source: str = Field(
        "default", description="测试集来源：default / external"
    )
