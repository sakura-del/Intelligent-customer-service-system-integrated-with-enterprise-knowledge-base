"""文档自动解析与更新机制测试。

覆盖 Task 17 三种更新策略：
- 全量更新：扫描入库 + 跳过未变更 + 删除失效记录
- 增量更新：扫描入库 + 跳过未变更 + 不删除
- 单文件更新：API 触发实时入库
- 并发安全：多线程调度不损坏状态
- 错误降级：目录不存在/文件损坏/单文件失败不阻断

测试隔离：独立 chroma 目录 + 函数级重置 document_store/update_scheduler，
保证每个用例从干净的注册表状态开始。
"""
from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path

import pytest

# 测试用独立持久化目录，与其他测试模块隔离
TEST_PERSIST_DIR = "./tests/_chroma_data_update"
SAMPLE_FAQ = Path(__file__).parent / "sample_data" / "faq.md"
SAMPLE_MANUAL = Path(__file__).parent / "sample_data" / "product_manual.md"
SAMPLE_POLICY = Path(__file__).parent / "sample_data" / "return_policy.md"


# ----------------------------------------------------------------------
# 模块级 fixture：隔离 ChromaDB 目录与重置全局单例
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离向量库目录并重置知识库相关单例。

    覆盖范围：vectorstore / document_store / embeddings / update_scheduler，
    保证模块内全部用例共享同一份干净的向量库与调度器配置。
    """
    from app.core.config import get_settings
    from app.knowledge import (
        document_store as document_store_module,
    )
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        update_mechanism as update_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留，保证从零开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置所有相关单例，确保新配置生效
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()
    update_module.reset_update_scheduler()

    yield

    # 测试结束恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    document_store_module.reset_document_store()
    update_module.reset_update_scheduler()


@pytest.fixture(autouse=True)
def _reset_per_test():
    """每个用例前重置 update_scheduler 与 document_store，保证用例隔离。

    update_scheduler 缓存（scan_cache / last_result）与 document_store 注册表
    在用例间共享会导致状态污染，这里每用例清空。
    vectorstore 复用模块级实例（含历史 chunks），但不影响更新计数的断言。
    """
    from app.knowledge import (
        document_store as document_store_module,
    )
    from app.knowledge import (
        update_mechanism as update_module,
    )

    # 重置调度器：清空扫描缓存与 last_result
    update_module.reset_update_scheduler()

    # 重置 document_store 并删除持久化文件，保证注册表从零开始
    document_store_module.reset_document_store()
    store_path = Path(TEST_PERSIST_DIR) / "_doc_store.json"
    if store_path.exists():
        store_path.unlink()

    yield

    # 用例结束后重置单例，避免影响后续用例
    update_module.reset_update_scheduler()
    document_store_module.reset_document_store()


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def _make_unique_file(dir_path: Path, name: str | None = None, content: str = "") -> Path:
    """创建内容唯一的测试文件，避免与其他用例的 doc_hash 冲突。

    每个文件追加 uuid 标记，保证 doc_hash 全局唯一，
    这样即使复用同一文件名也不会被误判为已存在。
    """
    suffix = ".md"
    if name is None:
        name = f"test_{uuid.uuid4().hex[:8]}{suffix}"
    file_path = dir_path / name
    # 唯一标记确保内容不重复，避免 fallback 模式下向量去重干扰
    marker = f"# 唯一标记 {uuid.uuid4().hex}"
    if content:
        file_path.write_text(content + "\n\n" + marker, encoding="utf-8")
    else:
        # 默认使用样例 FAQ 内容 + 唯一标记，保证有足够文本可切分
        faq_text = SAMPLE_FAQ.read_text(encoding="utf-8")
        file_path.write_text(faq_text + "\n\n" + marker, encoding="utf-8")
    return file_path


def _ingest_with_registration(file_path: Path) -> str:
    """辅助：入库并注册文档，返回 doc_id。"""
    from app.knowledge.pipeline import ingest_document

    result = ingest_document(
        file_path,
        metadata={"knowledge_type": "faq"},
        register_document=True,
    )
    assert result.error is None, f"入库失败：{result.error}"
    assert result.doc_id, "应返回 doc_id"
    return result.doc_id


# ----------------------------------------------------------------------
# SubTask 17.1：目录扫描
# ----------------------------------------------------------------------


def test_scan_directory_returns_supported_files(tmp_path):
    """扫描目录应返回所有支持格式的文件。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    # 创建多种格式的文件
    _make_unique_file(tmp_path, "doc1.md")
    _make_unique_file(tmp_path, "doc2.txt")
    (tmp_path / "doc3.html").write_text("<html><body>test</body></html>", encoding="utf-8")

    scheduler = get_update_scheduler()
    files = scheduler.scan_directory(tmp_path)

    assert len(files) == 3
    names = {f.name for f in files}
    assert names == {"doc1.md", "doc2.txt", "doc3.html"}


