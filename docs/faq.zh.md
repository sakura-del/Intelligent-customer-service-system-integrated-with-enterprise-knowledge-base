---
title: 常见问题
description: 安装、配置、使用、性能四类常见问题解答，按类别组织，每个问题可折叠展开。
weight: 120
---

# 常见问题

按 **安装 / 配置 / 使用 / 性能** 四类组织，点击问题展开查看答案。若未找到答案，请到 [GitHub Issues](https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base/issues) 提交。

---

## 安装问题

??? question "Q: chromadb 安装失败怎么办？"

    **A:** chromadb 依赖 `onnxruntime` 与 `pypika`，常见失败原因与解决方案：

    === "升级 pip 与构建工具"

        ```bash
        # 升级 pip 到最新版本，避免老版本解析依赖树失败
        python -m pip install --upgrade pip setuptools wheel
        # 重新安装
        pip install -r requirements.txt
        ```

    === "Windows 安装 VS Build Tools"

        部分依赖（如 `hnswlib`）需要 C++ 编译环境：

        1. 下载 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
        2. 安装时勾选「使用 C++ 的桌面开发」工作负载
        3. 重启终端后重新 `pip install`

    === "使用预编译 wheel"

        ```bash
        # 优先安装预编译版本，跳过源码编译
        pip install chromadb --only-binary :all:
        ```

??? question "Q: BGE 嵌入模型下载很慢怎么办？"

    **A:** BGE 模型托管在 Hugging Face，国内访问慢可使用镜像源。

    ```bash
    # 方式 1：设置 HF 镜像端点（推荐）
    export HF_ENDPOINT=https://hf-mirror.com
    pip install -r requirements.txt

    # 方式 2：手动下载模型权重到本地，在 .env 中指定路径
    # git clone https://hf-mirror.com/BAAI/bge-large-zh-v1.5 models/bge-large-zh
    # .env 中设置：
    # EMBEDDING_MODEL=./models/bge-large-zh
    ```

    !!! tip "首次加载会缓存"
        模型首次加载后会缓存到 `~/.cache/huggingface/`，后续启动无需重新下载。

??? question "Q: Python 3.10 能用吗？"

    **A:** 推荐使用 **Python 3.11+**，3.10 部分依赖可能不兼容。

    | 版本 | 支持情况 | 说明 |
    |------|----------|------|
    | 3.11+ | ✅ 推荐 | 全部依赖测试通过 |
    | 3.10 | ⚠️ 部分兼容 | `chromadb` / `langfuse` 部分特性可能异常 |
    | 3.9 及以下 | ❌ 不支持 | 类型注解与语法不兼容 |

    ```bash
    # 推荐使用 pyenv 或 conda 管理 Python 版本
    pyenv install 3.11.9
    pyenv local 3.11.9
    ```

??? question "Q: Redis 没有启动会怎样？"

    **A:** 系统会自动降级到内存队列，不影响主链路功能，但会话与缓存不持久化。

    ```bash
    # 推荐用 Docker 启动 Redis
    docker run -d --name redis -p 6379:6379 redis:7-alpine

    # 验证连接
    redis-cli ping  # 应返回 PONG
    ```

    !!! warning "生产环境必须启动 Redis"
        生产环境若不启动 Redis，进程重启后会话与缓存全部丢失。

---

## 配置问题

??? question "Q: API_KEY 留空安全吗？"

    **A:** 仅适用于本地开发模式，**生产环境必须设置非空 `API_KEY`**。

    - `API_KEY` 为空时，`verify_api_key` 依赖跳过鉴权，任意调用方可访问
    - `API_KEY` 非空时，所有标记 ✅ 的端点都要求请求头 `X-API-Key` 与之匹配

    ```bash
    # .env 示例
    # 开发模式（本地调试）
    API_KEY=

    # 生产模式（必填）
    API_KEY=your-strong-random-key-here
    ```

    !!! danger "安全风险"
        生产环境留空 `API_KEY` 等于完全开放接口，可被任意调用方利用，包括知识库入库、删除等敏感操作。

