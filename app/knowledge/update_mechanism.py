"""文档自动解析与更新机制。

实现 Task 17 的三种更新策略：
- 全量更新（月度）：扫描目录，入库新增/变更，清理失效记录
- 增量更新（周度）：扫描目录，仅处理新增/变更，不删除
- 实时更新（API 触发）：单文件入库，复用 pipeline.ingest_document

设计要点：
- 线程安全：调度器状态（last_result / scan_cache）用 threading.RLock 保护
- 延迟计算：调度器不自动启动，按需调用 run_* 方法
- 扫描缓存：同目录同扩展名的扫描结果缓存，避免重复 IO
- 降级策略：目录不存在返回空结果；单文件失败记入 errors 不阻断其他文件；
  document_store 不可用降级为不入注册表（仅入库向量）

不引入新依赖，调度器内部仅用 threading 实现，不使用 APScheduler。
"""
from __future__ import annotations

import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.knowledge.parsers import parse_file
from app.schemas.update import UpdateMode

logger = get_logger("app.knowledge.update_mechanism")

# 默认支持的文件扩展名：与 parsers 注册表对齐，集中配置便于扩展
DEFAULT_EXTENSIONS: Tuple[str, ...] = (
    ".pdf",
    ".docx",
    ".html",
    ".htm",
    ".txt",
    ".md",
    ".markdown",
)


class UpdateResult(BaseModel):
    """单次更新操作的汇总结果。

    记录扫描数、各分类计数、耗时与错误列表，
    便于 API 返回与监控统计。字段与 UpdateResultResponse 对齐。
    """

    mode: UpdateMode = Field(..., description="本次更新模式")
    scanned: int = Field(0, description="扫描到的文件总数")
    added: int = Field(0, description="新增入库的文件数")
    updated: int = Field(0, description="内容变更后重新入库的文件数")
    skipped: int = Field(0, description="已存在且未变更被跳过的文件数")
    deleted: int = Field(0, description="全量更新中清理的失效记录数")
    failed: int = Field(0, description="处理失败的文件数")
    duration_seconds: float = Field(0.0, description="本次更新总耗时（秒）")
    errors: List[str] = Field(
        default_factory=list,
        description="失败文件与错误信息列表，便于排查",
    )


