# 整合企业知识库的智能客服系统 Spec

## Why
传统客服存在响应慢、答案不一致、成本高三大痛点。本项目以"多 Agent 分工协作 + RAG 知识增强"双轮驱动，构建一个 AI 客服团队（调度 + 知识检索 + 业务查询 + 情感分析 + 工单 + 对话生成），目标实现独立解决率 ≥ 70%、平均响应 ≤ 3 秒、答案准确率 ≥ 92%、人工工作量降低 ≥ 45%、用户满意度 ≥ 88%。

## What Changes
- 从零搭建 FastAPI + Uvicorn 服务骨架与项目工程结构
- 构建文档解析 → 语义切分 → 向量化 → 写入向量库的知识库构建流水线
- 集成 ChromaDB/Milvus 向量库与 Elasticsearch BM25，实现向量 + 关键词混合检索 + RRF 融合 + Reranker 重排序
- 实现"1+5"多 Agent 架构（调度 Agent 为大脑，协调 5 个专业 Agent），基于 LangGraph 编排
- 实现多轮对话状态跟踪、分层摘要上下文管理、意图切换检测
- 实现业务查询 Agent 对接订单/会员/退换货等业务系统 API，含脱敏与二次确认
- 实现情感分析、人工转接机制与转接上下文卡片传递
- 实现工单处理 Agent 与知识回流闭环
- 构建知识库管理后台（文档管理、质量校验、版本管理、更新机制）
- 实现 Web 对话界面与 Agent 监控面板
- 性能优化（响应 ≤ 3 秒、并发 1000+）、监控告警、灰度发布与运营看板

## Impact
- Affected specs: 无（全新项目，从零构建）
- Affected code: 整个工程目录，按接入层 / Agent 协同层 / 知识与数据层 分层组织
- 技术栈：FastAPI + Uvicorn、LangGraph、DeepSeek-V3 / GPT-4o-mini、BGE-large-zh、Milvus/ChromaDB、Elasticsearch + BM25、Redis Pub/Sub、LangChain + Unstructured

## ADDED Requirements

### Requirement: 项目骨架与接入层
系统 SHALL 提供 FastAPI + Uvicorn 服务骨架，支持 Web/APP/微信/钉钉/API 多渠道统一接入，通过统一网关进行会话管理与鉴权。

#### Scenario: 服务启动
- **WHEN** 开发者执行启动命令
- **THEN** FastAPI 服务在配置端口运行，健康检查接口返回 200

#### Scenario: 多渠道接入
- **WHEN** 来自不同渠道的请求到达统一网关
- **THEN** 网关完成鉴权并建立会话，请求被路由到调度 Agent

### Requirement: 知识库构建流水线
系统 SHALL 提供文档解析（PDF/Word/HTML/OCR）、文本清洗、语义切分（chunk_size=512、overlap=128，按自然边界）、元数据标注、向量化（BGE-large-zh）、写入向量库、质量校验（cosine>0.95 去重、术语一致性、敏感词过滤）的完整流水线。

#### Scenario: 文档入库
- **WHEN** 管理员上传一篇产品文档
- **THEN** 系统解析、切分、向量化后写入向量库，并附带来源/页码/章节/产品分类/版本等元数据

#### Scenario: 质量校验
- **WHEN** 新入库 chunk 与已有 chunk 相似度 > 0.95
- **THEN** 系统判定为重复并跳过或提示

### Requirement: 混合检索与 RAG 问答
系统 SHALL 实现 Query 改写 → 向量检索 + BM25 关键词检索双路召回 → RRF 融合（向量 60%/关键词 40%）→ Reranker 重排序 → Top-K(3-5) → LLM 生成答案的流程，相似度阈值 0.6-0.7，低于阈值不强行回答并标注来源。

#### Scenario: 命中知识
- **WHEN** 用户提出知识库可覆盖的问题
- **THEN** 系统返回带来源标注的答案，准确率 ≥ 70%（MVP）→ ≥ 92%（终态）

