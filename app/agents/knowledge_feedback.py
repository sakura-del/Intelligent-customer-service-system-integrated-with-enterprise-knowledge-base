"""知识回流闭环。

人工客服处理完转接工单后，把"问题-方案"沉淀为 FAQ 入库，
形成"人工处理 → 沉淀 → 标注 → 补充知识库 → 下次智能回答"闭环。

核心流程：
1. record_human_solution：人工录入解决方案（暂存 pending 状态）
2. 自动/手动标注意图，便于后续归类与检索过滤
3. approve_solution：审核通过后 ingest_solution 写入向量库
4. 下次用户问相似问题时，KnowledgeAgent 检索命中即可自动回答

设计要点：
- 不直接写入向量库，需经 approve_solution 审核避免污染知识库
- ingest_solution 复用现有 VectorStore 单例，与流水线保持一致
- 用临时文件走 ingest_document 全链路，复用解析/切分/元数据标注逻辑
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agents.escalation import generate_solution_id
from app.core.logging import get_logger
from app.schemas.escalation import HumanSolutionRecord

logger = get_logger("app.agents.knowledge_feedback")

# 临时 FAQ 文件后缀：用 markdown 便于切分器按章节处理
FAQ_FILE_SUFFIX = ".md"

# 默认意图：未识别到具体意图时归入此类
DEFAULT_INTENT = "unknown"


class KnowledgeFeedback:
    """知识回流闭环管理器。

    持有进程内 pending 队列（pending → approved），
    所有读写经 RLock 串行化，保证多线程并发录入/审核时状态一致。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # solution_id -> HumanSolutionRecord，进程内存储便于测试与离线运行
        self._solutions: Dict[str, HumanSolutionRecord] = {}

    # ------------------------------------------------------------------
    # 录入与标注
    # ------------------------------------------------------------------
    def record_human_solution(
        self,
        session_id: Optional[str],
        question: str,
        solution: str,
        intent: Optional[str] = None,
    ) -> HumanSolutionRecord:
        """人工录入解决方案，自动标注意图。

        intent 未传入时调用 OrchestratorAgent 的意图识别自动标注，
        保证每条记录都有意图便于后续归类与检索过滤。
        返回的 record 默认 pending 状态，需 approve 后才会入库。
        """
        # 自动标注意图：未传入时复用 OrchestratorAgent 识别能力
        resolved_intent = intent or self._auto_annotate_intent(question)

        record = HumanSolutionRecord(
            solution_id=generate_solution_id(),
            session_id=session_id,
            question=question.strip(),
            solution=solution.strip(),
            intent=resolved_intent,
            status="pending",
        )
        with self._lock:
            self._solutions[record.solution_id] = record
        logger.info(
            "人工方案已录入：solution_id=%s intent=%s question=%s",
            record.solution_id,
            record.intent,
            record.question[:50],
        )
        return record

    @staticmethod
    def _auto_annotate_intent(question: str) -> str:
        """自动标注意图，复用 OrchestratorAgent 的关键词规则。

        降级到 DEFAULT_INTENT 保证总有标注，
        避免意图缺失影响后续入库元数据。
        """
        try:
            from app.agents.orchestrator import get_orchestrator

            orchestrator = get_orchestrator()
            intent_result = orchestrator._recognize_intent(question)
            return intent_result.intent.value
        except Exception as exc:
            logger.warning("自动标注意图失败，使用默认意图：%s", exc)
            return DEFAULT_INTENT

    # ------------------------------------------------------------------
    # 审核与入库
    # ------------------------------------------------------------------
    def approve_solution(self, solution_id: str) -> Optional[HumanSolutionRecord]:
        """审核通过并入库为 FAQ 知识。

        入库失败时保留 pending 状态便于重试，避免遗漏。
        返回更新后的 record；不存在或已审核返回 None。
        """
        with self._lock:
            record = self._solutions.get(solution_id)
            if record is None:
                return None
            if record.status == "approved":
                # 已审核的方案不重复入库，避免向量库重复写入
                return record

        # 入库操作在锁外执行，避免长 IO 阻塞其他录入
        ingest_ok = self.ingest_solution(
            question=record.question,
            solution=record.solution,
            intent=record.intent,
        )
        if not ingest_ok:
            logger.warning("方案入库失败，保留 pending 状态：%s", solution_id)
            return None

        with self._lock:
            record = self._solutions.get(solution_id)
            if record is None:
                return None
            record.status = "approved"
            return record.model_copy()

    def reject_solution(self, solution_id: str) -> Optional[HumanSolutionRecord]:
        """驳回方案，标记为 rejected 不入库。"""
        with self._lock:
            record = self._solutions.get(solution_id)
            if record is None:
                return None
            record.status = "rejected"
            return record.model_copy()

    # ------------------------------------------------------------------
    # 入库实现
    # ------------------------------------------------------------------
    def ingest_solution(
        self,
        question: str,
        solution: str,
        intent: str,
    ) -> bool:
        """把"问题-方案"结构化为 FAQ 文档入库。

        通过临时 markdown 文件走 ingest_document 全链路，
        复用切分器/向量器/去重逻辑，避免重复实现。
        入库后立即删除临时文件，避免磁盘泄漏。
        """
        from app.knowledge.pipeline import ingest_document

        # 构造 FAQ markdown：标题即问题，正文即方案，便于切分器按章节处理
        content = self._build_faq_content(question, solution, intent)
        tmp_path = self._write_temp_faq(content)

        try:
            result = ingest_document(
                tmp_path,
                metadata={
                    "knowledge_type": "faq",
                    "intent": intent,
                    "source": "human_solution",
                },
            )
            if result.error is not None:
                logger.warning("人工方案入库失败：%s", result.error)
                return False
            logger.info(
                "人工方案已入库：question=%s chunks=%d intent=%s",
                question[:30],
                result.added_chunks,
                intent,
            )
            return True
        except Exception as exc:
            logger.exception("人工方案入库异常：%s", exc)
            return False
        finally:
            # 无论成功失败都清理临时文件
            Path(tmp_path).unlink(missing_ok=True)

    @staticmethod
    def _build_faq_content(question: str, solution: str, intent: str) -> str:
        """构造 FAQ markdown 内容。

        格式：
        # 问题：<question>
        意图：<intent>
        ## 解答
        <solution>
        """
        return (
            f"# 问题：{question}\n\n"
            f"意图：{intent}\n\n"
            f"## 解答\n\n{solution}\n"
        )

    @staticmethod
    def _write_temp_faq(content: str) -> str:
        """写入临时 markdown 文件，返回路径。

        用 NamedTemporaryFile 保证文件名唯一，
        delete=False 让 pipeline 能读取后再清理。
        """
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=FAQ_FILE_SUFFIX,
            delete=False,
            encoding="utf-8",
        ) as buffer:
            buffer.write(content)
            return buffer.name

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def get_pending_solutions(self) -> List[HumanSolutionRecord]:
        """返回所有待审核方案，便于人工审核列表展示。"""
        with self._lock:
            return [
                record.model_copy()
                for record in self._solutions.values()
                if record.status == "pending"
            ]

    def get_solution(self, solution_id: str) -> Optional[HumanSolutionRecord]:
        """查询单条方案记录。"""
        with self._lock:
            record = self._solutions.get(solution_id)
            return record.model_copy() if record is not None else None

    def list_all_solutions(self) -> List[HumanSolutionRecord]:
        """返回所有方案记录，便于审计。"""
        with self._lock:
            return [record.model_copy() for record in self._solutions.values()]

    def reset(self) -> None:
        """清空所有方案记录，便于测试隔离。"""
        with self._lock:
            self._solutions.clear()


# 模块级单例：进程内复用，避免多实例导致 pending 队列分裂
_knowledge_feedback: Optional[KnowledgeFeedback] = None
_singleton_lock = threading.Lock()


def get_knowledge_feedback() -> KnowledgeFeedback:
    """获取 KnowledgeFeedback 单例。"""
    global _knowledge_feedback
    if _knowledge_feedback is None:
        with _singleton_lock:
            if _knowledge_feedback is None:
                _knowledge_feedback = KnowledgeFeedback()
    return _knowledge_feedback


def reset_knowledge_feedback() -> None:
    """重置单例，便于测试隔离。"""
    global _knowledge_feedback
    with _singleton_lock:
        _knowledge_feedback = None
