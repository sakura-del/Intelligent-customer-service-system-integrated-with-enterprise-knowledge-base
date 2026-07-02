"""知识库检索 Agent（混合检索 + 重排序）。

编排流程：Query 改写 → 混合检索（向量+BM25+RRF）→ Reranker 重排序
 Top-K 阈值过滤 → 返回 KnowledgeAnswer。

对外暴露两套接口：
- answer：同步返回 KnowledgeAnswer（默认不调用 LLM 摘要）
- handle_stream：流式生成答案，先检索重排再透传 LLM token

与 RAGAgent 区别：本 Agent 聚焦检索质量，
答案生成为可选（默认不调用 LLM 摘要，仅返回检索结果），
便于上层（Task 8）按需组合生成策略。
"""
from __future__ import annotations

from typing import Any, Dict, Generator, List, Optional

from app.agents.llm_client import LLMClient, get_llm_client
from app.agents.rag_agent import RAGAgent, get_rag_agent
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.performance import get_model_router
from app.knowledge.hybrid_retriever import (
    HybridRetriever,
    get_hybrid_retriever,
)
from app.knowledge.query_rewriter import QueryRewriter, get_query_rewriter
from app.knowledge.reranker import Reranker, get_reranker
from app.schemas.knowledge import KnowledgeAnswer, RetrievedChunk

logger = get_logger("app.agents.knowledge_agent")

# RRF 分数无统一量纲，阈值过滤改用 rerank 后的归一化分数
# 默认沿用 SIMILARITY_THRESHOLD(0.6)，但仅在 rerank 分数上生效
_DEFAULT_SCORE_THRESHOLD = 0.6
# 来源展示上限：避免来源行过长
_MAX_SOURCE_COUNT = 5


