"""检索调优与评测 API 数据模型。

定义 TunerParamsUpdateRequest / EvaluationRunRequest / ReportSummary，
作为 /api/v1/tuner 与 /api/v1/evaluation 路由的请求与响应契约。

设计要点：
- 调优更新请求只接收可变字段，避免覆盖未知参数；
- 评测运行请求 testset_path 与 top_k 均可选，缺省走内置默认；
- 报告摘要仅保留概要信息，详情通过 report_id 二次查询。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TunerParamsUpdateRequest(BaseModel):
    """调优参数更新请求体。

    所有字段可选：未传字段保持原值不变，便于局部调参。
    范围校验在 TunerParams 模型内统一执行。
    """

    vector_top_k: int | None = Field(None, description="向量召回数量，建议范围 20-30")
    bm25_top_k: int | None = Field(None, description="BM25 召回数量，建议范围 20-30")
    rerank_top_k: int | None = Field(None, description="重排序后保留数量，建议范围 3-5")
    similarity_threshold: float | None = Field(None, description="相似度阈值，建议范围 0.6-0.7")
    rrf_k: int | None = Field(None, description="RRF 平滑常数，建议范围 40-80")
    rrf_vector_weight: float | None = Field(None, description="RRF 向量路权重，建议 0.6")
    rrf_keyword_weight: float | None = Field(None, description="RRF 关键词路权重，建议 0.4")


class EvaluationRunRequest(BaseModel):
    """评测运行请求体。

    testset_path 为空时使用内置默认测试集；
    top_k 为空时使用当前调优参数中的 rerank_top_k。
    """

    testset_path: str | None = Field(None, description="外部测试集 JSON 路径，为空时使用内置默认集")
    top_k: int | None = Field(None, ge=1, le=50, description="评测时检索 Top-K，为空时使用调优参数")


class ReportSummary(BaseModel):
    """评测报告摘要，用于列表展示。

    仅保留概要信息，详情通过 /reports/{report_id} 查询。
    """

    report_id: str = Field(..., description="报告唯一 ID（时间戳派生）")
    created_at: str = Field(..., description="报告生成时间，ISO8601 字符串")
    total_queries: int = Field(0, description="测试集查询总数")
    recall_at_k: float = Field(0.0, description="Recall@K 召回率")
    precision_at_k: float = Field(0.0, description="Precision@K 精确率")
    hit_rate: float = Field(0.0, description="命中率")
    mrr: float = Field(0.0, description="MRR 平均倒数排名")
    hallucination_rate: float = Field(0.0, description="幻觉率")
    duration_seconds: float = Field(0.0, description="评测耗时（秒）")
    source: str = Field("default", description="测试集来源：default=内置 / external=外部")
