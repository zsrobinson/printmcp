"""Pure, bounded plain-text receipt layout and ESC/POS encoding."""
from dataclasses import dataclass
import io
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import re
import textwrap
import unicodedata


@dataclass(frozen=True)
class Receipt:
    preview: str
    lines: int
    columns: int
    data: bytes
    entries: tuple[tuple[str, str], ...]


def normalize(text: str) -> str:
    """Preserve line separators, remove controls, and produce CP1252 text."""
    replacements = {
        "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
        "…": "...", "•": "-", "\u00a0": " ", "→": "->", "←": "<-",
        "↔": "<->", "⇒": "->",
    }
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    result = []
    for char in text:
        if char == "\n":
            result.append(char)
            continue
        if unicodedata.category(char).startswith("C"):
            continue
        char = replacements.get(char, char)
        try:
            char.encode("cp1252")
        except UnicodeEncodeError:
            char = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode() or "?"
        result.append(char)
    return "".join(result)


def format_receipt(text: str, title: str | None = None, columns: int = 48,
                   cut: bool = True) -> Receipt:
    """Preview prefixes annotate style; titles use double-width character cells."""
    if len(text) > 10_000 or (title is not None and len(title) > 10_000):
        raise ValueError("Receipt text/title limit is 10,000 characters")
    if type(columns) is not int or not 24 <= columns <= 64:
        raise ValueError("Receipt columns must be 24–64")
    if type(cut) is not bool:
        raise ValueError("cut must be a boolean")
    source = normalize(text)
    entries: list[tuple[str, str]] = []

    def append(content: str, style: str = "", prefix: str = "") -> None:
        width = columns // 2 if style == "T" else columns
        value = ""
        if not style and " | " in content:
            left, right = content.rsplit(" | ", 1)
            if right.strip() and len(right.strip()) <= columns // 3:
                content, value = left, right.strip()
                width = columns - len(value) - 2
        if len(prefix) >= width:
            # An oversized numeric marker must itself wrap rather than overflow.
            content, prefix = prefix + content, ""
        wrapped = textwrap.wrap(content, width=width, initial_indent=prefix,
                                subsequent_indent=" " * len(prefix),
                                break_long_words=True, break_on_hyphens=False) or [prefix]
        first = True
        for line in wrapped:
            if value and first:
                line = line.ljust(columns - len(value)) + value
                first = False
            if style == "H":
                line = line.ljust(columns)
            entries.append((style, line))
            if len(entries) > 400:
                raise ValueError("Receipt output limit is 400 lines")

    if title is not None:
        append(re.sub(r"\*\*|__|`", "", normalize(title).replace("\n", " ")), "T")
    blanks = 0
    for raw in source.split("\n"):
        bold = re.fullmatch(r"\*\*(.+)\*\*", raw) is not None
        raw = re.sub(r"\*\*|__|`", "", raw)
        if not raw.strip():
            blanks += 1
            if blanks <= 2:
                append("")
            continue
        blanks = 0
        if raw.startswith("# "):
            append(raw[2:], "T")
        elif raw.startswith("## "):
            append(raw[3:], "H", prefix=" ")
        elif raw.strip() == "---":
            append(("- " * columns)[:columns].rstrip())
        elif bold:
            append(raw, "B")
        else:
            item = re.match(r"^(?:[-*] (\[[ xX]\] )|([-*] )|(\d+\. ))(.*)$", raw)
            if item:
                prefix = item[1] or ("- " if item[2] else item[3])
                append(item[4], prefix=prefix.replace("[X]", "[x]"))
            else:
                append(raw)
    data = bytearray(b"\x1b@\x1bt\x10")
    for style, line in entries:
        data.extend(b"\x1bE" + (b"\x01" if style else b"\x00"))
        data.extend(b"\x1d!" + (b"\x11" if style == "T" else b"\x00"))
        data.extend(b"\x1dB" + (b"\x01" if style == "H" else b"\x00"))
        data.extend(b"\x1ba\x00")
        data.extend(line.encode("cp1252") + b"\n")
        data.extend(b"\x1bE\x00\x1d!\x00\x1dB\x00\x1ba\x00")
    data.extend(b"\x1bE\x00\x1d!\x00\x1dB\x00\x1ba\x00")
    data.extend(b"\x1dVB\x03" if cut else b"\x1bd\x03")
    preview = "\n".join((f"[{style}] " if style else "") + line for style, line in entries)
    return Receipt(preview, len(entries), columns, bytes(data), tuple(entries))


def render_receipt(receipt: Receipt, font_path: str) -> bytes:
    """Render the shared layout at 12x24 pixels per normal printer cell."""
    regular = ImageFont.truetype(font_path, 20)
    path = Path(font_path)
    bold_path = path.with_name(path.stem + "-Bold" + path.suffix)
    bold = ImageFont.truetype(str(bold_path), 20) if bold_path.is_file() else regular
    height = sum(48 if style == "T" else 24 for style, _ in receipt.entries)
    image = Image.new("RGB", (receipt.columns * 12, max(24, height)), "white")
    y = 0
    for style, line in receipt.entries:
        scale = 2 if style == "T" else 1
        reverse = style == "H"
        row = Image.new("RGB", (receipt.columns * 12 // scale, 24),
                        "black" if reverse else "white")
        draw = ImageDraw.Draw(row)
        for x, char in enumerate(line):
            draw.text((x * 12, 0), char, font=bold if style else regular,
                      fill="white" if reverse else "black",
                      stroke_width=1 if style and bold is regular else 0)
        if scale == 2:
            row = row.resize((row.width * 2, 48), Image.Resampling.NEAREST)
        image.paste(row, (0, y))
        y += 24 * scale
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
