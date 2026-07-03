---
title: Quick Start in One Minute
description: Launch the intelligent customer service system in three steps. Defaults to no authentication, mock business system, and in-memory queue. Works out of the box without an LLM Key.
---

# :material-rocket-launch: Quick Start in One Minute

This guide helps you get the intelligent customer service system running in the shortest time. The system starts in **mock mode** by default, requiring no LLM API Key, no Redis, and no real business system. Just clone and run.

!!! tip "Minimal viable note"
    Under default configuration, the system runs in **mock mode**: when `LLM_API_KEY` is left empty, `_MockLLM` is automatically enabled and assembles a reply from the user message; `BUSINESS_ADAPTER_MODE=mock` uses an in-memory mock business system; when Redis is not configured, it degrades to an in-memory queue. **The full chain runs even without an LLM**, making it ideal for local development and demos.

---

## :material-check-circle: Prerequisites

| Item | Requirement |
|------|-------------|
| Python | 3.11 or above |
| Operating System | Windows / macOS / Linux all supported |
| Disk Space | ~2 GB (incl. BGE model weights) |
| Network | Internet required on first launch to download the BGE model (optional, auto-degrades on download failure) |

---

## :material-step-forward: Three-Step Quick Start

### Step 1: Clone the Repository

```bash
git clone https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base.git
cd Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base
```

### Step 2: Install Dependencies

```bash
# Recommended: use a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install all dependencies
pip install -r requirements.txt
```

!!! note "Dependency notes"
    Key dependencies include: `fastapi`, `langgraph`, `langfuse`, `openai`, `chromadb`, `sentence-transformers`, `rank-bm25`. See [Installation Guide](installation.md) for detailed installation troubleshooting.

### Step 3: Start the Service

```bash
# Copy the environment variable template (all empty by default, works out of the box)
cp .env.example .env

# Start the service
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

After a successful start, the console outputs something like:

```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
```

!!! success "Startup verification"
    Open your browser and visit the following addresses to confirm the service is running:
    
    - **Chat interface**: [http://localhost:8000/](http://localhost:8000/)
    - **API documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
    - **Monitoring dashboard**: [http://localhost:8000/monitor](http://localhost:8000/monitor)
    - **Operations dashboard**: [http://localhost:8000/operations](http://localhost:8000/operations)

    You can also quickly verify via command line:

    ```bash
    curl http://localhost:8000/api/v1/health
    # A 200 response means the service is healthy
    ```

---

## :material-chat: First Chat Example

Once the service is running, you can call the chat endpoint. Authentication is disabled by default (`API_KEY` empty), so no `X-API-Key` header is required.

=== "curl"

    ```bash
    curl -X POST http://localhost:8000/api/v1/chat \
      -H "Content-Type: application/json" \
      -d '{"message": "I forgot my login password. What should I do?"}'
    ```

    Expected response:

    ```json
    {
      "reply": "You can use the \"Forgot Password\" link on the login page...",
      "session_id": "a1b2c3d4-...",
      "intent": "knowledge_qa",
      "sources": ["faq.md"],
      "escalate_to_human": false
    }
    ```

=== "Python requests"

    ```python
    import requests

    # Call the chat endpoint (no auth by default)
    response = requests.post(
        "http://localhost:8000/api/v1/chat",
        json={"message": "I forgot my login password. What should I do?"},
        timeout=30,
    )

    data = response.json()
    print(f"Reply: {data['reply']}")
    print(f"Intent: {data['intent']}")
    print(f"Sources: {data['sources']}")
    print(f"Escalate to human: {data['escalate_to_human']}")
    ```

=== "Python httpx (async)"

    ```python
    import asyncio
    import httpx

    async def chat() -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "http://localhost:8000/api/v1/chat",
                json={"message": "What features does the product have"},
            )
            data = resp.json()
            print(data["reply"])

    asyncio.run(chat())
    ```

!!! info "Replies in mock mode"
    If `LLM_API_KEY` is not configured, the system uses `_MockLLM` and assembles a reply from knowledge base retrieval results. Although the reply is not LLM-generated, the retrieval chain (vector + BM25 + RRF + Reranker) runs in full, which can be used to verify knowledge base ingestion and retrieval effectiveness.

---

## :material-waveform: Streaming Chat Example (SSE)

The streaming endpoint `/api/v1/chat/stream` returns tokens one-by-one via Server-Sent Events, with lower first-token latency and a smoother experience.

=== "curl"

    ```bash
    # -N disables buffering for real-time SSE output
    curl -N -X POST http://localhost:8000/api/v1/chat/stream \
      -H "Content-Type: application/json" \
      -d '{"message": "What features does the product have"}'
    ```

    Output format (one event per line):

    ```
    data: {"type": "meta", "session_id": "a1b2c3d4-..."}

    data: {"type": "token", "content": "Our"}

    data: {"type": "token", "content": "product"}

    data: {"type": "token", "content": "offers"}

    data: {"type": "done", "sources": ["faq.md"]}
    ```

=== "Python (sseclient)"

    ```python
    import json
    import requests

    # Stream-read SSE events
    response = requests.post(
        "http://localhost:8000/api/v1/chat/stream",
        json={"message": "What features does the product have"},
        stream=True,
        timeout=60,
    )

    # Parse the SSE data field line by line
    for line in response.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        payload = json.loads(line[6:])
        if payload["type"] == "token":
            print(payload["content"], end="", flush=True)
        elif payload["type"] == "done":
            print(f"\n\nSources: {payload.get('sources', [])}")
    ```

!!! warning "Streaming behavior in mock mode"
    Mock mode has no real LLM streaming output; the system returns the assembled reply as a single token event. Token-by-token streaming is only available after configuring a real LLM.

---

## :material-database-plus: Ingest Your First Knowledge Document

To make the system answer your business questions, you first need to ingest knowledge documents. PDF, Word, HTML, and Markdown formats are supported.

```bash
# Upload a FAQ document (mock mode requires no auth, no X-API-Key needed)
curl -X POST http://localhost:8000/api/v1/knowledge/ingest \
  -F "file=@docs/faq.md" \
  -F "knowledge_type=faq"
