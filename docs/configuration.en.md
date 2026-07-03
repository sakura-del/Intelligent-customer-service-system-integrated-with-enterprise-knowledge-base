---
title: Configuration Guide
description: Default values, descriptions, and impact scope of all configuration options, including recommended configs for production/development and runtime effect notes.
---

# :material-tune: Configuration Guide

System configuration is loaded from environment variables and the `.env` file via `pydantic-settings`, defined in the `Settings` class in `app/core/config.py`. This document covers all configuration options and their impact scope.

!!! info "Configuration file location"
    The configuration template is at the project root `.env.example`; the actual configuration is written to `.env`. Both are UTF-8 encoded.

---

## :material-priority-high: Configuration Priority

Configuration loading follows this priority (high to low):

```
Environment Variables  >  .env File  >  Settings Class Defaults
```

1. **Environment variables**: Injected via `export` or container env vars at deployment, highest priority
2. **`.env` file**: Commonly used for local development, loaded from the same directory as `Settings`
3. **Default values**: Field defaults in the `Settings` class in `app/core/config.py`

!!! tip "Global singleton"
    `get_settings()` uses `@lru_cache` to ensure only one `Settings` instance is created per process, avoiding repeated reads of environment variables. In tests, call `get_settings.cache_clear()` to reset.

---

## :material-apps: Application Basic Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `APP_NAME` | `Intelligent Customer Service System` | Application name, used for logs and monitoring tags | Global |
| `APP_HOST` | `0.0.0.0` | Service listen address | Service startup |
| `APP_PORT` | `8000` | Service listen port | Service startup |
| `DEBUG` | `False` | Debug mode; when enabled, logs are more verbose and error stacks are returned directly | Global |

```bash
APP_NAME=Intelligent Customer Service System
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=True
```

!!! warning "Production environment"
    In production, always set `DEBUG=False` to avoid leaking sensitive info via error stacks.

---

## :material-shield-key: Authentication Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `API_KEY` | `""` (empty) | Server-side API Key; clients must send it in the `X-API-Key` header; **empty enables dev no-auth mode** | All API endpoints |

```bash
# Dev mode (no auth)
API_KEY=

# Production mode (auth required)
API_KEY=your-secret-api-key
```

!!! danger "Must configure in production"
    When `API_KEY` is empty, all API endpoints can be accessed anonymously. Production must set a strong random key, and clients must send it:

    ```bash
    curl -H "X-API-Key: your-secret-api-key" http://localhost:8000/api/v1/chat ...
    ```

---

## :material-robot: LLM Configuration

Main LLM, used for core tasks such as Query rewriting, RAG answer generation, and dialog polishing.

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `LLM_API_KEY` | `""` (empty) | LLM API Key; **empty auto-enables `_MockLLM` mock mode** | LLM calls |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | LLM API base URL, supports any OpenAI-compatible interface | LLM calls |
| `LLM_MODEL` | `gpt-4o-mini` | Main model name | LLM calls |

=== "DeepSeek (recommended)"

    ```bash
    LLM_API_KEY=sk-your-deepseek-key
    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_MODEL=deepseek-chat
    ```

=== "Qwen"

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

!!! note "Mock mode"
    When `LLM_API_KEY` is empty, `LLMClient` auto-instantiates `_MockLLM`: it does not call any external service, and assembles a reply by extracting content from the user message. In this mode, intent recognition uses keyword rules, answer generation uses retrieval result concatenation. **The full chain runs but without real LLM capability**.

---

## :material-speed-box: Small Model Configuration

