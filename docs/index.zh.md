---
hide:
  - navigation
  - toc
---

# 整合企业知识库的智能客服系统

<div class="hero" markdown>

# 整合企业知识库的智能客服系统

基于「多 Agent 分工协作 + RAG 知识增强」双轮驱动的企业级智能客服系统

<div class="badge-row">
  <span class="badge">:material-robot-happy: 多 Agent 协同</span>
  <span class="badge">:material-book-search: 混合检索 + RAG</span>
  <span class="badge">:material-rocket: 平均响应 ≤ 3s</span>
  <span class="badge">:material-account-tie: 坐席辅助工作台</span>
  <span class="badge">:material-chart-line: Langfuse 可观测性</span>
</div>

[立即开始 :material-arrow-right:](quick-start.md){: .md-button .md-button--primary}
[查看 GitHub :fontawesome-brands-github:](https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base){: .md-button}

</div>

---

## 核心能力

<div class="grid" markdown>

<div class="card" markdown>

### :material-robot-happy: 多 Agent 协同

基于 LangGraph 的「1+5」架构：1 个调度 Agent 协调 5 个专业 Agent（知识检索 / 业务查询 / 情感分析 / 工单处理 / 对话生成），LangGraph 不可用时自动降级同步编排。

[了解架构 :material-arrow-right:](architecture.md){: .md-button }

</div>

<div class="card" markdown>

### :material-book-search: 混合检索 + RAG

Query 改写 → 向量检索 + BM25 双路召回 → RRF 融合 → Reranker 重排序 → LLM 生成，相似度低于阈值不强答，Recall@5 = 1.0。

[查看检索流程 :material-arrow-right:](architecture/rag.md){: .md-button }

</div>

<div class="card" markdown>

### :material-account-tie: 坐席辅助工作台

转接后会话可被坐席接手，支持上下文延续、知识/业务辅助查询、方案沉淀回库，8 个 API 端点补齐人机协同短板。

[查看坐席端点 :material-arrow-right:](tutorials/agent-assist.md){: .md-button }

</div>

<div class="card" markdown>

### :material-rocket: 性能优化

HotQueryCache 热点缓存、ModelRouter 大小模型路由、IntentCache 同意图复用、意图识别快通道，知识问答首 Token <1s。

[查看性能优化 :material-arrow-right:](tutorials/performance.md){: .md-button }

</div>

<div class="card" markdown>

### :material-chart-line: Langfuse 可观测性

11 个 LLM 调用点全部标记 prompt name/version，trace 可视化全链路，token/cost/latency 自动上报，未配置自动降级。

[查看可观测性 :material-arrow-right:](tutorials/observability.md){: .md-button }

</div>

<div class="card" markdown>

### :material-shield-check: 降级策略

LLM / BGE / LangGraph / Redis / 业务 API / Langfuse 七层降级保障可用性，主链路永不阻塞。

[查看降级策略 :material-arrow-right:](architecture/fallback.md){: .md-button }

</div>

</div>

---

## 性能指标

真实 DeepSeek LLM + BGE 嵌入环境下验证：

| 指标 | 目标 | 实测 | 达标 |
|------|------|------|------|
| Recall@5 | ≥ 0.85 | 1.0 | :material-check:{.pass} |
| Hit Rate | ≥ 0.90 | 0.9333 | :material-check:{.pass} |
| 幻觉率 | ≤ 0.10 | 0.0 | :material-check:{.pass} |
| 独立解决率 | ≥ 60% | 80% | :material-check:{.pass} |
| 平均响应时间 | ≤ 3s | 2.27s | :material-check:{.pass} |

---

## 项目结构概览

```text
app/
├── api/v1/              # 接入层：REST API 端点
│   ├── chat.py          # 对话端点（同步 + SSE 流式）
│   ├── agent.py         # 坐席辅助端点（8 个）
│   └── ...              # 知识库/评估/性能/可观测/运营
├── agents/              # Agent 协同层
│   ├── orchestrator.py  # 调度 Agent
│   ├── graph.py         # LangGraph 状态机编排
│   └── ...              # 5 个专业 Agent + LLMClient
├── core/                # 核心基础设施
│   ├── session.py       # 会话管理（含坐席状态）
│   ├── performance.py   # HotQueryCache / ModelRouter
│   └── langfuse_client.py   # Langfuse 链路追踪
├── knowledge/           # 知识与数据层
│   ├── hybrid_retriever.py  # 混合检索
│   └── ...              # Reranker / 版本管理 / 质量校验
└── schemas/             # Pydantic 数据模型

tests/                   # 668+ 测试用例
```

---

## 下一步

- **[一分钟跑起来](quick-start.md)**：本地启动一个最小可用的客服系统
- **[安装指南](installation.md)**：完整部署与依赖安装
- **[配置说明](configuration.md)**：理解 30+ 配置项的作用
- **[架构设计](architecture.md)**：深入多 Agent 协同与 RAG 检索流程