def test_scan_directory_filters_by_extensions(tmp_path):
    """按扩展名过滤应只返回指定格式的文件。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    _make_unique_file(tmp_path, "doc1.md")
    _make_unique_file(tmp_path, "doc2.txt")
    (tmp_path / "doc3.html").write_text("<html></html>", encoding="utf-8")

    scheduler = get_update_scheduler()
    files = scheduler.scan_directory(tmp_path, extensions=[".md"])
    assert len(files) == 1
    assert files[0].name == "doc1.md"


def test_scan_directory_nonexistent_returns_empty():
    """目录不存在时应降级返回空列表，不抛异常。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    scheduler = get_update_scheduler()
    files = scheduler.scan_directory("/nonexistent/path/12345")
    assert files == []


def test_scan_directory_ignores_unsupported_formats(tmp_path):
    """扫描应忽略不支持的文件格式。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    _make_unique_file(tmp_path, "doc.md")
    (tmp_path / "image.png").write_bytes(b"fake png")
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")

    scheduler = get_update_scheduler()
    files = scheduler.scan_directory(tmp_path)
    assert len(files) == 1
    assert files[0].name == "doc.md"


def test_scan_directory_caches_results(tmp_path):
    """同目录同扩展名的扫描结果应被缓存，避免重复 IO。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    _make_unique_file(tmp_path, "cached.md")

    scheduler = get_update_scheduler()
    files1 = scheduler.scan_directory(tmp_path)
    files2 = scheduler.scan_directory(tmp_path)

    # 两次扫描结果应一致（命中缓存）
    assert len(files1) == 1
    assert len(files2) == 1
    assert files1[0].name == files2[0].name

    # 清空缓存后应重新扫描
    scheduler.clear_scan_cache()
    files3 = scheduler.scan_directory(tmp_path)
    assert len(files3) == 1


# ----------------------------------------------------------------------
# SubTask 17.1：全量更新
# ----------------------------------------------------------------------


def test_full_update_ingests_new_files(tmp_path):
    """全量更新应入库目录下所有新文件。"""
    from app.knowledge.update_mechanism import UpdateMode, get_update_scheduler

    _make_unique_file(tmp_path, "new1.md")
    _make_unique_file(tmp_path, "new2.md")

    scheduler = get_update_scheduler()
    result = scheduler.run_full_update(tmp_path)

    assert result.mode == UpdateMode.FULL
    assert result.scanned == 2
    assert result.added == 2
    assert result.updated == 0
    assert result.skipped == 0
    assert result.failed == 0
    assert result.deleted == 0
    assert result.duration_seconds > 0


def test_full_update_skips_unchanged_files(tmp_path):
    """全量更新对已注册且未变更的文件应跳过。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    # 先入库一个文件，使其注册到 document_store
    file_path = _make_unique_file(tmp_path, "skip_test.md")
    _ingest_with_registration(file_path)

    scheduler = get_update_scheduler()
    # 再次全量更新，文件内容未变，应跳过
    result = scheduler.run_full_update(tmp_path)

    assert result.scanned == 1
    assert result.skipped == 1
    assert result.added == 0
    assert result.updated == 0
    assert result.failed == 0


def test_full_update_updates_changed_files(tmp_path):
    """全量更新对内容变更的文件应重新入库。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    # 先入库原始内容
    file_path = tmp_path / "changed.md"
    file_path.write_text("# 原始内容\n\n这是一段测试文本。", encoding="utf-8")
    _ingest_with_registration(file_path)

    # 修改文件内容（doc_hash 会变化）
    file_path.write_text("# 修改后的内容\n\n这是完全不同的文本。", encoding="utf-8")

    scheduler = get_update_scheduler()
    result = scheduler.run_full_update(tmp_path)

    assert result.scanned == 1
    assert result.updated == 1
    assert result.added == 0
    assert result.skipped == 0
    assert result.failed == 0


