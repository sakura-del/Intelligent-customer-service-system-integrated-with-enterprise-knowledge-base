"""对话端点鉴权与多 Agent 协同响应测试。

通过临时修改全局 Settings 的 API_KEY 模拟生产鉴权模式，
验证无 Key 拒绝、有 Key 放行的行为，并校验多 Agent 协同返回的字段结构。
"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

# 测试用独立持久化目录，避免与其他测试模块共享向量库状态
TEST_PERSIST_DIR = "./tests/_chroma_data_chat"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_dir():
    """模块级 fixture：隔离 ChromaDB 目录与多 Agent 协同相关单例。

    本测试只验证端点结构，不依赖具体入库内容；
    隔离目录保证向量库为空时也能稳定返回兜底回复。
    重置 graph 模块缓存，避免上一测试模块的 compiled_graph 状态污染。
    """
    from app.agents import (
        dialog_agent as dialog_agent_module,
    )
    from app.agents import (
        graph as graph_module,
    )
    from app.agents import (
        knowledge_agent as knowledge_agent_module,
    )
    from app.agents import (
        llm_client as llm_client_module,
    )
    from app.agents import (
        orchestrator as orchestrator_module,
    )
    from app.agents import (
        rag_agent as rag_agent_module,
    )
    from app.core.session import session_manager
    from app.knowledge import (
        retriever as retriever_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 重置相关单例，确保新配置生效
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    session_manager.reset_all()

    yield

    # 恢复配置并清理单例，避免影响后续测试
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    knowledge_agent_module.reset_knowledge_agent()
    orchestrator_module.reset_orchestrator()
    dialog_agent_module.reset_dialog_agent()
    graph_module.reset_graph()
    session_manager.reset_all()


def test_chat_without_api_key_returns_401() -> None:
    """配置 API_KEY 后，无 Key 请求应返回 401。"""
    settings = get_settings()
    original_key = settings.API_KEY
    settings.API_KEY = "test-secret-key"
    try:
        client = TestClient(app)
        response = client.post("/api/v1/chat", json={"message": "你好"})
        assert response.status_code == 401
    finally:
        # 恢复原始配置，避免污染其他测试
        settings.API_KEY = original_key


def test_chat_with_valid_api_key_returns_rag_answer() -> None:
    """携带正确 API Key 应返回多 Agent 协同结果（含 intent/sources 等字段）。

    验证 chat 端点接入 run_graph 后的响应结构：
    - session_id 非空
    - reply 为润色后的真实回复，不再是占位文案
    - data 包含多 Agent 协同元信息（intent/sources/escalate_to_human/turn_count）
    """
    settings = get_settings()
    original_key = settings.API_KEY
    settings.API_KEY = "test-secret-key"
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/chat",
            json={"message": "你好"},
            headers={"X-API-Key": "test-secret-key"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "session_id" in body
        # 多 Agent 协同后 reply 应为润色后的真实回复，不再是占位文案
        assert "功能开发中" not in body["reply"]
        assert body["reply"]
        # data 字段应包含多 Agent 协同元信息
        assert "intent" in body["data"]
        assert "sources" in body["data"]
        assert "escalate_to_human" in body["data"]
        assert "turn_count" in body["data"]
        assert "failed_attempts" in body["data"]
        assert "emotion_score" in body["data"]
    finally:
        settings.API_KEY = original_key
