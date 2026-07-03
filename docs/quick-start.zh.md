---
title: 一分钟跑起来
description: 三步启动智能客服系统，默认免鉴权、mock 业务系统、内存队列，开箱即用无需 LLM Key。
---

# :material-rocket-launch: 一分钟跑起来

本指南帮助你在最短时间内把智能客服系统跑起来。系统默认以 **mock 模式**启动，无需配置任何 LLM API Key、无需 Redis、无需真实业务系统，克隆即用。

!!! tip "最小可用提示"
    默认配置下系统走 **mock 模式**：`LLM_API_KEY` 留空时自动启用 `_MockLLM`，从用户消息中拼装回复；`BUSINESS_ADAPTER_MODE=mock` 使用内存 mock 业务系统；Redis 未配置时降级内存队列。**无 LLM 也能跑通全链路**，便于本地开发与演示。

---

## :material-check-circle: 前置条件

| 项目 | 要求 |
|------|------|
| Python | 3.11 及以上 |
| 操作系统 | Windows / macOS / Linux 均可 |
| 磁盘空间 | 约 2 GB（含 BGE 模型权重） |
| 网络 | 首次启动需联网下载 BGE 模型（可选，下载失败自动降级） |

---

## :material-step-forward: 三步快速启动

### 第一步：克隆仓库

```bash
git clone https://github.com/sakura-del/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base.git
cd Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base
```

### 第二步：安装依赖

```bash
# 推荐使用虚拟环境
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 安装全部依赖
pip install -r requirements.txt
```

!!! note "依赖说明"
    关键依赖包括：`fastapi`、`langgraph`、`langfuse`、`openai`、`chromadb`、`sentence-transformers`、`rank-bm25`。详细安装排障见 [安装指南](installation.md)。

### 第三步：启动服务

```bash
# 复制环境变量模板（默认全部留空，开箱即用）
cp .env.example .env

# 启动服务
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动成功后，控制台会输出类似信息：

```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
```

!!! success "启动验证"
    打开浏览器访问以下地址确认服务正常：
    
    - **对话界面**：[http://localhost:8000/](http://localhost:8000/)
    - **API 文档**：[http://localhost:8000/docs](http://localhost:8000/docs)
    - **监控面板**：[http://localhost:8000/monitor](http://localhost:8000/monitor)
    - **运营看板**：[http://localhost:8000/operations](http://localhost:8000/operations)

    也可用命令行快速验证：

    ```bash
    curl http://localhost:8000/api/v1/health
    # 返回 200 即代表服务正常
    ```

---

## :material-chat: 首次对话示例

服务启动后即可调用对话接口。默认免鉴权（`API_KEY` 留空），无需携带 `X-API-Key` 请求头。

=== "curl"

    ```bash
    curl -X POST http://localhost:8000/api/v1/chat \
      -H "Content-Type: application/json" \
      -d '{"message": "忘记登录密码怎么办？"}'
    ```

    预期返回：

    ```json
    {
      "reply": "您可以通过登录页的「忘记密码」链接...",
      "session_id": "a1b2c3d4-...",
      "intent": "knowledge_qa",
      "sources": ["faq.md"],
      "escalate_to_human": false
    }
    ```

=== "Python requests"

    ```python
    import requests

    # 调用对话端点（默认免鉴权）
    response = requests.post(
        "http://localhost:8000/api/v1/chat",
        json={"message": "忘记登录密码怎么办？"},
        timeout=30,
    )

    data = response.json()
    print(f"回复：{data['reply']}")
    print(f"意图：{data['intent']}")
    print(f"来源：{data['sources']}")
    print(f"是否转人工：{data['escalate_to_human']}")
    ```

=== "Python httpx（异步）"

    ```python
    import asyncio
    import httpx

    async def chat() -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "http://localhost:8000/api/v1/chat",
                json={"message": "产品有哪些功能"},
            )
            data = resp.json()
            print(data["reply"])

    asyncio.run(chat())
    ```

!!! info "mock 模式下的回复"
    若未配置 `LLM_API_KEY`，系统走 `_MockLLM`，会从知识库检索结果中拼装回复。回复内容虽非 LLM 生成，但检索链路（向量 + BM25 + RRF + Reranker）完整运行，可用于验证知识库入库与检索效果。

---

## :material-waveform: 流式对话示例（SSE）

流式端点 `/api/v1/chat/stream` 以 Server-Sent Events 逐 token 返回，首字延迟更低、体验更流畅。

=== "curl"

    ```bash
    # -N 禁用缓冲，实时输出 SSE 流
    curl -N -X POST http://localhost:8000/api/v1/chat/stream \
      -H "Content-Type: application/json" \
      -d '{"message": "产品有哪些功能"}'
    ```

    输出格式（每行一个事件）：

    ```
    data: {"type": "meta", "session_id": "a1b2c3d4-..."}

    data: {"type": "token", "content": "我"}

    data: {"type": "token", "content": "们的"}

    data: {"type": "token", "content": "产品"}

    data: {"type": "done", "sources": ["faq.md"]}
    ```

=== "Python（sseclient）"

    ```python
    import json
    import requests

    # 流式读取 SSE 事件
    response = requests.post(
        "http://localhost:8000/api/v1/chat/stream",
        json={"message": "产品有哪些功能"},
        stream=True,
        timeout=60,
    )

    # 逐行解析 SSE data 字段
    for line in response.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        payload = json.loads(line[6:])
        if payload["type"] == "token":
            print(payload["content"], end="", flush=True)
        elif payload["type"] == "done":
            print(f"\n\n来源：{payload.get('sources', [])}")
    ```

!!! warning "mock 模式下的流式行为"
    mock 模式无真实 LLM 流式输出，系统会将拼装好的回复一次性作为单个 token 事件返回。配置真实 LLM 后才会逐 token 流式返回。

---

## :material-database-plus: 入库你的第一份知识文档

要让系统回答你的业务问题，需要先入库知识文档。支持 PDF、Word、HTML、Markdown 格式。

```bash
# 上传一份 FAQ 文档（mock 模式免鉴权，无需 X-API-Key）
curl -X POST http://localhost:8000/api/v1/knowledge/ingest \
  -F "file=@docs/faq.md" \
  -F "knowledge_type=faq"
