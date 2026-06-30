"""人工客服转接端点。

提供人工客服录入解决方案的接口，沉淀人工处理经验到知识库：
- POST /api/v1/escalation/solution：录入人工方案（待审核）
- GET /api/v1/escalation/solutions/pending：列出待审核方案
- POST /api/v1/escalation/solutions/{solution_id}/approve：审核入库

闭环：人工录入 → 自动标注意图 → 审核通过 → 入库为 FAQ → 下次智能客服可检索命中
"""
from typing import List

from fastapi import APIRouter, Depends

from app.agents.knowledge_feedback import get_knowledge_feedback
from app.core.logging import get_logger
from app.core.security import verify_api_key
from app.schemas.escalation import (
    HumanSolutionRecord,
    HumanSolutionRequest,
)

logger = get_logger("app.api.v1.escalation")

router = APIRouter(
    prefix="/api/v1/escalation",
    tags=["人工转接"],
    dependencies=[Depends(verify_api_key)],
)


@router.post("/solution", response_model=HumanSolutionRecord)
def record_human_solution(
    request: HumanSolutionRequest,
) -> HumanSolutionRecord:
    """人工客服录入解决方案。

    录入后自动标注意图（未传入时由系统识别），
    方案进入 pending 状态，需审核后才会入库为 FAQ。
    """
    feedback = get_knowledge_feedback()
    record = feedback.record_human_solution(
        session_id=request.session_id,
        question=request.question,
        solution=request.solution,
        intent=request.intent,
    )
    logger.info(
        "人工方案已录入：solution_id=%s intent=%s",
        record.solution_id,
        record.intent,
    )
    return record


@router.get("/solutions/pending", response_model=List[HumanSolutionRecord])
def list_pending_solutions() -> List[HumanSolutionRecord]:
    """列出所有待审核方案，供人工审核列表展示。"""
    return get_knowledge_feedback().get_pending_solutions()


@router.post(
    "/solutions/{solution_id}/approve",
    response_model=HumanSolutionRecord,
)
def approve_solution(solution_id: str) -> HumanSolutionRecord:
    """审核通过方案并入库为 FAQ 知识。

    入库后下次智能客服检索相似问题时可命中该方案，
    形成"人工处理 → 沉淀 → 下次智能回答"闭环。
    """
    feedback = get_knowledge_feedback()
    record = feedback.approve_solution(solution_id)
    if record is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404,
            detail=f"方案 {solution_id} 不存在或入库失败",
        )
    return record
