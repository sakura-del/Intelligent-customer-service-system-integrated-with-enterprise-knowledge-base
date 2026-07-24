"""文档管理数据模型。

定义知识库文档版本管理的数据契约：
- DocumentVersion：单版本元信息（版本号、哈希、状态、chunk 数）
- DocumentSummary：文档列表项（摘要信息）
- DocumentDetail：文档详情（含完整版本历史）
- RollbackRequest / CanaryRequest：API 请求体
- DeleteResult：删除操作结果

设计要点：
所有模型字段与 document_store 内部存储结构对齐，
但对外暴露精简字段，避免泄露 chunk_ids 等内部细节。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentVersion(BaseModel):
    """文档单版本信息。"""

    version: str = Field(..., description="版本号，如 v1 / v2")
    doc_hash: str = Field("", description="该版本内容哈希")
    status: str = Field("active", description="版本状态：active / archived / deleted")
    chunk_count: int = Field(0, description="该版本 chunk 数量")
    created_at: str = Field("", description="版本创建时间，ISO8601 字符串")


class DocumentSummary(BaseModel):
    """文档列表摘要项。"""

    doc_id: str = Field(..., description="文档 ID")
    source: str = Field("", description="来源文件名")
    current_version: str = Field("", description="当前版本号")
    status: str = Field("active", description="文档状态：active / deleted")
    version_count: int = Field(0, description="历史版本总数")
    updated_at: str = Field("", description="最近更新时间，ISO8601 字符串")


class DocumentDetail(BaseModel):
    """文档详情，含完整版本历史。"""

    doc_id: str = Field(..., description="文档 ID")
    source: str = Field("", description="来源文件名")
    current_version: str = Field("", description="当前版本号")
    status: str = Field("active", description="文档状态")
    created_at: str = Field("", description="文档创建时间")
    updated_at: str = Field("", description="最近更新时间")
    versions: list[DocumentVersion] = Field(default_factory=list, description="全部版本历史")


class RollbackRequest(BaseModel):
    """版本回滚请求体。"""

    target_version: str = Field(..., description="目标回滚版本号，如 v1")


class CanaryRequest(BaseModel):
    """灰度验证请求体。"""

    doc_id: str = Field(..., description="待验证的文档 ID")
    version: str = Field(..., description="待验证的目标版本号")
    sample_queries: list[str] = Field(
        default_factory=list,
        description="样本查询列表；为空时使用默认数量占位查询",
    )


class DeleteResult(BaseModel):
    """删除操作结果。"""

    doc_id: str = Field(..., description="被删除的文档 ID")
    deleted_chunks: int = Field(0, description="实际从向量库移除的 chunk 数")
    success: bool = Field(True, description="是否删除成功")
    error: str | None = Field(None, description="错误信息，成功时为空")


class DocumentListResponse(BaseModel):
    """文档分页列表响应。

    汇总当前页文档摘要与总数，便于前端分页展示。
    """

    items: list[DocumentSummary] = Field(default_factory=list, description="当前页文档摘要列表")
    total: int = Field(0, description="已注册文档总数")
    limit: int = Field(20, description="本次查询的分页大小")
    offset: int = Field(0, description="本次查询的起始偏移")


class RollbackResult(BaseModel):
    """版本回滚操作结果。"""

    doc_id: str = Field(..., description="回滚的文档 ID")
    target_version: str = Field("", description="回滚到的目标版本号")
    success: bool = Field(True, description="是否回滚成功")
    restored_chunks: int = Field(
        0, description="回滚时重新入库的 chunk 数（目标版本 chunks 已删时触发）"
    )
    current_version: str = Field("", description="回滚后的当前版本号")
    error: str | None = Field(None, description="错误信息，成功时为空")
