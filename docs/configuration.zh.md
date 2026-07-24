---
title: 配置说明
description: 全部配置项的默认值、说明与影响范围，含生产/开发环境推荐配置与运行时生效说明。
---

# :material-tune: 配置说明

系统配置通过 `pydantic-settings` 从环境变量与 `.env` 文件加载，定义在 `app/core/config.py` 的 `Settings` 类中。本文档覆盖全部配置项及其影响范围。

!!! info "配置文件位置"
    配置模板见项目根目录 `.env.example`，实际配置写入 `.env`。两者编码均为 UTF-8。

---

## :material-priority-high: 配置优先级

配置加载遵循以下优先级（从高到低）：

```
环境变量  >  .env 文件  >  Settings 类默认值
```

1. **环境变量**：部署时通过 `export` 或容器环境变量注入，优先级最高
2. **`.env` 文件**：本地开发常用，与 `Settings` 同目录加载
3. **默认值**：`app/core/config.py` 中 `Settings` 类的字段默认值

!!! tip "全局单例"
    `get_settings()` 使用 `@lru_cache` 保证进程内只创建一次 `Settings` 实例，避免重复读取环境变量。测试中调用 `get_settings.cache_clear()` 可重置。

---

## :material-apps: 应用基础配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `APP_NAME` | `Intelligent Customer Service System` | 应用名称，用于日志与监控标识 | 全局 |
| `APP_HOST` | `0.0.0.0` | 服务监听地址 | 服务启动 |
| `APP_PORT` | `8000` | 服务监听端口 | 服务启动 |
| `DEBUG` | `False` | 调试模式，开启后日志更详细、错误堆栈直接返回 | 全局 |

```bash
APP_NAME=Intelligent Customer Service System
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=True
```

!!! warning "生产环境"
    生产环境务必设置 `DEBUG=False`，避免错误堆栈泄露敏感信息。

---

## :material-shield-key: 鉴权配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `API_KEY` | `""`（空） | 服务端 API Key，客户端需在 `X-API-Key` 请求头携带；**留空则进入开发免鉴权模式** | 全部 API 端点 |

```bash
# 开发模式（免鉴权）
API_KEY=

# 生产模式（需鉴权）
API_KEY=your-secret-api-key
```

!!! danger "生产环境务必配置"
    `API_KEY` 留空时所有 API 端点均可匿名访问。生产环境必须设置强随机 Key，客户端请求时携带：
    
    ```bash
    curl -H "X-API-Key: your-secret-api-key" http://localhost:8000/api/v1/chat ...
    ```

!!! tip "常量时间比较"
    `verify_api_key` 使用 `secrets.compare_digest` 做恒定时间比较，避免攻击者通过响应时间差逐字符猜测 API Key。应用启动时若 `API_KEY` 为空会输出 WARNING 日志提醒运维人员。

---

## :material-shield-check: 安全配置

生产化加固引入的安全相关配置项，覆盖 CORS、限流、敏感词、会话生命周期四个维度。所有配置项均可通过环境变量或 `.env` 注入，详见 `app/core/config.py` 的 `Settings` 类。

### 配置项

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `ALLOWED_ORIGINS` | `""`（空） | CORS 白名单，逗号分隔多个源（如 `https://example.com,https://app.example.com`）；留空时 `DEBUG=True` 允许所有源（不允许凭据），`DEBUG=False` 拒绝所有源 | CORS 中间件 |
| `RATE_LIMIT_ENABLED` | `True` | 是否启用全局 IP 限流；`False` 时全部接口不再做速率限制（降级放行） | 全局限流中间件 |
| `SENSITIVE_WORDS` | `""`（空） | 运行时额外敏感词，逗号分隔；与 `app/knowledge/sensitive_words.txt` 合并生效，默认 `warn` 级 | 内容过滤器 |
| `SESSION_TTL` | `1800` | 会话超时时间（秒），超过该时长无活动的会话将被清理，默认 30 分钟 | 会话清理 |
| `SESSION_CLEANUP_INTERVAL` | `300` | 会话清理扫描间隔（秒），后台线程每隔该时长扫描一次过期会话，默认 5 分钟 | 会话清理 |

```bash
# CORS 白名单（生产环境必须配置前端域名）
ALLOWED_ORIGINS=https://example.com,https://app.example.com

# 限流开关（生产环境建议开启）
RATE_LIMIT_ENABLED=True

# 额外敏感词（与 sensitive_words.txt 合并生效）
SENSITIVE_WORDS=竞品A,竞品B,内部代号

# 会话超时与清理
SESSION_TTL=1800
SESSION_CLEANUP_INTERVAL=300
```

