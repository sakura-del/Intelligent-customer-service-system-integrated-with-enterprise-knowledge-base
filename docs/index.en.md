---
hide:
  - navigation
  - toc
---

# Intelligent Customer Service System Integrated with Enterprise Knowledge Base

<div class="hero" markdown>

# Intelligent Customer Service System Integrated with Enterprise Knowledge Base

An enterprise-grade intelligent customer service system powered by dual engines: "Multi-Agent Collaboration + RAG Knowledge Enhancement"

<div class="badge-row">
  <span class="badge">:material-robot-happy: Multi-Agent Collaboration</span>
  <span class="badge">:material-book-search: Hybrid Retrieval + RAG</span>
  <span class="badge">:material-rocket: Avg Response ≤ 3s</span>
  <span class="badge">:material-account-tie: Agent Assist Workbench</span>
  <span class="badge">:material-chart-line: Langfuse Observability</span>
</div>

[Get Started :material-arrow-right:](quick-start.md){: .md-button .md-button--primary}
[View on GitHub :fontawesome-brands-github:](https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base){: .md-button}

</div>

---

## Core Capabilities

<div class="grid" markdown>

<div class="card" markdown>

### :material-robot-happy: Multi-Agent Collaboration

Based on the LangGraph "1+5" architecture: 1 orchestration Agent coordinates 5 specialized Agents (Knowledge Retrieval / Business Query / Sentiment Analysis / Ticket Processing / Dialog Generation). Automatically degrades to synchronous orchestration when LangGraph is unavailable.

[Learn the Architecture :material-arrow-right:](architecture.md){: .md-button }

</div>

<div class="card" markdown>

### :material-book-search: Hybrid Retrieval + RAG

Query rewriting → vector retrieval + BM25 dual-path recall → RRF fusion → Reranker reranking → LLM generation. No forced answers when similarity is below threshold, achieving Recall@5 = 1.0.

[View Retrieval Pipeline :material-arrow-right:](architecture/rag.md){: .md-button }

</div>

<div class="card" markdown>

### :material-account-tie: Agent Assist Workbench

After escalation, sessions can be taken over by human agents, supporting context continuation, knowledge/business assisted queries, and solution archival back to the knowledge base. 8 API endpoints complete the human-AI collaboration gap.

[View Agent Endpoints :material-arrow-right:](tutorials/agent-assist.md){: .md-button }

</div>

<div class="card" markdown>

### :material-rocket: Performance Optimization

HotQueryCache hot path caching, ModelRouter large/small model routing, IntentCache same-intent reuse, and intent recognition fast path. First token for knowledge Q&A < 1s.

[View Performance Optimization :material-arrow-right:](tutorials/performance.md){: .md-button }

</div>

<div class="card" markdown>

### :material-chart-line: Langfuse Observability

All 11 LLM call points are tagged with prompt name/version. Trace visualization covers the full chain. Token/cost/latency are automatically reported, with automatic degradation when not configured.

[View Observability :material-arrow-right:](tutorials/observability.md){: .md-button }

</div>

<div class="card" markdown>

### :material-shield-check: Fallback Strategy

Seven-layer fallback for LLM / BGE / LangGraph / Redis / Business API / Langfuse ensures availability. The main path never blocks.

[View Fallback Strategy :material-arrow-right:](architecture/fallback.md){: .md-button }

</div>

</div>

---

## Performance Metrics

Validated under a real DeepSeek LLM + BGE embedding environment:

| Metric | Target | Actual | Pass |
|--------|--------|--------|------|
| Recall@5 | ≥ 0.85 | 1.0 | :material-check:{.pass} |
| Hit Rate | ≥ 0.90 | 0.9333 | :material-check:{.pass} |
| Hallucination Rate | ≤ 0.10 | 0.0 | :material-check:{.pass} |
| Independent Resolution Rate | ≥ 60% | 80% | :material-check:{.pass} |
| Avg Response Time | ≤ 3s | 2.27s | :material-check:{.pass} |

---

## Project Structure Overview

```text
app/
├── api/v1/              # Access Layer: REST API endpoints
│   ├── chat.py          # Chat endpoint (sync + SSE streaming)
│   ├── agent.py         # Agent assist endpoints (8)
│   └── ...              # Knowledge base / evaluation / performance / observability / operations
├── agents/              # Agent collaboration layer
│   ├── orchestrator.py  # Orchestration Agent
│   ├── graph.py         # LangGraph state machine orchestration
│   └── ...              # 5 specialized Agents + LLMClient
├── core/                # Core infrastructure
│   ├── session.py       # Session management (incl. agent state)
│   ├── performance.py   # HotQueryCache / ModelRouter
│   └── langfuse_client.py   # Langfuse tracing
├── knowledge/           # Knowledge and data layer
│   ├── hybrid_retriever.py  # Hybrid retrieval
│   └── ...              # Reranker / version management / quality validation
└── schemas/             # Pydantic data models

tests/                   # 668+ test cases
```

---

## Next Steps

- **[Quick Start in One Minute](quick-start.md)**: Launch a minimal viable customer service system locally
- **[Installation Guide](installation.md)**: Full deployment and dependency installation
- **[Configuration Guide](configuration.md)**: Understand the purpose of 30+ configuration options
- **[Architecture Design](architecture.md)**: Deep dive into multi-agent collaboration and RAG retrieval pipeline
