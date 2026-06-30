# 智能客服系统产品手册

## 1. 产品概述

智能客服系统是企业级客户服务平台，整合知识库检索、大模型生成与多渠道接入能力，帮助企业提升客服响应效率与一致性。系统支持 Web、App、微信、钉钉、API 五种接入渠道，可对接内部工单系统。

## 2. 功能模块

### 2.1 知识库管理

知识库支持 Markdown、PDF、Word、HTML、TXT 五种文档格式，单文件最大 50MB。文档入库后自动完成解析、切分、向量化、去重与元数据标注，平均入库耗时约 3 秒/百 KB。

### 2.2 RAG 智能问答

RAG 问答模块基于 BGE-large-zh 向量模型与 OpenAI 兼容大模型，提供带来源标注的答案。默认相似度阈值 0.6，Top-K 默认 5 条。当知识库未命中时返回兜底回复并标记 hit=False。

### 2.3 多渠道接入

Web 渠道支持嵌入式聊天窗口与 iframe 两种模式；App 渠道提供 iOS/Android SDK；微信渠道支持公众号与服务号被动回复；钉钉渠道通过机器人 webhook 接入；API 渠道提供 RESTful 接口供第三方系统调用。

### 2.4 工单系统集成

系统可将未解决的对话自动转工单，工单字段包含：会话 ID、用户 ID、问题摘要、客服建议、优先级、SLA 时长。工单 SLA 默认 24 小时，紧急工单 2 小时。

## 3. 部署与运维

### 3.1 硬件要求

生产环境最低配置：CPU 8 核、内存 16GB、磁盘 200GB SSD。建议配置：CPU 16 核、内存 32GB、磁盘 500GB NVMe。Embedding 模型加载需预留 4GB 显存（如使用 GPU）。

### 3.2 软件依赖

系统基于 Python 3.11 与 FastAPI 构建，向量库使用 ChromaDB 0.5.x，Embedding 模型使用 sentence-transformers 3.1.x。LLM 客户端兼容 OpenAI SDK 1.40+，支持 DeepSeek-V3、GPT-4o-mini、Qwen-Plus 等模型。

### 3.3 配置文件

主要配置项通过 .env 文件管理：APP_PORT 控制服务端口，DEBUG 控制日志级别，CHROMA_PERSIST_DIR 控制向量库持久化目录，SIMILARITY_THRESHOLD 控制检索严格度。修改配置后需重启服务生效。

## 4. 使用指南

### 4.1 知识库入库

通过 /api/v1/knowledge/ingest 接口上传文档，支持 multipart/form-data。入库返回 total_chunks、added_chunks、deduped_chunks 与 embedding_mode 字段，便于监控入库质量。重复入库同一文档会触发去重，相同内容不会重复写入。

### 4.2 调用对话接口

通过 /api/v1/chat 接口发起对话，请求体包含 message、session_id、channel、user_id 四个字段。响应中 reply 为最终回复，data.hit 标识是否命中知识库，data.sources 列出引用来源，data.confidence 反映置信度。

### 4.3 查询知识库统计

通过 /api/v1/knowledge/stats 接口查询当前向量库条目数、集合名与持久化目录，便于运维监控。该接口无需鉴权（开发模式）。

## 5. 常见问题

### 5.1 检索结果为空怎么办？

若所有问题都返回兜底回复，请按以下顺序排查：1) 确认知识库已入库（stats 接口）；2) 检查 SIMILARITY_THRESHOLD 是否过高；3) 检查 Embedding 模式是否为 fallback（fallback 模式无语义检索能力）；4) 查看 retriever 日志确认命中数。

### 5.2 LLM 调用失败如何处理？

LLM 调用失败时系统自动降级到 _MockLLM，基于 Top-1 chunk 拼接回复，保证对话链路不中断。建议在 .env 配置真实 LLM_API_KEY 与 LLM_BASE_URL 以获得最佳效果。常见失败原因：API Key 失效、Base URL 错误、模型名拼写错误、限流。

### 5.3 如何切换 LLM 模型？

修改 .env 中的 LLM_MODEL 字段即可切换模型，支持 deepseek-chat、gpt-4o-mini、qwen-plus 等 OpenAI 兼容模型。切换后需重启服务，新会话生效。已有会话的上下文不受影响。
