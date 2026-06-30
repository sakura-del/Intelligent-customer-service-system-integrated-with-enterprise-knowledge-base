"""文本向量化接口。

优先使用 sentence-transformers 加载 BGE-large-zh 模型，
失败时降级为确定性 hash 向量化，保证流水线在无网络/无模型环境下也能跑通。

加载策略（按优先级）：
1. HuggingFace 主源（huggingface.co）
2. HuggingFace 镜像源（hf-mirror.com）
3. 本地缓存目录（EMBEDDING_LOCAL_CACHE_DIR）
4. hash fallback（确定性降级，仅保证流程可用）

每次尝试独立捕获异常并记录诊断信息（源 URL、失败原因、耗时），
便于通过 get_embedding_diagnostics() 排查加载失败原因。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.knowledge.embeddings")

# BGE-large-zh 输出维度，fallback 模式需保持一致以避免向量库 schema 冲突
BGE_LARGE_ZH_DIMENSION = 1024

# 诊断信息读写锁：保证多线程并发加载时诊断列表一致
_DIAG_LOCK = threading.RLock()


class _FallbackEmbedder:
    """确定性 hash 向量化：仅供跑通流程。

    通过对文本做 sha256 分桶生成固定维度向量，
    保证相同文本得到相同向量，可被去重逻辑识别。
    不能用于真实语义检索，部署时应确保 BGE 可用。
    """

    def __init__(self, dim: int = BGE_LARGE_ZH_DIMENSION) -> None:
        self.dim = dim

    def encode(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for text in texts:
            # 用 sha256 派生 32 字节，循环填充到目标维度
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = [(b - 128) / 128.0 for b in (digest * (self.dim // len(digest) + 1))[: self.dim]]
            vectors.append(vector)
        return vectors


def _record_attempt(
    diagnostics: List[Dict[str, Any]],
    source: str,
    success: bool,
    elapsed_ms: float,
    error: Optional[str] = None,
) -> None:
    """安全追加一次加载尝试的诊断记录。

    使用全局 RLock 保证多线程并发加载时诊断列表不撕裂，
    追加失败不影响主链路。
    """
    record: Dict[str, Any] = {
        "source": source,
        "success": success,
        "elapsed_ms": int(elapsed_ms),
    }
    if error:
        record["error"] = error
    with _DIAG_LOCK:
        diagnostics.append(record)


class EmbeddingService:
    """统一 Embedding 服务。

    内部维护模型单例，避免重复加载；
    通过 mode 字段向外暴露当前使用模式（bge/fallback）。
    """

    def __init__(self, model_name: str = "") -> None:
        settings = get_settings()
        self.model_name = model_name or settings.EMBEDDING_MODEL
        self.batch_size = settings.EMBEDDING_BATCH_SIZE
        self.local_cache_dir = settings.EMBEDDING_LOCAL_CACHE_DIR
        self.hf_mirror_url = settings.HF_MIRROR_URL
        self.load_timeout = settings.EMBEDDING_LOAD_TIMEOUT
        self._model = None
        self._mode = "unknown"
        self._fallback = _FallbackEmbedder()
        # 诊断信息：记录每次加载尝试，便于排查
        self._diagnostics: List[Dict[str, Any]] = []
        self._load_model()

    # ------------------------------------------------------------------
    # 模型加载：四级回退链路
    # ------------------------------------------------------------------
    def _load_model(self) -> None:
        """尝试加载 BGE 模型，按四级回退链路逐个尝试。

        主源 → 镜像源 → 本地缓存 → hash fallback，
        每次尝试独立 try/except，失败则降级到下一级。
        最终成功时验证维度为 1024，与 fallback 维度对齐避免向量库 schema 冲突。
        """
        # 1. HuggingFace 主源
        model = self._try_load_huggingface_main()
        if model is not None and self._validate_dimension(model):
            self._apply_bge_model(model)
            return

        # 2. HuggingFace 镜像源
        model = self._try_load_hf_mirror()
        if model is not None and self._validate_dimension(model):
            self._apply_bge_model(model)
            return

        # 3. 本地缓存目录
        model = self._try_load_local_cache()
        if model is not None and self._validate_dimension(model):
            self._apply_bge_model(model)
            return

        # 4. 全部失败：降级 fallback
        self._apply_fallback()

    def _try_load_huggingface_main(self) -> Optional[Any]:
        """尝试从 HuggingFace 主源加载模型。"""
        start = time.perf_counter()
        try:
            from sentence_transformers import SentenceTransformer

            # 主源必须保证不使用离线模式，确保能访问 huggingface.co
            os.environ["HF_HUB_OFFLINE"] = "0"
            os.environ.pop("HF_ENDPOINT", None)
            logger.info("尝试从 HuggingFace 主源加载 Embedding 模型：%s", self.model_name)
            model = SentenceTransformer(self.model_name)
            _ = model.encode(["warmup"], normalize_embeddings=True)
            elapsed = (time.perf_counter() - start) * 1000.0
            _record_attempt(self._diagnostics, "huggingface_main", True, elapsed)
            logger.info(
                "HuggingFace 主源加载成功，维度=%d 耗时=%dms",
                model.get_sentence_embedding_dimension(),
                int(elapsed),
            )
            return model
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000.0
            _record_attempt(
                self._diagnostics, "huggingface_main", False, elapsed, str(exc)
            )
            logger.warning(
                "HuggingFace 主源加载失败（%dms）：%s，尝试镜像源",
                int(elapsed),
                exc,
            )
            return None

    def _try_load_hf_mirror(self) -> Optional[Any]:
        """尝试从 HuggingFace 镜像源加载模型。"""
        start = time.perf_counter()
        try:
            from sentence_transformers import SentenceTransformer

            # 设置 HF_ENDPOINT 指向镜像源，覆盖默认 huggingface.co
            os.environ["HF_ENDPOINT"] = self.hf_mirror_url
            os.environ["HF_HUB_OFFLINE"] = "0"
            logger.info(
                "尝试从 HuggingFace 镜像源加载 Embedding 模型：%s endpoint=%s",
                self.model_name,
                self.hf_mirror_url,
            )
            model = SentenceTransformer(self.model_name)
            _ = model.encode(["warmup"], normalize_embeddings=True)
            elapsed = (time.perf_counter() - start) * 1000.0
            _record_attempt(self._diagnostics, "hf_mirror", True, elapsed)
            logger.info(
                "镜像源加载成功，维度=%d 耗时=%dms",
                model.get_sentence_embedding_dimension(),
                int(elapsed),
            )
            return model
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000.0
            _record_attempt(
                self._diagnostics, "hf_mirror", False, elapsed, str(exc)
            )
            logger.warning(
                "镜像源加载失败（%dms）：%s，尝试本地缓存",
                int(elapsed),
                exc,
            )
            return None

    def _try_load_local_cache(self) -> Optional[Any]:
        """尝试从本地缓存目录加载模型。

        仅当目录存在且含 config.json + 权重文件时尝试，
        避免在空目录上触发 SentenceTransformer 的下载行为。
        """
        start = time.perf_counter()
        cache_path = self.local_cache_dir
        if not _has_local_weights(cache_path):
            _record_attempt(
                self._diagnostics,
                "local_cache",
                False,
                0.0,
                f"本地缓存目录 {cache_path} 缺少 config.json 或权重文件",
            )
            logger.info("本地缓存目录 %s 无可用权重，跳过", cache_path)
            return None

        try:
            from sentence_transformers import SentenceTransformer

            # 离线模式：禁止下载，强制使用本地文件
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ.pop("HF_ENDPOINT", None)
            logger.info("从本地缓存加载 Embedding 模型：%s", cache_path)
            model = SentenceTransformer(cache_path)
            _ = model.encode(["warmup"], normalize_embeddings=True)
            elapsed = (time.perf_counter() - start) * 1000.0
            _record_attempt(self._diagnostics, "local_cache", True, elapsed)
            logger.info(
                "本地缓存加载成功，维度=%d 耗时=%dms",
                model.get_sentence_embedding_dimension(),
                int(elapsed),
            )
            return model
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000.0
            _record_attempt(
                self._diagnostics, "local_cache", False, elapsed, str(exc)
            )
            logger.warning(
                "本地缓存加载失败（%dms）：%s，降级 fallback",
                int(elapsed),
                exc,
            )
            return None

    def _validate_dimension(self, model: Any) -> bool:
        """校验模型输出维度为 1024，与 fallback 对齐避免向量库 schema 冲突。

        维度不符则降级 fallback，保证向量库中所有向量维度一致。
        """
        try:
            dim = model.get_sentence_embedding_dimension()
        except Exception as exc:
            logger.warning("获取模型维度失败：%s，降级 fallback", exc)
            return False
        if dim != BGE_LARGE_ZH_DIMENSION:
            logger.warning(
                "BGE 模型维度 %d 与期望 %d 不符，降级 fallback 避免向量库 schema 冲突",
                dim,
                BGE_LARGE_ZH_DIMENSION,
            )
            return False
        return True

    def _apply_bge_model(self, model: Any) -> None:
        """加载成功：设置 BGE 模型为当前模式。"""
        self._model = model
        self._mode = "bge"

    def _apply_fallback(self) -> None:
        """降级到 hash fallback，仅保证流程可运行。"""
        logger.warning(
            "BGE 模型 %s 全部加载源失败，降级到 hash fallback 向量化（维度=%d）",
            self.model_name,
            BGE_LARGE_ZH_DIMENSION,
        )
        self._model = None
        self._mode = "fallback"

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def dimension(self) -> int:
        """返回当前向量维度，便于向量库初始化时校验。"""
        if self._mode == "bge" and self._model is not None:
            return self._model.get_sentence_embedding_dimension()
        return BGE_LARGE_ZH_DIMENSION

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量向量化，按 batch_size 分批避免一次性占用过多内存。"""
        if not texts:
            return []

        results: List[List[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            if self._mode == "bge" and self._model is not None:
                # BGE 系列建议做 L2 归一化，便于直接用内积近似 cosine
                # .tolist() 将 np.float32 转为 Python 原生 float，避免 ChromaDB 类型拒绝
                vectors = self._model.encode(batch, normalize_embeddings=True)
                results.extend([v.tolist() for v in vectors])
            else:
                results.extend(self._fallback.encode(batch))
        return results

    def embed_query(self, text: str) -> List[float]:
        """单条 query 向量化，检索时使用。"""
        vectors = self.embed_texts([text])
        return vectors[0] if vectors else []


def _has_local_weights(cache_dir: str) -> bool:
    """判断本地缓存目录是否包含可加载的 BGE 权重。

    必须同时存在 config.json 与至少一个权重文件（.bin / .safetensors），
    缺任一文件则视为不完整缓存，跳过本地加载避免触发下载。
    """
    if not cache_dir or not os.path.isdir(cache_dir):
        return False
    if not os.path.isfile(os.path.join(cache_dir, "config.json")):
        return False
    # 权重文件名兼容 BGE 常见两种格式
    weight_files = (
        "pytorch_model.bin",
        "model.safetensors",
        "pytorch_model.safetensors",
    )
    return any(os.path.isfile(os.path.join(cache_dir, name)) for name in weight_files)


# 模块级单例：进程内只加载一次模型，减少启动开销
_embedding_service: EmbeddingService | None = None

# 单例创建锁：多线程并发首次调用时只创建一次，避免重复加载模型
_EMBEDDING_SERVICE_LOCK = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """获取 Embedding 服务单例。

    使用锁保证多线程并发首次调用时只创建一次，
    避免重复加载模型占用过多内存。
    """
    global _embedding_service
    if _embedding_service is None:
        with _EMBEDDING_SERVICE_LOCK:
            # 二次检查：拿到锁后可能已被其他线程初始化
            if _embedding_service is None:
                _embedding_service = EmbeddingService()
    return _embedding_service


def reset_embedding_service() -> None:
    """重置单例，便于测试中切换模型配置。"""
    global _embedding_service
    with _EMBEDDING_SERVICE_LOCK:
        _embedding_service = None


def get_embedding_diagnostics() -> Dict[str, Any]:
    """获取当前 Embedding 服务的加载诊断信息。

    返回结构：
    - mode：当前加载模式（bge / fallback / unknown）
    - embedding_dim：当前向量维度
    - attempts：加载尝试列表，每项含 source / success / elapsed_ms / error
    - model_name：配置的 BGE 模型名

    用于排查 BGE 加载失败原因，定位网络/缓存问题。
    """
    service = get_embedding_service()
    with _DIAG_LOCK:
        attempts = list(service._diagnostics)
    return {
        "mode": service.mode,
        "embedding_dim": service.dimension,
        "attempts": attempts,
        "model_name": service.model_name,
    }