Used for routing simple tasks like intent recognition to lower first-token latency. `ModelRouter` routes by complexity score: simple queries go to the small model (~1s), complex queries go to the main LLM.

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `SMALL_LLM_API_KEY` | `""` (empty) | Small model API Key; **empty auto-degrades ModelRouter to the main LLM**, no side effects | Intent recognition |
| `SMALL_LLM_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | Small model API base URL (default Doubao) | Intent recognition |
| `SMALL_LLM_MODEL` | `doubao-lite-4k` | Small model name | Intent recognition |
| `SMALL_MODEL_THRESHOLD` | `0.5` | Small model routing threshold: queries with complexity below this go to the small model (0-1) | Intent recognition |

=== "Doubao (default)"

    ```bash
    SMALL_LLM_API_KEY=your-volc-key
    SMALL_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
    SMALL_LLM_MODEL=doubao-lite-4k
    ```

=== "Qwen qwen-turbo"

    ```bash
    SMALL_LLM_API_KEY=sk-your-qwen-key
    SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    SMALL_LLM_MODEL=qwen-turbo
    ```

??? tip "Why a small model?"
    Intent recognition is the first step of every conversation and is latency-sensitive. The main LLM (e.g., DeepSeek-V3) has a first-token time of ~2.7s, while a small model (Doubao/Qwen-turbo) is ~1s. Routing simple intent recognition to the small model reduces overall avg response from 2.7s to 2.27s. With IntentCache hits, it can drop further to ~800ms.

---

## :material-chart-bell-curve: Langfuse Configuration

LLM tracing and prompt version management. When not configured or `LANGFUSE_ENABLED=False`, all degrade to no-op, without affecting the main path.

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `LANGFUSE_ENABLED` | `False` | Whether to enable Langfuse; `False` degrades all to no-op | LLM tracing |
| `LANGFUSE_PUBLIC_KEY` | `""` (empty) | Langfuse public key (from Project Settings → API Keys) | LLM tracing |
| `LANGFUSE_SECRET_KEY` | `""` (empty) | Langfuse secret key, keep confidential | LLM tracing |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse service address; for self-hosted, use the intranet address | LLM tracing |

```bash
# Enable Langfuse cloud
LANGFUSE_ENABLED=True
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://cloud.langfuse.com

# Self-hosted deployment
LANGFUSE_HOST=http://your-langfuse-server:3000
```

!!! info "Degradation mechanism"
    When `LANGFUSE_ENABLED=False` or Key is empty, `LangfuseClient` degrades to no-op: `start_langfuse_trace` returns `None`, `finish_langfuse_trace(None, ...)` silently skips. LLM calls fall back to the native OpenAI SDK, not wrapped by langfuse.openai, so the main path is completely unaffected.

---

## :material-vector: Embedding and ChromaDB Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `EMBEDDING_MODEL` | `BAAI/bge-large-zh-v1.5` | Embedding model name (1024 dimensions) | Vector retrieval |
| `EMBEDDING_LOCAL_CACHE_DIR` | `./models/bge-large-zh` | BGE local cache directory, loaded from here in offline environments | Vector retrieval |
| `HF_MIRROR_URL` | `https://hf-mirror.com` | HuggingFace mirror source, fallback for domestic networks | Model download |
| `EMBEDDING_LOAD_TIMEOUT` | `60` | Model load timeout in seconds | Model loading |
| `EMBEDDING_BATCH_SIZE` | `32` | Embedding batch size, reduce when memory is tight | Document ingestion |
| `CHROMA_PERSIST_DIR` | `./chroma_data` | ChromaDB persistence directory | Vector store |
| `CHROMA_COLLECTION_NAME` | `knowledge_base` | ChromaDB collection name, used to isolate different business knowledge bases | Vector store |

```bash
EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5
EMBEDDING_LOCAL_CACHE_DIR=./models/bge-large-zh
HF_MIRROR_URL=https://hf-mirror.com
EMBEDDING_BATCH_SIZE=32
CHROMA_PERSIST_DIR=./chroma_data
CHROMA_COLLECTION_NAME=knowledge_base
```

!!! warning "Dimension consistency"
    BGE outputs 1024 dimensions, and hash fallback also aligns to 1024 dimensions. **All vectors in the same vector store must have consistent dimensions**, otherwise retrieval reports a schema conflict. Switching embedding models requires clearing `CHROMA_PERSIST_DIR` and re-ingesting.

---

## :material-scissors-cutting: Text Splitting Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `CHUNK_SIZE` | `512` | Text split chunk size (characters) | Document ingestion |
| `CHUNK_OVERLAP` | `128` | Split overlap (characters), ensures context continuity | Document ingestion |

```bash
CHUNK_SIZE=512
CHUNK_OVERLAP=128
```

??? tip "How to choose splitting parameters"
    - `CHUNK_SIZE` too large: too much info per chunk, retrieval precision drops
    - `CHUNK_SIZE` too small: context breaks, LLM generation quality drops
    - `CHUNK_OVERLAP` is typically 20%-30% of `CHUNK_SIZE`
    - FAQ-type short docs can be reduced to `256/64`; long docs can be increased to `1024/256`

