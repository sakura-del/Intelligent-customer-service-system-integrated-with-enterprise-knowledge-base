"""历史工单知识挖掘测试（Task 18）。

覆盖范围：
- IntentTagger：各分类关键词匹配、缓存命中、回退默认意图
- AnswerExtractor：手机号/身份证/订单号/邮箱脱敏、空白归一、空字符串
- TicketMiner：入库、去重、状态过滤、时间过滤、降级、并发安全
- API 端点：POST /tickets、GET /status

测试隔离：独立 chroma 目录 + 模块级 fixture 重置 vectorstore/embeddings/ticket_store/ticket_miner 单例。
"""
from __future__ import annotations

import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 测试用独立持久化目录，避免污染其他测试模块
TEST_PERSIST_DIR = "./tests/_chroma_data_ticket_miner"


# ----------------------------------------------------------------------
# 模块级 fixture：隔离向量库目录并重置相关单例
# ----------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _isolate_chroma_and_singletons():
    """模块级 fixture：隔离向量库目录并重置挖掘相关单例。

    覆盖：vectorstore / embeddings / ticket_store / ticket_miner，
    保证模块内全部用例共享同一份干净状态，且不污染其他测试模块。
    """
    from app.core.config import get_settings
    from app.agents import ticket_store as ticket_store_module
    from app.knowledge import (
        embeddings as embeddings_module,
    )
    from app.knowledge import (
        ticket_miner as ticket_miner_module,
    )
    from app.knowledge import (
        vectorstore as vectorstore_module,
    )

    settings = get_settings()
    original_persist_dir = settings.CHROMA_PERSIST_DIR
    settings.CHROMA_PERSIST_DIR = TEST_PERSIST_DIR

    # 清理上次残留，保证入库从零开始
    persist_path = Path(TEST_PERSIST_DIR)
    if persist_path.exists():
        shutil.rmtree(persist_path, ignore_errors=True)

    # 重置单例，确保新配置生效
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    ticket_store_module.reset_ticket_store()
    ticket_miner_module.reset_ticket_miner()

    yield

    # 测试结束恢复配置并清理单例，避免影响后续测试模块
    settings.CHROMA_PERSIST_DIR = original_persist_dir
    vectorstore_module.reset_vector_store()
    embeddings_module.reset_embedding_service()
    ticket_store_module.reset_ticket_store()
    ticket_miner_module.reset_ticket_miner()


@pytest.fixture()
def fresh_store():
    """每个用例独立的 TicketStore：清空单例并重建，避免用例间数据污染。

    工单挖掘通过 get_ticket_store() 取全局单例，因此这里必须重置单例
    而非直接 new 一个实例。
    """
    from app.agents.ticket_store import get_ticket_store, reset_ticket_store

    reset_ticket_store()
    store = get_ticket_store()
    yield store
    # 用例结束清空数据，进一步降低用例间影响
    store.reset_all()


@pytest.fixture()
def fresh_miner():
    """每个用例独立的 TicketMiner，重置单例并清空意图缓存。"""
    from app.knowledge.ticket_miner import (
        get_ticket_miner,
        reset_ticket_miner,
    )

    reset_ticket_miner()
    miner = get_ticket_miner()
    yield miner
    reset_ticket_miner()


def _create_ticket(
    store,
    description: str,
    category: str = "after_sale",
    priority: str = "medium",
    status: str = "resolved",
    title: str = "测试工单",
    related_order: str = "ORDER123456",
    contact: str = "13800138000",
):
    """辅助：在 store 中创建一条工单并切换到指定状态。

    TicketStore.create_ticket 默认 pending，需要手动 update_status。
    返回创建后的 Ticket。
    """
    from app.schemas.ticket import TicketCategory, TicketPriority, TicketStatus

    ticket = store.create_ticket(
        user_id="user_test",
        title=title,
        description=description,
        category=TicketCategory(category),
        priority=TicketPriority(priority),
        related_order=related_order,
        contact=contact,
    )
    if status != "pending":
        store.update_status(ticket.ticket_id, TicketStatus(status))
    return ticket


