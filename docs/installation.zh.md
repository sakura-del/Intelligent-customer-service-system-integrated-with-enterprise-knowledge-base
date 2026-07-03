---
title: 安装指南
description: 完整安装步骤、BGE 模型下载、环境变量配置、可选依赖与常见安装问题排查。
---

# :material-package-variant: 安装指南

本指南覆盖从环境准备到服务启动验证的完整流程，并包含常见安装问题的排查方案。

!!! tip "只想快速跑起来？"
    如果你只想最快速度启动，请直接看 [一分钟跑起来](quick-start.md)。本指南面向需要完整配置与排障的场景。

---

## :material-clipboard-list: 环境要求

| 项目 | 最低版本 | 推荐版本 | 说明 |
|------|----------|----------|------|
| Python | 3.11 | 3.11 / 3.12 | 使用了 `TypedDict`、`tomllib` 等新特性，3.10 及以下不可用 |
| pip | 23.0 | 最新版 | chromadb 等包需要较新 pip 解析依赖 |
| 操作系统 | — | — | Windows 10+ / macOS 12+ / Ubuntu 20.04+ 均已验证 |
| 内存 | 4 GB | 8 GB+ | BGE 模型加载约占 2 GB 内存 |
| 磁盘 | 2 GB | 3 GB+ | 含 BGE 权重、ChromaDB 持久化数据 |

!!! note "Windows 用户注意"
    chromadb 与 sentence-transformers 部分依赖需要 C++ 编译环境。若安装失败，请先安装 [Visual C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)，勾选「使用 C++ 的桌面开发」工作负载。

---

## :package: 依赖安装

### 1. 创建虚拟环境

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Windows (CMD)
.venv\Scripts\activate.bat
# macOS / Linux
source .venv/bin/activate
```

### 2. 升级 pip

```bash
# chromadb 等包依赖较新 pip 的依赖解析器
python -m pip install --upgrade pip setuptools wheel
```

### 3. 安装项目依赖

```bash
pip install -r requirements.txt
```

??? info "关键依赖一览"

    | 依赖 | 用途 |
    |------|------|
    | `fastapi` + `uvicorn` | Web 框架与 ASGI 服务器 |
    | `langgraph` | 多 Agent 状态机编排（不可用时自动降级同步编排） |
    | `openai` | OpenAI 兼容 SDK，对接 DeepSeek / 千问 / 豆包等 |
    | `chromadb` | 向量数据库（hnsw:space=cosine） |
    | `sentence-transformers` | BGE 嵌入与 Reranker 模型加载 |
    | `rank-bm25` | BM25 关键词检索 |
    | `langfuse` | LLM 链路追踪与 Prompt 版本管理（未配置自动降级） |
    | `pydantic-settings` | 配置管理，从 .env 与环境变量加载 |
    | `redis` | 会话持久化与缓存（可选，降级内存队列） |
    | `unstructured` + `PyMuPDF` + `python-docx` + `beautifulsoup4` | 多格式文档解析 |

---

## :material-download: BGE 模型下载

系统使用 `BAAI/bge-large-zh-v1.5`（1024 维）作为嵌入模型，`BAAI/bge-reranker-base` 作为重排序模型。首次启动时会自动下载，但国内网络环境下可能较慢或失败。

### 自动下载（默认）

首次调用检索或入库时，`EmbeddingService` 会按以下顺序尝试加载：

1. **HuggingFace 主源**（`https://huggingface.co`）
2. **国内镜像源**（`https://hf-mirror.com`，由 `HF_MIRROR_URL` 配置）
3. **本地缓存目录**（`./models/bge-large-zh`，由 `EMBEDDING_LOCAL_CACHE_DIR` 配置）
4. **hash fallback**（确定性降级，仅保证流程可跑通，检索质量无保障）

!!! warning "下载失败的降级影响"
    若四个加载源全部失败，系统降级到 hash fallback 向量化：用 `hashlib.sha256` 生成确定性 1024 维向量。**此模式下语义检索失效**，仅保证流程不中断。生产环境务必确保 BGE 模型可用。