---

## :material-filter: Retrieval Threshold Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `SIMILARITY_THRESHOLD` | `0.6` | Retrieval similarity threshold; **recall below this is treated as weakly relevant and filtered out**; no forced answer below threshold | RAG retrieval |
| `DEDUP_THRESHOLD` | `0.95` | Ingestion dedup threshold; above this is treated as a duplicate document, skipped | Document ingestion |

```bash
SIMILARITY_THRESHOLD=0.6
DEDUP_THRESHOLD=0.95
```

!!! tip "Threshold tuning tips"
    - `SIMILARITY_THRESHOLD` too high (e.g., 0.8): recall drops, many valid results are filtered
    - `SIMILARITY_THRESHOLD` too low (e.g., 0.3): false recalls increase, LLM may fabricate based on weakly related fragments
    - Recommended range: 0.5 ~ 0.7; project tested Recall@5=1.0 at 0.6

---

## :material-blender: Hybrid Retrieval Configuration

Hybrid retrieval = vector recall + BM25 recall + RRF fusion + Reranker reranking.

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `VECTOR_TOP_K` | `25` | Vector recall count | RAG retrieval |
| `BM25_TOP_K` | `25` | BM25 keyword recall count | RAG retrieval |
| `RRF_K` | `60` | RRF fusion smoothing parameter, smooths rank differences to avoid top-1 dominance | RAG retrieval |
| `RRF_VECTOR_WEIGHT` | `0.6` | RRF vector path weight (60%) | RAG retrieval |
| `RRF_KEYWORD_WEIGHT` | `0.4` | RRF keyword path weight (40%) | RAG retrieval |
| `RERANK_TOP_K` | `5` | Top-K after Reranker reranking to enter the final answer | RAG retrieval |
| `RERANKER_MODEL` | `BAAI/bge-reranker-base` | CrossEncoder reranker model name | RAG retrieval |

```bash
VECTOR_TOP_K=25
BM25_TOP_K=25
RRF_K=60
RRF_VECTOR_WEIGHT=0.6
RRF_KEYWORD_WEIGHT=0.4
RERANK_TOP_K=5
RERANKER_MODEL=BAAI/bge-reranker-base
```

??? info "RRF fusion formula"
    
    ```
    score = Σ weight_i × 1 / (k + rank_i)
    ```
    
    - `rank_i`: the rank of a chunk in the i-th recall path (starting from 1)
    - `k=60`: empirical smoothing value, prevents rank=1 results from dominating
    - Vector weight 0.6 + keyword weight 0.4 = 1.0

    **Why is the vector weight higher?** Vector retrieval excels at semantic matching (synonyms, paraphrases). In customer service scenarios, users ask in diverse ways, so semantic matching is more important than keyword matching. But keyword matching captures proper nouns (product models, order numbers), so a 40% weight is retained.

---

## :material-clock-outline: Human Escalation Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `WORKING_HOURS_START` | `9` | Human service start time (24-hour format, [START, END)) | Escalation decision |
| `WORKING_HOURS_END` | `18` | Human service end time | Escalation decision |
| `TIMEZONE` | `Asia/Shanghai` | Timezone, used for working hours check | Escalation decision |

```bash
WORKING_HOURS_START=9
WORKING_HOURS_END=18
TIMEZONE=Asia/Shanghai
```

!!! note "Escalation rules"
    - During working hours `[9, 18)`: sentiment sensitive / consecutive failures / user actively requests → escalate to human
    - Outside working hours: no **active** escalation due to sentiment/failure (to avoid unanswered escalations)
    - **When a user actively requests "escalate to human", the time window is ignored** and always escalates, to avoid blocking explicit user requests

---

## :material-domain: Business Adapter Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `BUSINESS_ADAPTER_MODE` | `mock` | Adapter mode: `mock`=in-memory mock; `http`=real business system REST API | Business query |
| `BUSINESS_API_BASE_URL` | `""` (empty) | Real business system API base URL, required for `http` mode; empty auto-degrades to mock with a warning | Business query |
| `BUSINESS_API_KEY` | `""` (empty) | Real business system API Key, sent in `X-API-Key` header for auth | Business query |
| `BUSINESS_API_TIMEOUT` | `10` | HTTP call timeout in seconds, avoids long hangs on a single call | Business query |

