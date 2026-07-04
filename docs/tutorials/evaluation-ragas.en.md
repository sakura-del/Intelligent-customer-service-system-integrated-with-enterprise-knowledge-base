---
title: RAGAS Generation Quality Evaluation Tutorial
description:
  End-to-end RAG quality evaluation with RAGAS, covering the four core metrics
  (Faithfulness, Answer Relevancy, Context Precision, Context Recall), API endpoint usage,
  the built-in test set, fallback strategy, and continuous monitoring recommendations.
---

# RAGAS Generation Quality Evaluation Tutorial

Retrieval evaluation (`/api/v1/evaluation/run`) only measures "did retrieval hit the target"; it cannot answer "is the final reply faithful and on-topic". RAGAS (Retrieval-Augmented Generation Assessment) fills this gap: it uses an LLM to score the full chain of "question → retrieved context → generated answer → reference answer", quantifying end-to-end RAG quality. This tutorial covers the four core metrics, the three API endpoints, the built-in test set, fallback strategy, and continuous monitoring recommendations.

!!! info "Prerequisites"

    - Endpoint prefix is `/api/v1/evaluation/ragas`, authenticated via the `X-API-Key` header
    - Requires the `ragas` library (`pip install ragas`) and a configured `LLM_API_KEY`; otherwise `POST /ragas/run` returns 503
    - Evaluation reuses the existing `RAGAgent` (retrieval + generation) and `LLMClient` (scoring); no additional LLM service required
    - Reports are persisted to the `{CHROMA_PERSIST_DIR}/ragas_reports/` directory

---

## Four Core RAGAS Metrics

RAGAS scores automatically via an LLM. No manual annotation of expected sources is needed; only `question` and `ground_truth` (reference answer) are required for end-to-end evaluation.

| Metric | Meaning | Evaluates | Ideal |
| --- | --- | --- | --- |
| **Faithfulness** | Whether the answer is faithful to retrieved context, without fabricated information | Generation quality | Higher is better (0-1) |
| **Answer Relevancy** | Whether the answer actually addresses the user's question | Generation quality | Higher is better (0-1) |
| **Context Precision** | The proportion of relevant content in the retrieved context | Retrieval quality | Higher is better (0-1) |
| **Context Recall** | Whether the retrieved context covers all information needed by the reference answer | Retrieval quality | Higher is better (0-1) |

!!! tip "Collaborative diagnosis with the four metrics"

    - **Low Faithfulness** → the model tends to hallucinate; check the prompt and LLM model choice
    - **Low Answer Relevancy** → the answer is off-topic; the retrieved context may be noisy or instruction-following is weak
    - **Low Context Precision** → too many irrelevant chunks retrieved; reduce `top_k` or raise the rerank threshold
    - **Low Context Recall** → insufficient retrieval; increase `top_k` or check the embedding model and chunk size

---

## API Endpoint Overview

| Endpoint | Method | Description | Auth |
| --- | --- | --- | :---: |
| `/api/v1/evaluation/ragas/run` | POST | Trigger a RAGAS evaluation run | ✅ |
| `/api/v1/evaluation/ragas/reports` | GET | List historical RAGAS report summaries | ✅ |
| `/api/v1/evaluation/ragas/reports/{report_id}` | GET | Query a single RAGAS report's details | ✅ |

---

## Trigger an Evaluation: POST /api/v1/evaluation/ragas/run

Runs the full "retrieve context → generate answer → LLM scoring" chain for each case, aggregates the four metrics, and persists the report.

### Request Body

```json
{
  "testset_path": null,
  "top_k": null
}
```

| Field | Type | Required | Default | Description |
| --- | --- | :---: | --- | --- |
| `testset_path` | string | ❌ | Built-in 12 cases | External test set JSON path; uses built-in default when empty |
| `top_k` | int | ❌ | `RAGAgent` default | Retrieval Top-K, range 1–50 |

### Examples

=== "curl"

    ```bash
    # Run RAGAS evaluation with the built-in test set
    curl -X POST http://localhost:8000/api/v1/evaluation/ragas/run \
      -H "Content-Type: application/json" \
      -H "X-API-Key: ${API_KEY}" \
      -d '{}'
    ```

    ```bash
    # Specify an external test set and top_k
    curl -X POST http://localhost:8000/api/v1/evaluation/ragas/run \
      -H "Content-Type: application/json" \
      -H "X-API-Key: ${API_KEY}" \
      -d '{"testset_path": "tests/sample_data/ragas_testset.json", "top_k": 5}'
    ```

=== "Python (httpx)"

    ```python
    import httpx

    # RAGAS evaluation involves many LLM calls; relax the timeout
    resp = httpx.post(
        "http://localhost:8000/api/v1/evaluation/ragas/run",
        headers={"X-API-Key": ""},
        json={"top_k": 5},
        timeout=600.0,  # 12 cases x retrieval+generation+scoring takes a while
    )
    report = resp.json()
    print(f"report_id: {report['report_id']}")
    print(f"faithfulness:        {report['faithfulness']:.3f}")
    print(f"answer_relevancy:    {report['answer_relevancy']:.3f}")
    print(f"context_precision:   {report['context_precision']:.3f}")
    print(f"context_recall:      {report['context_recall']:.3f}")
    ```

