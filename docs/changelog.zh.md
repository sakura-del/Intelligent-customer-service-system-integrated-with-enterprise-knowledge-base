---
title: 更新日志
description: 项目版本更新记录，按版本倒序排列，包含功能新增、优化与修复。
weight: 140
---

# 更新日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 规范，版本号格式为 `MAJOR.MINOR.PATCH`。本页按版本倒序记录所有变更。

!!! info "版本说明"

    - **MAJOR**：不兼容的 API 变更
    - **MINOR**：向后兼容的功能新增
    - **PATCH**：向后兼容的 Bug 修复

---

## v0.4.0

**发布日期**：2026-07-03

**主题**：坐席辅助工作台 — 补齐人机协同闭环

### :rocket: 新增功能

- **8 个 `/api/v1/agent/*` 端点**：补齐转接后坐席侧工作台 API 支撑
  - `GET /sessions/pending`：待接入会话列表（按 `EscalationPriority` 降序）
  - `GET /sessions/{session_id}`：会话详情（含 `EscalationCard` + 完整 `history`）
  - `POST /sessions/{session_id}/accept`：坐席接手（CAS `pending → assigned`）
  - `POST /sessions/{session_id}/messages`：坐席发消息追加到 `history`
  - `POST /sessions/{session_id}/knowledge-recommend`：知识推荐辅助
  - `POST /sessions/{session_id}/business-assist`：业务查询辅助（含脱敏）
  - `POST /sessions/{session_id}/resolve`：标记已解决（CAS `assigned → resolved`）
  - `POST /sessions/{session_id}/solution`：录入方案沉淀回库

- **SessionManager 扩展**：4 个新字段 + 4 个 CAS 方法
  - 新增字段：`agent_status` / `assigned_agent_id` / `escalation_card` / `resolve_note`
  - 新增方法：`list_pending_sessions()` / `assign_agent()` / `resolve_session()` / `mark_pending()`
  - 全部采用 CAS（Compare-And-Swap）保证并发安全

- **escalate_node 联动**：转接发生时自动调用 `mark_pending` 写入 `agent_status="pending"`，缓存 `EscalationCard` 避免重复构建

### :test_tube: 测试

- 新增 **28 个测试用例**，覆盖 8 个端点的正常路径、边界场景（404/409/422）与并发接手 CAS

### :books: 文档

- 新增 [坐席辅助工作台教程](tutorials/agent-assist.md)
- 更新 [API 参考](api-reference.zh.md) 补充 8 个端点说明

??? info "完整端点清单"
    | 端点 | 方法 | 说明 |
    |------|------|------|
    | `/api/v1/agent/sessions/pending` | GET | 待接入列表 |
    | `/api/v1/agent/sessions/{id}` | GET | 会话详情 |
    | `/api/v1/agent/sessions/{id}/accept` | POST | 坐席接手 |
    | `/api/v1/agent/sessions/{id}/messages` | POST | 坐席发消息 |
    | `/api/v1/agent/sessions/{id}/knowledge-recommend` | POST | 知识推荐 |
    | `/api/v1/agent/sessions/{id}/business-assist` | POST | 业务辅助 |
    | `/api/v1/agent/sessions/{id}/resolve` | POST | 标记解决 |
    | `/api/v1/agent/sessions/{id}/solution` | POST | 方案沉淀 |

---

## v0.3.0

**发布日期**：2026-07-02

**主题**：Langfuse LLM 可观测性 — 全链路 trace 可视化

### :rocket: 新增功能

- **11 个 LLM 调用点标记 prompt name/version**：覆盖意图识别、查询改写、知识生成、对话润色等全部 LLM 调用
  - `recognize_intent_v1`：意图识别
  - `query_rewrite_v1`：查询改写
  - `knowledge_generate_v1`：知识问答生成
  - `dialog_polish_v1`：对话润色
  - 等 11 个 prompt 标记

- **trace 与 monitor 双写**：Langfuse trace 与本地 `Monitor` 同时记录，互为兜底
  - Langfuse：可视化全链路、token/cost/latency 自动上报
  - Monitor：本地 trace 摘要，通过 `/api/v1/monitor/traces` 查询

- **未配置自动降级 no-op**：`LANGFUSE_ENABLED=False` 或凭据为空时，所有 Langfuse 调用降级为空操作，不影响主链路性能

### :gear: 配置

新增 Langfuse 配置项（详见 [.env.example](https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base/blob/main/.env.example)）：

