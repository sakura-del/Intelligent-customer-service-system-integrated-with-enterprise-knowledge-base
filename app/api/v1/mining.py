"""历史工单知识挖掘 API 端点（Task 18）。

提供触发挖掘与查询最近报告的 HTTP 接口：
- POST /api/v1/mining/tickets：触发一次挖掘，可指定时间范围与状态过滤
- GET  /api/v1/mining/status：返回最近一次挖掘报告，未挖掘过返回空报告

设计要点：
- 走 verify_api_key 依赖，与 knowledge / escalation 路由一致
- 入参用 MiningRequest（JSON body），便于扩展
- 挖掘同步执行，简单可靠；大库场景可后续改为异步任务
"""

from fastapi import APIRouter, Depends

from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.knowledge.ticket_miner import get_ticket_miner
from app.schemas.mining import MiningReport, MiningRequest

logger = get_logger("app.api.v1.mining")

router = APIRouter(
    prefix="/api/v1/mining",
    tags=["工单挖掘"],
    dependencies=[Depends(verify_api_key)],
)


@router.post("/tickets", response_model=MiningReport)
def mine_tickets(request: MiningRequest | None = None) -> MiningReport:
    """触发一次历史工单知识挖掘。

    参数通过 MiningRequest JSON body 传入，全部可选：
    - start_time / end_time：按 created_at 闭区间过滤
    - status：按 TicketStatus 过滤，常见用法传 "resolved" 仅挖掘已解决工单

    返回 MiningReport，含统计、明细 items 与 errors。
    """
    request = request or MiningRequest()
    miner = get_ticket_miner()
    report = miner.mine(
        start_time=request.start_time,
        end_time=request.end_time,
        status=request.status,
    )
    logger.info(
        "挖掘任务完成：total=%d ingested=%d",
        report.total_tickets,
        report.ingested,
    )
    return report


@router.get("/status", response_model=MiningReport)
def get_mining_status() -> MiningReport:
    """查询最近一次挖掘报告。

    未触发过挖掘时返回空报告（total_tickets=0），便于前端首次进入页面渲染。
    """
    report = get_ticket_miner().get_last_report()
    if report is None:
        # 返回空报告而非 404，前端无需特殊处理
        from datetime import datetime, timezone

        return MiningReport(started_at=datetime.now(timezone.utc))
    return report
