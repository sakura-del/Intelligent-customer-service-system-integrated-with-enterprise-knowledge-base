# BGE 修复与流式响应优化 Spec

## Why
真实 LLM 验证暴露两个阻碍上线的核心问题：(1) BGE-large-zh-v1.5 模型下载失败导致走 hash fallback，检索 Recall@5 仅 0.64 远低于 0.85 目标；(2) knowledge_qa 完整 RAG 链路 LLM 生成耗时 9.93s 拉高平均响应时间到 4.08s 超过 3s 目标。修复这两项后可使检索准确率与响应时间双达标，推动项目正式上线。

## What Changes
- 修复 BGE-large-zh-v1.5 模型加载：支持手动下载权重文件 + 多源镜像回退 + 明确错误诊断
- 实现 `/api/v1/chat/stream` SSE 流式响应端点：Token 边生成边返回，首 Token 响应时间 < 1s
- 优化 RAG 链路：流式透传 LLM 输出，避免完整生成后再返回
- 扩展 chat 端点支持 `stream=true` 参数，保持非流式端点向后兼容

## Impact
- Affected specs: `verify-with-real-llm`（修复后需复测 Recall@5 与响应时间）
- Affected code:
  - `app/knowledge/embeddings.py`：增强 BGE 加载逻辑，支持本地缓存与镜像源
  - `app/api/v1/chat.py`：新增流式响应端点（保留原端点）
  - `app/agents/llm_client.py`：新增 `stream_chat` 方法
  - `app/agents/rag_agent.py` / `knowledge_agent.py`：新增流式生成接口
  - `app/agents/orchestrator.py` / `graph.py`：编排流式生成路径
  - 新增前端 SSE 消费逻辑（`app/static/app.js`）

## ADDED Requirements

### Requirement: BGE 模型加载健壮性
系统 SHALL 在 HuggingFace 主源加载失败时，按以下顺序回退：(1) 本地缓存目录 `./models/bge-large-zh` (2) HuggingFace 镜像源 `hf-mirror.com` (3) hash fallback。加载失败时输出明确诊断日志（缺失文件名、尝试过的源、网络状态）。

#### Scenario: 主源加载失败时回退镜像源
- **WHEN** HuggingFace 主源无 `pytorch_model.bin` 或 `model.safetensors`
- **THEN** 自动尝试 `hf-mirror.com` 镜像下载，成功则 mode="bge"

#### Scenario: 镜像源也失败时使用本地缓存
- **WHEN** 用户已手动下载 BGE 权重到 `./models/bge-large-zh/`
- **THEN** 系统从本地缓存加载，mode="bge"，日志提示「使用本地缓存」

#### Scenario: 全部失败时降级 fallback
- **WHEN** 主源、镜像源、本地缓存均不可用
- **THEN** mode="fallback"，日志输出明确诊断信息（含失败源列表与建议）

### Requirement: 流式响应端点
系统 SHALL 提供 `POST /api/v1/chat/stream` 端点，以 Server-Sent Events 格式流式返回 LLM 生成内容，首 Token 响应时间 < 1s。

#### Scenario: 流式响应正常
- **WHEN** 客户端 POST `/api/v1/chat/stream` 且 `Accept: text/event-stream`
- **THEN** 返回 SSE 流，事件类型含 `meta`（intent/sources）、`token`（生成内容）、`done`（完成）

#### Scenario: 流式响应降级
- **WHEN** LLM 不支持流式或调用失败
- **THEN** 返回 `error` 事件并关闭流，HTTP 状态 200（SSE 协议约定）

### Requirement: 向后兼容
原 `POST /api/v1/chat` 端点 SHALL 保持非流式行为不变，已有客户端无需修改。

#### Scenario: 非流式请求不变
- **WHEN** 客户端 POST `/api/v1/chat`
- **THEN** 返回完整 ChatResponse JSON，行为与流式优化前完全一致

## MODIFIED Requirements

### Requirement: EmbeddingService 初始化
原 `EmbeddingService` 仅尝试 HuggingFace 主源后降级。现增加镜像源与本地缓存回退链路，加载逻辑更健壮。