```

入库后再次对话，系统会基于你的知识库检索回答：

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你们的退货政策是什么？"}'
```

!!! tip "知识库统计"
    调用 `GET /api/v1/knowledge/stats` 可查看已入库文档数、chunk 数与向量库状态。

---

## :material-tune-vertical: 开启真实 LLM（可选）

mock 模式仅用于跑通流程。要获得真实的 LLM 生成能力，编辑 `.env` 填入配置：

```bash
# 编辑 .env 文件
LLM_API_KEY=sk-your-deepseek-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

保存后**重启服务**即可生效。系统会自动用 DeepSeek 做意图识别、Query 改写与答案生成。

??? tip "为什么推荐 DeepSeek？"
    项目在真实 DeepSeek + BGE 环境下验证：Recall@5=1.0、平均响应 2.27s、幻觉率=0。DeepSeek 中文理解优秀且成本较低，适合企业客服场景。也可换用任意 OpenAI 兼容接口（如千问、豆包、GPT-4o-mini）。

---

## :material-arrow-right-bottom: 下一步

| 我想… | 去看 |
|-------|------|
| 了解完整安装与排障 | [安装指南](installation.md) |
| 看懂全部配置项 | [配置说明](configuration.md) |
| 理解系统整体架构 | [总体架构](architecture.md) |
| 了解多 Agent 协同机制 | [多 Agent 协同](architecture/agents.md) |
| 了解 RAG 检索流程 | [RAG 检索流程](architecture/rag.md) |
| 了解降级容错策略 | [降级策略](architecture/fallback.md) |
| 查看全部 API 端点 | [API 参考](api-reference.md) |

---

!!! question "遇到问题？"
    - **启动报错**：检查 Python 版本 ≥ 3.11，依赖是否安装完整。
    - **chromadb 安装失败**：升级 pip（`pip install --upgrade pip`）或安装 Visual C++ Build Tools。
    - **BGE 模型下载慢**：设置 `HF_ENDPOINT=https://hf-mirror.com` 使用国内镜像。
    - **端口被占用**：修改 `.env` 中 `APP_PORT` 或启动时加 `--port 8001`。

    更多排障见 [安装指南 · 常见问题](installation.md#_3)。
