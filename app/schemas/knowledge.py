"""知识库流水线数据模型。

定义文档解析、切分、元数据标注与入库结果的数据契约，
作为 parsers/ chunker/ metadata/ pipeline 模块间的统一结构。
"""

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.quality import QualityReport


class SectionInfo(BaseModel):
    """文档章节信息。

    保留标题文本与层级（如 H1/H2），用于切分时聚合语义边界。
    """

    title: str = Field(..., description="章节标题文本")
    level: int = Field(1, description="标题层级，1 表示一级标题，数字越大层级越深")


class ParsedPage(BaseModel):
    """解析后的单页内容。

    对于 PDF 按页生成；对于 Word/HTML 等无页码概念则按段落块合成虚拟页。
    """

    page_number: int = Field(..., description="页码，从 1 开始")
    text: str = Field(..., description="该页的纯文本内容")
    sections: list[SectionInfo] = Field(
        default_factory=list,
        description="该页命中的章节标题列表，用于后续切分聚合",
    )


class ParsedDocument(BaseModel):
    """单文档解析结果。

    由 parsers 模块产出，统一承载不同来源（PDF/Word/HTML/TXT）的结构化文本。
    """

    source: str = Field(..., description="来源文件名或标识")
    file_type: str = Field(..., description="文件类型：pdf/docx/html/txt/md")
    pages: list[ParsedPage] = Field(default_factory=list, description="解析后的页列表")
    doc_hash: str = Field("", description="文档内容哈希，用于去重与版本追踪")


class TextChunk(BaseModel):
    """切分后的文本片段。

    保留来源页码与章节，便于检索时回溯原文位置。
    """

    text: str = Field(..., description="片段文本内容")
    page_number: int = Field(1, description="所属页码")
    section: str = Field("", description="所属章节标题，多级用 / 拼接")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="附加元数据，供 metadata 模块补充",
    )


class IngestResult(BaseModel):
    """单文档入库结果。

    用于 API 返回入库统计，便于前端展示与监控。
    """

    source: str = Field(..., description="来源文件名")
    total_chunks: int = Field(0, description="切分生成的 chunk 总数")
    added_chunks: int = Field(0, description="实际写入向量库的 chunk 数")
    deduped_chunks: int = Field(0, description="被去重过滤的 chunk 数")
    duration_seconds: float = Field(0.0, description="流水线总耗时（秒）")
    doc_hash: str = Field("", description="文档哈希")
    embedding_mode: str = Field(
        "unknown",
        description="向量化模式：bge / fallback，便于排查质量",
    )
    error: str | None = Field(None, description="错误信息，成功时为空")
    # Task 16：文档管理与版本信息，默认空串保证旧调用方兼容
    doc_id: str = Field("", description="文档 ID，由 document_store 分配")
    version: str = Field("", description="本次入库对应的版本号，如 v1")
    # Task 16：可选质量校验报告，仅 validate_quality=true 时填充
    quality_report: QualityReport | None = Field(
        None, description="质量校验报告，未启用质量校验时为空"
    )


class KnowledgeStats(BaseModel):
    """知识库统计信息。"""

    collection_name: str = Field(..., description="集合名")
    total_documents: int = Field(0, description="向量库中总条目数")
    persist_dir: str = Field("", description="持久化目录路径")


class RetrievedChunk(BaseModel):
    """单条检索结果。

    仅保留 RAG 生成所需的最少字段，避免把向量库内部结构外泄，
    也便于上层在 prompt 构造时统一访问。
    """

    text: str = Field(..., description="命中的知识片段文本")
    score: float = Field(0.0, description="相似度得分，越大越相关")
    source: str = Field("", description="来源文件名")
    page_number: int = Field(1, description="所属页码")
    section: str = Field("", description="所属章节标题")
    knowledge_type: str = Field("doc", description="知识类型：faq/policy/doc 等")


class KnowledgeAnswer(BaseModel):
    """知识库检索 Agent 的返回结果。

    相比 RAGAnswer 增加检索链路诊断字段：
    - rewritten_queries：查询改写产生的变体，便于排查召回质量
    - retrieval_mode：检索模式（hybrid/vector_only），标识是否启用混合检索
    - reranker_mode：重排序模式（cross_encoder/fallback），标识 reranker 是否真实加载
    """

    answer: str = Field("", description="最终给用户的回答文本；未启用 LLM 摘要时为空")
    sources: list[str] = Field(
        default_factory=list,
        description="来源列表，格式如 '产品FAQ.md 第3页'",
    )
    retrieved_chunks: list[RetrievedChunk] = Field(
        default_factory=list,
        description="重排序后的 Top-K 知识片段",
    )
    confidence: float = Field(0.0, description="检索置信度，0-1 之间")
    hit: bool = Field(False, description="是否检索到相关知识")
    rewritten_queries: list[str] = Field(
        default_factory=list,
        description="查询改写产生的查询变体，便于调试",
    )
    retrieval_mode: str = Field(
        "hybrid",
        description="检索模式：hybrid（向量+BM25+RRF）/ vector_only",
    )
    reranker_mode: str = Field(
        "unknown",
        description="重排序模式：cross_encoder / fallback",
    )
