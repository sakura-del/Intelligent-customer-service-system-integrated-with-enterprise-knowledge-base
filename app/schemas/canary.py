"""灰度验证数据模型。

定义知识库版本灰度 A/B 对比的数据契约：
- CanaryHitItem：单条检索命中结果（含相似度与文本预览）
- CanaryQueryResult：单条样本查询在新旧版本的对比结果
- CanaryReport：聚合报告，供 API 返回

设计要点：
所有数值字段提供默认值，fallback embedding 模式下检索可能为空，
报告仍需结构完整以便上层展示。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CanaryHitItem(BaseModel):
    """灰度检索单条命中结果。

    仅保留对比所需的最少字段，避免泄露向量库内部结构。
    """

    text: str = Field("", description="命中文本预览，截断避免过长")
    score: float = Field(0.0, description="相似度得分")
    source: str = Field("", description="来源文件名")


class CanaryQueryResult(BaseModel):
    """单条样本查询的 A/B 对比结果。

    对比目标版本与当前版本在同一查询下的 Top-K 检索差异，
    similarity_diff 为两版本 Top-1 相似度差值，正值表示新版本更相关。
    """

    query: str = Field("", description="样本查询文本")
    target_version: str = Field("", description="目标版本号，如 v2")
    current_version: str = Field("", description="当前版本号，如 v1")
    target_results: list[CanaryHitItem] = Field(
        default_factory=list, description="目标版本 Top-K 检索结果"
    )
    current_results: list[CanaryHitItem] = Field(
        default_factory=list, description="当前版本 Top-K 检索结果"
    )
    similarity_diff: float = Field(
        0.0, description="Top-1 相似度差值（目标 - 当前），正值表示新版本更优"
    )


class CanaryReport(BaseModel):
    """灰度验证聚合报告。

    汇总所有样本查询的对比结果，并给出整体结论，
    便于决策是否将目标版本切换为当前版本。
    """

    doc_id: str = Field("", description="被验证文档 ID")
    target_version: str = Field("", description="目标版本号")
    current_version: str = Field("", description="当前版本号")
    query_results: list[CanaryQueryResult] = Field(
        default_factory=list, description="各样本查询的对比结果"
    )
    avg_similarity_diff: float = Field(
        0.0, description="所有查询的平均相似度差值，正值表示新版本整体更优"
    )
    summary: str = Field("", description="整体结论，便于快速判断")
    canary_unavailable: bool = Field(
        False,
        description="灰度集合是否不可用；为 True 时仅返回主集合结果并降级",
    )
    error: str | None = Field(None, description="灰度过程中的错误信息，成功时为空")
