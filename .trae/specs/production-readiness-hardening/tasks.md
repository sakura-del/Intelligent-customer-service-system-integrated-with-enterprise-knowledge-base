# Tasks

## Phase 1: 安全加固（P0/P1）

- [x] Task 1: API Key 安全比较 + 启动校验
  - [x] SubTask 1.1: `security.py` 中 `verify_api_key` 改用 `secrets.compare_digest`
  - [x] SubTask 1.2: `config.py` 新增 `insecure_mode` 属性 + `ALLOWED_ORIGINS` 配置项
  - [x] SubTask 1.3: `main.py` 启动事件中检查 API_KEY 为空时打印 WARNING
  - [x] SubTask 1.4: `health.py` 健康检查返回 `insecure_mode` 字段
  - [x] SubTask 1.5: 补充测试

- [x] Task 2: CORS 白名单 + 安全响应头
  - [x] SubTask 2.1: `main.py` CORS 配置改为从 `ALLOWED_ORIGINS` 读取，移除 `*` + `credentials`
  - [x] SubTask 2.2: 新增安全响应头中间件（HSTS/X-Frame-Options/X-Content-Type-Options/CSP/Referrer-Policy）
  - [x] SubTask 2.3: `.env.example` 添加 `ALLOWED_ORIGINS` 示例
  - [x] SubTask 2.4: 补充测试

- [x] Task 3: 全局限流中间件
  - [x] SubTask 3.1: `requirements.txt` 添加 `slowapi`
  - [x] SubTask 3.2: 新增 `app/middleware/rate_limit.py`，按 IP 60/min + 重操作端点 10/min
  - [x] SubTask 3.3: `main.py` 注册限流器
  - [x] SubTask 3.4: 补充测试

- [x] Task 4: 运行时双向敏感词过滤
  - [x] SubTask 4.1: `requirements.txt` 添加 `pyahocorasick`
  - [x] SubTask 4.2: 新增 `app/core/content_filter.py`，AC 自动机 + 三级分级（block/warn/mask）
  - [x] SubTask 4.3: `sensitive_words.txt` 填充基础敏感词（政治/色情/暴力/广告）
  - [x] SubTask 4.4: `chat.py` 流式管道中用户输入和 LLM 输出双向过滤
  - [x] SubTask 4.5: 补充测试

- [x] Task 5: 日志 PII 脱敏
  - [x] SubTask 5.1: `logging.py` 新增 `PIIMaskingFilter`（手机号/身份证/邮箱/银行卡正则替换）
  - [x] SubTask 5.2: `setup_logging` 中注册 Filter
  - [x] SubTask 5.3: 补充测试

- [x] Task 6: 会话超时清理
  - [x] SubTask 6.1: `session.py` 添加 `last_activity` 时间戳，`get_or_create`/`add_message` 时更新
  - [x] SubTask 6.2: 新增 `cleanup_expired_sessions` 方法，TTL 30 分钟
  - [x] SubTask 6.3: `main.py` 启动定时线程，每 5 分钟清扫一次
  - [x] SubTask 6.4: 补充测试

- [x] Task 7: 文件上传限制 + 输入校验
  - [x] SubTask 7.1: `knowledge.py` 入库端点添加文件类型白名单（.md/.txt/.pdf/.docx）+ 大小检查 10MB
  - [x] SubTask 7.2: `schemas/chat.py` 的 `ChatRequest.message` 添加 `max_length=2000`，`channel` 添加 pattern
  - [x] SubTask 7.3: 补充测试

## Phase 2: 性能优化

- [x] Task 8: 检索链路并行化
  - [x] SubTask 8.1: `hybrid_retriever.py` 的 `retrieve` 方法用 ThreadPoolExecutor 并行向量+BM25 召回
  - [x] SubTask 8.2: 异常降级：一路失败不影响另一路
  - [x] SubTask 8.3: 补充测试 + 延迟对比验证

- [x] Task 9: ModelRouter 竞态消除
  - [x] SubTask 9.1: `llm_client.py` 的 `chat`/`stream_chat` 支持传入 `model` 参数覆盖
  - [x] SubTask 9.2: `performance.py` 的 `chat_with_routing` 改为传参，不修改 `client.model`
  - [x] SubTask 9.3: 补充并发测试

- [x] Task 10: 运维 API 鉴权
  - [x] SubTask 10.1: `operations.py` 和 `performance.py` 路由添加 `Depends(verify_api_key)`
  - [x] SubTask 10.2: 更新现有测试（补 API Key header）

## Phase 3: 工程质量

- [x] Task 11: CI 测试工作流
  - [x] SubTask 11.1: 新增 `.github/workflows/test.yml`，触发 push/PR，运行 pytest + 覆盖率
  - [x] SubTask 11.2: 确认 37+ 测试在 CI 环境通过

- [x] Task 12: 代码质量门禁
  - [x] SubTask 12.1: 新增 `pyproject.toml`，配置 ruff（line-length=100, 规则集）+ mypy（strict 可选）
  - [x] SubTask 12.2: 新增 `.pre-commit-config.yaml`（ruff check + ruff format + trailing-whitespace + end-of-file-fixer）
  - [x] SubTask 12.3: 运行 `ruff check --fix` 修复现有代码问题

- [x] Task 13: 测试基础设施
  - [x] SubTask 13.1: 新增 `tests/conftest.py`，抽取公共 fixture（ChromaDB 隔离目录、单例重置、TestClient 工厂）
  - [x] SubTask 13.2: 确保现有测试不回归

- [x] Task 14: 文档更新
  - [x] SubTask 14.1: `docs/configuration.zh.md` / `.en.md` 新增安全配置说明（ALLOWED_ORIGINS / 限流 / 敏感词）
  - [x] SubTask 14.2: `README.md` 补充安全注意事项
  - [x] SubTask 14.3: `docs/tutorials/` 新增安全加固教程（中英双语）
  - [x] SubTask 14.4: `mkdocs.yml` 注册新文档页面
  - [x] SubTask 14.5: `.env.example` 更新所有新增配置项

# Task Dependencies
- [Task 2] depends on [Task 1]（ALLOWED_ORIGINS 配置项在 Task 1 中添加）
- [Task 4] depends on [无]（独立模块）
- [Task 10] depends on [Task 1]（鉴权函数在 Task 1 中修改）
- [Task 11] depends on [Task 12]（CI 中也跑 ruff）
- [Task 13] depends on [Task 12]（conftest 依赖 ruff 通过后不引入新问题）
- Phase 2 的 Task 8/9 可与 Phase 1 并行
- Phase 3 的 Task 11/12 可与 Phase 1/2 并行