??? question "Q: SMALL_LLM_API_KEY 不配置会怎样？"

    **A:** `ModelRouter` 会自动降级到主 LLM，无副作用，仅首 Token 延迟略增。

    | 配置情况 | 行为 | 首 Token 延迟 |
    |----------|------|---------------|
    | 配置小模型 + 主模型 | 意图识别走小模型，生成走主模型 | ~200ms |
    | 仅配置主模型 | 全部走主模型 | ~800ms |
    | 均未配置 | 走 mock 模式（无真实 LLM） | <50ms |

    ```bash
    # .env 配置示例（千问 qwen-turbo）
    SMALL_LLM_API_KEY=sk-xxxxx
    SMALL_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    SMALL_LLM_MODEL=qwen-turbo
    SMALL_MODEL_THRESHOLD=0.5
    ```

??? question "Q: Langfuse 配置错误会阻塞主链路吗？"

    **A:** 不会。Langfuse 客户端内置降级机制，配置错误时自动转为 no-op。

    ```python
    # app/core/langfuse_client.py 降级逻辑（简化）
    if not settings.LANGFUSE_ENABLED or not settings.LANGFUSE_PUBLIC_KEY:
        # 全部方法返回 None，不抛异常
        return NoOpLangfuseTrace()
    ```

    触发降级的条件：

    - `LANGFUSE_ENABLED=False`
    - `LANGFUSE_PUBLIC_KEY` 或 `LANGFUSE_SECRET_KEY` 为空
    - Langfuse 服务连接超时（默认 3 秒）

    !!! note "降级后的影响"
        降级后 trace 不会上报 Langfuse，但本地 `Monitor` 仍记录 trace 摘要，可通过 `/api/v1/monitor/traces` 查看。

??? question "Q: BUSINESS_ADAPTER_MODE 该选 mock 还是 http？"

    **A:** 开发与测试用 `mock`，对接真实业务系统时切 `http`。

    ```bash
    # mock 模式：内存模拟订单/会员/退换货 API，开箱即用
    BUSINESS_ADAPTER_MODE=mock

    # http 模式：调用真实业务系统 REST API
    BUSINESS_ADAPTER_MODE=http
    BUSINESS_API_BASE_URL=https://your-business-api.com
    BUSINESS_API_KEY=your-business-api-key
    BUSINESS_API_TIMEOUT=10
    ```

    !!! warning "http 模式降级"
        `BUSINESS_API_BASE_URL` 留空时自动降级 mock 并告警，不会阻塞业务查询。

??? question "Q: 如何调整工作时间段（转接时间窗）？"

    **A:** 修改 `.env` 中的 `WORKING_HOURS_START` / `WORKING_HOURS_END`：

    ```bash
    # 24 小时制，[START, END) 半开区间，超出该区间不主动转接情绪/失败类诉求
    WORKING_HOURS_START=9
    WORKING_HOURS_END=18
    TIMEZONE=Asia/Shanghai
    ```

    !!! note "用户主动转人工不受时间窗限制"
        即使在非工作时间段，用户主动说「转人工」仍会触发转接，仅系统主动转接受时间窗约束。

---

## 使用问题

