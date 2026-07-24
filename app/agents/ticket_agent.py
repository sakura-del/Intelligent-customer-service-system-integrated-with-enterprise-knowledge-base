"""工单处理 Agent（书记员）。

负责从用户对话中提取关键信息、创建工单、自动分类与定级，
并提供工单进度查询入口。

设计要点：
- 信息提取走 LLM 优先 + 规则兜底的双路：LLM 不可用（mock）时
  降级到关键词规则，保证离线环境也能创建工单。
- 分类与定级独立成方法，便于单测与未来替换为模型服务。
- Agent 自身无状态，所有工单数据落在 TicketStore，
 便于多实例部署与持久化扩展。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agents.llm_client import LLMClient, get_llm_client
from app.agents.ticket_store import TicketStore, get_ticket_store
from app.core.logging import get_logger
from app.schemas.ticket import (
    Ticket,
    TicketCategory,
    TicketPriority,
    TicketResult,
    TicketStatus,
)

logger = get_logger("app.agents.ticket_agent")

# 系统 prompt：约束 LLM 输出严格 JSON，便于解析
SYSTEM_PROMPT = (
    "你是一名客服工单书记员。请从用户消息中提取关键信息并生成工单。\n"
    "【输出要求】仅返回一个 JSON 对象，不要添加任何解释或 markdown 标记。\n"
    "【字段说明】\n"
    '  "title": 工单标题（不超过20字，概括核心问题）\n'
    '  "description": 问题描述详情（保留用户原意）\n'
    '  "category": 分类，取值之一：'
    "after_sale(售后:退换货/维修) / logistics(物流:发货/配送) / "
    "product(产品:质量/功能) / account(账户:登录/支付) / "
    "complaint(投诉:服务态度/体验)\n"
    '  "priority": 优先级，取值之一：'
    "urgent(愤怒/VIP/资金损失) / high(影响使用) / "
    "medium(一般咨询) / low(建议反馈)\n"
    '  "related_order": 相关订单号，无则留空字符串\n'
    '  "related_product": 相关产品名，无则留空字符串\n'
    '  "contact": 联系方式（手机号/邮箱），无则留空字符串\n'
)

# 标题最大长度：超过则截断，避免过长影响列表展示
MAX_TITLE_LENGTH = 20
# 描述最大长度：限制存储与展示成本
MAX_DESCRIPTION_LENGTH = 500

# === 规则兜底的关键词表 ===
# 分类关键词：按命中优先级排序，靠前的优先匹配
CATEGORY_KEYWORDS: list[tuple] = [
    (TicketCategory.complaint, ["投诉", "态度差", "服务差", "差评", "不满"]),
    (TicketCategory.after_sale, ["退货", "退款", "换货", "维修", "售后", "保修"]),
    (TicketCategory.logistics, ["发货", "配送", "快递", "物流", "送达", "运费"]),
    (TicketCategory.product, ["质量", "故障", "坏了", "不能用", "破损", "假货", "功能"]),
    (TicketCategory.account, ["登录", "密码", "账号", "账户", "支付", "扣款", "充值"]),
]

# 优先级关键词：从高到低匹配，命中即定级
URGENT_KEYWORDS = [
    "气死",
    "愤怒",
    "太差了",
    "无语",
    "立刻",
    "马上",
    "投诉",
    "VIP",
    "扣款",
    "扣了钱",
    "钱没了",
    "骗钱",
    "资金损失",
]
HIGH_KEYWORDS = ["无法使用", "不能用", "用不了", "影响使用", "出错", "错误", "故障"]
LOW_KEYWORDS = ["建议", "反馈", "期望", "希望", "能不能", "优化"]

# 订单号正则：匹配「订单」「订单号」后接数字/字母组合
ORDER_PATTERN = re.compile(r"(?:订单号|订单|单号)\s*[:：]?\s*([A-Za-z0-9\-]{6,})")
# 手机号正则：11 位数字，宽松匹配避免漏召回
PHONE_PATTERN = re.compile(r"(?:手机|电话|联系)\s*[:：]?\s*(1[3-9]\d{9})")
# 邮箱正则：常见邮箱格式
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


class TicketAgent:
    """工单处理 Agent。

    持有 LLM 客户端与 TicketStore 引用，对外暴露：
    - create_ticket_from_message：从用户消息创建工单
    - query_ticket：查询工单进度
    - update_ticket_status：更新工单状态
    LLM 不可用时走规则提取，保证离线可用。
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        ticket_store: TicketStore | None = None,
    ) -> None:
        # 延迟取单例，便于测试注入自定义实现
        self._llm_client = llm_client
        self._ticket_store = ticket_store

    @property
    def llm_client(self) -> LLMClient:
        """延迟初始化 LLM 客户端，未注入时取全局单例。"""
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    @property
    def ticket_store(self) -> TicketStore:
        """延迟初始化工单存储，未注入时取全局单例。"""
        if self._ticket_store is None:
            self._ticket_store = get_ticket_store()
        return self._ticket_store

    # ==================== 对外核心方法 ====================

    def create_ticket_from_message(
        self,
        message: str,
        user_id: str | None = None,
    ) -> TicketResult:
        """从用户消息提取信息并创建工单。

        LLM 可用时走 LLM 提取，否则降级到规则提取；
        无论哪条路径，最终都通过 TicketStore 落库以保证一致性。
        返回 TicketResult，含给用户的回复与工单关键属性。
        """
        if not message or not message.strip():
            return self._build_empty_result()

        # 1. 信息提取：LLM 优先，失败/空内容降级规则
        info = self._extract_info(message)

        # 2. 兜底：若提取结果缺失分类或优先级，用规则补齐
        info = self._fill_missing_fields(info, message)

        # 3. 落库创建工单
        ticket = self.ticket_store.create_ticket(
            user_id=user_id,
            title=info["title"],
            description=info["description"],
            category=info["category"],
            priority=info["priority"],
            related_order=info.get("related_order") or None,
            related_product=info.get("related_product") or None,
            contact=info.get("contact") or None,
        )

        # 4. 构造给用户的回复
        reply = self._build_reply(ticket)

        logger.info(
            "工单创建完成：ticket_id=%s category=%s priority=%s mock=%s",
            ticket.ticket_id,
            ticket.category.value,
            ticket.priority.value,
            self.llm_client.is_mock,
        )

        return TicketResult(
            reply=reply,
            ticket_id=ticket.ticket_id,
            category=ticket.category,
            priority=ticket.priority,
            status=ticket.status,
        )

    def query_ticket(self, ticket_id: str) -> Ticket | None:
        """查询工单进度，返回 Ticket 或 None。

        由调用方决定如何把状态呈现给用户，
        Agent 不在此处生成回复文本，保持职责单一。
        """
        return self.ticket_store.get_ticket(ticket_id)

    def update_ticket_status(self, ticket_id: str, status: TicketStatus) -> Ticket | None:
        """更新工单状态，返回更新后的 Ticket 或 None。"""
        return self.ticket_store.update_status(ticket_id, status)

    # ==================== 信息提取（LLM + 规则兜底）====================

    def _extract_info(self, message: str) -> dict[str, Any]:
        """提取工单信息：LLM 优先，失败时降级规则。

        LLM 模式下解析 JSON 失败、字段非法或为空时，
        统一回退到规则提取，保证总能产出可用结果。
        """
        if not self.llm_client.is_mock:
            info = self._llm_extract(message)
            if info is not None:
                return info
            logger.warning("LLM 提取失败或结果非法，降级到规则提取")
        return self._rule_extract(message)

    def _llm_extract(self, message: str) -> dict[str, Any] | None:
        """调用 LLM 提取结构化信息并解析 JSON。

        返回 None 表示提取失败需降级；返回 dict 表示成功。
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]
        try:
            # name 标识工单信息提取 prompt，metadata 记录 prompt 版本
            raw = self.llm_client.chat(
                messages=messages,
                temperature=0.1,
                name="ticket_extract",
                metadata={"prompt_version": "v1"},
            )
        except Exception as exc:
            # 调用异常时降级，避免拖垮整个工单创建流程
            logger.warning("LLM 调用异常，降级规则提取：%s", exc)
            return None

        parsed = self._parse_llm_json(raw)
        if parsed is None:
            return None
        # 校验必要字段是否合法
        if not self._validate_extracted(parsed):
            return None
        return parsed

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any] | None:
        """解析 LLM 返回的 JSON，兼容 markdown 代码块包裹。

        LLM 偶尔会在 JSON 外加 ```json ``` 标记，
        这里先剥离代码块再解析，提升鲁棒性。
        """
        if not raw:
            return None
        text = raw.strip()
        # 剥离可能的 markdown 代码块标记
        if text.startswith("```"):
            # 去掉首行 ```json 与结尾 ```
            lines = text.splitlines()
            if len(lines) >= 2:
                lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _validate_extracted(info: dict[str, Any]) -> bool:
        """校验提取结果：必要字段存在且枚举值合法。"""
        if not info.get("title") or not info.get("description"):
            return False
        try:
            TicketCategory(info["category"])
            TicketPriority(info["priority"])
        except (KeyError, ValueError):
            return False
        return True

    def _fill_missing_fields(self, info: dict[str, Any], message: str) -> dict[str, Any]:
        """对缺失或非法的字段用规则补齐，保证最终结果可用。

        即便 LLM 返回了主要字段，contact / related_order 等
        附加字段可能缺失，这里统一用规则兜底补全；
        category / priority 统一归一为枚举实例，避免下游处理裸字符串。
        """
        # 分类：缺失或非法时用规则判定
        info["category"] = self._normalize_category(info.get("category"), message)
        # 优先级：缺失或非法时用规则判定
        info["priority"] = self._normalize_priority(info.get("priority"), message)
        # 附加字段缺失时尝试正则提取
        if not info.get("related_order"):
            info["related_order"] = self._extract_order(message)
        if not info.get("contact"):
            info["contact"] = self._extract_contact(message)
        # 截断超长字段，控制存储成本
        info["title"] = info["title"][:MAX_TITLE_LENGTH]
        info["description"] = info["description"][:MAX_DESCRIPTION_LENGTH]
        return info

    @staticmethod
    def _normalize_category(value: Any, message: str) -> TicketCategory:
        """把 LLM 返回的分类值归一为枚举，非法时回退规则分类。"""
        if isinstance(value, TicketCategory):
            return value
        if isinstance(value, str):
            try:
                return TicketCategory(value)
            except ValueError:
                logger.warning("LLM 返回非法 category=%r，回退规则分类", value)
        return TicketAgent._rule_classify(message)

    @staticmethod
    def _normalize_priority(value: Any, message: str) -> TicketPriority:
        """把 LLM 返回的优先级值归一为枚举，非法时回退规则定级。"""
        if isinstance(value, TicketPriority):
            return value
        if isinstance(value, str):
            try:
                return TicketPriority(value)
            except ValueError:
                logger.warning("LLM 返回非法 priority=%r，回退规则定级", value)
        return TicketAgent._rule_judge_priority(message)

    def _rule_extract(self, message: str) -> dict[str, Any]:
        """规则提取：基于关键词与正则生成工单信息。

        作为 LLM 不可用时的兜底方案，覆盖常见客服场景。
        """
        title = self._build_rule_title(message)
        return {
            "title": title,
            "description": message.strip(),
            "category": self._rule_classify(message),
            "priority": self._rule_judge_priority(message),
            "related_order": self._extract_order(message),
            "related_product": None,
            "contact": self._extract_contact(message),
        }

    @staticmethod
    def _build_rule_title(message: str) -> str:
        """从消息生成标题：取首句或截断到 MAX_TITLE_LENGTH。"""
        text = message.strip()
        # 按句末标点切分，取首句作为标题
        first_sentence = re.split(r"[。！？\n]", text)[0].strip()
        title = first_sentence if first_sentence else text
        return title[:MAX_TITLE_LENGTH]

    @staticmethod
    def _rule_classify(message: str) -> TicketCategory:
        """规则分类：按关键词命中匹配分类。

        complaint 优先级最高，避免投诉被归到具体业务分类
        而错过专人跟进。
        """
        for category, keywords in CATEGORY_KEYWORDS:
            if any(keyword in message for keyword in keywords):
                return category
        # 默认归到售后：客服场景最常见
        return TicketCategory.after_sale

    @staticmethod
    def _rule_judge_priority(message: str) -> TicketPriority:
        """规则定级：从高到低匹配关键词，命中即定级。

        urgent 必须最先判断，避免被 high 的关键词
        （如「故障」）误降到 high。
        """
        if any(keyword in message for keyword in URGENT_KEYWORDS):
            return TicketPriority.urgent
        if any(keyword in message for keyword in HIGH_KEYWORDS):
            return TicketPriority.high
        if any(keyword in message for keyword in LOW_KEYWORDS):
            return TicketPriority.low
        # 默认中等：一般咨询
        return TicketPriority.medium

    @staticmethod
    def _extract_order(message: str) -> str | None:
        """正则提取订单号，未匹配返回 None。"""
        match = ORDER_PATTERN.search(message)
        return match.group(1) if match else None

    @staticmethod
    def _extract_contact(message: str) -> str | None:
        """正则提取联系方式（手机号优先，其次邮箱）。"""
        phone_match = PHONE_PATTERN.search(message)
        if phone_match:
            return phone_match.group(1)
        email_match = EMAIL_PATTERN.search(message)
        if email_match:
            return email_match.group(0)
        return None

    # ==================== 回复构造 ====================

    @staticmethod
    def _build_reply(ticket: Ticket) -> str:
        """构造给用户的工单创建成功回复。

        包含工单号、分类、优先级与下一步处理提示，
        让用户感知到问题已被记录并安排跟进。
        """
        category_text = {
            TicketCategory.after_sale: "售后",
            TicketCategory.logistics: "物流",
            TicketCategory.product: "产品",
            TicketCategory.account: "账户",
            TicketCategory.complaint: "投诉",
        }[ticket.category]
        priority_text = {
            TicketPriority.urgent: "紧急",
            TicketPriority.high: "高",
            TicketPriority.medium: "中",
            TicketPriority.low: "低",
        }[ticket.priority]
        return (
            f"已为您创建工单，工单号：{ticket.ticket_id}\n"
            f"问题分类：{category_text}；优先级：{priority_text}\n"
            f"我们将尽快安排专人跟进处理，您可凭工单号查询进度。"
        )

    @staticmethod
    def _build_empty_result() -> TicketResult:
        """空消息的兜底返回：避免工单系统记录无意义空内容。"""
        return TicketResult(
            reply="请描述您遇到的问题，我将为您创建工单。",
            ticket_id="",
            category=TicketCategory.after_sale,
            priority=TicketPriority.medium,
            status=TicketStatus.pending,
        )


# 模块级单例：Agent 编排无状态，进程内复用
_ticket_agent: TicketAgent | None = None


def get_ticket_agent() -> TicketAgent:
    """获取 TicketAgent 单例。"""
    global _ticket_agent
    if _ticket_agent is None:
        _ticket_agent = TicketAgent()
    return _ticket_agent


def reset_ticket_agent() -> None:
    """重置单例，便于测试切换配置。"""
    global _ticket_agent
    _ticket_agent = None
