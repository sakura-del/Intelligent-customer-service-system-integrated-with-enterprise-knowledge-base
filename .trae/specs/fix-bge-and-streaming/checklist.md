# Checklist

## BGE 模型加载
- [ ] `EMBEDDING_LOCAL_CACHE_DIR` / `HF_MIRROR_URL` / `EMBEDDING_LOAD_TIMEOUT` 配置项已添加
- [ ] BGE 加载逻辑实现四级回退：主源 → 镜像源 → 本地缓存 → hash fallback
- [ ] 加载成功时向量维度为 1024，与 fallback 一致
- [ ] `get_embedding_diagnostics()` 返回加载模式、尝试源列表、失败原因
- [ ] `tests/test_embeddings_resilience.py` ≥8 用例全部通过
- [ ] BGE 加载状态有明确诊断日志

## LLM 流式接口
- [ ] `LLMClient.stream_chat` 生成器方法实现
- [ ] mock 模式按字符切片流式
- [ ] 真实模式调用 OpenAI SDK stream=True
- [ ] 错误处理 yield error 事件
- [ ] `tests/test_llm_stream.py` ≥6 用例全部通过

## RAG 流式生成
- [ ] `RAGAgent.answer_stream` 生成器实现
- [ ] `KnowledgeAgent.handle_stream` 编排检索+reranker+流式生成
- [ ] 检索未命中时不走 LLM 流式，直接返回兜底回复
- [ ] `tests/test_rag_stream.py` ≥6 用例全部通过

## 流式 chat 端点
- [ ] `POST /api/v1/chat/stream` SSE 端点实现
- [ ] SSE 事件格式正确：meta / token / done / error
- [ ] 端点复用 SessionManager 与 OrchestratorAgent
- [ ] 原 `POST /api/v1/chat` 非流式行为不变
- [ ] `tests/test_chat_stream.py` ≥8 用例全部通过

## 前端 SSE 消费
- [ ] `app/static/app.js` 新增 `sendStreamMessage` 函数
- [ ] UI 边接收边渲染 Token
- [ ] 流式失败自动降级非流式

## 验证与回归
- [ ] BGE 加载状态明确（bge 或 fallback 有诊断）
- [ ] `/api/v1/chat/stream` 首 Token < 1s
- [ ] `/api/v1/evaluation/run` Recall@5 对比修复前有提升
- [ ] 5 条真实查询平均响应时间对比修复前有改善
- [ ] `pytest tests/ -q` 全量测试通过（579 + 新增）
- [ ] 原 `build-customer-service-system/checklist.md` 中「知识库覆盖率 ≥ 90%，检索准确率 ≥ 85%」与「平均响应时间 ≤ 3 秒」状态已根据复测结果更新