=== "mock mode (default, out of the box)"

    ```bash
    BUSINESS_ADAPTER_MODE=mock
    # The following are ignored in mock mode
    BUSINESS_API_BASE_URL=
    BUSINESS_API_KEY=
    BUSINESS_API_TIMEOUT=10
    ```

=== "http mode (real business system)"

    ```bash
    BUSINESS_ADAPTER_MODE=http
    BUSINESS_API_BASE_URL=https://your-business-api.com
    BUSINESS_API_KEY=your-business-api-key
    BUSINESS_API_TIMEOUT=10
    ```

!!! warning "http mode degradation"
    When `BUSINESS_ADAPTER_MODE=http` but `BUSINESS_API_BASE_URL` is empty, the system auto-degrades to mock mode and logs a warning. On call failure, BusinessAgent also degrades to the mock business system.

---

## :material-database: Cache and External Service Configuration

| Variable | Default | Description | Impact |
|----------|---------|-------------|--------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection address (session storage, cache); degrades to in-memory queue when unreachable | Session/cache |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch address (full-text retrieval enhancement, optional) | Full-text retrieval |

```bash
REDIS_URL=redis://localhost:6379/0
ELASTICSEARCH_URL=http://localhost:9200
```

---

## :material-flask: Recommended Configuration

### Development Environment

```bash
# .env (development)
APP_NAME=Intelligent Customer Service System
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=True

# No auth, for easy debugging
API_KEY=

# Mock mode, no LLM Key needed
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# Mock business system
BUSINESS_ADAPTER_MODE=mock

# Langfuse disabled
LANGFUSE_ENABLED=False

# Default retrieval parameters
CHUNK_SIZE=512
CHUNK_OVERLAP=128
SIMILARITY_THRESHOLD=0.6
VECTOR_TOP_K=25
BM25_TOP_K=25
RERANK_TOP_K=5
```

### Production Environment

```bash
# .env (production)
APP_NAME=Customer Service Prod
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=False

# Enforce auth
API_KEY=<strong-random-key>

# Real LLM
LLM_API_KEY=sk-your-deepseek-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# Small model routing (lower latency)
SMALL_LLM_API_KEY=sk-your-qwen-key
SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SMALL_LLM_MODEL=qwen-turbo
SMALL_MODEL_THRESHOLD=0.5

# Real business system
BUSINESS_ADAPTER_MODE=http
BUSINESS_API_BASE_URL=https://your-business-api.com
BUSINESS_API_KEY=<business-api-key>
BUSINESS_API_TIMEOUT=10

# Enable Langfuse tracing
LANGFUSE_ENABLED=True
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://cloud.langfuse.com

# Redis persistence
REDIS_URL=redis://redis:6379/0

# Retrieval parameters (production can increase recall appropriately)
VECTOR_TOP_K=30
BM25_TOP_K=30
RERANK_TOP_K=5
```

---

## :material-refresh: Runtime Effect Notes

!!! warning "The following options require a service restart after modification"
    
    Configuration is loaded via the `@lru_cache` global singleton and is not re-read after process startup. The following changes **require a service restart** to take effect:

    | Option | Reason |
    |--------|--------|
    | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | LLMClient singleton initializes on first call |
    | `SMALL_LLM_*` | Small model client singleton initialization |
    | `EMBEDDING_MODEL` / `EMBEDDING_LOCAL_CACHE_DIR` | EmbeddingService singleton caches after model load |
    | `CHROMA_PERSIST_DIR` / `CHROMA_COLLECTION_NAME` | VectorStore singleton connects to ChromaDB |
    | `LANGFUSE_*` | LangfuseClient singleton initialization |
    | `BUSINESS_ADAPTER_MODE` / `BUSINESS_API_*` | Business adapter singleton initialization |
    | `APP_HOST` / `APP_PORT` | Uvicorn startup parameters |

    **Hot-updatable options** (take effect indirectly via `POST /api/v1/performance/cache/invalidate` to clear the cache):

    - `SIMILARITY_THRESHOLD`: after clearing HotQueryCache, the next retrieval uses the new threshold
    - `VECTOR_TOP_K` / `BM25_TOP_K` / `RERANK_TOP_K`: after clearing the cache, the next retrieval uses the new params

    > Note: Hot updates only affect new requests after cache invalidation; cached results are not auto-updated. It is recommended to call the cache cleanup endpoint after modifying retrieval parameters.
