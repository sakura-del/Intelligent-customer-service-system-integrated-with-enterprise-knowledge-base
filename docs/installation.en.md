---
title: Installation Guide
description: Complete installation steps, BGE model download, environment variable configuration, optional dependencies, and common installation troubleshooting.
---

# :material-package-variant: Installation Guide

This guide covers the full process from environment preparation to service startup verification, including troubleshooting for common installation issues.

!!! tip "Just want to get started quickly?"
    If you only want to start as fast as possible, see [Quick Start in One Minute](quick-start.md). This guide is for scenarios requiring full configuration and troubleshooting.

---

## :material-clipboard-list: Environment Requirements

| Item | Minimum | Recommended | Notes |
|------|---------|-------------|-------|
| Python | 3.11 | 3.11 / 3.12 | Uses `TypedDict`, `tomllib` and other new features; 3.10 and below are not supported |
| pip | 23.0 | Latest | chromadb and other packages need a newer pip dependency resolver |
| OS | — | — | Windows 10+ / macOS 12+ / Ubuntu 20.04+ all verified |
| Memory | 4 GB | 8 GB+ | BGE model loading takes ~2 GB memory |
| Disk | 2 GB | 3 GB+ | Incl. BGE weights and ChromaDB persistent data |

!!! note "Windows users note"
    Some dependencies of chromadb and sentence-transformers require a C++ build environment. If installation fails, first install [Visual C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) and select the "Desktop development with C++" workload.

---

## :package: Dependency Installation

### 1. Create a Virtual Environment

```bash
# Create a virtual environment
python -m venv .venv

# Activate the virtual environment
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Windows (CMD)
.venv\Scripts\activate.bat
# macOS / Linux
source .venv/bin/activate
```

### 2. Upgrade pip

```bash
# chromadb and other packages depend on a newer pip dependency resolver
python -m pip install --upgrade pip setuptools wheel
```

### 3. Install Project Dependencies

```bash
pip install -r requirements.txt
```

??? info "Key dependencies overview"

    | Dependency | Purpose |
    |------------|---------|
    | `fastapi` + `uvicorn` | Web framework and ASGI server |
    | `langgraph` | Multi-Agent state machine orchestration (auto-degrades to synchronous orchestration when unavailable) |
    | `openai` | OpenAI-compatible SDK, integrates DeepSeek / Qwen / Doubao, etc. |
    | `chromadb` | Vector database (hnsw:space=cosine) |
    | `sentence-transformers` | BGE embedding and Reranker model loading |
    | `rank-bm25` | BM25 keyword retrieval |
    | `langfuse` | LLM tracing and prompt version management (auto-degrades when not configured) |
    | `pydantic-settings` | Configuration management, loads from .env and environment variables |
    | `redis` | Session persistence and caching (optional, degrades to in-memory queue) |
    | `unstructured` + `PyMuPDF` + `python-docx` + `beautifulsoup4` | Multi-format document parsing |

---

## :material-download: BGE Model Download

The system uses `BAAI/bge-large-zh-v1.5` (1024 dimensions) as the embedding model and `BAAI/bge-reranker-base` as the reranker model. They are downloaded automatically on first launch, but may be slow or fail under domestic network conditions.

### Automatic Download (Default)

On first retrieval or ingestion, `EmbeddingService` tries to load in the following order:

1. **HuggingFace primary source** (`https://huggingface.co`)
2. **Domestic mirror source** (`https://hf-mirror.com`, configured via `HF_MIRROR_URL`)
3. **Local cache directory** (`./models/bge-large-zh`, configured via `EMBEDDING_LOCAL_CACHE_DIR`)
4. **hash fallback** (deterministic degradation, only ensures the flow runs; retrieval quality is not guaranteed)

!!! warning "Impact of download failure degradation"
    If all four loading sources fail, the system degrades to hash fallback vectorization: uses `hashlib.sha256` to generate deterministic 1024-dimensional vectors. **Semantic retrieval is disabled in this mode**, only ensuring the flow does not break. Production environments must ensure the BGE model is available.

