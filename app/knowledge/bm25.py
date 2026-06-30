"""BM25 关键词检索器。

优先使用 rank-bm25 库的 BM25Okapi 实现；
若未安装或初始化失败则降级到自实现的简易 BM25（词频+idf 近似），
保证无网络环境下仍可承担混合检索中的关键词召回职责。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.schemas.knowledge import TextChunk

logger = get_logger("app.knowledge.bm25")

# 检查 rank-bm25 是否可用：仅在导入阶段执行一次
try:
    from rank_bm25 import BM25Okapi  # type: ignore

    _HAS_RANK_BM25 = True
except ImportError:  # pragma: no cover - 安装成功路径为主
    _HAS_RANK_BM25 = False
    logger.warning("未安装 rank-bm25，将使用自实现简易 BM25（精度较低）")


def tokenize(text: str) -> List[str]:
    """中文+英文混合分词：中文用字符级 bigram，英文按单词切分。

    为什么用 bigram：中文无空格分隔，单字切分区分度低、整句切分无法匹配子串；
    bigram（如「忘记」「记密」「密码」）既能匹配子串查询，又保留一定语义单元，
    是无词典场景下的常见折中方案。真实生产应接入 jieba 等分词器。
    """
    if not text:
        return []

    tokens: List[str] = []
    # 分离中文片段与非中文片段（英文/数字/标点）
    segments = re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z0-9]+", text)
    for segment in segments:
        if not segment:
            continue
        # 中文片段：生成 bigram，长度 1 时退化为单字
        if re.match(r"[\u4e00-\u9fa5]", segment[0]):
            if len(segment) == 1:
                tokens.append(segment)
            else:
                for i in range(len(segment) - 1):
                    tokens.append(segment[i : i + 2])
        else:
            # 英文/数字：直接作为单个 token（已按空格/标点分离）
            tokens.append(segment.lower())
    return tokens


class _SimpleBM25:
    """自实现简易 BM25：词频 + idf 近似，仅作 fallback。

    公式：score(q, d) = Σ idf(t) * (tf * (k1+1)) / (tf + k1*(1-b+b*dl/avgdl))
    参数 k1=1.5, b=0.75 是 BM25 经验默认值。
    """

    def __init__(self, corpus: List[List[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_len) / len(corpus) if corpus else 0.0
        # 文档频率：包含某 term 的文档数
        self.doc_freq: Dict[str, int] = {}
        self.term_freqs: List[Dict[str, int]] = []
        self._build_index()

    def _build_index(self) -> None:
        """构建倒排索引：统计每篇文档词频与全局文档频率。"""
        for doc in self.corpus:
            tf = Counter(doc)
            self.term_freqs.append(tf)
            for term in tf:
                self.doc_freq[term] = self.doc_freq.get(term, 0) + 1

    def get_scores(self, query: List[str]) -> List[float]:
        """计算查询词与每篇文档的 BM25 分数。"""
        n_docs = len(self.corpus)
        if n_docs == 0:
            return []
        scores = [0.0] * n_docs
        for term in query:
            if term not in self.doc_freq:
                continue
            # idf 用 BM25+ 平滑，避免负分
            df = self.doc_freq[term]
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for idx in range(n_docs):
                tf = self.term_freqs[idx].get(term, 0)
                if tf == 0:
                    continue
                dl = self.doc_len[idx] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[idx] += idf * (tf * (self.k1 + 1)) / denom
        return scores


class BM25Retriever:
    """BM25 关键词检索器。

    索引按需构建：传入 chunks 后构建倒排索引，
    search 返回 [(chunk_id, score)] 列表，分数越高越相关。
    大规模索引时按批构建避免一次性内存峰值。
    """

    def __init__(self) -> None:
        self._chunk_ids: List[str] = []
        self._chunk_texts: List[str] = []
        self._tokenized: List[List[str]] = []
        self._bm25 = None  # rank-bm25 或 _SimpleBM25 实例
        self._use_lib = _HAS_RANK_BM25

    def index(
        self, chunks: List[TextChunk], ids: Optional[List[str]] = None
    ) -> int:
        """构建 BM25 索引，返回索引条目数。

        chunks 来自向量库的全部文档；ids 可选，未提供时用索引位置作为 id，
        便于调用方用 chroma 真实 id 做 RRF 融合时去重匹配。
        内存优化：每次重新索引前清空旧数据，避免泄漏。
        """
        # 清空旧索引，避免重复累积导致内存泄漏与分数偏移
        self._chunk_ids = []
        self._chunk_texts = []
        self._tokenized = []
        self._bm25 = None

        if not chunks:
            return 0

        for idx, chunk in enumerate(chunks):
            # 优先用调用方传入的 id（如 chroma id），便于 RRF 融合跨路匹配
            chunk_id = ids[idx] if ids and idx < len(ids) else str(idx)
            self._chunk_ids.append(chunk_id)
            self._chunk_texts.append(chunk.text)
            self._tokenized.append(tokenize(chunk.text))

        # 优先用 rank-bm25，失败则降级到自实现
        if self._use_lib:
            try:
                self._bm25 = BM25Okapi(self._tokenized)
                logger.info(
                    "BM25 索引构建完成（rank-bm25）：条目数=%d", len(self._chunk_ids)
                )
            except Exception as exc:
                # 库版本/数据异常时降级，保证链路不中断
                logger.warning(
                    "rank-bm25 初始化失败，降级到自实现 BM25：%s", exc
                )
                self._use_lib = False
                self._bm25 = _SimpleBM25(self._tokenized)
        else:
            self._bm25 = _SimpleBM25(self._tokenized)
            logger.info(
                "BM25 索引构建完成（自实现 fallback）：条目数=%d", len(self._chunk_ids)
            )

        return len(self._chunk_ids)

    def search(self, query: str, top_k: int = 20) -> List[Tuple[str, float]]:
        """关键词检索：返回 [(chunk_id, score)] 按分数降序。

        top_k 控制返回数量，避免返回过多结果增加下游融合开销。
        """
        if not query or not self._bm25 or not self._chunk_ids:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        # 获取所有文档分数后排序，截断到 top_k
        if self._use_lib and isinstance(self._bm25, BM25Okapi):
            scores = self._bm25.get_scores(query_tokens)
        else:
            scores = self._bm25.get_scores(query_tokens)  # type: ignore[union-attr]

        # 按分数降序，并过滤零分文档（无任何 token 命中）
        ranked: List[Tuple[int, float]] = [
            (idx, float(score))
            for idx, score in enumerate(scores)
            if score > 0
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)

        # 转换为 (chunk_id, score)，截断到 top_k
        return [(self._chunk_ids[idx], score) for idx, score in ranked[:top_k]]

    def get_text(self, chunk_id: str) -> Optional[str]:
        """根据 chunk_id 反查原文，便于融合阶段统一访问。"""
        try:
            idx = self._chunk_ids.index(chunk_id)
            return self._chunk_texts[idx]
        except ValueError:
            return None

    @property
    def size(self) -> int:
        """当前索引的文档数。"""
        return len(self._chunk_ids)

    @property
    def backend(self) -> str:
        """当前使用的 BM25 后端：rank_bm25 / simple。"""
        return "rank_bm25" if self._use_lib else "simple"


# 模块级单例：知识库未变更时复用索引避免重建
_bm25_retriever: Optional[BM25Retriever] = None


def get_bm25_retriever() -> BM25Retriever:
    """获取 BM25Retriever 单例。"""
    global _bm25_retriever
    if _bm25_retriever is None:
        _bm25_retriever = BM25Retriever()
    return _bm25_retriever


def reset_bm25_retriever() -> None:
    """重置单例，便于测试切换索引数据。"""
    global _bm25_retriever
    _bm25_retriever = None
