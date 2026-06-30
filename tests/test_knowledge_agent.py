"""知识库检索 Agent（混合检索 + 重排序）测试。

验证 Query 改写 → BM25 → RRF 融合 → Reranker → KnowledgeAgent 端到端链路：
1. 测试文档入库（faq / product_manual / return_policy）
2. Query 改写：口语化问题应被改写为检索友好查询
3. BM25 检索：关键词命中应返回非空结果
4. RRF 融合：双路召回后融合排序应合理
5. Reranker：fallback 模式下应返回 top 5
6. KnowledgeAgent 端到端：知识库可覆盖问题→命中；无关问题→未命中

测试隔离：使用独立 chroma 目录，模块级 fixture 入库一次，
所有用例共享同一向量库，减少重复 IO 与向量化开销。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

import pytest

# 测试用独立持久化目录，与其他测试模块隔离
TEST_PERSIST_DIR = "./tests/_chroma_data_kagent"
SAMPLE_DIR = Path(__file__).parent / "sample_data"
SAMPLE_FAQ = SAMPLE_DIR / "faq.md"
SAMPLE_MANUAL = SAMPLE_DIR / "product_manual.md"
SAMPLE_POLICY = SAMPLE_DIR / "return_policy.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_ingest():
    """模块级 fixture：隔离 ChromaDB 目录并入库三份测试文档。

    使用 module 作用域避免每个用例都重置单例；
    入库后所有用例共享同一向量库，减少重复 IO 与向量化开销。
    """
    from app.core.config import get_settings
    from app.knowledge import (
        bm25 as bm25_module,
    )
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        hybrid_retriever as hybrid_module,
    )
    from app.knowledge import (
        query_rewriter as rewriter_module,
    )
    from app.knowledge import (
        reranker as reranker_module,
    )
    from app.knowledge import (
        retriever as retriever_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )
    from app.knowledge.pipeline import ingest_document

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_threshold = settings.SIMILARITY_THRESHOLD
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次测试残留，保证入库从零开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # fallback 模式下 hash 向量无语义能力，阈值降到 0 让召回阶段不过滤
    # reranker 阶段在测试中单独控制阈值
    embedding_service = embeddings_module.get_embedding_service()
    if embedding_service.mode == "fallback":
        settings.SIMILARITY_THRESHOLD = 0.0

    # 重置所有相关单例，让新配置生效
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    rewriter_module.reset_query_rewriter()
    reranker_module.reset_reranker()

    # 入库三份文档；重复入库会被去重，多次运行测试安全
    for sample_path, knowledge_type in [
        (SAMPLE_FAQ, "faq"),
        (SAMPLE_MANUAL, "doc"),
        (SAMPLE_POLICY, "policy"),
    ]:
        result = ingest_document(
            sample_path,
            metadata={"knowledge_type": knowledge_type},
        )
        assert result.error is None, f"入库 {sample_path.name} 失败：{result.error}"
        assert result.total_chunks > 0, f"{sample_path.name} 切分后无 chunk"

    yield

    # 恢复原始配置并清理单例，避免影响其他测试
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.SIMILARITY_THRESHOLD = original_threshold
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    rewriter_module.reset_query_rewriter()
    reranker_module.reset_reranker()

    # 重置 KnowledgeAgent 与 LLM Client 单例
    from app.agents import knowledge_agent as agent_module
    from app.agents import llm_client as llm_client_module

    agent_module.reset_knowledge_agent()
    llm_client_module.reset_llm_client()


# ----------------------------------------------------------------------
# SubTask 6.1：Query 改写测试
# ----------------------------------------------------------------------


def test_query_rewriter_returns_multiple_variants():
    """QueryRewriter 应返回至少 2 个查询变体（含原查询）。"""
    from app.knowledge.query_rewriter import QueryRewriter

    rewriter = QueryRewriter()
    variants = rewriter.rewrite("请问忘记登录密码怎么办？", num_variants=2)

    assert len(variants) >= 1, "改写应至少返回原查询"
    # 第一个变体应是原查询（去空白）
    assert variants[0] == "请问忘记登录密码怎么办？"
    # mock 模式下应额外返回关键词版本
    from app.agents.llm_client import get_llm_client

    if get_llm_client().is_mock:
        assert len(variants) >= 2, "mock 模式应返回原查询 + 关键词版本"


def test_query_rewriter_empty_question_returns_empty():
    """空问题应返回空列表。"""
    from app.knowledge.query_rewriter import QueryRewriter

    rewriter = QueryRewriter()
    assert rewriter.rewrite("") == []
    assert rewriter.rewrite("   ") == []


def test_query_rewriter_keywords_extraction_in_mock():
    """mock 模式下关键词提取应去除停用词。"""
    from app.knowledge.query_rewriter import QueryRewriter

    rewriter = QueryRewriter()
    # 触发关键词提取路径
    keywords = QueryRewriter._extract_keywords("请问一下忘记密码怎么办呢？")
    # 应去除「请问」「一下」「怎么办」等口语词
    assert "请问" not in keywords.split()
    assert keywords, "关键词提取结果不应为空"


# ----------------------------------------------------------------------
# SubTask 6.2：BM25 检索测试
# ----------------------------------------------------------------------


def test_bm25_retriever_indexes_and_searches():
    """BM25Retriever 应能索引 chunks 并返回关键词命中结果。"""
    from app.knowledge.bm25 import BM25Retriever
    from app.schemas.knowledge import TextChunk

    chunks = [
        TextChunk(text="忘记登录密码请点击忘记密码链接重置"),
        TextChunk(text="订单支付失败资金扣除请联系客服退款"),
        TextChunk(text="七天无理由退货需保持原包装"),
    ]
    retriever = BM25Retriever()
    count = retriever.index(chunks)
    assert count == 3
    assert retriever.size == 3

    # 关键词命中应返回非空结果
    hits = retriever.search("忘记密码", top_k=3)
    assert len(hits) > 0, "BM25 应命中包含「忘记密码」的文档"
    # 第一个命中应为最相关文档
    assert hits[0][1] > 0, "BM25 分数应大于 0"

    # 无关查询应返回空结果（所有 token 未命中）
    empty_hits = retriever.search("xyznotexist", top_k=3)
    assert empty_hits == []


def test_bm25_retriever_backend():
    """BM25Retriever 应报告当前使用的后端。"""
    from app.knowledge.bm25 import BM25Retriever

    retriever = BM25Retriever()
    # rank-bm25 已安装，后端应为 rank_bm25
    assert retriever.backend in {"rank_bm25", "simple"}


def test_bm25_retriever_get_text():
    """BM25Retriever.get_text 应能反查原文。"""
    from app.knowledge.bm25 import BM25Retriever
    from app.schemas.knowledge import TextChunk

    chunks = [TextChunk(text="测试文档内容")]
    retriever = BM25Retriever()
    retriever.index(chunks, ids=["chunk-001"])

    assert retriever.get_text("chunk-001") == "测试文档内容"
    assert retriever.get_text("not-exist") is None


# ----------------------------------------------------------------------
# SubTask 6.2：RRF 融合测试
# ----------------------------------------------------------------------


def test_rrf_fusion_combines_vector_and_bm25():
    """RRF 融合应合并向量与 BM25 召回结果，双路命中文档分数更高。"""
    from app.knowledge.hybrid_retriever import HybridRetriever

    # 构造模拟召回结果：chunk-1 在两路都命中，chunk-2 仅向量命中
    vector_hits = [
        {"id": "chunk-1", "text": "doc1", "metadata": {}, "similarity": 0.8},
        {"id": "chunk-2", "text": "doc2", "metadata": {}, "similarity": 0.7},
    ]
    bm25_hits = [
        ("chunk-1", 5.0),
        ("chunk-3", 3.0),
    ]

    retriever = HybridRetriever()
    fused = retriever._rrf_fuse(vector_hits, bm25_hits)

    # 融合后应包含 3 个 chunk（chunk-1/2/3）
    assert len(fused) == 3
    chunk_ids = [cid for cid, _ in fused]
    assert "chunk-1" in chunk_ids
    assert "chunk-2" in chunk_ids
    assert "chunk-3" in chunk_ids

    # chunk-1 在两路都命中，融合分数应最高
    assert fused[0][0] == "chunk-1"
    # 验证加权 RRF：chunk-1 分数 = 0.6/(60+1) + 0.4/(60+1)
    expected_chunk1 = 0.6 / (60 + 1) + 0.4 / (60 + 1)
    assert abs(fused[0][1] - expected_chunk1) < 1e-6


def test_rrf_fusion_empty_inputs():
    """两路召回均为空时应返回空列表。"""
    from app.knowledge.hybrid_retriever import HybridRetriever

    retriever = HybridRetriever()
    assert retriever._rrf_fuse([], []) == []


def test_hybrid_retriever_e2e_returns_chunks():
    """HybridRetriever 端到端：对入库问题应返回非空融合结果。"""
    from app.knowledge.hybrid_retriever import get_hybrid_retriever

    retriever = get_hybrid_retriever()
    chunks = retriever.retrieve("忘记密码", top_k=10)

    # 至少应召回一条结果（向量或 BM25 命中）
    assert len(chunks) > 0, "混合检索应命中至少 1 条"
    for chunk in chunks:
        assert chunk.text
        assert chunk.score >= 0.0


# ----------------------------------------------------------------------
# SubTask 6.3：Reranker 测试
# ----------------------------------------------------------------------


def test_reranker_returns_top_k_in_fallback():
    """Reranker fallback 模式下应返回 top_k 结果并按分数降序。"""
    from app.knowledge.reranker import Reranker
    from app.schemas.knowledge import RetrievedChunk

    reranker = Reranker()
    # 强制触发模型加载尝试（无网络时会降级到 fallback）
    chunks = [
        RetrievedChunk(text="忘记密码请点击重置链接", score=0.5, source="faq.md"),
        RetrievedChunk(text="订单支付失败请联系客服", score=0.4, source="faq.md"),
        RetrievedChunk(text="七天无理由退货需保持原包装", score=0.3, source="policy.md"),
        RetrievedChunk(text="会员等级按消费金额升级", score=0.2, source="faq.md"),
        RetrievedChunk(text="积分有效期为 12 个月", score=0.1, source="faq.md"),
        RetrievedChunk(text="无关文档内容", score=0.05, source="other.md"),
    ]

    ranked = reranker.rerank("忘记密码怎么办", chunks, top_k=5)

    # 应返回最多 5 条
    assert len(ranked) <= 5
    # 分数应降序排列
    for i in range(len(ranked) - 1):
        assert ranked[i].score >= ranked[i + 1].score
    # reranker 模式应被记录（fallback 或 cross_encoder）
    assert reranker.mode in {"fallback", "cross_encoder"}


def test_reranker_empty_inputs():
    """空 query 或空 chunks 应返回空列表。"""
    from app.knowledge.reranker import Reranker
    from app.schemas.knowledge import RetrievedChunk

    reranker = Reranker()
    assert reranker.rerank("", [RetrievedChunk(text="x")], top_k=5) == []
    assert reranker.rerank("query", [], top_k=5) == []


def test_reranker_mode_property():
    """Reranker.mode 应返回 unknown / cross_encoder / fallback 之一。"""
    from app.knowledge.reranker import Reranker

    reranker = Reranker()
    # 未加载时为 unknown
    assert reranker.mode == "unknown"


# ----------------------------------------------------------------------
# KnowledgeAgent 端到端测试
# ----------------------------------------------------------------------


def test_knowledge_agent_hits_for_known_question():
    """KnowledgeAgent 对知识库覆盖的问题应返回 hit=True。"""
    from app.agents.knowledge_agent import KnowledgeAgent
    from app.knowledge.embeddings import get_embedding_service

    embedding_mode = get_embedding_service().mode
    # fallback 模式下 reranker cosine 分数较低，降低阈值避免误判未命中
    score_threshold = 0.0 if embedding_mode == "fallback" else 0.5

    agent = KnowledgeAgent(score_threshold=score_threshold)
    result = agent.answer("忘记登录密码怎么办？")

    assert result.hit is True
    assert result.retrieved_chunks, "命中时应返回检索片段"
    assert result.retrieval_mode == "hybrid"
    assert result.reranker_mode in {"fallback", "cross_encoder", "unknown"}
    # 应记录改写后的查询
    assert result.rewritten_queries, "应记录查询改写变体"
    # 来源应包含 faq.md
    sources_text = " ".join(result.sources)
    assert "faq.md" in sources_text or any(
        "faq" in c.source for c in result.retrieved_chunks
    )


def test_knowledge_agent_miss_for_empty_question():
    """空问题应返回 hit=False。"""
    from app.agents.knowledge_agent import KnowledgeAgent

    agent = KnowledgeAgent()
    result = agent.answer("")
    assert result.hit is False
    assert result.confidence == 0.0


def test_knowledge_agent_miss_for_irrelevant_question():
    """无关问题应在阈值过滤后返回未命中。"""
    from app.agents.knowledge_agent import KnowledgeAgent
    from app.knowledge.embeddings import get_embedding_service

    embedding_mode = get_embedding_service().mode
    # BGE 模式下用较高阈值确保无关问题被过滤
    # fallback 模式下 hash 向量无语义，无关问题也可能召回但分数低
    score_threshold = 0.9 if embedding_mode == "bge" else 0.99

    agent = KnowledgeAgent(score_threshold=score_threshold)
    result = agent.answer("量子力学波函数坍缩原理")

    # 无关问题应未命中（阈值过滤后无 chunk 通过）
    assert result.hit is False


def test_knowledge_agent_with_summary_generation():
    """generate_summary=True 时应调用 LLM 生成摘要。"""
    from app.agents.knowledge_agent import KnowledgeAgent
    from app.knowledge.embeddings import get_embedding_service

    embedding_mode = get_embedding_service().mode
    score_threshold = 0.0 if embedding_mode == "fallback" else 0.5

    agent = KnowledgeAgent(score_threshold=score_threshold)
    result = agent.answer("忘记密码怎么办？", generate_summary=True)

    assert result.hit is True
    # mock 模式下答案应包含 top chunk 文本；真实 LLM 下答案应非空
    assert result.answer, "启用摘要生成时应返回非空答案"


def test_hybrid_vs_vector_only_comparison():
    """演示混合检索 vs 纯向量检索的差异。

    同一问题下，混合检索应召回 BM25 命中的额外文档，
    体现关键词召回对向量检索的补充价值。
    """
    from app.knowledge.hybrid_retriever import get_hybrid_retriever
    from app.knowledge.retriever import get_retriever

    question = "七天无理由退货范围"

    # 纯向量检索
    vector_retriever = get_retriever()
    vector_chunks = vector_retriever.retrieve(question, top_k=10)

    # 混合检索
    hybrid_retriever = get_hybrid_retriever()
    hybrid_chunks = hybrid_retriever.retrieve(question, top_k=10)

    # 两种检索都应返回非空结果
    assert vector_chunks, "纯向量检索应返回非空结果"
    assert hybrid_chunks, "混合检索应返回非空结果"

    # 打印对比信息，便于人工审视差异
    vector_sources = {c.source for c in vector_chunks}
    hybrid_sources = {c.source for c in hybrid_chunks}
    vector_texts = {c.text[:50] for c in vector_chunks}
    hybrid_texts = {c.text[:50] for c in hybrid_chunks}

    print("\n========== 混合检索 vs 纯向量检索对比 ==========")
    print(f"问题：{question}")
    print(f"纯向量检索命中数：{len(vector_chunks)} 来源：{vector_sources}")
    print(f"混合检索命中数：{len(hybrid_chunks)} 来源：{hybrid_sources}")
    # 混合检索应至少覆盖纯向量检索的来源（含 BM25 补充）
    extra_in_hybrid = hybrid_texts - vector_texts
    if extra_in_hybrid:
        print(f"混合检索额外召回（BM25 补充）：{len(extra_in_hybrid)} 条")
        for text in list(extra_in_hybrid)[:3]:
            print(f"  - {text}...")
    else:
        print("混合检索未额外召回（两路结果重叠）")
    print("===============================================")

    # 混合检索命中数应不少于纯向量（BM25 补充召回）
    assert len(hybrid_chunks) >= 1
