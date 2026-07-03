---
title: Changelog
description: Project version history, sorted in reverse chronological order, including new features, optimizations, and fixes.
weight: 140
---

# Changelog

This project follows [Semantic Versioning](https://semver.org/). Version numbers use the `MAJOR.MINOR.PATCH` format. This page records all changes in reverse chronological order.

!!! info "Version semantics"

    - **MAJOR**: Incompatible API changes
    - **MINOR**: Backward-compatible new features
    - **PATCH**: Backward-compatible bug fixes

---

## v0.4.0

**Release date**: 2026-07-03

**Theme**: Agent assist workbench — Closing the human-agent collaboration loop

### :rocket: New features

- **8 `/api/v1/agent/*` endpoints**: Provide agent-side workbench API support after escalation
  - `GET /sessions/pending`: Pending session list (sorted by `EscalationPriority` descending)
  - `GET /sessions/{session_id}`: Session details (including `EscalationCard` + full `history`)
  - `POST /sessions/{session_id}/accept`: Agent takes over (CAS `pending → assigned`)
  - `POST /sessions/{session_id}/messages`: Agent appends messages to `history`
  - `POST /sessions/{session_id}/knowledge-recommend`: Knowledge recommendation assistance
  - `POST /sessions/{session_id}/business-assist`: Business query assistance (with masking)
  - `POST /sessions/{session_id}/resolve`: Mark as resolved (CAS `assigned → resolved`)
  - `POST /sessions/{session_id}/solution`: Record solution and consolidate back to the knowledge base

- **SessionManager extension**: 4 new fields + 4 CAS methods
  - New fields: `agent_status` / `assigned_agent_id` / `escalation_card` / `resolve_note`
  - New methods: `list_pending_sessions()` / `assign_agent()` / `resolve_session()` / `mark_pending()`
  - All use CAS (Compare-And-Swap) to ensure concurrency safety

- **escalate_node integration**: On escalation, automatically calls `mark_pending` to set `agent_status="pending"` and caches the `EscalationCard` to avoid repeated construction

### :test_tube: Tests

- Added **28 test cases**, covering happy paths, boundary scenarios (404/409/422), and concurrent takeover CAS for all 8 endpoints

### :books: Documentation

- Added [Agent assist workbench tutorial](tutorials/agent-assist.md)
- Updated [API reference](api-reference.en.md) with descriptions for 8 endpoints

??? info "Complete endpoint list"
    | Endpoint | Method | Description |
    |----------|--------|-------------|
    | `/api/v1/agent/sessions/pending` | GET | Pending list |
    | `/api/v1/agent/sessions/{id}` | GET | Session details |
    | `/api/v1/agent/sessions/{id}/accept` | POST | Agent takeover |
    | `/api/v1/agent/sessions/{id}/messages` | POST | Agent sends message |
    | `/api/v1/agent/sessions/{id}/knowledge-recommend` | POST | Knowledge recommendation |
    | `/api/v1/agent/sessions/{id}/business-assist` | POST | Business assistance |
    | `/api/v1/agent/sessions/{id}/resolve` | POST | Mark resolved |
    | `/api/v1/agent/sessions/{id}/solution` | POST | Solution consolidation |

---

## v0.3.0

**Release date**: 2026-07-02

**Theme**: Langfuse LLM observability — Full-chain trace visualization

### :rocket: New features

- **11 LLM call points tagged with prompt name/version**: Covers all LLM calls including intent recognition, query rewriting, knowledge generation, and dialog polishing
  - `recognize_intent_v1`: Intent recognition
  - `query_rewrite_v1`: Query rewriting
  - `knowledge_generate_v1`: Knowledge Q&A generation
  - `dialog_polish_v1`: Dialog polishing
  - And 11 prompt tags in total

- **Dual write to trace and monitor**: Langfuse trace and local `Monitor` record simultaneously, acting as fallback for each other
  - Langfuse: Visualizes the full chain; automatically reports token/cost/latency
  - Monitor: Local trace summary, queryable via `/api/v1/monitor/traces`

- **Automatic no-op fallback when unconfigured**: When `LANGFUSE_ENABLED=False` or credentials are empty, all Langfuse calls degrade to no-ops without affecting main-chain performance

### :gear: Configuration

New Langfuse configuration items (see [.env.example](https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base/blob/main/.env.example)):

```bash
LANGFUSE_ENABLED=False        # Empty or False: degrade all calls to no-op
LANGFUSE_PUBLIC_KEY=          # Obtain from Langfuse Project Settings → API Keys
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

### :test_tube: Tests

- Added Langfuse integration tests, covering both successful reporting and fallback scenarios

??? tip "Fallback trigger conditions"
    - `LANGFUSE_ENABLED=False`
    - `LANGFUSE_PUBLIC_KEY` or `LANGFUSE_SECRET_KEY` is empty
    - Langfuse service connection timeout (default 3 seconds)

---

## v0.2.0

**Release date**: 2026-07-02

**Theme**: Streaming first-token optimization — Perceived wait reduced from 7-8s to <1s

### :rocket: New features

- **HotQueryCache / ModelRouter / IntentCache three-layer optimization**:
  - `HotQueryCache`: High-frequency query cache; on hit, first token <100ms, skipping the full orchestration
  - `ModelRouter`: Lightweight tasks like intent recognition use small models (about 1/10 cost); main LLM only for generation
  - `IntentCache`: Same-intent reuse, avoiding repeated intent recognition

- **Qwen qwen-turbo replaces Doubao lite**: Default small model switched to Qwen for better Chinese understanding
  - `SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`
  - `SMALL_LLM_MODEL=qwen-turbo`

- **Intent fast-track**: High-frequency intents like chitchat/escalation are matched by keywords, skipping LLM intent recognition, with first token <200ms

- **Non-knowledge Q&A intent streaming**: Intents like chitchat / business_query are sliced by sentence-ending punctuation and streamed out, instead of waiting for full generation before single-token output

- **First-token latency monitoring**: New `stream_first_token` instrumentation, queryable via `/api/v1/performance/metrics` for avg/p95

### :zap: Performance improvements

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| First token time (knowledge Q&A) | ~3s | <1s | 67% ↓ |
| First token time (chitchat fast-track) | ~3s | <200ms | 93% ↓ |
| First token time (cache hit) | ~3s | <100ms | 97% ↓ |
| Sync endpoint P95 | 7.94s | 2.27s | 71% ↓ |

### :test_tube: Tests

- Added tests for first-token time, fast-track streaming, and cache hits

??? info "Three-layer cache coordination"
    ```mermaid
    flowchart TD
        A[Request] --> B{HotQueryCache hit?}
        B -- Yes --> C[Return directly, first token <100ms]
        B -- No --> D{IntentCache hit?}
        D -- Yes --> E[Skip intent recognition]
        D -- No --> F[ModelRouter routes to small model]
        F --> G[Small model intent recognition]
        E --> H[Hybrid retrieval + generation]
        G --> H
        H --> I[Write to HotQueryCache]
        I --> J[Return result]
    ```

---

## v0.1.0

**Release date**: 2026-06

**Theme**: Initial version — Multi-agent collaboration + RAG knowledge-enhanced intelligent customer service system

### :rocket: Core features

- **Multi-agent collaboration architecture**: "1+5" architecture based on LangGraph
  - 1 orchestration Agent (OrchestratorAgent): Intent recognition, routing, fallback aggregation
  - 5 specialized Agents: Knowledge retrieval (KnowledgeAgent) / Business query (BusinessAgent) / Emotion analysis (EmotionAgent) / Ticket handling (TicketAgent) / Dialog polishing (DialogAgent)
  - Automatic fallback to synchronous orchestration when LangGraph is unavailable

- **Hybrid retrieval + RAG**:
  - Query rewriting → vector retrieval + BM25 dual-channel recall → RRF fusion → Reranker reordering → LLM generation
  - Refuses to answer when similarity is below threshold, avoiding hallucination
  - Recall@5 = 1.0, Hit Rate = 0.9333, hallucination rate = 0.0

- **Human escalation loop**:
  - Triggered by emotion sensitivity / consecutive failures / user request
  - Generates `EscalationCard` with escalation reason, priority, and context summary
  - Working-hours constraint (`WORKING_HOURS_START` / `WORKING_HOURS_END`)

- **Knowledge base governance**:
  - Document ingestion pipeline (PDF/Word/Markdown/HTML parsing)
  - Quality validation (deduplication, terminology, sensitive words)
  - Version management and rollback
  - Full / incremental / real-time update mechanisms

- **Business system integration**:
  - Order / member / return / account API adapter framework
  - Identity verification, phone masking, two-step confirmation for write operations
  - mock / http dual mode, out of the box

### :gear: Endpoint list (v0.1.0)

| Module | Endpoint count | Description |
|--------|----------------|-------------|
| Chat | 2 | `/chat` + `/chat/stream` |
| Knowledge base | 9 | Ingestion/stats/document management/quality/version/canary |
| Escalation | 3 | Solution submission/review/ingestion |
| Document update | 4 | Full/incremental/real-time/status |
| Ticket mining | 2 | Trigger/status |
| Retrieval tuning | 3 | Query/update/reset |
| Retrieval evaluation | 3 | Run/list/detail |
| Performance monitoring | 3 | Metrics/cache/invalidate |
| Observability | 5 | Circuit breaker/alerts/health/token |
| Monitoring | 5 | Overview/trace/agent/session |
| Operations | 6 | Experiment/dashboard/checklist |
| Health check | 1 | Liveness probe |
| Gateway | 1 | Multi-channel access |

### :test_tube: Tests

- Initial test suite of **640+ cases**, covering core chains and boundary scenarios

### :books: Documentation

- Initial documentation system: Quick start, installation guide, configuration, architecture design, tutorials

??? success "v0.1.0 performance metrics (verified with real LLM)"
    | Metric | Target | Actual | Pass |
    |--------|--------|--------|------|
    | Recall@5 | ≥ 0.85 | 1.0 | ✅ |
    | Hit Rate | ≥ 0.90 | 0.9333 | ✅ |
    | Hallucination rate | ≤ 0.10 | 0.0 | ✅ |
    | Independent resolution rate | ≥ 60% | 80% | ✅ |
    | Average response time | ≤ 3s | 2.27s | ✅ |

---

## Version planning

??? info "Next version v0.5.0 plan (draft)"
    - Frontend agent workbench UI (consumes 8 agent endpoints)
    - Knowledge base management console UI
    - Multi-language support (English)
    - Elasticsearch full-text search integration

---

## Related documentation

- [Contributing guide](contributing.en.md): Version release process
- [API reference](api-reference.en.md): Current complete endpoints
- [Architecture design](architecture.md): Overall system architecture
