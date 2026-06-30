"""知识质量校验。

提供入库前的质量检查能力，作为可选环节挂在 pipeline 上：
- check_duplicates：基于 cosine 相似度检测重复片段（返回报告而非直接过滤）
- check_term_consistency：术语一致性检查，对照术语表检测不同写法
- check_sensitive_words：敏感词检查，命中片段上报
- run_quality_check：聚合三项检查，返回 QualityReport

降级策略：
任一环节失败不影响其他环节与主入库流程，错误信息记录到报告的 error 字段。
术语表与敏感词文件默认为空，保证开箱即用。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.knowledge.embeddings import get_embedding_service
from app.knowledge.metadata import cosine_similarity
from app.schemas.knowledge import TextChunk
from app.schemas.quality import QualityIssue, QualityReport

logger = get_logger("app.knowledge.quality")

# 术语表与敏感词文件相对本模块的位置
_TERM_DICT_PATH = Path(__file__).parent / "term_dict.json"
_SENSITIVE_WORDS_PATH = Path(__file__).parent / "sensitive_words.txt"

# 内置默认术语表：覆盖繁简等常见异写，保证开箱即用的基础检测能力
_DEFAULT_TERM_DICT: Dict[str, List[str]] = {
    "客服": ["客務"],
}

# 内置默认敏感词：覆盖常见样例，便于默认配置下即可验证流程
_DEFAULT_SENSITIVE_WORDS: List[str] = [
    "违禁词样例",
]


def _load_term_dict() -> Dict[str, List[str]]:
    """加载术语表，结构为 {canonical: [alias1, alias2, ...]}。

    合并内置默认术语表与文件术语表，文件别名追加到默认别名后（去重）。
    读取失败或格式异常时降级为默认术语表，保证开箱即用的基础检测能力。
    """
    # 以内置默认术语表为基础，保证开箱即用
    merged: Dict[str, List[str]] = {
        canonical: list(aliases) for canonical, aliases in _DEFAULT_TERM_DICT.items()
    }
    try:
        if _TERM_DICT_PATH.exists():
            raw = _TERM_DICT_PATH.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            terms = data.get("terms", {}) or {}
            for canonical, aliases in terms.items():
                # 规范化：确保 value 为列表
                alias_list = list(aliases) if isinstance(aliases, list) else [str(aliases)]
                existing = merged.setdefault(str(canonical), [])
                for alias in alias_list:
                    if alias not in existing:
                        existing.append(alias)
    except Exception as exc:
        logger.warning("术语表加载失败，仅使用默认术语表：%s", exc)
    return merged


def _load_sensitive_words() -> List[str]:
    """加载敏感词列表，合并默认敏感词、文件敏感词与 settings.SENSITIVE_WORDS 配置。

    - 默认敏感词：内置常见样例，保证开箱即用的基础检测能力
    - 文件敏感词：每行一个词，空行与注释行（# 开头）忽略
    - settings.SENSITIVE_WORDS：逗号分隔字符串，便于运行时动态配置
    去重后返回，避免重复检测。
    """
    # 以内置默认敏感词为基础，保证开箱即用
    words: List[str] = list(_DEFAULT_SENSITIVE_WORDS)
    try:
        if _SENSITIVE_WORDS_PATH.exists():
            lines = _SENSITIVE_WORDS_PATH.read_text(encoding="utf-8").splitlines()
            for line in lines:
                word = line.strip()
                if word and not word.startswith("#") and word not in words:
                    words.append(word)
    except Exception as exc:
        logger.warning("敏感词文件加载失败，仅使用默认与配置敏感词：%s", exc)

    # 合并 settings.SENSITIVE_WORDS（逗号分隔），便于运行时动态配置
    try:
        configured = get_settings().SENSITIVE_WORDS
        if configured:
            for word in configured.split(","):
                word = word.strip()
                if word and word not in words:
                    words.append(word)
    except Exception as exc:
        logger.warning("settings.SENSITIVE_WORDS 加载失败：%s", exc)
    return words


def check_duplicates(
    chunks: List[TextChunk],
    existing_embeddings: List[List[float]],
) -> List[QualityIssue]:
    """基于 cosine 相似度检测重复片段。

    将 chunks 向量化后与 existing_embeddings 逐一比对，
    相似度高于阈值（Settings.QUALITY_DEDUP_THRESHOLD）视为重复。
    返回重复问题列表，不直接过滤，由调用方决定处理方式。
    """
    if not chunks or not existing_embeddings:
        return []

    issues: List[QualityIssue] = []
    settings = get_settings()
    threshold = settings.QUALITY_DEDUP_THRESHOLD
    try:
        embedding_service = get_embedding_service()
        new_embeddings = embedding_service.embed_texts([chunk.text for chunk in chunks])
    except Exception as exc:
        logger.warning("重复检测向量化失败：%s", exc)
        return issues

    for index, embedding in enumerate(new_embeddings):
        for existing in existing_embeddings:
            similarity = cosine_similarity(embedding, existing)
            if similarity >= threshold:
                issues.append(
                    QualityIssue(
                        chunk_index=index,
                        issue_type="duplicate",
                        detail=f"与已有内容相似度 {similarity:.3f} 超过阈值 {threshold}",
                        snippet=chunks[index].text[:80],
                    )
                )
                break  # 一个 chunk 仅报告一次重复
    return issues


def check_internal_duplicates(
    chunks: List[TextChunk],
    embeddings: List[List[float]],
) -> List[QualityIssue]:
    """库内已有 chunks 两两重复检测。

    将 chunks 向量化后与提供的 embeddings 逐一比对（跳过自身索引），
    相似度高于阈值（Settings.QUALITY_DEDUP_THRESHOLD）视为内部重复。
    用于发现库内已入库内容之间的重复对，每个 chunk 至多报告一次重复。
    """
    if not chunks or not embeddings or len(chunks) != len(embeddings):
        return []

    issues: List[QualityIssue] = []
    settings = get_settings()
    threshold = settings.QUALITY_DEDUP_THRESHOLD
    try:
        embedding_service = get_embedding_service()
        new_embeddings = embedding_service.embed_texts([chunk.text for chunk in chunks])
    except Exception as exc:
        logger.warning("库内重复检测向量化失败：%s", exc)
        return issues

    for index, embedding in enumerate(new_embeddings):
        for other_index, existing in enumerate(embeddings):
            # 跳过自身索引，避免 chunk 与自身比对必然重复
            if other_index == index:
                continue
            similarity = cosine_similarity(embedding, existing)
            if similarity >= threshold:
                issues.append(
                    QualityIssue(
                        chunk_index=index,
                        issue_type="duplicate",
                        detail=f"与库内 chunk#{other_index} 相似度 {similarity:.3f} 超过阈值 {threshold}",
                        snippet=chunks[index].text[:80],
                    )
                )
                break  # 一个 chunk 仅报告一次重复
    return issues


def check_term_consistency(chunks: List[TextChunk]) -> List[QualityIssue]:
    """术语一致性检查。

    对照术语表（{canonical: [aliases]}），检测 chunk 中使用了非标准别名的情况，
    提示统一为 canonical 写法。空术语表时返回空列表。
    """
    term_dict = _load_term_dict()
    if not term_dict or not chunks:
        return []

    issues: List[QualityIssue] = []
    for index, chunk in enumerate(chunks):
        text = chunk.text
        for canonical, aliases in term_dict.items():
            hit_aliases = [alias for alias in aliases if alias and alias in text]
            if hit_aliases and canonical not in text:
                # 使用了别名但未使用标准术语，提示统一
                issues.append(
                    QualityIssue(
                        chunk_index=index,
                        issue_type="term_inconsistency",
                        detail=f"建议统一术语：{hit_aliases} -> {canonical}",
                        snippet=text[:80],
                    )
                )
    return issues


def check_sensitive_words(chunks: List[TextChunk]) -> List[QualityIssue]:
    """敏感词检查。

    从 sensitive_words.txt 加载敏感词列表，检测 chunk 中命中的敏感词。
    空列表时返回空结果，保证默认配置下无干扰。
    """
    sensitive_words = _load_sensitive_words()
    if not sensitive_words or not chunks:
        return []

    issues: List[QualityIssue] = []
    for index, chunk in enumerate(chunks):
        text = chunk.text
        hit_words = [word for word in sensitive_words if word and word in text]
        if hit_words:
            issues.append(
                QualityIssue(
                    chunk_index=index,
                    issue_type="sensitive_word",
                    detail=f"命中敏感词：{hit_words}",
                    snippet=text[:80],
                )
            )
    return issues


def run_quality_check(
    chunks: List[TextChunk],
    existing_embeddings: Optional[List[List[float]]] = None,
) -> QualityReport:
    """聚合三项质量检查，返回 QualityReport。

    任一环节异常被捕获并记录到 error 字段，不影响其他环节。
    existing_embeddings 为 None 时跳过重复检测（如首次入库无已有内容）。
    """
    if not chunks:
        return QualityReport(total_chunks=0, summary="无 chunk 需要检查")

    total = len(chunks)
    duplicate_issues: List[QualityIssue] = []
    term_issues: List[QualityIssue] = []
    sensitive_issues: List[QualityIssue] = []
    error: Optional[str] = None

    # 重复检测：existing_embeddings 为 None 时跳过
    if existing_embeddings is not None:
        try:
            duplicate_issues = check_duplicates(chunks, existing_embeddings)
        except Exception as exc:
            logger.warning("重复检测异常：%s", exc)
            error = f"重复检测异常：{exc}"

    # 术语一致性检查
    try:
        term_issues = check_term_consistency(chunks)
    except Exception as exc:
        logger.warning("术语一致性检查异常：%s", exc)
        error = f"{error}; 术语一致性检查异常：{exc}" if error else f"术语一致性检查异常：{exc}"

    # 敏感词检查
    try:
        sensitive_issues = check_sensitive_words(chunks)
    except Exception as exc:
        logger.warning("敏感词检查异常：%s", exc)
        error = f"{error}; 敏感词检查异常：{exc}" if error else f"敏感词检查异常：{exc}"

    issue_count = len(duplicate_issues) + len(term_issues) + len(sensitive_issues)
    summary = f"共检查 {total} 个 chunk，发现 {issue_count} 个问题（重复 {len(duplicate_issues)}、术语 {len(term_issues)}、敏感词 {len(sensitive_issues)}）"

    # 重复率告警：仅在实际执行了重复检测时评估，超过阈值在 summary 追加告警
    if total > 0 and existing_embeddings is not None:
        dup_ratio = len(duplicate_issues) / total
        alert_ratio = get_settings().QUALITY_DEDUP_ALERT_RATIO
        if dup_ratio > alert_ratio:
            summary += f"；重复率 {dup_ratio:.1%} 超过告警阈值 {alert_ratio:.1%}"

    return QualityReport(
        total_chunks=total,
        duplicate_issues=duplicate_issues,
        term_issues=term_issues,
        sensitive_issues=sensitive_issues,
        summary=summary,
        error=error,
    )


def run_quality_check_on_existing(
    chunks: List[TextChunk],
    embeddings: List[List[float]],
) -> QualityReport:
    """对已入库内容执行质量巡检。

    与 run_quality_check 的区别：embeddings 必传且非空，
    重复检测基于库内已有向量两两比对，发现内部重复片段。
    """
    if not chunks:
        return QualityReport(total_chunks=0, summary="无已入库内容可巡检")

    total = len(chunks)
    duplicate_issues: List[QualityIssue] = []
    term_issues: List[QualityIssue] = []
    sensitive_issues: List[QualityIssue] = []
    error: Optional[str] = None

    # 库内两两重复检测：将 chunks 向量化后与已有 embeddings 比对
    try:
        duplicate_issues = check_duplicates(chunks, embeddings)
    except Exception as exc:
        logger.warning("库内重复检测异常：%s", exc)
        error = f"库内重复检测异常：{exc}"

    # 术语一致性检查
    try:
        term_issues = check_term_consistency(chunks)
    except Exception as exc:
        logger.warning("术语一致性检查异常：%s", exc)
        error = f"{error}; 术语一致性检查异常：{exc}" if error else f"术语一致性检查异常：{exc}"

    # 敏感词检查
    try:
        sensitive_issues = check_sensitive_words(chunks)
    except Exception as exc:
        logger.warning("敏感词检查异常：%s", exc)
        error = f"{error}; 敏感词检查异常：{exc}" if error else f"敏感词检查异常：{exc}"

    issue_count = len(duplicate_issues) + len(term_issues) + len(sensitive_issues)
    summary = f"共巡检 {total} 个 chunk，发现 {issue_count} 个问题（重复 {len(duplicate_issues)}、术语 {len(term_issues)}、敏感词 {len(sensitive_issues)}）"

    return QualityReport(
        total_chunks=total,
        duplicate_issues=duplicate_issues,
        term_issues=term_issues,
        sensitive_issues=sensitive_issues,
        summary=summary,
        error=error,
    )