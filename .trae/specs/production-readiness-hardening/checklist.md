# Checklist

## Phase 1: 安全加固

- [x] `verify_api_key` 使用 `secrets.compare_digest`，不再用 `!=` 明文比较
- [x] 启动时 API_KEY 为空会打印 WARNING 日志并标记 `insecure_mode=True`
- [x] 健康检查端点返回 `insecure_mode` 字段
- [x] CORS 配置从 `ALLOWED_ORIGINS` 读取，不再使用 `*` + `credentials` 组合
- [x] 安全响应头中间件已注册（HSTS/X-Frame-Options/X-Content-Type-Options/CSP/Referrer-Policy）
- [x] 全局限流生效：IP 维度 60/min，重操作端点 10/min，超出返回 429
- [x] 敏感词过滤在用户输入和 LLM 输出双向执行
- [x] AC 自动机匹配敏感词，支持 block/warn/mask 三级分级
- [x] `sensitive_words.txt` 已填充基础敏感词
- [x] 日志中手机号/身份证/邮箱/银行卡自动脱敏
- [x] 会话超时 30 分钟自动清理，定时清扫 5 分钟间隔
- [x] 文件上传限制类型 `.md/.txt/.pdf/.docx` + 大小 10MB
- [x] `ChatRequest.message` 有 `max_length=2000`，`channel` 有 pattern 校验

## Phase 2: 性能优化

- [x] `hybrid_retriever.retrieve` 中向量召回和 BM25 召回并行执行
- [x] 一路召回失败不影响另一路（降级处理）
- [x] `LLMClient.chat`/`stream_chat` 支持传入 `model` 参数覆盖
- [x] `ModelRouter.chat_with_routing` 不再修改 `client.model` 属性
- [x] 并发调用 ModelRouter 无模型错配
- [x] `/api/v1/operations/*` 和 `/api/v1/performance/*` 强制 API Key 鉴权

## Phase 3: 工程质量

- [x] `.github/workflows/test.yml` 在 push/PR 时运行 pytest
- [x] CI 中 37+ 测试全部通过
- [x] `pyproject.toml` 配置了 ruff（lint + format）
- [x] `pyproject.toml` 配置了 mypy（类型检查）
- [x] `.pre-commit-config.yaml` 注册了 ruff + mypy + 基础钩子
- [x] `ruff check --fix` 已修复现有代码问题
- [x] `tests/conftest.py` 提供公共 fixture（ChromaDB 隔离、单例重置、TestClient 工厂）
- [x] 现有测试在使用 conftest 后无回归
- [x] `docs/configuration` 中英双语新增安全配置说明
- [x] `README.md` 补充安全注意事项
- [x] `docs/tutorials/` 新增安全加固教程（中英双语）
- [x] `mkdocs.yml` 注册新文档页面
- [x] `.env.example` 更新所有新增配置项（ALLOWED_ORIGINS 等）
