"""文档元数据存储（基于 JSON 文件持久化）。

为知识库管理后台提供文档级元数据与版本管理能力：
- 维护文档 ID、当前版本号、历史版本列表、各版本 chunk_ids 与 chunk 文本快照
- 支持版本回滚（含向向量库恢复 chunks）、归档、删除
- 线程安全（RLock），降级策略：读取失败时回退为空字典

设计要点：
不引入新数据库依赖，复用 ChromaDB 持久化目录存放 JSON 文件。
chunk 文本快照用于回滚时重新入库，避免依赖向量库的软删除能力。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("app.knowledge.document_store")

# 文档状态枚举：active=可用 / deleted=已删除 / archived=已归档（历史版本）
DOC_STATUS_ACTIVE = "active"
DOC_STATUS_DELETED = "deleted"
DOC_STATUS_ARCHIVED = "archived"


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串，便于跨时区统一。"""
    return datetime.now(timezone.utc).isoformat()


def _generate_doc_id() -> str:
    """生成文档 ID：doc-{uuid4_hex[:12]}，保证全局唯一且可读。"""
    return f"doc-{uuid.uuid4().hex[:12]}"


class DocumentStore:
    """文档元数据存储。

    持久化到 {CHROMA_PERSIST_DIR}/{DOC_STORE_FILENAME} 的 JSON 文件，
    内部结构：
        {
          "documents": {
            "doc-xxxxxxxxxxxx": {
              "doc_id": "...", "source": "...", "doc_hash": "...",
              "current_version": "v1", "status": "active",
              "created_at": "...", "updated_at": "...",
              "versions": [
                {"version": "v1", "doc_hash": "...", "chunk_ids": [...],
                 "chunk_texts": [...], "status": "active", "created_at": "..."}
              ]
            }
          }
        }

    线程安全：所有写操作持 RLock，避免并发入库导致 JSON 文件损坏。
    """

    def __init__(
        self,
        persist_dir: str = "",
        filename: str = "",
    ) -> None:
        settings = get_settings()
        self._persist_dir = persist_dir or settings.CHROMA_PERSIST_DIR
        self._filename = filename or settings.DOC_STORE_FILENAME
        self._store_path = Path(self._persist_dir) / self._filename
        self._lock = RLock()
        # documents: doc_id -> 文档记录字典
        self._documents: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 持久化读写
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """从 JSON 文件加载文档记录，失败时降级为空字典避免阻断启动。"""
        try:
            if self._store_path.exists():
                raw = self._store_path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
                self._documents = data.get("documents", {}) or {}
                logger.info(
                    "文档存储加载完成：共 %d 个文档，路径=%s",
                    len(self._documents),
                    self._store_path,
                )
        except Exception as exc:
            # 降级：读取失败不阻断主流程，使用空字典继续
            logger.warning("文档存储加载失败，降级为空字典：%s", exc)
            self._documents = {}

    def _save(self) -> None:
        """持久化到 JSON 文件，先写临时文件再替换避免写中途崩溃损坏。"""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"documents": self._documents}
            tmp_path = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._store_path)
        except Exception as exc:
            # 持久化失败仅告警，内存状态仍可用，避免拖垮入库主流程
            logger.warning("文档存储持久化失败：%s", exc)

    # ------------------------------------------------------------------
    # 向量库交互（外部组合，不修改 vectorstore.py）
    # ------------------------------------------------------------------

    def _get_collection(self):
        """获取 ChromaDB collection，用于按元数据查询 / 删除 chunks。

        通过 vectorstore 单例访问内部 collection，避免重复打开 PersistentClient。
        """
        from app.knowledge.vectorstore import get_vector_store

        return get_vector_store()._collection

    def _query_chunk_ids_by_version(self, doc_id: str, version: str) -> list[str]:
        """通过 doc_id + version 元数据查询向量库中实际存在的 chunk_ids。

        入库时 metadata 已回填 doc_id 与 version，可据此精确过滤。
        ChromaDB 多条件过滤需用 $and 操作符，直接传多 key 字典会报错。
        查询失败时返回空列表，调用方按"无 chunks"处理。
        """
        try:
            result = self._get_collection().get(
                where={"$and": [{"doc_id": doc_id}, {"version": version}]},
            )
            return list(result.get("ids") or [])
        except Exception as exc:
            logger.warning(
                "查询版本 chunks 失败 doc_id=%s version=%s：%s",
                doc_id,
                version,
                exc,
            )
            return []

    def _delete_chunks_from_vectorstore(self, chunk_ids: list[str]) -> int:
        """从向量库删除指定 chunks，返回成功删除的数量。"""
        if not chunk_ids:
            return 0
        try:
            self._get_collection().delete(ids=chunk_ids)
            logger.info("从向量库删除 %d 个 chunks", len(chunk_ids))
            return len(chunk_ids)
        except Exception as exc:
            logger.warning("删除 chunks 失败：%s", exc)
            return 0

    def _reingest_chunks(self, texts: list[str], doc_id: str, version: str) -> list[str]:
        """重新入库 chunk 文本，返回新生成的 chunk_ids。

        回滚场景下目标版本 chunks 可能已被删除，需用存储的文本快照重建。
        复用 embedding 服务与 vectorstore，保证与正常入库一致。
        跳过去重检查：回滚的 chunks 此前已被删除，不应因其他版本存在相似内容而被跳过。
        """
        if not texts:
            return []
        try:
            from app.knowledge.embeddings import get_embedding_service
            from app.knowledge.vectorstore import get_vector_store
            from app.schemas.knowledge import TextChunk

            embedding_service = get_embedding_service()
            embeddings = embedding_service.embed_texts(texts)
            chunks = [
                TextChunk(
                    text=text,
                    metadata={"doc_id": doc_id, "version": version},
                )
                for text in texts
            ]
            metadatas = [{"doc_id": doc_id, "version": version} for _ in texts]
            store = get_vector_store()
            # 回滚重入库跳过去重：chunks 此前已被删除，不应被其他版本相似内容拦截
            store.add_chunks(chunks, embeddings, metadatas, skip_dedup=True)
            # 查询刚写入的 chunks 获取真实 ids
            return self._query_chunk_ids_by_version(doc_id, version)
        except Exception as exc:
            logger.warning("重新入库 chunks 失败 doc_id=%s version=%s：%s", doc_id, version, exc)
            return []

    # ------------------------------------------------------------------
    # 文档 / 版本管理 API
    # ------------------------------------------------------------------

    def prepare_version(self, doc_hash: str, source: str = "") -> tuple[str, str]:
        """预分配文档 ID 与版本号（入库前调用，便于回填 metadata）。

        如果 doc_hash 已存在则视为同文档新版本，否则新建文档记录。
        此时 chunk_ids 尚未填充，需在入库后调用 finalize_version 补全。
        """
        with self._lock:
            existing_doc_id = self._find_doc_id_by_hash(doc_hash)
            if existing_doc_id is not None:
                doc_id = existing_doc_id
                version = self._append_version(doc_id, doc_hash, source)
            else:
                doc_id = _generate_doc_id()
                version = "v1"
                now = _now_iso()
                self._documents[doc_id] = {
                    "doc_id": doc_id,
                    "source": source,
                    "doc_hash": doc_hash,
                    "current_version": version,
                    "status": DOC_STATUS_ACTIVE,
                    "created_at": now,
                    "updated_at": now,
                    "versions": [
                        {
                            "version": version,
                            "doc_hash": doc_hash,
                            "chunk_ids": [],
                            "chunk_texts": [],
                            "status": DOC_STATUS_ACTIVE,
                            "created_at": now,
                        }
                    ],
                }
                logger.info("新建文档记录：doc_id=%s version=%s source=%s", doc_id, version, source)
            self._save()
            return doc_id, version

    def _append_version(self, doc_id: str, doc_hash: str, source: str) -> str:
        """为已存在文档追加新版本，旧 active 版本标记为 archived。"""
        doc = self._documents[doc_id]
        # 旧 active 版本归档，避免多个 active 共存
        for ver in doc["versions"]:
            if ver["status"] == DOC_STATUS_ACTIVE:
                ver["status"] = DOC_STATUS_ARCHIVED
        version_num = len(doc["versions"]) + 1
        version = f"v{version_num}"
        now = _now_iso()
        doc["versions"].append(
            {
                "version": version,
                "doc_hash": doc_hash,
                "chunk_ids": [],
                "chunk_texts": [],
                "status": DOC_STATUS_ACTIVE,
                "created_at": now,
            }
        )
        doc["current_version"] = version
        doc["doc_hash"] = doc_hash
        doc["updated_at"] = now
        # 文档曾被删除则恢复为 active
        if doc["status"] == DOC_STATUS_DELETED:
            doc["status"] = DOC_STATUS_ACTIVE
        if source:
            doc["source"] = source
        logger.info("文档 %s 追加新版本 %s", doc_id, version)
        return version

    def finalize_version(
        self,
        doc_id: str,
        version: str,
        chunk_texts: list[str],
    ) -> None:
        """入库后补全版本的 chunk_ids 与文本快照（用于回滚恢复）。

        chunk_ids 通过 doc_id+version 元数据从向量库查询获取，
        chunk_texts 由 pipeline 传入，作为回滚时的重建素材。
        """
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                logger.warning("finalize_version 文档不存在：%s", doc_id)
                return
            ver = self._find_version(doc, version)
            if ver is None:
                logger.warning("finalize_version 版本不存在：%s/%s", doc_id, version)
                return
            chunk_ids = self._query_chunk_ids_by_version(doc_id, version)
            ver["chunk_ids"] = chunk_ids
            ver["chunk_texts"] = list(chunk_texts)
            doc["updated_at"] = _now_iso()
            self._save()
            logger.info(
                "版本 %s/%s 完成 finalize，chunk_ids=%d",
                doc_id,
                version,
                len(chunk_ids),
            )

    def _find_doc_id_by_hash(self, doc_hash: str) -> str | None:
        """通过 doc_hash 查找已存在文档（任一版本 hash 匹配即视为同文档）。"""
        if not doc_hash:
            return None
        for doc_id, doc in self._documents.items():
            if doc.get("doc_hash") == doc_hash:
                return doc_id
            for ver in doc.get("versions", []):
                if ver.get("doc_hash") == doc_hash:
                    return doc_id
        return None

    @staticmethod
    def _find_version(doc: dict[str, Any], version: str) -> dict[str, Any] | None:
        """在文档记录中查找指定版本字典。"""
        for ver in doc.get("versions", []):
            if ver.get("version") == version:
                return ver
        return None

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def list_documents(self) -> list[dict[str, Any]]:
        """列出全部文档摘要（浅拷贝避免外部修改内部状态）。"""
        with self._lock:
            summaries: list[dict[str, Any]] = []
            for doc in self._documents.values():
                summaries.append(
                    {
                        "doc_id": doc["doc_id"],
                        "source": doc.get("source", ""),
                        "current_version": doc.get("current_version", ""),
                        "status": doc.get("status", DOC_STATUS_ACTIVE),
                        "version_count": len(doc.get("versions", [])),
                        "updated_at": doc.get("updated_at", ""),
                    }
                )
            return summaries

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        """查询单个文档详情（含版本历史，深拷贝避免外部修改）。"""
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                return None
            return json.loads(json.dumps(doc, ensure_ascii=False))

    def list_versions(self, doc_id: str) -> list[dict[str, Any]]:
        """列出文档的全部版本（精简字段，不含 chunk_ids/texts）。"""
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                return []
            versions: list[dict[str, Any]] = []
            for ver in doc.get("versions", []):
                versions.append(
                    {
                        "version": ver.get("version", ""),
                        "doc_hash": ver.get("doc_hash", ""),
                        "status": ver.get("status", DOC_STATUS_ACTIVE),
                        "chunk_count": len(ver.get("chunk_ids", [])),
                        "created_at": ver.get("created_at", ""),
                    }
                )
            return versions

    def get_current_version(self, doc_id: str) -> str:
        """返回文档当前版本号，不存在时返回空串。"""
        with self._lock:
            doc = self._documents.get(doc_id)
            return doc.get("current_version", "") if doc else ""

    def get_version_chunk_ids(self, doc_id: str, version: str) -> list[str]:
        """返回指定版本的 chunk_ids（用于灰度检索过滤）。"""
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                return []
            ver = self._find_version(doc, version)
            return list(ver.get("chunk_ids", [])) if ver else []

    # ------------------------------------------------------------------
    # 删除 / 回滚 / 归档
    # ------------------------------------------------------------------

    def delete_document(self, doc_id: str) -> int:
        """删除文档：从向量库移除全部 chunks，并标记文档状态为 deleted。

        返回实际删除的 chunk 数量。文档记录保留用于审计，仅状态变更。
        """
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                logger.warning("删除文档不存在：%s", doc_id)
                return 0
            # 收集所有版本的 chunk_ids 去重后删除
            all_chunk_ids: list[str] = []
            for ver in doc.get("versions", []):
                all_chunk_ids.extend(ver.get("chunk_ids", []))
            unique_ids = list(set(cid for cid in all_chunk_ids if cid))
            deleted_count = self._delete_chunks_from_vectorstore(unique_ids)
            # 标记文档与各版本为 deleted
            doc["status"] = DOC_STATUS_DELETED
            for ver in doc.get("versions", []):
                ver["status"] = DOC_STATUS_DELETED
            doc["updated_at"] = _now_iso()
            self._save()
            logger.info("文档 %s 已删除，移除 chunks=%d", doc_id, deleted_count)
            return deleted_count

    def rollback_document(self, doc_id: str, target_version: str) -> bool:
        """回滚到指定版本。

        步骤：
        1. 当前 active 版本标记为 archived
        2. 目标版本标记为 active，并设为 current_version
        3. 若目标版本 chunks 已从向量库删除，用存储的文本快照重新入库
        """
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                logger.warning("回滚失败，文档不存在：%s", doc_id)
                return False
            target_ver = self._find_version(doc, target_version)
            if target_ver is None:
                logger.warning("回滚失败，版本不存在：%s/%s", doc_id, target_version)
                return False
            # 当前 active 版本归档
            for ver in doc["versions"]:
                if ver["status"] == DOC_STATUS_ACTIVE and ver["version"] != target_version:
                    ver["status"] = DOC_STATUS_ARCHIVED
            # 检查目标版本 chunks 是否仍在向量库中
            existing_ids = set(self._query_chunk_ids_by_version(doc_id, target_version))
            stored_texts = target_ver.get("chunk_texts", [])
            # 向量库中无 chunks 但有文本快照时需重建，覆盖两种场景：
            # 1) chunks 被 delete_document 删除
            # 2) chunks 因入库去重未实际写入（chunk_ids 为空但 chunk_texts 非空）
            missing_texts: list[str] = []
            if not existing_ids and stored_texts:
                missing_texts = stored_texts
            if missing_texts:
                new_ids = self._reingest_chunks(missing_texts, doc_id, target_version)
                if new_ids:
                    target_ver["chunk_ids"] = new_ids
            # 标记目标版本为 active
            target_ver["status"] = DOC_STATUS_ACTIVE
            doc["current_version"] = target_version
            doc["status"] = DOC_STATUS_ACTIVE
            doc["updated_at"] = _now_iso()
            self._save()
            logger.info("文档 %s 已回滚到版本 %s", doc_id, target_version)
            return True

    def archive_version(self, doc_id: str, version: str) -> bool:
        """归档指定版本（不删除 chunks，仅状态变更）。"""
        with self._lock:
            doc = self._documents.get(doc_id)
            if doc is None:
                return False
            ver = self._find_version(doc, version)
            if ver is None:
                return False
            ver["status"] = DOC_STATUS_ARCHIVED
            doc["updated_at"] = _now_iso()
            self._save()
            logger.info("文档 %s 版本 %s 已归档", doc_id, version)
            return True


# 模块级单例，避免重复加载 JSON 文件
_document_store: DocumentStore | None = None


def get_document_store() -> DocumentStore:
    """获取 DocumentStore 单例。"""
    global _document_store
    if _document_store is None:
        _document_store = DocumentStore()
    return _document_store


def reset_document_store() -> None:
    """重置单例，便于测试切换持久化目录。"""
    global _document_store
    _document_store = None
