# RAGAS 评估 Spec

## Why

当前系统已有基于人工标注的检索评估（Recall@K / Hit Rate / MRR / Hallucination Rate），但缺少对**生成质量**的自动化评估。RAGAS（RAG Assessment）框架可通过 LLM 自动评估答案的忠实度、相关性、上下文精确度与召回率，**无需人工标注**即可持续监控 RAG 端到端质量。

## What Changes

- 新增 `app/knowledge/ragas_evaluator.py`：RAGAS 评估器，编排"检索 → 生成 → LLM 评分"全链路
- 新增 `app/schemas/ragas_evaluation.py`：RAGAS 评估数据模型（请求体、报告、单条详情）
- 扩展 `app/api/v1/evaluation.py`：新增 `POST /api/v1/evaluation/ragas/run`、`GET /api/v1/evaluation/ragas/reports`、`GET /api/v1/evaluation/ragas/reports/{report_id}` 三个端点
- 新增 `requirements.txt`：添加 `ragas` 依赖
- 新增 `tests/test_ragas_evaluation.py`：单元测试
- 更新 `docs/tutorials/` 下中英文文档：新增 RAGAS 评估章节
- 更新 `README.md`：在评估能力中补充 RAGAS

## Impact

- Affected specs: 检索评测（evaluation）能力扩展，不修改现有检索评估逻辑
- Affected code:
  - `app/knowledge/evaluation.py`（不改，仅复用报告目录结构）
  - `app/api/v1/evaluation.py`（扩展路由）
  - `app/schemas/evaluation.py`（不改，新建独立 schema 文件）
  - `requirements.txt`（新增依赖）

## ADDED Requirements

### Requirement: RAGAS 生成质量评估

系统 SHALL 提供 RAGAS 评估能力，对 RAG 端到端流程（检索 + 生成）进行无需人工标注的质量评估，输出四个核心指标：Faithfulness（忠实度）、Answer Relevancy（答案相关性）、Context Precision（上下文精确度）、Context Recall（上下文召回率）。

#### Scenario: 正常评估流程

- **WHEN** 用户调用 `POST /api/v1/evaluation/ragas/run`，不传 `testset_path`
- **THEN** 系统使用内置 RAGAS 测试集（至少 10 条，包含 question + ground_truth 字段），对每条用例执行"检索 → 生成答案 → RAGAS LLM 评分"，返回包含四项指标的报告

#### Scenario: 指标计算

- **WHEN** 评测完成
- **THEN** 报告包含 `faithfulness`（0-1）、`answer_relevancy`（0-1）、`context_precision`（0-1）、`context_recall`（0-1）四项聚合指标，以及每条用例的单条得分

#### Scenario: 报告持久化

- **WHEN** 评测报告生成
- **THEN** 报告持久化到 `{CHROMA_PERSIST_DIR}/ragas_reports/` 目录，按时间戳命名，可通过 `GET /api/v1/evaluation/ragas/reports` 列出历史报告

#### Scenario: 降级策略

- **WHEN** `ragas` 库未安装或 `LLM_API_KEY` 为空
- **THEN** 评估端点返回 503 + 错误提示"RAGAS 评估需要安装 ragas 库并配置 LLM_API_KEY"，不影响现有检索评估功能

#### Scenario: 复用现有 LLM 客户端

- **WHEN** RAGAS 评估需要 LLM 评分
- **THEN** 复用 `LLMClient`（DeepSeek-V3 / GPT-4o-mini），通过 OpenAI 兼容接口供 RAGAS 内部调用，不引入额外 LLM 依赖

### Requirement: RAGAS 测试集格式

系统 SHALL 支持内置 RAGAS 测试集与外部 JSON 测试集，格式包含 `question` 与 `ground_truth`（标准答案）两个字段。

#### Scenario: 内置测试集

- **WHEN** 未提供外部测试集路径
- **THEN** 使用内置测试集（至少 10 条），覆盖 FAQ、订单、退货、会员等场景，每条包含 `question` 和 `ground_truth`

#### Scenario: 外部测试集加载

- **WHEN** 传入 `testset_path` 指向合法 JSON 文件
- **THEN** 加载外部测试集执行评测；加载失败时降级到内置集并记录 warning 日志

## MODIFIED Requirements

### Requirement: 评测 API 端点

在现有 `/api/v1/evaluation/*` 路由组下新增 RAGAS 子路由，与现有检索评估端点共存，互不干扰。

- `POST /api/v1/evaluation/ragas/run`：触发 RAGAS 评测
- `GET /api/v1/evaluation/ragas/reports`：列出 RAGAS 历史报告
- `GET /api/v1/evaluation/ragas/reports/{report_id}`：查询单个 RAGAS 报告

所有 RAGAS 端点复用 `verify_api_key` 鉴权，与现有评测端点保持一致。
