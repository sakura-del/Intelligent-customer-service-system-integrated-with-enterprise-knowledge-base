# 生产化加固 Spec

## Why

系统功能已基本完整（多 Agent 编排 + RAG + 流式 + 评估 + 可观测性），但**安全策略处于骨架级**（API_KEY 默认空导致免鉴权、CORS 通配+凭据、无全局限流、敏感词表为空、无日志脱敏），**性能存在已知瓶颈**（检索链路未并行、伪流式、ModelRouter 竞态），**工程质量缺口明显**（无 CI 测试工作流、无 lint/type-check、无 pre-commit）。当前状态不适合直接生产部署，需系统性加固。

## 能力边界

### 当前能力（已实现）
- 多 Agent 编排：orchestrator → dialog/knowledge/business/emotion/ticket 6 个 Agent
- 知识库 RAG：BGE-large-zh + BM25 混合检索 + RRF 融合 + HNSW 索引
- 流式响应：SSE 三级缓存（HotQueryCache/IntentCache/关键词快通道），首 Token < 100ms
- 评估体系：检索 5 指标（Recall@K/Precision/HitRate/MRR/Hallucination）+ RAGAS 4 指标
- 可观测性：告警抑制 + 健康检查 + Token 追踪 + Langfuse 链路追踪
- 运维能力：灰度实验框架 + 上线检查清单 + 运营看板
- 熔断降级：三态机 + 按依赖独立熔断
- 文档站点：MkDocs Material 中英双语 + Cloudflare/GitHub Pages 部署

### 目标能力（本次新增/加固）
- 生产级认证授权：常量时间比较 + 启动强制校验 + 可选 JWT
- 请求安全防护：全局限流 + CORS 白名单 + 安全响应头 + 文件上传限制
- 内容安全：运行时双向敏感词过滤（AC 自动机 + 分级）
- 数据安全：日志 PII 脱敏 + 会话超时清理
- 性能优化：检索并行化 + ModelRouter 竞态消除
- 工程质量：CI 测试工作流 + ruff/mypy + pre-commit

### 不在本次范围
- 多实例部署 / Redis 会话共享（架构变更过大，后续迭代）
- 数据静态加密 / KMS 集成（需基础设施支持）
- 用户级认证体系 / RBAC（需产品定义用户模型）
- 端点全量 async 化（涉及全链路改造，后续迭代）

## What Changes

### 安全加固
- API Key 比较改用 `secrets.compare_digest`，启动时强制校验非空
- CORS 改为白名单配置（`ALLOWED_ORIGINS` 环境变量），移除 `*` + `credentials` 组合
- 添加安全响应头中间件（HSTS/X-Frame-Options/X-Content-Type-Options/CSP）
- 新增全局限流中间件（slowapi，按 IP + 按 API Key 双维度）
- 敏感词过滤扩展为运行时双向（用户输入 + LLM 输出），AC 自动机匹配 + 三级分级
- 日志脱敏过滤器（手机号/身份证/邮箱/银行卡自动打码）
- 会话超时清理（TTL 30 分钟 + 定时清扫）
- 文件上传限制（类型白名单 + 大小上限 10MB）
- `ChatRequest.message` 添加 `max_length=2000`，`channel` 添加 pattern

### 性能优化
- 检索链路并行化：向量召回 + BM25 召回用 ThreadPoolExecutor 并行
- ModelRouter 竞态消除：改为每次调用传 model 参数，不修改 client.model
- `_stream_non_knowledge` 改为真流式（LLM stream_chat 透传）

### 工程质量
- 新增 `.github/workflows/test.yml`：pytest + 覆盖率上报
- 新增 `pyproject.toml`：ruff + mypy 配置
- 新增 `.pre-commit-config.yaml`：ruff + mypy + trailing-whitespace
- 新增 `tests/conftest.py`：公共 fixture（ChromaDB 隔离、单例重置、TestClient 工厂）
- 运维 API（operations/performance）添加 API Key 鉴权

## Impact
- Affected code: `app/core/security.py`, `app/core/config.py`, `app/core/session.py`, `app/core/logging.py`, `app/main.py`, `app/knowledge/quality.py`, `app/knowledge/hybrid_retriever.py`, `app/core/performance.py`, `app/api/v1/chat.py`, `app/schemas/chat.py`
- Affected specs: 无直接依赖（独立加固）
- 新增依赖: `slowapi`, `pyahocorasick`

## ADDED Requirements

### Requirement: API Key 常量时间比较
系统 SHALL 使用 `secrets.compare_digest` 比较 API Key，防止时序攻击。

#### Scenario: 正常鉴权
- **WHEN** 请求携带正确的 `X-API-Key`
- **THEN** 鉴权通过，返回 `api_key_verified`