# ----------------------------------------------------------------------
# IntentTagger 单元测试
# ----------------------------------------------------------------------


def test_intent_tagger_return_refund():
    """「退货」关键词应标注为「退货咨询」。"""
    from app.knowledge.ticket_miner import IntentTagger

    tagger = IntentTagger()
    assert tagger.tag("我想退货，商品没拆封", "after_sale") == "退货咨询"


def test_intent_tagger_logistics():
    """「物流/快递」关键词应标注为「物流查询」。"""
    from app.knowledge.ticket_miner import IntentTagger

    tagger = IntentTagger()
    assert tagger.tag("快递一直没到，请帮我查下物流", "logistics") == "物流查询"


def test_intent_tagger_payment():
    """「扣款」关键词应标注为「支付问题」。"""
    from app.knowledge.ticket_miner import IntentTagger

    tagger = IntentTagger()
    assert tagger.tag("重复扣款了，麻烦处理一下", "account") == "支付问题"


def test_intent_tagger_complaint():
    """「投诉」关键词应标注为「产品投诉」。"""
    from app.knowledge.ticket_miner import IntentTagger

    tagger = IntentTagger()
    assert tagger.tag("我要投诉客服态度差", "complaint") == "产品投诉"


def test_intent_tagger_fallback_to_category_default():
    """未命中关键词时应回退到 category 默认意图。"""
    from app.knowledge.ticket_miner import IntentTagger

    tagger = IntentTagger()
    # 无任何关键词命中
    intent = tagger.tag("hello world 12345", "after_sale")
    assert intent == "售后咨询"


def test_intent_tagger_cache_hit():
    """相同 description+category 第二次调用应命中缓存返回相同结果。"""
    from app.knowledge.ticket_miner import IntentTagger

    tagger = IntentTagger()
    desc = "我要退货，麻烦尽快处理"
    first = tagger.tag(desc, "after_sale")
    second = tagger.tag(desc, "after_sale")
    assert first == second == "退货咨询"
    # 缓存应非空
    assert len(tagger._cache) > 0


# ----------------------------------------------------------------------
# AnswerExtractor 单元测试
# ----------------------------------------------------------------------


def test_answer_extractor_desensitize_phone():
    """手机号应被替换为 [手机号] 占位符。"""
    from app.knowledge.ticket_miner import AnswerExtractor

    extractor = AnswerExtractor()
    text = "请联系用户 13800138000 确认退货地址"
    result = extractor.extract(text)
    assert "13800138000" not in result
    assert "[手机号]" in result


def test_answer_extractor_desensitize_email():
    """邮箱应被替换为 [邮箱] 占位符。"""
    from app.knowledge.ticket_miner import AnswerExtractor

    extractor = AnswerExtractor()
    text = "请发送凭证到 user@example.com 邮箱"
    result = extractor.extract(text)
    assert "user@example.com" not in result
    assert "[邮箱]" in result


def test_answer_extractor_desensitize_id_card():
    """身份证号应被替换为 [身份证] 占位符。"""
    from app.knowledge.ticket_miner import AnswerExtractor

    extractor = AnswerExtractor()
    text = "请提供身份证 110101199003073928 用于核验"
    result = extractor.extract(text)
    assert "110101199003073928" not in result
    assert "[身份证]" in result


def test_answer_extractor_desensitize_order():
    """订单号应被替换为 [订单号] 占位符。"""
    from app.knowledge.ticket_miner import AnswerExtractor

    extractor = AnswerExtractor()
    text = "订单号：ORD20240601000123 已发起退款"
    result = extractor.extract(text)
    assert "ORD20240601000123" not in result
    assert "[订单号]" in result


