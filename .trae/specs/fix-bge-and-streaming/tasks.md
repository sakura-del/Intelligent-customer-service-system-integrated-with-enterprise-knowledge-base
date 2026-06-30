# Tasks

> 目标：修复 BGE 模型加载 + 实现流式响应端点，使检索准确率与响应时间双达标。所有修改保持向后兼容，不破坏现有 579 个测试。

- [ ] Task 1: 修复 BGE 模型加载健壮性
  - [ ] SubTask 1.1: 在 `app/core/config.py` 新增配置 `EMBEDDING_LOCAL_CACHE_DIR`（默认 `./models/bge-large-zh`）、`HF_MIRROR_URL`（默认 `https://hf-mirror.com`）、`EMBEDDING_LOAD_TIMEOUT`（默认 60 秒）
  - [ ] SubTask 1.2: 重构 `app/knowledge/embeddings.py` 的 BGE 加载逻辑：主源 → 镜像源 → 本地缓存 → hash fallback 四级回退；每次尝试独立捕获异常并记录诊断日志（源 URL、失败原因、耗时）
  - [ ] SubTask 1.3: 加载成功后验证向量维度为 1024，与 fallback 维度一致避免向量库 schema 冲突
  - [ ] SubTask 1.4: 提供 `get_embedding_diagnostics() -> dict` 工具函数，返回当前加载模式、尝试过的源列表、失败原因，便于排查
  - [ ] SubTask 1.5: 编写 `tests/test_embeddings_resilience.py`（≥8 用例）：主源成功 / 主源失败镜像成功 / 全部失败降级 / 本地缓存命中 / 维度校验 / 诊断函数 / 并发加载 / 配置切换

- [ ] Task 2: LLM 客户端新增流式接口
  - [ ] SubTask 2.1: 在 `app/agents/llm_client.py` 新增 `stream_chat(messages, **kwargs)` 生成器方法，yield `{"type":"token","content":"..."}` 或 `{"type":"error","message":"..."}`
  - [ ] SubTask 2.2: mock 模式下模拟流式（按字符切片 yield，间隔 10ms）
  - [ ] SubTask 2.3: 真实模式调用 OpenAI SDK 的 `stream=True`，透传 chunk
  - [ ] SubTask 2.4: 编写 `tests/test_llm_stream.py`（≥6 用例）：真实流式 / mock 流式 / 错误处理 / 空响应 / 大响应 / 中断

- [ ] Task 3: RAG Agent 流式生成
  - [ ] SubTask 3.1: 在 `app/agents/rag_agent.py` 新增 `answer_stream(query, context_chunks)` 生成器，先 yield 检索结果元信息，再流式 yield LLM Token
  - [ ] SubTask 3.2: 在 `app/agents/knowledge_agent.py` 新增 `handle_stream(query)` 生成器，编排检索 → reranker → 流式生成
  - [ ] SubTask 3.3: 编写 `tests/test_rag_stream.py`（≥6 用例）：检索命中流式 / 检索未命中 / LLM 异常 / mock 模式 / 来源标注 / 上下文衔接

- [ ] Task 4: 流式 chat 端点
  - [ ] SubTask 4.1: 在 `app/api/v1/chat.py` 新增 `POST /api/v1/chat/stream` 端点，返回 `StreamingResponse`，media_type `text/event-stream`
  - [ ] SubTask 4.2: SSE 事件格式：`event: meta\ndata: {intent, sources}\n\n` → `event: token\ndata: {content}\n\n`（多次）→ `event: done\ndata: {turn_count, escalate}\n\n`
  - [ ] SubTask 4.3: 端点内部复用 SessionManager 与 OrchestratorAgent 的意图识别/路由，仅最终生成阶段走流式
  - [ ] SubTask 4.4: 错误处理：任一阶段异常 yield `event: error` 后关闭流
  - [ ] SubTask 4.5: 编写 `tests/test_chat_stream.py`（≥8 用例）：知识问答流式 / 闲聊流式 / 业务查询流式 / 转人工流式 / 错误事件 / 会话保持 / 鉴权 / 非 SSE 客户端兼容

- [ ] Task 5: 前端 SSE 消费
  - [ ] SubTask 5.1: 在 `app/static/app.js` 新增 `sendStreamMessage` 函数，使用 `fetch` + `ReadableStream` 消费 SSE（不引入 EventSource，因需 POST）
  - [ ] SubTask 5.2: UI 边接收边渲染 Token，添加打字光标效果
  - [ ] SubTask 5.3: 保留原非流式路径作为降级（流式失败自动重试非流式）

- [ ] Task 6: 验证与回归
  - [ ] SubTask 6.1: 启动服务，确认 BGE 加载状态（mode 为 bge 或 fallback 有明确诊断）
  - [ ] SubTask 6.2: 调用 `/api/v1/chat/stream` 验证流式响应首 Token < 1s
  - [ ] SubTask 6.3: 跑 `/api/v1/evaluation/run` 复测 Recall@5，对比修复前
  - [ ] SubTask 6.4: 跑 5 条真实查询测响应时间，对比修复前
  - [ ] SubTask 6.5: 运行 `pytest tests/ -q` 确保全量测试通过（579 + 新增）

# Task Dependencies
- Task 2 依赖 Task 1（先确保 LLM 客户端可用）
- Task 3 依赖 Task 2（RAG 流式依赖 LLM 流式接口）
- Task 4 依赖 Task 3（chat 端点依赖 RAG 流式）
- Task 5 依赖 Task 4（前端依赖后端端点）
- Task 6 依赖 Task 1、Task 4、Task 5（全部实现后验证）
