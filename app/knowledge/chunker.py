"""文本语义切分。

按章节标题与段落等自然边界优先切分，
不足 chunk_size 时再用滑动窗口按字符数补足，
每个 chunk 保留页码与章节信息以支持检索回溯。
"""
from __future__ import annotations

import re
from typing import List

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.knowledge import ParsedDocument, ParsedPage, SectionInfo, TextChunk

logger = get_logger("app.knowledge.chunker")

# 段落分隔：空行或 markdown 列表项边界
_PARAGRAPH_SPLIT_PATTERN = re.compile(r"\n\s*\n")
# Markdown 标题正则：用于在文本中识别章节起点
_HEADING_LINE_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$")


class SemanticChunker:
    """语义切分器。

    采用“先按章节、再按段落、最后按字符”的三级降级策略，
    保证切分粒度可控且保留语义完整性。
    """

    def __init__(self, chunk_size: int = 0, chunk_overlap: int = 0) -> None:
        settings = get_settings()
        self.chunk_size = chunk_size or settings.CHUNK_SIZE
        self.chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP

    def chunk_document(self, document: ParsedDocument) -> List[TextChunk]:
        """对整篇文档执行切分，聚合各页结果。"""
        chunks: List[TextChunk] = []
        for page in document.pages:
            chunks.extend(self._chunk_page(page))
        logger.info("文档 %s 切分完成，共 %d 个 chunk", document.source, len(chunks))
        return chunks

    def _chunk_page(self, page: ParsedPage) -> List[TextChunk]:
        """对单页执行切分，并维护当前章节上下文。"""
        if not page.text.strip():
            return []

        # 优先按章节切：根据 sections 的标题位置将页面拆成若干段
        section_blocks = self._split_by_sections(page.text, page.sections)
        chunks: List[TextChunk] = []
        # 章节栈：保留层级路径，便于拼出 "章节1/子章节2"
        section_stack: List[SectionInfo] = []
        for section_path, block_text in section_blocks:
            if section_path:
                # 更新章节栈：丢弃比当前层级更深的历史记录
                current_level = section_path[-1].level
                section_stack = [s for s in section_stack if s.level < current_level]
                section_stack.append(section_path[-1])

            current_section_title = " / ".join(s.title for s in section_stack)
            chunks.extend(
                self._split_block(block_text, page.page_number, current_section_title)
            )
        return chunks

    def _split_by_sections(self, text: str, sections: List[SectionInfo]) -> List[tuple]:
        """按标题位置把文本拆成 (章节路径, 文本块) 列表。

        若文档未提供 sections 信息，则直接返回整段文本，
        由后续段落/字符级切分兜底。
        """
        if not sections:
            return [([], text)]

        # 在文本中定位每个标题首次出现位置作为分界点
        boundaries: List[tuple] = []
        cursor = 0
        for section in sections:
            # 用行级匹配避免误命中正文中的相同字串
            match = _HEADING_LINE_PATTERN.search(text, cursor)
            if match and match.group(2).strip() == section.title:
                boundaries.append((section, match.start()))
                cursor = match.end()
            else:
                # 退化：直接按标题字串搜索，找不到则跳过
                idx = text.find(section.title, cursor)
                if idx >= 0:
                    boundaries.append((section, idx))
                    cursor = idx + len(section.title)

        if not boundaries:
            return [([], text)]

        # 构造 (section_path, block_text)：标题之前的文本归入前一段
        result: List[tuple] = []
        prev_pos = 0
        prev_section: List[SectionInfo] = []
        for section, pos in boundaries:
            if pos > prev_pos:
                block = text[prev_pos:pos].strip()
                if block:
                    result.append((prev_section, block))
            prev_pos = pos
            prev_section = [section]
        # 末尾段
        tail = text[prev_pos:].strip()
        if tail:
            result.append((prev_section, tail))
        return result

    def _split_block(self, text: str, page_number: int, section: str) -> List[TextChunk]:
        """对一段文本执行段落级 + 字符级切分。"""
        paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_PATTERN.split(text) if p.strip()]
        if not paragraphs:
            return []

        chunks: List[TextChunk] = []
        buffer = ""
        for paragraph in paragraphs:
            # 段落本身超长时，先用字符级滑动窗口单独切分，避免单段越界
            if len(paragraph) > self.chunk_size:
                if buffer:
                    chunks.append(self._make_chunk(buffer, page_number, section))
                    buffer = ""
                chunks.extend(self._split_by_chars(paragraph, page_number, section))
                continue

            # 累积段落，超出 chunk_size 则落盘
            candidate = f"{buffer}\n{paragraph}".strip() if buffer else paragraph
            if len(candidate) > self.chunk_size and buffer:
                chunks.append(self._make_chunk(buffer, page_number, section))
                # overlap：保留末尾字符，承接上下文语义
                buffer = self._tail_overlap(buffer) + paragraph
            else:
                buffer = candidate

        if buffer.strip():
            chunks.append(self._make_chunk(buffer, page_number, section))
        return chunks

    def _split_by_chars(self, text: str, page_number: int, section: str) -> List[TextChunk]:
        """字符级滑动窗口兜底切分：保证超长段落仍可入库。"""
        chunks: List[TextChunk] = []
        if not text:
            return chunks
        step = max(self.chunk_size - self.chunk_overlap, 1)
        for start in range(0, len(text), step):
            piece = text[start : start + self.chunk_size]
            if not piece.strip():
                continue
            chunks.append(self._make_chunk(piece, page_number, section))
            # 已到末尾，避免多余循环
            if start + self.chunk_size >= len(text):
                break
        return chunks

    def _tail_overlap(self, text: str) -> str:
        """取末尾 overlap 长度字符作为上下文承接。"""
        if self.chunk_overlap <= 0 or len(text) <= self.chunk_overlap:
            return ""
        return text[-self.chunk_overlap :]

    def _make_chunk(self, text: str, page_number: int, section: str) -> TextChunk:
        return TextChunk(text=text, page_number=page_number, section=section)


def chunk_document(document: ParsedDocument) -> List[TextChunk]:
    """便捷入口：使用默认配置切分文档。"""
    return SemanticChunker().chunk_document(document)