### Manual Download (Recommended for domestic users)

```python
# Download BGE models to the local cache directory in advance
from sentence_transformers import SentenceTransformer

# Download the embedding model (~1.3 GB)
model = SentenceTransformer("BAAI/bge-large-zh-v1.5")
model.save("./models/bge-large-zh")

# Download the reranker model (~400 MB)
from sentence_transformers import CrossEncoder
reranker = CrossEncoder("BAAI/bge-reranker-base")
reranker.save("./models/bge-reranker-base")
```

### Use a Domestic Mirror

```bash
# Method 1: Set environment variable (recommended)
export HF_ENDPOINT=https://hf-mirror.com

# Windows PowerShell
$env:HF_ENDPOINT = "https://hf-mirror.com"

# Method 2: Configure in .env (the project has built-in fallback to this mirror)
# HF_MIRROR_URL=https://hf-mirror.com  (already filled by default)
```

After setting the mirror, automatic download will prefer `hf-mirror.com`, significantly improving speed for domestic users.

??? tip "Offline environment deployment"
    Run the manual download script on an internet-connected machine, then copy the entire `./models/bge-large-zh` directory to the same path on the offline machine. The system will load directly from the local cache when detected, without needing internet.

---

## :material-cog: Environment Variable Configuration

```bash
# Copy the configuration template
cp .env.example .env
```

`.env.example` contains all configuration options with comments, and the default values are safe for out-of-the-box use. For minimal usage, focus on the following:

```bash
# === Minimal configuration (mock mode, out of the box) ===
APP_PORT=8000
DEBUG=True
API_KEY=               # Empty = no auth
LLM_API_KEY=           # Empty = mock mode
BUSINESS_ADAPTER_MODE=mock  # mock business system

# === Connect a real LLM (optional) ===
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

!!! info "Full configuration guide"
    For detailed explanations, default values, and impact scope of all configuration options, see [Configuration Guide](configuration.md).

---

## :material-server-plus: Optional Dependencies

The following components are not required, but enhance system capabilities when enabled. When not installed or not configured, the system automatically degrades without affecting the main path.

### Redis (Session Persistence)

```bash
pip install redis
```

```bash
# .env configuration
REDIS_URL=redis://localhost:6379/0
```

| Scenario | No Redis (default) | With Redis |
|----------|--------------------|------------|
| Session storage | In-process dict, lost on restart | Cross-process persistence |
| Cache | In-memory dict | Distributed cache |
| Queue | In-memory queue (degraded) | Redis Pub/Sub |

??? note "When you need Redis"
    - **Multi-instance deployment**: Multiple FastAPI processes need to share session state
    - **Session persistence**: Preserve conversation history after service restart
    - **Production environment**: Recommended to ensure reliability

    Redis is not needed for single-machine development; in-memory mode is fully sufficient.

### Elasticsearch (Full-text Retrieval Enhancement)

```bash
pip install elasticsearch
```

```bash
# .env configuration
ELASTICSEARCH_URL=http://localhost:9200
```

BM25 already implements built-in keyword retrieval based on `rank-bm25`. Elasticsearch is for very large knowledge bases (millions of chunks), providing more efficient distributed full-text retrieval.

### Real Business System

```bash
# .env configuration
BUSINESS_ADAPTER_MODE=http
BUSINESS_API_BASE_URL=https://your-business-api.com
BUSINESS_API_KEY=your-business-api-key
BUSINESS_API_TIMEOUT=10
```

The default `mock` mode uses an in-memory mock business system (orders/members/returns/accounts). After switching to `http` mode, BusinessAgent will call the real business system REST API.

!!! warning "http mode degradation"
    When `BUSINESS_ADAPTER_MODE=http` but `BUSINESS_API_BASE_URL` is empty, the system automatically degrades to mock mode and prints a warning log.

---

## :material-check-network: Startup Verification

### 1. Start the Service

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Health Check

```bash
curl http://localhost:8000/api/v1/health
```

A `200` response means the service is healthy:

```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### 3. Component Health Check