### 安全特性一览

系统已内置以下安全机制，无需额外配置即可生效：

- **API Key 常量时间比较**：`secrets.compare_digest` 防止时序攻击
- **启动时 API_KEY 空值 WARNING**：应用启动时检测到 `API_KEY` 为空会在日志输出安全告警
- **CORS 白名单 + 安全响应头**：白名单模式下才允许凭据；响应自动附加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: strict-origin-when-cross-origin`，HTTPS 下追加 `Strict-Transport-Security`（HSTS）
- **全局 IP 限流**：滑动窗口算法，默认 60 req/min/IP，超限返回 429 并带 `Retry-After` 头
- **运行时双向敏感词过滤**：基于 AC 自动机的高效多模式匹配，分 `block`（拦截）/`warn`（整词替换为 `***`）/`mask`（保留首尾字符中间打码）三级；输入侧仅 `block` 拦截，输出侧 `warn`/`mask` 替换
- **日志 PII 自动脱敏**：日志过滤器自动识别并打码手机号（前 3 后 4）、身份证（前 6 后 4）、邮箱（用户名首字符）、银行卡（前 4 后 4）
- **会话超时自动清理**：后台 daemon 线程按 `SESSION_CLEANUP_INTERVAL` 周期扫描，清理超过 `SESSION_TTL` 未活动的会话
- **文件上传限制**：类型白名单（`.md`/`.txt`/`.pdf`/`.docx`）+ 10MB 上限，超限返回 413/415
- **运维 API 鉴权**：`/api/v1/knowledge/*`、`/api/v1/operations/*` 等运维端点强制依赖 `verify_api_key`

!!! warning "CORS 白名单模式"
    `ALLOWED_ORIGINS` 留空 + `DEBUG=False` 时，CORS 拒绝所有跨源请求，前端将无法访问。生产环境必须显式配置前端域名。

!!! tip "限流降级"
    `RATE_LIMIT_ENABLED=False` 时全局限流降级为放行，仅建议在压测或调试场景临时关闭。多进程部署需替换为 Redis 等共享存储的限流方案。

??? info "敏感词文件格式"
    `app/knowledge/sensitive_words.txt` 每行格式 `词|级别`，无 `|` 时默认 `warn` 级；`#` 开头为注释。修改后需重启服务或调用 `reset_content_filter()` 重新构建 AC 自动机。

---

## :material-robot: LLM 配置

主 LLM，用于 Query 改写、RAG 答案生成、对话润色等核心任务。

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `LLM_API_KEY` | `""`（空） | LLM API Key；**留空时自动启用 `_MockLLM` mock 模式** | LLM 调用 |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | LLM API 基址，支持任意 OpenAI 兼容接口 | LLM 调用 |
| `LLM_MODEL` | `gpt-4o-mini` | 主模型名称 | LLM 调用 |

=== "DeepSeek（推荐）"

    ```bash
    LLM_API_KEY=sk-your-deepseek-key
    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_MODEL=deepseek-chat
    ```

=== "千问"

    ```bash
    LLM_API_KEY=sk-your-qwen-key
    LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    LLM_MODEL=qwen-plus
    ```

=== "OpenAI"

    ```bash
    LLM_API_KEY=sk-your-openai-key
    LLM_BASE_URL=https://api.openai.com/v1
    LLM_MODEL=gpt-4o-mini
    ```

!!! note "mock 模式"
    `LLM_API_KEY` 留空时，`LLMClient` 自动实例化 `_MockLLM`：不调用任何外部服务，从用户消息中提取内容拼装回复。此模式下意图识别走关键词规则，答案生成走检索结果拼接，**全链路可跑通但无真实 LLM 能力**。

---

## :material-speed-box: 小模型配置

用于意图识别等简单任务的路由，降低首 Token 延迟。通过 `ModelRouter` 按复杂度评分路由：简单查询走小模型（~1s），复杂查询走主 LLM。

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `SMALL_LLM_API_KEY` | `""`（空） | 小模型 API Key；**留空时 ModelRouter 自动降级到主 LLM**，无副作用 | 意图识别 |
| `SMALL_LLM_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 小模型 API 基址（默认豆包） | 意图识别 |
| `SMALL_LLM_MODEL` | `doubao-lite-4k` | 小模型名称 | 意图识别 |
| `SMALL_MODEL_THRESHOLD` | `0.5` | 小模型路由阈值：复杂度评分低于该值走小模型（0-1） | 意图识别 |

=== "豆包（默认）"

    ```bash
    SMALL_LLM_API_KEY=your-volc-key
    SMALL_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
    SMALL_LLM_MODEL=doubao-lite-4k
    ```

=== "千问 qwen-turbo"

    ```bash
    SMALL_LLM_API_KEY=sk-your-qwen-key
    SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    SMALL_LLM_MODEL=qwen-turbo
    ```

??? tip "为什么需要小模型？"
    意图识别是每次对话的第一步，延迟敏感。主 LLM（如 DeepSeek-V3）首 Token 约 2.7s，而小模型（豆包/千问-turbo）首 Token 约 1s。将简单意图识别路由到小模型，整体平均响应从 2.7s 降到 2.27s。配合 IntentCache 缓存命中，可进一步降到 ~800ms。

---

## :material-chart-bell-curve: Langfuse 配置

LLM 链路追踪与 Prompt 版本管理。未配置或 `LANGFUSE_ENABLED=False` 时全部降级为 no-op，不影响主链路。

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `LANGFUSE_ENABLED` | `False` | 是否启用 Langfuse；`False` 时全部降级 no-op | LLM 追踪 |
| `LANGFUSE_PUBLIC_KEY` | `""`（空） | Langfuse 公钥（Project Settings → API Keys 获取） | LLM 追踪 |
| `LANGFUSE_SECRET_KEY` | `""`（空） | Langfuse 私钥，注意保密 | LLM 追踪 |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse 服务地址；自建部署填内网地址 | LLM 追踪 |

```bash
# 启用 Langfuse 云版
LANGFUSE_ENABLED=True
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://cloud.langfuse.com

# 自建部署
LANGFUSE_HOST=http://your-langfuse-server:3000
```

!!! info "降级机制"
    `LANGFUSE_ENABLED=False` 或 Key 为空时，`LangfuseClient` 降级为 no-op：`start_langfuse_trace` 返回 `None`，`finish_langfuse_trace(None, ...)` 静默跳过。LLM 调用回退原生 OpenAI SDK，不经过 langfuse.openai 包装，主链路完全不受影响。

---

## :material-vector: Embedding 与 ChromaDB 配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `EMBEDDING_MODEL` | `BAAI/bge-large-zh-v1.5` | 嵌入模型名（1024 维） | 向量检索 |
| `EMBEDDING_LOCAL_CACHE_DIR` | `./models/bge-large-zh` | BGE 本地缓存目录，离线环境从此加载 | 向量检索 |
| `HF_MIRROR_URL` | `https://hf-mirror.com` | HuggingFace 镜像源，国内网络回退 | 模型下载 |
| `EMBEDDING_LOAD_TIMEOUT` | `60` | 模型加载超时秒数 | 模型加载 |
| `EMBEDDING_BATCH_SIZE` | `32` | Embedding 批大小，内存紧张时调小 | 文档入库 |
| `CHROMA_PERSIST_DIR` | `./chroma_data` | ChromaDB 持久化目录 | 向量库 |
| `CHROMA_COLLECTION_NAME` | `knowledge_base` | ChromaDB 集合名，用于隔离不同业务知识库 | 向量库 |

```bash
EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5
EMBEDDING_LOCAL_CACHE_DIR=./models/bge-large-zh
HF_MIRROR_URL=https://hf-mirror.com
EMBEDDING_BATCH_SIZE=32
CHROMA_PERSIST_DIR=./chroma_data
CHROMA_COLLECTION_NAME=knowledge_base
```

!!! warning "维度一致性"
    BGE 输出 1024 维，hash fallback 也对齐到 1024 维。**同一向量库中所有向量维度必须一致**，否则检索会报 schema 冲突。切换嵌入模型需清空 `CHROMA_PERSIST_DIR` 重新入库。

---

## :material-scissors-cutting: 文本切分配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `CHUNK_SIZE` | `512` | 文本切分块大小（字符数） | 文档入库 |
| `CHUNK_OVERLAP` | `128` | 切分重叠（字符数），保证上下文连续性 | 文档入库 |

```bash
CHUNK_SIZE=512
CHUNK_OVERLAP=128
```

??? tip "如何选择切分参数"
    - `CHUNK_SIZE` 过大：单 chunk 信息过多，检索精度下降
    - `CHUNK_SIZE` 过小：上下文断裂，LLM 生成质量下降
    - `CHUNK_OVERLAP` 一般取 `CHUNK_SIZE` 的 20%-30%
    - FAQ 类短文档可调小到 `256/64`，长文档可调大到 `1024/256`

---

## :material-filter: 检索阈值配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `SIMILARITY_THRESHOLD` | `0.6` | 检索相似度阈值，**低于该值的召回视为弱相关，过滤掉**；低于阈值不强行回答 | RAG 检索 |
| `DEDUP_THRESHOLD` | `0.95` | 入库去重阈值，高于该值视为重复文档，跳过写入 | 文档入库 |

```bash
SIMILARITY_THRESHOLD=0.6
DEDUP_THRESHOLD=0.95
```

!!! tip "阈值调优建议"
    - `SIMILARITY_THRESHOLD` 过高（如 0.8）：召回率下降，很多有效结果被过滤
    - `SIMILARITY_THRESHOLD` 过低（如 0.3）：误召回增多，LLM 可能基于弱相关片段编造
    - 推荐范围：0.5 ~ 0.7，项目实测 0.6 时 Recall@5=1.0

---

## :material-blender: 混合检索配置

混合检索 = 向量召回 + BM25 召回 + RRF 融合 + Reranker 重排序。

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `VECTOR_TOP_K` | `25` | 向量召回数量 | RAG 检索 |
| `BM25_TOP_K` | `25` | BM25 关键词召回数量 | RAG 检索 |
| `RRF_K` | `60` | RRF 融合平滑参数，平滑排名差异避免 top-1 过度主导 | RAG 检索 |
| `RRF_VECTOR_WEIGHT` | `0.6` | RRF 向量路权重（60%） | RAG 检索 |
| `RRF_KEYWORD_WEIGHT` | `0.4` | RRF 关键词路权重（40%） | RAG 检索 |
| `RERANK_TOP_K` | `5` | Reranker 重排序后取 Top-K 进入最终答案 | RAG 检索 |
| `RERANKER_MODEL` | `BAAI/bge-reranker-base` | CrossEncoder 重排序模型名 | RAG 检索 |

```bash
VECTOR_TOP_K=25
BM25_TOP_K=25
RRF_K=60
RRF_VECTOR_WEIGHT=0.6
RRF_KEYWORD_WEIGHT=0.4
RERANK_TOP_K=5
RERANKER_MODEL=BAAI/bge-reranker-base
```

??? info "RRF 融合公式"
    
    ```
    score = Σ weight_i × 1 / (k + rank_i)
    ```
    
    - `rank_i`：某 chunk 在第 i 路召回中的排名（从 1 开始）
    - `k=60`：经验平滑值，避免 rank=1 的结果过度主导
    - 向量权重 0.6 + 关键词权重 0.4 = 1.0

    **为什么向量权重更高？** 向量检索擅长语义匹配（同义词、 paraphrase），在客服场景下用户提问方式多样，语义匹配比关键词匹配更重要。但关键词匹配能捕获专有名词（产品型号、订单号），因此保留 40% 权重。

---

## :material-clock-outline: 人工转接配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `WORKING_HOURS_START` | `9` | 人工服务开始时间（24 小时制，[START, END)） | 转接决策 |
| `WORKING_HOURS_END` | `18` | 人工服务结束时间 | 转接决策 |
| `TIMEZONE` | `Asia/Shanghai` | 时区，用于工作时间判断 | 转接决策 |

```bash
WORKING_HOURS_START=9
WORKING_HOURS_END=18
TIMEZONE=Asia/Shanghai
```

!!! note "转接规则"
    - 工作时间 `[9, 18)` 内：情绪敏感/连续失败/用户主动要求 → 转人工
    - 非工作时间：不因情绪/失败**主动**转接（避免无人接听）
    - **用户主动要求「转人工」时无视时间段**，始终转接，避免阻断用户明确诉求

---

## :material-domain: 业务适配器配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `BUSINESS_ADAPTER_MODE` | `mock` | 适配器模式：`mock`=内存 mock；`http`=真实业务系统 REST API | 业务查询 |
| `BUSINESS_API_BASE_URL` | `""`（空） | 真实业务系统 API 基址，`http` 模式必填；留空自动降级 mock 并告警 | 业务查询 |
| `BUSINESS_API_KEY` | `""`（空） | 真实业务系统 API Key，写入 `X-API-Key` 请求头鉴权 | 业务查询 |
| `BUSINESS_API_TIMEOUT` | `10` | HTTP 调用超时秒数，避免单次调用长时间挂起 | 业务查询 |

=== "mock 模式（默认，开箱即用）"

    ```bash
    BUSINESS_ADAPTER_MODE=mock
    # 以下配置在 mock 模式下忽略
    BUSINESS_API_BASE_URL=
    BUSINESS_API_KEY=
    BUSINESS_API_TIMEOUT=10
    ```

=== "http 模式（真实业务系统）"

    ```bash
    BUSINESS_ADAPTER_MODE=http
    BUSINESS_API_BASE_URL=https://your-business-api.com
    BUSINESS_API_KEY=your-business-api-key
    BUSINESS_API_TIMEOUT=10
    ```

!!! warning "http 模式降级"
    `BUSINESS_ADAPTER_MODE=http` 但 `BUSINESS_API_BASE_URL` 为空时，系统自动降级到 mock 模式并在日志打印告警。调用失败时 BusinessAgent 也降级到 mock 业务系统。

---

## :material-database: 缓存与外部服务配置

| 变量名 | 默认值 | 说明 | 影响范围 |
|--------|--------|------|----------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接地址（会话存储、缓存）；不可达时降级内存队列 | 会话/缓存 |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch 地址（全文检索增强，可选） | 全文检索 |

```bash
REDIS_URL=redis://localhost:6379/0
ELASTICSEARCH_URL=http://localhost:9200
```

---

## :material-flask: 推荐配置方案

### 开发环境

```bash
# .env（开发）
APP_NAME=Intelligent Customer Service System
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=True

# 免鉴权，便于调试
API_KEY=

# mock 模式，无需 LLM Key
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# mock 业务系统
BUSINESS_ADAPTER_MODE=mock

# Langfuse 关闭
LANGFUSE_ENABLED=False

# 默认检索参数
CHUNK_SIZE=512
CHUNK_OVERLAP=128
SIMILARITY_THRESHOLD=0.6
VECTOR_TOP_K=25
BM25_TOP_K=25
RERANK_TOP_K=5
```

### 生产环境

```bash
# .env（生产）
APP_NAME=Customer Service Prod
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=False

# 强制鉴权
API_KEY=<strong-random-key>

# 真实 LLM
LLM_API_KEY=sk-your-deepseek-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# 小模型路由（降低延迟）
SMALL_LLM_API_KEY=sk-your-qwen-key
SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SMALL_LLM_MODEL=qwen-turbo
SMALL_MODEL_THRESHOLD=0.5

# 真实业务系统
BUSINESS_ADAPTER_MODE=http
BUSINESS_API_BASE_URL=https://your-business-api.com
BUSINESS_API_KEY=<business-api-key>
BUSINESS_API_TIMEOUT=10

# 启用 Langfuse 追踪
LANGFUSE_ENABLED=True
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://cloud.langfuse.com

# Redis 持久化
REDIS_URL=redis://redis:6379/0

# 检索参数（生产可适当调大召回）
VECTOR_TOP_K=30
BM25_TOP_K=30
RERANK_TOP_K=5
```

---

## :material-refresh: 运行时生效说明

!!! warning "以下配置项修改后需重启服务"
    
    配置通过 `@lru_cache` 全局单例加载，进程启动后不再重新读取。以下配置修改后**必须重启服务**才能生效：

    | 配置项 | 原因 |
    |--------|------|
    | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | LLMClient 单例在首次调用时初始化 |
    | `SMALL_LLM_*` | 小模型客户端单例初始化 |
    | `EMBEDDING_MODEL` / `EMBEDDING_LOCAL_CACHE_DIR` | EmbeddingService 单例加载模型后缓存 |
    | `CHROMA_PERSIST_DIR` / `CHROMA_COLLECTION_NAME` | VectorStore 单例连接 ChromaDB |
    | `LANGFUSE_*` | LangfuseClient 单例初始化 |
    | `BUSINESS_ADAPTER_MODE` / `BUSINESS_API_*` | 业务适配器单例初始化 |
    | `APP_HOST` / `APP_PORT` | Uvicorn 启动参数 |

    **可热更新的配置**（通过 `POST /api/v1/performance/cache/invalidate` 清缓存间接生效）：

    - `SIMILARITY_THRESHOLD`：清空 HotQueryCache 后下次检索使用新阈值
    - `VECTOR_TOP_K` / `BM25_TOP_K` / `RERANK_TOP_K`：清缓存后下次检索使用新参数

    > 注意：热更新仅影响缓存失效后的新请求，已缓存的结果不会自动更新。建议修改检索参数后调用缓存清理接口。