def test_full_update_deletes_missing_records(tmp_path):
    """全量更新应删除注册表中已不存在文件的记录。"""
    from app.knowledge.document_store import get_document_store
    from app.knowledge.update_mechanism import get_update_scheduler

    # 在注册表中创建一个"幽灵"文档（对应文件不存在于扫描目录）
    store = get_document_store()
    ghost_doc_id, _ = store.prepare_version("ghost_hash_unique_001", "ghost_missing.md")
    assert store.get_document(ghost_doc_id) is not None

    # 扫描目录里只放一个真实文件
    _make_unique_file(tmp_path, "real.md")

    scheduler = get_update_scheduler()
    result = scheduler.run_full_update(tmp_path)

    assert result.scanned == 1
    assert result.added == 1  # real.md 是新文件
    assert result.deleted == 1  # ghost_missing.md 被清理
    assert result.failed == 0

    # 验证幽灵文档已被标记删除
    ghost_doc = store.get_document(ghost_doc_id)
    assert ghost_doc["status"] == "deleted"


def test_full_update_directory_not_exist_returns_empty():
    """全量更新目录不存在时应返回空结果，不抛异常。"""
    from app.knowledge.update_mechanism import UpdateMode, get_update_scheduler

    scheduler = get_update_scheduler()
    result = scheduler.run_full_update("/nonexistent/path/67890")

    assert result.mode == UpdateMode.FULL
    assert result.scanned == 0
    assert result.added == 0
    assert result.failed == 0
    assert result.duration_seconds >= 0


def test_full_update_mixed_scenario(tmp_path):
    """全量更新混合场景：新增 + 跳过 + 删除同时发生。"""
    from app.knowledge.document_store import get_document_store
    from app.knowledge.update_mechanism import get_update_scheduler

    # 1. 预入库一个文件（扫描时会跳过）
    keep_file = _make_unique_file(tmp_path, "keep.md")
    _ingest_with_registration(keep_file)

    # 2. 注册一个幽灵文档（扫描时会删除）
    store = get_document_store()
    store.prepare_version("ghost_hash_mixed_002", "ghost_mixed.md")

    # 3. 新增一个文件（扫描时会入库）
    _make_unique_file(tmp_path, "extra.md")

    scheduler = get_update_scheduler()
    result = scheduler.run_full_update(tmp_path)

    assert result.scanned == 2  # keep.md + extra.md
    assert result.skipped == 1  # keep.md 未变更
    assert result.added == 1  # extra.md 新增
    assert result.deleted == 1  # ghost_mixed.md 被清理
    assert result.failed == 0


# ----------------------------------------------------------------------
# SubTask 17.1：增量更新
# ----------------------------------------------------------------------


def test_incremental_update_ingests_new_files(tmp_path):
    """增量更新应入库目录下所有新文件。"""
    from app.knowledge.update_mechanism import UpdateMode, get_update_scheduler

    _make_unique_file(tmp_path, "incr1.md")
    _make_unique_file(tmp_path, "incr2.md")

    scheduler = get_update_scheduler()
    result = scheduler.run_incremental_update(tmp_path)

    assert result.mode == UpdateMode.INCREMENTAL
    assert result.scanned == 2
    assert result.added == 2
    assert result.failed == 0
    assert result.deleted == 0  # 增量更新不删除


