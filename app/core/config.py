"""应用配置加载。

使用 pydantic-settings 从环境变量读取配置，
提供全局 Settings 单例供各模块复用，避免重复读取。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置模型。

    通过 pydantic-settings 自动从 .env 与环境变量加载，
    字段命名与环境变量保持一致，便于运维与部署。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # 应用基础配置
    APP_NAME: str = "Intelligent Customer Service System"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = False

    # 鉴权配置：为空时进入开发免鉴权模式
    API_KEY: str = ""

    # LLM 配置（后续接入 RAG 使用）
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"

    # Embedding 模型配置：默认使用 BGE-large-zh，适合中文语义检索
    EMBEDDING_MODEL: str = "BAAI/bge-large-zh-v1.5"

    # BGE 本地缓存目录：HuggingFace 主源与镜像源均失败时从此目录加载权重，
    # 便于离线/受限网络环境下仍能启用真实语义检索
    EMBEDDING_LOCAL_CACHE_DIR: str = "./models/bge-large-zh"
    # HuggingFace 镜像源：国内网络环境下用作主源回退，提升下载成功率
    HF_MIRROR_URL: str = "https://hf-mirror.com"
    # 模型加载超时秒数：避免单次加载卡住拖垮启动链路
    EMBEDDING_LOAD_TIMEOUT: int = 60

    # ChromaDB 向量库持久化目录
    CHROMA_PERSIST_DIR: str = "./chroma_data"

    # ChromaDB 集合名：用于隔离不同业务的知识库
    CHROMA_COLLECTION_NAME: str = "knowledge_base"

    # 文本切分参数：按 token 友好的字符数控制
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 128

    # 检索相似度阈值：低于该值的召回视为弱相关，过滤掉
    SIMILARITY_THRESHOLD: float = 0.6

    # 入库去重阈值：高于该值视为重复文档，跳过写入
    DEDUP_THRESHOLD: float = 0.95

    # Embedding 批大小：避免大文档一次性加载导致 OOM
    EMBEDDING_BATCH_SIZE: int = 32

    # Redis 连接地址（会话存储、缓存）
    REDIS_URL: str = "redis://localhost:6379/0"

    # Elasticsearch 地址（全文检索）
    ELASTICSEARCH_URL: str = "http://localhost:9200"

    # ===== 混合检索（向量 + BM25 + RRF）配置 =====
    # 单路召回数量：向量与关键词各召回 top-N 后融合，过大易拖慢检索
    VECTOR_TOP_K: int = 25
    BM25_TOP_K: int = 25
    # RRF 融合参数：k 平滑排名差异，权重用于加权 RRF（向量 60% / 关键词 40%）
    RRF_K: int = 60
    RRF_VECTOR_WEIGHT: float = 0.6
    RRF_KEYWORD_WEIGHT: float = 0.4
    # Reranker 参数：取 top-K 进入最终答案
    RERANK_TOP_K: int = 5
    # CrossEncoder 模型名：默认 BGE 系列中文重排序模型
    RERANKER_MODEL: str = "BAAI/bge-reranker-base"

    # ===== 人工客服转接配置 =====
    # 人工服务时间段：[START, END) 24 小时制，超出该区间不因情绪/失败主动转接
    # 但用户主动要求"转人工"时仍会转接，避免阻断用户明确诉求
    WORKING_HOURS_START: int = 9
    WORKING_HOURS_END: int = 18
    # 时区：用于工作时间判断，默认上海时区
    TIMEZONE: str = "Asia/Shanghai"

    # ===== 业务系统适配器配置（Task 15）=====
    # 适配器模式：mock=使用内存 mock；http=调用真实业务系统 REST API
    # 默认 mock 保证开箱即用，真实测试环境切换为 http 即可
    BUSINESS_ADAPTER_MODE: str = "mock"
    # 真实业务系统 API 基址，http 模式必填，留空将自动降级 mock 并告警
    BUSINESS_API_BASE_URL: str = ""
    # 真实业务系统 API Key，http 模式下写入 X-API-Key 请求头鉴权
    BUSINESS_API_KEY: str = ""
    # HTTP 调用超时秒数，避免单次调用长时间挂起拖垮服务
    BUSINESS_API_TIMEOUT: int = 10

    # ===== 知识库管理后台配置（Task 16）=====
    # 是否在入库前执行质量校验，默认关闭避免影响现有性能
    ENABLE_QUALITY_CHECK: bool = False
    # 文档元数据存储文件名（位于 CHROMA_PERSIST_DIR 下）
    DOC_STORE_FILENAME: str = "_doc_store.json"
    # 灰度验证默认样本查询数
    CANARY_DEFAULT_SAMPLE_SIZE: int = 5
    # 灰度验证 Top-K 对比数
    CANARY_TOP_K: int = 3
    # 重复片段判定阈值（cosine 相似度）
    QUALITY_DEDUP_THRESHOLD: float = 0.92
    # 重复率告警阈值：重复 chunk 占比超过该值时在质量报告中告警
    QUALITY_DEDUP_ALERT_RATIO: float = 0.2
    # 敏感词配置：逗号分隔字符串，运行时与 sensitive_words.txt 合并生效
    SENSITIVE_WORDS: str = ""
    # 术语表自定义路径：为空时使用内置 term_dict.json 与默认表
    TERM_DICTIONARY_PATH: str = ""
    # 灰度集合后缀：主集合名 + 后缀构成灰度集合名，用于版本灰度验证
    CANARY_COLLECTION_SUFFIX: str = "_canary"

    @property
    def api_key_configured(self) -> bool:
        """判断是否已配置 API Key，便于鉴权开关控制。"""
        return bool(self.API_KEY)


@lru_cache()
def get_settings() -> Settings:
    """获取全局 Settings 单例。

    使用 lru_cache 保证进程内只创建一次，
    避免重复读取环境变量与 .env 文件。
    """
    return Settings()
