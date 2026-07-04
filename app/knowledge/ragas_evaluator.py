"""RAGAS 生成质量评估器。

实现 RagasEvaluator 类，编排「检索 → 生成 → LLM 评分」全链路，
对 RAG 端到端流程进行无需人工标注的质量评估。

四项核心指标：
- Faithfulness（忠实度）：答案是否忠实于检索上下文，未编造信息
- Answer Relevancy（答案相关性）：答案是否切题回答了用户问题
- Context Precision（上下文精确度）：相关上下文在召回结果中的占比
- Context Recall（上下文召回率）：标准答案是否被召回的上下文覆盖

设计要点：
- 复用 KnowledgeRetriever 检索、RAGAgent 生成、LLMClient 评分
- 报告持久化到 {CHROMA_PERSIST_DIR}/ragas_reports/ 目录，复用现有报告目录模式
- 线程安全：评测状态与报告缓存用 RLock 保护
- 降级策略：ragas 未安装或 LLM_API_KEY 为空时返回降级报告（指标全 0）

RAGAS 调用兼容：ragas 0.2.x API 在不同小版本有差异，
此处用 try-except 包裹导入与调用，失败时记录错误并返回降级报告。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.ragas_evaluation import (
    RagasCaseDetail,
    RagasEvaluationReport,
    RagasTestCase,
    RagasTestSet,
)

logger = get_logger("app.knowledge.ragas_evaluator")

# 报告目录名：位于 CHROMA_PERSIST_DIR 下，与 evaluation_reports 平级
_REPORTS_DIRNAME = "ragas_reports"

# 降级时返回的零指标，避免异常阻断主链路
_ZERO_METRICS = {
    "faithfulness": 0.0,
    "answer_relevancy": 0.0,
    "context_precision": 0.0,
    "context_recall": 0.0,
}


# ----------------------------------------------------------------------
# 内置默认测试集（覆盖 FAQ/订单/退货/会员/账户等场景）
# ----------------------------------------------------------------------


def _build_default_testset() -> RagasTestSet:
    """构造内置 RAGAS 测试集。

    至少 10 条用例，每条包含 question 与 ground_truth（标准答案），
    覆盖 FAQ、订单、退货、会员、账户等核心客服场景，
    便于无外部测试集时也能跑通端到端 RAGAS 评估。
    """
    cases = [
        # ===== 账号与登录 =====
        RagasTestCase(
            question="忘记登录密码怎么办？",
            ground_truth=(
                "如果忘记登录密码，可以在登录页面点击「忘记密码」，"
                "输入注册邮箱后系统会发送重置链接到邮箱，"
                "点击链接即可重置密码。重置链接 30 分钟内有效。"
            ),
        ),
        RagasTestCase(
            question="如何修改绑定的手机号？",
            ground_truth=(
                "登录后进入「账户安全」-「手机号管理」，"
                "通过原手机号验证码校验后即可更换手机号。"
                "若原号已停用，需提交工单人工审核。"
            ),
        ),
        # ===== 订单与支付 =====
        RagasTestCase(
            question="订单支付失败但资金已扣除怎么处理？",
            ground_truth=(
                "银行通常会在 1-3 个工作日内自动退回资金。"
                "若超时未到账，请提供订单号与扣款截图提交工单，"
                "客服将在 24 小时内核实并跟进退款进度。"
            ),
        ),
        RagasTestCase(
            question="可以修改已提交订单的收货地址吗？",
            ground_truth=(
                "订单处于「待发货」状态前可自行修改收货地址，"
                "发货后无法变更。建议下单时仔细核对地址。"
            ),
        ),
        # ===== 退换货政策 =====
        RagasTestCase(
            question="七天无理由退货的范围是什么？",
            ground_truth=(
                "七天无理由退货要求商品保持原包装、附件齐全且不影响二次销售。"
                "定制商品、贴身衣物、已拆封的影音制品、虚拟商品等不支持无理由退货。"
            ),
        ),
        RagasTestCase(
            question="退货运费由谁承担？",
            ground_truth=(
                "质量问题导致的退货运费由商家承担；"
                "非质量问题（如尺寸不合、不喜欢）由买家承担。"
                "建议退货前先与客服确认。"
            ),
        ),
        RagasTestCase(
            question="换货期限是多久？",
            ground_truth=(
                "换货期限为收货后 7 天内，"
                "仅支持同款商品的规格、颜色、尺码变更，不支持跨款换货。"
            ),
        ),
        RagasTestCase(
            question="退款多久能到账？",
            ground_truth=(
                "退货商品签收后，商家需在 3 个工作日内完成质检并退款。"
                "退款原路返回：支付宝/微信 1-3 个工作日到账，"
                "银行卡 3-7 个工作日到账，信用卡 7-15 个工作日到账。"
            ),
        ),
        # ===== 会员与积分 =====
        RagasTestCase(
            question="会员等级如何升级？",
            ground_truth=(
                "会员等级按近 90 天的有效消费金额累计计算："
                "银卡满 500 元、金卡满 2000 元、钻石卡满 8000 元。"
                "等级每月初自动结算。"
            ),
        ),
        RagasTestCase(
            question="积分有效期是多久？",
            ground_truth=(
                "积分自获取之日起 12 个月内有效，过期自动清零。"
                "建议在积分到期前登录「我的积分」页面兑换礼品或抵扣订单。"
            ),
        ),
        # ===== 产品手册与系统功能 =====
        RagasTestCase(
            question="知识库支持哪些文档格式？",
            ground_truth=(
                "知识库支持 Markdown、PDF、Word、HTML、TXT 五种文档格式，"
                "单文件最大 50MB。"
            ),
        ),
        RagasTestCase(
            question="客服热线电话是多少？",
            ground_truth=(
                "客服热线 400-888-8888，工作日 9:00-21:00 接听。"
                "复杂工单、大额退款、跨部门协调等问题建议拨打热线。"
            ),
        ),
    ]
    return RagasTestSet(
        cases=cases,
        meta={"version": "default-v1", "description": "内置 RAGAS 默认测试集"},
    )


# 模块级常量：避免每次调用都重新构造
DEFAULT_TESTSET: RagasTestSet = _build_default_testset()


# ----------------------------------------------------------------------
# RagasEvaluator
# ----------------------------------------------------------------------


class RagasEvaluator:
    """RAGAS 评估运行器：编排 检索 → 生成 → LLM 评分 全链路。

    - load_testset：加载外部 JSON 或用内置默认集；
    - run：执行评测，对每条用例检索并生成答案，再批量调用 RAGAS 评分；
    - list_reports / get_report：查询历史报告。

    线程安全：评测状态与报告缓存用 RLock 保护，避免并发评测污染结果。
    降级策略：ragas 未安装或 LLM_API_KEY 为空时返回降级报告（指标全 0）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 报告缓存：report_id -> RagasEvaluationReport，避免重复读盘
        self._report_cache: Dict[str, RagasEvaluationReport] = {}

    def load_testset(self, path: Optional[str] = None) -> RagasTestSet:
        """加载测试集，path 为空或加载失败时用内置默认集。"""
        if not path:
            return DEFAULT_TESTSET.model_copy()

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            testset = RagasTestSet(**data)
            testset.meta["source"] = f"external:{path}"
            return testset
        except Exception as exc:
            logger.warning(
                "加载外部 RAGAS 测试集失败，降级到默认集：%s path=%s",
                exc,
                path,
            )
            return DEFAULT_TESTSET.model_copy()

    def run(
        self,
        testset: Optional[RagasTestSet] = None,
        top_k: Optional[int] = None,
    ) -> RagasEvaluationReport:
        """执行 RAGAS 评测：检索 → 生成答案 → LLM 评分 → 聚合报告。

        流程：
        1. 加载测试集（传入或内置）
        2. 对每条用例：检索 contexts → 生成 answer → 收集四元组
        3. 批量调用 RAGAS 计算四项指标
        4. 聚合并持久化报告

        ragas 未安装或调用异常时返回降级报告（指标全 0），不中断主链路。
        """
        with self._lock:
            effective_testset = testset or DEFAULT_TESTSET
            return self._run_evaluation(effective_testset, top_k)

    def list_reports(self) -> List[Dict[str, object]]:
        """列出历史 RAGAS 报告摘要，按时间倒序返回。"""
        with self._lock:
            reports = self._load_all_reports()
            summaries = [self._to_summary(r) for r in reports]
            # 按时间倒序：最新在前
            summaries.sort(key=lambda x: x["created_at"], reverse=True)
            return summaries

    def get_report(
        self, report_id: str
    ) -> Optional[RagasEvaluationReport]:
        """查询单个报告详情，不存在时返回 None。"""
        with self._lock:
            if report_id in self._report_cache:
                return self._report_cache[report_id]
            path = self._report_path(report_id)
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                report = RagasEvaluationReport(**payload)
                self._report_cache[report_id] = report
                return report
            except Exception as exc:
                logger.warning(
                    "读取 RAGAS 报告失败 report_id=%s：%s", report_id, exc
                )
                return None

    # ----- 内部实现 -----

    def _run_evaluation(
        self, testset: RagasTestSet, top_k: Optional[int]
    ) -> RagasEvaluationReport:
        """单次 RAGAS 评测核心逻辑：检索生成 → LLM 评分 → 聚合。"""
        start = time.time()
        # 1. 收集每条用例的 question/answer/contexts/ground_truth
        samples = self._collect_samples(testset, top_k)

        # 2. 批量调用 RAGAS 计算指标（失败时降级零指标）
        metrics_per_case = self._compute_ragas_metrics(samples)

        # 3. 组装单条详情
        details: List[RagasCaseDetail] = []
        for sample, metrics in zip(samples, metrics_per_case):
            details.append(
                RagasCaseDetail(
                    question=sample["question"],
                    ground_truth=sample["ground_truth"],
                    answer=sample["answer"],
                    contexts=sample["contexts"],
                    faithfulness=metrics.get("faithfulness", 0.0),
                    answer_relevancy=metrics.get("answer_relevancy", 0.0),
                    context_precision=metrics.get("context_precision", 0.0),
                    context_recall=metrics.get("context_recall", 0.0),
                    error=sample.get("error"),
                )
            )

        # 4. 聚合指标：取所有用例的算术平均
        agg_metrics = self._aggregate_metrics(details)
        duration = time.time() - start
        report_id = self._generate_report_id()
        report = RagasEvaluationReport(
            report_id=report_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            total_queries=len(testset.cases),
            case_details=details,
            duration_seconds=round(duration, 4),
            **agg_metrics,
        )

        # 持久化失败不影响返回
        self._persist_report(report)
        self._report_cache[report.report_id] = report
        logger.info(
            "RAGAS 评测完成：total=%d faithfulness=%.3f answer_relevancy=%.3f "
            "context_precision=%.3f context_recall=%.3f",
            report.total_queries,
            report.faithfulness,
            report.answer_relevancy,
            report.context_precision,
            report.context_recall,
        )
        return report

    def _collect_samples(
        self, testset: RagasTestSet, top_k: Optional[int]
    ) -> List[Dict[str, Any]]:
        """对每条用例执行检索与生成，收集 RAGAS 评分所需的样本。

        每条样本包含：question, answer, contexts, ground_truth。
        检索或生成异常时记 error，不中断整体评测。
        """
        # 延迟导入：避免在模块加载阶段触发单例创建
        from app.agents.rag_agent import get_rag_agent

        agent = get_rag_agent()
        samples: List[Dict[str, Any]] = []
        # 检索结果缓存：相同 question 不重复检索，避免重复计算
        retrieve_cache: Dict[str, List[str]] = {}

        for case in testset.cases:
            sample = self._collect_single_sample(
                case, agent, top_k, retrieve_cache
            )
            samples.append(sample)
        return samples

    @staticmethod
    def _collect_single_sample(
        case: RagasTestCase,
        agent: Any,
        top_k: Optional[int],
        retrieve_cache: Dict[str, List[str]],
    ) -> Dict[str, Any]:
        """收集单条用例的样本数据：检索 contexts + 生成 answer。

        复用 RAGAgent.answer 完成检索+生成链路，并从 retrieved_chunks
        提取 contexts 文本列表供 RAGAS 评分。
        """
        try:
            rag_answer = agent.answer(
                question=case.question, top_k=top_k
            )
            # 优先复用缓存：相同问题不重复检索
            contexts = retrieve_cache.get(case.question)
            if contexts is None:
                contexts = [
                    chunk.text for chunk in rag_answer.retrieved_chunks
                ]
                retrieve_cache[case.question] = contexts
            return {
                "question": case.question,
                "ground_truth": case.ground_truth,
                "answer": rag_answer.answer,
                "contexts": contexts,
                "error": None,
            }
        except Exception as exc:
            # 检索或生成异常：记 error，contexts/answer 留空
            logger.warning(
                "RAGAS 用例生成失败 question=%r：%s",
                case.question[:30],
                exc,
            )
            return {
                "question": case.question,
                "ground_truth": case.ground_truth,
                "answer": "",
                "contexts": [],
                "error": str(exc),
            }

    def _compute_ragas_metrics(
        self, samples: List[Dict[str, Any]]
    ) -> List[Dict[str, float]]:
        """调用 RAGAS 批量计算四项指标。

        ragas 未安装或调用异常时返回全零指标，不中断评测。
        RAGAS 0.2.x 的 API 在不同小版本有差异，此处做兼容处理。
        """
        if not samples:
            return []

        try:
            return self._call_ragas_evaluate(samples)
        except Exception as exc:
            # RAGAS 调用失败：降级返回全零指标，保证评测不中断
            logger.warning(
                "RAGAS 评估调用失败，降级返回零指标：%s", exc
            )
            return [dict(_ZERO_METRICS) for _ in samples]

    @staticmethod
    def _call_ragas_evaluate(
        samples: List[Dict[str, Any]]
    ) -> List[Dict[str, float]]:
        """调用 ragas.evaluate 计算指标，返回每条样本的指标字典。

        兼容 ragas 0.2.x 的 API：
        - 优先用 LangchainLLMWrapper 包装现有 LLM 客户端
        - 失败时降级为 OpenAI 客户端直传
        - 解析结果时兼容 dict / 对象属性两种访问方式
        """
        # 延迟导入：ragas 未安装时此处抛 ImportError，由上层捕获降级
        from ragas import evaluate
        from ragas.metrics import (
            answer_relevancy,
            context_precision,
            context_recall,
            faithfulness,
        )

        # 构造 RAGAS 评估器所需的 LLM wrapper
        llm_wrapper = RagasEvaluator._build_ragas_llm()
        if llm_wrapper is None:
            raise RuntimeError("无法构造 RAGAS LLM wrapper，LLM_API_KEY 未配置")

        # 构造 Dataset：ragas 0.2.x 接受含 question/answer/contexts/ground_truth 的数据集
        dataset = RagasEvaluator._build_ragas_dataset(samples)

        # 调用 evaluate：metrics 指定四项核心指标，llm 复用现有客户端
        result = evaluate(
            dataset=dataset,
            metrics=[
                faithfulness,
                answer_relevancy,
                context_precision,
                context_recall,
            ],
            llm=llm_wrapper,
        )

        return RagasEvaluator._parse_ragas_result(result, len(samples))

    @staticmethod
    def _build_ragas_llm() -> Optional[Any]:
        """构造 RAGAS 评估所需的 LLM wrapper。

        复用现有 LLMClient 的 OpenAI 兼容接口，避免引入额外 LLM 依赖。
        优先用 LangchainLLMWrapper + ChatOpenAI，失败时返回 None。
        """
        from app.agents.llm_client import get_llm_client
        from app.core.config import get_settings

        settings = get_settings()
        # LLM_API_KEY 为空时无法构造 RAGAS LLM wrapper
        if not settings.LLM_API_KEY:
            return None

        client = get_llm_client()
        # mock 模式下 client 没有真实 OpenAI 客户端，无法供 RAGAS 使用
        if client.is_mock:
            return None

        try:
            # 优先使用 LangchainLLMWrapper 包装 ChatOpenAI
            # ragas 0.2.x 推荐 LangchainLLMWrapper，需 langchain-openai
            from langchain_openai import ChatOpenAI
            from ragas.llms import LangchainLLMWrapper

            chat_model = ChatOpenAI(
                model=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
                temperature=0,
            )
            return LangchainLLMWrapper(chat_model)
        except ImportError as exc:
            # langchain-openai 未安装：尝试用 OpenAI client 直传
            logger.warning(
                "LangchainLLMWrapper 构造失败（%s），尝试直传 OpenAI client",
                exc,
            )
            try:
                from openai import OpenAI

                return OpenAI(
                    api_key=settings.LLM_API_KEY,
                    base_url=settings.LLM_BASE_URL,
                )
            except Exception as fallback_exc:
                logger.warning(
                    "OpenAI 客户端构造也失败：%s", fallback_exc
                )
                return None
        except Exception as exc:
            logger.warning("RAGAS LLM wrapper 构造失败：%s", exc)
            return None

    @staticmethod
    def _build_ragas_dataset(samples: List[Dict[str, Any]]) -> Any:
        """构造 RAGAS evaluate 接受的数据集对象。

        ragas 0.2.x 推荐使用 EvaluationDataset，由 Dataset 字典列表构造；
        不同小版本也兼容 pandas DataFrame / HuggingFace Dataset。
        优先用 EvaluationDataset，失败时回退到字典列表。
        """
        # 构造每条样本的字段：ragas 0.2.x 用 dataclass 封装
        try:
            from ragas.dataset_schema import (
                EvaluationDataset,
                SingleTurnSample,
            )

            single_samples = []
            for s in samples:
                single_samples.append(
                    SingleTurnSample(
                        user_input=s["question"],
                        response=s["answer"],
                        retrieved_contexts=s["contexts"],
                        reference=s["ground_truth"],
                    )
                )
            return EvaluationDataset(single_samples)
        except ImportError:
            # 旧版 ragas 接受字典列表 / Dataset，回退构造
            logger.info(
                "ragas EvaluationDataset 不可用，回退到字典列表格式"
            )
            return [
                {
                    "question": s["question"],
                    "answer": s["answer"],
                    "contexts": s["contexts"],
                    "ground_truth": s["ground_truth"],
                }
                for s in samples
            ]
        except Exception as exc:
            # SingleTurnSample 字段名差异等：回退到字典列表
            logger.warning(
                "EvaluationDataset 构造失败，回退字典列表：%s", exc
            )
            return [
                {
                    "question": s["question"],
                    "answer": s["answer"],
                    "contexts": s["contexts"],
                    "ground_truth": s["ground_truth"],
                }
                for s in samples
            ]

    @staticmethod
    def _parse_ragas_result(
        result: Any, sample_count: int
    ) -> List[Dict[str, float]]:
        """解析 RAGAS evaluate 返回的结果，统一为每条样本的指标字典列表。

        兼容多种返回形态：
        - Result 对象含 to_pandas / to_list 方法
        - 直接是 dict（按列存储）
        - 其他可迭代结构
        """
        metric_keys = [
            "faithfulness",
            "answer_relevancy",
            "context_precision",
            "context_recall",
        ]

        # 1. 优先尝试 to_pandas：ragas 0.2.x Result 对象常见入口
        try:
            df = result.to_pandas()
            results: List[Dict[str, float]] = []
            for idx in range(len(df)):
                row_metrics: Dict[str, float] = {}
                for key in metric_keys:
                    if key in df.columns:
                        value = df.iloc[idx][key]
                        row_metrics[key] = (
                            float(value) if value is not None else 0.0
                        )
                    else:
                        row_metrics[key] = 0.0
                results.append(row_metrics)
            return results
        except Exception:
            pass

        # 2. 尝试 to_list：部分版本返回列表
        try:
            items = list(result.to_list())
            results = []
            for item in items:
                row_metrics = {}
                for key in metric_keys:
                    value = item.get(key) if isinstance(item, dict) else None
                    row_metrics[key] = (
                        float(value) if value is not None else 0.0
                    )
                results.append(row_metrics)
            if results:
                return results
        except Exception:
            pass

        # 3. 直接是 dict（按列存储）：{metric: [v1, v2, ...]}
        if isinstance(result, dict):
            results = []
            for idx in range(sample_count):
                row_metrics = {}
                for key in metric_keys:
                    col = result.get(key, [])
                    if isinstance(col, list) and idx < len(col):
                        value = col[idx]
                        row_metrics[key] = (
                            float(value) if value is not None else 0.0
                        )
                    else:
                        row_metrics[key] = 0.0
                results.append(row_metrics)
            return results

        # 4. 兜底：无法解析，返回全零
        logger.warning(
            "RAGAS 结果解析失败，返回全零指标：result_type=%s",
            type(result).__name__,
        )
        return [dict(_ZERO_METRICS) for _ in range(sample_count)]

    @staticmethod
    def _aggregate_metrics(
        details: List[RagasCaseDetail],
    ) -> Dict[str, float]:
        """聚合所有用例的指标：取算术平均，无有效用例时返回全零。"""
        if not details:
            return dict(_ZERO_METRICS)
        return {
            "faithfulness": round(
                sum(d.faithfulness for d in details) / len(details), 4
            ),
            "answer_relevancy": round(
                sum(d.answer_relevancy for d in details) / len(details), 4
            ),
            "context_precision": round(
                sum(d.context_precision for d in details) / len(details), 4
            ),
            "context_recall": round(
                sum(d.context_recall for d in details) / len(details), 4
            ),
        }

    @staticmethod
    def _generate_report_id() -> str:
        """生成报告 ID：时间戳 + 短 UUID，避免冲突。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"{timestamp}_{short_uuid}"

    # ----- 报告持久化（复用现有 evaluation.py 的模式）-----

    def _reports_dir(self) -> Path:
        """返回 RAGAS 报告持久化目录。"""
        settings = get_settings()
        return Path(settings.CHROMA_PERSIST_DIR) / _REPORTS_DIRNAME

    def _report_path(self, report_id: str) -> Path:
        """返回单条报告的持久化路径。"""
        return self._reports_dir() / f"{report_id}.json"

    def _persist_report(self, report: RagasEvaluationReport) -> None:
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
            logger.warning(
                "RAGAS 报告持久化失败 report_id=%s：%s",
                report.report_id,
                exc,
            )

    def _load_all_reports(self) -> List[RagasEvaluationReport]:
        """加载所有持久化 RAGAS 报告。"""
        reports: List[RagasEvaluationReport] = []
        reports_dir = self._reports_dir()
        if not reports_dir.exists():
            return reports
        for path in sorted(reports_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                reports.append(RagasEvaluationReport(**payload))
            except Exception as exc:
                logger.warning(
                    "加载 RAGAS 报告失败 path=%s：%s", path, exc
                )
        return reports

    @staticmethod
    def _to_summary(
        report: RagasEvaluationReport,
    ) -> Dict[str, object]:
        """将报告转为摘要字典，用于列表展示。"""
        return {
            "report_id": report.report_id,
            "created_at": report.created_at,
            "total_queries": report.total_queries,
            "faithfulness": report.faithfulness,
            "answer_relevancy": report.answer_relevancy,
            "context_precision": report.context_precision,
            "context_recall": report.context_recall,
            "duration_seconds": report.duration_seconds,
            "source": report.source,
        }


# ----------------------------------------------------------------------
# 可用性检查（供 API 层判断是否返回 503）
# ----------------------------------------------------------------------


def is_ragas_available() -> bool:
    """检查 RAGAS 评估是否可用：ragas 已安装 且 LLM_API_KEY 已配置。

    供 API 端点判断是否返回 503，避免在不可用时触发评测。
    """
    settings = get_settings()
    if not settings.LLM_API_KEY:
        return False
    try:
        import ragas  # noqa: F401

        return True
    except ImportError:
        return False


# ----------------------------------------------------------------------
# 模块级单例：评测状态进程内共享
# ----------------------------------------------------------------------

_ragas_evaluator: Optional[RagasEvaluator] = None
_evaluator_lock = threading.Lock()


def get_ragas_evaluator() -> RagasEvaluator:
    """获取 RagasEvaluator 单例。

    双重检查锁避免并发首调重复创建。
    """
    global _ragas_evaluator
    if _ragas_evaluator is None:
        with _evaluator_lock:
            if _ragas_evaluator is None:
                _ragas_evaluator = RagasEvaluator()
    return _ragas_evaluator


def reset_ragas_evaluator() -> None:
    """重置单例，便于测试切换持久化目录。"""
    global _ragas_evaluator
    with _evaluator_lock:
        _ragas_evaluator = None
