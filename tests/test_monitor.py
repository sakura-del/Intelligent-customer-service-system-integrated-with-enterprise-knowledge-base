"""Agent 监控面板测试。

覆盖 Task 9 的核心场景：
1. Monitor 单元能力：记录 step、trace 列表、详情、限长、agent 统计
2. 监控 API 端点：overview / traces / traces/{id} / agents / sessions 返回正确结构
3. run_graph 埋点：调用对话接口后 trace 与 agent 统计被正确采集
4. trace 限长生效：超过上限时按 FIFO 丢弃最旧

测试隔离：使用独立 chroma 目录，模块级 fixture 入库三份文档；
每个用例前重置 Monitor 与 SessionManager 状态，避免相互污染。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 测试用独立持久化目录，与其他测试模块隔离
TEST_PERSIST_DIR = "./tests/_chroma_data_monitor"
SAMPLE_DIR = Path(__file__).parent / "sample_data"
SAMPLE_FAQ = SAMPLE_DIR / "faq.md"
SAMPLE_MANUAL = SAMPLE_DIR / "product_manual.md"
SAMPLE_POLICY = SAMPLE_DIR / "return_policy.md"


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_ingest():
    """模块级 fixture：隔离 ChromaDB 目录并入库三份测试文档。

    重置所有相关单例让新配置生效；
    fallback embedding 模式下注入低阈值 KnowledgeAgent 单例，保证 rerank 后能命中。
    """
    from app.agents import (
        dialog_agent as dialog_agent_module,
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
    from app.core.config import get_settings
    from app.core.session import session_manager
    from app.knowledge import (
        bm25 as bm25_module,
    )
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        hybrid_retriever as hybrid_module,
    )
    from app.knowledge import (
        query_rewriter as rewriter_module,
    )
    from app.knowledge import (
        reranker as reranker_module,
    )
    from app.knowledge import (
        retriever as retriever_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )
    from app.knowledge.pipeline import ingest_document

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    original_threshold = settings.SIMILARITY_THRESHOLD
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # fallback 模式下 hash 向量无语义能力，阈值降到 0 让召回阶段不过滤
    embedding_service = embeddings_module.get_embedding_service()
    if embedding_service.mode == "fallback":
        settings.SIMILARITY_THRESHOLD = 0.0

    # 重置所有相关单例
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    rewriter_module.reset_query_rewriter()
    reranker_module.reset_reranker()
    knowledge_agent_module.reset_knowledge_agent()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    dialog_agent_module.reset_dialog_agent()
    orchestrator_module.reset_orchestrator()

    from app.agents import graph as graph_module
    from app.core.monitor import reset_monitor

    graph_module.reset_graph()
    reset_monitor()
    session_manager.reset_all()

    # 入库三份文档供 knowledge_qa 测试检索
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

    # fallback 模式下注入低阈值 KnowledgeAgent 单例
    if embedding_service.mode == "fallback":
        knowledge_agent_module._knowledge_agent = (
            knowledge_agent_module.KnowledgeAgent(score_threshold=0.0)
        )

    yield

    # 恢复配置并清理单例
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    settings.SIMILARITY_THRESHOLD = original_threshold
    vectorstore_module.reset_vector_store()
    retriever_module.reset_retriever()
    bm25_module.reset_bm25_retriever()
    hybrid_module.reset_hybrid_retriever()
    rewriter_module.reset_query_rewriter()
    reranker_module.reset_reranker()
    knowledge_agent_module.reset_knowledge_agent()
    rag_agent_module.reset_rag_agent()
    llm_client_module.reset_llm_client()
    dialog_agent_module.reset_dialog_agent()
    orchestrator_module.reset_orchestrator()
    graph_module.reset_graph()
    reset_monitor()
    session_manager.reset_all()


@pytest.fixture(autouse=True)
def _reset_monitor_per_test():
    """每个用例前重置 Monitor 与 SessionManager 状态。

    避免上一用例的 trace 累计污染下一用例的统计断言。
    """
    from app.core.monitor import get_monitor
    from app.core.session import session_manager

    get_monitor().reset()
    session_manager.reset_all()
    yield
    get_monitor().reset()
    session_manager.reset_all()


# ----------------------------------------------------------------------
# Monitor 单元能力测试
# ----------------------------------------------------------------------


def test_monitor_record_step_and_get_trace():
    """record_step 应正确写入 step，get_trace 返回详情含 steps。"""
    from app.core.monitor import get_monitor

    monitor = get_monitor()
    trace_id = monitor.start_trace("session-1", "你好")

    monitor.record_step(trace_id, "intent", "你好", "intent=chitchat", 1.5)
    monitor.record_step(trace_id, "route", "intent=chitchat", "next=agent", 0.5)
    monitor.record_agent_call(trace_id, "chitchat", "你好", "您好...", 2.0, True)
    monitor.record_step(trace_id, "chitchat", "你好", "您好...", 2.0)
    monitor.finish_trace(
        trace_id,
        intent="chitchat",
        final_reply="您好，很高兴为您服务",
        turn_count=1,
    )

    trace = monitor.get_trace(trace_id)
    assert trace is not None
    assert trace["trace_id"] == trace_id
    assert trace["session_id"] == "session-1"
    assert trace["intent"] == "chitchat"
    assert trace["status"] == "success"
    assert trace["final_reply"] == "您好，很高兴为您服务"
    # 应记录 3 个 step（intent / route / chitchat）
    assert len(trace["steps"]) == 3
    assert trace["steps"][0]["node"] == "intent"
    assert trace["steps"][1]["node"] == "route"
    assert trace["steps"][2]["node"] == "chitchat"
    # route_path 应与 step 节点链一致
    assert trace["route_path"] == ["intent", "route", "chitchat"]
    # sub_tasks 应记录 1 个 agent 调用
    assert len(trace["sub_tasks"]) == 1
    assert trace["sub_tasks"][0]["agent_name"] == "chitchat"
    assert trace["sub_tasks"][0]["success"] is True


def test_monitor_get_traces_returns_summary():
    """get_traces 应返回摘要列表（不含 steps），按时间倒序。"""
    from app.core.monitor import get_monitor

    monitor = get_monitor()
    tid1 = monitor.start_trace("s1", "msg1")
    monitor.finish_trace(tid1, intent="chitchat", final_reply="r1")
    tid2 = monitor.start_trace("s2", "msg2")
    monitor.finish_trace(tid2, intent="knowledge_qa", final_reply="r2")

    traces = monitor.get_traces(limit=10)
    assert len(traces) == 2
    # 最新的在前
    assert traces[0]["trace_id"] == tid2
    assert traces[1]["trace_id"] == tid1
    # 摘要不应包含 steps 字段
    assert "steps" not in traces[0]
    assert "sub_tasks" not in traces[0]
    # 应包含关键字段
    assert "trace_id" in traces[0]
    assert "intent" in traces[0]
    assert "route_path" in traces[0]
    assert "status" in traces[0]


def test_monitor_get_trace_not_exist_returns_none():
    """get_trace 对不存在的 trace_id 应返回 None。"""
    from app.core.monitor import get_monitor

    monitor = get_monitor()
    assert monitor.get_trace("non-existent-id") is None


def test_monitor_fail_trace_marks_failed():
    """fail_trace 应标记 status=failed 并记录 error。"""
    from app.core.monitor import get_monitor

    monitor = get_monitor()
    trace_id = monitor.start_trace("s1", "msg")
    monitor.fail_trace(trace_id, "something went wrong")

    trace = monitor.get_trace(trace_id)
    assert trace is not None
    assert trace["status"] == "failed"
    assert "something went wrong" in trace["error"]


def test_monitor_trace_limit_evicts_oldest():
    """trace 超过上限时应按 FIFO 丢弃最旧。"""
    from app.core.monitor import Monitor

    # 用小上限便于测试
    monitor = Monitor(max_traces=3)
    ids = []
    for i in range(5):
        tid = monitor.start_trace(f"s{i}", f"msg{i}")
        monitor.finish_trace(tid, intent="chitchat", final_reply=f"r{i}")
        ids.append(tid)

    traces = monitor.get_traces(limit=10)
    # 只保留最近 3 条
    assert len(traces) == 3
    # 最旧的 2 条应被淘汰
    assert monitor.get_trace(ids[0]) is None
    assert monitor.get_trace(ids[1]) is None
    # 最近的 3 条应仍存在
    assert monitor.get_trace(ids[2]) is not None
    assert monitor.get_trace(ids[3]) is not None
    assert monitor.get_trace(ids[4]) is not None


def test_monitor_agent_stats_aggregation():
    """get_agent_stats 应按 agent_name 聚合调用次数、平均耗时、成功率。"""
    from app.core.monitor import get_monitor

    monitor = get_monitor()
    trace_id = monitor.start_trace("s1", "msg")
    # chitchat 调用 2 次，1 次成功 1 次失败
    monitor.record_agent_call(trace_id, "chitchat", "in1", "out1", 10.0, True)
    monitor.record_agent_call(trace_id, "chitchat", "in2", "out2", 30.0, False)
    # knowledge_qa 调用 1 次，成功
    monitor.record_agent_call(trace_id, "knowledge_qa", "in3", "out3", 20.0, True)
    monitor.finish_trace(trace_id, intent="chitchat", final_reply="r")

    stats = monitor.get_agent_stats()
    stats_map = {s["name"]: s for s in stats}

    chitchat = stats_map["chitchat"]
    assert chitchat["total_calls"] == 2
    assert chitchat["success_count"] == 1
    assert chitchat["total_duration_ms"] == 40.0
    assert chitchat["avg_duration_ms"] == 20.0
    assert chitchat["success_rate"] == 0.5

    knowledge = stats_map["knowledge_qa"]
    assert knowledge["total_calls"] == 1
    assert knowledge["success_count"] == 1
    assert knowledge["avg_duration_ms"] == 20.0
    assert knowledge["success_rate"] == 1.0


def test_monitor_overview_aggregation():
    """get_overview 应返回总 trace 数、成功率、平均耗时、活跃会话数。"""
    from app.core.monitor import get_monitor

    monitor = get_monitor()
    # 2 条成功，1 条失败
    for i in range(2):
        tid = monitor.start_trace(f"s{i}", f"m{i}")
        monitor.finish_trace(tid, intent="chitchat", final_reply="r")
    tid_fail = monitor.start_trace("s2", "m2")
    monitor.fail_trace(tid_fail, "err")

    overview = monitor.get_overview()
    assert overview["total_traces"] == 3
    assert overview["success_count"] == 2
    assert overview["failed_count"] == 1
    # 成功率 2/3 ≈ 0.6667
    assert 0.6 < overview["success_rate"] < 0.7


def test_monitor_truncate_long_text():
    """长文本输入输出应被截断，避免内存膨胀。"""
    from app.core.monitor import get_monitor

    monitor = get_monitor()
    long_text = "x" * 500
    trace_id = monitor.start_trace("s1", long_text)
    monitor.record_step(trace_id, "intent", long_text, long_text, 1.0)
    monitor.finish_trace(trace_id, intent="chitchat", final_reply="r")

    trace = monitor.get_trace(trace_id)
    # message 应被截断（默认 200 字符 + "..."）
    assert len(trace["message"]) <= 205
    # step 的 input_summary / output_summary 也应被截断
    assert len(trace["steps"][0]["input_summary"]) <= 205
    assert len(trace["steps"][0]["output_summary"]) <= 205


def test_monitor_thread_safety():
    """多线程并发埋点应不丢数据、不崩溃。"""
    import threading

    from app.core.monitor import get_monitor

    monitor = get_monitor()
    trace_id = monitor.start_trace("s1", "msg")
    barrier = threading.Barrier(10)

    def worker(idx):
        barrier.wait()
        for i in range(20):
            monitor.record_step(trace_id, f"node_{idx}_{i}", "in", "out", 1.0)
            monitor.record_agent_call(
                trace_id, f"agent_{idx}", "in", "out", 1.0, True
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    monitor.finish_trace(trace_id, intent="chitchat", final_reply="r")
    trace = monitor.get_trace(trace_id)
    # 10 线程 × 20 步 = 200 step
    assert len(trace["steps"]) == 200
    # 10 线程 × 20 调用 = 200 agent_call
    assert len(trace["sub_tasks"]) == 200


# ----------------------------------------------------------------------
# 监控 API 端点测试
# ----------------------------------------------------------------------


def test_api_overview_returns_correct_structure():
    """GET /api/v1/monitor/overview 返回概览统计字段。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/monitor/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_traces" in body
    assert "success_count" in body
    assert "failed_count" in body
    assert "success_rate" in body
    assert "avg_duration_ms" in body
    assert "active_sessions" in body


