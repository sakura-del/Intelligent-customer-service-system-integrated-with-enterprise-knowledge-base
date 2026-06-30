"""RAG 端到端测试。

验证 KnowledgeRetriever → RAGAgent → LLMClient 完整链路：
1. 测试文档入库（faq / product_manual / return_policy）
2. 对标注问答对调用 rag_agent.answer
3. 按 embedding 模式调整断言策略：
   - BGE 模式：严格断言 hit=True 且来源/关键词命中，准确率 ≥ 70%
   - fallback 模式：放宽为流程完整性（hit=True 且答案含 chunk 文本），
     因 hash 向量无语义检索能力，仅验证链路可跑通
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

import pytest

# 测试用独立持久化目录，避免污染正式环境与其他测试模块
TEST_PERSIST_DIR = "./tests/_chroma_data_rag"
SAMPLE_DIR = Path(__file__).parent / "sample_data"
SAMPLE_FAQ = SAMPLE_DIR / "faq.md"
SAMPLE_MANUAL = SAMPLE_DIR / "product_manual.md"
SAMPLE_POLICY = SAMPLE_DIR / "return_policy.md"

# 准确率门槛：BGE 模式下必须达到，fallback 模式下作为流程命中率的参考
ACCURACY_THRESHOLD = 0.7


# 标注问答对：question + 期望来源（任一命中即可）+ 期望关键词（任一命中即可）
# 覆盖三个测试文档，验证多文档混合检索能力
QA_PAIRS: List[dict] = [
    {
        "question": "忘记登录密码怎么办？",
        "expected_sources": ["faq.md"],
        "expected_keywords": ["忘记密码", "重置链接", "邮箱"],
    },
    {
        "question": "订单支付失败但资金已扣除如何处理？",
        "expected_sources": ["faq.md"],
        "expected_keywords": ["银行", "退款", "工单"],
    },
    {
        "question": "七天无理由退货的范围是什么？",
        "expected_sources": ["faq.md", "return_policy.md"],
        "expected_keywords": ["原包装", "二次销售", "定制商品"],
    },
    {
        "question": "退货运费由谁承担？",
        "expected_sources": ["faq.md", "return_policy.md"],
        "expected_keywords": ["质量问题", "商家承担", "买家承担"],
    },
    {
        "question": "会员等级如何升级？",
        "expected_sources": ["faq.md"],
        "expected_keywords": ["银卡", "金卡", "钻石卡", "消费金额"],
    },
    {
        "question": "积分有效期是多久？",
        "expected_sources": ["faq.md"],
        "expected_keywords": ["12 个月", "清零", "积分"],
    },
    {
        "question": "智能客服系统支持哪些接入渠道？",
        "expected_sources": ["product_manual.md"],
        "expected_keywords": ["Web", "App", "微信", "钉钉"],
    },
    {
        "question": "知识库支持哪些文档格式？",
        "expected_sources": ["product_manual.md"],
        "expected_keywords": ["Markdown", "PDF", "Word", "HTML"],
    },
    {
        "question": "换货期限是多久？",
        "expected_sources": ["return_policy.md"],
        "expected_keywords": ["7 天", "同款", "规格"],
    },
    {
        "question": "退款多久能到账？",
        "expected_sources": ["return_policy.md"],
        "expected_keywords": ["支付宝", "银行卡", "工作日"],
    },
]


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_ingest():
    """模块级 fixture：隔离 ChromaDB 目录并入库三份测试文档。

    使用 module 作用域避免每个用例都重置单例；
    入库后所有用例共享同一向量库，减少重复 IO 与向量化开销。
    """
    from app.core.config import get_settings
    from app.knowledge import embeddings as embeddings_module
    from app.knowledge import retriever as retriever_module
    from app.knowledge import vectorstore as vectorstore_module
    from app.knowledge.pipeline import ingest_document

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_threshold = settings.SIMILARITY_THRESHOLD
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次测试残留，保证入库从零开始，避免重复入库全部判重
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # fallback 模式下 hash 向量无语义能力，阈值降到 0 让 top_k 全返回，
    # 保证链路可跑通；BGE 模式下保持 0.6 做真实语义过滤
    embedding_service = embeddings_module.get_embedding_service()
    if embedding_service.mode == "fallback":
        settings.SIMILARITY_THRESHOLD = 0.0
    else:
        settings.SIMILARITY_THRESHOLD = 0.6

    # 重置单例让新配置生效
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    # embeddings 单例不重置：模型加载昂贵，复用即可

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

    # 恢复原始配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.SIMILARITY_THRESHOLD = original_threshold
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()

    # 重置 RAG Agent 与 LLM Client 单例，避免 mock 状态泄漏到其他测试
    from app.agents import llm_client as llm_client_module
    from app.agents import rag_agent as rag_agent_module

    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()


def _is_source_hit(rag_answer, expected_sources: List[str]) -> bool:
    """判断 retrieved_chunks 是否命中任一期望来源文件。"""
    for chunk in rag_answer.retrieved_chunks:
        if chunk.source in expected_sources:
            return True
    return False


def _is_keyword_hit(answer: str, expected_keywords: List[str]) -> bool:
    """判断答案是否包含任一期望关键词。"""
    return any(keyword in answer for keyword in expected_keywords)


def test_retriever_returns_chunks_for_faq_question():
    """检索器对 FAQ 问题应返回非空结果。"""
    from app.knowledge.retriever import get_retriever

    retriever = get_retriever()
    chunks = retriever.retrieve("忘记密码怎么办？", top_k=3)
    assert len(chunks) > 0, "FAQ 问题检索应命中至少 1 条"
    # 每条结果应包含必要字段
    for chunk in chunks:
        assert chunk.text
        assert chunk.source
        assert chunk.score >= 0.0


def test_rag_agent_returns_hit_for_known_question():
    """RAGAgent 对知识库覆盖的问题应返回 hit=True。"""
    from app.agents.rag_agent import get_rag_agent

    agent = get_rag_agent()
    rag_answer = agent.answer(question="忘记登录密码怎么办？")
    assert rag_answer.hit is True
    assert rag_answer.retrieved_chunks
    assert rag_answer.confidence > 0.0
    # mock 模式下答案应包含 top-1 chunk 文本；真实 LLM 下答案应非空
    assert rag_answer.answer


def test_rag_agent_returns_miss_for_empty_question():
    """空问题应返回 hit=False 与固定兜底文案。"""
    from app.agents.rag_agent import get_rag_agent

    agent = get_rag_agent()
    rag_answer = agent.answer(question="")
    assert rag_answer.hit is False
    assert rag_answer.confidence == 0.0
    assert rag_answer.answer


def test_rag_accuracy_meets_threshold():
    """对标注问答对计算准确率，断言 ≥ 70%。

    BGE 模式：严格断言 hit=True 且 (来源命中 OR 关键词命中)。
    fallback 模式：放宽为 hit=True 且答案包含 retrieved_chunks[0].text 子串，
    因 hash 向量无语义能力，仅验证 mock LLM 拼接的端到端流程。
    """
    from app.agents.llm_client import get_llm_client
    from app.agents.rag_agent import get_rag_agent
    from app.knowledge.embeddings import get_embedding_service

    agent = get_rag_agent()
    llm_client = get_llm_client()
    embedding_mode = get_embedding_service().mode

    hits = 0
    details: List[str] = []
    for pair in QA_PAIRS:
        rag_answer = agent.answer(question=pair["question"])

        if embedding_mode == "bge":
            # BGE 模式：来源或关键词命中才算正确
            source_hit = _is_source_hit(rag_answer, pair["expected_sources"])
            keyword_hit = _is_keyword_hit(
                rag_answer.answer, pair["expected_keywords"]
            )
            correct = rag_answer.hit and (source_hit or keyword_hit)
        else:
            # fallback 模式：hash 向量无语义，放宽为流程完整性
            # 检索非空 + mock LLM 把 top-1 chunk 拼进答案，即视为流程跑通
            top_chunk_text = (
                rag_answer.retrieved_chunks[0].text
                if rag_answer.retrieved_chunks
                else ""
            )
            correct = (
                rag_answer.hit
                and bool(top_chunk_text)
                and top_chunk_text[:50] in rag_answer.answer
            )

        if correct:
            hits += 1
        details.append(
            f"  [{'✓' if correct else '✗'}] Q={pair['question'][:30]} "
            f"hit={rag_answer.hit} score="
            f"{rag_answer.retrieved_chunks[0].score if rag_answer.retrieved_chunks else 0:.2f}"
        )

    accuracy = hits / len(QA_PAIRS)
    print("\n========== RAG 准确率报告 ==========")
    print(f"Embedding 模式：{embedding_mode}")
    print(f"LLM 模式：{'mock' if llm_client.is_mock else 'real'}")
    print(f"准确率：{hits}/{len(QA_PAIRS)} = {accuracy:.2%}")
    print(f"门槛：{ACCURACY_THRESHOLD:.0%}")
    print("\n".join(details))
    print("=====================================")

    # BGE 模式严格断言；fallback 模式仅作流程验证，准确率应接近 100%
    if embedding_mode == "bge":
        assert accuracy >= ACCURACY_THRESHOLD, (
            f"BGE 模式准确率 {accuracy:.2%} 低于门槛 {ACCURACY_THRESHOLD:.0%}"
        )
    else:
        # fallback 模式下准确率应至少 70%（流程应稳定跑通）
        assert accuracy >= ACCURACY_THRESHOLD, (
            f"fallback 模式流程命中率 {accuracy:.2%} 低于门槛，"
            "请检查 RAG 链路是否完整"
        )
