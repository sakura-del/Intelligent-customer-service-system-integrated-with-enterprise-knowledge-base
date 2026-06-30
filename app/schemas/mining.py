"""历史工单知识挖掘数据模型。

定义 Task 18 挖掘流程对外/对内的数据契约：
- MiningRequest：挖掘请求入参（时间范围、状态过滤）
- MinedKnowledgeItem：单条已挖掘知识条目（含意图、答案、来源工单）
- MiningReport：挖掘报告（统计 + 错误列表 + 详情）

设计目标：
- 报告字段足够前端展示「本次挖了多少 / 入库多少 / 失败多少 / 跳过多少」
- 错误条目带 ticket_id 与原因，便于人工排查
- 字段命名与既有 IngestResult 风格保持一致
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class MiningRequest(BaseModel):
    """挖掘请求体。

    start_time / end_time 均为可选，未传表示不限；
    status 仅支持 TicketStatus 枚举值（pending/processing/resolved/closed），
    传 None 表示不过滤状态（默认行为：挖掘全部工单）。
    """

    start_time: Optional[datetime] = Field(
        None, description="工单创建时间下界（含），ISO8601；不传表示不限"
    )
    end_time: Optional[datetime] = Field(
        None, description="工单创建时间上界（含），ISO8601；不传表示不限"
    )
    status: Optional[str] = Field(
        None,
        description=(
            "工单状态过滤：pending/processing/resolved/closed；"
            "不传表示不过滤状态"
        ),
    )


class MinedKnowledgeItem(BaseModel):
    """单条挖掘产出的知识条目。

    一条 item 对应一个工单的「意图 + 抽取的答案」，
    入库时作为 metadata 写入向量库，便于后续按工单回溯。
    """

    source_ticket_id: str = Field(..., description="来源工单 ID")
    intent: str = Field(..., description="意图标签，如「退货咨询」")
    answer: str = Field(..., description="抽取的答案片段（已脱敏）")
    category: str = Field(..., description="工单分类，与 TicketCategory 对齐")
    priority: str = Field(..., description="工单优先级，与 TicketPriority 对齐")
    ingested: bool = Field(
        False, description="是否已成功入库向量库"
    )
    skip_reason: Optional[str] = Field(
        None, description="未入库原因：duplicate/empty/ingest_failed"
    )


class MiningReport(BaseModel):
    """挖掘报告。

    统计字段含义：
    - total_tickets：本次扫描的工单总数（过滤后）
    - processed：成功完成「标注 + 抽取」的工单数
    - ingested：实际写入向量库的知识条数（去重后）
    - deduped：因相似答案被去重跳过的条数
    - skipped：因空内容/抽取失败被跳过的条数
    - failed：处理过程异常的工单数
    - duration_seconds：本次挖掘总耗时
    """

    started_at: datetime = Field(..., description="挖掘开始时间（UTC）")
    finished_at: Optional[datetime] = Field(
        None, description="挖掘结束时间（UTC），进行中为空"
    )
    total_tickets: int = Field(0, description="扫描的工单总数")
    processed: int = Field(0, description="完成标注+抽取的工单数")
    ingested: int = Field(0, description="实际入库的知识条数")
    deduped: int = Field(0, description="去重跳过的条数")
    skipped: int = Field(0, description="空内容/抽取失败跳过的条数")
    failed: int = Field(0, description="处理异常的工单数")
    duration_seconds: float = Field(0.0, description="本次挖掘总耗时（秒）")
    items: List[MinedKnowledgeItem] = Field(
        default_factory=list,
        description="本次挖掘的明细条目，便于前端展示与审计",
    )
    errors: List[str] = Field(
        default_factory=list,
        description="错误信息列表，每条含 ticket_id 与原因摘要",
    )
    filters: dict = Field(
        default_factory=dict,
        description="本次挖掘使用的过滤条件快照",
    )
