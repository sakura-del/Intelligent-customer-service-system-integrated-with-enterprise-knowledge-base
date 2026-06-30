"""文档更新机制数据模型。

定义 Task 17 全量 / 增量 / 单文件更新接口的请求与响应体，
作为 update_mechanism 模块与 api/v1/update 路由之间的数据契约。

设计要点：
- UpdateMode 用枚举约束取值，避免字符串散落难以维护
- 响应模型字段与内部 UpdateResult 对齐，便于直接序列化返回
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class UpdateMode(str, Enum):
    """更新模式枚举。

    FULL=全量更新（含删除失效记录），INCREMENTAL=增量更新（仅处理新增与变更）。
    继承 str 便于 JSON 序列化与 OpenAPI 文档展示。
    """

    FULL = "full"
    INCREMENTAL = "incremental"


class UpdateRequest(BaseModel):
    """触发批量更新的请求体。

    dir_path 为待扫描的根目录；extensions 为空时使用默认支持格式。
    """

    dir_path: str = Field(..., description="待扫描的文档根目录绝对或相对路径")
    extensions: Optional[List[str]] = Field(
        None,
        description="待处理的文件扩展名列表，为空时使用 .pdf/.docx/.html/.txt/.md",
    )


class UpdateSingleFileRequest(BaseModel):
    """单文件更新请求体。

    支持传入文件路径触发入库；metadata 用于覆盖产品分类、知识类型等元数据。
    """

    file_path: str = Field(..., description="待入库的单文件路径")
    metadata: Optional[Dict[str, str]] = Field(
        None,
        description="元数据覆盖项，如 product_category / knowledge_type 等",
    )


class UpdateResultResponse(BaseModel):
    """更新结果响应体。

    字段与内部 UpdateResult 对齐，便于调度器结果直接序列化返回。
    """

    mode: UpdateMode = Field(..., description="本次更新模式")
    scanned: int = Field(0, description="扫描到的文件总数")
    added: int = Field(0, description="新增入库的文件数")
    updated: int = Field(0, description="内容变更后重新入库的文件数")
    skipped: int = Field(0, description="已存在且未变更被跳过的文件数")
    deleted: int = Field(0, description="全量更新中清理的失效记录数")
    failed: int = Field(0, description="处理失败的文件数")
    duration_seconds: float = Field(0.0, description="本次更新总耗时（秒）")
    errors: List[str] = Field(
        default_factory=list,
        description="失败文件与错误信息列表，便于排查",
    )


class UpdateStatusResponse(BaseModel):
    """查询最近一次更新状态的响应体。

    last_update 为空表示尚未执行过更新；message 提供人类可读的提示。
    """

    last_update: Optional[UpdateResultResponse] = Field(
        None, description="最近一次更新结果，未执行过时为空"
    )
    message: str = Field("", description="状态说明文本")