### Response Body (RagasEvaluationReport)

```json
{
  "report_id": "20260704_103045_a1b2c3d4",
  "created_at": "2026-07-04T10:30:45",
  "total_queries": 12,
  "faithfulness": 0.8723,
  "answer_relevancy": 0.9015,
  "context_precision": 0.8456,
  "context_recall": 0.7890,
  "duration_seconds": 187.45,
  "source": "default",
  "case_details": [
    {
      "question": "What should I do if I forget my login password?",
      "ground_truth": "If you forget your login password, click \"Forgot Password\" on the login page...",
      "answer": "You can click \"Forgot Password\" on the login page and enter your registered email...",
      "contexts": [
        "When a user forgets their password, they can reset it via the \"Forgot Password\" entry...",
        "The reset link is valid for 30 minutes..."
      ],
      "faithfulness": 0.92,
      "answer_relevancy": 0.95,
      "context_precision": 0.88,
      "context_recall": 0.85,
      "error": null
    }
  ]
}
```

!!! warning "Evaluation takes time"

    RAGAS runs "retrieve → generate → LLM score" for each case, and internally invokes the LLM separately for each of the four metrics. 12 built-in cases × 4 metrics ≈ dozens of LLM calls, typically taking 2–5 minutes in total. Run during off-peak hours, or shrink the case set via an external test set.

!!! note "Value of the case_details field"

    The `case_details` array in the response lists the full chain data (question/answer/contexts) and the four metric scores per case, making it easy to triage individual case quality. For example, if a case's `faithfulness` is notably below the average, you can inspect its retrieved context and generated answer specifically.

---

## Query Historical Reports

### GET /api/v1/evaluation/ragas/reports — List Historical Report Summaries

```bash
curl http://localhost:8000/api/v1/evaluation/ragas/reports \
  -H "X-API-Key: ${API_KEY}"
```

```json
[
  {
    "report_id": "20260704_103045_a1b2c3d4",
    "created_at": "2026-07-04T10:30:45",
    "total_queries": 12,
    "faithfulness": 0.8723,
    "answer_relevancy": 0.9015,
    "context_precision": 0.8456,
    "context_recall": 0.7890,
    "duration_seconds": 187.45,
    "source": "default"
  }
]
```

Returned in descending `created_at` order; newest report first.

### GET /api/v1/evaluation/ragas/reports/{report_id} — Query a Single Report's Details

```bash
curl http://localhost:8000/api/v1/evaluation/ragas/reports/20260704_103045_a1b2c3d4 \
  -H "X-API-Key: ${API_KEY}"
```

Returns the full `RagasEvaluationReport` (including `case_details`). Returns `404` if `report_id` does not exist.

---

## Built-in Test Set

The system ships with 12 built-in RAGAS test cases covering core customer service scenarios, so you can run end-to-end evaluation without preparing external data.

| Scenario | Cases | Sample Questions |
| --- | :---: | --- |
| Account & Login | 2 | Forgot password, change bound phone number |
| Order & Payment | 2 | Payment failure with funds deducted, modify shipping address |
| Return & Exchange Policy | 4 | 7-day no-reason return scope, return shipping fees, exchange period, refund arrival time |
| Membership & Points | 2 | Membership tier upgrade, points validity period |
| Product Manual & System Features | 1 | Supported knowledge base document formats |
| Customer Service Hotline | 1 | Customer service hotline and hours |

Each case contains only `question` and `ground_truth` (reference answer); no manual annotation of expected sources is required:

```json
{
  "question": "What should I do if I forget my login password?",
  "ground_truth": "If you forget your login password, click \"Forgot Password\" on the login page, enter your registered email, and the system will send a reset link to your inbox. Click the link to reset your password. The reset link is valid for 30 minutes."
}
```

### External Test Set Format

An external test set is a JSON file with the same structure as the built-in set:

```json
{
  "cases": [
    {
      "question": "Who bears the return shipping fee?",
      "ground_truth": "Quality-issue returns are paid by the merchant; non-quality-issue returns are paid by the buyer."
    },
    {
      "question": "How do I upgrade my membership tier?",
      "ground_truth": "Membership tier is based on the cumulative valid spending in the past 90 days: Silver at 500, Gold at 2000, Diamond at 8000."
    }
  ],
  "meta": {
    "version": "custom-v1",
    "description": "Custom RAGAS test set"
  }
}
```

!!! tip "Fallback when loading an external test set fails"

    When the external file does not exist or is malformed, the system automatically falls back to the built-in default set and logs a warning; evaluation is not interrupted.

---

## Fallback Strategy

RAGAS depends on the `ragas` library and an LLM service; when either is missing, it degrades gracefully to keep the main chain available.

