"""Pure, bounded plain-text receipt layout and ESC/POS encoding."""
from dataclasses import dataclass
import re
import textwrap
import unicodedata


@dataclass(frozen=True)
class Receipt:
    preview: str
    lines: int
    columns: int
    data: bytes


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
    """Preview prefixes annotate style; H1 strings use half-width character cells."""
    if len(text) > 10_000 or (title is not None and len(title) > 10_000):
        raise ValueError("Receipt text/title limit is 10,000 characters")
    if type(columns) is not int or not 24 <= columns <= 64:
        raise ValueError("Receipt columns must be 24–64")
    if type(cut) is not bool:
        raise ValueError("cut must be a boolean")
    source = normalize(text)
    entries: list[tuple[str, str]] = []

    def append(content: str, style: str = "", prefix: str = "") -> None:
        width = columns // 2 if style == "H1" else columns
        if len(prefix) >= width:
            # An oversized numeric marker must itself wrap rather than overflow.
            content, prefix = prefix + content, ""
        wrapped = textwrap.wrap(content, width=width, initial_indent=prefix,
                                subsequent_indent=" " * len(prefix),
                                break_long_words=True, break_on_hyphens=False) or [prefix]
        for line in wrapped:
            if style == "H1":
                line = line.center(width)
            entries.append((style, line))
            if len(entries) > 400:
                raise ValueError("Receipt output limit is 400 lines")

    if title is not None:
        append(re.sub(r"\*\*|__|`", "", normalize(title).replace("\n", " ")), "H1")
    blanks = 0
    for raw in source.split("\n"):
        raw = re.sub(r"\*\*|__|`", "", raw)
        if not raw.strip():
            blanks += 1
            if blanks <= 2:
                append("")
            continue
        blanks = 0
        if raw.startswith("# "):
            append(raw[2:], "H1")
        elif raw.startswith("## "):
            append(raw[3:], "B")
        elif raw.strip() == "---":
            append("-" * columns)
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
        data.extend(b"\x1d!" + (b"\x11" if style == "H1" else b"\x00"))
        # H1 centering is represented by the same padding in preview and bytes.
        data.extend(b"\x1ba\x00")
        data.extend(line.encode("cp1252") + b"\n")
    data.extend(b"\x1bE\x00\x1d!\x00\x1ba\x00")
    data.extend(b"\x1dVB\x03" if cut else b"\x1bd\x03")
    preview = "\n".join((f"[{style}] " if style else "") + line for style, line in entries)
    return Receipt(preview, len(entries), columns, bytes(data))