#### Scenario: 未命中知识
- **WHEN** 检索相似度低于阈值
- **THEN** 系统不强行回答，触发兜底或转人工

### Requirement: 多 Agent 协同架构
系统 SHALL 采用"1+5"架构：1 个调度 Agent（意图识别、任务拆解、路由分发、结果整合、兜底处理）+ 5 个专业 Agent（知识库检索、业务查询、情感分析、工单处理、对话生成），通过 Redis Pub/Sub 异步通信，由 LangGraph 编排。

#### Scenario: 简单问题
- **WHEN** 用户提出单一领域问题
- **THEN** 调度 Agent 识别意图后单轮派发给对应 Agent 解决

#### Scenario: 复杂问题
- **WHEN** 用户提出跨域复杂问题
- **THEN** 调度 Agent 拆解为子任务并行派发给多个 Agent，汇总后生成回复

#### Scenario: 兜底转人工
- **WHEN** 连续 2 轮未解决或无法识别意图
- **THEN** 调度 Agent 自动转人工

### Requirement: 业务查询 Agent
系统 SHALL 从对话提取参数调用业务系统 API（订单查询、退换货、会员信息、账户查询），实现用户身份校验、敏感信息脱敏（手机号中间 4 位打码）、写操作二次确认、调用频率限制。

#### Scenario: 订单查询
- **WHEN** 已登录用户提供订单号查询
- **THEN** 系统返回订单状态/物流/金额的结构化转自然语言回复

#### Scenario: 写操作确认
- **WHEN** 用户发起退换货等写操作
- **THEN** 系统二次确认用户意图后执行

### Requirement: 情感分析与人工转接
系统 SHALL 识别用户情绪（愤怒/焦虑/失望/满意/中性）并分级（1-5 分），按策略应对；按转接规则（用户主动、愤怒>4 分、连续失败、跨 3 域、VIP）触发转接，并传递上下文卡片（用户信息、对话摘要、已尝试方案、转接原因）。

#### Scenario: 情绪激动转接
- **WHEN** 用户愤怒情绪得分 > 4
- **THEN** 系统优先安抚并转人工，同时传递上下文卡片

### Requirement: 多轮对话与上下文管理
系统 SHALL 跟踪会话状态（session_id、user_id、current_intent、slots、history、emotion_score、turn_count、failed_attempts），采用分层摘要（近 5 轮原文、中期单句摘要、早期会话级摘要），并检测意图切换（相似度<0.6 或用户明示）。

#### Scenario: 长对话不失忆
- **WHEN** 对话超过 5 轮
- **THEN** 系统对早期对话做摘要，关键信息保留率 ≥ 90%，Token 消耗降低 60%+

### Requirement: 工单处理与知识回流
系统 SHALL 自动创建、分类、定优先级、查询工单进度；并形成"人工处理 → 解决方案沉淀 → 意图标注 → 补充知识库 → 下次智能回答"的回流闭环。

#### Scenario: 工单创建
- **WHEN** 复杂问题需人工跟进
- **THEN** 工单 Agent 自动提取关键信息生成工单并归类定级

### Requirement: 知识库管理后台
系统 SHALL 提供知识库管理后台，支持文档自动解析与更新（全量/增量/实时）、版本管理与回滚、灰度验证、知识质量校验、历史工单知识挖掘、检索效果评估指标。

#### Scenario: 知识更新
- **WHEN** 重要政策变更
- **THEN** API 触发单篇实时更新，先灰度验证再全量上线

### Requirement: 性能与可观测性
系统 SHALL 满足平均响应 ≤ 3 秒、并发 1000+ 在线，具备日志、监控告警、熔断降级、运营数据看板、Token 用量监控、大小模型分层与热点缓存。

#### Scenario: 高并发
- **WHEN** 1000+ 用户同时在线
- **THEN** 系统通过多实例部署与熔断降级保持稳定，响应 ≤ 3 秒
