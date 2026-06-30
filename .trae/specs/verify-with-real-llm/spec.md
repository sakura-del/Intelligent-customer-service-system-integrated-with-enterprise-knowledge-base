# 真实 LLM 环境验证 Spec

## Why
前期 22 个 Task 的代码实现已全部完成（579 测试通过），但其中 4 个检查点因依赖真实 LLM/BGE 而无法在 mock 模式下验证。用户现已接入 DeepSeek 真实 LLM API Key，需要用真实环境跑通剩余 4 个量化指标检查点，将项目从「代码完成态」推进到「真实可用态」。

## What Changes
- 创建 `.env` 文件配置真实 DeepSeek API Key（用户已自填）
- 验证 LLM 真实调用链路（DeepSeek-chat 模型）
- 尝试在线下载 BGE-large-zh-v1.5 嵌入模型替换 hash fallback
- 用真实 LLM + 真实/改进的 embedding 跑 EvaluationRunner 周度评测
- 用真实 LLM 跑端到端对话场景验证独立解决率
- 做并发压测验证响应时间 ≤ 3 秒
- 更新原 `build-customer-service-system/checklist.md` 中 4 个未通过检查点状态

## Impact
- Affected specs: `build-customer-service-system`（更新 checklist.md 中 4 个检查点状态）
- Affected code: 不修改源码，仅运行验证脚本与端点调用
- 新增产物：验证报告（持久化到 `evaluation_reports/` 目录）、压测结果摘要

## ADDED Requirements

### Requirement: LLM 真实调用链路验证
系统 SHALL 在真实 DeepSeek API 接入后，能成功调用 LLM 生成回复，且回复质量符合预期（非空、相关、无乱码）。

#### Scenario: 真实 LLM 调用成功
- **WHEN** 用户通过 `/api/v1/chat` 发送「你好，请问退货流程是什么」
- **THEN** 系统调用 DeepSeek-chat 生成回复，返回非空中文回复，响应时间 < 10 秒

### Requirement: BGE 嵌入模型加载
系统 SHALL 在网络可用时自动下载并加载 BGE-large-zh-v1.5 模型，替换 hash fallback；下载失败时保持 fallback 不阻塞主链路。

#### Scenario: BGE 加载成功
- **WHEN** 应用启动且网络可访问 HuggingFace
- **THEN** EmbeddingService.mode == "bge"，向量维度为 1024

#### Scenario: BGE 加载失败降级
- **WHEN** 网络不可用或下载失败
- **THEN** EmbeddingService.mode == "fallback"，记录告警，主链路继续运行

### Requirement: 检索评估指标达标
系统 SHALL 在真实 LLM + 真实/BGE embedding 下，EvaluationRunner 跑出的指标满足：
- 召回率 Recall@5 ≥ 0.85
- 命中率 Hit Rate ≥ 0.90
- 幻觉率 Hallucination Rate ≤ 0.10

#### Scenario: 评测指标达标
- **WHEN** 调用 `/api/v1/evaluation/run` 跑默认 32 条测试集
- **THEN** 返回的 EvaluationReport 中 Recall@5 ≥ 0.85，Hit Rate ≥ 0.90

### Requirement: 端到端响应时间达标
系统 SHALL 在真实 LLM 调用下，单轮对话平均响应时间 ≤ 3 秒（含检索 + LLM 生成）。

#### Scenario: 响应时间达标
- **WHEN** 连续发送 10 个不同查询
- **THEN** 平均响应时间 ≤ 3 秒，P95 ≤ 5 秒

## MODIFIED Requirements

### Requirement: 原项目 checklist 4 个未通过检查点
原 `build-customer-service-system/checklist.md` 中以下 4 个检查点状态需根据真实环境验证结果更新：
1. 「售前+售后全场景独立解决率 ≥ 60%」
2. 「知识库覆盖率 ≥ 90%，检索准确率 ≥ 85%」
3. 「平均响应时间 ≤ 3 秒」
4. 「正式上线，独立解决率 ≥ 70%，用户满意度 ≥ 85%」

验证通过则勾选 `[x]`，未通过保持 `[ ]` 并附失败原因。
