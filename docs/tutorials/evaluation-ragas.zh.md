---
title: RAGAS 生成质量评估使用教程
description:
  RAGAS 端到端 RAG 质量评估教程，覆盖 Faithfulness、Answer Relevancy、Context Precision、Context Recall
  四项核心指标说明、API 端点调用、内置测试集、降级策略与持续监控建议。
---

# RAGAS 生成质量评估使用教程

检索评估（`/api/v1/evaluation/run`）只衡量「检索是否命中」，无法回答「最终回复是否忠实、是否切题」。RAGAS（Retrieval-Augmented Generation Assessment）补齐了这一缺口：通过 LLM 对「问题 → 检索上下文 → 生成答案 → 标准答案」全链路打分，量化 RAG 端到端质量。本教程介绍四项核心指标、三个 API 端点的使用方法、内置测试集、降级策略与持续监控建议。

!!! info "前置条件"

    - 端点前缀统一为 `/api/v1/evaluation/ragas`，鉴权头 `X-API-Key`
    - 需安装 `ragas` 库（`pip install ragas`）并配置 `LLM_API_KEY`，否则 `POST /ragas/run` 返回 503
    - 评测复用现有 `RAGAgent`（检索 + 生成）与 `LLMClient`（评分），无需额外 LLM 服务
    - 报告持久化到 `{CHROMA_PERSIST_DIR}/ragas_reports/` 目录

---

## RAGAS 四项核心指标

RAGAS 通过 LLM 自动评分，无需人工标注期望来源，仅需 `question` 与 `ground_truth`（标准答案）即可端到端评估。

| 指标 | 含义 | 评估对象 | 理想值 |
| --- | --- | --- | --- |
| **Faithfulness**（忠实度） | 答案是否忠实于检索上下文，未编造信息 | 生成质量 | 越高越好（0-1） |
| **Answer Relevancy**（答案相关性） | 答案是否切题回答了用户问题 | 生成质量 | 越高越好（0-1） |
| **Context Precision**（上下文精确度） | 检索到的上下文中相关内容的占比 | 检索质量 | 越高越好（0-1） |
| **Context Recall**（上下文召回率） | 标准答案所需信息是否都被检索到的上下文覆盖 | 检索质量 | 越高越好（0-1） |

!!! tip "四项指标的协作诊断"

    - **Faithfulness 低** → 模型有幻觉倾向，应检查 Prompt 与 LLM 模型选择
    - **Answer Relevancy 低** → 答案跑题，可能是检索上下文干扰或指令遵循不足
    - **Context Precision 低** → 检索召回过多无关片段，应调小 `top_k` 或提升 rerank 阈值
    - **Context Recall 低** → 检索召回不足，应调大 `top_k` 或检查嵌入模型与切片粒度

---

## API 端点概览

| 端点 | 方法 | 说明 | 鉴权 |
| --- | --- | --- | :---: |
| `/api/v1/evaluation/ragas/run` | POST | 触发 RAGAS 评测 | ✅ |
| `/api/v1/evaluation/ragas/reports` | GET | 列出历史 RAGAS 报告摘要 | ✅ |
| `/api/v1/evaluation/ragas/reports/{report_id}` | GET | 查询单个 RAGAS 报告详情 | ✅ |

---

## 触发评测：POST /api/v1/evaluation/ragas/run

对每条用例执行「检索上下文 → 生成答案 → LLM 评分」全链路，聚合四项指标并持久化报告。

### 请求体