def test_answer_extractor_empty_string():
    """空字符串或仅空白应返回空串。"""
    from app.knowledge.ticket_miner import AnswerExtractor

    extractor = AnswerExtractor()
    assert extractor.extract("") == ""
    assert extractor.extract("   \n\n  ") == ""


# ----------------------------------------------------------------------
# TicketMiner 集成测试
# ----------------------------------------------------------------------


def test_miner_empty_store_returns_empty_report(fresh_miner):
    """ticket_store 为空时应返回空报告（降级策略）。"""
    report = fresh_miner.mine()
    assert report.total_tickets == 0
    assert report.processed == 0
    assert report.ingested == 0
    assert report.items == []


def test_miner_ingests_single_ticket(fresh_store, fresh_miner):
    """单条有效工单应被标注、抽取并成功入库。"""
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封",
        status="resolved",
    )

    report = fresh_miner.mine(status="resolved")
    assert report.total_tickets == 1
    assert report.processed == 1
    assert report.ingested == 1
    assert len(report.items) == 1
    item = report.items[0]
    assert item.intent == "退货咨询"
    assert item.ingested is True
    assert item.skip_reason is None


def test_miner_dedup_same_intent_similar_answer(fresh_store, fresh_miner):
    """相同意图且答案相似的工单应去重，仅入库一条。"""
    # 两条完全相同描述的工单：embedding 完全一致，相似度=1.0
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封，请尽快处理退款",
        status="resolved",
    )
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封，请尽快处理退款",
        status="resolved",
    )

    report = fresh_miner.mine(status="resolved")
    assert report.total_tickets == 2
    assert report.ingested == 1
    assert report.deduped == 1
    # 第二条应被标记为 duplicate
    dup_item = next(it for it in report.items if not it.ingested)
    assert dup_item.skip_reason == "duplicate"


def test_miner_status_filter(fresh_store, fresh_miner):
    """status 过滤应仅挖掘指定状态的工单。"""
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封",
        status="resolved",
    )
    _create_ticket(
        fresh_store,
        description="另一个问题，请处理",
        status="pending",
    )

    # 仅挖掘 resolved
    report = fresh_miner.mine(status="resolved")
    assert report.total_tickets == 1
    assert report.items[0].source_ticket_id.startswith("TK-")


def test_miner_invalid_status_falls_back_to_all(fresh_store, fresh_miner):
    """非法 status 应视为不过滤，返回全部工单（降级策略）。"""
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封",
        status="resolved",
    )

    report = fresh_miner.mine(status="not_a_valid_status")
    # 非法状态降级为不过滤，应扫到该工单
    assert report.total_tickets == 1


def test_miner_time_range_filter(fresh_store, fresh_miner):
    """start_time / end_time 应按 created_at 闭区间过滤。"""
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封",
        status="resolved",
    )

    # 用一个早于工单创建时间的 end_time，应过滤掉该工单
    end_time = datetime.now(timezone.utc) - timedelta(days=1)
    report = fresh_miner.mine(end_time=end_time)
    assert report.total_tickets == 1  # 拉取总数仍计入
    # 但扫描后无入库（被时间过滤跳过）
    assert report.ingested == 0
    assert report.processed == 0


def test_miner_skip_short_answer(fresh_store, fresh_miner):
    """答案过短（< MIN_ANSWER_LENGTH）应跳过不入库。"""
    # 描述极短，抽取后长度不足
    _create_ticket(
        fresh_store,
        description="hi",
        status="resolved",
    )

    report = fresh_miner.mine(status="resolved")
    assert report.total_tickets == 1
    assert report.processed == 0
    assert report.ingested == 0
    assert report.skipped == 1


def test_miner_records_last_report(fresh_store, fresh_miner):
    """挖掘完成后 get_last_report 应返回最近一次报告。"""
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封",
        status="resolved",
    )

    fresh_miner.mine(status="resolved")
    last = fresh_miner.get_last_report()
    assert last is not None
    assert last.total_tickets == 1
    assert last.finished_at is not None


