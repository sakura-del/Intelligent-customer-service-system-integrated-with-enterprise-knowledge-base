"""可观测性模块。

集中提供告警管理、健康检查与 Token 用量追踪能力，
为运维与监控面板提供统一的数据来源。

模块组成：
- AlertManager：告警记录、查询与抑制（避免告警风暴）
- HealthChecker：依赖健康检查（LLM / 向量库 / Redis / 磁盘）
- TokenUsageTracker：LLM Token 用量统计与预算告警

设计要点：
- 所有共享状态用 RLock 保护，支持多线程并发埋点
- 单例模式：进程内复用，避免每个调用点各起一套采集器
- 降级策略：告警/记录失败时仅记日志，不影响主流程
- 延迟计算：健康检查延迟到调用时执行，避免启动期阻塞
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.logging import get_logger

logger = get_logger("app.core.observability")


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串，统一时间格式便于序列化。"""
    return datetime.now(timezone.utc).isoformat()


def _now_utc() -> datetime:
    """返回当前 UTC 时间 datetime 对象，用于窗口过滤计算。"""
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# 告警相关数据结构
# ----------------------------------------------------------------------


class AlertLevel(str, Enum):
    """告警级别，从低到高。

    使用 str + Enum 便于 JSON 序列化与日志级别映射。
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class Alert(BaseModel):
    """单条告警记录。

    作为告警队列与 API 响应的统一结构，包含告警级别、来源、消息与元数据。
    """

    alert_id: str = Field(..., description="告警唯一 ID")
    level: AlertLevel = Field(..., description="告警级别")
    source: str = Field(..., description="告警来源，如 token_usage/circuit_breaker")
    message: str = Field(..., description="告警消息")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="附加元数据")
    timestamp: str = Field(..., description="告警时间（ISO8601 UTC）")


# 告警级别到标准 logging 级别的映射，便于按级别输出日志
_ALERT_LEVEL_TO_LOGGING = {
    AlertLevel.INFO: logging.INFO,
    AlertLevel.WARN: logging.WARNING,
    AlertLevel.ERROR: logging.ERROR,
    AlertLevel.CRITICAL: logging.CRITICAL,
}


class AlertManager:
    """告警管理器。

    - record_alert：记录告警到内存队列，同时输出日志
    - list_alerts：按 level/source/since 过滤查询
    - 告警抑制：相同 (level, source, message) 5 分钟内仅记录一次

    降级策略：记录失败时仅日志告警，不抛异常，不影响主流程。
    """

    # 抑制窗口：相同告警 5 分钟内仅记录一次
    SUPPRESSION_WINDOW_SECONDS = 300
    # 默认告警保留上限，超过则按 FIFO 丢弃
    DEFAULT_MAX_ALERTS = 1000

    def __init__(self, max_alerts: int = DEFAULT_MAX_ALERTS) -> None:
        self._alerts: Deque[Alert] = deque(maxlen=max_alerts)
        # 抑制缓存：(level, source, message) -> 最近一次记录的单调时间戳
        # 使用 monotonic 避免系统时钟调整导致抑制失效
        self._suppression: Dict[Tuple[AlertLevel, str, str], float] = {}
        self._lock = threading.RLock()

    def record_alert(
        self,
        level: AlertLevel,
        source: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Alert]:
        """记录一条告警。

        命中抑制窗口时仅输出日志不入队，返回 None；
        正常记录时返回创建的 Alert 对象。
        任意异常被吞掉，仅日志告警，保证不影响主流程。
        """
        try:
            now_mono = time.monotonic()
            now_iso = _now_iso()
            meta = dict(metadata) if metadata else {}

            with self._lock:
                key = (level, source, message)
                last_time = self._suppression.get(key)
                # 命中抑制窗口：仅日志，不入队
                if (
                    last_time is not None
                    and (now_mono - last_time) < self.SUPPRESSION_WINDOW_SECONDS
                ):
                    self._log_alert(level, source, message, meta, suppressed=True)
                    return None

                self._suppression[key] = now_mono
                alert = Alert(
                    alert_id=str(uuid.uuid4()),
                    level=level,
                    source=source,
                    message=message,
                    metadata=meta,
                    timestamp=now_iso,
                )
                self._alerts.append(alert)

            self._log_alert(level, source, message, meta, suppressed=False)
            return alert
        except Exception as exc:
            # 告警记录失败时仅日志，不影响主流程
            logger.error("告警记录失败：%s", exc)
            return None

    def list_alerts(
        self,
        level: Optional[AlertLevel] = None,
        source: Optional[str] = None,
        since: Optional[str] = None,
    ) -> List[Alert]:
        """查询告警列表。

        全部参数可选，未指定的维度不做过滤。
        since 为 ISO 时间字符串，按字符串字典序比较（ISO8601 保证字典序与时间序一致）。
        返回浅拷贝列表，避免外部修改内部状态。
        """
        with self._lock:
            results: List[Alert] = []
            for alert in self._alerts:
                if level is not None and alert.level != level:
                    continue
                if source is not None and alert.source != source:
                    continue
                if since is not None and alert.timestamp < since:
                    continue
                results.append(alert)
            return results

    @staticmethod
    def _log_alert(
        level: AlertLevel,
        source: str,
        message: str,
        metadata: Dict[str, Any],
        suppressed: bool,
    ) -> None:
        """按级别输出告警日志。

        suppressed=True 表示被抑制的告警，仅日志不入队。
        """
        log_level = _ALERT_LEVEL_TO_LOGGING.get(level, logging.INFO)
        suffix = "（已抑制）" if suppressed else ""
        logger.log(
            log_level,
            "[告警%s] source=%s message=%s metadata=%s",
            suffix,
            source,
            message,
            metadata,
        )

    def reset(self) -> None:
        """清空所有告警与抑制缓存，主要用于测试隔离。"""
        with self._lock:
            self._alerts.clear()
            self._suppression.clear()


# 模块级单例：告警管理器
_alert_manager: Optional[AlertManager] = None
_alert_manager_lock = threading.Lock()


def get_alert_manager() -> AlertManager:
    """获取 AlertManager 单例。"""
    global _alert_manager
    if _alert_manager is None:
        with _alert_manager_lock:
            if _alert_manager is None:
                _alert_manager = AlertManager()
    return _alert_manager


def reset_alert_manager() -> None:
    """重置单例，便于测试切换配置或注入 mock。"""
    global _alert_manager
    with _alert_manager_lock:
        _alert_manager = None


# ----------------------------------------------------------------------
# 健康检查相关数据结构
# ----------------------------------------------------------------------


class HealthStatus(str, Enum):
    """健康状态枚举。"""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class HealthItem(BaseModel):
    """单项健康检查结果。"""

    name: str = Field(..., description="检查项名称，如 llm/redis/vector_store/disk_space")
    status: HealthStatus = Field(..., description="健康状态")
    message: str = Field("", description="检查信息，人类可读")
    duration_ms: float = Field(0.0, description="检查耗时（毫秒）")
    checked_at: str = Field(..., description="检查时间（ISO）")


class HealthReport(BaseModel):
    """健康检查聚合报告。"""

    overall: HealthStatus = Field(..., description="整体健康状态")
    items: List[HealthItem] = Field(default_factory=list, description="各依赖检查结果")
    checked_at: str = Field(..., description="报告生成时间（ISO）")


class HealthChecker:
    """健康检查器。

    检查各外部依赖的可用性：LLM / 向量库 / Redis / 磁盘空间。
    每项检查独立执行，单项失败不影响其他检查项。
    所有检查在调用时才执行（延迟计算），避免启动期阻塞。

    降级策略：单项检查抛异常时该项标记为 UNHEALTHY，其他继续。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def check_all(self) -> HealthReport:
        """执行全部健康检查并返回聚合报告。

        每项检查独立计时，异常被捕获并标记为 UNHEALTHY，
        保证一项失败不影响其他检查结果。
        """
        # 检查项列表：name -> 检查函数
        # 在此声明而非构造时绑定，避免导入阶段触发依赖初始化
        checks: List[Tuple[str, Callable[[], Tuple[HealthStatus, str]]]] = [
            ("llm", self._check_llm),
            ("vector_store", self._check_vector_store),
            ("redis", self._check_redis),
            ("disk_space", self._check_disk_space),
        ]

        items: List[HealthItem] = []
        for name, check_func in checks:
            item = self._safe_check(name, check_func)
            items.append(item)

        overall = self._compute_overall(items)
        return HealthReport(overall=overall, items=items, checked_at=_now_iso())

    @staticmethod
    def _safe_check(
        name: str, check_func: Callable[[], Tuple[HealthStatus, str]]
    ) -> HealthItem:
        """执行单项检查，捕获所有异常。

        检查函数应返回 (status, message) 元组；
        抛异常时返回 UNHEALTHY 与异常信息。
        """
        start = time.monotonic()
        try:
            status, message = check_func()
        except Exception as exc:
            status = HealthStatus.UNHEALTHY
            message = f"检查异常：{exc}"
        duration_ms = (time.monotonic() - start) * 1000.0
        return HealthItem(
            name=name,
            status=status,
            message=message,
            duration_ms=round(duration_ms, 2),
            checked_at=_now_iso(),
        )

    @staticmethod
    def _compute_overall(items: List[HealthItem]) -> HealthStatus:
        """根据各检查项汇总整体健康状态。

        - 全部 HEALTHY：整体 HEALTHY
        - 任一 UNHEALTHY：整体 UNHEALTHY
        - 否则（含 UNKNOWN）：整体 UNKNOWN
        """
        if not items:
            return HealthStatus.UNKNOWN
        if all(item.status == HealthStatus.HEALTHY for item in items):
            return HealthStatus.HEALTHY
        if any(item.status == HealthStatus.UNHEALTHY for item in items):
            return HealthStatus.UNHEALTHY
        return HealthStatus.UNKNOWN

    # ------------------------------------------------------------------
    # 各依赖检查实现
    # ------------------------------------------------------------------
    @staticmethod
    def _check_llm() -> Tuple[HealthStatus, str]:
        """检查 LLM 客户端可用性。

        mock 模式视为 HEALTHY（开发态可用），
        真实模式下确认客户端已初始化。
        """
        try:
            from app.agents.llm_client import get_llm_client

            client = get_llm_client()
            if client.is_mock:
                return HealthStatus.HEALTHY, "LLM 处于 mock 模式（无 API Key 或降级）"
            return HealthStatus.HEALTHY, f"LLM 客户端就绪，model={client.model}"
        except Exception as exc:
            return HealthStatus.UNHEALTHY, f"LLM 客户端不可用：{exc}"

    @staticmethod
    def _check_vector_store() -> Tuple[HealthStatus, str]:
        """检查向量库连接与集合可访问性。"""
        try:
            from app.knowledge.vectorstore import get_vector_store

            store = get_vector_store()
            collection = store.collection
            count = collection.count()
            return (
                HealthStatus.HEALTHY,
                f"向量库连接正常，collection={collection.name}, count={count}",
            )
        except Exception as exc:
            return HealthStatus.UNHEALTHY, f"向量库不可用：{exc}"

    @staticmethod
    def _check_redis() -> Tuple[HealthStatus, str]:
        """检查 Redis 连接。

        未安装 redis 库时返回 UNKNOWN（开发态常见，非故障）；
        连接失败时返回 UNHEALTHY。
        """
        try:
            import redis as redis_lib  # noqa: F401
        except ImportError:
            return HealthStatus.UNKNOWN, "未安装 redis 库，跳过 Redis 检查"

        try:
            from app.core.config import get_settings

            client = redis_lib.Redis.from_url(
                get_settings().REDIS_URL, decode_responses=True
            )
            try:
                client.ping()
            finally:
                client.close()
            return HealthStatus.HEALTHY, "Redis 连接正常"
        except Exception as exc:
            return HealthStatus.UNHEALTHY, f"Redis 不可用：{exc}"

    @staticmethod
    def _check_disk_space() -> Tuple[HealthStatus, str]:
        """检查持久化目录所在磁盘剩余空间。

        低于 1GB 视为 UNHEALTHY，避免持久化失败导致数据丢失。
        """
        try:
            import shutil

            from app.core.config import get_settings

            target_dir = get_settings().CHROMA_PERSIST_DIR
            # 目录不存在时检查父目录所在磁盘
            check_dir = target_dir if os.path.exists(target_dir) else "."
            usage = shutil.disk_usage(check_dir)
            free_gb = usage.free / (1024 ** 3)
            if free_gb < 1.0:
                return (
                    HealthStatus.UNHEALTHY,
                    f"磁盘空间不足：剩余 {free_gb:.2f} GB（低于 1GB 阈值）",
                )
            return HealthStatus.HEALTHY, f"磁盘空间充足：剩余 {free_gb:.2f} GB"
        except Exception as exc:
            return HealthStatus.UNHEALTHY, f"磁盘空间检查失败：{exc}"