### 手动下载（推荐国内用户）

```python
# 提前下载 BGE 模型到本地缓存目录
from sentence_transformers import SentenceTransformer

# 下载嵌入模型（约 1.3 GB）
model = SentenceTransformer("BAAI/bge-large-zh-v1.5")
model.save("./models/bge-large-zh")

# 下载重排序模型（约 400 MB）
from sentence_transformers import CrossEncoder
reranker = CrossEncoder("BAAI/bge-reranker-base")
reranker.save("./models/bge-reranker-base")
```

### 使用国内镜像

```bash
# 方式一：设置环境变量（推荐）
export HF_ENDPOINT=https://hf-mirror.com

# Windows PowerShell
$env:HF_ENDPOINT = "https://hf-mirror.com"

# 方式二：在 .env 中配置（项目已内置回退到该镜像）
# HF_MIRROR_URL=https://hf-mirror.com  （默认已填）
```

设置镜像后，自动下载会优先走 `hf-mirror.com`，国内速度显著提升。

??? tip "离线环境部署"
    在有网机器上执行手动下载脚本，把 `./models/bge-large-zh` 整个目录拷贝到离线机器的同路径下。系统检测到本地缓存存在会直接加载，无需联网。

---

## :material-cog: 环境变量配置

```bash
# 复制配置模板
cp .env.example .env
```

`.env.example` 已包含全部配置项及注释，默认值均为开箱即用的安全值。最小化使用只需关注以下几项：

```bash
# === 最小配置（mock 模式，开箱即用）===
APP_PORT=8000
DEBUG=True
API_KEY=               # 留空 = 免鉴权
LLM_API_KEY=           # 留空 = mock 模式
BUSINESS_ADAPTER_MODE=mock  # mock 业务系统

# === 接入真实 LLM（可选）===
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

!!! info "完整配置说明"
    全部配置项的详细说明、默认值、影响范围请见 [配置说明](configuration.md)。

---

## :material-server-plus: 可选依赖

以下组件非必需，但启用后可增强系统能力。未安装或未配置时系统自动降级，不影响主链路。

### Redis（会话持久化）

```bash
pip install redis
```

```bash
# .env 配置
REDIS_URL=redis://localhost:6379/0
```

| 场景 | 无 Redis（默认） | 有 Redis |
|------|-------------------|----------|
| 会话存储 | 进程内字典，重启丢失 | 跨进程持久化 |
| 缓存 | 内存字典 | 分布式缓存 |
| 队列 | 内存队列（降级） | Redis Pub/Sub |

??? note "何时需要 Redis"
    - **多实例部署**：多个 FastAPI 进程需共享会话状态
    - **会话持久化**：服务重启后保留对话历史
    - **生产环境**：建议启用以保障可靠性

    单机开发无需 Redis，内存模式完全够用。

### Elasticsearch（全文检索增强）

```bash
pip install elasticsearch
```

```bash
# .env 配置
ELASTICSEARCH_URL=http://localhost:9200
```

BM25 已基于 `rank-bm25` 实现内置关键词检索。Elasticsearch 用于超大规模知识库（百万级 chunk）场景，提供更高效的分布式全文检索能力。

### 真实业务系统

```bash
# .env 配置
BUSINESS_ADAPTER_MODE=http
BUSINESS_API_BASE_URL=https://your-business-api.com
BUSINESS_API_KEY=your-business-api-key
BUSINESS_API_TIMEOUT=10
```

默认 `mock` 模式使用内存 mock 业务系统（订单/会员/退换货/账户）。切换为 `http` 模式后，BusinessAgent 会调用真实业务系统 REST API。

!!! warning "http 模式降级"
    `BUSINESS_ADAPTER_MODE=http` 但 `BUSINESS_API_BASE_URL` 为空时，系统自动降级到 mock 模式并打印告警日志。

---

## :material-check-network: 启动验证

### 1. 启动服务

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. 健康检查

```bash
curl http://localhost:8000/api/v1/health
```

返回 `200` 即代表服务正常：

```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### 3. 组件健康检查