#### Scenario: 错误 Key
- **WHEN** 请求携带错误的 `X-API-Key`
- **THEN** 返回 401，响应时间与正确 Key 一致（±1ms）

### Requirement: 启动时 API Key 强制校验
系统 SHALL 在启动时检查 `API_KEY` 非空，空值时打印 WARNING 日志并标记 `insecure_mode=True`。

#### Scenario: 未配置 API_KEY
- **WHEN** `API_KEY` 为空字符串启动
- **THEN** 打印醒目 WARNING 日志，`settings.insecure_mode=True`，健康检查返回 `insecure: true`

### Requirement: CORS 白名单
系统 SHALL 从 `ALLOWED_ORIGINS` 环境变量读取允许的源，不再使用通配符。

#### Scenario: 白名单内的源
- **WHEN** 请求 Origin 为 `https://example.com` 且在白名单中
- **THEN** 返回 `Access-Control-Allow-Origin: https://example.com`

#### Scenario: 白名单外的源
- **WHEN** 请求 Origin 不在白名单中
- **THEN** 不返回 CORS 头，浏览器拒绝跨域

### Requirement: 全局限流
系统 SHALL 对所有 API 端点实施限流，默认 60 req/min/IP + 10 req/min/API-Key（知识库入库等重操作）。

#### Scenario: 超出限流
- **WHEN** 单个 IP 1 分钟内发送超过 60 个请求
- **THEN** 返回 429 + `Retry-After` 头

### Requirement: 运行时敏感词过滤
系统 SHALL 在用户输入和 LLM 输出双向执行敏感词检测，使用 AC 自动机匹配，支持三级分级（block/warn/mask）。

#### Scenario: 用户输入命中 block 级敏感词
- **WHEN** 用户消息包含 block 级敏感词
- **THEN** 返回预设拒绝回复，不进入 LLM 链路

#### Scenario: LLM 输出命中 warn 级敏感词
- **WHEN** LLM 生成内容包含 warn 级敏感词
- **THEN** 替换为 `***`，记录告警

### Requirement: 日志 PII 脱敏
系统 SHALL 在日志输出前自动脱敏手机号（前3后4）、身份证（前6后4）、邮箱（首字母+域名）、银行卡（前4后4）。

#### Scenario: 日志含手机号
- **WHEN** 日志消息包含 `13812345678`
- **THEN** 输出为 `138****5678`

### Requirement: 会话超时清理
系统 SHALL 对超过 30 分钟无活动的会话自动清理，定时清扫间隔 5 分钟。

#### Scenario: 会话超时
- **WHEN** 会话最后活动时间距今超过 30 分钟
- **THEN** 会话被清理，内存释放

### Requirement: 文件上传安全限制
系统 SHALL 限制知识库入库文件类型为 `.md/.txt/.pdf/.docx`，大小上限 10MB。

#### Scenario: 超大文件
- **WHEN** 上传文件 > 10MB
- **THEN** 返回 413 Payload Too Large

### Requirement: CI 测试工作流
系统 SHALL 在每次 PR 和 push 到 main 时自动运行 pytest，失败则阻断合并。

#### Scenario: 测试通过
- **WHEN** 所有 37+ 测试通过
- **THEN** CI 检查状态为 success

#### Scenario: 测试失败
- **WHEN** 任一测试失败
- **THEN** CI 检查状态为 failure，附失败详情

### Requirement: 代码质量门禁
系统 SHALL 配置 ruff（lint + format）和 mypy（类型检查），通过 pre-commit 在提交前执行。

#### Scenario: 提交含 lint 错误
- **WHEN** 开发者提交含未使用变量的代码
- **THEN** pre-commit 阻止提交，提示修复

## MODIFIED Requirements

### Requirement: 检索链路并行化
`hybrid_retriever.retrieve` SHALL 并行执行向量召回和 BM25 召回，使用 ThreadPoolExecutor。

#### Scenario: 正常检索
- **WHEN** 执行混合检索
- **THEN** 向量召回和 BM25 召回并行执行，总延迟 ≈ max(向量延迟, BM25延迟) 而非两者之和

### Requirement: ModelRouter 竞态消除
`ModelRouter.chat_with_routing` SHALL 通过参数传递模型名称，不修改 `LLMClient.model` 属性。

#### Scenario: 并发调用
- **WHEN** 多个请求同时路由到不同模型
- **THEN** 每个请求使用正确的模型，无竞态错配

### Requirement: 运维 API 鉴权
`/api/v1/operations/*` 和 `/api/v1/performance/*` SHALL 强制 API Key 鉴权。

#### Scenario: 无 Key 访问运维 API
- **WHEN** 请求未携带 `X-API-Key`
- **THEN** 返回 401