# 模块级单例：健康检查器
_health_checker: Optional[HealthChecker] = None
_health_checker_lock = threading.Lock()


def get_health_checker() -> HealthChecker:
    """获取 HealthChecker 单例。"""
    global _health_checker
    if _health_checker is None:
        with _health_checker_lock:
            if _health_checker is None:
                _health_checker = HealthChecker()
    return _health_checker


def reset_health_checker() -> None:
    """重置单例，便于测试切换配置或注入 mock。"""
    global _health_checker
    with _health_checker_lock:
        _health_checker = None


# ----------------------------------------------------------------------
# Token 用量相关数据结构
# ----------------------------------------------------------------------

# 默认每小时 Token 预算，settings 未配置时使用
DEFAULT_TOKEN_BUDGET_PER_HOUR = 100_000
# 持久化间隔：每 60 秒落盘一次，避免每次调用都写文件
FLUSH_INTERVAL_SECONDS = 60
# 内存中保留的最大记录数，超过按 FIFO 丢弃
MAX_TOKEN_RECORDS = 10_000
# 持久化文件名（位于 CHROMA_PERSIST_DIR 下）
TOKEN_USAGE_FILENAME = "token_usage.json"


class TokenUsageStats(BaseModel):
    """Token 用量统计结果。

    按窗口（minute/hour/day）聚合，包含总量与按 model/user/endpoint 维度的细分。
    """

    window: str = Field(..., description="统计窗口：minute/hour/day")
    total_prompt_tokens: int = Field(0, description="窗口内 prompt token 总数")
    total_completion_tokens: int = Field(0, description="窗口内 completion token 总数")
    total_tokens: int = Field(0, description="窗口内 total token 总数")
    call_count: int = Field(0, description="窗口内 LLM 调用次数")
    by_model: Dict[str, Dict[str, int]] = Field(
        default_factory=dict, description="按 model 维度统计"
    )
    by_user: Dict[str, Dict[str, int]] = Field(
        default_factory=dict, description="按 user 维度统计"
    )
    by_endpoint: Dict[str, Dict[str, int]] = Field(
        default_factory=dict, description="按 endpoint 维度统计"
    )
    window_start: str = Field("", description="窗口起始时间（ISO）")
    window_end: str = Field("", description="窗口结束时间（ISO）")