```bash
curl http://localhost:8000/api/v1/observability/health
```

返回各组件状态，便于确认 LLM、向量库、Redis、磁盘是否就绪：

```json
{
  "llm": "mock",
  "vectorstore": "healthy",
  "redis": "degraded",
  "disk": "ok"
}
```

### 4. 对话验证

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好"}'
```

mock 模式下应返回闲聊问候语。配置真实 LLM 后返回 LLM 生成的回复。

---

## :material-alert-circle: 常见安装问题

### chromadb 安装失败

**现象**：`pip install chromadb` 报编译错误或依赖冲突。

??? "解决方案"

    === "升级 pip（首选）"

        ```bash
        python -m pip install --upgrade pip setuptools wheel
        pip install chromadb
        ```

    === "Windows 安装 Build Tools"

        1. 下载 [Visual C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
        2. 安装时勾选「使用 C++ 的桌面开发」
        3. 重启终端后重新 `pip install chromadb`

    === "使用预编译 wheel"

        ```bash
        # 强制使用预编译 wheel，跳过源码编译
        pip install chromadb --only-binary :all:
        ```

### sentence-transformers 下载慢

**现象**：首次启动卡在模型下载，或超时失败。

??? "解决方案"

    ```bash
    # 方案一：设置 HuggingFace 镜像（推荐）
    export HF_ENDPOINT=https://hf-mirror.com
    # Windows PowerShell
    $env:HF_ENDPOINT = "https://hf-mirror.com"

    # 方案二：提前手动下载（见上文「手动下载」章节）
    # 方案三：系统已内置镜像回退，无需额外操作也能自动尝试 hf-mirror.com
    ```

    下载失败不会阻塞启动——系统自动降级到 hash fallback 向量化，主链路可跑通（但语义检索质量下降）。

### LangGraph 不可用

**现象**：日志出现 `LangGraph 构建失败，降级到同步编排器`。

??? "解决方案"

    **无需手动处理**。这是预期行为：LangGraph 不可用或构建失败时，系统自动降级到 `_SynchOrchestrator` 同步编排器，复用相同的节点函数（intent_node / agent_node / dialog_node / escalate_node），行为与 LangGraph 版本完全一致，仅缺少图结构调度。

    如需启用 LangGraph：

    ```bash
    pip install langgraph
    ```

    安装后重启服务，日志应显示 `LangGraph 编排器已构建成功`。

### 端口被占用

**现象**：`OSError: [Errno 48] Address already in use`。

??? "解决方案"

    ```bash
    # 方案一：换端口
    python -m uvicorn app.main:app --port 8001

    # 方案二：修改 .env
    # APP_PORT=8001

    # 方案三：释放占用端口（Linux/macOS）
    lsof -i :8000      # 查看占用进程
    kill -9 <PID>      # 结束进程
    ```

### 内存不足（OOM）

**现象**：启动或检索时进程被 kill，日志无明确错误。

??? "解决方案"

    ```bash
    # 减小 embedding 批大小，降低峰值内存
    # .env 配置
    EMBEDDING_BATCH_SIZE=16   # 默认 32，内存紧张时调小

    # 减小召回数量，降低单次检索内存占用
    VECTOR_TOP_K=15
    BM25_TOP_K=15
    ```

    生产环境建议内存 ≥ 8 GB，并启用 Redis 分担缓存压力。

### import 报错 ModuleNotFoundError

**现象**：`ModuleNotFoundError: No module named 'app'`。

??? "解决方案"

    确保在**项目根目录**（含 `app/` 目录的层级）执行命令：

    ```bash
    # 检查当前目录
    ls app/main.py    # 应能看到该文件

    # 必须在项目根目录启动
    cd /path/to/Intelligent-customer-service-system-integrated-with-enterprise-knowledge-base
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
    ```

---

## :material-arrow-right: 下一步

- [配置说明](configuration.md) — 了解全部配置项
- [总体架构](architecture.md) — 理解系统设计
- [一分钟跑起来](quick-start.md) — 快速验证安装结果
