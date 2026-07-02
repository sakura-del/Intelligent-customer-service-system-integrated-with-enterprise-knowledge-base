"""单 Agent RAG 问答。

编排 检索 → 构造 prompt → LLM 生成 → 标注来源 的完整链路，
对外暴露 RAGAgent.answer 与 RAGAgent.answer_stream 两套入口：
- answer：同步返回完整 RAGAnswer
- answer_stream：流式 yield meta/token/done 事件，供 SSE 端点透传
"""
from __future__ import annotations

from typing import Any, Dict, Generator, List, Optional

from app.agents.llm_client import LLMClient, get_llm_client
from app.core.logging import get_logger
from app.knowledge.retriever import KnowledgeRetriever, get_retriever
from app.schemas.chat import RAGAnswer
from app.schemas.knowledge import RetrievedChunk

logger = get_logger("app.agents.rag_agent")

# 系统提示：约束模型只能基于检索片段回答，避免编造
SYSTEM_PROMPT = (
    "你是一名企业客服助手。请严格基于下方提供的知识片段回答用户问题，"
    "不要编造未在片段中出现的信息。若知识片段不足以回答问题，"
    "请明确说明「知识库中未找到相关内容」。"
    "回答末尾需以「来源：」开头列出引用的知识片段来源，"
    "格式如「来源：产品FAQ.md 第3页」，多个来源用逗号分隔。"
)

# 单条片段的引用上限：避免来源行过长影响阅读与 token 消耗
MAX_SOURCE_COUNT = 3
# 单条片段文本在 prompt 中的字符上限，控制 token 成本
MAX_CHUNK_CHARS = 800


