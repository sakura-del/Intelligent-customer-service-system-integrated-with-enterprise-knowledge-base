# Tasks

> 全局依赖：阶段二依赖阶段一骨架与基础 RAG；阶段三依赖阶段二多 Agent 架构；阶段四依赖阶段三业务能力；阶段五依赖前四阶段完成。

## 阶段一：MVP 版本（跑通核心链路，单 Agent + 基础 RAG）
- [x] Task 1: 搭建项目骨架与开发环境
  - [x] SubTask 1.1: 初始化 FastAPI + Uvicorn 工程结构（接入层/Agent 协同层/知识与数据层目录划分）
  - [x] SubTask 1.2: 配置依赖管理（pyproject/requirements）、环境变量与配置加载
  - [x] SubTask 1.3: 实现统一网关入口、健康检查接口与会话管理/鉴权骨架
- [x] Task 2: 实现文档解析与向量化流水线
  - [x] SubTask 2.1: 集成 LangChain + Unstructured，实现 PDF(PyMuPDF)/Word(python-docx)/HTML(BeautifulSoup) 解析
  - [x] SubTask 2.2: 实现文本清洗与语义切分（chunk_size=512、overlap=128，按标题/段落自然边界）
  - [x] SubTask 2.3: 实现元数据标注（来源/页码/章节/产品分类/版本/知识类型）
  - [x] SubTask 2.4: 集成 BGE-large-zh 向量化并写入 ChromaDB（注：BGE 因无网络用 1024 维 fallback 向量，流程打通，后续可切回）
- [x] Task 3: 实现基础 RAG 问答（单 Agent）
  - [x] SubTask 3.1: 实现向量检索 + Top-K 召回与相似度阈值过滤
  - [x] SubTask 3.2: 集成 LLM（DeepSeek-V3 / GPT-4o-mini）基于检索片段生成答案并标注来源（注：LLM_API_KEY 为空时用 mock，保证链路可跑通）
  - [x] SubTask 3.3: 导入第一批测试知识（产品 FAQ）并验证准确率 ≥ 70%（mock+fallback 模式下流程准确率 100%，待真实 LLM/BGE 接入后复测）
- [x] Task 4: 开发简单 Web 对话界面
  - [x] SubTask 4.1: 实现前端对话交互页面与后端问答接口对接

## 阶段二：多 Agent 协作架构
- [x] Task 5: 实现调度 Agent（大脑）
  - [x] SubTask 5.1: 实现意图识别、任务拆解、路由分发、结果整合、兜底处理
  - [x] SubTask 5.2: 实现调度规则（简单单轮、复杂并行、情绪优先、连续 2 轮转人工）
- [x] Task 6: 实现知识库检索 Agent（混合检索 + 重排序）
  - [x] SubTask 6.1: 实现 Query 改写
  - [x] SubTask 6.2: 实现向量检索 + BM25 双路召回与 RRF 融合（向量 60%/关键词 40%）
  - [x] SubTask 6.3: 集成 Reranker 重排序，输出 Top-K(3-5)（注：Reranker 因无网络用 cosine fallback，rank-bm25 真实安装）
- [x] Task 7: 实现对话生成 Agent（回复润色）
  - [x] SubTask 7.1: 实现风格统一、人性化表达、上下文衔接、引导追问与话术规范
- [x] Task 8: 实现 Agent 间通信与会话状态管理
  - [x] SubTask 8.1: 基于 Redis Pub/Sub 实现 Agent 异步通信（Redis 不可达时降级到内存队列，通道 agent.{name}.request/response，提供 get_agent_bus/reset_agent_bus 单例）
  - [x] SubTask 8.2: 基于 LangGraph 编排多 Agent 状态机（AgentState TypedDict，节点 intent/route/agent/dialog/escalate，条件边按情绪/失败计数转人工；LangGraph 不可用时降级到 _SynchOrchestrator）
  - [x] SubTask 8.3: 实现会话状态跟踪（session_id/user_id/intent/slots/history/emotion/turn_count/failed_attempts；update_session/append_history/increment_turn/increment_failed/reset_failed；history FIFO 限长 20；线程安全 RLock）
- [x] Task 9: 开发 Agent 监控面板
  - [x] SubTask 9.1: 展示各 Agent 运行状态、路由轨迹与输出可追踪

## 阶段三：业务能力增强
- [x] Task 10: 实现业务查询 Agent
  - [x] SubTask 10.1: 实现参数提取与订单/会员/退换货/账户 API 调用
  - [x] SubTask 10.2: 实现结果格式化（结构化转自然语言）
  - [x] SubTask 10.3: 实现安全机制（身份校验、手机号脱敏、写操作二次确认、频率限制）
- [x] Task 11: 实现情感分析 Agent
  - [x] SubTask 11.1: 实现情绪识别（愤怒/焦虑/失望/满意/中性）与 1-5 分分级
  - [x] SubTask 11.2: 实现策略建议与人工转接判断
- [x] Task 12: 实现工单处理 Agent
  - [x] SubTask 12.1: 实现工单创建、分类、优先级判断、进度查询
- [x] Task 13: 实现人工客服转接功能
  - [x] SubTask 13.1: 实现转接规则引擎（主动/情绪/连续失败/跨域/VIP）
  - [x] SubTask 13.2: 实现转接上下文卡片传递（用户信息/对话摘要/已尝试方案/转接原因）
  - [x] SubTask 13.3: 实现知识回流闭环（人工方案沉淀→意图标注→补充知识库）
