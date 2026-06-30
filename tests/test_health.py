"""健康检查接口测试。

使用 FastAPI TestClient 进行内存级测试，无需启动真实服务。
"""
from fastapi.testclient import TestClient

from app.main import app


def test_health_check_returns_ok() -> None:
    """健康检查应返回 200 与 ok 状态。"""
    client = TestClient(app)
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "app" in body
    assert "version" in body
    assert "timestamp" in body
