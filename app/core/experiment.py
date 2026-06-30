"""灰度发布与 A/B 测试模块。

提供 ExperimentManager：实验配置管理、确定性分流、指标记录与聚合统计。

设计要点：
- 确定性分流：基于 user_id + 实验名的 SHA256 哈希取模，保证同用户始终进同组
- 灰度渐进：通过调整 control / treatment 权重实现 5%→10%→50%→100% 放量
- 线程安全：所有共享状态用 RLock 保护
- 持久化：实验配置与指标持久化到 {CHROMA_PERSIST_DIR}/experiments.json
- 降级：加载失败或未配置实验时所有用户进 control 组
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.operations import (
    CreateExperimentRequest,
    Experiment,
    ExperimentResults,
    Variant,
    VariantMetricStats,
)

logger = get_logger("app.core.experiment")

# 实验配置持久化文件名（位于 CHROMA_PERSIST_DIR 下）
EXPERIMENTS_FILENAME = "experiments.json"

# 默认 control 分组名：降级或未启用时所有用户进入此分组
DEFAULT_CONTROL_VARIANT = "control"

# 哈希取模基数：足够大以保证均匀分布
HASH_MODULUS = 10000


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串，统一时间格式。"""
    return datetime.now(timezone.utc).isoformat()


def _hash_user(user_id: str, experiment_name: str) -> int:
    """基于 user_id + 实验名生成稳定的哈希值。

    加入实验名避免同一用户在不同实验中始终进同一分组，
    保证实验之间的独立性。
    """
    payload = f"{experiment_name}::{user_id}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # 取前 8 位 hex 转 int 即可保证均匀性，避免处理超长整数
    return int(digest[:8], 16) % HASH_MODULUS


