"""混合检索并行化测试。

覆盖 HybridRetriever.retrieve 的并行召回链路：
1. 双路正常时返回 RRF 融合后的正确结果（与顺序执行一致）
2. 向量路失败时仅用 BM25 结果降级
3. BM25 路失败时仅用向量结果降级
4. 两路都失败时返回空列表
5. 单路超时时使用已完成的结果降级
6. 空问题与双路空结果的边界场景

测试通过 mock _vector_retrieve / _bm25_retrieve / _ensure_bm25_index
避免依赖真实向量库与 BM25 索引，保证用例快速且可重复。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from app.knowledge.hybrid_retriever import HybridRetriever


def _make_vector_hits(
    items: List[Tuple[str, float, str]],
) -> List[Dict[str, Any]]:
    """构造向量召回结果列表。

    items: [(chunk_id, similarity, text)]
    """
    return [
        {
            "id": chunk_id,
            "similarity": sim,
            "text": text,
            "metadata": {
                "source": f"{chunk_id}.md",
                "page_number": 1,
                "section": "",
                "knowledge_type": "doc",
            },
        }
        for chunk_id, sim, text in items
    ]


def _make_bm25_hits(items: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """构造 BM25 召回结果列表，items: [(chunk_id, score)]。"""
    return list(items)


@pytest.fixture()
def retriever() -> HybridRetriever:
    """构造并行超时为 2s 的 HybridRetriever 实例。

    直接注入 mock 的 bm25_retriever 避免触发真实单例初始化；
    bm25_retriever 是只读 property，无法用 patch.object 替换，
    因此通过设置底层 _bm25_retriever 字段实现注入。
    """
    inst = HybridRetriever(parallel_timeout=2.0)
    # 注入 mock BM25 检索器：仅 BM25 命中场景需要 get_text 反查文本
    inst._bm25_retriever = MagicMock()
    return inst


def test_parallel_retrieve_returns_expected_results(retriever: HybridRetriever):
    """双路正常并行召回：应返回 RRF 融合后的正确结果。

    验证：
    - 返回结果非空，且包含两路并集去重后的所有 chunk_id
    - 两路同时命中的 chunk_id（chunk_b）RRF 分数最高，应排在最前
    - 所有结果 score > 0
    """
    vector_hits = _make_vector_hits(
        [
            ("chunk_a", 0.9, "向量命中A"),
            ("chunk_b", 0.7, "向量命中B"),
            ("chunk_c", 0.5, "向量命中C"),
        ]
    )
    bm25_hits = _make_bm25_hits([("chunk_b", 3.5), ("chunk_d", 2.0)])

    # 仅 BM25 命中的 chunk_d 需要通过 get_text 反查文本
    retriever._bm25_retriever.get_text.return_value = "BM25命中D"

    with patch.object(retriever, "_vector_retrieve", return_value=vector_hits), \
            patch.object(retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(retriever, "_bm25_retrieve", return_value=bm25_hits):
        results = retriever.retrieve("测试问题", top_k=10)

    # 融合后应包含 4 个不同 chunk_id
    assert len(results) == 4
    # 两路同时命中的 chunk_b 应排在最前（RRF 双路加权后分数最高）
    assert results[0].text == "向量命中B"
    # 所有结果 score > 0
    for chunk in results:
        assert chunk.score > 0.0


def test_parallel_retrieve_consistent_with_sequential(
    retriever: HybridRetriever,
):
    """并行执行结果应稳定可重复：RRF 是确定性算法，两次调用结果应完全一致。"""
    vector_hits = _make_vector_hits([("a", 0.8, "A"), ("b", 0.6, "B")])
    bm25_hits = _make_bm25_hits([("b", 2.0), ("c", 1.0)])

    retriever._bm25_retriever.get_text.return_value = "C"

    with patch.object(retriever, "_vector_retrieve", return_value=vector_hits), \
            patch.object(retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(retriever, "_bm25_retrieve", return_value=bm25_hits):
        results1 = retriever.retrieve("测试", top_k=10)
        results2 = retriever.retrieve("测试", top_k=10)

    assert len(results1) == len(results2)
    for r1, r2 in zip(results1, results2):
        assert r1.text == r2.text
        assert r1.score == r2.score


def test_vector_failure_degrades_to_bm25_only(retriever: HybridRetriever):
    """向量召回失败时：仅使用 BM25 召回结果降级返回。"""

    def boom_vector(q: str, where: Any) -> List[Dict[str, Any]]:
        raise RuntimeError("向量服务不可用")

    bm25_hits = _make_bm25_hits([("chunk_x", 3.0), ("chunk_y", 1.5)])

    # 仅 BM25 命中场景需要 get_text 反查文本
    retriever._bm25_retriever.get_text.side_effect = lambda cid: f"BM25文本_{cid}"

    with patch.object(retriever, "_vector_retrieve", side_effect=boom_vector), \
            patch.object(retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(retriever, "_bm25_retrieve", return_value=bm25_hits):
        results = retriever.retrieve("测试", top_k=10)

    assert len(results) == 2
    # 仅 BM25 命中时 source 为空字符串（无向量元数据）
    assert all(chunk.source == "" for chunk in results)
    # 文本应来自 BM25 反查，分数最高的 chunk_x 排第一
    assert results[0].text == "BM25文本_chunk_x"


def test_bm25_failure_degrades_to_vector_only(retriever: HybridRetriever):
    """BM25 召回失败时：仅使用向量召回结果降级返回。"""

    def boom_bm25(q: str) -> List[Tuple[str, float]]:
        raise RuntimeError("BM25 索引未就绪")

    vector_hits = _make_vector_hits(
        [("chunk_a", 0.9, "向量A"), ("chunk_b", 0.6, "向量B")]
    )

    with patch.object(retriever, "_vector_retrieve", return_value=vector_hits), \
            patch.object(retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(retriever, "_bm25_retrieve", side_effect=boom_bm25):
        results = retriever.retrieve("测试", top_k=10)

    assert len(results) == 2
    # 仅向量命中时 source 来自向量元数据
    assert results[0].source == "chunk_a.md"
    assert results[0].text == "向量A"


def test_both_failures_returns_empty(retriever: HybridRetriever):
    """两路召回都失败时：应返回空列表。"""

    def boom_vector(q: str, where: Any) -> List[Dict[str, Any]]:
        raise RuntimeError("向量服务不可用")

    def boom_bm25(q: str) -> List[Tuple[str, float]]:
        raise RuntimeError("BM25 索引未就绪")

    with patch.object(retriever, "_vector_retrieve", side_effect=boom_vector), \
            patch.object(retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(retriever, "_bm25_retrieve", side_effect=boom_bm25):
        results = retriever.retrieve("测试", top_k=10)

    assert results == []


def test_timeout_degrades_to_completed_results():
    """超时降级：向量路慢、BM25 路快，超时后应使用已完成的 BM25 结果。

    用极短超时（0.3s）+ 向量路 sleep 1s 模拟慢召回，
    验证 BM25 路结果在超时后被正确采用。
    注意：ThreadPoolExecutor with 退出时会等待慢 worker 完成，
    所以总耗时会略大于 1s；本用例只断言降级结果正确，不断言总耗时。
    """
    slow_retriever = HybridRetriever(parallel_timeout=0.3)
    # 注入 mock BM25 检索器（property 只读，直接设底层字段）
    slow_retriever._bm25_retriever = MagicMock()

    vector_hits = _make_vector_hits([("slow", 0.9, "慢路结果")])
    bm25_hits = _make_bm25_hits([("fast", 2.0)])

    def slow_vector(q: str, where: Any) -> List[Dict[str, Any]]:
        # 故意慢于 0.3s 超时，触发超时降级路径
        time.sleep(1.0)
        return vector_hits

    slow_retriever._bm25_retriever.get_text.return_value = "快路BM25结果"

    with patch.object(slow_retriever, "_vector_retrieve", side_effect=slow_vector), \
            patch.object(slow_retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(slow_retriever, "_bm25_retrieve", return_value=bm25_hits):
        start = time.monotonic()
        results = slow_retriever.retrieve("测试", top_k=10)
        elapsed = time.monotonic() - start

    # 验证：超时降级路径生效，返回的是 BM25 结果（向量路被超时跳过）
    assert len(results) == 1
    assert results[0].text == "快路BM25结果"
    assert results[0].source == ""  # 仅 BM25 命中，无向量元数据
    # 总耗时应大于超时阈值（0.3s），证明超时机制确实触发
    assert elapsed > 0.3


def test_empty_question_returns_empty(retriever: HybridRetriever):
    """空问题应返回空列表，且不触发任何召回。"""
    with patch.object(retriever, "_vector_retrieve") as mock_vec, \
            patch.object(retriever, "_bm25_retrieve") as mock_bm25:
        assert retriever.retrieve("", top_k=10) == []
        assert retriever.retrieve("   ", top_k=10) == []
        mock_vec.assert_not_called()
        mock_bm25.assert_not_called()


def test_both_empty_returns_empty(retriever: HybridRetriever):
    """两路召回都返回空列表时：应返回空列表，不进入融合阶段。"""
    with patch.object(retriever, "_vector_retrieve", return_value=[]), \
            patch.object(retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(retriever, "_bm25_retrieve", return_value=[]):
        results = retriever.retrieve("测试", top_k=10)

    assert results == []


def test_parallel_does_not_call_real_index(retriever: HybridRetriever):
    """并行执行时不应在主线程调用 _ensure_bm25_index，应交给 BM25 worker。

    验证 _ensure_bm25_index 仅被调用一次（在 BM25 worker 中），
    避免主线程与 worker 重复构建索引产生竞争。
    """
    vector_hits = _make_vector_hits([("a", 0.8, "A")])
    bm25_hits = _make_bm25_hits([("a", 2.0)])

    with patch.object(retriever, "_vector_retrieve", return_value=vector_hits), \
            patch.object(
                retriever, "_ensure_bm25_index", return_value=None
            ) as mock_ensure, \
            patch.object(retriever, "_bm25_retrieve", return_value=bm25_hits):
        retriever.retrieve("测试", top_k=10)

    # 索引构建只应被调用一次（在 BM25 worker 内）
    mock_ensure.assert_called_once()


def test_score_threshold_filters_results(retriever: HybridRetriever):
    """score_threshold 应过滤掉 RRF 分数低于阈值的结果。"""
    # 构造两路都命中的 chunk（RRF 分数高）与仅向量命中的 chunk（分数低）
    vector_hits = _make_vector_hits(
        [("both", 0.9, "双路命中"), ("vec_only", 0.5, "仅向量")]
    )
    bm25_hits = _make_bm25_hits([("both", 3.0)])

    with patch.object(retriever, "_vector_retrieve", return_value=vector_hits), \
            patch.object(retriever, "_ensure_bm25_index", return_value=None), \
            patch.object(retriever, "_bm25_retrieve", return_value=bm25_hits):
        # 先调用一次拿到实际分数，再用中间值作为阈值
        all_results = retriever.retrieve("测试", top_k=10)
        # 双路命中的分数应高于仅向量命中
        both_score = next(c.score for c in all_results if c.text == "双路命中")
        vec_only_score = next(c.score for c in all_results if c.text == "仅向量")
        threshold = (both_score + vec_only_score) / 2

        filtered = retriever.retrieve(
            "测试", top_k=10, score_threshold=threshold
        )

    # 仅保留双路命中的结果（分数高于阈值）
    assert len(filtered) == 1
    assert filtered[0].text == "双路命中"