def test_miner_concurrent_safe(fresh_store, fresh_miner):
    """多线程并发触发挖掘不应抛异常，且至少有一次成功写入报告。

    RLock 保护 _last_report 与缓存，并发场景下最终状态一致。
    """
    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封",
        status="resolved",
    )

    errors: List[Exception] = []

    def _run():
        try:
            fresh_miner.mine(status="resolved")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发挖掘出现异常：{errors}"
    last = fresh_miner.get_last_report()
    assert last is not None
    assert last.total_tickets == 1


def test_miner_metadata_written_to_vectorstore(fresh_store, fresh_miner):
    """入库后向量库中应能查到 knowledge_type=ticket 的条目。"""
    from app.knowledge.vectorstore import get_vector_store

    _create_ticket(
        fresh_store,
        description="我要退货，商品没拆封",
        status="resolved",
    )
    fresh_miner.mine(status="resolved")

    store = get_vector_store()
    chunks = store.get_all_chunks()
    ticket_chunks = [
        c for c in chunks if c.get("metadata", {}).get("knowledge_type") == "ticket"
    ]
    assert len(ticket_chunks) >= 1
    meta = ticket_chunks[0]["metadata"]
    assert meta["intent"] == "退货咨询"
    assert meta["source_ticket_id"].startswith("TK-")
    assert meta["category"] == "after_sale"


# ----------------------------------------------------------------------
# API 端点测试
# ----------------------------------------------------------------------


@pytest.fixture()
def api_client():
    """提供挂载 mining 路由的独立 FastAPI TestClient。

    按任务要求，测试中通过 app.include_router 注册路由，
    不依赖 app.main.create_app，避免污染主应用配置。
    """
    app = FastAPI()
    from app.api.v1.mining import router as mining_router

    app.include_router(mining_router)
    return TestClient(app)


def test_api_mine_tickets_returns_report(api_client, fresh_store, fresh_miner):
    """POST /api/v1/mining/tickets 应返回挖掘报告。"""
    # 使用独特描述避免被 vector_store 全局去重命中前序测试遗留内容
    _create_ticket(
        fresh_store,
        description="我要退货 api 端点测试 20240601，商品没拆封",
        status="resolved",
    )

    response = api_client.post(
        "/api/v1/mining/tickets",
        json={"status": "resolved"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_tickets"] == 1
    assert body["ingested"] == 1
    assert body["items"][0]["intent"] == "退货咨询"


def test_api_mine_tickets_with_empty_body(api_client, fresh_store, fresh_miner):
    """POST /api/v1/mining/tickets 无 body 应触发全量挖掘。"""
    _create_ticket(
        fresh_store,
        description="我要退货 empty body 测试 20240602，商品没拆封",
        status="resolved",
    )

    response = api_client.post("/api/v1/mining/tickets")
    assert response.status_code == 200
    body = response.json()
    assert body["total_tickets"] == 1


def test_api_get_status_returns_last_report(api_client, fresh_store, fresh_miner):
    """GET /api/v1/mining/status 应返回最近一次挖掘报告。"""
    _create_ticket(
        fresh_store,
        description="我要退货 status 端点测试 20240603，商品没拆封",
        status="resolved",
    )
    # 先触发一次挖掘
    api_client.post("/api/v1/mining/tickets", json={"status": "resolved"})

    response = api_client.get("/api/v1/mining/status")
    assert response.status_code == 200
    body = response.json()
    assert body["total_tickets"] == 1
    assert body["finished_at"] is not None


def test_api_get_status_empty_when_never_mined(api_client):
    """从未触发过挖掘时 GET /status 应返回空报告而非 404。"""
    # 重置 miner 单例确保无历史报告
    from app.knowledge.ticket_miner import reset_ticket_miner

    reset_ticket_miner()

    response = api_client.get("/api/v1/mining/status")
    assert response.status_code == 200
    body = response.json()
    assert body["total_tickets"] == 0
    assert body["items"] == []
