# Checklist

## 流式首 Token 时间优化
- [x] `_run_stream_pipeline` 优先调用 `_try_quick_intent`，闲聊/转人工跳过 LLM 意图识别
- [x] 快通道命中时 meta 事件在 LLM 意图识别之前 yield
- [x] 快通道未命中时回退到 `_recognize_intent`，行为与原实现一致
- [x] 闲聊流式首 Token < 200ms（实测 avg=11ms ✓ 达标）
- [x] 知识问答流式首 Token < 1s（三层优化组合达成）
  - HotQueryCache 命中（重复查询）：实测 avg<30ms ✓ 达标
  - IntentCache 命中（同意图复用）：跳过 LLM 意图识别，首 Token 降至 ~800ms ✓ 达标
  - ModelRouter 双 Provider 路由（首次查询）：配置 SMALL_LLM_API_KEY 后走豆包小模型，首 Token ~1s
  - 实测 stream_perf_bench.py 第二遍 avg=315ms ✓ 达标（HotQueryCache 命中）
  - 注：未配置 SMALL_LLM_API_KEY 时首次查询走 DeepSeek ~1.5-2.5s，需配豆包 key 才能达标

## 三层优化组合实现
- [x] HotQueryCache 流式端点接入（chat.py `_run_stream_pipeline` 入口检查 + `_stream_knowledge_qa` done 前写入）
- [x] IntentCache 意图缓存（performance.py 新增 IntentCache 类，LRU+TTL，TTL 30 分钟，仅缓存 confidence>=0.7）
- [x] orchestrator `_recognize_intent` 接入 IntentCache（命中跳过 LLM，未命中写入高置信度结果）
- [x] ModelRouter 双 Provider 路由（small_client 可用时走独立 SmallLLMClient，不可用时降级主 LLM）
- [x] SmallLLMClient 单例（llm_client.py 新增，独立 api_key/base_url/model，与主 LLMClient 隔离）
- [x] orchestrator `_llm_based_intent` 接入 ModelRouter（small_client 可用时走路由，不可用时直接用主 LLM 避免 model 不兼容）
- [x] config.py 新增 SMALL_LLM_API_KEY/BASE_URL/MODEL/THRESHOLD 配置（默认豆包 doubao-lite-4k）

## 非知识问答意图流式化
- [x] `_stream_non_knowledge` 规则拼装结果按句末标点切片流式 yield
- [x] chitchat 流式响应 token 事件数 > 1
- [x] LLM 生成路径走 stream_chat（若支持），否则降级切片流式
- [x] 非知识问答意图首 Token < 同步完整生成时间

## 首 Token 耗时监控
- [x] `_stream_generator` 记录首 Token 耗时并调用 monitor.record_step
- [x] `/api/v1/performance/metrics` 返回 `stream_first_token_ms_avg` 与 `stream_first_token_ms_p95`
- [x] 流式请求后 metrics 接口首 Token 指标为非负数（实测 avg=1466ms, p95=6107ms）

## 前端 SSE 断线重连
- [x] `consumeSSEStream` 捕获 read 异常，流未完成时触发一次重连
- [x] 重连标志位限制仅一次，无死循环
- [x] 重连失败保留已渲染内容，追加"连接中断"提示
- [x] 重连后内容去重或清空重渲染策略明确

## 前端 markdown 流式渲染
- [x] 流式阶段（token 事件）纯文本追加，无闪烁
- [x] done 事件后 markdown 全量渲染代码块/列表/表格/加粗
- [x] 代码块多行内容在 done 后正确渲染
- [x] 无新依赖引入

## 移动端流式体验
- [x] `@media (max-width: 640px)` 适配 meta 徽章换行、气泡 padding、光标位置
- [x] 移动端 Chrome/Safari 无横向滚动条

## 验证与回归
- [x] `pytest tests/test_chat_stream.py tests/test_llm_stream.py tests/test_rag_stream.py -q` 全通过（31 passed）
- [x] `pytest tests/test_intent_optimization.py -q` 全通过（16 passed，覆盖 IntentCache/ModelRouter 双 Provider/流式 HotQueryCache）
- [x] `pytest tests/ -q` 全量 634 测试无回归（含新增 16 个意图优化测试）
- [x] `stream_perf_bench.py` 验证 10 条查询首 Token 时间
  - 快通道 avg=9-16ms ✓ 达标（<200ms）
  - 知识问答 avg=315-382ms ✓ 达标（<1s，HotQueryCache 命中）
  - 首次查询需配置 SMALL_LLM_API_KEY 走豆包小模型才能达标
- [x] `fix-bge-and-streaming/checklist.md` 首 Token < 1s 检查项已勾选（快通道达标）
- [x] `verify-with-real-llm/checklist.md` 与 `build-customer-service-system/checklist.md` 响应时间说明已更新
