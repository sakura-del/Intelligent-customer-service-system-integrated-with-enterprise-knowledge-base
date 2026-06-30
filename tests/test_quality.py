"""知识质量校验测试。

覆盖 Task 16 SubTask 16.2 的三项检查：
- check_duplicates：基于 cosine 相似度的重复检测
- check_term_consistency：术语一致性（别名 -> 标准术语）
- check_sensitive_words：敏感词命中检测
- run_quality_check：聚合报告与降级策略
- run_quality_check_on_existing：库内重复巡检

测试隔离：重置 embedding 单例确保 fallback 模式可预测。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# 测试用独立持久化目录（embedding fallback 模式下不实际写入，但保持隔离一致性）
TEST_PERSIST_DIR = "./tests/_chroma_data_quality"


@pytest.fixture(scope="module", autouse=True)
def _isolate_singletons():
    """模块级 fixture：重置 embedding 与向量库单例。"""
    from app.core.config import get_settings
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()

    yield

    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()


def _make_chunk(text: str, index: int = 0):
    """构造测试用 TextChunk。"""
    from app.schemas.knowledge import TextChunk

    return TextChunk(text=text, page_number=1)


# ----------------------------------------------------------------------
# 术语一致性
# ----------------------------------------------------------------------


def test_term_consistency_detects_alias():
    """使用了非标准别名应被检测出术语不一致。"""
    from app.knowledge.quality import check_term_consistency

    # 内置默认术语表含 "客服": ["客務", ...]，使用别名 "客務" 应触发
    chunks = [_make_chunk("请联系客務人员获取帮助")]
    issues = check_term_consistency(chunks)
    assert len(issues) == 1
    assert issues[0].issue_type == "term_inconsistency"
    assert "客服" in issues[0].detail


def test_term_consistency_passes_canonical():
    """使用标准术语不应触发告警。"""
    from app.knowledge.quality import check_term_consistency

    chunks = [_make_chunk("请联系客服人员获取帮助")]
    issues = check_term_consistency(chunks)
    assert len(issues) == 0


def test_term_consistency_empty_chunks():
    """空 chunks 应返回空列表。"""
    from app.knowledge.quality import check_term_consistency

    assert check_term_consistency([]) == []


# ----------------------------------------------------------------------
# 敏感词
# ----------------------------------------------------------------------


def test_sensitive_words_detects_builtin():
    """命中内置敏感词应被检测出。"""
    from app.knowledge.quality import check_sensitive_words

    # 内置默认敏感词含 "违禁词样例"
    chunks = [_make_chunk("这里包含违禁词样例的内容")]
    issues = check_sensitive_words(chunks)
    assert len(issues) == 1
    assert issues[0].issue_type == "sensitive_word"
    assert "违禁词样例" in issues[0].detail


def test_sensitive_words_detects_configured():
    """通过 SENSITIVE_WORDS 配置的敏感词应被检测出。"""
    from app.core.config import get_settings
    from app.knowledge.quality import check_sensitive_words

    settings = get_settings()
    original = settings.SENSITIVE_WORDS
    settings.SENSITIVE_WORDS = "自定义敏感词,测试违禁"
    try:
        chunks = [_make_chunk("这段文字包含测试违禁内容")]
        issues = check_sensitive_words(chunks)
        assert any("测试违禁" in i.detail for i in issues)
    finally:
        settings.SENSITIVE_WORDS = original


def test_sensitive_words_no_hit():
    """无敏感词的文本不应触发告警。"""
    from app.knowledge.quality import check_sensitive_words

    chunks = [_make_chunk("这是一段正常的产品说明文字")]
    issues = check_sensitive_words(chunks)
    assert len(issues) == 0


# ----------------------------------------------------------------------
# 重复检测
# ----------------------------------------------------------------------


def test_duplicates_detects_similar_content():
    """与已有向量高度相似的 chunk 应被标记为重复。"""
    from app.knowledge.embeddings import get_embedding_service
    from app.knowledge.quality import check_duplicates

    embedding_service = get_embedding_service()
    # 用相同文本生成向量，相似度应为 1.0
    existing_text = "退款政策说明：购买后7天内可全额退款"
    existing_embeddings = embedding_service.embed_texts([existing_text])

    chunks = [_make_chunk(existing_text)]
    issues = check_duplicates(chunks, existing_embeddings)
    assert len(issues) == 1
    assert issues[0].issue_type == "duplicate"


def test_duplicates_skips_when_no_existing():
    """无已有向量时应跳过重复检测，返回空列表。"""
    from app.knowledge.quality import check_duplicates

    chunks = [_make_chunk("某段文本")]
    assert check_duplicates(chunks, []) == []
    assert check_duplicates([], [[0.1] * 1024]) == []


def test_internal_duplicates_finds_pairs():
    """库内重复巡检应发现相互重复的 chunk 对。"""
    from app.knowledge.embeddings import get_embedding_service
    from app.knowledge.quality import check_internal_duplicates

    embedding_service = get_embedding_service()
    # 两个完全相同的文本 + 一个不同文本
    text_a = "退货流程说明"
    text_b = "退货流程说明"
    text_c = "完全不同的内容关于产品功能"
    embeddings = embedding_service.embed_texts([text_a, text_b, text_c])
    chunks = [_make_chunk(text_a), _make_chunk(text_b), _make_chunk(text_c)]
    issues = check_internal_duplicates(chunks, embeddings)
    # text_a 与 text_b 相同，应检测出至少 1 个重复
    assert len(issues) >= 1
    assert all(i.issue_type == "duplicate" for i in issues)


# ----------------------------------------------------------------------
# 聚合报告
# ----------------------------------------------------------------------


def test_run_quality_check_aggregates_all_issues():
    """run_quality_check 应聚合术语与敏感词问题。"""
    from app.knowledge.quality import run_quality_check

    chunks = [_make_chunk("请联系客務人员，这里有违禁词样例")]
    report = run_quality_check(chunks, existing_embeddings=None)
    assert report.total_chunks == 1
    # 无 existing_embeddings 时跳过去重
    assert len(report.duplicate_issues) == 0
    # 应检测出术语不一致
    assert len(report.term_issues) >= 1
    # 应检测出敏感词
    assert len(report.sensitive_issues) >= 1
    assert report.error is None
    assert "敏感词" in report.summary or "术语" in report.summary


def test_run_quality_check_empty_chunks():
    """空 chunks 应返回空报告。"""
    from app.knowledge.quality import run_quality_check

    report = run_quality_check([])
    assert report.total_chunks == 0
    assert "无 chunk" in report.summary


def test_run_quality_check_alert_ratio():
    """重复率超阈值时 summary 应包含告警。"""
    from app.core.config import get_settings
    from app.knowledge.embeddings import get_embedding_service
    from app.knowledge.quality import run_quality_check

    embedding_service = get_embedding_service()
    text = "完全相同的重复内容用于测试告警"
    existing_embeddings = embedding_service.embed_texts([text])

    # 5 个相同 chunk，重复率 100% 超过默认告警阈值 20%
    chunks = [_make_chunk(text) for _ in range(5)]
    report = run_quality_check(chunks, existing_embeddings=existing_embeddings)
    assert len(report.duplicate_issues) == 5
    settings = get_settings()
    # summary 应包含"重复率"告警
    assert "重复率" in report.summary or "超过告警阈值" in report.summary


def test_run_quality_check_on_existing():
    """run_quality_check_on_existing 应对已入库内容做内部重复检测。"""
    from app.knowledge.embeddings import get_embedding_service
    from app.knowledge.quality import run_quality_check_on_existing

    embedding_service = get_embedding_service()
    text_dup = "重复的退货说明文本"
    text_unique = "独特的功能介绍内容"
    embeddings = embedding_service.embed_texts([text_dup, text_dup, text_unique])
    chunks = [_make_chunk(text_dup), _make_chunk(text_dup), _make_chunk(text_unique)]
    report = run_quality_check_on_existing(chunks, embeddings)
    assert report.total_chunks == 3
    assert len(report.duplicate_issues) >= 1
