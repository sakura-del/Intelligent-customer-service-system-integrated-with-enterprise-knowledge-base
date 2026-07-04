---
title: FAQ
description: Common questions across the four categories of installation, configuration, usage, and performance, organized by category with collapsible answers.
weight: 120
---

# FAQ

Organized into the four categories of **Installation / Configuration / Usage / Performance**. Click a question to expand the answer. If you cannot find an answer, please submit an issue at [GitHub Issues](https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base/issues).

---

## Installation

??? question "Q: What should I do if chromadb installation fails?"

    **A:** chromadb depends on `onnxruntime` and `pypika`. Common failure causes and solutions:

    === "Upgrade pip and build tools"

        ```bash
        # Upgrade pip to the latest version to avoid older versions failing to resolve the dependency tree
        python -m pip install --upgrade pip setuptools wheel
        # Reinstall
        pip install -r requirements.txt
        ```

    === "Install VS Build Tools on Windows"

        Some dependencies (such as `hnswlib`) require a C++ build environment:

        1. Download [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
        2. During installation, select the "Desktop development with C++" workload
        3. Restart the terminal and re-run `pip install`

    === "Use prebuilt wheels"

        ```bash
        # Prefer prebuilt versions to skip source compilation
        pip install chromadb --only-binary :all:
        ```

??? question "Q: The BGE embedding model downloads very slowly. What should I do?"

    **A:** The BGE model is hosted on Hugging Face. If access is slow in your region, use a mirror endpoint.

    ```bash
    # Option 1: set the HF mirror endpoint (recommended)
    export HF_ENDPOINT=https://hf-mirror.com
    pip install -r requirements.txt

    # Option 2: manually download the model weights to a local path and specify it in .env
    # git clone https://hf-mirror.com/BAAI/bge-large-zh-v1.5 models/bge-large-zh
    # In .env set:
    # EMBEDDING_MODEL=./models/bge-large-zh
    ```

    !!! tip "First load is cached"
        After the first load, the model is cached under `~/.cache/huggingface/`; subsequent startups do not need to re-download it.

??? question "Q: Can I use Python 3.10?"

    **A:** We recommend **Python 3.11+**. Some dependencies may be incompatible on 3.10.

    | Version | Support | Notes |
    |----------|---------|------|
    | 3.11+ | ✅ Recommended | All dependencies tested successfully |
    | 3.10 | ⚠️ Partially compatible | Some `chromadb` / `langfuse` features may behave abnormally |
    | 3.9 and below | ❌ Not supported | Type annotations and syntax incompatible |

    ```bash
    # Recommend managing the Python version with pyenv or conda
    pyenv install 3.11.9
    pyenv local 3.11.9
    ```

??? question "Q: What happens if Redis is not started?"

    **A:** The system automatically degrades to an in-memory queue without affecting main-chain functionality, but sessions and caches are not persisted.

    ```bash
    # Recommend starting Redis with Docker
    docker run -d --name redis -p 6379:6379 redis:7-alpine

    # Verify the connection
    redis-cli ping  # should return PONG
    ```

    !!! warning "Redis must be started in production"
        In production, if Redis is not started, all sessions and caches are lost on process restart.

---

## Configuration

??? question "Q: Is it safe to leave API_KEY empty?"

    **A:** Only for local development mode. **Production must set a non-empty `API_KEY`**.

    - When `API_KEY` is empty, the `verify_api_key` dependency skips authentication; any caller can access the system
    - When `API_KEY` is non-empty, all endpoints marked ✅ require a matching `X-API-Key` request header

    ```bash
    # .env example
    # Development mode (local debugging)
    API_KEY=

    # Production mode (required)
    API_KEY=your-strong-random-key-here
    ```

    !!! danger "Security risk"
        Leaving `API_KEY` empty in production is equivalent to fully opening the interface, which can be exploited by any caller, including sensitive operations like ingestion and deletion.

??? question "Q: What happens if SMALL_LLM_API_KEY is not configured?"

    **A:** `ModelRouter` automatically falls back to the main LLM, with no side effects. Only the first-token latency increases slightly.

    | Configuration | Behavior | First-Token Latency |
    |---------------|----------|---------------------|
    | Small model + main model configured | Intent recognition on small model; generation on main model | ~200ms |
    | Only main model configured | Everything on the main model | ~800ms |
    | Neither configured | Mock mode (no real LLM) | <50ms |

    ```bash
    # .env example (Qwen qwen-turbo)
    SMALL_LLM_API_KEY=sk-xxxxx
    SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    SMALL_LLM_MODEL=qwen-turbo
    SMALL_MODEL_THRESHOLD=0.5
    ```

??? question "Q: Will incorrect Langfuse configuration block the main chain?"

    **A:** No. The Langfuse client has a built-in fallback; on configuration errors it automatically becomes a no-op.

    ```python
    # Degradation logic in app/core/langfuse_client.py (simplified)
    if not settings.LANGFUSE_ENABLED or not settings.LANGFUSE_PUBLIC_KEY:
        # All methods return None without raising
        return NoOpLangfuseTrace()
    ```

    Conditions that trigger fallback:

    - `LANGFUSE_ENABLED=False`
    - `LANGFUSE_PUBLIC_KEY` or `LANGFUSE_SECRET_KEY` is empty
    - Langfuse service connection timeout (default 3 seconds)

    !!! note "Impact after fallback"
        After fallback, traces are not reported to Langfuse, but the local `Monitor` still records trace summaries, viewable via `/api/v1/monitor/traces`.

??? question "Q: Should BUSINESS_ADAPTER_MODE be mock or http?"

    **A:** Use `mock` for development and testing; switch to `http` when integrating with a real business system.

    ```bash
    # mock mode: in-memory simulated order/membership/return APIs; out of the box
    BUSINESS_ADAPTER_MODE=mock

    # http mode: calls the real business system REST API
    BUSINESS_ADAPTER_MODE=http
    BUSINESS_API_BASE_URL=https://your-business-api.com
    BUSINESS_API_KEY=your-business-api-key
    BUSINESS_API_TIMEOUT=10
    ```

    !!! warning "http mode fallback"
        When `BUSINESS_API_BASE_URL` is empty, the system automatically falls back to mock and emits a warning; it does not block business queries.

??? question "Q: How do I adjust working hours (the escalation time window)?"

    **A:** Modify `WORKING_HOURS_START` / `WORKING_HOURS_END` in `.env`:

    ```bash
    # 24-hour format, half-open interval [START, END). Outside this range, emotion/failure requests are not proactively escalated
    WORKING_HOURS_START=9
    WORKING_HOURS_END=18
    TIMEZONE=Asia/Shanghai
    ```

    !!! note "User-initiated transfers are not constrained by the time window"
        Even outside working hours, if the user explicitly says "transfer to human", escalation is still triggered. Only system-initiated escalations are constrained by the time window.

---

## Usage

??? question "Q: Answers don't change after a knowledge base update. What should I do?"

    **A:** You need to call `/api/v1/performance/cache/invalidate` to clear the hot cache.

    ```bash
    # Clear the hot cache; the next query will go through retrieval again
    curl -X POST http://localhost:8000/api/v1/performance/cache/invalidate \
      -H "X-API-Key: $API_KEY"
    ```

    **Reason**: `HotQueryCache` caches replies for high-frequency queries (default 1000 entries). After a knowledge base update, the cache still returns stale replies.

    !!! tip "Auto-clear cache after batch ingestion"
        The [batch ingestion script](examples.en.md) already includes a cache-clear call, so no manual clearing is needed.

??? question "Q: Streaming responses (SSE) frequently disconnect. What should I do?"

    **A:** Usually Nginx / CDN is buffering the SSE stream; you need to disable buffering.

    === "Nginx configuration"

        ```nginx
        location /api/v1/chat/stream {
            proxy_pass http://backend;
            proxy_buffering off;          # disable response buffering
            proxy_cache off;              # disable caching
            chunked_transfer_encoding on; # enable chunked transfer
            proxy_read_timeout 300s;      # extend read timeout
            proxy_set_header Connection '';  # clear the Connection header
        }
        ```

    === "Cloudflare CDN"

        Cloudflare buffers SSE by default; disable it in Page Rules:
        - Match: `*/api/v1/chat/stream*`
        - Setting: `Cache Level: Bypass`

    === "Client verification"

        ```bash
        # -N disables curl buffering; verify whether SSE returns in real time
        curl -N -X POST http://localhost:8000/api/v1/chat/stream \
          -H "X-API-Key: $API_KEY" \
          -H "Content-Type: application/json" \
          -d '{"message": "test streaming"}'
        ```

    The system already sets the `X-Accel-Buffering: no` response header, but some proxies still require explicit configuration.

??? question "Q: Is the session history retained after escalation?"

    **A:** Yes. After escalation, the session `history` is fully retained; the agent can view the entire context.

    ```bash
# View session details; the history field contains all conversation records
curl http://localhost:8000/api/v1/agent/sessions/$SESSION_ID \
  -H "X-API-Key: $API_KEY" | jq .history
```

    The returned `history` includes all user / assistant messages from before the escalation, so the agent can quickly understand the user's request.

??? question "Q: How does the system decide whether to escalate to a human?"

    **A:** The system automatically triggers escalation in the following three scenarios:

    | Trigger | Description |
    |----------|-------------|
    | User explicit request | Matches keywords such as "transfer to human" or "human agent" |
    | Emotion-sensitive intent | Emotion score below threshold; recognized as `emotion_sensitive` |
    | Consecutive failures reach threshold | `failed_attempts >= ESCALATE_FAILED_THRESHOLD` (default 3) |

    After escalation, an `EscalationCard` is generated with the escalation reason, priority, and context summary, and is passed to the agent workbench.

??? question "Q: How do I enter a human solution to consolidate back to the knowledge base?"

    **A:** Enter it via `/api/v1/agent/sessions/{session_id}/solution` or `/api/v1/escalation/solution`.

    ```bash
    # Option 1: agent endpoint (recommended; associated with the session)
    curl -X POST http://localhost:8000/api/v1/agent/sessions/$SESSION_ID/solution \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "question": "How do I return a product?",
        "solution": "Please click Return on the order page...",
        "intent": "knowledge_qa"
      }'

    # Option 2: escalation endpoint (independent of a session)
    curl -X POST http://localhost:8000/api/v1/escalation/solution \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"session_id": "xxx", "question": "...", "solution": "..."}'
    ```

    After entry, the solution enters the `pending` review queue. Once approved and ingested as a FAQ, the next bot retrieval can match it.

??? question "Q: How do I view traces to troubleshoot?"

    **A:** The system provides two ways to view traces:

    === "Local Monitor"

        ```bash
        # View the 10 most recent traces
        curl "http://localhost:8000/api/v1/monitor/traces?limit=10" | jq .

        # View a single trace's details (including per-step duration)
        curl http://localhost:8000/api/v1/monitor/traces/$TRACE_ID | jq .
        ```

    === "Langfuse console"

        After configuring `LANGFUSE_ENABLED=True`, visit [Langfuse Cloud](https://cloud.langfuse.com) to view:

        - Full-chain trace visualization
        - name/version for the 11 prompt markers
        - Automatic token / cost / latency statistics

        See [Example 5: Integrating with Langfuse](examples.en.md).

---

## Performance

??? question "Q: The first query is very slow (~2.7 seconds). What should I do?"

    **A:** The main cost is the DeepSeek query rewrite phase; you can optimize by disabling `query_rewrite`.

    **Latency breakdown** (typical scenario):

    | Phase | Latency | Notes |
    |-------|---------|-------|
    | Intent recognition | ~300ms | Small model (qwen-turbo) |
    | Query rewrite | ~1500ms | Main LLM (DeepSeek); blocks the main chain |
    | Hybrid retrieval | ~280ms | Vector + BM25 + RRF |
    | Reranking | ~150ms | BGE reranker |
    | Generation | ~500ms | Main LLM streaming generation |

    **Optimization options**:

    ```bash
    # Option 1: disable query rewrite (sacrifice a little recall for latency)
    # Set enable_query_rewrite to false in retrieval_tuner
    curl -X PUT http://localhost:8000/api/v1/tuner/params \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"enable_query_rewrite": false}'

    # Option 2: use the streaming endpoint (first token <1s; users perceive it as faster)
    curl -N -X POST http://localhost:8000/api/v1/chat/stream \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"message": "return process"}'
    ```

    !!! tip "Hot cache hit gives first token <100ms"
        The second request with the same query hits `HotQueryCache`, skipping all orchestration; first token <100ms.

??? question "Q: Responses slow down under heavy concurrency. What should I do?"

    **A:** Adjust retrieval parameters and enable the hot cache to reduce per-request overhead.

    === "Adjust retrieval parameters"

        ```bash
        # Lower VECTOR_TOP_K and BM25_TOP_K to reduce recall volume
        curl -X PUT http://localhost:8000/api/v1/tuner/params \
          -H "X-API-Key: $API_KEY" \
          -H "Content-Type: application/json" \
          -d '{
            "vector_top_k": 15,
            "bm25_top_k": 15,
            "rerank_top_k": 3
          }'
        ```

        | Parameter | Default | Recommended (high concurrency) | Impact |
        |-----------|---------|--------------------------------|--------|
        | `VECTOR_TOP_K` | 25 | 15 | Vector recall volume |
        | `BM25_TOP_K` | 25 | 15 | Keyword recall volume |
        | `RERANK_TOP_K` | 5 | 3 | Number of chunks entering generation |

    === "Confirm the hot cache is effective"

        ```bash
        # View the cache hit rate; should be >50% to be considered effective
        curl http://localhost:8000/api/v1/performance/cache/stats | jq .
        ```

        If the hit rate is low, check whether `HotQueryCache` is enabled, or increase the cache capacity.

    === "View comprehensive performance metrics"

        ```bash
        curl http://localhost:8000/api/v1/performance/metrics | jq .
        # Focus on cache_hit_rate / avg_response_ms / concurrent_requests
        ```

??? question "Q: Memory usage is too high. What should I do?"

    **A:** Adjust `EMBEDDING_BATCH_SIZE` and ChromaDB caching strategy.

    ```bash
    # .env adjust batch size to lower the per-embedding memory peak
    EMBEDDING_BATCH_SIZE=16   # default 32; halve when memory is tight

    # ChromaDB persistence directory to avoid in-memory indexes
    CHROMA_PERSIST_DIR=./chroma_data
    ```

    **Main sources of memory usage**:

    | Component | Usage | Optimization |
    |-----------|-------|--------------|
    | BGE embedding model | ~2GB | Model resident in memory; cannot be released |
    | ChromaDB index | ~500MB | Persist to disk; reduce in-memory indexes |
    | LLM client connection pool | ~100MB | Reuse connections; avoid repeated creation |
    | Session history | Depends on concurrency | Adjust `MAX_HISTORY_LENGTH` |

    !!! tip "Container deployment recommendation"
        For Docker deployment, reserve 4GB of memory. For Kubernetes, set `resources.limits.memory: 4Gi`.

??? question "Q: How do I monitor token usage to avoid budget overruns?"

    **A:** View usage via the `/api/v1/observability/token-usage` endpoint, combined with the alerting mechanism.

    ```bash
    # View hourly token usage
    curl "http://localhost:8000/api/v1/observability/token-usage?window=hour" | jq .

    # View the alert list (including token over-budget alerts)
    curl "http://localhost:8000/api/v1/observability/alerts?source=token_usage" | jq .
    ```

    !!! tip "ModelRouter automatically reduces cost"
        After configuring `SMALL_LLM_API_KEY`, lightweight tasks such as intent recognition use the small model (cost is about 1/10 of GPT-4o-mini); the main LLM is only used for generation.

??? question "Q: How do I evaluate whether retrieval effectiveness meets the bar?"

    **A:** Call `/api/v1/evaluation/run` to trigger an evaluation; focus on Recall@5 and Hit Rate.

    ```bash
    # Evaluate with the built-in 30-case test set
    curl -X POST http://localhost:8000/api/v1/evaluation/run \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{}' | jq .

    # View historical reports
    curl http://localhost:8000/api/v1/evaluation/reports | jq .
    ```

    **Pass criteria** (project measurements):

    | Metric | Target | Measured |
    |--------|--------|----------|
    | Recall@5 | ≥ 0.85 | 1.0 |
    | Hit Rate | ≥ 0.90 | 0.9333 |
    | Hallucination rate | ≤ 0.10 | 0.0 |

---

## Related Documentation

- [API Reference](api-reference.en.md): detailed descriptions of all endpoints
- [Examples](examples.en.md): 5 hands-on examples
- [Configuration](configuration.md): 30+ configuration items explained
- [Performance Optimization](tutorials/performance.md): three-layer caching and concurrency optimization
- [Contributing Guide](contributing.en.md): guidelines for submitting Issues and PRs
