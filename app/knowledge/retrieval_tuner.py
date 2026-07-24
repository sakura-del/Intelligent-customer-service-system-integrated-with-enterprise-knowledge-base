"""检索参数动态调优模块。

提供 RetrievalTuner 类，支持运行时调整：
- vector_top_k / bm25_top_k：双路召回数量
- rerank_top_k：重排序保留数量
- similarity_threshold：相似度阈值
- rrf_k / rrf_vector_weight / rrf_keyword_weight：RRF 融合参数

调参后立即重置 hybrid_retriever / reranker / bm25 单例，
保证下一次检索使用新参数。参数持久化到 JSON 文件，重启后恢复。

线程安全：参数缓存用 RLock 保护，避免并发更新导致状态不一致。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.knowledge.retrieval_tuner")

# 持久化文件名：位于 CHROMA_PERSIST_DIR 下
_TUNER_FILENAME = "tuner_params.json"


class TunerParams(BaseModel):
    """调优参数模型，含字段范围校验。

    范围参考 Spec：vector_top_k 20-30、bm25_top_k 20-30、
    rerank_top_k 3-5、similarity_threshold 0.6-0.7、
    rrf_k 40-80、rrf 权重之和应接近 1.0。
    """

    vector_top_k: int = Field(25, description="向量召回数量")
    bm25_top_k: int = Field(25, description="BM25 召回数量")
    rerank_top_k: int = Field(5, description="重排序保留数量")
    similarity_threshold: float = Field(0.6, description="相似度阈值")
    rrf_k: int = Field(60, description="RRF 平滑常数")
    rrf_vector_weight: float = Field(0.6, description="RRF 向量路权重")
    rrf_keyword_weight: float = Field(0.4, description="RRF 关键词路权重")

    @field_validator("vector_top_k")
    @classmethod
    def _check_vector_top_k(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("vector_top_k 必须在 [1, 100] 范围内")
        return value

    @field_validator("bm25_top_k")
    @classmethod
    def _check_bm25_top_k(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("bm25_top_k 必须在 [1, 100] 范围内")
        return value

    @field_validator("rerank_top_k")
    @classmethod
    def _check_rerank_top_k(cls, value: int) -> int:
        if not 1 <= value <= 50:
            raise ValueError("rerank_top_k 必须在 [1, 50] 范围内")
        return value

    @field_validator("similarity_threshold")
    @classmethod
    def _check_similarity_threshold(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("similarity_threshold 必须在 [0.0, 1.0] 范围内")
        return value

    @field_validator("rrf_k")
    @classmethod
    def _check_rrf_k(cls, value: int) -> int:
        if not 1 <= value <= 200:
            raise ValueError("rrf_k 必须在 [1, 200] 范围内")
        return value

    @field_validator("rrf_vector_weight")
    @classmethod
    def _check_rrf_vector_weight(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("rrf_vector_weight 必须在 [0.0, 1.0] 范围内")
        return value

    @field_validator("rrf_keyword_weight")
    @classmethod
    def _check_rrf_keyword_weight(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("rrf_keyword_weight 必须在 [0.0, 1.0] 范围内")
        return value

    def merge(self, patch: TunerParams) -> TunerParams:
        """用非空 patch 字段覆盖当前参数，返回新实例。

        用于 PUT /params 部分更新场景：仅更新传入字段，其余保持原值。
        """
        merged = self.model_copy()
        # 通过类访问 model_fields，避免实例属性弃用警告
        for field_name in type(self).model_fields:
            value = getattr(patch, field_name)
            if value is not None:
                setattr(merged, field_name, value)
        return merged


class RetrievalTuner:
    """检索参数调优器。

    - 加载时优先读持久化文件，无文件用默认值；
    - update_params 立即重置下游单例，保证调参立即生效；
    - reset_to_defaults 清空缓存并恢复默认值。

    线程安全：所有读写操作通过 RLock 串行化。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 加载阶段不触发单例重置，避免初始化循环
        self._params: TunerParams = self._load_or_defaults()

    def get_params(self) -> TunerParams:
        """获取当前调优参数（返回副本避免外部修改）。"""
        with self._lock:
            return self._params.model_copy()

    def update_params(self, new_params: TunerParams) -> TunerParams:
        """更新调优参数并立即生效。

        合并 patch 后校验范围，通过则持久化并重置相关单例，
        让下次检索使用新参数。返回更新后的参数副本。
        """
        with self._lock:
            merged = self._params.merge(new_params)
            # 触发 Pydantic 校验，超范围会抛 ValueError
            validated = TunerParams(**merged.model_dump())
            self._params = validated
            self._persist()
            self._reset_downstream_singletons()
            logger.info(
                "检索参数已更新：vector_top_k=%d bm25_top_k=%d rerank_top_k=%d "
                "threshold=%.2f rrf_k=%d weights=%.2f/%.2f",
                validated.vector_top_k,
                validated.bm25_top_k,
                validated.rerank_top_k,
                validated.similarity_threshold,
                validated.rrf_k,
                validated.rrf_vector_weight,
                validated.rrf_keyword_weight,
            )
            return validated.model_copy()

    def reset_to_defaults(self) -> TunerParams:
        """重置为默认参数并立即生效。

        清空持久化文件后用默认值重新加载，再重置下游单例。
        """
        with self._lock:
            self._params = TunerParams()
            self._persist()
            self._reset_downstream_singletons()
            logger.info("检索参数已重置为默认值")
            return self._params.model_copy()

    def _reset_downstream_singletons(self) -> None:
        """重置下游单例，让调参立即生效。

        延迟导入避免循环依赖；hybrid_retriever / bm25 / reranker
        在下次调用时按新参数重建。
        """
        # 延迟导入：避免模块加载阶段触发单例初始化
        from app.knowledge.bm25 import reset_bm25_retriever
        from app.knowledge.hybrid_retriever import reset_hybrid_retriever
        from app.knowledge.reranker import reset_reranker

        reset_hybrid_retriever()
        reset_bm25_retriever()
        reset_reranker()

    def _persist(self) -> None:
        """持久化参数到 JSON 文件。

        持久化失败仅记录警告，不影响调参主流程。
        """
        try:
            path = self._persist_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._params.model_dump()
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("调优参数持久化失败：%s", exc)

    def _load_or_defaults(self) -> TunerParams:
        """加载持久化参数，失败时降级到默认值。"""
        try:
            path = self._persist_path()
            if not path.exists():
                return TunerParams()
            payload = json.loads(path.read_text(encoding="utf-8"))
            return TunerParams(**payload)
        except Exception as exc:
            logger.warning("加载持久化调优参数失败，使用默认值：%s", exc)
            return TunerParams()

    def _persist_path(self) -> Path:
        """返回持久化文件路径（CHROMA_PERSIST_DIR/tuner_params.json）。"""
        settings = get_settings()
        return Path(settings.CHROMA_PERSIST_DIR) / _TUNER_FILENAME


# 模块级单例：参数缓存进程内共享
_retrieval_tuner: RetrievalTuner | None = None
_tuner_lock = threading.Lock()


def get_retrieval_tuner() -> RetrievalTuner:
    """获取 RetrievalTuner 单例。

    双重检查锁避免并发首调重复创建。
    """
    global _retrieval_tuner
    if _retrieval_tuner is None:
        with _tuner_lock:
            if _retrieval_tuner is None:
                _retrieval_tuner = RetrievalTuner()
    return _retrieval_tuner


def reset_retrieval_tuner() -> None:
    """重置单例，便于测试切换持久化目录。"""
    global _retrieval_tuner
    with _tuner_lock:
        _retrieval_tuner = None
