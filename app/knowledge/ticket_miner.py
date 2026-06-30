"""历史工单知识挖掘核心模块（Task 18）。

实现「意图标注 + 答案抽取 + 去重 + 入库」的端到端挖掘流程，
将历史工单沉淀为可复用知识补充到向量库。

模块组成：
- IntentTagger：基于关键词规则的意图标注，内置常见客服意图词典
- AnswerExtractor：从 solution 抽取答案，自动脱敏手机号/身份证/订单号/邮箱
- TicketMiner：编排挖掘主流程，RLock 保护并发，提供模块级单例

设计要点：
- 不引入新依赖，复用现有 embeddings / vectorstore / ticket_store
- 单工单处理失败不影响其他，错误计入报告
- 入库失败时跳过该条不抛错
- 意图标签缓存避免重复计算
- 去重以「同意图 + cosine 相似度」判定，阈值默认 0.92
- Ticket schema 当前未含 solution 字段，自动回退到 description 作为兜底
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.schemas.mining import MinedKnowledgeItem, MiningReport
from app.schemas.ticket import Ticket, TicketStatus

logger = get_logger("app.knowledge.ticket_miner")

# 默认意图：未命中任何关键词时归入此类
DEFAULT_INTENT = "其他咨询"

# 工单去重相似度阈值：高于该值视为同意图下的相似答案
DEFAULT_DEDUP_THRESHOLD = 0.92

# 答案为空时跳过入库的最小长度
MIN_ANSWER_LENGTH = 4


class IntentTagger:
    """基于关键词规则的意图标注器。

    内置客服场景常见意图词典（退货/物流/产品/账户/支付/活动/优惠/技术/投诉/咨询），
    按「先匹配具体意图、再回退到 category 默认意图」的顺序判定。
    无 LLM 依赖，离线可用。

    线程安全：内置 RLock 保护缓存读写。
    """

    # 意图词典：意图标签 -> 关键词列表
    # 顺序即匹配优先级：靠前的优先匹配，避免被宽泛词抢占
    INTENT_KEYWORDS: List[Tuple[str, List[str]]] = [
        ("退货咨询", ["退货", "退换", "退掉", "7天", "七天无理由", "退回"]),
        ("退款咨询", ["退款", "退钱", "退还款", "原路退回", "仅退款"]),
        ("维修咨询", ["维修", "保修", "报修", "修一下", "上门维修"]),
        ("物流查询", ["物流", "快递", "发货", "配送", "送达", "运单", "运费", "揽收"]),
        ("产品投诉", ["投诉", "差评", "态度差", "服务差", "不满", "举报"]),
        ("产品质量", ["质量", "故障", "坏了", "破损", "假货", "瑕疵", "不能用"]),
        ("功能咨询", ["功能", "怎么用", "如何使用", "操作", "说明书", "不会用"]),
        ("账户问题", ["登录", "密码", "账号", "账户", "注册", "找回"]),
        ("支付问题", ["支付", "扣款", "充值", "付款", "钱包", "未付款", "重复扣款"]),
        ("活动咨询", ["活动", "促销", "秒杀", "预售", "抢购"]),
        ("优惠咨询", ["优惠", "优惠券", "红包", "折扣", "满减", "折扣码"]),
        ("技术支持", ["报错", "异常", "崩溃", "闪退", "卡顿", "网络错误"]),
    ]

    # 工单分类到默认意图的兜底映射
    CATEGORY_DEFAULT_INTENT: Dict[str, str] = {
        "after_sale": "售后咨询",
        "logistics": "物流查询",
        "product": "产品咨询",
        "account": "账户问题",
        "complaint": "服务投诉",
    }

    def __init__(self) -> None:
        # 意图缓存：(description_hash + category) -> intent
        # 避免相同描述重复走关键词扫描
        self._cache: Dict[str, str] = {}
        self._lock = threading.RLock()

    def tag(self, description: str, category: str) -> str:
        """标注意图标签。

        优先用关键词匹配具体意图，未命中时回退到 category 默认意图，
        都不命中时返回 DEFAULT_INTENT，保证总有标注。
        """
        if not description:
            return self._fallback(category)

        cache_key = self._cache_key(description, category)
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        intent = self._match_by_keywords(description)
        if intent is None:
            intent = self._fallback(category)

        with self._lock:
            self._cache[cache_key] = intent
        return intent

    @classmethod
    def _match_by_keywords(cls, description: str) -> Optional[str]:
        """按词典优先级匹配意图，未命中返回 None。"""
        for intent, keywords in cls.INTENT_KEYWORDS:
            if any(keyword in description for keyword in keywords):
                return intent
        return None

    @classmethod
    def _fallback(cls, category: str) -> str:
        """分类未识别时回退到默认意图。"""
        return cls.CATEGORY_DEFAULT_INTENT.get(category, DEFAULT_INTENT)

    @staticmethod
    def _cache_key(description: str, category: str) -> str:
        """生成缓存键：description 哈希 + category，避免长字符串占内存。"""
        digest = hashlib.sha1(description.encode("utf-8")).hexdigest()[:16]
        return f"{digest}:{category}"

    def clear_cache(self) -> None:
        """清空意图缓存，便于测试隔离与内存回收。"""
        with self._lock:
            self._cache.clear()


class AnswerExtractor:
    """从工单 solution 抽取可复用答案片段。

    职责：
    - 脱敏：手机号 / 身份证 / 订单号 / 邮箱替换为占位符
    - 清洗：去除多余空白与首尾空白，保留核心解决步骤
    - 截断：超过最大长度时截断，避免单条过长影响检索

    无状态、线程安全，可作为单例复用。
    """

    # 脱敏正则与替换占位符
    # 手机号：1 开头 11 位数字
    PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")
    # 身份证：18 位（最后一位可为 X），宽松匹配避免漏召回
    ID_CARD_PATTERN = re.compile(r"\b[1-9]\d{16}[0-9Xx]\b")
    # 订单号：订单/单号 后接数字/字母组合（6 位以上）
    ORDER_PATTERN = re.compile(
        r"(?:订单号|订单|单号|order)\s*[:：]?\s*([A-Za-z0-9\-]{6,})",
        re.IGNORECASE,
    )
    # 邮箱：常见邮箱格式
    EMAIL_PATTERN = re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    )

    # 占位符：脱敏后保留语义但去除敏感信息
    PHONE_PLACEHOLDER = "[手机号]"
    ID_CARD_PLACEHOLDER = "[身份证]"
    ORDER_PLACEHOLDER = "[订单号]"
    EMAIL_PLACEHOLDER = "[邮箱]"

    # 答案最大长度：超过则截断，控制存储与展示成本
    MAX_ANSWER_LENGTH = 800

    def extract(self, solution: str) -> str:
        """抽取并清洗答案。

        步骤：脱敏 -> 去多余空白 -> 截断 -> 去首尾空白。
        空字符串或仅空白返回空串，由上层判断是否跳过入库。
        """
        if not solution:
            return ""

        text = self._desensitize(solution)
        text = self._normalize_whitespace(text)
        text = text[: self.MAX_ANSWER_LENGTH]
        return text.strip()

    @classmethod
    def _desensitize(cls, text: str) -> str:
        """依次脱敏邮箱/身份证/手机号/订单号。

        顺序很重要：邮箱必须在手机号之前，避免邮箱本地段被误判为手机号；
        订单号脱敏用整体替换，保留「订单号」字样便于语义理解。
        """
        text = cls.EMAIL_PATTERN.sub(cls.EMAIL_PLACEHOLDER, text)
        text = cls.ID_CARD_PATTERN.sub(cls.ID_CARD_PLACEHOLDER, text)
        text = cls.PHONE_PATTERN.sub(cls.PHONE_PLACEHOLDER, text)
        text = cls.ORDER_PATTERN.sub(cls.ORDER_PLACEHOLDER, text)
        return text

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        """归一化空白：连续空白压成单个空格，保留换行。"""
        # 仅压缩非换行空白，保留段落结构
        return re.sub(r"[^\S\n]+", " ", text)


class TicketMiner:
    """历史工单知识挖掘器。

    主流程：拉工单 -> 标注意图 -> 抽取答案 -> 同意图去重 -> 入库
    - RLock 保护 _last_report 与共享状态，支持并发触发挖掘
    - 单工单失败不影响其他，错误计入报告
    - 入库失败时跳过该条不抛错

    入库方式：直接调用 vector_store.add_chunks，绕过 pipeline 的解析切分，
    避免对短文本做无谓的文件 IO；metadata 写入 source_ticket_id/intent/category/priority/knowledge_type。
    """

    def __init__(
        self,
        intent_tagger: Optional[IntentTagger] = None,
        answer_extractor: Optional[AnswerExtractor] = None,
        dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> None:
        self._intent_tagger = intent_tagger or IntentTagger()
        self._answer_extractor = answer_extractor or AnswerExtractor()
        self.dedup_threshold = dedup_threshold
        self._lock = threading.RLock()
        # 最近一次挖掘报告，供 /status 端点查询
        self._last_report: Optional[MiningReport] = None

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def mine(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        status: Optional[str] = None,
    ) -> MiningReport:
        """执行一次挖掘并返回报告。

        参数：
        - start_time / end_time：按 created_at 过滤，闭区间
        - status：按 TicketStatus 过滤，None 表示不过滤

        降级策略：
        - ticket_store 为空 -> 返回空报告
        - status 非法 -> 视为不过滤
        - 单工单异常 -> 计入 errors，继续其他工单
        """
        started_at = datetime.now(timezone.utc)
        start_ts = time.time()

        # 拉工单：复用 TicketStore 单例
        tickets = self._fetch_tickets(status)

        # 同意图去重缓存：intent -> [(embedding, ticket_id), ...]
        # 仅本次挖掘生命周期内有效，避免跨次挖掘误判
        dedup_cache: Dict[str, List[Tuple[List[float], str]]] = {}

        items: List[MinedKnowledgeItem] = []
        errors: List[str] = []
        processed = 0
        ingested = 0
        deduped_count = 0
        skipped_count = 0
        failed_count = 0

        for ticket in tickets:
            # 时间过滤：created_at 为 UTC，传入时间也视为 UTC 比较
            if not self._in_time_range(ticket, start_time, end_time):
                continue

            try:
                item = self._process_ticket(ticket, dedup_cache)
            except Exception as exc:
                # 单工单异常不中断整体流程，记入错误列表
                failed_count += 1
                msg = f"ticket_id={ticket.ticket_id} 处理失败：{exc}"
                errors.append(msg)
                logger.warning("工单挖掘异常：%s", msg)
                continue

            if item is None:
                # 抽取失败或答案为空，跳过
                skipped_count += 1
                continue

            processed += 1

            if item.skip_reason == "duplicate":
                deduped_count += 1
                items.append(item)
                continue

            # 入库：失败时回填 skip_reason 不抛错
            self._ingest_item(item)
            if item.ingested:
                ingested += 1
                # 入库成功才加入去重缓存，避免入库失败的条目被误判为已存在
                self._add_to_dedup_cache(dedup_cache, item)
            else:
                # 入库失败计入 failed，但 item 已有 skip_reason 便于排查
                failed_count += 1
            items.append(item)

        finished_at = datetime.now(timezone.utc)
        report = MiningReport(
            started_at=started_at,
            finished_at=finished_at,
            total_tickets=len(tickets),
            processed=processed,
            ingested=ingested,
            deduped=deduped_count,
            skipped=skipped_count,
            failed=failed_count,
            duration_seconds=time.time() - start_ts,
            items=items,
            errors=errors,
            filters=self._build_filter_snapshot(start_time, end_time, status),
        )

        with self._lock:
            self._last_report = report

        logger.info(
            "工单挖掘完成：total=%d processed=%d ingested=%d deduped=%d skipped=%d failed=%d 耗时=%.2fs",
            report.total_tickets,
            report.processed,
            report.ingested,
            report.deduped,
            report.skipped,
            report.failed,
            report.duration_seconds,
        )
        return report

    def get_last_report(self) -> Optional[MiningReport]:
        """返回最近一次挖掘报告，未挖掘过返回 None。"""
        with self._lock:
            return self._last_report.model_copy() if self._last_report else None

    # ------------------------------------------------------------------
    # 工单拉取与过滤
    # ------------------------------------------------------------------
    @staticmethod
    def _fetch_tickets(status: Optional[str]) -> List[Ticket]:
        """从 TicketStore 拉取工单。

        status 合法时按状态过滤；非法或 None 时返回全部工单。
        store 为空时返回空列表（降级策略）。
        """
        from app.agents.ticket_store import get_ticket_store

        store = get_ticket_store()
        if status:
            try:
                ticket_status = TicketStatus(status)
                return store.list_tickets_by_status(ticket_status)
            except ValueError:
                # 非法状态视为不过滤，避免 API 因参数错误返回空
                logger.warning("非法 status=%s，视为不过滤", status)
        return store.list_tickets()

    @staticmethod
    def _in_time_range(
        ticket: Ticket,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> bool:
        """判断工单 created_at 是否落在 [start_time, end_time] 内。

        带时区与无时区时间都按 timestamp 比较，避免时区差异导致漏召回。
        """
        created_ts = ticket.created_at.timestamp()
        if start_time is not None and created_ts < start_time.timestamp():
            return False
        if end_time is not None and created_ts > end_time.timestamp():
            return False
        return True

    @staticmethod
    def _build_filter_snapshot(
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        status: Optional[str],
    ) -> dict:
        """构造过滤条件快照，写入报告便于审计。"""
        snapshot: Dict[str, Any] = {}
        if start_time is not None:
            snapshot["start_time"] = start_time.isoformat()
        if end_time is not None:
            snapshot["end_time"] = end_time.isoformat()
        if status:
            snapshot["status"] = status
        return snapshot

    # ------------------------------------------------------------------
    # 单工单处理：标注 + 抽取 + 去重判定
    # ------------------------------------------------------------------
    def _process_ticket(
        self,
        ticket: Ticket,
        dedup_cache: Dict[str, List[Tuple[List[float], str]]],
    ) -> Optional[MinedKnowledgeItem]:
        """处理单个工单：标注意图、抽取答案、判定去重。

        返回 None 表示答案为空跳过；
        返回 item 且 skip_reason='duplicate' 表示去重跳过；
        返回 item 且 skip_reason=None 表示待入库。
        """
        # Ticket schema 当前未含 solution 字段，自动回退到 description
        solution = self._get_solution_text(ticket)
        answer = self._answer_extractor.extract(solution)
        if len(answer) < MIN_ANSWER_LENGTH:
            # 答案过短无复用价值，跳过
            return None

        intent = self._intent_tagger.tag(ticket.description, ticket.category.value)

        # 同意图去重：embedding cosine 相似度
        embedding = self._embed_text(answer)
        duplicate_ticket_id = self._find_duplicate(dedup_cache, intent, embedding)
        if duplicate_ticket_id is not None:
            return MinedKnowledgeItem(
                source_ticket_id=ticket.ticket_id,
                intent=intent,
                answer=answer,
                category=ticket.category.value,
                priority=ticket.priority.value,
                ingested=False,
                skip_reason="duplicate",
            )

        return MinedKnowledgeItem(
            source_ticket_id=ticket.ticket_id,
            intent=intent,
            answer=answer,
            category=ticket.category.value,
            priority=ticket.priority.value,
            ingested=False,
            skip_reason=None,
        )

    @staticmethod
    def _get_solution_text(ticket: Ticket) -> str:
        """获取工单的 solution 文本，schema 未含该字段时回退到 description。

        通过 getattr 兼容未来 schema 扩展（如新增 solution 字段），
        避免修改共享的 ticket.py。
        """
        solution = getattr(ticket, "solution", None)
        if isinstance(solution, str) and solution.strip():
            return solution
        return ticket.description or ""

    @staticmethod
    def _embed_text(text: str) -> List[float]:
        """向量化文本，失败时返回空列表。

        走全局 embedding 服务单例，与流水线保持一致；
        fallback 模式下也能产出确定性向量，保证去重可重复。
        """
        from app.knowledge.embeddings import get_embedding_service

        service = get_embedding_service()
        return service.embed_query(text)

    def _find_duplicate(
        self,
        dedup_cache: Dict[str, List[Tuple[List[float], str]]],
        intent: str,
        embedding: List[float],
    ) -> Optional[str]:
        """在同意图缓存中查找相似答案。

        相似度 >= dedup_threshold 时视为重复，返回被匹配的 ticket_id；
        未匹配返回 None。空 embedding 直接返回 None 避免误判。
        """
        if not embedding:
            return None
        candidates = dedup_cache.get(intent, [])
        for cand_embedding, cand_ticket_id in candidates:
            similarity = self._cosine(embedding, cand_embedding)
            if similarity >= self.dedup_threshold:
                return cand_ticket_id
        return None

    @staticmethod
    def _cosine(vec_a: List[float], vec_b: List[float]) -> float:
        """计算两个向量的余弦相似度。

        长度不一致或零向量时返回 0.0，避免除零异常。
        """
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = sum(a * a for a in vec_a) ** 0.5
        norm_b = sum(b * b for b in vec_b) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _add_to_dedup_cache(
        dedup_cache: Dict[str, List[Tuple[List[float], str]]],
        item: MinedKnowledgeItem,
    ) -> None:
        """将已入库条目加入去重缓存，供后续同意图条目比对。"""
        embedding = TicketMiner._embed_text(item.answer)
        if not embedding:
            return
        dedup_cache.setdefault(item.intent, []).append(
            (embedding, item.source_ticket_id)
        )

    # ------------------------------------------------------------------
    # 入库
    # ------------------------------------------------------------------
    @staticmethod
    def _ingest_item(item: MinedKnowledgeItem) -> None:
        """将单条挖掘知识写入向量库。

        直接调用 vector_store.add_chunks，绕过 pipeline 的解析/切分，
        避免对短文本做无谓 IO；metadata 写入工单溯源字段。
        入库失败仅记日志并保持 ingested=False，不抛错。
        """
        from app.knowledge.vectorstore import get_vector_store
        from app.schemas.knowledge import TextChunk

        try:
            chunk = TextChunk(
                text=item.answer,
                metadata={
                    "knowledge_type": "ticket",
                    "source_ticket_id": item.source_ticket_id,
                    "intent": item.intent,
                    "category": item.category,
                    "priority": item.priority,
                    "source": f"ticket:{item.source_ticket_id}",
                },
            )
            embedding = TicketMiner._embed_text(item.answer)
            if not embedding:
                logger.warning(
                    "工单知识向量化为空，跳过入库：ticket_id=%s",
                    item.source_ticket_id,
                )
                item.skip_reason = "ingest_failed"
                return

            store = get_vector_store()
            added = store.add_chunks([chunk], [embedding], [dict(chunk.metadata)])
            if added > 0:
                item.ingested = True
                logger.info(
                    "工单知识已入库：ticket_id=%s intent=%s",
                    item.source_ticket_id,
                    item.intent,
                )
            else:
                # add_chunks 返回 0 通常是已被全局去重判定为重复
                item.skip_reason = "duplicate"
        except Exception as exc:
            # 入库异常不阻断主流程，仅标记失败
            logger.warning(
                "工单知识入库失败 ticket_id=%s：%s",
                item.source_ticket_id,
                exc,
            )
            item.skip_reason = "ingest_failed"


# 模块级单例：进程内复用，避免重复初始化 tagger/extractor
_ticket_miner: Optional[TicketMiner] = None
_singleton_lock = threading.Lock()


def get_ticket_miner() -> TicketMiner:
    """获取 TicketMiner 单例。"""
    global _ticket_miner
    if _ticket_miner is None:
        with _singleton_lock:
            if _ticket_miner is None:
                _ticket_miner = TicketMiner()
    return _ticket_miner


def reset_ticket_miner() -> None:
    """重置单例，便于测试隔离。"""
    global _ticket_miner
    with _singleton_lock:
        _ticket_miner = None