```bash
curl http://localhost:8000/api/v1/observability/health
```

Returns the status of each component to confirm whether LLM, vector store, Redis, and disk are ready:

```json
{
  "llm": "mock",
  "vectorstore": "healthy",
  "redis": "degraded",
  "disk": "ok"
}
```

### 4. Chat Verification

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello"}'
```

In mock mode, it should return a chitchat greeting. With a real LLM configured, it returns an LLM-generated reply.

---

## :material-alert-circle: Common Installation Issues

### chromadb Installation Fails

**Symptom**: `pip install chromadb` reports a compile error or dependency conflict.

??? "Solution"

    === "Upgrade pip (preferred)"

        ```bash
        python -m pip install --upgrade pip setuptools wheel
        pip install chromadb
        ```

    === "Install Build Tools on Windows"

        1. Download [Visual C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
        2. During installation, select "Desktop development with C++"
        3. Restart the terminal and re-run `pip install chromadb`

    === "Use pre-built wheel"

        ```bash
        # Force use of pre-built wheel, skip source compilation
        pip install chromadb --only-binary :all:
        ```

### sentence-transformers Downloads Slowly

**Symptom**: First launch hangs on model download, or times out and fails.

??? "Solution"

    ```bash
    # Option 1: Set HuggingFace mirror (recommended)
    export HF_ENDPOINT=https://hf-mirror.com
    # Windows PowerShell
    $env:HF_ENDPOINT = "https://hf-mirror.com"

    # Option 2: Download manually in advance (see "Manual Download" section above)
    # Option 3: The system has built-in mirror fallback, will automatically try hf-mirror.com without extra action
    ```

    Download failure does not block startup — the system auto-degrades to hash fallback vectorization, and the main path runs (but semantic retrieval quality drops).

### LangGraph Unavailable

**Symptom**: Log shows `LangGraph build failed, degrading to synchronous orchestrator`.

??? "Solution"

    **No manual action needed**. This is expected behavior: when LangGraph is unavailable or build fails, the system auto-degrades to the `_SynchOrchestrator` synchronous orchestrator, reusing the same node functions (intent_node / agent_node / dialog_node / escalate_node). Behavior is fully consistent with the LangGraph version, only lacking graph-structure scheduling.

    To enable LangGraph:

    ```bash
    pip install langgraph
    ```

    After installation, restart the service. The log should show `LangGraph orchestrator built successfully`.

### Port Already in Use

**Symptom**: `OSError: [Errno 48] Address already in use`.

??? "Solution"

    ```bash
    # Option 1: Change the port
    python -m uvicorn app.main:app --port 8001

    # Option 2: Modify .env
    # APP_PORT=8001

    # Option 3: Release the occupied port (Linux/macOS)
    lsof -i :8000      # Find the occupying process
    kill -9 <PID>      # Kill the process
    ```

### Out of Memory (OOM)

**Symptom**: Process is killed during startup or retrieval, with no clear error in logs.

??? "Solution"

    ```bash
    # Reduce the embedding batch size to lower peak memory
    # .env configuration
    EMBEDDING_BATCH_SIZE=16   # Default 32, reduce when memory is tight

    # Reduce recall count to lower per-retrieval memory usage
    VECTOR_TOP_K=15
    BM25_TOP_K=15
    ```

    Production environments should have ≥ 8 GB memory and enable Redis to share cache pressure.

### Import Error ModuleNotFoundError

**Symptom**: `ModuleNotFoundError: No module named 'app'`.

??? "Solution"

    Make sure to run commands in the **project root directory** (the level containing the `app/` directory):

    ```bash
    # Check current directory
    ls app/main.py    # You should see this file

    # Must start from the project root
    cd /path/to/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
    ```

---

## :material-arrow-right: Next Steps

- [Configuration Guide](configuration.md) — Understand all configuration options
- [Architecture](architecture.md) — Understand the system design
- [Quick Start in One Minute](quick-start.md) — Quickly verify the installation result