```

After ingestion, chat again and the system will answer based on your knowledge base:

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is your return policy?"}'
```

!!! tip "Knowledge base statistics"
    Call `GET /api/v1/knowledge/stats` to view the number of ingested documents, chunks, and vector store status.

---

## :material-tune-vertical: Enable a Real LLM (Optional)

Mock mode is only for validating the flow. To get real LLM generation, edit `.env` with the configuration:

```bash
# Edit the .env file
LLM_API_KEY=sk-your-deepseek-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

**Restart the service** to apply changes. The system will automatically use DeepSeek for intent recognition, Query rewriting, and answer generation.

??? tip "Why recommend DeepSeek?"
    The project is validated under a real DeepSeek + BGE environment: Recall@5=1.0, avg response 2.27s, hallucination rate=0. DeepSeek excels at Chinese understanding at a lower cost, making it suitable for enterprise customer service scenarios. You can also use any OpenAI-compatible interface (such as Qwen, Doubao, GPT-4o-mini).

---

## :material-arrow-right-bottom: Next Steps

| I want to… | See |
|------------|-----|
| Understand full installation and troubleshooting | [Installation Guide](installation.md) |
| Understand all configuration options | [Configuration Guide](configuration.md) |
| Understand the overall system architecture | [Architecture](architecture.md) |
| Understand multi-agent collaboration | [Multi-Agent Collaboration](architecture/agents.md) |
| Understand the RAG retrieval pipeline | [RAG Retrieval Pipeline](architecture/rag.md) |
| Understand fallback and fault tolerance | [Fallback Strategy](architecture/fallback.md) |
| View all API endpoints | [API Reference](api-reference.md) |

---

!!! question "Having issues?"
    - **Startup error**: Check Python version ≥ 3.11 and that dependencies are fully installed.
    - **chromadb install fails**: Upgrade pip (`pip install --upgrade pip`) or install Visual C++ Build Tools.
    - **BGE model downloads slowly**: Set `HF_ENDPOINT=https://hf-mirror.com` to use a domestic mirror.
    - **Port already in use**: Change `APP_PORT` in `.env` or add `--port 8001` at startup.

    For more troubleshooting, see [Installation Guide · Common Issues](installation.md#common-installation-issues).