def test_incremental_update_skips_unchanged(tmp_path):
    """增量更新对未变更文件应跳过。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    file_path = _make_unique_file(tmp_path, "incr_skip.md")
    _ingest_with_registration(file_path)

    scheduler = get_update_scheduler()
    result = scheduler.run_incremental_update(tmp_path)

    assert result.scanned == 1
    assert result.skipped == 1
    assert result.added == 0
    assert result.failed == 0


def test_incremental_update_does_not_delete_missing(tmp_path):
    """增量更新不应删除注册表中已不存在的文件记录。"""
    from app.knowledge.document_store import get_document_store
    from app.knowledge.update_mechanism import get_update_scheduler

    # 注册一个幽灵文档
    store = get_document_store()
    ghost_doc_id, _ = store.prepare_version("ghost_incr_003", "ghost_incr.md")

    # 扫描目录里放一个真实文件
    _make_unique_file(tmp_path, "real_incr.md")

    scheduler = get_update_scheduler()
    result = scheduler.run_incremental_update(tmp_path)

    assert result.scanned == 1
    assert result.added == 1
    assert result.deleted == 0  # 增量更新不删除
    assert result.failed == 0

    # 幽灵文档应仍存在且未被删除
    ghost_doc = store.get_document(ghost_doc_id)
    assert ghost_doc is not None
    assert ghost_doc["status"] == "active"


# ----------------------------------------------------------------------
# SubTask 17.1：单文件更新（API 触发）
# ----------------------------------------------------------------------


def test_update_single_file_success(tmp_path):
    """单文件更新应成功入库并注册文档。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    file_path = _make_unique_file(tmp_path, "single.md")

    scheduler = get_update_scheduler()
    result = scheduler.update_single_file(file_path)

    assert result.scanned == 1
    assert result.added == 1
    assert result.failed == 0
    assert result.errors == []
    assert result.duration_seconds > 0


def test_update_single_file_nonexistent_degrades():
    """单文件不存在时应降级返回失败结果，不抛异常。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    scheduler = get_update_scheduler()
    result = scheduler.update_single_file("/nonexistent/file/12345.md")

    assert result.scanned == 1
    assert result.failed == 1
    assert result.added == 0
    assert len(result.errors) == 1
    assert "文件不存在" in result.errors[0]


def test_update_single_file_with_metadata(tmp_path):
    """单文件更新应支持传入元数据覆盖。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    file_path = _make_unique_file(tmp_path, "metadata_test.md")

    scheduler = get_update_scheduler()
    result = scheduler.update_single_file(
        file_path,
        metadata={
            "knowledge_type": "policy",
            "product_category": "electronics",
        },
    )

    assert result.added == 1
    assert result.failed == 0


# ----------------------------------------------------------------------
# 状态查询
# ----------------------------------------------------------------------


