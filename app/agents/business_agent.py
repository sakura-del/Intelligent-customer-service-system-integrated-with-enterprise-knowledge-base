"""业务查询 Agent（操作员）。

编排 参数提取 → 身份校验 → 适配器调用 → 结果脱敏与格式化 的完整链路，
对外暴露 BusinessAgent.execute 作为业务查询的统一入口。

设计要点：
- 业务系统通过 get_business_adapter() 工厂获取，可在 mock/http 间配置切换
- 参数提取优先用 LLM（结构化 JSON），失败降级正则+关键词规则，保证离线可用
- 写操作（创建/取消退换货）走二次确认，先生成 confirmation_token 待用户确认
- 敏感信息脱敏在格式化前完成，避免泄漏到日志或回复
- 频率限制计数器加锁，保证多线程下计数准确
"""
from __future__ import annotations

import collections
import json
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from app.agents.business_adapters import (
    BusinessSystemAdapter,
    MockBusinessAdapter,
    get_business_adapter,
)
from app.agents.llm_client import LLMClient, get_llm_client
from app.agents.mock_business_api import MockBusinessAPI
from app.core.logging import get_logger
from app.core.session import SessionManager, session_manager
from app.schemas.business import (
    BusinessResult,
    BusinessScene,
    ExtractedParams,
)

logger = get_logger("app.agents.business_agent")

# 频率限制：同一 session 在窗口内最多调用次数，防止刷接口
RATE_LIMIT_WINDOW_SECONDS = 10
RATE_LIMIT_MAX_CALLS = 5

# 二次确认意图识别词。取消词优先于确认词，避免"不确认"被误判为确认
CONFIRM_WORDS = ("确认", "确定", "是的", "对", "同意", "yes", "y")
CANCEL_WORDS = ("取消", "算了", "撤销", "放弃", "不退", "不要", "不确认", "no", "n")

# 参数提取的 LLM 系统提示与用户提示模板
_EXTRACT_SYSTEM_PROMPT = "你是参数提取助手，只输出 JSON，不要输出其他文字。"
_EXTRACT_USER_PROMPT = """从用户对话中提取业务查询参数，只返回 JSON。
字段：
- scene: order/return/member/account 之一
- action: query/create/cancel（默认 query）
- order_id: 订单号字符串或 null
- return_id: 退换单号字符串或 null
- user_id: 用户标识或 null
- phone: 手机号或 null
- query_type: status/logistics/amount/points/level/coupons/balance/bill/transactions 之一或 null
- reason: 退换货原因或 null
用户对话：{query}"""

# 格式化的 LLM 系统提示
_FORMAT_SYSTEM_PROMPT = (
    "你是企业客服助手，把结构化业务数据转成简洁友好的中文自然语言回复，"
    "金额保留原值，不要编造数据，不要提及脱敏字段。"
)


