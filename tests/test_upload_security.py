"""文件上传安全限制与输入校验测试。

覆盖：
1. /ingest 端点文件类型白名单（.md/.txt/.pdf/.docx 放行，其他返回 415）
2. /ingest 端点文件大小限制（超过 10MB 返回 413）
3. ChatRequest.message 长度限制（超过 2000 字符校验失败）
4. ChatRequest.channel 渠道白名单（非法值校验失败，合法值通过）

说明：415/413 在解析前拦截，不会触达向量库；
允许类型用例通过 patch ingest_document 聚焦校验层，避免真实入库开销。
"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas.chat import ChatRequest
from app.schemas.knowledge import IngestResult


@pytest.fixture()
def client() -> TestClient:
    """提供 FastAPI TestClient，每用例独立创建避免跨用例状态污染。"""
    from app.main import app

    return TestClient(app)


def _dummy_ingest_result(source: str = "test.md") -> IngestResult:
    """构造成功的 IngestResult，供 patch 使用以跳过真实解析。"""
    return IngestResult(source=source, total_chunks=1, added_chunks=1)


# ----------------------------------------------------------------------
# 1. 文件类型白名单
# ----------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["doc.md", "doc.txt", "doc.pdf", "doc.docx"])
def test_allowed_file_types_accepted(client: TestClient, filename: str) -> None:
    """允许的文件类型应通过安全校验，不被 415/413 拦截。"""
    # patch ingest_document 跳过真实解析，聚焦文件类型校验层
    with patch("app.api.v1.knowledge.ingest_document") as mocked:
        mocked.return_value = _dummy_ingest_result(filename)
        response = client.post(
            "/api/v1/knowledge/ingest",
            files={
                "file": (
                    filename,
                    io.BytesIO(b"content"),
                    "application/octet-stream",
                )
            },
            data={"register": "false"},
        )
    assert response.status_code == 200, (
        f"允许的类型不应被拦截：{filename}，实际状态码：{response.status_code}"
    )
    # 确认 ingest_document 被调用，说明类型与大小校验均通过
    assert mocked.called, "通过校验后应进入入库流程"


@pytest.mark.parametrize("filename", ["malware.exe", "script.js", "page.html", "archive.zip"])
def test_disallowed_file_types_rejected_415(client: TestClient, filename: str) -> None:
    """不允许的文件类型应返回 415 Unsupported Media Type。"""
    response = client.post(
        "/api/v1/knowledge/ingest",
        files={
            "file": (
                filename,
                io.BytesIO(b"content"),
                "application/octet-stream",
            )
        },
        data={"register": "false"},
    )
    assert response.status_code == 415
    detail = response.json()["detail"]
    assert "不支持的文件类型" in detail
    # 错误信息应提示允许的类型，便于调用方修正
    assert ".md" in detail


# ----------------------------------------------------------------------
# 2. 文件大小限制
# ----------------------------------------------------------------------


def test_oversized_file_rejected_413(client: TestClient) -> None:
    """超过 10MB 的文件应返回 413 Payload Too Large。"""
    from app.api.v1.knowledge import MAX_FILE_SIZE

    # 构造刚好超过上限的内容，验证边界判定为拒绝
    oversized_content = b"x" * (MAX_FILE_SIZE + 1)
    response = client.post(
        "/api/v1/knowledge/ingest",
        files={"file": ("big.md", io.BytesIO(oversized_content), "text/markdown")},
        data={"register": "false"},
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert "文件过大" in detail
    assert "10MB" in detail


def test_max_boundary_size_accepted(client: TestClient) -> None:
    """恰好等于上限的文件应通过大小校验（边界值包含在内）。"""
    from app.api.v1.knowledge import MAX_FILE_SIZE

    boundary_content = b"x" * MAX_FILE_SIZE
    # patch ingest_document 避免对纯字节内容做真实解析
    with patch("app.api.v1.knowledge.ingest_document") as mocked:
        mocked.return_value = _dummy_ingest_result("boundary.md")
        response = client.post(
            "/api/v1/knowledge/ingest",
            files={
                "file": (
                    "boundary.md",
                    io.BytesIO(boundary_content),
                    "text/markdown",
                )
            },
            data={"register": "false"},
        )
    assert response.status_code == 200, "恰好等于上限不应被 413 拦截"
    assert mocked.called, "边界大小应通过校验进入入库流程"


def test_max_file_size_is_10mb() -> None:
    """配置项 MAX_FILE_SIZE 应为 10MB（10 * 1024 * 1024 字节）。"""
    from app.api.v1.knowledge import MAX_FILE_SIZE

    assert MAX_FILE_SIZE == 10 * 1024 * 1024


# ----------------------------------------------------------------------
# 3. ChatRequest.message 长度限制
# ----------------------------------------------------------------------


def test_chat_request_message_at_limit_passes() -> None:
    """message 恰好 2000 字符应校验通过（边界值包含在内）。"""
    req = ChatRequest(message="a" * 2000)
    assert len(req.message) == 2000


def test_chat_request_message_over_limit_fails() -> None:
    """message 超过 2000 字符应校验失败。"""
    with pytest.raises(ValidationError):
        ChatRequest(message="a" * 2001)


# ----------------------------------------------------------------------
# 4. ChatRequest.channel 渠道白名单
# ----------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["web", "app", "wechat", "dingtalk", "api"])
def test_chat_request_valid_channel_passes(channel: str) -> None:
    """合法渠道应校验通过。"""
    req = ChatRequest(message="hi", channel=channel)
    assert req.channel == channel


@pytest.mark.parametrize(
    "channel", ["", "email", "web ", "WEB", "slack", "weixin", "dingtalk2"]
)
def test_chat_request_invalid_channel_fails(channel: str) -> None:
    """非法渠道应校验失败。"""
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", channel=channel)


def test_chat_request_default_channel_passes() -> None:
    """未传 channel 时默认值 api 应校验通过。"""
    req = ChatRequest(message="hi")
    assert req.channel == "api"