class KnowledgeAgent:
    """知识库检索 Agent。

    持有 query_rewriter / hybrid_retriever / reranker / llm_client，
    通过 answer 方法串联「改写 → 混合检索 → 重排序 → 过滤」链路。
    generate_summary 控制是否调用 LLM 生成摘要（默认关闭，仅返回检索结果）。
    """

    def __init__(
        self,
        query_rewriter: Optional[QueryRewriter] = None,
        hybrid_retriever: Optional[HybridRetriever] = None,
        reranker: Optional[Reranker] = None,
        llm_client: Optional[LLMClient] = None,
        rerank_top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
        rag_agent: Optional[RAGAgent] = None,
    ) -> None:
        # 延迟取单例，便于测试注入自定义实现
        self._query_rewriter = query_rewriter
        self._hybrid_retriever = hybrid_retriever
        self._reranker = reranker
        self._llm_client = llm_client
        # 流式生成阶段复用 RAGAgent.answer_stream，注入入口便于测试隔离
        self._rag_agent = rag_agent
        settings = get_settings()
        self._rerank_top_k = rerank_top_k or settings.RERANK_TOP_K
        self._score_threshold = (
            score_threshold if score_threshold is not None else _DEFAULT_SCORE_THRESHOLD
        )

    @property
    def query_rewriter(self) -> QueryRewriter:
        if self._query_rewriter is None:
            self._query_rewriter = get_query_rewriter()
        return self._query_rewriter

    @property
    def hybrid_retriever(self) -> HybridRetriever:
        if self._hybrid_retriever is None:
            self._hybrid_retriever = get_hybrid_retriever()
        return self._hybrid_retriever

    @property
    def reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = get_reranker()
        return self._reranker

    @property
    def llm_client(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    @property
    def rag_agent(self) -> RAGAgent:
        """延迟获取 RAGAgent 单例，流式生成阶段复用其 answer_stream 方法。"""
        if self._rag_agent is None:
            self._rag_agent = get_rag_agent()
        return self._rag_agent

    def answer(
        self,
        question: str,
        session_id: Optional[str] = None,
        generate_summary: bool = False,
    ) -> KnowledgeAnswer:
        """对用户问题执行混合检索 + 重排序，返回 KnowledgeAnswer。

        流程：
        1. Query 改写：生成 2-3 个检索友好查询
        2. 混合检索：对每个改写查询做向量+BM25+RRF，合并去重
        3. Reranker 重排序：取 top rerank_top_k
        4. 阈值过滤：< score_threshold 丢弃
        5. 可选 LLM 摘要：generate_summary=True 时调用 LLM 生成答案
        """
        if not question or not question.strip():
            return KnowledgeAnswer(
                answer="问题不能为空。",
                hit=False,
                reranker_mode=self._get_reranker_mode(),
            )

        # 1. 查询改写：得到多个检索友好查询
        queries = self.query_rewriter.rewrite(question, num_variants=2)
        if not queries:
            queries = [question.strip()]
        logger.info(
            "查询改写完成：original=%r variants=%d", question[:30], len(queries)
        )

        # 2. 混合检索：对每个改写查询召回，合并去重
        candidates = self._retrieve_and_merge(queries)

        if not candidates:
            logger.info("混合检索为空，返回未命中：question=%r", question[:50])
            return KnowledgeAnswer(
                answer="抱歉，知识库中未找到相关内容。",
                hit=False,
                rewritten_queries=queries,
                retrieval_mode="hybrid",
                reranker_mode=self._get_reranker_mode(),
            )

        # 3. Reranker 重排序：取 top rerank_top_k
        ranked_chunks = self.reranker.rerank(
            query=question,
            chunks=candidates,
            top_k=self._rerank_top_k,
        )

        # 4. 阈值过滤：rerank 后分数已归一化到 [0, 1]，可直接用阈值
        filtered = [c for c in ranked_chunks if c.score >= self._score_threshold]

        if not filtered:
            logger.info(
                "重排序后无 chunk 通过阈值过滤：top_score=%.2f 阈值=%.2f",
                ranked_chunks[0].score if ranked_chunks else 0.0,
                self._score_threshold,
            )
            return KnowledgeAnswer(
                answer="抱歉，知识库中未找到高度相关的内容。",
                hit=False,
                retrieved_chunks=ranked_chunks,  # 保留 top 结果便于调试
                rewritten_queries=queries,
                retrieval_mode="hybrid",
                reranker_mode=self._get_reranker_mode(),
            )

        # 5. 构造来源列表与置信度
        sources = self._format_sources(filtered)
        confidence = self._estimate_confidence(filtered)

        # 6. 可选 LLM 摘要
        answer_text = ""
        if generate_summary:
            answer_text = self._generate_summary(question, filtered)

        logger.info(
            "KnowledgeAgent 完成：session=%s 命中=%d 置信度=%.2f reranker=%s",
            session_id or "-",
            len(filtered),
            confidence,
            self._get_reranker_mode(),
        )

        return KnowledgeAnswer(
            answer=answer_text,
            sources=sources,
            retrieved_chunks=filtered,
            confidence=confidence,
            hit=True,
            rewritten_queries=queries,
            retrieval_mode="hybrid",
            reranker_mode=self._get_reranker_mode(),
        )

    def handle_stream(
        self,
        query: str,
        session_id: Optional[str] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """流式编排：检索 → 重排序 → 流式生成答案。

        协议：
        - 检索未命中：直接 yield done（不走 LLM），answer 为兜底文案
        - 检索命中：透传 RAGAgent.answer_stream 的事件（meta/token/done/error）

        与 answer() 区别：handle_stream 固定调用 LLM 流式生成答案，
        且不返回 KnowledgeAnswer 对象，而是 yield 事件流。
        """
        # 1. 空问题：直接 yield done 兜底
        if not query or not query.strip():
            yield {
                "type": "done",
                "answer": "问题不能为空。",
                "sources": [],
            }
            return

        # 2. 查询改写 + 混合检索 + 重排序 + 阈值过滤
        filtered = self._retrieve_filter(query)
        if not filtered:
            # 检索未命中：不走 LLM，直接返回兜底文案
            logger.info("流式检索未命中：query=%r", query[:50])
            yield {
                "type": "done",
                "answer": "抱歉，知识库中未找到相关内容。",
                "sources": [],
            }
            return

        # 3. 检索命中：透传 RAGAgent.answer_stream 的事件流
        logger.info(
            "流式检索命中：session=%s 命中=%d reranker=%s",
            session_id or "-",
            len(filtered),
            self._get_reranker_mode(),
        )
        yield from self.rag_agent.answer_stream(
            query=query,
            context_chunks=filtered,
            session_id=session_id,
        )

    def _retrieve_filter(self, query: str) -> List[RetrievedChunk]:
        """执行查询改写 → 混合检索 → 重排序 → 阈值过滤链路。

        抽取公共逻辑便于 answer 与 handle_stream 复用，
        避免流式与非流式路径检索行为分裂。
        """
        # 1. 查询改写
        queries = self.query_rewriter.rewrite(query, num_variants=2)
        if not queries:
            queries = [query.strip()]

        # 2. 混合检索合并
        candidates = self._retrieve_and_merge(queries)
        if not candidates:
            return []

        # 3. Reranker 重排序
        ranked_chunks = self.reranker.rerank(
            query=query,
            chunks=candidates,
            top_k=self._rerank_top_k,
        )

        # 4. 阈值过滤
        return [c for c in ranked_chunks if c.score >= self._score_threshold]

    def _retrieve_and_merge(self, queries: List[str]) -> List[RetrievedChunk]:
        """对多个改写查询分别混合检索，合并去重。

        去重键用 chunk 文本前 100 字符：相同文本不同来源视为同一片段。
        合并时保留较高分数，避免低分重复条目挤占 top 位。
        """
        merged: dict[str, RetrievedChunk] = {}
        for query in queries:
            chunks = self.hybrid_retriever.retrieve(query, top_k=20)
            for chunk in chunks:
                # 用文本前缀作为去重键，平衡精度与碰撞率
                key = chunk.text[:100]
                if not key:
                    continue
                existing = merged.get(key)
                if existing is None or chunk.score > existing.score:
                    merged[key] = chunk
        return list(merged.values())

    @staticmethod
    def _format_sources(chunks: List[RetrievedChunk]) -> List[str]:
        """构造来源展示列表，去重并限制数量。"""
        seen = set()
        sources: List[str] = []
        for chunk in chunks:
            if not chunk.source:
                continue
            source_key = f"{chunk.source}|{chunk.page_number}"
            if source_key in seen:
                continue
            seen.add(source_key)
            sources.append(f"{chunk.source} 第{chunk.page_number}页")
            if len(sources) >= _MAX_SOURCE_COUNT:
                break
        return sources

    @staticmethod
    def _estimate_confidence(chunks: List[RetrievedChunk]) -> float:
        """根据重排序分数估计置信度。

        以 Top-1 分数为主，命中数越多置信度越高，
        clamp 到 [0, 1] 区间。
        """
        if not chunks:
            return 0.0
        top_score = max(chunk.score for chunk in chunks)
        bonus = min(0.1, (len(chunks) - 1) * 0.02)
        return max(0.0, min(1.0, top_score + bonus))

    def _get_reranker_mode(self) -> str:
        """获取当前 reranker 模式字符串。

        触发 reranker 单例创建以读取真实状态；
        未加载时返回 unknown，加载后返回 cross_encoder / fallback。
        """
        return self.reranker.mode

    def _generate_summary(
        self, question: str, chunks: List[RetrievedChunk]
    ) -> str:
        """调用 LLM 基于检索片段生成摘要回答。

        复用 RAGAgent 的 prompt 结构，但本 Agent 默认不调用，
        仅在 generate_summary=True 时触发。
        """
        if not chunks:
            return ""

        # 构造上下文片段，截断控制 token
        context_lines: List[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            truncated = chunk.text[:800]
            context_lines.append(
                f"[片段{idx}] 来源：{chunk.source} 第{chunk.page_number}页\n{truncated}"
            )
        context_block = "\n\n".join(context_lines)

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一名企业客服助手。请严格基于下方知识片段回答用户问题，"
                    "不要编造未在片段中出现的信息。"
                    "回答末尾以「来源：」开头列出引用来源。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"知识片段：\n{context_block}\n\n"
                    f"用户问题：{question}\n\n请基于片段回答。"
                ),
            },
        ]
        context_texts = [chunk.text for chunk in chunks]
        # 性能优化：接入 ModelRouter，简单查询路由到小模型降低延迟与成本
        # mock 模式下 chat_with_routing 透传到 mock，行为与原 chat 一致
        # name/metadata 标记 prompt name=knowledge_summary，便于 Langfuse 聚合
        try:
            router = get_model_router()
            return router.chat_with_routing(
                messages=messages,
                query=question,
                temperature=0.3,
                context_chunks=context_texts,
                name="knowledge_summary",
                metadata={"prompt_version": "v1"},
            )
        except Exception as exc:
            # 路由失败时降级到普通 chat，保证链路可用
            logger.warning("ModelRouter 调用失败，降级普通 chat：%s", exc)
            return self.llm_client.chat(
                messages=messages,
                temperature=0.3,
                context_chunks=context_texts,
                name="knowledge_summary",
                metadata={"prompt_version": "v1"},
            )


# 模块级单例：Agent 编排无状态，进程内复用
_knowledge_agent: Optional[KnowledgeAgent] = None


def get_knowledge_agent() -> KnowledgeAgent:
    """获取 KnowledgeAgent 单例。"""
    global _knowledge_agent
    if _knowledge_agent is None:
        _knowledge_agent = KnowledgeAgent()
    return _knowledge_agent


def reset_knowledge_agent() -> None:
    """重置单例，便于测试切换配置。"""
    global _knowledge_agent
    _knowledge_agent = None
