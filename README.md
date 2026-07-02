# 整合企业知识库的智能客服系统

基于「多 Agent 分工协作 + RAG 知识增强」双轮驱动的企业级智能客服系统。通过 1 个调度 Agent 协调 5 个专业 Agent，结合混合检索与 LLM 生成，实现高准确率、低延迟、可监控的自动化客服能力。

## 核心特性

- **多 Agent 协同架构**：基于 LangGraph 的「1+5」架构（调度 + 知识检索/业务查询/情感分析/工单处理/对话生成），LangGraph 不可用时自动降级到同步编排
- **混合检索 + RAG**：Query 改写 → 向量检索 + BM25 双路召回 → RRF 融合 → Reranker 重排序 → LLM 生成，相似度低于阈值不强行回答
- **多轮对话管理**：分层摘要上下文、意图切换检测、槽位管理、FIFO 限长历史，长对话不失忆
- **业务系统集成**：订单/会员/退换货/账户 API 适配器框架，含身份校验、手机号脱敏、写操作二次确认
- **人工转接闭环**：情绪敏感/连续失败/用户主动要求触发转接，附转接上下文卡片传递给人工客服
- **知识库治理**：文档管理、质量校验（去重/术语/敏感词）、版本管理与回滚、全量/增量/实时三种更新机制
- **性能优化**：HotQueryCache 热点缓存、ModelRouter 大小模型路由、并发限流降级
- **可观测性**：熔断降级、监控告警、Token 用量追踪、灰度发布、运营看板、Langfuse LLM 链路追踪与 Prompt 版本管理

## 技术栈

| 类别 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 多 Agent 编排 | LangGraph（降级同步编排器） |
| LLM | OpenAI 兼容 SDK（DeepSeek-V3 / GPT-4o-mini） |
| 嵌入模型 | BGE-large-zh-v1.5（1024 维） |
| 向量库 | ChromaDB（hnsw:space=cosine） |
| 关键词检索 | rank-bm25 |
| 文档解析 | Unstructured + PyMuPDF + python-docx + BeautifulSoup4 |
| 异步通信 | Redis Pub/Sub（降级内存队列） |
| LLM 可观测性 | Langfuse（trace 可视化 + Prompt 版本管理，未配置自动降级） |
| 测试 | pytest（640+ 测试用例） |

## 项目结构

```
app/
├── api/v1/              # 接入层：REST API 端点
│   ├── chat.py          # 对话端点（同步 + SSE 流式）
│   ├── knowledge.py     # 知识库管理
│   ├── evaluation.py    # 检索评估
│   ├── performance.py   # 性能监控
│   ├── observability.py # 可观测性（熔断/告警/Token）
│   └── operations.py    # 运营看板与灰度发布
├── agents/              # Agent 协同层
│   ├── orchestrator.py  # 调度 Agent（意图识别/路由/兜底）
│   ├── graph.py         # LangGraph 状态机编排
│   ├── knowledge_agent.py    # 知识检索 Agent（混合检索+重排）
│   ├── business_agent.py     # 业务查询 Agent
│   ├── emotion_agent.py      # 情感分析 Agent
│   ├── ticket_agent.py       # 工单处理 Agent
│   ├── dialog_agent.py       # 对话润色 Agent
│   └── llm_client.py    # LLM 客户端（mock 兜底）
├── core/                # 核心基础设施
│   ├── config.py        # 配置管理
│   ├── session.py       # 会话管理
│   ├── performance.py   # HotQueryCache / ModelRouter / 并发优化
│   ├── circuit_breaker.py   # 熔断降级
│   ├── observability.py # 监控告警
│   ├── langfuse_client.py   # Langfuse 链路追踪客户端（未配置自动降级）
│   └── experiment.py    # 灰度发布与 A/B 测试
├── knowledge/           # 知识与数据层
│   ├── pipeline.py      # 文档入库流水线
│   ├── hybrid_retriever.py  # 混合检索（向量+BM25+RRF）
│   ├── reranker.py      # 重排序
│   ├── vectorstore.py   # ChromaDB 封装
│   ├── embeddings.py    # BGE 嵌入服务
│   ├── quality.py       # 质量校验
│   ├── versioning.py    # 版本管理
│   └── update_mechanism.py  # 全量/增量/实时更新
├── schemas/             # Pydantic 数据模型
└── static/              # 前端界面（对话/监控/运营看板）

tests/                   # 615+ 测试用例
models/bge-large-zh/     # 本地 BGE 模型权重
.trae/specs/             # Spec 驱动开发文档
```

## 快速开始

### 环境准备

1. **Python 3.11+**
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

3. **配置环境变量**：复制 `.env.example` 为 `.env` 并填写：
   ```bash
   cp .env.example .env
   ```
   关键配置项：
   - `LLM_API_KEY`：LLM API Key（如 DeepSeek），留空则走 mock 模式
   - `LLM_BASE_URL`：LLM API 基址（如 `https://api.deepseek.com/v1`）
   - `LLM_MODEL`：模型名（如 `deepseek-chat`）
   - `EMBEDDING_MODEL`：嵌入模型（默认 `BAAI/bge-large-zh-v1.5`）
   - `API_KEY`：服务端鉴权 Key，留空则开发模式免鉴权

4. **BGE 模型**：首次启动会自动下载 BGE 权重；若 HuggingFace 仓库不可达，可用 `sentence_transformers` 手动下载：
   ```python
   from sentence_transformers import SentenceTransformer
   model = SentenceTransformer('BAAI/bge-large-zh-v1.5')
   model.save('./models/bge-large-zh')
   ```
   下载失败时自动降级到 hash fallback 向量，主链路不阻塞。

