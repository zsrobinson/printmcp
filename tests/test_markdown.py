import base64
import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.platypus import HRFlowable, ListFlowable, Paragraph, Preformatted, Table

import markdown_pdf as renderer
import server

pytestmark = pytest.mark.usefixtures('cups')

SAMPLE = '''# Quarterly review

A **bold** and *italic* paragraph with `inline code`, café, Ελληνικά, and Привет.
[Visible link](https://example.com) and ![Image description](https://example.com/image.png).

- First bullet
  1. Nested numbered item
  2. Another item
- Second bullet

7. Starting at seven
8. Next item

> A quoted paragraph.
>
> Another quoted paragraph.

```python
print("<b>literal code</b> & safe")
```

---

| Name | Value |
| :--- | ---: |
| **Revenue** | €42 |
| Café | A & B |
'''


def read_pdf(tmp_path, content):
    path = tmp_path / 'rendered.pdf'
    renderer.markdown_pdf(content, path, server.PAPER['Letter'])
    return PdfReader(path)


def test_structure_and_styles():
    story = renderer.markdown_story(SAMPLE, 528)
    assert isinstance(story[0], Paragraph)
    assert story[0].style.fontSize > story[1].style.fontSize
    assert '<b>bold</b>' in story[1].text and '<i>italic</i>' in story[1].text
    assert '<font name=' in story[1].text
    lists = [item for item in story if isinstance(item, ListFlowable)]
    assert len(lists) == 2 and lists[1]._start == 7
    assert lists[0]._start == 'bulletchar'
    assert any(isinstance(item, ListFlowable) for item in lists[0]._flowables[0]._flowables)
    bold = story[1].frags[1]
    assert bold.fontName != story[1].frags[0].fontName
    assert any(isinstance(item, Preformatted) for item in story)
    assert any(isinstance(item, HRFlowable) for item in story)
    assert any(isinstance(item, Table) for item in story)
    quotes = [item for item in story if isinstance(item, Paragraph) and item.style.leftIndent]
    assert len(quotes) == 2


def test_rendered_unicode_tables_and_no_urls(tmp_path):
    reader = read_pdf(tmp_path, SAMPLE)
    text = ' '.join(' '.join(page.extract_text().split()) for page in reader.pages)
    for value in ('Quarterly review', 'café', 'Ελληνικά', 'Привет', 'Visible link',
                  'Image description', 'Revenue', '€42', 'A & B', '<b>literal code</b>'):
        assert value in text
    assert 'https://' not in text
    fonts = reader.pages[0]['/Resources']['/Font'].get_object()
    assert any('DejaVuSans' in str(font.get_object()['/BaseFont']) for font in fonts.values())
    assert not reader.pages[0].get('/Annots')


@pytest.mark.parametrize('content', [
    '<img src="https://example.com/evil"/> & <b>literal</b>',
    '[<font color="red">label</font>](https://example.com)',
    '![<img src="file:///etc/passwd"/>](https://example.com)',
    '`<b>code</b> &lt;tag&gt;`',
    '| Value |\n| --- |\n| <img src="https://example.com"/> |',
])
def test_escape_all_user_markup(tmp_path, content):
    # Any interpreted img would attempt forbidden network/file access or disappear.
    reader = read_pdf(tmp_path, content)
    text = reader.pages[0].extract_text()
    assert '<' in text and '>' in text
    assert '/XObject' not in reader.pages[0]['/Resources']


@pytest.mark.parametrize('content', [
    ('A long paragraph with **emphasis** and café. ' * 1500),
    '- item\n' * 300,
    '```\n' + ('long code line ' * 120 + '\n') * 80 + '```',
    '| Head | Other |\n| --- | --- |\n' + '| cell | value |\n' * 150,
    '| Head | Other |\n| --- | --- |\n| ' + 'cell ' * 3000 + '| value |',
])
def test_long_content_paginates(tmp_path, content):
    reader = read_pdf(tmp_path, content)
    assert len(reader.pages) > 1
    assert all(page.extract_text().strip() for page in reader.pages)


def test_missing_fonts_fall_back(tmp_path, monkeypatch):
    for name in ('PRINT_BODY_FONT', 'PRINT_BODY_FONT_BOLD', 'PRINT_BODY_FONT_ITALIC',
                 'PRINT_BODY_FONT_BOLD_ITALIC', 'PRINT_FONT'):
        monkeypatch.setenv(name, '/missing/font.ttf')
    reader = read_pdf(tmp_path, '# Heading\n\n**Bold** *italic* `code`\n\n```\ncode\n```')
    assert 'Heading' in reader.pages[0].extract_text()
    fonts = reader.pages[0]['/Resources']['/Font'].get_object()
    names = str([font.get_object()['/BaseFont'] for font in fonts.values()])
    assert 'Helvetica' in names and 'Courier' in names


@pytest.mark.parametrize('format', ['text', 'markdown', 'pdf'])
def test_document_dry_run(cups, format):
    content = '# Heading\n\nHello café'
    if format == 'pdf':
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        stream = io.BytesIO()
        writer.write(stream)
        content = base64.b64encode(stream.getvalue()).decode()
    result = server.print_document(content, format=format, dry_run=True, copies=2,
                                   page_range='1', color='color', paper_size='A4')
    assert result == {'dry_run': True, 'content_type': 'application/pdf', 'pages': 1,
                      'bytes': result['bytes'], 'selected_pages': [1], 'copies': 2,
                      'color': 'color', 'duplex': 'one-sided', 'paper_size': 'A4',
                      'prepared_pdf_bytes': result['bytes']}
    assert result['bytes'] > 100
    cups.assert_not_called()


@pytest.mark.parametrize('options', [{'page_range': '2'}, {'dry_run': 1}, {'dry_run': 'false'}])
def test_document_dry_run_validation(cups, options):
    with pytest.raises(ValueError):
        server.print_document('one page', **({'dry_run': True} | options))
    cups.assert_not_called()


def test_document_dry_run_cleanup(cups, monkeypatch):
    paths = []
    original = server.markdown_pdf
    def render(content, path, page_size):
        paths.append(path)
        original(content, path, page_size)
    monkeypatch.setattr(server, 'markdown_pdf', render)
    server.print_document(SAMPLE, format='markdown', dry_run=True)
    assert paths and not paths[0].exists()
    cups.assert_not_called()


def test_text_still_prints_source(tmp_path):
    path = tmp_path / 'text.pdf'
    server.text_pdf('# Heading\n**literal** <b>source</b>', path, 'Letter')
    text = PdfReader(path).pages[0].extract_text()
    assert '# Heading' in text and '**literal** <b>source</b>' in text


def test_document_dry_run_selects_pages(cups):
    result = server.print_document('paragraph\n\n' * 300, format='markdown',
                                   page_range='1-2,4', dry_run=True)
    assert result['pages'] >= 4 and result['selected_pages'] == [1, 2, 4]
    cups.assert_not_called()