class BusinessAgent:
    """业务查询 Agent。

    持有 LLM 客户端、业务 API 与会话管理器引用，均延迟取单例，
    便于测试注入自定义实现。execute 串联整条业务查询链路。
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        business_api: Optional[MockBusinessAPI] = None,
        business_adapter: Optional[BusinessSystemAdapter] = None,
        session_manager_ref: Optional[SessionManager] = None,
    ) -> None:
        # 延迟取单例，便于测试注入自定义实现
        self._llm_client = llm_client
        # 适配器优先：新代码用 business_adapter 注入；
        # business_api（MockBusinessAPI）为旧参数，保留向后兼容，自动包装为适配器
        self._business_adapter = business_adapter
        self._legacy_mock_api = business_api
        self._session_manager = session_manager_ref
        # 频率限制计数器：session_id -> 时间戳队列，加锁保证线程安全
        self._rate_lock = threading.RLock()
        self._rate_counters: Dict[str, "collections.deque[float]"] = {}
        # 待确认操作：token -> op。用 RLock 因 _build_pending 内部会调
        # _remove_pending_by_session，需支持同线程重入，否则死锁
        self._pending_lock = threading.RLock()
        self._pending_ops: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 延迟依赖
    # ------------------------------------------------------------------
    @property
    def llm_client(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    @property
    def business_adapter(self) -> BusinessSystemAdapter:
        """获取业务系统适配器。

        优先级：构造时注入的适配器 > 旧版 MockBusinessAPI（懒包装）> 工厂单例。
        旧版 MockBusinessAPI 用 MockBusinessAdapter 包装，保证共享底层 mock 状态，
        测试断言可直接读原始 mock 实例。
        """
        if self._business_adapter is not None:
            return self._business_adapter
        if self._legacy_mock_api is not None:
            # 懒包装：包装一次后缓存，复用底层 mock 实例避免重复包装
            self._business_adapter = MockBusinessAdapter(self._legacy_mock_api)
            return self._business_adapter
        self._business_adapter = get_business_adapter()
        return self._business_adapter

    @property
    def business_api(self) -> BusinessSystemAdapter:
        """向后兼容别名：返回业务系统适配器。

        旧调用方可能仍通过 business_api 访问，统一返回适配器实例；
        适配器对外方法与原 MockBusinessAPI 同名，调用方无需改动。
        """
        return self.business_adapter

    @property
    def session_manager(self) -> SessionManager:
        if self._session_manager is None:
            self._session_manager = session_manager
        return self._session_manager

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def execute(self, query: str, session_id: str) -> BusinessResult:
        """执行业务查询主流程。

        顺序：空校验 → 频率限制 → 待确认处理 → 参数提取 → 路由分发。
        任一前置环节未通过即返回对应提示，不进入后续链路。
        """
        if not query or not query.strip():
            return BusinessResult(
                reply="请告诉我您需要查询的业务。",
                success=False,
                error="empty_query",
            )

        # 频率限制：超频直接返回，避免刷接口拖慢服务
        if not self._check_rate_limit(session_id):
            logger.warning("频率限制触发 session=%s", session_id)
            return BusinessResult(
                reply="您提问过于频繁，请稍后再试。",
                success=False,
                error="rate_limited",
            )

        # 待确认操作优先处理：用户可能在回复"确认/取消"
        pending_result = self._try_handle_pending(query, session_id)
        if pending_result is not None:
            return pending_result

        # 参数提取：LLM 优先，规则兜底，失败返回引导
        session = self.session_manager.get_session(session_id) or {}
        params = self._extract_params(query, session)
        if params is None:
            return BusinessResult(
                reply="我暂时没识别到具体业务，可以问我：订单查询、退换货申请、会员信息、账户查询。",
                success=False,
                error="unknown_scene",
            )

        return self._route(params, session, session_id)

    # ------------------------------------------------------------------
    # 频率限制（线程安全）
    # ------------------------------------------------------------------
    def _check_rate_limit(self, session_id: str) -> bool:
        """滑动窗口频率限制。

        用 deque 记录窗口内时间戳，超窗弹出；超限拒绝。
        加锁保证多线程并发调用时计数准确。
        """
        now = time.monotonic()
        with self._rate_lock:
            times = self._rate_counters.get(session_id)
            if times is None:
                times = collections.deque()
                self._rate_counters[session_id] = times
            # 清理窗口外时间戳，避免 deque 无限增长
            while times and now - times[0] > RATE_LIMIT_WINDOW_SECONDS:
                times.popleft()
            if len(times) >= RATE_LIMIT_MAX_CALLS:
                return False
            times.append(now)
            return True

    def reset_rate_limit(self) -> None:
        """重置频率限制计数器，便于测试隔离。"""
        with self._rate_lock:
            self._rate_counters.clear()

    # ------------------------------------------------------------------
    # 待确认操作处理
    # ------------------------------------------------------------------
    def _try_handle_pending(
        self, query: str, session_id: str
    ) -> Optional[BusinessResult]:
        """处理待确认操作的确认/取消。

        该 session 无 pending 或用户既非确认也非取消时返回 None，
        交由主流程继续做参数提取。
        """
        op = self._find_pending(session_id)
        if op is None:
            return None

        if self._is_cancel_intent(query):
            self._remove_pending(op["token"])
            return BusinessResult(
                reply="已取消本次操作。",
                scene=op.get("scene"),
                success=True,
            )
        if self._is_confirm(query):
            self._remove_pending(op["token"])
            return self._execute_pending(op)
        return None

    def _find_pending(self, session_id: str) -> Optional[Dict[str, Any]]:
        """查找该 session 的待确认操作。同一 session 仅保留一个 pending。"""
        with self._pending_lock:
            for op in self._pending_ops.values():
                if op["session_id"] == session_id:
                    return op
        return None

    def _build_pending(
        self,
        session_id: str,
        op_type: str,
        params_data: Dict[str, Any],
        scene: BusinessScene = BusinessScene.RETURN,
    ) -> str:
        """创建待确认操作并返回 token。同 session 新操作覆盖旧操作。"""
        token = uuid.uuid4().hex
        with self._pending_lock:
            self._remove_pending_by_session(session_id)
            self._pending_ops[token] = {
                "token": token,
                "session_id": session_id,
                "op_type": op_type,
                "params": params_data,
                "scene": scene,
            }
        return token

    def _remove_pending(self, token: str) -> None:
        with self._pending_lock:
            self._pending_ops.pop(token, None)

    def _remove_pending_by_session(self, session_id: str) -> None:
        with self._pending_lock:
            stale = [
                t for t, op in self._pending_ops.items()
                if op["session_id"] == session_id
            ]
            for t in stale:
                self._pending_ops.pop(t, None)

    def clear_all_pending(self) -> None:
        """清空全部待确认操作，便于测试隔离。"""
        with self._pending_lock:
            self._pending_ops.clear()

    @staticmethod
    def _is_confirm(query: str) -> bool:
        """判断是否为确认意图。取消意图优先排除，避免否定句误判。"""
        if BusinessAgent._is_cancel_intent(query):
            return False
        q = query.strip().lower()
        return any(word in q for word in CONFIRM_WORDS)

    @staticmethod
    def _is_cancel_intent(query: str) -> bool:
        q = query.strip().lower()
        return any(word in q for word in CANCEL_WORDS)

    def _execute_pending(self, op: Dict[str, Any]) -> BusinessResult:
        """执行待确认的写操作。"""
        op_type = op["op_type"]
        params_data = op["params"]
        if op_type == "create_return":
            return self._do_create_return(params_data)
        if op_type == "cancel_return":
            return self._do_cancel_return(params_data)
        return BusinessResult(
            reply="未知操作，已取消。",
            success=False,
            error="unknown_op",
        )

    # ------------------------------------------------------------------
    # 参数提取
    # ------------------------------------------------------------------
    def _extract_params(
        self, query: str, session: Dict[str, Any]
    ) -> Optional[ExtractedParams]:
        """参数提取：规则兜底 + LLM 增强。

        先用规则提取保证离线可用，LLM 非 mock 时再用 LLM 结果覆盖补充。
        无法识别 scene 时返回 None，由主流程返回引导。
        """
        rule = self._extract_by_rules(query)
        # LLM 非 mock 模式才调用，避免 _MockLLM 拼接干扰
        if not self.llm_client.is_mock:
            try:
                llm_data = self._extract_by_llm(query)
                if llm_data:
                    # LLM 非空结果覆盖规则结果
                    for key, value in llm_data.items():
                        if value:
                            rule[key] = value
            except Exception as exc:
                logger.warning("LLM 参数提取失败，使用规则结果：%s", exc)

        scene = self._resolve_scene(rule.get("scene"))
        if scene is None:
            return None
        return ExtractedParams(
            scene=scene,
            action=rule.get("action") or "query",
            order_id=rule.get("order_id"),
            return_id=rule.get("return_id"),
            user_id=rule.get("user_id"),
            phone=rule.get("phone"),
            query_type=rule.get("query_type"),
            reason=rule.get("reason"),
        )

    @staticmethod
    def _extract_by_rules(query: str) -> Dict[str, Any]:
        """正则+关键词规则提取参数，作为 LLM 不可用时的兜底。"""
        result: Dict[str, Any] = {}

        # 场景识别：退货/会员/账户优先于订单，避免"退货订单"被误判为订单查询
        if any(k in query for k in ("退货", "退换", "退款", "换货")):
            result["scene"] = BusinessScene.RETURN.value
        elif any(k in query for k in ("会员", "积分", "等级", "优惠券")):
            result["scene"] = BusinessScene.MEMBER.value
        elif any(k in query for k in ("账户", "余额", "账单", "交易", "流水")):
            result["scene"] = BusinessScene.ACCOUNT.value
        elif any(k in query for k in ("订单", "物流", "快递", "发货", "到货")):
            result["scene"] = BusinessScene.ORDER.value

        # 手机号优先提取，避免被订单号正则吞掉
        phone_match = re.search(r"1\d{10}", query)
        if phone_match:
            result["phone"] = phone_match.group()

        # 订单号：8 位以上数字，排除已识别为手机号的部分
        for match in re.finditer(r"\d{8,}", query):
            digits = match.group()
            if digits == result.get("phone"):
                continue
            result["order_id"] = digits
            break

        # 退换单号：R 开头字母数字
        return_match = re.search(r"R\d{3,}", query, re.IGNORECASE)
        if return_match:
            result["return_id"] = return_match.group().upper()

        # 查询类型细分
        result["query_type"] = BusinessAgent._infer_query_type(query, result.get("scene"))

        # 退换货操作类型：create 关键词需精确，避免"查退换货记录"被误判为创建
        if result.get("scene") == BusinessScene.RETURN.value:
            if any(k in query for k in ("取消", "撤销", "放弃")):
                result["action"] = "cancel"
            elif any(k in query for k in ("申请", "创建", "我要退货", "发起退货", "退货申请")):
                result["action"] = "create"
            else:
                result["action"] = "query"

        # 退换货原因：粗略识别，便于演示
        if any(k in query for k in ("质量", "破损", "损坏")):
            result["reason"] = "质量问题"
        elif any(k in query for k in ("不喜欢", "不想要", "七天")):
            result["reason"] = "七天无理由"

        return result

    @staticmethod
    def _infer_query_type(query: str, scene: Optional[str]) -> Optional[str]:
        """根据关键词推断查询细分类型。"""
        if scene == BusinessScene.ORDER.value:
            if any(k in query for k in ("物流", "快递", "单号")):
                return "logistics"
            if any(k in query for k in ("金额", "多少钱", "价格")):
                return "amount"
            return "status"
        if scene == BusinessScene.MEMBER.value:
            if "积分" in query:
                return "points"
            if "等级" in query:
                return "level"
            if "优惠券" in query:
                return "coupons"
            return None
        if scene == BusinessScene.ACCOUNT.value:
            if "余额" in query:
                return "balance"
            if "账单" in query:
                return "bill"
            if any(k in query for k in ("交易", "流水")):
                return "transactions"
            return None
        return None

    def _extract_by_llm(self, query: str) -> Optional[Dict[str, Any]]:
        """调用 LLM 提取参数并解析 JSON，失败返回 None。"""
        messages = [
            {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": _EXTRACT_USER_PROMPT.format(query=query)},
        ]
        # name 用于标识业务参数提取 prompt，metadata 记录 prompt 版本便于追踪
        raw = self.llm_client.chat(
            messages,
            temperature=0.0,
            name="business_extract",
            metadata={"prompt_version": "v1"},
        )
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        """从 LLM 输出中提取首个 JSON 对象，兼容 markdown 代码块。"""
        if not text:
            return None
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            data = json.loads(match.group())
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _resolve_scene(scene_str: Optional[str]) -> Optional[BusinessScene]:
        """字符串转 BusinessScene，非法值返回 None。"""
        if not scene_str:
            return None
        try:
            return BusinessScene(scene_str)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # 路由分发
    # ------------------------------------------------------------------
    def _route(
        self,
        params: ExtractedParams,
        session: Dict[str, Any],
        session_id: str,
    ) -> BusinessResult:
        """按场景路由到具体处理逻辑。"""
        user_id = self._resolve_user_id(params, session)
        if params.scene == BusinessScene.ORDER:
            return self._handle_order(params, user_id)
        if params.scene == BusinessScene.RETURN:
            return self._handle_return(params, user_id, session_id)
        if params.scene == BusinessScene.MEMBER:
            return self._handle_member(params, user_id)
        if params.scene == BusinessScene.ACCOUNT:
            return self._handle_account(params, user_id)
        return BusinessResult(
            reply="未识别到具体业务场景。",
            success=False,
            error="unknown_scene",
        )

    @staticmethod
    def _resolve_user_id(
        params: ExtractedParams, session: Dict[str, Any]
    ) -> Optional[str]:
        """优先用对话提取的 user_id，其次会话中的 user_id。

        mock 场景允许用户在对话中传入 user_id，便于测试与演示。
        """
        return params.user_id or session.get("user_id")

    def _require_login(
        self, user_id: Optional[str], scene: BusinessScene
    ) -> Optional[BusinessResult]:
        """查询类与写操作需登录；未登录返回提示。"""
        if user_id:
            return None
        return BusinessResult(
            reply="该操作需要先登录，请提供您的用户标识或登录后再试。",
            scene=scene,
            success=False,
            error="not_logged_in",
        )

    # ------------------------------------------------------------------
    # 订单处理
    # ------------------------------------------------------------------
    def _handle_order(
        self, params: ExtractedParams, user_id: Optional[str]
    ) -> BusinessResult:
        """订单查询：依赖订单号，不强制登录（订单号本身即凭证）。"""
        if not params.order_id:
            return BusinessResult(
                reply="请提供订单号，我帮您查询订单状态。",
                scene=BusinessScene.ORDER,
                success=False,
                error="missing_order_id",
            )
        order = self.business_api.query_order(params.order_id)
        if not order:
            return BusinessResult(
                reply=f"未找到订单 #{params.order_id}，请确认订单号是否正确。",
                scene=BusinessScene.ORDER,
                success=False,
                error="order_not_found",
                data={"order_id": params.order_id},
            )
        masked = self._mask_sensitive(order)
        reply = self._format_result(BusinessScene.ORDER, masked, params)
        return BusinessResult(
            reply=reply,
            scene=BusinessScene.ORDER,
            data=masked,
        )

    # ------------------------------------------------------------------
    # 退换货处理
    # ------------------------------------------------------------------
    def _handle_return(
        self,
        params: ExtractedParams,
        user_id: Optional[str],
        session_id: str,
    ) -> BusinessResult:
        """退换货：创建/取消走二次确认，查询直接返回。"""
        login_err = self._require_login(user_id, BusinessScene.RETURN)
        if login_err:
            return login_err

        if params.action == "create":
            return self._request_create_return(params, user_id, session_id)
        if params.action == "cancel":
            return self._request_cancel_return(params, user_id, session_id)
        return self._query_return(params, user_id)

    def _request_create_return(
        self,
        params: ExtractedParams,
        user_id: Optional[str],
        session_id: str,
    ) -> BusinessResult:
        """创建退换货：写操作，先生成 pending 等待确认。"""
        if not params.order_id:
            return BusinessResult(
                reply="请提供要退货的订单号。",
                scene=BusinessScene.RETURN,
                success=False,
                error="missing_order_id",
            )
        reason = params.reason or "用户申请退货"
        token = self._build_pending(
            session_id,
            "create_return",
            {
                "order_id": params.order_id,
                "reason": reason,
                "user_id": user_id,
            },
        )
        reply = (
            f"确认为订单 #{params.order_id} 发起退换货申请吗？原因：{reason}。"
            "回复「确认」提交，「取消」放弃。"
        )
        return BusinessResult(
            reply=reply,
            scene=BusinessScene.RETURN,
            need_confirmation=True,
            confirmation_token=token,
            data={"order_id": params.order_id, "reason": reason},
        )

    def _request_cancel_return(
        self,
        params: ExtractedParams,
        user_id: Optional[str],
        session_id: str,
    ) -> BusinessResult:
        """取消退换货：写操作，先生成 pending 等待确认。"""
        return_id = params.return_id
        if not return_id:
            return BusinessResult(
                reply="请提供要取消的退换货单号。",
                scene=BusinessScene.RETURN,
                success=False,
                error="missing_return_id",
            )
        token = self._build_pending(
            session_id,
            "cancel_return",
            {"return_id": return_id, "user_id": user_id},
        )
        return BusinessResult(
            reply=f"确认取消退换货单 {return_id} 吗？回复「确认」执行，「取消」放弃。",
            scene=BusinessScene.RETURN,
            need_confirmation=True,
            confirmation_token=token,
            data={"return_id": return_id},
        )

    def _query_return(
        self, params: ExtractedParams, user_id: Optional[str]
    ) -> BusinessResult:
        """查询退换货进度或列出用户全部退换货。"""
        if params.return_id:
            record = self.business_api.query_return(params.return_id)
            if not record:
                return BusinessResult(
                    reply=f"未找到退换货单 {params.return_id}。",
                    scene=BusinessScene.RETURN,
                    success=False,
                    error="return_not_found",
                )
            masked = self._mask_sensitive(record)
            reply = self._format_result(BusinessScene.RETURN, masked, params)
            return BusinessResult(reply=reply, scene=BusinessScene.RETURN, data=masked)

        # 未指定单号：列出用户全部退换货
        records = self.business_api.list_returns_by_user(user_id or "")
        masked_list = [self._mask_sensitive(r) for r in records]
        wrapped = {"returns": masked_list, "query_type": "list"}
        reply = self._format_result(BusinessScene.RETURN, wrapped, params)
        return BusinessResult(
            reply=reply,
            scene=BusinessScene.RETURN,
            data={"returns": masked_list},
        )

    def _do_create_return(self, params_data: Dict[str, Any]) -> BusinessResult:
        """执行创建退换货（确认后调用）。"""
        record = self.business_api.create_return(
            params_data["order_id"],
            params_data["reason"],
            params_data.get("user_id"),
        )
        if not record:
            return BusinessResult(
                reply="退换货申请失败，订单不存在或已有进行中的申请。",
                scene=BusinessScene.RETURN,
                success=False,
                error="create_failed",
            )
        masked = self._mask_sensitive(record)
        params = ExtractedParams(scene=BusinessScene.RETURN, action="create")
        reply = self._format_result(BusinessScene.RETURN, masked, params)
        return BusinessResult(reply=reply, scene=BusinessScene.RETURN, data=masked)

    def _do_cancel_return(self, params_data: Dict[str, Any]) -> BusinessResult:
        """执行取消退换货（确认后调用）。"""
        record = self.business_api.cancel_return(params_data["return_id"])
        if not record:
            return BusinessResult(
                reply="取消失败，退换货单不存在或已终态。",
                scene=BusinessScene.RETURN,
                success=False,
                error="cancel_failed",
            )
        masked = self._mask_sensitive(record)
        params = ExtractedParams(scene=BusinessScene.RETURN, action="cancel")
        reply = self._format_result(BusinessScene.RETURN, masked, params)
        return BusinessResult(reply=reply, scene=BusinessScene.RETURN, data=masked)

    # ------------------------------------------------------------------
    # 会员处理
    # ------------------------------------------------------------------
    def _handle_member(
        self, params: ExtractedParams, user_id: Optional[str]
    ) -> BusinessResult:
        """会员信息查询：需登录。"""
        login_err = self._require_login(user_id, BusinessScene.MEMBER)
        if login_err:
            return login_err
        member = self.business_api.query_member(user_id)
        if not member:
            return BusinessResult(
                reply=f"未找到会员 {user_id} 的信息。",
                scene=BusinessScene.MEMBER,
                success=False,
                error="member_not_found",
            )
        masked = self._mask_sensitive(member)
        reply = self._format_result(BusinessScene.MEMBER, masked, params)
        return BusinessResult(reply=reply, scene=BusinessScene.MEMBER, data=masked)

    # ------------------------------------------------------------------
    # 账户处理
    # ------------------------------------------------------------------
    def _handle_account(
        self, params: ExtractedParams, user_id: Optional[str]
    ) -> BusinessResult:
        """账户查询：需登录。"""
        login_err = self._require_login(user_id, BusinessScene.ACCOUNT)
        if login_err:
            return login_err
        account = self.business_api.query_account(user_id)
        if not account:
            return BusinessResult(
                reply=f"未找到账户 {user_id} 的信息。",
                scene=BusinessScene.ACCOUNT,
                success=False,
                error="account_not_found",
            )
        masked = self._mask_sensitive(account)
        reply = self._format_result(BusinessScene.ACCOUNT, masked, params)
        return BusinessResult(reply=reply, scene=BusinessScene.ACCOUNT, data=masked)

    # ------------------------------------------------------------------
    # 敏感信息脱敏
    # ------------------------------------------------------------------
    def _mask_sensitive(self, data: Any) -> Any:
        """递归脱敏：对 phone/id_card 等敏感字段值打码，金额保留。

        返回新结构，不修改原数据，避免污染 mock 内部状态。
        """
        if isinstance(data, dict):
            return {k: self._mask_value(k, v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._mask_sensitive(item) for item in data]
        return data

    def _mask_value(self, key: str, value: Any) -> Any:
        """按字段名判断是否脱敏。非字符串容器递归处理。"""
        if isinstance(value, (dict, list)):
            return self._mask_sensitive(value)
        if not isinstance(value, str):
            return value
        lower_key = key.lower()
        if "phone" in lower_key or "mobile" in lower_key:
            return self._mask_phone(value)
        if "id_card" in lower_key or "idcard" in lower_key or "identity" in lower_key:
            return self._mask_id_card(value)
        return value

    @staticmethod
    def _mask_phone(phone: str) -> str:
        """手机号脱敏：138****1234（前 3 后 4）。"""
        if len(phone) == 11 and phone.isdigit():
            return phone[:3] + "****" + phone[7:]
        return phone

    @staticmethod
    def _mask_id_card(id_card: str) -> str:
        """身份证脱敏：前 6 后 4，中间打码。"""
        if len(id_card) >= 10:
            masked_len = len(id_card) - 10
            return id_card[:6] + "*" * masked_len + id_card[-4:]
        return id_card

    # ------------------------------------------------------------------
    # 结果格式化
    # ------------------------------------------------------------------
    def _format_result(
        self,
        scene: BusinessScene,
        data: Dict[str, Any],
        params: ExtractedParams,
    ) -> str:
        """结构化转自然语言：LLM 优先，mock 模式或失败走模板。"""
        if not self.llm_client.is_mock:
            try:
                return self._format_by_llm(scene, data, params)
            except Exception as exc:
                logger.warning("LLM 格式化失败，降级模板：%s", exc)
        return self._format_by_template(scene, data, params)

    def _format_by_llm(
        self,
        scene: BusinessScene,
        data: Dict[str, Any],
        params: ExtractedParams,
    ) -> str:
        """调用 LLM 生成自然语言回复。"""
        user_content = (
            f"业务场景：{scene.value}\n"
            f"查询类型：{params.query_type or '-'}\n"
            f"操作类型：{params.action}\n"
            f"数据：{json.dumps(data, ensure_ascii=False)}"
        )
        messages = [
            {"role": "system", "content": _FORMAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        # name 用于标识业务回复格式化 prompt，metadata 记录 prompt 版本便于追踪
        reply = self.llm_client.chat(
            messages,
            temperature=0.3,
            name="business_format",
            metadata={"prompt_version": "v1"},
        )
        reply = reply.strip()
        # LLM 返回空时降级模板，保证总有可读回复
        return reply or self._format_by_template(scene, data, params)

    def _format_by_template(
        self,
        scene: BusinessScene,
        data: Dict[str, Any],
        params: ExtractedParams,
    ) -> str:
        """模板拼装兜底格式化。"""
        if scene == BusinessScene.ORDER:
            return self._format_order(data, params)
        if scene == BusinessScene.RETURN:
            return self._format_return(data, params)
        if scene == BusinessScene.MEMBER:
            return self._format_member(data, params)
        if scene == BusinessScene.ACCOUNT:
            return self._format_account(data, params)
        return "暂无相关信息。"

    @staticmethod
    def _format_order(data: Dict[str, Any], params: ExtractedParams) -> str:
        """订单模板：状态+物流+预计送达+金额，按 query_type 侧重展示。"""
        status_map = {
            "paid": "已支付，待发货",
            "shipped": "已发货",
            "delivered": "已签收",
            "closed": "已关闭",
        }
        status_desc = status_map.get(data.get("status"), data.get("status", "未知"))
        parts: List[str] = [
            f"您的订单 #{data.get('order_id')} 当前状态：{status_desc}。"
        ]
        # 物流单号与预计送达仅发货后才有意义
        if data.get("tracking_no"):
            parts.append(f"物流单号：{data['tracking_no']}。")
        if data.get("eta"):
            parts.append(f"预计{data['eta']}。")
        parts.append(f"订单金额：¥{data.get('amount', 0):.2f}。")
        return "".join(parts)

    @staticmethod
    def _format_return(data: Dict[str, Any], params: ExtractedParams) -> str:
        """退换货模板：按 action 区分创建/取消/查询/列表。"""
        status_map = {
            "pending": "待审核",
            "approved": "已通过",
            "rejected": "已拒绝",
            "cancelled": "已取消",
        }
        if params.action == "create":
            status = status_map.get(data.get("status"), data.get("status"))
            return (
                f"退换货申请已创建，单号 {data.get('return_id')}，"
                f"状态：{status}。"
            )
        if params.action == "cancel":
            return f"退换货单 {data.get('return_id')} 已取消。"
        # 列表场景
        if "returns" in data:
            records = data.get("returns") or []
            if not records:
                return "您目前没有退换货记录。"
            detail = "、".join(
                f"{r['return_id']}(订单{r['order_id']},{r['status']})"
                for r in records
            )
            return f"您的退换货记录：{detail}。"
        status = status_map.get(data.get("status"), data.get("status"))
        return f"退换货单 {data.get('return_id')} 状态：{status}。"

    @staticmethod
    def _format_member(data: Dict[str, Any], params: ExtractedParams) -> str:
        """会员模板：按 query_type 侧重积分/等级/优惠券。"""
        query_type = params.query_type
        if query_type == "points":
            return f"您的积分：{data.get('points', 0)} 分。"
        if query_type == "level":
            return f"您的会员等级：{data.get('level', '-')}。"
        if query_type == "coupons":
            coupons = data.get("coupons") or []
            if not coupons:
                return "您目前没有可用优惠券。"
            detail = "、".join(f"{c['code']}(¥{c['amount']})" for c in coupons)
            return f"您的优惠券：{detail}。"
        return (
            f"您的会员信息：等级 {data.get('level', '-')}，"
            f"积分 {data.get('points', 0)} 分。"
        )

    @staticmethod
    def _format_account(data: Dict[str, Any], params: ExtractedParams) -> str:
        """账户模板：按 query_type 侧重余额/账单/交易记录。"""
        query_type = params.query_type
        if query_type == "bill":
            bills = data.get("bills") or []
            if not bills:
                return "您目前没有账单记录。"
            detail = "、".join(
                f"{b['bill_id']}(¥{b['amount']:.2f},{b['status']})" for b in bills
            )
            return f"您的账单：{detail}。"
        if query_type == "transactions":
            txs = data.get("transactions") or []
            if not txs:
                return "您目前没有交易记录。"
            detail = "、".join(
                f"{t['type']}¥{t['amount']:.2f}({t['time']})" for t in txs
            )
            return f"您的交易记录：{detail}。"
        # 默认展示余额
        return f"您的账户余额：¥{data.get('balance', 0):.2f}。"


# 模块级单例：Agent 编排无状态（除 pending/rate 计数），进程内复用
_business_agent: Optional[BusinessAgent] = None


def get_business_agent() -> BusinessAgent:
    """获取 BusinessAgent 单例。"""
    global _business_agent
    if _business_agent is None:
        _business_agent = BusinessAgent()
    return _business_agent


def reset_business_agent() -> None:
    """重置单例，便于测试切换配置。"""
    global _business_agent
    _business_agent = None