```bash
LANGFUSE_ENABLED=False        # 留空或 False 时全部降级 no-op
LANGFUSE_PUBLIC_KEY=          # 在 Langfuse Project Settings → API Keys 获取
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

### :test_tube: 测试

- 新增 Langfuse 集成测试，覆盖正常上报与降级场景

??? tip "降级触发条件"
    - `LANGFUSE_ENABLED=False`
    - `LANGFUSE_PUBLIC_KEY` 或 `LANGFUSE_SECRET_KEY` 为空
    - Langfuse 服务连接超时（默认 3 秒）

---

## v0.2.0

**发布日期**：2026-07-02

**主题**：流式首 Token 优化 — 用户感知等待从 7-8s 降至 <1s

### :rocket: 新增功能

- **HotQueryCache / ModelRouter / IntentCache 三层组合优化**：
  - `HotQueryCache`：高频查询缓存，命中后首 Token <100ms，跳过全部编排
  - `ModelRouter`：意图识别等轻量任务走小模型（成本约 1/10），主 LLM 仅用于生成
  - `IntentCache`：同意图复用，避免重复意图识别

- **千问 qwen-turbo 替代豆包 lite**：小模型默认改为千问，中文理解更优
  - `SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`
  - `SMALL_LLM_MODEL=qwen-turbo`

- **意图识别快通道**：闲聊/转人工等高频意图通过关键词命中跳过 LLM 意图识别，首 Token <200ms

- **非知识问答意图流式化**：chitchat / business_query 等意图按句末标点切片流式吐出，而非等完整生成后单 token 输出

- **首 Token 耗时监控**：新增 `stream_first_token` 埋点，通过 `/api/v1/performance/metrics` 查询 avg/p95

### :zap: 性能提升

| 指标 | 优化前 | 优化后 | 提升幅度 |
|------|--------|--------|----------|
| 首 Token 时间（知识问答） | ~3s | <1s | 67% ↓ |
| 首 Token 时间（闲聊快通道） | ~3s | <200ms | 93% ↓ |
| 首 Token 时间（缓存命中） | ~3s | <100ms | 97% ↓ |
| 同步端点 P95 | 7.94s | 2.27s | 71% ↓ |

### :test_tube: 测试

- 新增首 Token 时间、快通道流式、缓存命中测试

??? info "三层缓存协同"
    ```mermaid
    flowchart TD
        A[请求] --> B{HotQueryCache 命中?}
        B -- 是 --> C[直接返回, 首 Token <100ms]
        B -- 否 --> D{IntentCache 命中?}
        D -- 是 --> E[跳过意图识别]
        D -- 否 --> F[ModelRouter 路由到小模型]
        F --> G[小模型意图识别]
        E --> H[混合检索 + 生成]
        G --> H
        H --> I[写入 HotQueryCache]
        I --> J[返回结果]
    ```

---

## v0.1.0

**发布日期**：2026-06

**主题**：初始版本 — 多 Agent 协同 + RAG 知识增强的智能客服系统

### :rocket: 核心功能

- **多 Agent 协同架构**：基于 LangGraph 的「1+5」架构
  - 1 个调度 Agent（OrchestratorAgent）：意图识别、路由分发、兜底聚合
  - 5 个专业 Agent：知识检索（KnowledgeAgent）/ 业务查询（BusinessAgent）/ 情感分析（EmotionAgent）/ 工单处理（TicketAgent）/ 对话润色（DialogAgent）
  - LangGraph 不可用时自动降级同步编排

- **混合检索 + RAG**：
  - Query 改写 → 向量检索 + BM25 双路召回 → RRF 融合 → Reranker 重排序 → LLM 生成
  - 相似度低于阈值不强行回答，避免幻觉
  - Recall@5 = 1.0，Hit Rate = 0.9333，幻觉率 = 0.0

- **人工转接闭环**：
  - 情绪敏感 / 连续失败 / 用户主动要求触发转接
  - 生成 `EscalationCard` 含转接原因、优先级、上下文摘要
  - 工作时间段约束（`WORKING_HOURS_START` / `WORKING_HOURS_END`）

- **知识库治理**：
  - 文档入库流水线（PDF/Word/Markdown/HTML 解析）
  - 质量校验（去重、术语、敏感词）
  - 版本管理与回滚
  - 全量/增量/实时三种更新机制

- **业务系统集成**：
  - 订单/会员/退换货/账户 API 适配器框架
  - 身份校验、手机号脱敏、写操作二次确认
  - mock / http 双模式，开箱即用

### :gear: 端点清单（v0.1.0）

| 模块 | 端点数 | 说明 |
|------|--------|------|
| 对话 | 2 | `/chat` + `/chat/stream` |
| 知识库 | 9 | 入库/统计/文档管理/质量/版本/灰度 |
| 人工转接 | 3 | 方案录入/审核/入库 |
| 文档更新 | 4 | 全量/增量/实时/状态 |
| 工单挖掘 | 2 | 触发/状态 |
| 检索调优 | 3 | 查询/更新/重置 |
| 检索评测 | 3 | 运行/列表/详情 |
| 性能监控 | 3 | 指标/缓存/失效 |
| 可观测性 | 5 | 熔断/告警/健康/Token |
| 监控 | 5 | 概览/trace/agent/会话 |
| 运营 | 6 | 实验/看板/检查清单 |
| 健康检查 | 1 | 探活 |
| 网关 | 1 | 多渠道接入 |

### :test_tube: 测试

- 初始测试用例 **640+ 个**，覆盖核心链路与边界场景

### :books: 文档

- 初始文档体系：快速开始、安装指南、配置说明、架构设计、使用教程

??? success "v0.1.0 性能指标（真实 LLM 验证）"
    | 指标 | 目标 | 实测 | 达标 |
    |------|------|------|------|
    | Recall@5 | ≥ 0.85 | 1.0 | ✅ |
    | Hit Rate | ≥ 0.90 | 0.9333 | ✅ |
    | 幻觉率 | ≤ 0.10 | 0.0 | ✅ |
    | 独立解决率 | ≥ 60% | 80% | ✅ |
    | 平均响应时间 | ≤ 3s | 2.27s | ✅ |

---

## 版本规划

??? info "下一版本 v0.5.0 规划（草案）"
    - 前端坐席工作台界面（消费 8 个 agent 端点）
    - 知识库管理后台 UI
    - 多语言支持（英文）
    - Elasticsearch 全文检索集成

---

## 相关文档

- [贡献指南](contributing.zh.md)：版本发布流程
- [API 参考](api-reference.zh.md)：当前完整端点
- [架构设计](architecture.md)：系统整体架构