def test_get_last_result_none_initially():
    """未执行过更新时 get_last_result 应返回 None。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    scheduler = get_update_scheduler()
    assert scheduler.get_last_result() is None


def test_get_last_result_returns_recent(tmp_path):
    """执行更新后 get_last_result 应返回最近一次结果。"""
    from app.knowledge.update_mechanism import UpdateMode, get_update_scheduler

    _make_unique_file(tmp_path, "status_test.md")

    scheduler = get_update_scheduler()
    result = scheduler.run_full_update(tmp_path)

    last = scheduler.get_last_result()
    assert last is not None
    assert last.mode == UpdateMode.FULL
    assert last.scanned == result.scanned
    assert last.added == result.added


# ----------------------------------------------------------------------
# 并发安全
# ----------------------------------------------------------------------


def test_concurrent_full_update_thread_safe(tmp_path):
    """多线程并发全量更新应全部成功且不损坏调度器状态。

    预入库所有文件后并发更新，所有线程应报告全部跳过（无新增无失败）。
    """
    from app.knowledge.update_mechanism import get_update_scheduler

    # 预入库 3 个文件，使并发更新时全部命中跳过逻辑
    files = [
        _make_unique_file(tmp_path, f"concurrent_{i}.md") for i in range(3)
    ]
    for f in files:
        _ingest_with_registration(f)

    scheduler = get_update_scheduler()
    results = []
    lock = threading.Lock()

    def run_update():
        res = scheduler.run_full_update(tmp_path)
        with lock:
            results.append(res)

    threads = [threading.Thread(target=run_update) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 全部 4 个线程应成功完成
    assert len(results) == 4
    for res in results:
        assert res.scanned == 3
        assert res.skipped == 3
        assert res.added == 0
        assert res.failed == 0


def test_concurrent_single_file_thread_safe(tmp_path):
    """多线程并发单文件更新应全部成功完成。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    # 每个线程更新不同的文件，避免 doc_hash 冲突
    files = [
        _make_unique_file(tmp_path, f"single_concurrent_{i}.md") for i in range(3)
    ]

    scheduler = get_update_scheduler()
    results = []
    lock = threading.Lock()

    def update_file(file_path):
        res = scheduler.update_single_file(file_path)
        with lock:
            results.append(res)

    threads = [threading.Thread(target=update_file, args=(f,)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 全部 3 个线程应成功完成
    assert len(results) == 3
    for res in results:
        assert res.failed == 0
        assert res.duration_seconds >= 0


# ----------------------------------------------------------------------
# 错误降级
# ----------------------------------------------------------------------


def test_full_update_corrupt_file_degrades(tmp_path):
    """损坏文件应记入 errors 不影响其他文件处理。"""
    from app.knowledge.update_mechanism import get_update_scheduler

    # 创建一个有效的 md 文件
    _make_unique_file(tmp_path, "valid.md")

    # 创建一个损坏的 pdf 文件（内容不是有效 PDF）
    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"this is not a valid pdf file content")

    scheduler = get_update_scheduler()
    result = scheduler.run_full_update(tmp_path)

    assert result.scanned == 2
    # 有效文件应成功入库
    assert result.added >= 1
    # 损坏文件应失败
    assert result.failed == 1
    assert len(result.errors) == 1
    assert "corrupt.pdf" in result.errors[0]


# ----------------------------------------------------------------------
# API 端点测试
# ----------------------------------------------------------------------


@pytest.fixture
def update_client():
    """提供仅挂载 update 路由的 FastAPI TestClient。

    不复用 app.main.app 避免引入未重置的其他路由状态，
    update_router 自带 prefix 与 verify_api_key 依赖。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1.update import router as update_router

    app = FastAPI()
    app.include_router(update_router)
    return TestClient(app)


def test_api_full_update_endpoint(update_client, tmp_path):
    """POST /api/v1/update/full 应触发全量更新并返回结构化结果。"""
    _make_unique_file(tmp_path, "api_full.md")

    response = update_client.post(
        "/api/v1/update/full",
        json={"dir_path": str(tmp_path)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "full"
    assert body["scanned"] == 1
    assert body["added"] == 1
    assert body["failed"] == 0


def test_api_incremental_update_endpoint(update_client, tmp_path):
    """POST /api/v1/update/incremental 应触发增量更新。"""
    _make_unique_file(tmp_path, "api_incr.md")

    response = update_client.post(
        "/api/v1/update/incremental",
        json={"dir_path": str(tmp_path)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "incremental"
    assert body["scanned"] == 1
    assert body["added"] == 1
    assert body["deleted"] == 0


def test_api_single_file_endpoint(update_client, tmp_path):
    """POST /api/v1/update/file 应触发单文件更新。"""
    file_path = _make_unique_file(tmp_path, "api_single.md")

    response = update_client.post(
        "/api/v1/update/file",
        json={"file_path": str(file_path)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scanned"] == 1
    assert body["added"] == 1
    assert body["failed"] == 0


def test_api_status_endpoint(update_client, tmp_path):
    """GET /api/v1/update/status 应返回最近一次更新结果。"""
    # 先触发一次更新
    _make_unique_file(tmp_path, "api_status.md")
    update_client.post(
        "/api/v1/update/full",
        json={"dir_path": str(tmp_path)},
    )

    # 查询状态
    response = update_client.get("/api/v1/update/status")
    assert response.status_code == 200
    body = response.json()
    assert body["last_update"] is not None
    assert body["last_update"]["mode"] == "full"
    assert body["message"]  # 应有非空提示


def test_api_status_empty_initially(update_client):
    """未执行更新时 GET /status 应返回 last_update 为空。"""
    response = update_client.get("/api/v1/update/status")
    assert response.status_code == 200
    body = response.json()
    assert body["last_update"] is None
    assert "尚未执行" in body["message"]