```json
{
  "testset_path": null,
  "top_k": null
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | :---: | --- | --- |
| `testset_path` | string | ❌ | 内置 12 条 | 外部测试集 JSON 路径，为空使用内置默认集 |
| `top_k` | int | ❌ | `RAGAgent` 默认值 | 检索 Top-K，取值范围 1–50 |

### 示例

=== "curl"

    ```bash
    # 使用内置测试集跑 RAGAS 评测
    curl -X POST http://localhost:8000/api/v1/evaluation/ragas/run \
      -H "Content-Type: application/json" \
      -H "X-API-Key: ${API_KEY}" \
      -d '{}'
    ```

    ```bash
    # 指定外部测试集与 top_k
    curl -X POST http://localhost:8000/api/v1/evaluation/ragas/run \
      -H "Content-Type: application/json" \
      -H "X-API-Key: ${API_KEY}" \
      -d '{"testset_path": "tests/sample_data/ragas_testset.json", "top_k": 5}'
    ```

=== "Python (httpx)"

    ```python
    import httpx

    # RAGAS 评测涉及多次 LLM 调用，超时需放宽
    resp = httpx.post(
        "http://localhost:8000/api/v1/evaluation/ragas/run",
        headers={"X-API-Key": ""},
        json={"top_k": 5},
        timeout=600.0,  # 12 条用例 × 检索+生成+评分，整体耗时较长
    )
    report = resp.json()
    print(f"report_id: {report['report_id']}")
    print(f"faithfulness:        {report['faithfulness']:.3f}")
    print(f"answer_relevancy:    {report['answer_relevancy']:.3f}")
    print(f"context_precision:   {report['context_precision']:.3f}")
    print(f"context_recall:      {report['context_recall']:.3f}")
    ```

### 响应体（RagasEvaluationReport）

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
      "question": "忘记登录密码怎么办？",
      "ground_truth": "如果忘记登录密码，可以在登录页面点击「忘记密码」……",
      "answer": "您可以在登录页点击「忘记密码」，输入注册邮箱后……",
      "contexts": [
        "用户忘记密码时，可通过登录页「忘记密码」入口重置……",
        "重置链接 30 分钟内有效……"
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

!!! warning "评测耗时较长"

    RAGAS 评测对每条用例都要执行「检索 → 生成 → LLM 评分」三步，且 RAGAS 内部对四项指标分别调用 LLM。12 条内置用例 + 4 项指标 ≈ 数十次 LLM 调用，整体耗时通常在 2–5 分钟。建议在非高峰期执行，或通过外部测试集缩小用例规模。

!!! note "case_details 字段价值"

    响应体的 `case_details` 列出每条用例的完整链路数据（question/answer/contexts）与四项指标得分，便于排查单条用例质量。例如某条 `faithfulness` 显著低于均值，可针对性检查该问题对应的检索上下文与生成答案。

---

## 查询历史报告

### GET /api/v1/evaluation/ragas/reports — 列出历史报告摘要

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

按 `created_at` 倒序返回，最新报告在前。

### GET /api/v1/evaluation/ragas/reports/{report_id} — 查询单个报告详情

```bash
curl http://localhost:8000/api/v1/evaluation/ragas/reports/20260704_103045_a1b2c3d4 \
  -H "X-API-Key: ${API_KEY}"