def test_api_traces_returns_list():
    """GET /api/v1/monitor/traces 返回 trace 列表。"""
    from app.core.monitor import get_monitor
    from app.main import app

    monitor = get_monitor()
    tid = monitor.start_trace("s1", "msg")
    monitor.finish_trace(tid, intent="chitchat", final_reply="r")

    client = TestClient(app)
    resp = client.get("/api/v1/monitor/traces")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert body[0]["trace_id"] == tid


def test_api_trace_detail_returns_steps():
    """GET /api/v1/monitor/traces/{trace_id} 返回详情含 steps。"""
    from app.core.monitor import get_monitor
    from app.main import app

    monitor = get_monitor()
    tid = monitor.start_trace("s1", "msg")
    monitor.record_step(tid, "intent", "in", "out", 1.0)
    monitor.finish_trace(tid, intent="chitchat", final_reply="r")

    client = TestClient(app)
    resp = client.get(f"/api/v1/monitor/traces/{tid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == tid
    assert "steps" in body
    assert len(body["steps"]) == 1
    assert body["steps"][0]["node"] == "intent"


def test_api_trace_detail_not_found_returns_404():
    """不存在的 trace_id 应返回 404。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/monitor/traces/non-existent-id")
    assert resp.status_code == 404


def test_api_agents_returns_stats():
    """GET /api/v1/monitor/agents 返回 agent 统计列表。"""
    from app.core.monitor import get_monitor
    from app.main import app

    monitor = get_monitor()
    tid = monitor.start_trace("s1", "msg")
    monitor.record_agent_call(tid, "chitchat", "in", "out", 5.0, True)
    monitor.finish_trace(tid, intent="chitchat", final_reply="r")

    client = TestClient(app)
    resp = client.get("/api/v1/monitor/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # 应包含已注册的 agent（即使未调用也展示）
    names = [item["name"] for item in body]
    assert "chitchat" in names
    # 各字段齐全
    chitchat = next(item for item in body if item["name"] == "chitchat")
    assert chitchat["total_calls"] == 1
    assert chitchat["success_count"] == 1
    assert "avg_duration_ms" in chitchat
    assert "success_rate" in chitchat


def test_api_sessions_returns_list():
    """GET /api/v1/monitor/sessions 返回活跃会话列表。"""
    from app.core.session import session_manager
    from app.main import app

    session_manager.create_session(channel="web", user_id="u1")

    client = TestClient(app)
    resp = client.get("/api/v1/monitor/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert "session_id" in body[0]
    assert "turn_count" in body[0]
    assert "failed_attempts" in body[0]


# ----------------------------------------------------------------------
# run_graph 埋点集成测试
# ----------------------------------------------------------------------


def test_run_graph_produces_trace():
    """调用 run_graph 后应产生一条 trace，含完整步骤。"""
    from app.agents.graph import run_graph
    from app.core.monitor import get_monitor

    state = run_graph("你好")
    monitor = get_monitor()
    traces = monitor.get_traces(limit=10)

    assert len(traces) >= 1
    latest = traces[0]
    assert latest["session_id"] == state["session_id"]
    assert latest["intent"] == "chitchat"
    assert latest["status"] == "success"
    # route_path 至少包含 intent / route / chitchat / dialog
    assert "intent" in latest["route_path"]
    assert "route" in latest["route_path"]
    assert "chitchat" in latest["route_path"]
    assert "dialog" in latest["route_path"]


def test_run_graph_trace_detail_has_steps():
    """run_graph 产生的 trace 详情应含各节点 step 与 agent 调用。"""
    from app.agents.graph import run_graph
    from app.core.monitor import get_monitor

    state = run_graph("你好")
    monitor = get_monitor()
    traces = monitor.get_traces(limit=10)
    latest = traces[0]

    detail = monitor.get_trace(latest["trace_id"])
    assert detail is not None
    # steps 至少 4 个：intent / route / chitchat / dialog
    assert len(detail["steps"]) >= 4
    # sub_tasks 至少 1 个：chitchat agent 调用
    assert len(detail["sub_tasks"]) >= 1
    assert detail["sub_tasks"][0]["agent_name"] == "chitchat"


def test_run_graph_escalate_trace_recorded():
    """情绪敏感触发转人工时，trace 应记录 escalate 节点。"""
    from app.agents.graph import run_graph
    from app.core.monitor import get_monitor

    state = run_graph("你们这垃圾产品真差劲")
    assert state["escalate_to_human"] is True

    monitor = get_monitor()
    traces = monitor.get_traces(limit=10)
    latest = traces[0]
    assert latest["intent"] == "emotion_sensitive"
    assert latest["escalate_to_human"] is True
    # route_path 应包含 escalate
    assert "escalate" in latest["route_path"]


def test_run_graph_agent_stats_incremented():
    """run_graph 调用后 agent 统计应正确累计。"""
    from app.agents.graph import run_graph
    from app.core.monitor import get_monitor

    # 调用一次闲聊
    run_graph("你好")
    monitor = get_monitor()
    stats = monitor.get_agent_stats()
    stats_map = {s["name"]: s for s in stats}

    # chitchat agent 应至少调用 1 次
    assert stats_map["chitchat"]["total_calls"] >= 1
    assert stats_map["chitchat"]["success_count"] >= 1


# ----------------------------------------------------------------------
# 监控面板页面测试
# ----------------------------------------------------------------------


def test_monitor_page_accessible():
    """GET /monitor 应返回监控面板 HTML 页面。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/monitor")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    # 页面应包含关键元素
    assert "Agent 监控面板" in resp.text


def test_monitor_static_assets_accessible():
    """监控面板的静态资源应可访问。"""
    from app.main import app

    client = TestClient(app)
    for path in ["/static/monitor.html", "/static/monitor.js", "/static/monitor.css"]:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} 应可访问"


def test_index_page_has_monitor_link():
    """对话界面应包含监控面板入口链接。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/monitor" in resp.text
    assert "监控面板" in resp.text
