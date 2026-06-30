"""检索效果评测模块。

实现 EvaluationRunner 类，加载测试集 → 运行检索 → 计算指标 → 输出报告。
指标：
- Recall@K：期望来源出现在 Top-K 的比例
- Precision@K：Top-K 中相关结果比例
- Hit Rate：是否检索到任何期望来源
- MRR：第一个相关结果的平均倒数排名
- Hallucination Rate：检索未命中但 Agent 仍生成答案的比例

内置 DEFAULT_TESTSET 至少 30 条，覆盖知识问答/订单/退货/会员/账户等场景。
报告持久化到 {CHROMA_PERSIST_DIR}/evaluation_reports/ 目录，按时间戳命名。

线程安全：评测状态与报告缓存用 RLock 保护。
降级策略：测试集加载失败用内置默认集；检索异常记为 miss；空库返回全零指标。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.knowledge.retrieval_tuner import get_retrieval_tuner
from app.schemas.knowledge import RetrievedChunk

logger = get_logger("app.knowledge.evaluation")

# 报告目录名：位于 CHROMA_PERSIST_DIR 下
_REPORTS_DIRNAME = "evaluation_reports"


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------


class TestCase(BaseModel):
    """单条评测用例。

    expected_sources：期望命中的来源列表（如 ['faq.md', 'return_policy.md']），
        命中任一即视为召回成功。
    expected_keywords：期望答案包含的关键词，用于幻觉率辅助判断。
    should_hit：该用例是否应命中知识库（False 时检索命中视为幻觉）。
    """

    query: str = Field(..., description="测试查询文本")
    expected_sources: List[str] = Field(
        default_factory=list, description="期望命中的来源列表"
    )
    expected_keywords: List[str] = Field(
        default_factory=list, description="期望答案包含的关键词"
    )
    should_hit: bool = Field(True, description="该用例是否应命中知识库")


class TestSet(BaseModel):
    """测试集：用例列表 + 元信息。"""

    cases: List[TestCase] = Field(default_factory=list, description="用例列表")
    meta: Dict[str, str] = Field(
        default_factory=dict, description="元信息，如版本、说明"
    )


class CaseDetail(BaseModel):
    """单条用例评测详情。"""

    query: str = Field("", description="查询文本")
    should_hit: bool = Field(True, description="是否应命中")
    hit: bool = Field(False, description="实际是否命中期望来源")
    top_sources: List[str] = Field(
        default_factory=list, description="检索 Top-K 来源列表"
    )
    first_relevant_rank: int = Field(
        -1, description="第一个相关结果排名（1-based），-1 表示未命中"
    )
    relevant_count: int = Field(
        0, description="Top-K 中相关结果数量"
    )
    error: Optional[str] = Field(None, description="异常信息，正常时为空")


class EvaluationReport(BaseModel):
    """评测聚合报告，含指标、单条详情与耗时。"""

    report_id: str = Field(..., description="报告唯一 ID")
    created_at: str = Field(..., description="生成时间，ISO8601 字符串")
    total_queries: int = Field(0, description="测试集查询总数")
    top_k: int = Field(0, description="评测使用的 Top-K")
    recall_at_k: float = Field(0.0, description="Recall@K 召回率")
    precision_at_k: float = Field(0.0, description="Precision@K 精确率")
    hit_rate: float = Field(0.0, description="命中率")
    mrr: float = Field(0.0, description="MRR 平均倒数排名")
    hallucination_rate: float = Field(0.0, description="幻觉率")
    duration_seconds: float = Field(0.0, description="评测耗时（秒）")
    source: str = Field(
        "default", description="测试集来源：default / external"
    )
    case_details: List[CaseDetail] = Field(
        default_factory=list, description="单条用例详情"
    )


# ----------------------------------------------------------------------
# 内置默认测试集（30 条，覆盖知识问答/订单/退货/会员/账户等场景）
# ----------------------------------------------------------------------


def _build_default_testset() -> TestSet:
    """构造内置默认测试集。

    覆盖 FAQ、订单、退货、会员、账户、产品手册、客服流程等场景，
    should_hit=False 的用例用于验证幻觉率指标。
    """
    cases = [
        # ===== 账号与登录 =====
        TestCase(
            query="忘记登录密码怎么办？",
            expected_sources=["faq.md"],
            expected_keywords=["忘记密码", "重置", "邮箱"],
            should_hit=True,
        ),
        TestCase(
            query="如何修改绑定的手机号？",
            expected_sources=["faq.md"],
            expected_keywords=["手机号", "账户安全", "验证码"],
            should_hit=True,
        ),
        TestCase(
            query="账号登录不了怎么回事",
            expected_sources=["faq.md"],
            expected_keywords=["登录", "账号"],
            should_hit=True,
        ),
        # ===== 订单与支付 =====
        TestCase(
            query="订单支付失败但资金已扣除怎么处理？",
            expected_sources=["faq.md"],
            expected_keywords=["支付失败", "资金", "退款"],
            should_hit=True,
        ),
        TestCase(
            query="可以修改已提交订单的收货地址吗？",
            expected_sources=["faq.md"],
            expected_keywords=["收货地址", "订单", "待发货"],
            should_hit=True,
        ),
        TestCase(
            query="订单发货后能改地址吗",
            expected_sources=["faq.md"],
            expected_keywords=["发货", "地址"],
            should_hit=True,
        ),
        TestCase(
            query="支付后多久能退款到账",
            expected_sources=["faq.md", "return_policy.md"],
            expected_keywords=["退款", "到账"],
            should_hit=True,
        ),
        # ===== 退换货政策 =====
        TestCase(
            query="七天无理由退货的范围是什么？",
            expected_sources=["faq.md", "return_policy.md"],
            expected_keywords=["七天无理由", "退货", "原包装"],
            should_hit=True,
        ),
        TestCase(
            query="退货运费由谁承担？",
            expected_sources=["faq.md", "return_policy.md"],
            expected_keywords=["退货运费", "商家", "买家"],
            should_hit=True,
        ),
        TestCase(
            query="质量问题退货运费谁出",
            expected_sources=["return_policy.md"],
            expected_keywords=["质量问题", "运费", "商家"],
            should_hit=True,
        ),
        TestCase(
            query="换货期限是多久？",
            expected_sources=["return_policy.md"],
            expected_keywords=["换货", "期限", "7 天"],
            should_hit=True,
        ),
        TestCase(
            query="大件商品退货怎么收费",
            expected_sources=["return_policy.md"],
            expected_keywords=["大件商品", "退货", "运费"],
            should_hit=True,
        ),
        TestCase(
            query="虚拟商品能退货吗",
            expected_sources=["return_policy.md"],
            expected_keywords=["虚拟商品", "退货"],
            should_hit=True,
        ),
        TestCase(
            query="定制商品支持无理由退货吗",
            expected_sources=["return_policy.md"],
            expected_keywords=["定制商品", "退货"],
            should_hit=True,
        ),
        TestCase(
            query="退款多久能到账",
            expected_sources=["return_policy.md"],
            expected_keywords=["退款", "到账", "工作日"],
            should_hit=True,
        ),
        TestCase(
            query="部分退款怎么申请",
            expected_sources=["return_policy.md"],
            expected_keywords=["部分退款"],
            should_hit=True,
        ),
        # ===== 会员与积分 =====
        TestCase(
            query="会员等级如何升级？",
            expected_sources=["faq.md"],
            expected_keywords=["会员等级", "升级", "消费金额"],
            should_hit=True,
        ),
        TestCase(
            query="积分有效期是多久？",
            expected_sources=["faq.md"],
            expected_keywords=["积分", "有效期", "12 个月"],
            should_hit=True,
        ),
        TestCase(
            query="金卡会员需要消费多少",
            expected_sources=["faq.md"],
            expected_keywords=["金卡", "消费"],
            should_hit=True,
        ),
        # ===== 产品手册与系统功能 =====
        TestCase(
            query="知识库支持哪些文档格式？",
            expected_sources=["product_manual.md"],
            expected_keywords=["文档格式", "Markdown", "PDF"],
            should_hit=True,
        ),
        TestCase(
            query="RAG 问答默认相似度阈值是多少？",
            expected_sources=["product_manual.md"],
            expected_keywords=["相似度", "阈值", "0.6"],
            should_hit=True,
        ),
        TestCase(
            query="系统支持哪些接入渠道？",
            expected_sources=["product_manual.md"],
            expected_keywords=["接入渠道", "Web", "App"],
            should_hit=True,
        ),
        TestCase(
            query="工单 SLA 默认多长时间",
            expected_sources=["product_manual.md"],
            expected_keywords=["SLA", "工单", "24 小时"],
            should_hit=True,
        ),
        TestCase(
            query="生产环境最低硬件配置是什么",
            expected_sources=["product_manual.md"],
            expected_keywords=["硬件", "CPU", "内存"],
            should_hit=True,
        ),
        TestCase(
            query="如何切换 LLM 模型",
            expected_sources=["product_manual.md"],
            expected_keywords=["LLM", "切换", "模型"],
            should_hit=True,
        ),
        TestCase(
            query="检索结果为空如何排查",
            expected_sources=["product_manual.md"],
            expected_keywords=["检索", "排查", "SIMILARITY_THRESHOLD"],
            should_hit=True,
        ),
        # ===== 客服联系方式 =====
        TestCase(
            query="客服热线电话是多少",
            expected_sources=["return_policy.md"],
            expected_keywords=["客服热线", "400"],
            should_hit=True,
        ),
        TestCase(
            query="在线客服工作时间",
            expected_sources=["return_policy.md"],
            expected_keywords=["在线客服", "工作日", "9:00"],
            should_hit=True,
        ),
        # ===== 不应命中的场景（验证幻觉率）=====
        TestCase(
            query="量子力学波函数坍缩原理",
            expected_sources=[],
            expected_keywords=[],
            should_hit=False,
        ),
        TestCase(
            query="如何烤一个完美的披萨",
            expected_sources=[],
            expected_keywords=[],
            should_hit=False,
        ),
    ]
    return TestSet(
        cases=cases,
        meta={"version": "default-v1", "description": "内置默认测试集"},
    )


# 模块级常量：避免每次调用都重新构造
DEFAULT_TESTSET: TestSet = _build_default_testset()


# ----------------------------------------------------------------------
# EvaluationRunner
# ----------------------------------------------------------------------


class EvaluationRunner:
    """评测运行器：加载测试集、执行检索、计算指标、持久化报告。

    - load_testset：加载外部 JSON 或用内置默认集；
    - run：执行评测，返回 EvaluationReport；
    - list_reports / get_report：查询历史报告。

    线程安全：评测状态与报告缓存用 RLock 保护，避免并发评测污染结果。
    单次评测内检索结果缓存：相同 query 不重复检索。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 报告缓存：report_id -> EvaluationReport，避免重复读盘
        self._report_cache: Dict[str, EvaluationReport] = {}

    def load_testset(self, path: Optional[str] = None) -> TestSet:
        """加载测试集，path 为空或加载失败时用内置默认集。"""
        if not path:
            return DEFAULT_TESTSET.model_copy()

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            testset = TestSet(**data)
            testset.meta["source"] = f"external:{path}"
            return testset
        except Exception as exc:
            logger.warning(
                "加载外部测试集失败，降级到默认集：%s path=%s", exc, path
            )
            return DEFAULT_TESTSET.model_copy()

    def run(
        self,
        testset: Optional[TestSet] = None,
        top_k: Optional[int] = None,
    ) -> EvaluationReport:
        """执行评测：对每条用例检索 → 计算指标 → 持久化报告。"""
        with self._lock:
            effective_testset = testset or DEFAULT_TESTSET
            effective_top_k = top_k or self._resolve_top_k()
            return self._run_evaluation(
                effective_testset, effective_top_k
            )

    def list_reports(self) -> List[Dict[str, object]]:
        """列出历史评测报告摘要，按时间倒序返回。"""
        with self._lock:
            reports = self._load_all_reports()
            summaries = [self._to_summary(r) for r in reports]
            # 按时间倒序：最新在前
            summaries.sort(key=lambda x: x["created_at"], reverse=True)
            return summaries

    def get_report(self, report_id: str) -> Optional[EvaluationReport]:
        """查询单个报告详情，不存在时返回 None。"""
        with self._lock:
            if report_id in self._report_cache:
                return self._report_cache[report_id]
            path = self._report_path(report_id)
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                report = EvaluationReport(**payload)
                self._report_cache[report_id] = report
                return report
            except Exception as exc:
                logger.warning("读取报告失败 report_id=%s：%s", report_id, exc)
                return None

    # ----- 内部实现 -----

    def _run_evaluation(
        self, testset: TestSet, top_k: int
    ) -> EvaluationReport:
        """单次评测核心逻辑：检索每条用例并计算指标。"""
        start = time.time()
        details: List[CaseDetail] = []
        # 检索结果缓存：相同 query 不重复检索，避免重复计算
        query_cache: Dict[str, List[RetrievedChunk]] = {}

        for case in testset.cases:
            detail = self._evaluate_case(case, top_k, query_cache)
            details.append(detail)

        # 聚合指标
        metrics = self._compute_metrics(details, top_k)
        duration = time.time() - start
        report_id = self._generate_report_id()
        report = EvaluationReport(
            report_id=report_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            total_queries=len(testset.cases),
            top_k=top_k,
            case_details=details,
            duration_seconds=round(duration, 4),
            **metrics,
        )

        # 持久化失败不影响返回
        self._persist_report(report)
        self._report_cache[report.report_id] = report
        logger.info(
            "评测完成：total=%d recall=%.3f precision=%.3f hit=%.3f mrr=%.3f hallucination=%.3f",
            report.total_queries,
            report.recall_at_k,
            report.precision_at_k,
            report.hit_rate,
            report.mrr,
            report.hallucination_rate,
        )
        return report

    def _evaluate_case(
        self,
        case: TestCase,
        top_k: int,
        query_cache: Dict[str, List[RetrievedChunk]],
    ) -> CaseDetail:
        """评测单条用例：检索并计算命中信息。

        检索异常时记为 miss，不中断整体评测。
        """
        try:
            chunks = query_cache.get(case.query)
            if chunks is None:
                chunks = self._retrieve(case.query, top_k)
                query_cache[case.query] = chunks
        except Exception as exc:
            logger.warning("检索异常 query=%r：%s", case.query[:30], exc)
            return CaseDetail(
                query=case.query,
                should_hit=case.should_hit,
                hit=False,
                error=str(exc),
            )

        top_sources = [c.source for c in chunks[:top_k] if c.source]
        # 找到第一个相关结果的排名（1-based）
        first_rank = -1
        relevant_count = 0
        for idx, chunk in enumerate(chunks[:top_k], start=1):
            if self._is_relevant(chunk, case.expected_sources):
                if first_rank == -1:
                    first_rank = idx
                relevant_count += 1

        hit = first_rank != -1
        return CaseDetail(
            query=case.query,
            should_hit=case.should_hit,
            hit=hit,
            top_sources=top_sources,
            first_relevant_rank=first_rank,
            relevant_count=relevant_count,
        )

    @staticmethod
    def _is_relevant(
        chunk: RetrievedChunk, expected_sources: List[str]
    ) -> bool:
        """判断 chunk 是否相关：source 命中任一期望来源即视为相关。"""
        if not expected_sources:
            return False
        if not chunk.source:
            return False
        for expected in expected_sources:
            # 用子串匹配，兼容 source 含路径前缀的情况
            if expected in chunk.source or chunk.source in expected:
                return True
        return False

    def _retrieve(
        self, query: str, top_k: int
    ) -> List[RetrievedChunk]:
        """调用 HybridRetriever 检索，空库或异常返回空列表。"""
        from app.knowledge.hybrid_retriever import get_hybrid_retriever

        retriever = get_hybrid_retriever()
        return retriever.retrieve(query, top_k=top_k)

    @staticmethod
    def _compute_metrics(
        details: List[CaseDetail], top_k: int
    ) -> Dict[str, float]:
        """计算 Recall@K / Precision@K / Hit Rate / MRR / Hallucination Rate。

        - Recall@K：期望来源出现在 Top-K 的比例（仅 should_hit=True 用例）
        - Precision@K：Top-K 中相关结果比例（相关数 / top_k）
        - Hit Rate：检索到任何期望来源的比例
        - MRR：第一个相关结果的平均倒数排名
        - Hallucination Rate：检索未命中但应未命中的反向指标
          （should_hit=False 但 hit=True 视为幻觉，应命中但未命中不计幻觉）
        """
        if not details:
            return {
                "recall_at_k": 0.0,
                "precision_at_k": 0.0,
                "hit_rate": 0.0,
                "mrr": 0.0,
                "hallucination_rate": 0.0,
            }

        # 应命中的用例子集
        should_hit_cases = [d for d in details if d.should_hit]
        # Recall@K：应命中用例中实际命中的比例
        if should_hit_cases:
            hit_in_should = sum(1 for d in should_hit_cases if d.hit)
            recall = hit_in_should / len(should_hit_cases)
        else:
            recall = 0.0

        # Precision@K：Top-K 中相关结果平均比例
        precision_values = [
            d.relevant_count / top_k
            for d in details
            if d.should_hit and top_k > 0
        ]
        precision = (
            sum(precision_values) / len(precision_values)
            if precision_values
            else 0.0
        )

        # Hit Rate：所有用例中检索到期望来源的比例
        hit_rate = sum(1 for d in details if d.hit) / len(details)

        # MRR：应命中用例中第一个相关结果的平均倒数排名
        rr_values = [
            1.0 / d.first_relevant_rank
            for d in should_hit_cases
            if d.first_relevant_rank > 0
        ]
        mrr = sum(rr_values) / len(should_hit_cases) if should_hit_cases else 0.0

        # Hallucination Rate：不应命中但检索命中的比例
        # （mock 模式下用 hit 字段判断：should_hit=False 且 hit=True）
        should_not_hit_cases = [d for d in details if not d.should_hit]
        if should_not_hit_cases:
            hallucinated = sum(1 for d in should_not_hit_cases if d.hit)
            hallucination_rate = hallucinated / len(should_not_hit_cases)
        else:
            hallucination_rate = 0.0

        return {
            "recall_at_k": round(recall, 4),
            "precision_at_k": round(precision, 4),
            "hit_rate": round(hit_rate, 4),
            "mrr": round(mrr, 4),
            "hallucination_rate": round(hallucination_rate, 4),
        }

    @staticmethod
    def _generate_report_id() -> str:
        """生成报告 ID：时间戳 + 短 UUID，避免冲突。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"{timestamp}_{short_uuid}"

    def _resolve_top_k(self) -> int:
        """从调优参数解析默认 top_k。"""
        try:
            tuner = get_retrieval_tuner()
            params = tuner.get_params()
            # 评测 top_k 略大于 rerank_top_k，覆盖召回范围
            return max(params.rerank_top_k, 5)
        except Exception:
            return 5

    # ----- 报告持久化 -----

    def _reports_dir(self) -> Path:
        """返回报告持久化目录。"""
        settings = get_settings()
        return Path(settings.CHROMA_PERSIST_DIR) / _REPORTS_DIRNAME

    def _report_path(self, report_id: str) -> Path:
        """返回单条报告的持久化路径。"""
        return self._reports_dir() / f"{report_id}.json"

    def _persist_report(self, report: EvaluationReport) -> None:
        """持久化报告到 JSON 文件，失败仅告警不影响返回。"""
        try:
            reports_dir = self._reports_dir()
            reports_dir.mkdir(parents=True, exist_ok=True)
            path = self._report_path(report.report_id)
            path.write_text(
                report.model_dump_json(indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("报告持久化失败 report_id=%s：%s", report.report_id, exc)

    def _load_all_reports(self) -> List[EvaluationReport]:
        """加载所有持久化报告。"""
        reports: List[EvaluationReport] = []
        reports_dir = self._reports_dir()
        if not reports_dir.exists():
            return reports
        for path in sorted(reports_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                reports.append(EvaluationReport(**payload))
            except Exception as exc:
                logger.warning("加载报告失败 path=%s：%s", path, exc)
        return reports

    @staticmethod
    def _to_summary(report: EvaluationReport) -> Dict[str, object]:
        """将报告转为摘要字典，用于列表展示。"""
        return {
            "report_id": report.report_id,
            "created_at": report.created_at,
            "total_queries": report.total_queries,
            "recall_at_k": report.recall_at_k,
            "precision_at_k": report.precision_at_k,
            "hit_rate": report.hit_rate,
            "mrr": report.mrr,
            "hallucination_rate": report.hallucination_rate,
            "duration_seconds": report.duration_seconds,
            "source": report.source,
        }


# 模块级单例：评测状态进程内共享
_evaluation_runner: Optional[EvaluationRunner] = None
_runner_lock = threading.Lock()


def get_evaluation_runner() -> EvaluationRunner:
    """获取 EvaluationRunner 单例。

    双重检查锁避免并发首调重复创建。
    """
    global _evaluation_runner
    if _evaluation_runner is None:
        with _runner_lock:
            if _evaluation_runner is None:
                _evaluation_runner = EvaluationRunner()
    return _evaluation_runner


def reset_evaluation_runner() -> None:
    """重置单例，便于测试切换持久化目录。"""
    global _evaluation_runner
    with _runner_lock:
        _evaluation_runner = None


def run_evaluation(
    testset_path: Optional[str] = None, top_k: Optional[int] = None
) -> EvaluationReport:
    """高层 API：加载测试集并执行评测。

    内置默认测试集，可加载外部测试集覆盖；
    评测结果持久化到 {CHROMA_PERSIST_DIR}/evaluation_reports/ 目录。
    """
    runner = get_evaluation_runner()
    testset = runner.load_testset(testset_path)
    if testset_path:
        testset.meta["source"] = f"external:{testset_path}"
    report = runner.run(testset=testset, top_k=top_k)
    # 标记测试集来源
    report.source = "external" if testset_path else "default"
    return report