@dataclass
class TokenUsageRecord:
    """单次 LLM 调用的 Token 用量记录。

    内部使用 dataclass 而非 Pydantic，避免大量记录时的序列化开销。
    """

    timestamp: datetime
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    user_id: Optional[str] = None
    endpoint: Optional[str] = None


class TokenUsageTracker:
    """Token 用量追踪器。

    - record：记录一次 LLM 调用的 token 用量
    - get_stats：按窗口（minute/hour/day）聚合统计
    - 预算告警：超 TOKEN_BUDGET_PER_HOUR 时记录告警
    - 持久化：定期 flush 到 JSON 文件，启动时加载历史记录

    降级策略：记录失败时仅日志，不影响 LLM 调用；
    持久化失败时内存中保留，下次重试。
    """

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._records: Deque[TokenUsageRecord] = deque(maxlen=MAX_TOKEN_RECORDS)
        self._lock = threading.RLock()
        self._last_flush_monotonic = time.monotonic()
        self._dirty = False
        # 持久化路径：优先使用传入参数，其次从 settings 拼接
        self._persist_path = persist_path or self._resolve_persist_path()
        # 启动时加载历史记录，让重启后仍可查询近期用量
        self._load_persisted()

    @staticmethod
    def _resolve_persist_path() -> Optional[str]:
        """从 settings 拼接持久化路径，失败时返回 None。"""
        try:
            from app.core.config import get_settings

            persist_dir = get_settings().CHROMA_PERSIST_DIR
            return os.path.join(persist_dir, TOKEN_USAGE_FILENAME)
        except Exception as exc:
            logger.warning("解析 Token 用量持久化路径失败：%s", exc)
            return None

    # ------------------------------------------------------------------
    # 记录与统计
    # ------------------------------------------------------------------
    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        user_id: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> None:
        """记录一次 LLM 调用的 token 用量。

        任何异常被吞掉，仅日志告警，保证不影响 LLM 主流程。
        """
        try:
            prompt_tokens = int(prompt_tokens)
            completion_tokens = int(completion_tokens)
            total = prompt_tokens + completion_tokens
            record = TokenUsageRecord(
                timestamp=_now_utc(),
                model=str(model),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total,
                user_id=user_id,
                endpoint=endpoint,
            )
            with self._lock:
                self._records.append(record)
                self._dirty = True
                # 预算告警检查
                self._check_budget_locked()
                # 定期持久化：达到间隔才落盘
                now_mono = time.monotonic()
                if now_mono - self._last_flush_monotonic >= FLUSH_INTERVAL_SECONDS:
                    self._flush_locked()
                    self._last_flush_monotonic = now_mono
        except Exception as exc:
            logger.warning("Token 用量记录失败：%s", exc)

    def get_stats(self, window: str = "hour") -> TokenUsageStats:
        """返回指定窗口的 token 用量统计。

        window 取值：minute / hour / day，其他值抛 ValueError。
        """
        with self._lock:
            now = _now_utc()
            start = self._window_start(window, now)
            filtered = [r for r in self._records if r.timestamp >= start]
            return self._aggregate_locked(filtered, window, start, now)

    @staticmethod
    def _window_start(window: str, now: datetime) -> datetime:
        """根据窗口类型计算起始时间。"""
        if window == "minute":
            return now - timedelta(minutes=1)
        if window == "hour":
            return now - timedelta(hours=1)
        if window == "day":
            return now - timedelta(days=1)
        raise ValueError(f"未知时间窗口：{window}，支持 minute/hour/day")

    @staticmethod
    def _aggregate_locked(
        records: List[TokenUsageRecord],
        window: str,
        start: datetime,
        end: datetime,
    ) -> TokenUsageStats:
        """聚合窗口内记录为 TokenUsageStats，调用方需持锁。"""
        if not records:
            return TokenUsageStats(
                window=window,
                window_start=start.isoformat(),
                window_end=end.isoformat(),
            )
        total_prompt = sum(r.prompt_tokens for r in records)
        total_completion = sum(r.completion_tokens for r in records)
        total = sum(r.total_tokens for r in records)
        return TokenUsageStats(
            window=window,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_tokens=total,
            call_count=len(records),
            by_model=TokenUsageTracker._aggregate_by_key(
                records, lambda r: r.model
            ),
            by_user=TokenUsageTracker._aggregate_by_key(
                records, lambda r: r.user_id or "anonymous"
            ),
            by_endpoint=TokenUsageTracker._aggregate_by_key(
                records, lambda r: r.endpoint or "unknown"
            ),
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )

    @staticmethod
    def _aggregate_by_key(
        records: List[TokenUsageRecord], key_func: Callable[[TokenUsageRecord], str]
    ) -> Dict[str, Dict[str, int]]:
        """按指定 key 聚合 prompt/completion/total/call_count。"""
        result: Dict[str, Dict[str, int]] = {}
        for r in records:
            key = key_func(r)
            bucket = result.setdefault(
                key,
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "call_count": 0,
                },
            )
            bucket["prompt_tokens"] += r.prompt_tokens
            bucket["completion_tokens"] += r.completion_tokens
            bucket["total_tokens"] += r.total_tokens
            bucket["call_count"] += 1
        return result

    # ------------------------------------------------------------------
    # 预算告警
    # ------------------------------------------------------------------
    def _check_budget_locked(self) -> None:
        """检查小时预算，超限时记录告警。调用方需持锁。"""
        budget = self._get_budget_per_hour()
        if budget <= 0:
            return
        now = _now_utc()
        hour_ago = now - timedelta(hours=1)
        total = sum(r.total_tokens for r in self._records if r.timestamp >= hour_ago)
        if total <= budget:
            return
        # 超预算时记录告警（AlertManager 内部有抑制，不会告警风暴）
        try:
            get_alert_manager().record_alert(
                level=AlertLevel.WARN,
                source="token_usage",
                message=f"Token 用量超预算：{total} > {budget}/hour",
                metadata={"total": total, "budget": budget, "window": "hour"},
            )
        except Exception as exc:
            logger.warning("Token 预算告警记录失败：%s", exc)

    @staticmethod
    def _get_budget_per_hour() -> int:
        """读取小时预算。

        优先读 settings.TOKEN_BUDGET_PER_HOUR（用户可在 .env 中配置），
        未配置时使用默认值。读取失败时返回 0（不检查预算）。
        """
        try:
            from app.core.config import get_settings

            return int(
                getattr(
                    get_settings(),
                    "TOKEN_BUDGET_PER_HOUR",
                    DEFAULT_TOKEN_BUDGET_PER_HOUR,
                )
            )
        except Exception:
            return DEFAULT_TOKEN_BUDGET_PER_HOUR

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load_persisted(self) -> None:
        """从持久化文件加载历史记录，启动时调用一次。"""
        if not self._persist_path:
            return
        try:
            if not os.path.exists(self._persist_path):
                return
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            records = data.get("records", [])
            if not isinstance(records, list):
                return
            loaded = 0
            for item in records:
                record = self._deserialize_record(item)
                if record is not None:
                    self._records.append(record)
                    loaded += 1
            if loaded > 0:
                logger.info("已加载 %d 条 Token 用量历史记录", loaded)
        except Exception as exc:
            logger.warning("加载 Token 用量历史记录失败：%s", exc)

    @staticmethod
    def _deserialize_record(item: Any) -> Optional[TokenUsageRecord]:
        """反序列化单条记录，字段缺失或格式错误时返回 None。"""
        if not isinstance(item, dict):
            return None
        try:
            timestamp_str = item["timestamp"]
            # 兼容带/不带时区信息的 ISO 字符串
            timestamp = datetime.fromisoformat(timestamp_str)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return TokenUsageRecord(
                timestamp=timestamp,
                model=str(item["model"]),
                prompt_tokens=int(item["prompt_tokens"]),
                completion_tokens=int(item["completion_tokens"]),
                total_tokens=int(item["total_tokens"]),
                user_id=item.get("user_id"),
                endpoint=item.get("endpoint"),
            )
        except (KeyError, ValueError, TypeError):
            return None

    def _flush_locked(self) -> None:
        """将当前记录持久化到 JSON 文件。调用方需持锁。

        采用 .tmp + os.replace 原子写入，避免崩溃导致文件损坏。
        持久化失败时仅日志，内存中保留 _dirty=True，下次重试。
        """
        if not self._persist_path or not self._dirty:
            return
        try:
            persist_dir = os.path.dirname(self._persist_path)
            if persist_dir:
                os.makedirs(persist_dir, exist_ok=True)
            data = {
                "records": [
                    {
                        "timestamp": r.timestamp.isoformat(),
                        "model": r.model,
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                        "total_tokens": r.total_tokens,
                        "user_id": r.user_id,
                        "endpoint": r.endpoint,
                    }
                    for r in self._records
                ],
                "updated_at": _now_iso(),
            }
            # 原子写入：先写 .tmp 再 rename，避免崩溃导致文件损坏
            tmp_path = self._persist_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, self._persist_path)
            self._dirty = False
        except Exception as exc:
            logger.warning("Token 用量持久化失败：%s", exc)

    def flush(self) -> None:
        """手动触发持久化，便于测试或关停时落盘。"""
        with self._lock:
            self._flush_locked()
            self._last_flush_monotonic = time.monotonic()

    def reset(self) -> None:
        """清空内存记录（不删除持久化文件），主要用于测试隔离。"""
        with self._lock:
            self._records.clear()
            self._dirty = False
            self._last_flush_monotonic = time.monotonic()


# 模块级单例：Token 用量追踪器
_token_tracker: Optional[TokenUsageTracker] = None
_token_tracker_lock = threading.Lock()


def get_token_usage_tracker() -> TokenUsageTracker:
    """获取 TokenUsageTracker 单例。"""
    global _token_tracker
    if _token_tracker is None:
        with _token_tracker_lock:
            if _token_tracker is None:
                _token_tracker = TokenUsageTracker()
    return _token_tracker


def reset_token_usage_tracker() -> None:
    """重置单例，便于测试切换配置或注入 mock。"""
    global _token_tracker
    with _token_tracker_lock:
        _token_tracker = None
