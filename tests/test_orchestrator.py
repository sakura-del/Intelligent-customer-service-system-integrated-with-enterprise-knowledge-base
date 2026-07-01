"""调度 Agent 端到端测试。

覆盖意图识别、路由分发、结果整合、兜底处理与调度规则：
1. 知识问答意图 → 路由到 RAGAgent（复用 tests/sample_data 入库）
2. 闲聊意图 → chitchat 处理
3. 含脏话 → emotion_sensitive + 情绪标记
4. unknown → 兜底引导
5. 连续失败 → escalate_to_human=True
6. 业务查询/工单 → 多子任务拆解

测试隔离：使用独立 chroma 目录避免与其他测试模块冲突，
重置相关单例让配置生效，注入 fake LLM/RAGAgent 控制特定路径。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, List

import pytest

from app.schemas.chat import RAGAnswer
from app.schemas.orchestrator import Intent, OrchestratorResult

# 测试用独立持久化目录，避免污染正式环境与其他测试模块
TEST_PERSIST_DIR = "./tests/_chroma_data_orchestrator"
SAMPLE_DIR = Path(__file__).parent / "sample_data"
SAMPLE_FAQ = SAMPLE_DIR / "faq.md"
SAMPLE_MANUAL = SAMPLE_DIR / "product_manual.md"
SAMPLE_POLICY = SAMPLE_DIR / "return_policy.md"


class FakeLLMClient:
    """可控的 LLM 客户端：用于测试 LLM 意图识别路径。

    is_mock=False 触发 _llm_based_intent 分支，
    chat 返回预设 JSON 串，便于断言意图识别与解析逻辑。
    """

    def __init__(self, intent_json: str) -> None:
        self._intent_json = intent_json
        self.is_mock = False

    def chat(
        self,
        messages: List[dict],
        temperature: float = 0.3,
        **kwargs: Any,
    ) -> str:
        return self._intent_json


class FakeRAGAgent:
    """可控的 RAGAgent：用于测试未解决/转人工场景。

    固定返回 hit=False，让 _handle_knowledge_qa 产出未命中标记，
    触发 _is_resolved=False 以累加 failed_attempts。
    """

    def __init__(self, hit: bool = False, answer: str = "") -> None:
        self._hit = hit
        self._answer = answer

    def answer(
        self,
        question: str,
        session_id: str | None = None,
        top_k: int | None = None,
    ) -> RAGAnswer:
        return RAGAnswer(
            answer=self._answer or "[未命中]知识库中未找到相关内容。",
            sources=[],
            retrieved_chunks=[],
            confidence=0.0,
            hit=self._hit,
        )


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_ingest():
    """模块级 fixture：隔离 ChromaDB 目录并入库三份测试文档。

    复用 test_rag.py 的隔离策略：清空目录 → 重置单例 → 入库文档，
    保证 knowledge_qa 测试能在真实向量库上验证路由到 RAGAgent。
    """
    from app.core.config import get_settings
    from app.knowledge import embeddings as embeddings_module
    from app.knowledge import retriever as retriever_module
    from app.knowledge import vectorstore as vectorstore_module
    from app.knowledge.pipeline import ingest_document

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_threshold = settings.SIMILARITY_THRESHOLD
    original_llm_key = settings.LLM_API_KEY
    original_small_key = settings.SMALL_LLM_API_KEY
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR
    # 强制 mock 模式与小模型不可用，避免 .env 配置真实 key 时
    # _llm_based_intent 走 ModelRouter 调用真实千问，导致 fake LLM 注入失效
    settings.LLM_API_KEY = ""
    settings.SMALL_LLM_API_KEY = ""

    # 清理上次测试残留，保证入库从零开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # fallback 模式下 hash 向量无语义能力，阈值降到 0 让 top_k 全返回
    embedding_service = embeddings_module.get_embedding_service()
    if embedding_service.mode == "fallback":
        settings.SIMILARITY_THRESHOLD = 0.0
    else:
        settings.SIMILARITY_THRESHOLD = 0.6

    # 重置单例让新配置生效
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()

    # 入库三份文档，供 knowledge_qa 测试检索
    for sample_path, knowledge_type in [
        (SAMPLE_FAQ, "faq"),
        (SAMPLE_MANUAL, "doc"),
        (SAMPLE_POLICY, "policy"),
    ]:
        result = ingest_document(
            sample_path,
            metadata={"knowledge_type": knowledge_type},
        )
        assert result.error is None, f"入库 {sample_path.name} 失败：{result.error}"
        assert result.total_chunks > 0, f"{sample_path.name} 切分后无 chunk"

    yield

    # 恢复配置并清理单例，避免影响后续测试
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.SIMILARITY_THRESHOLD = original_threshold
    settings.LLM_API_KEY = original_llm_key
    settings.SMALL_LLM_API_KEY = original_small_key
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()

    # 重置 Agent 单例，避免 mock/注入状态泄漏
    from app.agents import llm_client as llm_client_module
    from app.agents import rag_agent as rag_agent_module

    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    llm_client_module.reset_small_llm_client()


@pytest.fixture(autouse=True)
def _reset_orchestrator_session():
    """每个用例前重置 OrchestratorAgent 单例与会话状态。

    避免上一个用例的 failed_attempts 累计污染下一个用例的转人工判断。
    """
    from app.agents import orchestrator as orchestrator_module

    orchestrator_module.reset_orchestrator()
    yield
    orchestrator_module.reset_orchestrator()


def _make_agent_with_mock_llm():
    """构造 mock LLM 模式的 OrchestratorAgent（走关键词规则）。

    显式注入 _MockLLM，保证测试在真实 LLM_API_KEY 已配置的环境下
    仍走关键词规则路径，结果稳定可断言。
    """
    from app.agents.llm_client import LLMClient, _MockLLM
    from app.agents.orchestrator import OrchestratorAgent

    client = LLMClient()
    # 强制切到 mock，避免真实 LLM 调用导致意图识别结果不稳定
    client._mock = _MockLLM(reason="测试强制 mock")
    return OrchestratorAgent(llm_client=client)


def test_knowledge_qa_routes_to_rag_agent():
    """知识问答意图应路由到 RAGAgent 并返回命中回复。"""
    agent = _make_agent_with_mock_llm()
    result = agent.orchestrate("忘记登录密码怎么办？")

    assert result.intent == Intent.KNOWLEDGE_QA
    # 子任务应路由到 knowledge_qa agent
    assert any(task.agent_name == "knowledge_qa" for task in result.sub_tasks)
    # mock 模式下 RAG 应命中并返回非空回复
    assert result.reply
    assert "开发中" not in result.reply
    # 未命中知识库标记不应出现（faq.md 中有忘记密码相关内容）
    assert "[未命中知识库]" not in result.reply
    assert result.escalate_to_human is False


def test_chitchat_intent():
    """闲聊意图应路由到 chitchat handler 并返回问候。"""
    agent = _make_agent_with_mock_llm()
    result = agent.orchestrate("你好")

    assert result.intent == Intent.CHITCHAT
    assert result.sub_tasks[0].agent_name == "chitchat"
    # chitchat handler 应返回问候语
    assert "您好" in result.reply or "高兴" in result.reply


def test_emotion_sensitive_with_offensive_word():
    """含脏话应识别为情绪敏感并优先处理。"""
    agent = _make_agent_with_mock_llm()
    result = agent.orchestrate("你们这垃圾产品真差劲")

    assert result.intent == Intent.EMOTION_SENSITIVE
    # 情绪分应超过阈值
    assert result.metadata.get("emotion_score", 0) >= 4
    # 情绪处理应优先安抚
    assert "抱歉" in result.reply or "理解" in result.reply
    # need_emotion_check 应在意图识别阶段被标记
    assert result.sub_tasks[0].agent_name == "emotion_sensitive"


def test_unknown_fallback_with_fake_llm():
    """unknown 意图应返回兜底引导语。

    注入 fake LLM 返回 unknown 意图，验证 LLM 路径解析与兜底处理。
    """
    from app.agents.orchestrator import OrchestratorAgent

    fake_llm = FakeLLMClient(
        '{"intent": "unknown", "confidence": 0.2, '
        '"sub_tasks": [], "need_emotion_check": false}'
    )
    agent = OrchestratorAgent(llm_client=fake_llm)
    result = agent.orchestrate("xyzrandom无意义输入")

    assert result.intent == Intent.UNKNOWN
    # 兜底引导语应包含引导提示
    assert "尝试" in result.reply or "转人工" in result.reply
    assert result.escalate_to_human is False  # 首轮未达阈值


def test_escalate_after_consecutive_failures():
    """连续 2 轮未解决应自动标记转人工。

    注入 FakeRAGAgent 固定返回 hit=False，模拟知识库未命中场景，
    验证 failed_attempts 累加与 escalate_to_human 触发。
    显式注入 mock LLM，避免真实 LLM 把"查我的订单物流"识别为
    business_query 导致 failed_attempts 计数与测试期望不一致。
    """
    from app.agents.llm_client import LLMClient, _MockLLM
    from app.agents.orchestrator import OrchestratorAgent

    fake_rag = FakeRAGAgent(hit=False)
    mock_client = LLMClient()
    mock_client._mock = _MockLLM(reason="测试强制 mock")
    agent = OrchestratorAgent(rag_agent=fake_rag, llm_client=mock_client)
    session_id = "test-escalate-session"

    # 第一轮：未命中，failed_attempts=1，不应转人工
    result1 = agent.orchestrate("查我的订单物流", session_id=session_id)
    assert result1.failed_attempts == 1
    assert result1.escalate_to_human is False

    # 第二轮：仍未解决，failed_attempts=2，应触发转人工
    result2 = agent.orchestrate("还是没解决", session_id=session_id)
    assert result2.failed_attempts >= 2
    assert result2.escalate_to_human is True
    # 转人工后回复应为转接话术
    assert "转接人工" in result2.reply or "人工客服" in result2.reply


def test_resolved_resets_failed_attempts():
    """解决后应清零失败计数，避免历史失败累计误触发转人工。

    用 knowledge_qa 意图问题测试：第一轮 RAG 未命中视为未解决，
    第二轮切换为命中的 RAGAgent，验证 failed_attempts 清零。
    显式注入 mock LLM，保证意图识别走关键词规则稳定命中 knowledge_qa。
    """
    from app.agents.llm_client import LLMClient, _MockLLM
    from app.agents.orchestrator import OrchestratorAgent

    fake_rag_fail = FakeRAGAgent(hit=False)
    mock_client = LLMClient()
    mock_client._mock = _MockLLM(reason="测试强制 mock")
    agent = OrchestratorAgent(rag_agent=fake_rag_fail, llm_client=mock_client)
    session_id = "test-reset-session"

    # 第一轮：knowledge_qa 检索未命中，视为未解决
    result1 = agent.orchestrate("忘记登录密码怎么办？", session_id=session_id)
    assert result1.failed_attempts == 1
    assert result1.escalate_to_human is False

    # 第二轮切换为命中场景：注入 hit=True 的 RAGAgent
    agent._rag_agent = FakeRAGAgent(hit=True, answer="您可以通过邮箱重置密码。")
    result2 = agent.orchestrate("忘记登录密码怎么办？", session_id=session_id)
    assert result2.failed_attempts == 0
    assert result2.escalate_to_human is False


def test_business_query_keyword():
    """订单类问题应识别为业务查询意图。"""
    agent = _make_agent_with_mock_llm()
    result = agent.orchestrate("查我的订单物流状态")

    assert result.intent == Intent.BUSINESS_QUERY
    assert result.sub_tasks[0].agent_name == "business_query"
    # 业务查询能力开发中，应返回占位文案
    assert "开发中" in result.reply


def test_business_query_with_ticket_subtasks():
    """退货退款应拆解为 business_query + ticket 双子任务。"""
    agent = _make_agent_with_mock_llm()
    result = agent.orchestrate("我要退货退款")

    # 主意图为业务查询（关键词规则：退货退款同时命中业务+工单）
    assert result.intent in (Intent.BUSINESS_QUERY, Intent.TICKET)
    agent_names = {task.agent_name for task in result.sub_tasks}
    assert "business_query" in agent_names
    assert "ticket" in agent_names


def test_multiple_subtasks_aggregated_in_reply():
    """多子任务结果应按序号拼接整合到最终回复。"""
    agent = _make_agent_with_mock_llm()
    result = agent.orchestrate("我要退货退款")

    # 多子任务时回复应包含序号标注
    assert "[1]" in result.reply
    assert "[2]" in result.reply


def test_llm_intent_recognition_parses_valid_json():
    """LLM 返回合法 JSON 时应正确解析为 IntentResult。"""
    from app.agents.orchestrator import OrchestratorAgent

    fake_llm = FakeLLMClient(
        '{"intent": "ticket", "confidence": 0.9, '
        '"sub_tasks": [{"agent_name": "ticket", "input": "我要投诉"}], '
        '"need_emotion_check": false}'
    )
    agent = OrchestratorAgent(llm_client=fake_llm)
    result = agent.orchestrate("我要投诉")

    assert result.intent == Intent.TICKET
    assert result.sub_tasks[0].agent_name == "ticket"
    assert result.sub_tasks[0].result is not None


def test_llm_intent_recognition_fallback_on_invalid_json():
    """LLM 返回非 JSON 时应降级到关键词规则。"""
    from app.agents.orchestrator import OrchestratorAgent

    fake_llm = FakeLLMClient("这不是一个JSON")
    agent = OrchestratorAgent(llm_client=fake_llm)
    result = agent.orchestrate("你好")

    # 降级到关键词规则，应识别为闲聊
    assert result.intent == Intent.CHITCHAT
    assert "您好" in result.reply


def test_emotion_override_for_business_query_with_offensive():
    """业务问题含脏话应被覆盖为情绪敏感，优先安抚。"""
    agent = _make_agent_with_mock_llm()
    result = agent.orchestrate("查订单，你们这垃圾系统")

    # 应被覆盖为情绪敏感
    assert result.intent == Intent.EMOTION_SENSITIVE
    assert result.metadata.get("emotion_score", 0) >= 4
    # 第一个子任务应为情绪处理
    assert result.sub_tasks[0].agent_name == "emotion_sensitive"
