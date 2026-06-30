"""知识质量校验数据模型。

定义质量校验环节产出的数据契约：
- QualityIssue：单项质量问题（重复片段 / 术语不一致 / 敏感词命中）
- QualityReport：聚合报告，供 API 返回与前端展示

设计要点：
所有字段均提供默认值，保证任一检查环节降级失败时仍能构造合法报告，
不影响主入库流程。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class QualityIssue(BaseModel):
    """单个质量问题条目。

    用于描述一项具体的质量缺陷，包含所在 chunk 索引、
    问题类型与人类可读说明，便于调用方定位修复。
    """

    chunk_index: int = Field(0, description="命中问题的 chunk 索引，便于定位原文")
    issue_type: str = Field(
        "",
        description="问题类型：duplicate / term_inconsistency / sensitive_word",
    )
    detail: str = Field("", description="问题详情，人类可读")
    snippet: str = Field("", description="命中文本片段，便于人工核对")


class QualityReport(BaseModel):
    """质量校验聚合报告。

    汇总三项检查结果，summary 字段给出整体概览，
    供 API 调用方快速判断是否需要人工介入。
    """

    total_chunks: int = Field(0, description="参与校验的 chunk 总数")
    duplicate_issues: List[QualityIssue] = Field(
        default_factory=list, description="重复片段问题列表"
    )
    term_issues: List[QualityIssue] = Field(
        default_factory=list, description="术语不一致问题列表"
    )
    sensitive_issues: List[QualityIssue] = Field(
        default_factory=list, description="敏感词命中问题列表"
    )
    summary: str = Field("", description="整体概览，便于快速判断质量")
    error: Optional[str] = Field(None, description="校验过程中的错误信息，成功时为空")


class QualityCheckRequest(BaseModel):
    """已入库内容质量巡检请求体。

    source 与 doc_id 均为可选过滤条件，都为空时巡检全量内容。
    """

    source: Optional[str] = Field(None, description="按来源文件名过滤")
    doc_id: Optional[str] = Field(None, description="按文档 ID 过滤")
