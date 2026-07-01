# 流式响应体验优化 Spec

## Why
当前同步 `/api/v1/chat` 的 P95=7.94s 未达 ≤ 5s 目标，根因为 knowledge_qa 真实 LLM 生成固需 7-8s。后端 `/api/v1/chat/stream` SSE 端点与前端 SSE 消费虽已实现，但存在三个阻碍体验达标的真实问题：(1) 首 Token 时间未验证且可能 > 1s（意图识别走 LLM 时阻塞 meta 事件）；(2) 非知识问答意图走"非流式收集完整结果后单 token 输出"，首 Token = 同步生成时间，未真正流式；(3) 前端缺断线重连与 markdown 流式渲染。优化后用户感知等待从 7-8s 降至 < 1s 首 Token，彻底解决 P95 痛点。

## What Changes
- 流式链路首 Token 时间优化：意图识别快通道（_try_quick_intent）在流式链路中复用，闲聊/转人工跳过 LLM 意图识别直达首 Token
- 非知识问答意图流式化：chitchat 等高频意图首 Token 立即返回（规则拼装结果流式吐出），而非等完整 LLM 生成
- 新增首 Token 时间指标采集：在 chat_stream 端点记录 meta 事件与首个 token 事件的耗时，纳入监控
- 前端 SSE 断线重连：流式中途网络异常时自动重连一次（仅未完成时），保留已渲染内容
- 前端 markdown 流式渲染：长答案（代码块/列表/表格）流式渲染不闪烁，使用轻量 markdown 增量解析
- 移动端流式体验适配：打字光标、meta 徽章、气泡在小屏可读

## Impact
- Affected specs: `fix-bge-and-streaming`（首 Token < 1s 检查项在此 spec 中验证达标）、`verify-with-real-llm`（响应时间复测）
- Affected code:
  - `app/api/v1/chat.py`：`_run_stream_pipeline` 复用 `_try_quick_intent` 快通道；非知识问答意图改为流式吐出；新增首 Token 耗时埋点
  - `app/agents/orchestrator.py`：暴露 `_try_quick_intent` 供流式链路调用（已存在，需确认可复用）
  - `app/static/app.js`：`consumeSSEStream` 新增断线重连；`renderStreamToken` 接入 markdown 增量渲染
  - `app/static/style.css`：移动端 media query、流式渲染闪烁修复
  - `app/core/monitor.py`：新增 `record_stream_first_token` 埋点（可选，复用现有 record_step）
  - `tests/test_chat_stream.py`：新增首 Token 时间、快通道流式、断线重连测试

## ADDED Requirements

### Requirement: 流式首 Token 时间 < 1s
系统 SHALL 在 `/api/v1/chat/stream` 端点保证首 Token（meta 或 token 事件）响应时间 < 1s（真实 LLM 环境，含意图识别与检索）。闲聊/转人工等高频意图通过关键词快通道跳过 LLM 意图识别，首 Token < 200ms。

#### Scenario: 闲聊首 Token 极速返回
- **WHEN** 用户发送"你好"到 `/api/v1/chat/stream`
- **THEN** 首 Token（meta 事件）在 200ms 内到达，无需等待 LLM 意图识别

#### Scenario: 知识问答首 Token 达标
- **WHEN** 用户发送知识问答类查询到 `/api/v1/chat/stream`
- **THEN** meta 事件（含 intent）在 1s 内到达，token 事件紧随其后流式返回

#### Scenario: 非知识问答意图流式吐出
- **WHEN** 用户发送业务查询/闲聊等非知识问答意图
- **THEN** 系统流式吐出回复内容（按句末标点切片），而非等完整生成后单 token 输出

### Requirement: 首 Token 耗时监控
系统 SHALL 采集 `/api/v1/chat/stream` 的首 Token 耗时（从请求开始到首个 meta/token 事件 yield），并暴露在性能指标接口供运维监控。

#### Scenario: 首 Token 耗时可查询
- **WHEN** 运维查询 `/api/v1/performance/metrics`
- **THEN** 返回包含 `stream_first_token_ms_avg` 与 `stream_first_token_ms_p95` 指标

### Requirement: 前端 SSE 断线重连
前端 SHALL 在流式响应中途网络异常时自动重连一次（仅在流未完成时），重连失败则保留已渲染内容并提示连接中断。

#### Scenario: 中途断线自动重连
- **WHEN** 流式接收过程中网络中断且流未完成
- **THEN** 前端自动重连一次相同请求，重连成功则继续接收剩余内容

#### Scenario: 重连失败保留内容
- **WHEN** 重连仍失败
- **THEN** 保留已渲染的 token 内容，追加"连接中断"提示，不清空气泡

### Requirement: Markdown 流式渲染
前端 SHALL 在流式接收 token 时增量渲染 markdown（代码块、列表、表格），避免每次 token 到达全量重渲染导致的闪烁。

#### Scenario: 代码块流式渲染
- **WHEN** 流式响应包含多行代码块
- **THEN** 代码块边接收边渲染，不出现整体闪烁或重排

## MODIFIED Requirements

### Requirement: 非知识问答意图流式生成
原 `_stream_non_knowledge` 同步收集完整结果后作为单 token 输出。现改为：规则拼装结果按句末标点切片流式吐出，LLM 生成结果走真实流式（若 LLM 支持）。

## Scope
本 spec 仅优化流式体验，不改动同步 `/api/v1/chat` 行为，不改动 RAG 检索与重排序逻辑，不引入新依赖。
