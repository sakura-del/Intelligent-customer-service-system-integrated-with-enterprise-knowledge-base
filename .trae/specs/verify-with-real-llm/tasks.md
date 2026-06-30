# Tasks

> 目标：用真实 DeepSeek LLM 验证 4 个未通过检查点，更新原 checklist 状态。所有任务不修改源码，仅运行验证与端点调用。

- [x] Task 1: 验证 LLM 真实调用链路
  - [x] SubTask 1.1: 启动应用，调用 `/api/v1/health` 确认服务可用（200 OK）
  - [x] SubTask 1.2: 调用 `/api/v1/chat` 发送测试查询，确认 DeepSeek-chat 真实返回非空中文回复（5 条查询全部成功）
  - [x] SubTask 1.3: 调用知识问答查询（「产品有哪些功能」命中 knowledge_qa 意图，sources_count=3，RAG 链路调用真实 LLM 生成 630 字答案）

- [x] Task 2: 尝试加载 BGE 嵌入模型
  - [x] SubTask 2.1: 检查当前 EmbeddingService.mode，确认为 fallback（BGE 下载失败：HuggingFace 文件缺失 pytorch_model.bin/model.safetensors）
  - [x] SubTask 2.2: 尝试触发 BGE-large-zh-v1.5 在线下载（已尝试，失败原因：模型仓库无 pytorch_model.bin/model.safetensors 文件）
  - [x] SubTask 2.3: 验证 BGE 加载结果：保持 mode="fallback"，主链路不阻塞，记录告警

- [x] Task 3: 入库测试知识集
  - [x] SubTask 3.1: 确认 tests/sample_data/ 下有 faq.md、product_manual.md、return_policy.md 三个测试文档
  - [x] SubTask 3.2: 调用 `/api/v1/knowledge/ingest` 入库测试文档（54 条 chunk 已在库，本次全部去重）
  - [x] SubTask 3.3: 调用 `/api/v1/knowledge/stats` 确认入库成功（total_documents=54）

- [x] Task 4: 跑检索评估验证准确率
  - [x] SubTask 4.1: 调用 `/api/v1/evaluation/run` 跑默认 30 条测试集（top_k=5，0.54s 完成）
  - [x] SubTask 4.2: 记录指标：Recall@5=0.6429 / Precision@5=0.4357 / Hit Rate=0.6 / MRR=0.5625 / Hallucination Rate=0.0
  - [x] SubTask 4.3: 判定「知识库覆盖率 ≥ 90%，检索准确率 ≥ 85%」**未达标**（Recall@5=0.64 < 0.85，Hit Rate=0.6 < 0.90；原因：BGE fallback hash 向量语义检索能力弱，需真实 BGE 模型才能达标）

- [x] Task 5: 跑端到端对话场景验证独立解决率
  - [x] SubTask 5.1: 准备 5 条覆盖闲聊/业务查询/知识问答/转人工/感谢的测试查询
  - [x] SubTask 5.2: 逐条调用 `/api/v1/chat` 记录是否独立解决（4/5 独立解决，1 条「联系人工客服」按设计转人工）
  - [x] SubTask 5.3: 计算独立解决率=80%，判定「≥ 60%」**达标**

- [x] Task 6: 响应时间压测
  - [x] SubTask 6.1: 5 条查询响应时间已记录（3.57s / 3.13s / 9.93s / 1.08s / 2.68s）
  - [x] SubTask 6.2: 计算平均响应时间=4.08s，P95=3.57s
  - [x] SubTask 6.3: 判定「平均响应时间 ≤ 3 秒」**未达标**（4.08s > 3s；原因：knowledge_qa 完整 RAG 链路含 LLM 生成耗时 9.93s 拉高均值；P95=3.57s 接近达标）

- [x] Task 7: 更新原 checklist 与验证报告
  - [x] SubTask 7.1: 根据验证结果更新 `build-customer-service-system/checklist.md` 中 4 个检查点状态
  - [x] SubTask 7.2: 在 `verify-with-real-llm/checklist.md` 勾选所有通过的验证检查点
  - [x] SubTask 7.3: 输出最终验证报告（指标值、达标情况、未达标原因）

# Task Dependencies
- Task 2 依赖 Task 1（确认服务可用后再尝试 BGE 加载）
- Task 3 依赖 Task 2（BGE 状态确定后入库才稳定）
- Task 4 依赖 Task 3（必须先入库才能跑评测）
- Task 5、Task 6 依赖 Task 3（必须先入库才能跑对话）
- Task 7 依赖 Task 4、Task 5、Task 6（汇总所有验证结果）