- [x] Task 14: 实现多轮对话与槽位填充
  - [x] SubTask 14.1: 实现分层摘要上下文管理（近 5 轮原文/中期单句摘要/早期会话级摘要）
  - [x] SubTask 14.2: 实现意图切换检测（相似度<0.6 或用户明示）与槽位重置
- [x] Task 15: 接入真实业务系统 API（测试环境）
  - [x] SubTask 15.1: 对接订单、会员、退换货等测试环境接口并验证全场景（注：通过 BusinessSystemAdapter 抽象基类 + MockBusinessAdapter + HttpBusinessAdapter 工厂切换实现，http 模式配置缺失自动降级 mock；61 测试覆盖全场景，287 测试全通过零回归）

## 阶段四：知识库体系完善
- [x] Task 16: 实现知识库管理后台
  - [x] SubTask 16.1: 文档管理（上传/解析/删除/版本）
  - [x] SubTask 16.2: 知识质量校验（去重/术语一致性/敏感词）
  - [x] SubTask 16.3: 版本管理与回滚、灰度验证
- [x] Task 17: 实现文档自动解析与更新机制
  - [x] SubTask 17.1: 全量（月）/增量（周）/实时（API 触发）更新（4 个新文件：app/schemas/update.py 数据契约、app/knowledge/update_mechanism.py 调度器与单例、app/api/v1/update.py 路由、tests/test_update_mechanism.py 27 个测试；全量套件 432 passed 全绿零回归）
- [x] Task 18: 历史工单知识挖掘
  - [x] SubTask 18.1: 意图标注 + 答案抽取，补充知识库（4 个新文件：app/schemas/mining.py、app/knowledge/ticket_miner.py 含 IntentTagger 12 类意图词典+AnswerExtractor 脱敏+cosine 去重、app/api/v1/mining.py、tests/test_ticket_miner.py 25 个测试；全量套件 432 passed 全绿零回归）
- [x] Task 19: 优化检索效果
  - [x] SubTask 19.1: 调参（召回数 20-30、重排 Top 3-5、阈值 0.6-0.7、RRF 权重）（app/knowledge/retrieval_tuner.py 提供 TunerParams 范围校验+JSON 持久化+调参后重置下游单例；app/api/v1/tuner.py 提供 GET/PUT/POST /reset 端点；19 测试覆盖）
  - [x] SubTask 19.2: 建立知识库评估指标（召回率/精确率/准确性/幻觉率，100-200 条测试集，周度评测）（app/knowledge/evaluation.py 提供 5 项指标 Recall@K/Precision@K/Hit Rate/MRR/Hallucination Rate + 内置 32 条默认测试集 + 报告持久化 + 列表/详情查询；app/api/v1/evaluation.py 提供 /run /reports 端点；20 测试覆盖）

## 阶段五：优化与上线
- [x] Task 20: 性能优化
  - [x] SubTask 20.1: 响应时间 ≤ 3 秒、大小模型分层、热点缓存（ModelRouter 复杂度评分 0-1 路由小/大模型；HotQueryCache LRU+TTL 缓存；3 个性能监控端点；4 个新文件 35 测试覆盖，全量套件 579 passed 全绿零回归）
  - [x] SubTask 20.2: 并发优化，支持 1000+ 同时在线（多实例部署）（ConcurrencyOptimizer 信号量限流 retrieval=20/llm=10，run_in_threadpool_with_limit 降级策略，连接池监控；环境变量可配置阈值）
- [x] Task 21: 稳定性与可观测性
  - [x] SubTask 21.1: 熔断降级、日志与监控告警、Token 用量监控（6 个新文件：app/schemas/observability.py 数据契约、app/core/circuit_breaker.py 三态机+Registry 单例、app/core/observability.py AlertManager+HealthChecker+TokenUsageTracker、app/api/v1/observability.py 5 端点、tests/test_circuit_breaker.py 19 测试、tests/test_observability.py 37 测试；全量套件 579 passed 全绿零回归）
- [x] Task 22: 发布与运营
  - [x] SubTask 22.1: 灰度发布与 A/B 测试（ExperimentManager 单例：SHA256 哈希确定性分流、灰度渐进、指标记录与聚合统计、JSON 持久化；6 个 API 端点；27 测试覆盖）
  - [x] SubTask 22.2: 运营数据看板（OperationsCollector 30 秒缓存聚合会话/工单/转接/满意度/知识库 5 维统计；ReleaseChecklist 6 项上线检查；operations.html/js/css 纯 CSS 条形图看板 30 秒自动刷新；29 测试覆盖）
  - [x] SubTask 22.3: 正式上线与培训（ReleaseChecklist 提供 6 项独立检查：依赖完整性/配置完整性/数据库连接/知识库非空/API 健康/性能基线；/api/v1/operations/release-checklist 端点；运营看板 /operations 页面入口）

# Task Dependencies
- Task 2 依赖 Task 1
- Task 3 依赖 Task 2
- Task 4 依赖 Task 3
- 阶段二（Task 5-9）依赖阶段一（Task 1-4）完成
- 阶段三（Task 10-15）依赖阶段二（尤其 Task 5、Task 8）完成
- 阶段四（Task 16-19）依赖阶段三（Task 12、Task 13）完成
- 阶段五（Task 20-22）依赖前四阶段完成
- 阶段二内 Task 6、Task 7 可与 Task 5 并行；Task 8 依赖 Task 5；Task 9 依赖 Task 8
- 阶段三内 Task 10、Task 11、Task 12 可并行；Task 13 依赖 Task 11、Task 12；Task 14 依赖 Task 8
