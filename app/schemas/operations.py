"""发布与运营数据契约。

定义灰度实验、运营看板、上线检查清单的 Pydantic 模型，
作为 operations 路由与核心模块之间的数据契约。

设计要点：
- 所有数值字段提供默认值，聚合失败或空数据场景仍能返回结构完整的响应
- 模型分层清晰：Experiment / Variant 等小模型组合为聚合报告
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ----------------------------------------------------------------------
# 灰度发布与 A/B 测试
# ----------------------------------------------------------------------


class Variant(BaseModel):
    """实验分组定义。

    一个实验可包含多个分组（control / treatment 等），
    weight 决定该分组流量占比，所有分组权重之和应大于 0。
    """

    name: str = Field(..., description="分组名，如 control / treatment")
    weight: int = Field(
        1,
        ge=0,
        description="分组权重，按权重比例分配流量",
    )
    description: str = Field("", description="分组描述")


class Experiment(BaseModel):
    """实验配置。

    持久化到 {CHROMA_PERSIST_DIR}/experiments.json，
    enabled=False 时 assign_user 始终返回 control 分组（降级）。
    """

    name: str = Field(..., description="实验名，全局唯一")
    description: str = Field("", description="实验描述")
    variants: list[Variant] = Field(
        default_factory=list,
        description="分组列表，至少包含 control 与一个 treatment",
    )
    target_metrics: list[str] = Field(
        default_factory=list,
        description="实验关注的目标指标名列表",
    )
    enabled: bool = Field(True, description="是否启用，关闭后所有用户进 control")
    created_at: str = Field("", description="创建时间（ISO 字符串）")
    updated_at: str = Field("", description="最近更新时间（ISO 字符串）")


class VariantMetricStats(BaseModel):
    """单分组单指标的统计结果。"""

    mean: float = Field(0.0, description="均值")
    std: float = Field(0.0, description="标准差")
    sample_count: int = Field(0, description="样本数")


class ExperimentResults(BaseModel):
    """实验聚合结果。

    metrics 结构：{variant_name: {metric_name: VariantMetricStats}}
    便于前端按分组对比各指标差异。
    """

    name: str = Field(..., description="实验名")
    enabled: bool = Field(True, description="实验是否启用")
    variants: list[str] = Field(
        default_factory=list,
        description="分组名列表",
    )
    metrics: dict[str, dict[str, VariantMetricStats]] = Field(
        default_factory=dict,
        description="按分组聚合的指标统计",
    )


class CreateExperimentRequest(BaseModel):
    """创建实验请求体。"""

    name: str = Field(..., description="实验名，全局唯一")
    description: str = Field("", description="实验描述")
    variants: list[Variant] = Field(
        ...,
        description="分组列表，至少含 control 与一个 treatment",
    )
    target_metrics: list[str] = Field(
        default_factory=list,
        description="目标指标名列表",
    )
    enabled: bool = Field(True, description="是否立即启用")


class RecordMetricRequest(BaseModel):
    """记录实验指标请求体。"""

    variant: str = Field(..., description="分组名")
    metric_name: str = Field(..., description="指标名")
    value: float = Field(..., description="指标值")


# ----------------------------------------------------------------------
# 运营看板
# ----------------------------------------------------------------------


class SessionStats(BaseModel):
    """会话统计。"""

    total_sessions: int = Field(0, description="总会话数")
    active_sessions: int = Field(0, description="活跃会话数")
    avg_turn_count: float = Field(0.0, description="平均对话轮数")


class TicketStats(BaseModel):
    """工单统计。"""

    total: int = Field(0, description="工单总数")
    new_count: int = Field(0, description="新增工单数（pending）")
    resolved_count: int = Field(0, description="已解决工单数（resolved + closed）")
    unresolved_count: int = Field(0, description="未解决工单数（pending + processing）")
    category_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="按分类分布，key=分类 value=数量",
    )


class EscalationStats(BaseModel):
    """转接统计。"""

    total_escalations: int = Field(0, description="转接次数")
    reason_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="转接原因分布，key=规则名 value=次数",
    )
    human_pickup_rate: float = Field(
        0.0,
        description="人工接通率（mock：基于转接数推算）",
    )


class SatisfactionStats(BaseModel):
    """满意度统计（mock）。"""

    avg_score: float = Field(0.0, description="平均满意度（0-5）")
    sample_count: int = Field(0, description="样本数")
    positive_rate: float = Field(0.0, description="正面评价比例")


class KnowledgeStats(BaseModel):
    """知识库统计。"""

    total_entries: int = Field(0, description="知识库总条目数")
    type_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="按类型分布，key=类型 value=数量",
    )
    recent_7d_ingest_count: int = Field(
        0,
        description="近 7 天入库量",
    )


class OperationsDashboard(BaseModel):
    """运营看板聚合数据。"""

    session: SessionStats = Field(
        default_factory=SessionStats,
        description="会话统计",
    )
    ticket: TicketStats = Field(
        default_factory=TicketStats,
        description="工单统计",
    )
    escalation: EscalationStats = Field(
        default_factory=EscalationStats,
        description="转接统计",
    )
    satisfaction: SatisfactionStats = Field(
        default_factory=SatisfactionStats,
        description="满意度统计",
    )
    knowledge: KnowledgeStats = Field(
        default_factory=KnowledgeStats,
        description="知识库统计",
    )
    collected_at: str = Field(
        "",
        description="数据采集时间（ISO 字符串）",
    )


# ----------------------------------------------------------------------
# 上线检查清单
# ----------------------------------------------------------------------


class CheckItem(BaseModel):
    """单项检查结果。"""

    name: str = Field(..., description="检查项名称")
    status: str = Field(
        "pending",
        description="检查状态：pass / fail / warn / skipped",
    )
    message: str = Field("", description="检查信息（人类可读）")
    duration_ms: float = Field(0.0, description="检查耗时（毫秒）")


class ChecklistReport(BaseModel):
    """上线检查清单报告。"""

    passed: int = Field(0, description="通过项数")
    failed: int = Field(0, description="失败项数")
    warned: int = Field(0, description="警告项数")
    skipped: int = Field(0, description="跳过项数")
    total: int = Field(0, description="总检查项数")
    items: list[CheckItem] = Field(
        default_factory=list,
        description="各检查项详情",
    )
    generated_at: str = Field(
        "",
        description="报告生成时间（ISO 字符串）",
    )
