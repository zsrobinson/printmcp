"""Offline Markdown to Platypus: only renderer-owned markup reaches Paragraph."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
from xml.sax.saxutils import escape
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token
from markdown_it.tree import SyntaxTreeNode
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError
from reportlab.platypus import (
    Flowable, HRFlowable, ListFlowable, ListItem, Paragraph, Preformatted,
    SimpleDocTemplate, Table, TableStyle,
)

_FONT_LOCK = threading.Lock()
_FONT_DIR = Path('/usr/share/fonts/truetype/dejavu')


def _font(path: str, fallback: str, role: str = 'normal') -> str:
    name = 'Markdown-' + hashlib.sha256((path + role).encode()).hexdigest()[:16]
    with _FONT_LOCK:
        if name in pdfmetrics.getRegisteredFontNames():
            return name
        try:
            pdfmetrics.registerFont(TTFont(name, path))
        except (OSError, ValueError, TTFError):
            return fallback
    return name


def _fonts() -> tuple[str, str]:
    body_path = os.environ.get('PRINT_BODY_FONT', str(_FONT_DIR / 'DejaVuSans.ttf'))
    body = _font(body_path, 'Helvetica')
    variants = []
    for suffix, file, fallback in (
        ('BOLD', 'DejaVuSans-Bold.ttf', 'Helvetica-Bold'),
        ('ITALIC', 'DejaVuSans-Oblique.ttf', 'Helvetica-Oblique'),
        ('BOLD_ITALIC', 'DejaVuSans-BoldOblique.ttf', 'Helvetica-BoldOblique'),
    ):
        default = (str(_FONT_DIR / file)
                   if body_path == str(_FONT_DIR / 'DejaVuSans.ttf') else body_path)
        # Distinct names matter even when faces share a file: ReportLab's reverse
        # family mapping otherwise treats the normal face as bold-italic.
        substitute = _font(body_path, fallback, suffix)
        variants.append(_font(os.environ.get('PRINT_BODY_FONT_' + suffix, default),
                              substitute, suffix))
    with _FONT_LOCK:
        pdfmetrics.registerFontFamily(body, normal=body, bold=variants[0],
                                      italic=variants[1], boldItalic=variants[2])
    mono = _font(os.environ.get('PRINT_FONT', str(_FONT_DIR / 'DejaVuSansMono.ttf')), 'Courier')
    return body, mono


def _inline(tokens: list[Token], mono: str) -> str:
    parts = []
    for token in tokens:
        if token.type in ('text', 'html_inline'):
            parts.append(escape(token.content))
        elif token.type == 'image':
            # Ignore src/title entirely; even nested alt markup stays literal.
            parts.append(escape(token.content))
        elif token.type == 'code_inline':
            parts.append(f'<font name="{mono}">{escape(token.content)}</font>')
        elif token.type in ('strong_open', 'strong_close', 'em_open', 'em_close'):
            tag = 'b' if token.type.startswith('strong') else 'i'
            parts.append(f'<{"/" if token.nesting == -1 else ""}{tag}>')
        elif token.type == 'hardbreak':
            parts.append('<br/>')
        elif token.type == 'softbreak':
            parts.append(' ')
        # Links deliberately print only their visible label, never their URL.
    return ''.join(parts)


def markdown_story(content: str, width: float) -> list[Flowable]:
    """Build splittable flowables; HTML and all remote resources are disabled."""
    body, mono = _fonts()
    root = SyntaxTreeNode(MarkdownIt('commonmark', {'html': False}).enable('table').parse(content))

    def blocks(node: SyntaxTreeNode, available: float, indent: int = 0) -> list[Flowable]:
        story: list[Flowable] = []
        style = ParagraphStyle('Body', fontName=body, fontSize=10, leading=15,
                               spaceAfter=8, leftIndent=indent, rightIndent=indent,
                               splitLongWords=True)
        for child in node.children:
            kind = child.type
            if kind in ('paragraph', 'heading'):
                current = style
                if kind == 'heading':
                    level = int(child.tag[1])
                    current = ParagraphStyle('Heading', parent=style, fontSize=22 - 2 * level,
                                             leading=27 - 2 * level, spaceBefore=10,
                                             keepWithNext=True)
                inline = ''.join(_inline(n.token.children or [], mono)
                                 for n in child.children if n.token is not None)
                if kind == 'heading':
                    inline = '<b>' + inline + '</b>'
                story.append(Paragraph(inline, current))
            elif kind in ('bullet_list', 'ordered_list'):
                items = [ListItem(blocks(item, available - 20, indent)) for item in child.children]
                start = int(child.attrGet('start') or 1) if kind == 'ordered_list' else 'bulletchar'
                # ReportLab accepts ListItem, omitted by types-reportlab.
                story.append(ListFlowable(items,  # type: ignore[arg-type]
                                          bulletType='1' if kind == 'ordered_list' else 'bullet',
                                          start=start, leftIndent=20, bulletFontName=body,
                                          bulletFontSize=10, spaceAfter=8))
            elif kind == 'blockquote':
                story.extend(blocks(child, available - 24, indent + 12))
            elif kind in ('fence', 'code_block'):
                code_style = ParagraphStyle('Code', parent=style, fontName=mono, fontSize=9,
                                            leading=12, backColor=colors.HexColor('#f3f4f6'))
                columns = max(1, int((available - 2 * indent) / pdfmetrics.stringWidth('M', mono, 9)))
                story.append(Preformatted(child.content.expandtabs(4), code_style,
                                           maxLineLength=columns))
            elif kind == 'hr':
                story.append(HRFlowable(width='100%', color=colors.HexColor('#a0a0a0'),
                                        spaceBefore=8, spaceAfter=12))
            elif kind == 'table':
                rows = [row for section in child.children for row in section.children]
                count = max(len(row.children) for row in rows)
                data = []
                for row in rows:
                    cells = []
                    for cell in row.children:
                        markup = ''.join(_inline(n.token.children or [], mono)
                                         for n in cell.children if n.token is not None)
                        if cell.type == 'th':
                            markup = '<b>' + markup + '</b>'
                        alignments: dict[str, Literal[0, 1, 2]] = {
                            'text-align:left': 0, 'text-align:center': 1, 'text-align:right': 2,
                        }
                        alignment = alignments.get(str(cell.attrGet('style')), 0)
                        cell_style = ParagraphStyle('Cell', parent=style, alignment=alignment,
                                                    leftIndent=0, rightIndent=0)
                        cells.append(Paragraph(markup, cell_style))
                    data.append(cells)
                table = Table(data, colWidths=[available / count] * count, repeatRows=1,
                              splitByRow=1, splitInRow=1, hAlign='LEFT')
                table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8edf2')),
                    ('GRID', (0, 0), (-1, -1), .4, colors.HexColor('#b0b0b0')),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('TOPPADDING', (0, 0), (-1, -1), 6),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ]))
                story.append(table)
        return story

    return blocks(root, width)


def markdown_pdf(content: str, path: Path, page_size: tuple[float, float]) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=page_size, leftMargin=36,
                            rightMargin=36, topMargin=36, bottomMargin=36)
    doc.build(markdown_story(content, page_size[0] - 84) or [Paragraph(' ', ParagraphStyle('Empty'))])