??? question "Q: 知识库更新后回答没变怎么办？"

    **A:** 需调用 `/api/v1/performance/cache/invalidate` 清空热点缓存。

    ```bash
    # 清空热点缓存，下次查询会重新走检索
    curl -X POST http://localhost:8000/api/v1/performance/cache/invalidate \
      -H "X-API-Key: $API_KEY"
    ```

    **原因**：`HotQueryCache` 会缓存高频查询的回复（默认 1000 条），知识库更新后缓存仍返回旧回复。

    !!! tip "批量入库后自动清缓存"
        使用 [批量入库脚本](examples.zh.md#示例-3知识库批量入库脚本) 时已内置清缓存调用，无需手动清。

??? question "Q: 流式响应（SSE）经常断开怎么办？"

    **A:** 通常是 Nginx / CDN 缓冲了 SSE 流，需关闭缓冲。

    === "Nginx 配置"

        ```nginx
        location /api/v1/chat/stream {
            proxy_pass http://backend;
            proxy_buffering off;          # 关闭响应缓冲
            proxy_cache off;              # 关闭缓存
            chunked_transfer_encoding on; # 启用分块传输
            proxy_read_timeout 300s;      # 延长读超时
            proxy_set_header Connection '';  # 清除 Connection 头
        }
        ```

    === "Cloudflare CDN"

        Cloudflare 默认缓冲 SSE，需在 Page Rules 中关闭：
        - 匹配：`*/api/v1/chat/stream*`
        - 设置：`Cache Level: Bypass`

    === "客户端验证"

        ```bash
        # -N 禁用 curl 缓冲，验证 SSE 是否实时返回
        curl -N -X POST http://localhost:8000/api/v1/chat/stream \
          -H "X-API-Key: $API_KEY" \
          -H "Content-Type: application/json" \
          -d '{"message": "测试流式"}'
        ```

    系统已设置 `X-Accel-Buffering: no` 响应头，但部分代理仍需显式配置。

??? question "Q: 转接后会话历史保留吗？"

    **A:** 是，转接后会话 `history` 完整保留，坐席可查看全部上下文。

    ```bash
# 查看会话详情，history 字段包含全部对话记录
curl http://localhost:8000/api/v1/agent/sessions/$SESSION_ID \
  -H "X-API-Key: $API_KEY" | jq .history
```

    返回的 `history` 包含转接前的全部 user / assistant 消息，坐席据此快速了解用户诉求。

??? question "Q: 如何判断是否需要转人工？"

    **A:** 系统在以下三种场景自动触发转人工：

    | 触发条件 | 说明 |
    |----------|------|
    | 用户主动要求 | 命中「转人工」「人工客服」等关键词 |
    | 情绪敏感意图 | 情绪评分低于阈值，识别为 `emotion_sensitive` |
    | 连续失败达阈值 | `failed_attempts >= ESCALATE_FAILED_THRESHOLD`（默认 3 次） |

    转接后会生成 `EscalationCard`，含转接原因、优先级、上下文摘要，传递给坐席工作台。

??? question "Q: 如何录入人工方案沉淀回知识库？"

    **A:** 通过 `/api/v1/agent/sessions/{session_id}/solution` 或 `/api/v1/escalation/solution` 录入。

    ```bash
    # 方式 1：坐席端点录入（推荐，与会话关联）
    curl -X POST http://localhost:8000/api/v1/agent/sessions/$SESSION_ID/solution \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "question": "如何退货？",
        "solution": "请在订单页点击退货……",
        "intent": "knowledge_qa"
      }'

    # 方式 2：转接端点录入（独立于会话）
    curl -X POST http://localhost:8000/api/v1/escalation/solution \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"session_id": "xxx", "question": "...", "solution": "..."}'
    ```

    录入后进入 `pending` 审核队列，审核通过后入库为 FAQ，下次智能客服可检索命中。

??? question "Q: 如何查看 trace 排查问题？"

    **A:** 系统提供两套 trace 查看方式：

    === "本地 Monitor"

        ```bash
        # 查看最近 10 条 trace
        curl "http://localhost:8000/api/v1/monitor/traces?limit=10" | jq .

        # 查看单条 trace 详情（含每步耗时）
        curl http://localhost:8000/api/v1/monitor/traces/$TRACE_ID | jq .
        ```

    === "Langfuse 控制台"

        配置 `LANGFUSE_ENABLED=True` 后，访问 [Langfuse Cloud](https://cloud.langfuse.com) 查看：

        - 全链路 trace 可视化
        - 11 个 prompt 标记的 name/version
        - token / cost / latency 自动统计

        详见 [示例 5：与 Langfuse 集成](examples.zh.md#示例-5与-langfuse-集成查看-trace)。

---

## 性能问题

??? question "Q: 首次查询很慢（约 2.7 秒）怎么办？"

    **A:** 主要耗时在 DeepSeek 查询改写阶段，可通过关闭 `query_rewrite` 优化。

    **耗时分布**（典型场景）：

    | 阶段 | 耗时 | 说明 |
    |------|------|------|
    | 意图识别 | ~300ms | 小模型（qwen-turbo） |
    | 查询改写 | ~1500ms | 主 LLM（DeepSeek），阻塞主链路 |
    | 混合检索 | ~280ms | 向量 + BM25 + RRF |
    | 重排序 | ~150ms | BGE reranker |
    | 生成 | ~500ms | 主 LLM 流式生成 |

    **优化方案**：

    ```bash
    # 方案 1：关闭查询改写（牺牲少量召回率换延迟）
    # 在 retrieval_tuner 中将 enable_query_rewrite 设为 false
    curl -X PUT http://localhost:8000/api/v1/tuner/params \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"enable_query_rewrite": false}'

    # 方案 2：使用流式端点（首 Token <1s，用户感知更快）
    curl -N -X POST http://localhost:8000/api/v1/chat/stream \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"message": "退货流程"}'
    ```

    !!! tip "热点缓存命中后首 Token <100ms"
        相同 query 第二次请求会命中 `HotQueryCache`，跳过全部编排，首 Token <100ms。

??? question "Q: 大量并发时响应变慢怎么办？"

    **A:** 调整检索参数与开启热点缓存，降低单次请求开销。

    === "调整检索参数"

        ```bash
        # 降低 VECTOR_TOP_K 与 BM25_TOP_K，减少召回量
        curl -X PUT http://localhost:8000/api/v1/tuner/params \
          -H "X-API-Key: $API_KEY" \
          -H "Content-Type: application/json" \
          -d '{
            "vector_top_k": 15,
            "bm25_top_k": 15,
            "rerank_top_k": 3
          }'
        ```

        | 参数 | 默认 | 推荐（高并发） | 影响 |
        |------|------|----------------|------|
        | `VECTOR_TOP_K` | 25 | 15 | 向量召回量 |
        | `BM25_TOP_K` | 25 | 15 | 关键词召回量 |
        | `RERANK_TOP_K` | 5 | 3 | 进入生成的片段数 |

    === "确认热点缓存生效"

        ```bash
        # 查看缓存命中率，应 >50% 才算生效
        curl http://localhost:8000/api/v1/performance/cache/stats | jq .
        ```

        命中率低时检查 `HotQueryCache` 是否启用，或调大缓存容量。

    === "查看综合性能指标"

        ```bash
        curl http://localhost:8000/api/v1/performance/metrics | jq .
        # 关注 cache_hit_rate / avg_response_ms / concurrent_requests
        ```

??? question "Q: 内存占用过高怎么办？"

    **A:** 调整 `EMBEDDING_BATCH_SIZE` 与 ChromaDB 缓存策略。

    ```bash
    # .env 调整批大小，降低单次嵌入内存峰值
    EMBEDDING_BATCH_SIZE=16   # 默认 32，内存紧张时减半

    # ChromaDB 持久化目录，避免内存索引
    CHROMA_PERSIST_DIR=./chroma_data
    ```

    **内存占用主要来源**：

    | 组件 | 占用 | 优化方式 |
    |------|------|----------|
    | BGE 嵌入模型 | ~2GB | 模型常驻内存，无法释放 |
    | ChromaDB 索引 | ~500MB | 持久化到磁盘，减少内存索引 |
    | LLM 客户端连接池 | ~100MB | 复用连接，避免重复创建 |
    | 会话历史 | 取决于并发 | 调整 `MAX_HISTORY_LENGTH` |

    !!! tip "容器部署建议"
        Docker 部署时建议预留 4GB 内存，Kubernetes 部署时设置 `resources.limits.memory: 4Gi`。

??? question "Q: 如何监控 Token 用量避免超预算？"

    **A:** 通过 `/api/v1/observability/token-usage` 端点查看用量，配合告警机制。

    ```bash
    # 查看小时级 Token 用量
    curl "http://localhost:8000/api/v1/observability/token-usage?window=hour" | jq .

    # 查看告警列表（含 Token 超限告警）
    curl "http://localhost:8000/api/v1/observability/alerts?source=token_usage" | jq .
    ```

    !!! tip "ModelRouter 自动降本"
        配置 `SMALL_LLM_API_KEY` 后，意图识别等轻量任务走小模型（成本约为 GPT-4o-mini 的 1/10），主 LLM 仅用于生成。

??? question "Q: 如何评估检索效果是否达标？"

    **A:** 调用 `/api/v1/evaluation/run` 触发评测，关注 Recall@5 与 Hit Rate。

    ```bash
    # 使用内置 30 条测试集评测
    curl -X POST http://localhost:8000/api/v1/evaluation/run \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d '{}' | jq .

    # 查看历史报告
    curl http://localhost:8000/api/v1/evaluation/reports | jq .
    ```

    **达标标准**（项目实测）：

    | 指标 | 目标 | 实测 |
    |------|------|------|
    | Recall@5 | ≥ 0.85 | 1.0 |
    | Hit Rate | ≥ 0.90 | 0.9333 |
    | 幻觉率 | ≤ 0.10 | 0.0 |

---

## 相关文档

- [API 参考](api-reference.zh.md)：全部端点详细说明
- [示例代码](examples.zh.md)：5 个实战示例
- [配置说明](configuration.md)：30+ 配置项详解
- [性能优化](tutorials/performance.md)：三层缓存与并发优化
- [贡献指南](contributing.zh.md)：提交 Issue 与 PR 的规范