class ExperimentManager:
    """实验管理器。

    持有进程内实验字典、指标记录字典与一把 RLock，
    所有暴露方法均加锁，保证多线程并发分流与指标写入时状态一致。

    实验配置与指标均持久化到 experiments.json：
        {
          "experiments": {name: Experiment 序列化 dict},
          "metrics": {name: {variant: {metric: [values]}}}
        }
    """

    def __init__(self, persist_dir: str = "") -> None:
        settings = get_settings()
        self._persist_dir = persist_dir or settings.CHROMA_PERSIST_DIR
        self._store_path = Path(self._persist_dir) / EXPERIMENTS_FILENAME
        self._lock = threading.RLock()
        # 实验配置：name -> Experiment
        self._experiments: Dict[str, Experiment] = {}
        # 指标记录：experiment_name -> variant -> metric -> [values]
        self._metrics: Dict[str, Dict[str, Dict[str, List[float]]]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """从 JSON 文件加载实验与指标，失败时降级为空字典。"""
        try:
            if not self._store_path.exists():
                return
            raw = self._store_path.read_text(encoding="utf-8")
            if not raw.strip():
                return
            data = json.loads(raw)
            exp_data = data.get("experiments", {}) or {}
            self._experiments = {
                name: Experiment(**item) for name, item in exp_data.items()
            }
            # 指标直接保留为 dict 结构，避免反复序列化
            self._metrics = data.get("metrics", {}) or {}
            logger.info(
                "实验配置加载完成：共 %d 个实验，路径=%s",
                len(self._experiments),
                self._store_path,
            )
        except Exception as exc:
            # 降级：加载失败不阻断主流程，使用空字典继续
            logger.warning("实验配置加载失败，降级为空字典：%s", exc)
            self._experiments = {}
            self._metrics = {}

    def _save(self) -> None:
        """持久化到 JSON 文件，先写临时文件再替换避免写中途崩溃损坏。"""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "experiments": {
                    name: exp.model_dump() for name, exp in self._experiments.items()
                },
                "metrics": self._metrics,
            }
            tmp_path = self._store_path.with_suffix(
                self._store_path.suffix + ".tmp"
            )
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._store_path)
        except Exception as exc:
            # 持久化失败仅告警，内存状态仍可用
            logger.warning("实验配置持久化失败：%s", exc)

    # ------------------------------------------------------------------
    # 实验管理
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        name: str,
        description: str,
        variants: List[Variant],
        target_metrics: Optional[List[str]] = None,
        enabled: bool = True,
    ) -> Experiment:
        """创建实验，若已存在则覆盖。

        校验：variants 至少包含 control 分组，否则补一个默认 control。
        权重为 0 的分组不会被分配流量但仍保留配置。
        """
        normalized = self._normalize_variants(variants)
        now = _now_iso()
        experiment = Experiment(
            name=name,
            description=description,
            variants=normalized,
            target_metrics=list(target_metrics or []),
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._experiments[name] = experiment
            # 重置该实验的历史指标，避免旧数据污染
            self._metrics.pop(name, None)
            self._save()
        logger.info(
            "实验创建成功：name=%s variants=%d enabled=%s",
            name,
            len(normalized),
            enabled,
        )
        return experiment.model_copy()

    def create_experiment_from_request(
        self, request: CreateExperimentRequest
    ) -> Experiment:
        """从 API 请求体创建实验，便于路由层调用。"""
        return self.create_experiment(
            name=request.name,
            description=request.description,
            variants=request.variants,
            target_metrics=request.target_metrics,
            enabled=request.enabled,
        )

    def get_experiment(self, name: str) -> Optional[Experiment]:
        """查询实验配置，不存在返回 None。"""
        with self._lock:
            exp = self._experiments.get(name)
            return exp.model_copy() if exp is not None else None

    def list_experiments(self) -> List[Experiment]:
        """列出全部实验，按创建顺序返回。"""
        with self._lock:
            return [exp.model_copy() for exp in self._experiments.values()]

    def delete_experiment(self, name: str) -> bool:
        """删除实验及其指标记录，返回是否删除成功。"""
        with self._lock:
            if name not in self._experiments:
                return False
            self._experiments.pop(name, None)
            self._metrics.pop(name, None)
            self._save()
        return True

    # ------------------------------------------------------------------
    # 分流
    # ------------------------------------------------------------------

    def assign_user(self, experiment_name: str, user_id: str) -> str:
        """根据 user_id 哈希分配到某分组，返回分组名。

        确定性：同一 user_id 在同一实验下始终返回相同分组。
        降级场景：
        - 实验不存在或未启用 → 返回 control
        - 分组权重之和为 0 → 返回 control
        """
        with self._lock:
            exp = self._experiments.get(experiment_name)
            if exp is None or not exp.enabled:
                return DEFAULT_CONTROL_VARIANT
            return self._assign_locked(exp, user_id)

    def _assign_locked(self, experiment: Experiment, user_id: str) -> str:
        """执行分流（调用方需持锁），返回分组名。"""
        variants = experiment.variants
        if not variants:
            return DEFAULT_CONTROL_VARIANT
        total_weight = sum(v.weight for v in variants)
        if total_weight <= 0:
            return DEFAULT_CONTROL_VARIANT
        # 哈希值映射到 [0, total_weight) 区间
        bucket = _hash_user(user_id, experiment.name) % total_weight
        cumulative = 0
        for variant in variants:
            cumulative += variant.weight
            if bucket < cumulative:
                return variant.name
        # 兜底：返回最后一个分组（理论不会到达）
        return variants[-1].name

    # ------------------------------------------------------------------
    # 指标记录与统计
    # ------------------------------------------------------------------

    def record_metric(
        self,
        experiment_name: str,
        variant: str,
        metric_name: str,
        value: float,
    ) -> None:
        """记录一条实验指标。

        即使实验不存在也允许记录，便于回放与离线分析。
        """
        with self._lock:
            exp_metrics = self._metrics.setdefault(experiment_name, {})
            variant_metrics = exp_metrics.setdefault(variant, {})
            values = variant_metrics.setdefault(metric_name, [])
            values.append(float(value))
            self._save()

    def get_results(self, experiment_name: str) -> Optional[ExperimentResults]:
        """返回实验各分组指标统计（均值/标准差/样本数）。

        实验不存在时返回 None；存在但无指标记录时返回空 metrics。
        """
        with self._lock:
            exp = self._experiments.get(experiment_name)
            if exp is None:
                return None
            exp_metrics = self._metrics.get(experiment_name, {})
            variant_names = [v.name for v in exp.variants]
            metrics_result: Dict[str, Dict[str, VariantMetricStats]] = {}
            for variant_name, metric_map in exp_metrics.items():
                stats_map: Dict[str, VariantMetricStats] = {}
                for metric_name, values in metric_map.items():
                    stats_map[metric_name] = self._compute_stats(values)
                metrics_result[variant_name] = stats_map
            return ExperimentResults(
                name=experiment_name,
                enabled=exp.enabled,
                variants=variant_names,
                metrics=metrics_result,
            )

    @staticmethod
    def _compute_stats(values: List[float]) -> VariantMetricStats:
        """计算均值/标准差/样本数，空列表返回零值。"""
        if not values:
            return VariantMetricStats()
        count = len(values)
        mean = sum(values) / count
        # 总体标准差，避免样本量 1 时除零
        if count == 1:
            std = 0.0
        else:
            variance = sum((v - mean) ** 2 for v in values) / count
            std = variance ** 0.5
        return VariantMetricStats(
            mean=round(mean, 6),
            std=round(std, 6),
            sample_count=count,
        )

    # ------------------------------------------------------------------
    # 维护与测试辅助
    # ------------------------------------------------------------------

    def reset_all(self) -> None:
        """清空所有实验与指标，主要用于测试隔离。"""
        with self._lock:
            self._experiments.clear()
            self._metrics.clear()
            # 测试场景下文件可能不存在，删除失败静默处理
            try:
                if self._store_path.exists():
                    self._store_path.unlink()
            except Exception as exc:
                logger.debug("清理实验配置文件失败：%s", exc)

    @staticmethod
    def _normalize_variants(variants: List[Variant]) -> List[Variant]:
        """规范化分组列表：保证至少含 control 分组。

        若传入列表为空或无 control，则前置一个默认 control 分组，
        避免降级场景下 assign_user 找不到 control。
        """
        if not variants:
            return [Variant(name=DEFAULT_CONTROL_VARIANT, weight=1)]
        has_control = any(v.name == DEFAULT_CONTROL_VARIANT for v in variants)
        if has_control:
            return list(variants)
        return [Variant(name=DEFAULT_CONTROL_VARIANT, weight=1)] + list(variants)


# ----------------------------------------------------------------------
# 单例管理
# ----------------------------------------------------------------------

_experiment_manager: Optional[ExperimentManager] = None
_experiment_singleton_lock = threading.Lock()


def get_experiment_manager() -> ExperimentManager:
    """获取 ExperimentManager 单例。

    首次调用时按当前配置创建，后续复用同一实例。
    """
    global _experiment_manager
    if _experiment_manager is None:
        with _experiment_singleton_lock:
            if _experiment_manager is None:
                _experiment_manager = ExperimentManager()
    return _experiment_manager


def reset_experiment_manager() -> None:
    """重置单例，便于测试切换持久化目录或注入 mock。"""
    global _experiment_manager
    with _experiment_singleton_lock:
        _experiment_manager = None
