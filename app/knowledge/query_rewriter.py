"""查询改写模块。

将用户口语化问题转换为检索友好的查询：
- 调用 LLM 提取关键词、补充实体、去除口语噪声
- 支持多查询生成（2-3 个变体）以提升召回
- LLM 不可用时（mock）退化为基于停用词的关键词提取

为什么需要查询改写：用户提问往往包含「请问」「怎么办」等口语
噪声词，与文档语言风格不一致，会显著降低 BM25 与向量召回质量。
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from app.agents.llm_client import LLMClient, get_llm_client
from app.core.logging import get_logger

logger = get_logger("app.knowledge.query_rewriter")

# 中文常见停用词与口语噪声：用于 LLM 不可用时的兜底分词清洗
_STOPWORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "那", "它", "他", "她", "们", "什么", "怎么",
    "怎么", "怎样", "怎么办", "请问", "麻烦", "一下", "一下吗", "吗", "呢",
    "啊", "吧", "哦", "嗯", "想", "需要", "求", "帮", "帮忙", "谢谢",
    "一下", "可以", "能", "能够", "应该", "如何", "为何", "为何",
}

# 单条改写查询最大字符数：避免过长噪声污染召回
_MAX_QUERY_LEN = 64
# 多查询生成上限：超过此数量截断，控制下游检索成本
_MAX_VARIANTS = 3


class QueryRewriter:
    """查询改写器。

    优先用 LLM 改写为检索友好的多个查询变体；
    LLM 不可用时（mock）退化为停用词清洗 + 关键词提取，
    保证无网络/无 Key 环境下仍能改善召回。
    """

    def __init__(self, llm_client: Optional[LLMClient] = None) -> None:
        # 延迟取单例，便于测试注入 mock 实现
        self._llm_client = llm_client

    @property
    def llm_client(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    def rewrite(self, question: str, num_variants: int = 2) -> List[str]:
        """改写问题，返回检索友好的查询列表。

        num_variants 控制返回的查询数量（含原查询去重后），
        LLM 模式下生成多查询变体；mock 模式下返回原查询 + 关键词版本。
        """
        if not question or not question.strip():
            return []

        # 始终保留原查询：避免改写丢失用户意图
        normalized = question.strip()
        queries: List[str] = [normalized]

        # LLM 真实可用时走多查询改写路径
        if not self.llm_client.is_mock:
            llm_queries = self._rewrite_with_llm(normalized, num_variants)
            for query in llm_queries:
                # 去重且非空，避免重复查询浪费召回资源
                if query and query not in queries:
                    queries.append(query)
                if len(queries) >= _MAX_VARIANTS:
                    break

        # mock 模式或 LLM 改写失败：补一个关键词版本，仍能改善 BM25 召回
        if len(queries) < 2:
            keyword_query = self._extract_keywords(normalized)
            if keyword_query and keyword_query not in queries:
                queries.append(keyword_query)

        # 截断到目标变体数量，控制下游成本
        return queries[: max(num_variants, 1)]

    def _rewrite_with_llm(self, question: str, num_variants: int) -> List[str]:
        """调用 LLM 生成多个检索友好查询变体。

        通过 system prompt 约束输出为 JSON 数组，便于稳定解析；
        调用失败时返回空列表，由上层走关键词兜底。
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "你是查询改写助手。将用户问题改写为 2-3 个检索友好的查询变体："
                    "去除口语词（请问/怎么办/一下），"
                    "补充关键实体与同义词，保留问题核心意图。"
                    "仅返回 JSON 字符串数组，不要任何解释，例如："
                    '["忘记密码 重置链接 邮箱", "登录密码 找回 30分钟有效"]'
                ),
            },
            {"role": "user", "content": question},
        ]
        try:
            reply = self.llm_client.chat(messages=messages, temperature=0.2)
            return self._parse_llm_queries(reply)
        except Exception as exc:
            # LLM 调用失败时不阻断主流程，记录日志后由上层兜底
            logger.warning("LLM 查询改写失败，降级到关键词提取：%s", exc)
            return []

    @staticmethod
    def _parse_llm_queries(reply: str) -> List[str]:
        """解析 LLM 返回的 JSON 数组为查询列表。

        LLM 偶尔会输出多余文本，尝试提取首个 JSON 数组片段；
        解析失败时回退为按行切分，保证健壮性。
        """
        if not reply:
            return []

        text = reply.strip()
        # 优先尝试 JSON 解析（最规范的输出）
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(item).strip()[:_MAX_QUERY_LEN] for item in data if str(item).strip()]
        except json.JSONDecodeError:
            pass

        # 兜底：从文本中抽取第一个 [ ... ] 区间
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, list):
                    return [str(item).strip()[:_MAX_QUERY_LEN] for item in data if str(item).strip()]
            except json.JSONDecodeError:
                pass

        # 最终兜底：按换行/逗号切分
        items = [s.strip().strip('"').strip("'")[:_MAX_QUERY_LEN] for s in re.split(r"[\n,]", text)]
        return [item for item in items if item]

    @staticmethod
    def _extract_keywords(question: str) -> str:
        """从口语化问题中提取关键词，去除停用词与噪声。

        采用字符级中文分词近似：保留长度>=2 的非停用词片段，
        拼接为空格分隔的关键词串，便于 BM25 索引匹配。
        """
        # 去除标点与多余空白
        cleaned = re.sub(r"[，。？！,.?!；;：:\s]+", " ", question).strip()
        if not cleaned:
            return ""

        # 按空格切分后再过滤停用词
        tokens = [t for t in cleaned.split() if t and t not in _STOPWORDS]
        if not tokens:
            return ""

        return " ".join(tokens)[:_MAX_QUERY_LEN]


# 模块级单例：无状态，进程内复用
_query_rewriter: Optional[QueryRewriter] = None


def get_query_rewriter() -> QueryRewriter:
    """获取 QueryRewriter 单例。"""
    global _query_rewriter
    if _query_rewriter is None:
        _query_rewriter = QueryRewriter()
    return _query_rewriter


def reset_query_rewriter() -> None:
    """重置单例，便于测试切换 LLM 配置。"""
    global _query_rewriter
    _query_rewriter = None
