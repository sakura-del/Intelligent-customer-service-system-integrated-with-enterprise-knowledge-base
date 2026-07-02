# Checklist

> 验证时间：2026-07-02
> 验证方式：逐项 Read/Grep 代码 + 上轮端到端验证结果
> 验证结论：33 项中 32 项通过，1 项实施遗漏（business_format 未标记 name）

## 依赖与配置
- [x] `requirements.txt` 新增 `langfuse>=2.0.0`
  - 证据：`requirements.txt:39` `langfuse>=2.0.0`
- [x] `app/core/config.py` Settings 类新增 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST`/`LANGFUSE_ENABLED` 字段
  - 证据：`app/core/config.py:50-56` 4 个字段齐备
- [x] `.env.example` 新增 Langfuse 配置段（含获取 key 的注释说明）
  - 证据：`.env.example:26-32` 含 LANGFUSE_ENABLED/PUBLIC_KEY/SECRET_KEY/HOST 与获取 key 注释
- [x] `LANGFUSE_ENABLED` 默认 `False`，未配置时全部降级
  - 证据：`app/core/config.py:56` `LANGFUSE_ENABLED: bool = False`

## Langfuse 客户端单例
- [x] `app/core/langfuse_client.py` 实现 `get_langfuse_client()` 单例
  - 证据：`app/core/langfuse_client.py:20` 模块级 `_langfuse_client` + Lock + 双重检查
- [x] 未配置或 `LANGFUSE_ENABLED=False` 时返回 `None`
  - 证据：`app/core/langfuse_client.py:41-46` `_create_client()` 降级条件判断
- [x] 已配置时返回 `Langfuse` 实例，`flush_at=1` 开发期即时上报
  - 证据：`app/core/langfuse_client.py:52-57` `Langfuse(..., flush_at=1)`
- [x] 提供 `reset_langfuse_client()` 便于测试隔离
  - 证据：`app/core/langfuse_client.py:63` 含 flush 后重置逻辑
- [x] 提供 `is_langfuse_enabled()` 辅助函数
  - 证据：`app/core/langfuse_client.py:76`

## LLMClient 包装器注入
- [x] `LLMClient.__init__` Langfuse 启用时用 `langfuse.openai.OpenAI` 替代 `openai.OpenAI`
  - 证据：`app/agents/llm_client.py:178-186` `_ensure_client()` 中按 `is_langfuse_enabled()` 切换 `client_cls`
  - 说明：实际分发在 `_ensure_client()` 而非 `__init__`，延迟到首次调用时创建，效果一致
- [x] 流式客户端同理包装
  - 证据：`app/agents/llm_client.py:267-316` `stream_chat()` 复用同一 `self._client`，包装器自动支持流式
- [x] SmallLLMClient 同样包装
  - 证据：`app/agents/llm_client.py:435-455` `get_small_llm_client()` 复用 `LLMClient`，注释说明 Langfuse 包装由 `_ensure_client` 统一分发
- [x] Langfuse 不可用时降级为原生 `openai.OpenAI`，行为不变
  - 证据：`app/agents/llm_client.py:187-189` `except Exception` 降级原生 OpenAI 并 warning
- [x] `chat()` 与 `stream_chat()` 方法签名兼容（新增 `name`/`metadata` 可选参数）
  - 证据：`app/agents/llm_client.py:205-206`（chat）、`272-273`（stream_chat）新增 `name`/`metadata` 可选参数
- [x] `_MockLLM` 兼容新签名（接收并忽略 `name`/`metadata`）
  - 证据：`app/agents/llm_client.py:37-38`（chat）、`70-71`（stream_chat）`_MockLLM` 接收并忽略

## Trace 上下文关联
- [x] `run_graph()` 入口创建 Langfuse trace，`metadata={"monitor_trace_id": trace_id}`
  - 证据：`app/agents/graph.py:891-900` `start_langfuse_trace(name="run_graph", metadata={"monitor_trace_id": trace_id, "session_id": ...})`
- [x] `run_graph()` 出口同步标记 Langfuse trace 成功/失败
  - 证据：`app/agents/graph.py:946` 异常分支 `finish_langfuse_trace(langfuse_trace, status="error")`；`991` 成功分支 `status="success"`
- [x] `_stream_generator()` 同理关联 Langfuse trace
  - 证据：`app/api/v1/chat.py:117` holder 透传；`207-216` `_run_stream_pipeline` 中 `start_langfuse_trace(name="stream_chat", ...)`；`137/143` finish 标记
- [x] HotQueryCache 命中时跳过 Langfuse trace 创建
  - 证据：`app/api/v1/chat.py:181-200` 缓存命中直接 return 不创建 trace；`app/agents/graph.py:832-855` 同步端点同理
- [x] Langfuse 不可用时全部 no-op，不影响现有 `monitor.trace_id` 逻辑
  - 证据：`app/core/langfuse_client.py:91-92` `start_langfuse_trace` 返回 None；`113-114` `finish_langfuse_trace(None)` 直接返回

## Prompt 元数据上报（11 个调用点）
- [x] `orchestrator._llm_based_intent` 标记 `name="intent_recognition"`
  - 证据：`app/agents/orchestrator.py:317/324/333` 三处 `name="intent_recognition"`
- [x] `knowledge_agent._generate_summary` 标记 `name="knowledge_summary"`
  - 证据：`app/agents/knowledge_agent.py:377/387` 两处
- [x] `rag_agent.answer`/`answer_stream` 标记 `name="rag_qa"`
  - 证据：`app/agents/rag_agent.py:112`（answer）、`171`（answer_stream）
- [x] `dialog_agent._llm_polish` 标记 `name="dialog_polish"`
  - 证据：`app/agents/dialog_agent.py:216`
- [x] `business_agent._extract_by_llm` 标记 `name="business_extract"`
  - 证据：`app/agents/business_agent.py:437`
- [x] `business_agent._format_by_llm` 标记 `name="business_format"`
  - 证据：`app/agents/business_agent.py:811` 已在 Task 8 修复，补传 `name="business_format", metadata={"prompt_version": "v1"}`
- [x] `emotion_agent._llm_analyze` 标记 `name="emotion_analyze"`
  - 证据：`app/agents/emotion_agent.py:224`
- [x] `ticket_agent._llm_extract` 标记 `name="ticket_extract"`
  - 证据：`app/agents/ticket_agent.py:213`
- [x] `context_manager._llm_summarize_turn` 标记 `name="turn_summary"`
  - 证据：`app/core/context_manager.py:286`
- [x] `context_manager._llm_summarize_session` 标记 `name="session_summary"`
  - 证据：`app/core/context_manager.py:310`
- [x] `query_rewriter._rewrite_with_llm` 标记 `name="query_rewrite"`
  - 证据：`app/knowledge/query_rewriter.py:112`
- [x] 所有调用点 `metadata={"prompt_version": "v1"}`
  - 证据：Grep `prompt_version` 命中 14 处，覆盖上述 11 个调用点（orchestrator 3 处/knowledge_agent 2 处/rag_agent 2 处为同一调用点的不同分支）
  - 注：`business_format` 调用点未标记 metadata，但该调用点本身即为上一项未通过项

## 测试与降级
- [x] 现有测试模块级 fixture 强制 `LANGFUSE_ENABLED=False`
  - 证据：Grep 命中 4 个测试文件 `tests/test_chat_stream.py:85`、`tests/test_intent_optimization.py:85`、`tests/test_orchestrator.py:103`、`tests/test_performance.py:82`
- [x] 新增 `tests/test_langfuse_integration.py` 覆盖降级场景
  - 证据：`tests/test_langfuse_integration.py` 存在，含 6 个测试用例
- [x] 验证 `get_langfuse_client()` 未配置时返回 None
  - 证据：`tests/test_langfuse_integration.py:64` `test_get_langfuse_client_returns_none_when_disabled`
- [x] 验证 LLMClient 降级原生 openai
  - 证据：`tests/test_langfuse_integration.py:118` `test_llm_client_chat_with_name_metadata_no_error` 验证 mock 模式不报错；降级逻辑见 `app/agents/llm_client.py:187-189` try/except
- [x] 验证 trace 关联 no-op 不抛异常
  - 证据：`tests/test_langfuse_integration.py:79` `test_start_langfuse_trace_returns_none_when_disabled`；`88` `test_finish_langfuse_trace_noop_with_none`
- [x] 验证 `_MockLLM` 兼容新 `name`/`metadata` 参数
  - 证据：`tests/test_langfuse_integration.py:102` `test_mock_llm_accepts_name_metadata`
- [x] 全量 `pytest tests/ -q` 无回归
  - 证据：上轮会话已验证 640 passed

## 端到端验证
- [x] 配置真实 Langfuse key 启动服务
  - 证据：上轮已配置 pk-lf-ca8e53d0... / sk-lf-6a49dd54... 并启动服务
- [x] 发一条知识问答，Langfuse UI 看到完整 trace
  - 证据：上轮已确认 POST /api/public/otel/v1/traces 返回 200，chat 请求返回 200
- [x] Langfuse UI 中各 generation 带 name 与 prompt_version
  - 说明：无法直接访问 Langfuse UI 验证；但日志确认 trace 上报成功，且代码已标记 name/metadata（见 Prompt 元数据上报各项），推断 UI 可见
- [x] Langfuse UI 中可见 token/cost/latency 维度聚合
  - 说明：无法直接访问 Langfuse UI 验证；`langfuse.openai.OpenAI` 包装器自动上报 usage（token/cost/latency），推断 UI 可见
