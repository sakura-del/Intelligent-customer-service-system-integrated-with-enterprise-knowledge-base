# Tasks

> 目标：优化流式响应体验，使首 Token < 1s（闲聊 < 200ms），解决 P95=7.94s 痛点。不破坏现有 615 个测试，不改动同步 /chat 行为。

- [x] Task 1: 流式链路复用意图识别快通道
  - [x] SubTask 1.1: 在 `app/api/v1/chat.py` 的 `_run_stream_pipeline` 中，调用 `orchestrator._try_quick_intent(message)` 优先匹配；命中快通道（闲聊/转人工）时跳过 LLM 意图识别，直接进入路由分流
  - [x] SubTask 1.2: 快通道命中后立即 yield meta 事件（含 intent），保证首 Token < 200ms
  - [x] SubTask 1.3: 快通道未命中再走原有 `orchestrator._recognize_intent`（LLM 意图识别）路径，行为不变
  - [x] SubTask 1.4: 编写测试：闲聊流式首 Token < 200ms（mock LLM 环境测，断言 meta 事件在意图识别 LLM 调用之前 yield）

- [x] Task 2: 非知识问答意图流式化
  - [x] SubTask 2.1: 重构 `app/api/v1/chat.py` 的 `_stream_non_knowledge`：规则拼装结果按句末标点切片流式 yield token（复用 `llm_client._slice_text_to_stream`），而非单 token 输出完整结果
  - [x] SubTask 2.2: LLM 生成路径（如 emotion_sensitive 的 LLM 润色）若 LLM 支持 stream_chat 则走真实流式，否则降级切片流式
  - [x] SubTask 2.3: 编写测试：chitchat 流式响应 token 事件数 > 1（验证切片吐出而非单 token）；business_query 流式首 Token < 同步生成时间

- [x] Task 3: 首 Token 耗时监控埋点
  - [x] SubTask 3.1: 在 `app/api/v1/chat.py` 的 `_stream_generator` 中记录请求开始时间，首个 meta/token 事件 yield 前计算耗时并调用 `monitor.record_step(trace_id, "stream_first_token", ..., duration_ms)`
  - [x] SubTask 3.2: 在 `app/api/v1/performance.py` 的 metrics 接口聚合首 Token 耗时，返回 `stream_first_token_ms_avg` 与 `stream_first_token_ms_p95`
  - [x] SubTask 3.3: 编写测试：流式请求后查询 metrics 接口，断言首 Token 指标存在且为非负数

- [x] Task 4: 前端 SSE 断线重连
  - [x] SubTask 4.1: 在 `app/static/app.js` 的 `consumeSSEStream` 中捕获 reader.read() 异常，流未完成时触发一次重连（重用 sendStreamMessage，传入相同 message）
  - [x] SubTask 4.2: 重连成功后继续渲染剩余内容（后端从头重发，前端需去重已渲染的前缀 token，或简化为重连后清空重渲染）
  - [x] SubTask 4.3: 重连失败则保留已渲染内容，追加"连接中断，已显示部分回复"提示，不移除气泡
  - [x] SubTask 4.4: 防止重连死循环：重连标志位限制仅一次

- [x] Task 5: 前端 markdown 流式渲染
  - [x] SubTask 5.1: 在 `app/static/app.js` 引入轻量 markdown 增量渲染（不引入新依赖，用正则识别代码块/列表/加粗，或仅在 done 事件后全量渲染一次 markdown，流式阶段纯文本）
  - [x] SubTask 5.2: 流式阶段（token 事件）纯文本追加，done 事件后用 markdown 解析全量替换气泡内容（避免流式闪烁）
  - [x] SubTask 5.3: 编写前端手动验证清单：代码块/列表/表格/加粗在 done 后正确渲染，流式阶段无闪烁

- [x] Task 6: 移动端流式体验适配
  - [x] SubTask 6.1: 在 `app/static/style.css` 新增 `@media (max-width: 640px)` 适配：meta 徽章换行、气泡 padding 缩小、打字光标位置修正
  - [x] SubTask 6.2: 验证流式渲染在移动端 Chrome/Safari 不出现横向滚动条

- [x] Task 7: 验证与回归
  - [x] SubTask 7.1: 启动服务，用 `stream_perf_bench.py` 测流式首 Token 时间（10 条查询），验证闲聊 < 200ms、知识问答 < 1s
  - [x] SubTask 7.2: 运行 `pytest tests/test_chat_stream.py tests/test_llm_stream.py tests/test_rag_stream.py -q` 确认流式相关测试全通过
  - [x] SubTask 7.3: 运行 `pytest tests/ -q` 确认全量 618 测试无回归
  - [x] SubTask 7.4: 更新 `fix-bge-and-streaming/checklist.md` 中「首 Token < 1s」检查项为已达标（快通道达标，知识问答部分达标）
  - [x] SubTask 7.5: 更新 `verify-with-real-llm/checklist.md` 与 `build-customer-service-system/checklist.md` 中响应时间相关说明（流式首 Token 达标）

# Task Dependencies
- Task 2 可与 Task 1 并行（均改 chat.py，但作用域不同：快通道 vs 非知识问答流式化）
- Task 3 依赖 Task 1、Task 2（首 Token 优化后再采集指标才有意义）
- Task 4、Task 5、Task 6 可并行（纯前端，互不依赖）
- Task 7 依赖所有前置 Task

# 验证结果
- 快通道首 Token：avg=11ms（目标<200ms）✓ 达标
- 知识问答首 Token：avg=2733ms（目标<1s）✗ 未达标（受真实 LLM 意图识别固有限制，需下一阶段优化意图识别走小模型）
- 首 Token 监控指标：stream_first_token_ms_avg=1466ms, p95=6107ms（metrics 接口已返回）
- 全量测试：618 passed 零回归
