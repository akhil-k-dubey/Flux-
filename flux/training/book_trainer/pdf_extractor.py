"""
flux/training/book_trainer/pdf_extractor.py

Raw PDF → structured page text with section/chapter awareness.
Uses PyMuPDF for high-fidelity extraction including math-heavy layouts.

The key principle: preserve structure signals (font size, bold, spacing)
because "Definition 2.3" is only a definition if it LOOKS different from
surrounding text. Losing structure = losing the ability to extract strictly.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pymupdf  # replaces import fitz (deprecated API warning)

log = logging.getLogger(__name__)


@dataclass
class PageBlock:
    """A single coherent text block from a PDF page."""
    page_num:    int
    block_num:   int
    text:        str
    font_size:   float       # dominant font size in block
    is_bold:     bool
    is_heading:  bool        # larger than body text
    bbox:        tuple       # (x0, y0, x1, y1)


@dataclass
class BookPage:
    """A single page of extracted content."""
    page_num:    int
    raw_text:    str
    blocks:      list[PageBlock] = field(default_factory=list)
    chapter:     str | None = None
    section:     str | None = None


@dataclass
class ExtractedBook:
    """Full extraction result from one PDF."""
    title:       str
    path:        str
    domain:      str
    pages:       list[BookPage]
    page_count:  int
    toc:         list[tuple[int, str, int]]   # (level, title, page)


class PDFExtractor:
    """
    Extracts structured content from academic PDFs.

    Preserves:
      - Font size hierarchy (headings vs body vs captions)
      - Bold text (often marks Definition/Theorem keywords)
      - Chapter/section titles (from TOC or font analysis)
      - Raw text per page (for LTC training corpus)
    """

    # Minimum font size ratio relative to median body size to be considered heading
    _HEADING_RATIO = 1.10
    # Minimum block text length to bother processing
    _MIN_BLOCK_LEN = 20

    def extract(self, pdf_path: str | Path, domain: str = "unknown") -> ExtractedBook:
        """
        Extract full book content.

        Parameters
        ----------
        pdf_path : path to PDF file
        domain   : domain label (thermodynamics, machine_learning, etc.)

        Returns
        -------
        ExtractedBook with all pages and structure
        """
        path = Path(pdf_path)
        doc  = pymupdf.open(str(path))

        # Extract TOC
        toc = doc.get_toc()   # [(level, title, page), ...]

        # Estimate body font size from first 20 pages
        body_font_size = self._estimate_body_font_size(doc)
        heading_threshold = body_font_size * self._HEADING_RATIO

        pages: list[BookPage] = []
        current_chapter = None
        current_section = None

        for page_num in range(len(doc)):
            try:
                page_obj = doc[page_num]
            except Exception as e:
                log.warning("PDFExtractor: skipping corrupted page %d in %s: %s",
                            page_num, path.name, e)
                continue

            # Process page — wrap entirely so partial corruption skips cleanly
            try:
                raw_text = page_obj.get_text("text")
                blocks   = self._extract_blocks(
                    page_obj, page_num, heading_threshold, body_font_size
                )
            except Exception as e:
                log.warning("PDFExtractor: error reading page %d content in %s: %s",
                            page_num, path.name, e)
                continue

            # Update chapter/section tracking from TOC
            for level, title, toc_page in toc:
                if toc_page == page_num + 1:   # TOC is 1-indexed
                    if level == 1:
                        current_chapter = title.strip()
                        current_section = None
                    elif level == 2:
                        current_section = title.strip()

            # Also detect chapter/section from headings in page
            if not current_chapter:
                for block in blocks:
                    if block.is_heading and block.text.strip():
                        m = re.match(
                            r"^(chapter|part)\s+\d+[:\.\s]+(.+)",
                            block.text.strip(), re.I
                        )
                        if m:
                            current_chapter = m.group(2).strip()

            pages.append(BookPage(
                page_num = page_num,
                raw_text = raw_text,
                blocks   = blocks,
                chapter  = current_chapter,
                section  = current_section,
            ))

        doc.close()
        log.info(
            "PDFExtractor: %s → %d pages, body_font=%.1f",
            path.name, len(pages), body_font_size
        )
        return ExtractedBook(
            title      = path.stem,
            path       = str(path),
            domain     = domain,
            pages      = pages,
            page_count = len(pages),
            toc        = toc,
        )

    def iter_text_chunks(self, book: ExtractedBook, chunk_size: int = 512) -> Iterator[str]:
        """
        Yield overlapping text chunks for LTC training.
        Chunks respect sentence boundaries.
        """
        buffer = ""
        for page in book.pages:
            buffer += " " + page.raw_text
            while len(buffer) >= chunk_size:
                # Find sentence boundary near chunk_size
                cutoff = chunk_size
                for punct in (". ", ".\n", "! ", "? "):
                    idx = buffer.rfind(punct, 0, chunk_size + 100)
                    if idx > 0:
                        cutoff = idx + len(punct)
                        break
                yield buffer[:cutoff].strip()
                buffer = buffer[cutoff - 128:]   # 128-char overlap
        if buffer.strip():
            yield buffer.strip()

    # ── Private ────────────────────────────────────────────────────────────────

    def _estimate_body_font_size(self, doc: pymupdf.Document) -> float:
        """Estimate the dominant body font size from the first 20 pages."""
        sizes: list[float] = []
        for page_num in range(min(20, len(doc))):
            page = doc[page_num]
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:   # text block
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        size = span.get("size", 0)
                        if 8 <= size <= 16:   # reasonable body text range
                            sizes.append(size)

        if not sizes:
            return 11.0   # safe default

        # Use median
        sizes.sort()
        return sizes[len(sizes) // 2]

    def _extract_blocks(
        self,
        page: pymupdf.Page,
        page_num: int,
        heading_threshold: float,
        body_font_size: float,
    ) -> list[PageBlock]:
        """Extract structured text blocks from a single page."""
        blocks: list[PageBlock] = []

        raw_blocks = page.get_text("dict")["blocks"]
        for bidx, block in enumerate(raw_blocks):
            if block.get("type") != 0:
                continue   # skip image blocks

            # Collect all text and font info
            texts: list[str] = []
            sizes: list[float] = []
            bold_count = 0
            span_count = 0

            for line in block.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    size = span.get("size", body_font_size)
                    flags = span.get("flags", 0)
                    is_bold_span = bool(flags & 2**4)   # bold flag in PDF flags bitmask
                    line_text += text
                    sizes.append(size)
                    if is_bold_span:
                        bold_count += 1
                    span_count += 1
                if line_text.strip():
                    texts.append(line_text)

            full_text = "\n".join(texts).strip()
            if len(full_text) < self._MIN_BLOCK_LEN:
                continue

            dom_size  = sizes[len(sizes) // 2] if sizes else body_font_size
            is_bold   = (bold_count / max(span_count, 1)) > 0.5
            is_head   = dom_size >= heading_threshold or (
                is_bold and len(full_text) < 120 and "\n" not in full_text.strip()
            )

            bbox = tuple(block.get("bbox", (0, 0, 0, 0)))
            blocks.append(PageBlock(
                page_num   = page_num,
                block_num  = bidx,
                text       = full_text,
                font_size  = dom_size,
                is_bold    = is_bold,
                is_heading = is_head,
                bbox       = bbox,
            ))

        return blocks