```

返回完整的 `RagasEvaluationReport`（含 `case_details`）。`report_id` 不存在时返回 `404`。

---

## 内置测试集

系统内置 12 条 RAGAS 测试用例，覆盖客服核心场景，无需准备外部数据即可跑通端到端评估。

| 场景分类 | 用例数 | 覆盖问题示例 |
| --- | :---: | --- |
| 账号与登录 | 2 | 忘记密码、修改绑定手机号 |
| 订单与支付 | 2 | 支付失败资金扣除、修改收货地址 |
| 退换货政策 | 4 | 七天无理由退货范围、退货运费、换货期限、退款到账时长 |
| 会员与积分 | 2 | 会员等级升级、积分有效期 |
| 产品手册与系统功能 | 1 | 知识库支持的文档格式 |
| 客服热线 | 1 | 客服热线电话与接听时间 |

每条用例仅包含 `question` 与 `ground_truth`（标准答案），无需人工标注期望来源：

```json
{
  "question": "忘记登录密码怎么办？",
  "ground_truth": "如果忘记登录密码，可以在登录页面点击「忘记密码」，输入注册邮箱后系统会发送重置链接到邮箱，点击链接即可重置密码。重置链接 30 分钟内有效。"
}
```

### 外部测试集格式

外部测试集为 JSON 文件，结构与内置测试集一致：

```json
{
  "cases": [
    {
      "question": "退货运费由谁承担？",
      "ground_truth": "质量问题导致的退货运费由商家承担；非质量问题由买家承担。"
    },
    {
      "question": "会员等级如何升级？",
      "ground_truth": "会员等级按近 90 天的有效消费金额累计计算：银卡满 500 元、金卡满 2000 元、钻石卡满 8000 元。"
    }
  ],
  "meta": {
    "version": "custom-v1",
    "description": "自定义 RAGAS 测试集"
  }
}
```

!!! tip "外部测试集加载失败时的降级"

    外部测试集文件不存在或格式非法时，系统自动降级到内置默认集并记录告警日志，评测不会中断。

---

## 降级策略

RAGAS 评估依赖 `ragas` 库与 LLM 服务，缺失时自动降级以保证主链路可用。

| 降级场景 | 触发条件 | 行为 |
| --- | --- | --- |
| RAGAS 不可用 | `ragas` 未安装 **或** `LLM_API_KEY` 为空 | `POST /ragas/run` 返回 `503 Service Unavailable` + 错误描述 |
| RAGAS 调用异常 | ragas evaluate 抛错（API 版本差异等） | 评测不中断，对应用例返回零指标，`error` 字段记录原因 |
| 外部测试集加载失败 | 文件不存在或 JSON 解析失败 | 自动降级到内置默认集 |
| 报告持久化失败 | 磁盘满或权限不足 | 仅记录告警日志，不影响返回结果 |

### 503 响应示例

```json
{
  "detail": "RAGAS 评估需要安装 ragas 库并配置 LLM_API_KEY"
}
```

!!! note "不影响现有检索评估"

    RAGAS 降级只影响 `/api/v1/evaluation/ragas/*` 三个端点，现有 `/api/v1/evaluation/run`（检索评估）与 `/api/v1/evaluation/reports` 不受影响，可继续使用。

---

## 与现有检索评估的区别

系统同时提供「检索评估」与「RAGAS 生成质量评估」两条评估链路，两者互补而非替代。

| 维度 | 检索评估 `/evaluation/run` | RAGAS 评估 `/evaluation/ragas/run` |
| --- | --- | --- |
| **评估对象** | 检索阶段 | 检索 + 生成端到端 |
| **核心指标** | Recall@K / Hit Rate / MRR / 幻觉率 | Faithfulness / Answer Relevancy / Context Precision / Context Recall |
| **评分方式** | 关键词与来源匹配（规则） | LLM 自动评分 |
| **测试集字段** | `query` + `expected_sources` + `expected_answer_keywords` | `question` + `ground_truth` |
| **是否需要 LLM** | 否（检索即可） | 是（生成答案 + 评分） |
| **典型耗时** | 秒级 | 分钟级 |
| **依赖** | 无外部依赖 | `ragas` 库 + `LLM_API_KEY` |
| **失败降级** | 直接报错 | 503 或零指标降级报告 |

!!! tip "两者配合使用"

    - **检索评估**：快速验证 `VECTOR_TOP_K` / `BM25_TOP_K` / `RERANK_TOP_K` 调参效果，秒级出结果
    - **RAGAS 评估**：上线前与版本发布前的端到端质量验证，量化生成质量
    - 建议工作流：调参时用检索评估快速迭代 → 调参稳定后用 RAGAS 评估做端到端验收

---

## 持续监控建议

RAGAS 评估耗时长，不适合每次请求都触发，建议作为定期质量监控手段。

### 定期运行

| 频率 | 触发时机 | 测试集 | 用途 |
| --- | --- | --- | --- |
| 每日 | 凌晨低峰期 | 内置 12 条 | 监控日常质量波动 |
| 每次发布 | 上线前 | 内置 + 业务核心外部集 | 发布质量门禁 |
| 知识库大变更 | 入库/删除/回滚后 | 内置 12 条 | 验证变更未引入回归 |

```bash
# 每日凌晨 2 点跑 RAGAS 评测的 cron 示例
0 2 * * * curl -X POST http://localhost:8000/api/v1/evaluation/ragas/run \
  -H "X-API-Key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### 指标阈值告警

结合 `/api/v1/observability/alerts`，可对 RAGAS 指标设置阈值告警：

| 指标 | 建议阈值 | 告警级别 | 触发动作 |
| --- | --- | --- | --- |
| `faithfulness` | < 0.80 | error | 立即排查幻觉来源，检查 Prompt 与 LLM 模型 |
| `answer_relevancy` | < 0.75 | warn | 检查检索结果是否引入干扰上下文 |
| `context_precision` | < 0.70 | warn | 调小 `top_k` 或提升 rerank 阈值 |
| `context_recall` | < 0.70 | error | 调大 `top_k` 或检查嵌入模型与切片粒度 |

!!! warning "阈值需结合业务校准"

    上表为通用建议值，首次部署后建议先跑 3–5 次评测取基线，再根据基线设置告警阈值（基线 -10% 作为 warn，基线 -20% 作为 error），避免误报。

### 趋势分析

通过 `GET /api/v1/evaluation/ragas/reports` 拉取历史报告，绘制四项指标随时间的趋势曲线，识别长期质量漂移：

- **缓慢下降** → 知识库内容陈旧或分布漂移，需补充新文档
- **断崖下跌** → 通常对应某次变更（模型升级、知识库大改），需对比变更前后报告定位
- **周期性波动** → 业务高峰期检索质量受并发影响，可结合 `/api/v1/performance/metrics` 交叉分析

---

## 下一步

- [知识库管理教程](knowledge.zh.md)：入库后跑 RAGAS 评估验证端到端质量
- [性能优化教程](performance.zh.md)：用检索评估快速验证调参效果
- [API 参考](../api-reference.zh.md)：RAGAS 三个端点的完整请求/响应字段说明