### 启动服务

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后访问：
- 对话界面：http://localhost:8000/
- API 文档：http://localhost:8000/docs
- 监控面板：http://localhost:8000/monitor
- 运营看板：http://localhost:8000/operations

### 入库知识文档

通过 API 上传文档（支持 PDF/Word/HTML/Markdown）：

```bash
curl -X POST http://localhost:8000/api/v1/knowledge/ingest \
  -H "X-API-Key: $API_KEY" \
  -F "file=@docs/faq.md" \
  -F "knowledge_type=faq"
```

### 调用对话

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"message": "忘记登录密码怎么办？"}'
```

流式响应（SSE）：

```bash
curl -X POST http://localhost:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"message": "产品有哪些功能"}'
```

## 主要 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/health` | GET | 健康检查 |
| `/api/v1/chat` | POST | 同步对话 |
| `/api/v1/chat/stream` | POST | SSE 流式对话 |
| `/api/v1/knowledge/ingest` | POST | 文档入库 |
| `/api/v1/knowledge/stats` | GET | 知识库统计 |
| `/api/v1/evaluation/run` | POST | 跑检索评估（Recall@K/Hit Rate/MRR/幻觉率） |
| `/api/v1/performance/metrics` | GET | 性能指标（缓存命中率/路由统计/响应时间） |
| `/api/v1/performance/cache/invalidate` | POST | 清空热点缓存（知识库更新后调用） |
| `/api/v1/observability/health` | GET | 组件健康检查（LLM/向量库/Redis/磁盘） |
| `/api/v1/operations/dashboard` | GET | 运营看板数据 |

完整 API 列表见 `/docs`。

## 测试

```bash
# 全量测试
python -m pytest tests/ -q

# 单个模块
python -m pytest tests/test_graph.py -q
```

当前测试规模：615+ 用例，覆盖核心链路与边界场景。

## 性能指标

真实 DeepSeek LLM + BGE 嵌入环境下验证：

| 指标 | 目标 | 实测 | 达标 |
|------|------|------|------|
| Recall@5 | ≥ 0.85 | 1.0 | ✓ |
| Hit Rate | ≥ 0.90 | 0.9333 | ✓ |
| 幻觉率 | ≤ 0.10 | 0.0 | ✓ |
| 独立解决率 | ≥ 60% | 80% | ✓ |
| 平均响应时间 | ≤ 3s | 2.27s | ✓ |
| P95 响应时间 | ≤ 5s | 7.94s | ✗（需流式响应优化） |

响应时间优化措施：
- HotQueryCache 接入 `run_graph` 入出口，知识问答命中缓存降至 0.002s
- ModelRouter 接入 KnowledgeAgent，简单查询路由小模型
- 意图识别快通道 + 非知识问答跳过 DialogAgent LLM 润色

## 配置说明

完整配置项见 `.env.example`，关键参数：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CHUNK_SIZE` | 512 | 文本切分块大小 |
| `CHUNK_OVERLAP` | 128 | 切分重叠 |
| `SIMILARITY_THRESHOLD` | 0.6 | 检索相似度阈值，低于不回答 |
| `VECTOR_TOP_K` | 25 | 向量召回数量 |
| `BM25_TOP_K` | 25 | 关键词召回数量 |
| `RRF_VECTOR_WEIGHT` | 0.6 | RRF 向量权重 |
| `RRF_KEYWORD_WEIGHT` | 0.4 | RRF 关键词权重 |
| `RERANK_TOP_K` | 5 | 重排序取 Top-K |
| `BUSINESS_ADAPTER_MODE` | mock | 业务适配器模式（mock/http） |
| `WORKING_HOURS_START` | 9 | 人工服务开始时间 |
| `WORKING_HOURS_END` | 18 | 人工服务结束时间 |
| `LANGFUSE_ENABLED` | False | Langfuse 链路追踪开关，False 或未配置 key 时全部降级 no-op |
| `LANGFUSE_PUBLIC_KEY` | 空 | Langfuse 公钥（Project Settings → API Keys 获取） |
| `LANGFUSE_SECRET_KEY` | 空 | Langfuse 私钥 |
| `LANGFUSE_HOST` | https://cloud.langfuse.com | Langfuse 服务地址（自建填内网地址） |

## 降级策略

系统在多个环节设计了降级保障可用性：

- **LLM 不可用** → 自动降级到 `_MockLLM` 拼装回复
- **BGE 加载失败** → 降级到 hash fallback 向量
- **LangGraph 不可用** → 降级到同步编排器
- **Redis 不可达** → 降级到内存队列
- **业务 API 失败** → 降级到 mock 业务系统
- **真实 LLM 调用失败** → ModelRouter 自动回退默认模型重试
- **Langfuse 未配置或上报失败** → 降级为 no-op，LLMClient 回退原生 OpenAI SDK，不影响主链路

## 开发规范

- **Spec 驱动开发**：`.trae/specs/` 下维护 spec.md / tasks.md / checklist.md 三件套
- **命名约定**：使用描述性名称，遵循 PEP 8
- **注释原则**：解释「为什么」而非「做什么」，公共 API 提供文档字符串
- **测试保障**：重构前确保测试覆盖，修改后跑全量回归

## 许可证

本项目仅用于学习与内部研究。