| Fallback Scenario | Trigger | Behavior |
| --- | --- | --- |
| RAGAS unavailable | `ragas` not installed **or** `LLM_API_KEY` empty | `POST /ragas/run` returns `503 Service Unavailable` with an error description |
| RAGAS call exception | ragas evaluate raises (API version differences, etc.) | Evaluation is not interrupted; affected cases return zero metrics with the cause recorded in `error` |
| External test set load failure | File missing or JSON parse error | Falls back to the built-in default set |
| Report persistence failure | Disk full or insufficient permission | Only logs a warning; the returned result is unaffected |

### 503 Response Example

```json
{
  "detail": "RAGAS 评估需要安装 ragas 库并配置 LLM_API_KEY"
}
```

!!! note "No impact on retrieval evaluation"

    RAGAS fallback only affects the three `/api/v1/evaluation/ragas/*` endpoints. The existing `/api/v1/evaluation/run` (retrieval evaluation) and `/api/v1/evaluation/reports` are unaffected and remain usable.

---

## Difference from Retrieval Evaluation

The system provides two evaluation chains — "retrieval evaluation" and "RAGAS generation quality evaluation" — that complement rather than replace each other.

| Dimension | Retrieval Evaluation `/evaluation/run` | RAGAS Evaluation `/evaluation/ragas/run` |
| --- | --- | --- |
| **Evaluates** | Retrieval stage | Retrieval + generation end-to-end |
| **Core Metrics** | Recall@K / Hit Rate / MRR / Hallucination rate | Faithfulness / Answer Relevancy / Context Precision / Context Recall |
| **Scoring Method** | Keyword and source matching (rule-based) | LLM automatic scoring |
| **Test Set Fields** | `query` + `expected_sources` + `expected_answer_keywords` | `question` + `ground_truth` |
| **Requires LLM** | No (retrieval only) | Yes (generate answer + score) |
| **Typical Duration** | Seconds | Minutes |
| **Dependencies** | None | `ragas` library + `LLM_API_KEY` |
| **Failure Behavior** | Direct error | 503 or zero-metric fallback report |

!!! tip "Use them together"

    - **Retrieval evaluation**: quickly validate the effect of tuning `VECTOR_TOP_K` / `BM25_TOP_K` / `RERANK_TOP_K`; results in seconds
    - **RAGAS evaluation**: end-to-end quality verification before launch or release; quantifies generation quality
    - Recommended workflow: iterate quickly with retrieval evaluation during tuning → run RAGAS evaluation for end-to-end acceptance once tuning stabilizes

---

## Continuous Monitoring Recommendations

RAGAS evaluation is time-consuming and not suitable for triggering on every request; use it as a periodic quality monitor.

### Run Periodically

| Frequency | When | Test Set | Purpose |
| --- | --- | --- | --- |
| Daily | Off-peak early morning | Built-in 12 cases | Monitor daily quality fluctuation |
| Each release | Before launch | Built-in + business-critical external set | Release quality gate |
| Major KB change | After ingest/delete/rollback | Built-in 12 cases | Verify the change introduced no regression |

```bash
# Cron example: run RAGAS evaluation at 02:00 every day
0 2 * * * curl -X POST http://localhost:8000/api/v1/evaluation/ragas/run \
  -H "X-API-Key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Metric Threshold Alerts

Combine with `/api/v1/observability/alerts` to set threshold alerts for RAGAS metrics:

| Metric | Suggested Threshold | Alert Level | Action |
| --- | --- | --- | --- |
| `faithfulness` | < 0.80 | error | Investigate hallucination sources immediately; check prompt and LLM model |
| `answer_relevancy` | < 0.75 | warn | Check whether retrieval returned noisy context |
| `context_precision` | < 0.70 | warn | Reduce `top_k` or raise the rerank threshold |
| `context_recall` | < 0.70 | error | Increase `top_k` or check the embedding model and chunk size |

!!! warning "Calibrate thresholds to your business"

    The table above lists generic suggested values. After first deployment, run 3–5 evaluations to establish a baseline, then set alert thresholds relative to it (baseline −10% as warn, baseline −20% as error) to avoid false positives.

### Trend Analysis

Pull historical reports via `GET /api/v1/evaluation/ragas/reports` and plot the four metrics over time to spot long-term quality drift:

- **Slow decline** → stale KB content or distribution drift; supplement with new documents
- **Cliff drop** → usually corresponds to a change (model upgrade, major KB change); compare reports before and after the change
- **Periodic fluctuation** → retrieval quality affected by peak concurrency; cross-analyze with `/api/v1/performance/metrics`

---

## Next Steps

- [Knowledge Base Management Tutorial](knowledge.en.md): run RAGAS evaluation after ingestion to verify end-to-end quality
- [Performance Optimization Tutorial](performance.en.md): use retrieval evaluation to quickly validate tuning effects
- [API Reference](../api-reference.en.md): full request/response field reference for the three RAGAS endpoints
