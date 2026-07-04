# Tasks

- [x] Task 1: 添加 ragas 依赖并创建数据模型
  - [x] SubTask 1.1: 在 `requirements.txt` 中添加 `ragas>=0.2.0` 依赖
  - [x] SubTask 1.2: 创建 `app/schemas/ragas_evaluation.py`，定义 `RagasTestCase`、`RagasTestSet`、`RagasCaseDetail`、`RagasEvaluationReport`、`RagasRunRequest`、`RagasReportSummary` 数据模型
  - [x] SubTask 1.3: 在 `app/knowledge/ragas_evaluator.py` 中定义内置 RAGAS 测试集（至少 10 条，含 question + ground_truth）

- [x] Task 2: 实现 RAGAS 评估器核心逻辑
  - [x] SubTask 2.1: 创建 `app/knowledge/ragas_evaluator.py`，实现 `RagasEvaluator` 类
  - [x] SubTask 2.2: 实现 `run()` 方法：加载测试集 → 对每条用例执行检索（复用 KnowledgeRetriever）→ 生成答案（复用 RAGAgent）→ 调用 RAGAS 计算四项指标 → 聚合报告
  - [x] SubTask 2.3: 实现降级策略：ragas 未安装或 LLM 不可用时返回明确错误
  - [x] SubTask 2.4: 实现报告持久化：保存到 `{CHROMA_PERSIST_DIR}/ragas_reports/`，复用现有报告目录管理模式
  - [x] SubTask 2.5: 实现 `list_reports()` 与 `get_report()` 方法

- [x] Task 3: 扩展 API 端点
  - [x] SubTask 3.1: 在 `app/api/v1/evaluation.py` 中新增 `POST /ragas/run` 端点
  - [x] SubTask 3.2: 新增 `GET /ragas/reports` 端点
  - [x] SubTask 3.3: 新增 `GET /ragas/reports/{report_id}` 端点

- [x] Task 4: 编写单元测试
  - [x] SubTask 4.1: 创建 `tests/test_ragas_evaluation.py`，测试数据模型、测试集加载、指标计算、报告持久化、降级策略
  - [x] SubTask 4.2: 运行全部测试确保无回归

- [x] Task 5: 更新文档
  - [x] SubTask 5.1: 在 `docs/tutorials/` 下中英文文档中新增 RAGAS 评估章节（或新建 `docs/tutorials/evaluation-ragas.zh.md` / `.en.md`）
  - [x] SubTask 5.2: 更新 `docs/api-reference.zh.md` / `.en.md` 添加 RAGAS 端点说明
  - [x] SubTask 5.3: 更新 `mkdocs.yml` 注册新文档页面
  - [x] SubTask 5.4: 更新 `README.md` 评估能力描述

# Task Dependencies

- Task 2 depends on Task 1（评估器依赖数据模型与测试集）
- Task 3 depends on Task 2（API 端点依赖评估器实现）
- Task 4 depends on Task 3（测试依赖完整链路）
- Task 5 可与 Task 4 并行（文档不依赖测试通过）