class RAGAgent:
    """单 Agent RAG 问答编排器。

    持有 retriever 与 llm_client，answer 方法串联整条 RAG 链路。
    检索为空时直接返回兜底回复，避免无意义的 LLM 调用。
    """

    def __init__(
        self,
        retriever: Optional[KnowledgeRetriever] = None,
        llm_client: Optional[LLMClient] = None,
        top_k: int = 5,
    ) -> None:
        # 延迟取单例，便于在测试中注入自定义实现
        self._retriever = retriever
        self._llm_client = llm_client
        self.top_k = top_k

    @property
    def retriever(self) -> KnowledgeRetriever:
        if self._retriever is None:
            self._retriever = get_retriever()
        return self._retriever

    @property
    def llm_client(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    def answer(
        self,
        question: str,
        session_id: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> RAGAnswer:
        """对用户问题产出 RAG 回答。

        检索为空时返回固定兜底文案，hit=False，confidence=0；
        命中时构造含来源信息的 prompt 交给 LLM 生成最终答案。
        """
        if not question or not question.strip():
            return RAGAnswer(
                answer="问题不能为空。",
                sources=[],
                retrieved_chunks=[],
                confidence=0.0,
                hit=False,
            )

        # 1. 检索：top_k 未指定时使用实例默认值
        effective_top_k = top_k if top_k is not None else self.top_k
        chunks = self.retriever.retrieve(question, top_k=effective_top_k)

        # 2. 检索为空：直接返回兜底，避免无意义调用 LLM
        if not chunks:
            logger.info("检索为空，返回兜底回复：question=%r", question[:50])
            return RAGAnswer(
                answer="抱歉，知识库中未找到相关内容。",
                sources=[],
                retrieved_chunks=[],
                confidence=0.0,
                hit=False,
            )

        # 3. 构造 prompt 与来源列表
        sources = self._format_sources(chunks)
        prompt_messages = self._build_prompt_messages(question, chunks)
        # mock 模式需要原始文本拼接回复，真实模式不读取该字段
        context_chunks = [chunk.text for chunk in chunks]

        # 4. LLM 生成
        # name/metadata 标记 prompt name=rag_qa，便于 Langfuse 聚合分析
        reply = self.llm_client.chat(
            messages=prompt_messages,
            temperature=0.3,
            context_chunks=context_chunks,
            name="rag_qa",
            metadata={"prompt_version": "v1"},
        )

        # 5. 综合置信度：取 Top-1 相似度作为基础，受命中数加成
        confidence = self._estimate_confidence(chunks)

        logger.info(
            "RAG 回答完成：session=%s 命中=%d 置信度=%.2f mock=%s",
            session_id or "-",
            len(chunks),
            confidence,
            self.llm_client.is_mock,
        )

        return RAGAnswer(
            answer=reply,
            sources=sources,
            retrieved_chunks=chunks,
            confidence=confidence,
            hit=True,
        )

    def answer_stream(
        self,
        query: str,
        context_chunks: List[RetrievedChunk],
        session_id: Optional[str] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """流式 RAG 问答：基于已检索的 context_chunks 流式生成答案。

        协议：
        - {"type": "meta", "sources": [...], "context_count": N}：先发来源元信息
        - {"type": "token", "content": "..."}：LLM 流式 token（多次）
        - {"type": "error", "message": "..."}：LLM 异常
        - {"type": "done", "answer": "...", "sources": [...]}：完成事件

        与 answer() 区别：检索已由上层完成，本方法只负责流式生成，
        避免流式阶段重复检索造成延迟。
        """
        # 1. 先发 meta 事件：让前端尽早展示来源
        sources = self._format_sources(context_chunks)
        yield {
            "type": "meta",
            "sources": sources,
            "context_count": len(context_chunks),
        }

        # 2. 构造 prompt 与上下文文本，复用 answer 的构造逻辑保证一致
        prompt_messages = self._build_prompt_messages(query, context_chunks)
        context_texts = [chunk.text for chunk in context_chunks]

        # 3. 流式生成：透传 LLM token，同时累积完整文本用于 done 事件
        # name/metadata 标记 prompt name=rag_qa，便于 Langfuse 聚合分析
        full_text_parts: List[str] = []
        for event in self.llm_client.stream_chat(
            messages=prompt_messages,
            temperature=0.3,
            context_chunks=context_texts,
            name="rag_qa",
            metadata={"prompt_version": "v1"},
        ):
            if event["type"] == "token":
                full_text_parts.append(event["content"])
                yield {"type": "token", "content": event["content"]}
            elif event["type"] == "error":
                yield {"type": "error", "message": event["message"]}
            elif event["type"] == "done":
                # done 事件以 LLM 给的完整文本为准，兼容 LLM 不返回 done 的场景
                full_text = event.get("content") or "".join(full_text_parts)
                yield {"type": "done", "answer": full_text, "sources": sources}
                return

        # 4. 兜底：LLM 未发 done 事件时，主动发一次 done 收尾
        full_text = "".join(full_text_parts)
        logger.info(
            "RAG 流式回答完成（兜底 done）：session=%s 长度=%d",
            session_id or "-",
            len(full_text),
        )
        yield {"type": "done", "answer": full_text, "sources": sources}

    @staticmethod
    def _format_sources(chunks: List[RetrievedChunk]) -> List[str]:
        """构造来源展示列表，格式「文件名 第N页」。

        去重避免同一文件多页重复展示，最多保留 MAX_SOURCE_COUNT 条。
        """
        seen = set()
        sources: List[str] = []
        for chunk in chunks:
            source_key = f"{chunk.source}|{chunk.page_number}"
            if source_key in seen:
                continue
            seen.add(source_key)
            sources.append(f"{chunk.source} 第{chunk.page_number}页")
            if len(sources) >= MAX_SOURCE_COUNT:
                break
        return sources

    @staticmethod
    def _build_prompt_messages(
        question: str, chunks: List[RetrievedChunk]
    ) -> List[dict]:
        """构造 LLM 消息列表：system 约束 + 检索片段 + 用户问题。

        片段文本截断到 MAX_CHUNK_CHARS 控制总 token，避免超长上下文。
        """
        context_lines: List[str] = []
        for index, chunk in enumerate(chunks, start=1):
            # 截断过长片段，保留开头最相关内容
            truncated = chunk.text[:MAX_CHUNK_CHARS]
            context_lines.append(
                f"[片段{index}] 来源：{chunk.source} 第{chunk.page_number}页 "
                f"章节：{chunk.section or '未知'}\n{truncated}"
            )
        context_block = "\n\n".join(context_lines)

        user_content = (
            f"知识片段：\n{context_block}\n\n"
            f"用户问题：{question}\n\n"
            "请基于上述知识片段回答，并在末尾标注来源。"
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _estimate_confidence(chunks: List[RetrievedChunk]) -> float:
        """根据检索结果粗略估计置信度。

        以 Top-1 相似度为主，命中数越多置信度越高，
        最终 clamp 到 [0, 1] 区间，仅供前端展示参考。
        """
        if not chunks:
            return 0.0
        top_score = max(chunk.score for chunk in chunks)
        # 命中数加成：每多一条 +0.02，最多 +0.1
        bonus = min(0.1, (len(chunks) - 1) * 0.02)
        confidence = top_score + bonus
        return max(0.0, min(1.0, confidence))


# 模块级单例：Agent 编排无状态，进程内复用
_rag_agent: Optional[RAGAgent] = None


def get_rag_agent() -> RAGAgent:
    """获取 RAGAgent 单例。"""
    global _rag_agent
    if _rag_agent is None:
        _rag_agent = RAGAgent()
    return _rag_agent


def reset_rag_agent() -> None:
    """重置单例，便于测试切换配置。"""
    global _rag_agent
    _rag_agent = None