class UpdateScheduler:
    """文档更新调度器。

    提供目录扫描、全量/增量/单文件更新能力，
    内部用 RLock 保护 _last_result 与 _scan_cache，保证并发安全。

    单例通过 get_update_scheduler() 获取，测试用 reset_update_scheduler() 重置。
    """

    def __init__(self) -> None:
        # RLock 允许同线程重入，便于嵌套调用 run_* 方法时不死锁
        self._lock = RLock()
        # 最近一次更新结果，供 /status 端点查询
        self._last_result: Optional[UpdateResult] = None
        # 扫描缓存：key=(dir_path, extensions_tuple) -> List[Path]
        # 避免同一次批量更新内多次扫描同目录的 IO 开销
        self._scan_cache: Dict[Tuple[str, Tuple[str, ...]], List[Path]] = {}

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_last_result(self) -> Optional[UpdateResult]:
        """返回最近一次更新结果（线程安全拷贝）。"""
        with self._lock:
            return self._last_result.model_copy(deep=True) if self._last_result else None

    def clear_scan_cache(self) -> None:
        """清空扫描缓存，便于测试或强制重新扫描。"""
        with self._lock:
            self._scan_cache.clear()

    # ------------------------------------------------------------------
    # 目录扫描
    # ------------------------------------------------------------------

    def scan_directory(
        self,
        dir_path: str | Path,
        extensions: Optional[List[str]] = None,
    ) -> List[Path]:
        """递归扫描目录下指定扩展名的文件。

        目录不存在时返回空列表（降级策略：不抛错）。
        结果按 (dir_path, extensions) 缓存，避免重复 IO。
        返回 Path 列表按文件名排序，保证多次扫描顺序稳定。
        """
        path = Path(dir_path)
        if not path.exists() or not path.is_dir():
            # 降级：目录不存在不抛错，返回空列表
            logger.warning("扫描目录不存在或非目录：%s", path)
            return []

        # 规范化扩展名为小写元组，便于缓存键比较
        ext_tuple = self._normalize_extensions(extensions)
        cache_key = (str(path.resolve()), ext_tuple)

        with self._lock:
            # 命中缓存直接返回，避免重复遍历目录
            cached = self._scan_cache.get(cache_key)
            if cached is not None:
                logger.debug("命中扫描缓存：%s", cache_key)
                return list(cached)

        # 实际扫描在锁外执行，避免长时间持锁阻塞其他查询
        matched: List[Path] = []
        for file_path in path.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in ext_tuple:
                matched.append(file_path)
        # 排序保证顺序稳定，便于测试断言
        matched.sort(key=lambda p: p.name)

        with self._lock:
            self._scan_cache[cache_key] = list(matched)

        logger.info("扫描目录 %s 完成，匹配 %d 个文件", path, len(matched))
        return matched

    @staticmethod
    def _normalize_extensions(extensions: Optional[List[str]]) -> Tuple[str, ...]:
        """将扩展名列表规范为小写元组，空时使用默认支持格式。"""
        if not extensions:
            return DEFAULT_EXTENSIONS
        # 统一加点前缀并转小写，避免调用方传入 "pdf" / ".PDF" 等不一致写法
        normalized = []
        for ext in extensions:
            item = ext.lower()
            if not item.startswith("."):
                item = "." + item
            normalized.append(item)
        return tuple(normalized)

    # ------------------------------------------------------------------
    # 全量更新（月度）
    # ------------------------------------------------------------------

    def run_full_update(
        self,
        dir_path: str | Path,
        extensions: Optional[List[str]] = None,
    ) -> UpdateResult:
        """全量更新：扫描目录，入库新增/变更，并删除注册表中已不存在的记录。

        步骤：
        1. 扫描目录获取文件列表
        2. 构建 document_store 中 source -> doc_hash 映射
        3. 逐个文件解析比对 doc_hash，未变更跳过，变更或新增则入库
        4. 删除注册表中 source 不在扫描结果里的文档记录及 chunks
        """
        start_ts = time.time()
        result = UpdateResult(mode=UpdateMode.FULL)

        files = self.scan_directory(dir_path, extensions)
        result.scanned = len(files)
        if not files:
            # 目录为空或不存在，直接返回空结果
            result.duration_seconds = time.time() - start_ts
            self._save_result(result)
            return result

        # 构建注册表快照：source -> (doc_id, doc_hash)
        # 失败时返回空字典，降级为全部视为新增
        store_snapshot = self._build_store_snapshot()
        scanned_sources = {f.name for f in files}

        for file_path in files:
            self._process_file_for_update(
                file_path=file_path,
                store_snapshot=store_snapshot,
                result=result,
            )

        # 全量更新特有：清理注册表中已不存在的文件记录
        result.deleted = self._purge_missing_documents(store_snapshot, scanned_sources)

        result.duration_seconds = time.time() - start_ts
        logger.info(
            "全量更新完成：scanned=%d added=%d updated=%d skipped=%d deleted=%d failed=%d",
            result.scanned,
            result.added,
            result.updated,
            result.skipped,
            result.deleted,
            result.failed,
        )
        self._save_result(result)
        return result

    # ------------------------------------------------------------------
    # 增量更新（周度）
    # ------------------------------------------------------------------

    def run_incremental_update(
        self,
        dir_path: str | Path,
        extensions: Optional[List[str]] = None,
    ) -> UpdateResult:
        """增量更新：仅处理新增或 doc_hash 变化的文件，不删除已不存在的记录。

        与全量更新共用文件处理逻辑，但跳过 _purge_missing_documents 步骤。
        """
        start_ts = time.time()
        result = UpdateResult(mode=UpdateMode.INCREMENTAL)

        files = self.scan_directory(dir_path, extensions)
        result.scanned = len(files)
        if not files:
            result.duration_seconds = time.time() - start_ts
            self._save_result(result)
            return result

        store_snapshot = self._build_store_snapshot()

        for file_path in files:
            self._process_file_for_update(
                file_path=file_path,
                store_snapshot=store_snapshot,
                result=result,
            )

        result.duration_seconds = time.time() - start_ts
        logger.info(
            "增量更新完成：scanned=%d added=%d updated=%d skipped=%d failed=%d",
            result.scanned,
            result.added,
            result.updated,
            result.skipped,
            result.failed,
        )
        self._save_result(result)
        return result

    # ------------------------------------------------------------------
    # 单文件更新（API 触发）
    # ------------------------------------------------------------------

    def update_single_file(
        self,
        file_path: str | Path,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UpdateResult:
        """单文件更新：直接调用 pipeline 入库并注册版本。

        用于实时更新场景，不依赖目录扫描。
        失败时错误记入 result.errors，不抛异常。
        """
        start_ts = time.time()
        result = UpdateResult(mode=UpdateMode.INCREMENTAL)
        result.scanned = 1

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            # 文件不存在视为失败，不抛错
            result.failed = 1
            result.errors.append(f"文件不存在：{path}")
            result.duration_seconds = time.time() - start_ts
            self._save_result(result)
            return result

        try:
            # 复用 pipeline 入库，开启注册与文档版本管理
            # 延迟导入避免循环依赖
            from app.knowledge.pipeline import ingest_document

            ingest_result = ingest_document(
                path,
                metadata=metadata or {},
                register_document=True,
            )
            if ingest_result.error:
                # pipeline 内部已捕获异常并返回 error 字段
                result.failed = 1
                result.errors.append(f"{path.name}: {ingest_result.error}")
            else:
                # 单文件更新无法区分新增/变更（pipeline 不返回该信息），
                # 统一记为 added，调用方可通过 document_store 查询版本历史
                result.added = 1
        except Exception as exc:
            # 兜底：pipeline 未捕获的异常
            logger.exception("单文件更新失败：%s", path)
            result.failed = 1
            result.errors.append(f"{path.name}: {exc}")

        result.duration_seconds = time.time() - start_ts
        self._save_result(result)
        return result

    # ------------------------------------------------------------------
    # 内部辅助：文件处理与注册表清理
    # ------------------------------------------------------------------

    def _process_file_for_update(
        self,
        file_path: Path,
        store_snapshot: Dict[str, Tuple[str, str]],
        result: UpdateResult,
    ) -> None:
        """处理单个文件：解析比对 doc_hash，决定跳过/新增/更新。

        单文件失败不影响其他文件，错误记入 result.errors。
        store_snapshot: source -> (doc_id, doc_hash) 映射。
        """
        source = file_path.name
        try:
            # 解析获取 doc_hash，用于与注册表比对
            # 解析成本低于完整入库，可作为是否需要重入库的预筛
            parsed = parse_file(file_path)
            doc_hash = parsed.doc_hash

            existing = store_snapshot.get(source)
            if existing is not None and existing[1] == doc_hash:
                # 已存在且 hash 一致，跳过入库
                result.skipped += 1
                logger.debug("文件未变更，跳过：%s", source)
                return

            # 需要入库：区分新增 vs 更新
            is_update = existing is not None
            self._ingest_file(file_path)
            if is_update:
                result.updated += 1
            else:
                result.added += 1
        except Exception as exc:
            # 单文件失败不阻断其他文件
            logger.warning("处理文件 %s 失败：%s", source, exc)
            result.failed += 1
            result.errors.append(f"{source}: {exc}")

    @staticmethod
    def _ingest_file(file_path: Path) -> None:
        """调用 pipeline 入库并注册文档版本。

        延迟导入避免循环依赖；register_document=True 启用版本管理，
        document_store 不可用时 pipeline 内部会降级为不入注册表。
        """
        from app.knowledge.pipeline import ingest_document

        ingest_result = ingest_document(
            file_path,
            register_document=True,
        )
        if ingest_result.error:
            # pipeline 返回错误时抛出，由调用方记入 errors
            raise RuntimeError(ingest_result.error)

    def _build_store_snapshot(self) -> Dict[str, Tuple[str, str]]:
        """构建注册表快照：source -> (doc_id, doc_hash)。

        document_store 不可用时返回空字典，降级为全部视为新增。
        只采集 status != deleted 的文档，避免清理已删除记录时误判。
        """
        snapshot: Dict[str, Tuple[str, str]] = {}
        try:
            from app.knowledge.document_store import get_document_store

            store = get_document_store()
            for summary in store.list_documents():
                # 跳过已删除文档，避免误处理
                if summary.get("status") == "deleted":
                    continue
                doc_id = summary["doc_id"]
                doc = store.get_document(doc_id)
                if doc is None:
                    continue
                source = doc.get("source", "")
                doc_hash = doc.get("doc_hash", "")
                if source:
                    snapshot[source] = (doc_id, doc_hash)
        except Exception as exc:
            # document_store 不可用：降级为空快照，全部文件视为新增
            logger.warning("构建注册表快照失败，降级为空：%s", exc)
        return snapshot

    @staticmethod
    def _purge_missing_documents(
        store_snapshot: Dict[str, Tuple[str, str]],
        scanned_sources: set,
    ) -> int:
        """清理注册表中 source 不在扫描结果里的文档记录。

        返回成功删除的文档数。document_store 不可用时返回 0。
        """
        deleted_count = 0
        # 收集待删除 doc_id，避免在遍历快照时同时调用删除导致状态不一致
        to_delete: List[str] = []
        for source, (doc_id, _) in store_snapshot.items():
            if source not in scanned_sources:
                to_delete.append(doc_id)

        if not to_delete:
            return 0

        try:
            from app.knowledge.document_store import get_document_store

            store = get_document_store()
            for doc_id in to_delete:
                # delete_document 内部会移除向量库 chunks 并标记状态
                deleted = store.delete_document(doc_id)
                if deleted >= 0:
                    # delete_document 返回删除的 chunk 数，>=0 表示文档记录已处理
                    deleted_count += 1
                    logger.info("全量更新清理失效文档：%s", doc_id)
        except Exception as exc:
            # document_store 不可用：跳过清理，不阻断主流程
            logger.warning("清理失效文档失败：%s", exc)
        return deleted_count

    # ------------------------------------------------------------------
    # 内部辅助：结果缓存
    # ------------------------------------------------------------------

    def _save_result(self, result: UpdateResult) -> None:
        """保存最近一次更新结果（线程安全）。"""
        with self._lock:
            self._last_result = result


# ----------------------------------------------------------------------
# 模块级单例
# ----------------------------------------------------------------------

_update_scheduler: Optional[UpdateScheduler] = None
_singleton_lock = RLock()


def get_update_scheduler() -> UpdateScheduler:
    """获取 UpdateScheduler 单例。

    用独立锁保护单例创建，避免多线程并发时重复构造。
    单例本身内部已有 RLock 保护运行时状态。
    """
    global _update_scheduler
    if _update_scheduler is None:
        with _singleton_lock:
            if _update_scheduler is None:
                _update_scheduler = UpdateScheduler()
    return _update_scheduler


def reset_update_scheduler() -> None:
    """重置单例，便于测试切换持久化目录或清空缓存。"""
    global _update_scheduler
    with _singleton_lock:
        _update_scheduler = None
