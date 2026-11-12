import asyncio
import json
from pathlib import Path
import stat
import subprocess

import pytest
from mcp import types

from receipt import format_receipt, normalize
import server

pytestmark = pytest.mark.usefixtures("cups")


@pytest.mark.parametrize("columns", [24, 48])
def test_wrap(columns):
    result = format_receipt("word " * 30 + "z" * 80, columns=columns)
    assert all(len(line) <= columns for line in result.preview.splitlines())
    assert format_receipt("z" * 80, columns=columns).preview.splitlines()[0] == "z" * columns


@pytest.mark.parametrize("prefix,printed", [("- ", "- "), ("* ", "- "),
    ("- [ ] ", "[ ] "), ("- [x] ", "[x] "), ("1. ", "1. ")])
def test_hanging_indent(prefix, printed):
    lines = format_receipt(prefix + "task " * 10, columns=24).preview.splitlines()
    assert lines[0].startswith(printed)
    assert all(line.startswith(" " * len(printed)) for line in lines[1:])
    assert all(len(line) <= 24 for line in lines)


def test_headings_rules_title_and_blanks():
    result = format_receipt("# " + "a" * 30 + "\n## **Bold**\n---\n\n\n\nlast", title="Tasks", columns=24)
    lines = result.preview.splitlines()
    assert lines[0] == "[T] Tasks"
    assert [line[4:].strip() for line in lines[1:4]] == ["a" * 12, "a" * 12, "a" * 6]
    assert lines[4:8] == ["[H] " + " Bold".ljust(24), ("- " * 12).rstrip(), "", ""]
    assert lines[8] == "last"
    assert result.lines == 9


def test_transliteration_and_controls():
    assert normalize("‘a’ “b” – — … •\u00a0→ ← café Ā 中 😀") == "'a' \"b\" - - ... - -> <- café A ? ?"
    result = format_receipt("**a** __b__ `c`\tX\x1b\x1d\x00\x7f\x85")
    assert result.preview == "a b c X"
    assert normalize("a\tX") == "a   X"
    assert normalize("é").encode("cp1252") == b"\xe9"
    assert normalize("a\r\nb\rc") == "a\nb\nc"


@pytest.mark.parametrize("cut,ending", [(True, b"\x1dVB\x03"), (False, b"\x1bd\x03")])
def test_exact_framing(cut, ending):
    result = format_receipt("café", cut=cut)
    reset = b"\x1bE\x00\x1d!\x00\x1dB\x00\x1ba\x00"
    assert result.data == b"\x1b@\x1bt\x10" + reset + b"caf\xe9\n" + reset * 2 + ending
    styled = format_receipt("# A\n## B\n**C**", columns=24).data
    assert styled == (b"\x1b@\x1bt\x10"
        + b"\x1bE\x01\x1d!\x11\x1dB\x00\x1ba\x00A\n" + reset
        + b"\x1bE\x01\x1d!\x00\x1dB\x01\x1ba\x00" + b" B".ljust(24) + b"\n" + reset
        + b"\x1bE\x01\x1d!\x00\x1dB\x00\x1ba\x00C\n" + reset * 2 + b"\x1dVB\x03")



@pytest.mark.parametrize("kwargs,message", [
    ({"text": "x" * 10001}, "10,000"), ({"title": "x" * 10001}, "10,000"),
    ({"text": "x\n" * 400}, "400 lines"), ({"columns": 23}, "24–64"),
    ({"columns": 65}, "24–64"), ({"cut": 1}, "boolean")])
def test_limits(kwargs, message):
    with pytest.raises(ValueError, match=message):
        format_receipt(**({"text": "ok"} | kwargs))


def test_limit_boundaries():
    assert format_receipt("x" * 10000).lines <= 400
    assert format_receipt("\n".join(["x"] * 400)).lines == 400


def test_oversized_numeric_marker_cannot_overflow():
    result = format_receipt("1" * 60 + ". task", columns=24)
    assert all(len(line) <= 24 for line in result.preview.splitlines())


def test_dry_run(cups, monkeypatch):
    monkeypatch.setenv("PRINT_RECEIPT_QUEUE", "receipt")
    monkeypatch.setenv("PRINT_RECEIPT_COLUMNS", "24")
    expected = format_receipt("- [ ] Pick up milk", "Tasks", 24)
    assert server.print_receipt("- [ ] Pick up milk", "Tasks", dry_run=True) == {
        "preview": expected.preview, "lines": expected.lines, "columns": 24,
        "bytes": len(expected.data), "submitted": False}
    cups.assert_not_called()


@pytest.mark.parametrize("outcome", ["success", "unknown", "timeout", "failure"])
def test_submit_and_cleanup(cups, monkeypatch, outcome):
    monkeypatch.setenv("PRINT_RECEIPT_QUEUE", "receipt.queue")
    paths = []
    def submit(args, **kwargs):
        assert args[:-1] == ("lp", "-h", server.CUPS, "-d", "receipt.queue", "-o", "raw", "-t", "Receipt")
        path = Path(args[-1]); paths.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert path.read_bytes() == format_receipt("Hello").data
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        if outcome == "failure":
            raise subprocess.CalledProcessError(1, args, stderr="offline")
        return subprocess.CompletedProcess(args, 0, "request id is " +
            ("receipt.queue-42 (1 file(s))" if outcome == "success" else "other-42"), "")
    cups.side_effect = submit
    if outcome == "success":
        assert server.print_receipt("Hello")["job_id"] == "receipt.queue-42"
    else:
        with pytest.raises((RuntimeError, TimeoutError)):
            server.print_receipt("Hello")
    assert len(paths) == 1 and not paths[0].exists()


def test_receipt_jobs_and_queue_only_status(cups, monkeypatch):
    monkeypatch.setenv("PRINT_RECEIPT_QUEUE", "receipt")
    monkeypatch.setenv("PRINT_STATUS_URI", "ipp://never-contact.invalid")
    cups.side_effect = None
    cups.return_value = subprocess.CompletedProcess([], 0, "idle", "")
    assert server.list_jobs(True, "receipt") == {"jobs": "idle"}
    assert cups.call_args.args[0] == ("lpstat", "-h", server.CUPS, "-W", "all", "-o", "receipt")
    server.cancel_job("receipt-2", "receipt")
    assert cups.call_args.args[0] == ("cancel", "-h", server.CUPS, "receipt-2")
    for job, printer in [("epson-2", "receipt"), ("receipt-2", "document")]:
        with pytest.raises(ValueError):
            server.cancel_job(job, printer)
    cups.reset_mock()
    assert server.printer_status("receipt") == {"queue": "receipt", "queue_state_source": "cups", "state": "idle"}
    assert cups.call_count == 1
    assert cups.call_args.args[0] == ("lpstat", "-h", server.CUPS, "-p", "receipt", "-l")


def test_unconfigured(cups, monkeypatch):
    monkeypatch.delenv("PRINT_RECEIPT_QUEUE", raising=False)
    for call in [lambda: server.print_receipt("x", dry_run=True),
                 lambda: server.list_jobs(printer="receipt"),
                 lambda: server.cancel_job("receipt-1", "receipt"),
                 lambda: server.printer_status("receipt")]:
        with pytest.raises(ValueError, match="not configured"):
            call()
    cups.assert_not_called()


def test_registration_schema_and_telemetry(cups, monkeypatch, capsys):
    async def check(enabled):
        mcp = server.create_server()
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        assert ("print_receipt" in tools) == enabled
        for name in ("list_jobs", "cancel_job", "printer_status"):
            prop = tools[name].inputSchema["properties"]["printer"]
            assert prop["default"] == "document" and prop["enum"] == ["document", "receipt"]
        if enabled:
            request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
                name="print_receipt", arguments={"text": "PRIVATE_SENTINEL", "dry_run": True}))
            response = await mcp._mcp_server.request_handlers[types.CallToolRequest](request)
            assert not response.root.isError
    monkeypatch.delenv("PRINT_RECEIPT_QUEUE", raising=False)
    asyncio.run(check(False))
    monkeypatch.setenv("PRINT_RECEIPT_QUEUE", "receipt")
    asyncio.run(check(True))
    output = capsys.readouterr().err
    record = json.loads(next(line[9:] for line in output.splitlines() if line.startswith("mcp_call ")))
    assert record["tool"] == "print_receipt" and record["outcome"] == "ok"
    assert "render" in record["phase_ms"] and "PRIVATE_SENTINEL" not in output
    cups.assert_not_called()


@pytest.mark.parametrize("columns", ["23", "65", "bad"])
def test_invalid_configuration(monkeypatch, columns):
    monkeypatch.setenv("PRINT_RECEIPT_QUEUE", "receipt")
    monkeypatch.setenv("PRINT_RECEIPT_COLUMNS", columns)
    with pytest.raises(ValueError):
        server.create_server()


@pytest.mark.parametrize("prefix", ["", "- ", "- [ ] ", "- [x] ", "1. "])
def test_values(prefix):
    printed = prefix.removeprefix("- ") if "[" in prefix else prefix
    result = format_receipt(prefix + "one two three four five six seven | 5:00 PM", columns=32)
    assert result.preview.splitlines() == [
        (printed + ("one two three four" if prefix else "one two three four five")).ljust(25) + "5:00 PM",
        " " * len(printed) + ("five six seven" if prefix else "six seven")]
    assert format_receipt("a | b | noon", columns=32).preview == "a | b".ljust(28) + "noon"


def test_value_fallback_and_headings():
    assert format_receipt("left | ").preview == "left |"
    assert format_receipt("left | " + "v" * 17).preview == "left | " + "v" * 17
    assert format_receipt("# a | b").preview == "[T] a | b"
    assert format_receipt("## a | b").preview == "[H] " + " a | b".ljust(48)
    assert format_receipt("**a | b**").preview == "[B] a | b"


def test_narrow_styles():
    assert format_receipt("# abcdefghijklmnopqrstuvwxyz\n## " + "x" * 34 + "\n**Bold**\n---", columns=32).preview.splitlines() == [
        "[T] abcdefghijklmnop", "[T] qrstuvwxyz",
        "[H]  " + "x" * 31, "[H] " + (" " + "xxx").ljust(32),
        "[B] Bold", ("- " * 16).rstrip()]


def test_png_and_mcp(cups, monkeypatch):
    import base64
    import io
    from PIL import Image
    from receipt import render_receipt
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    short = Image.open(io.BytesIO(render_receipt(format_receipt("plain"), font)))
    png = render_receipt(format_receipt("# Title\n## BAR\nplain"), font)
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG" and image.size == (576, 96)
    assert image.height > short.height
    assert image.getpixel((575, 48)) == (0, 0, 0)
    assert image.getpixel((575, 72)) == (255, 255, 255)
    monkeypatch.setenv("PRINT_RECEIPT_QUEUE", "receipt")
    async def check():
        mcp = server.create_server()
        request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="print_receipt", arguments={"text": "## BAR", "dry_run": True, "preview_image": True}))
        result = (await mcp._mcp_server.request_handlers[types.CallToolRequest](request)).root
        assert not result.isError
        assert result.structuredContent["submitted"] is False
        fields = json.loads(result.content[0].text)
        assert fields == result.structuredContent
        assert fields["columns"] == 48 and fields["preview"] == "[H] " + " BAR".ljust(48)
        picture = result.content[1]
        assert picture.type == "image" and picture.mimeType == "image/png"
        assert Image.open(io.BytesIO(base64.b64decode(picture.data))).width == 576
    asyncio.run(check())
    cups.assert_not_called()


SAMPLE = """**Morning brief**
Thursday, November 12 2026  7:30 AM
from Example Assistant
---

Good morning. It's a light Thursday: one lecture, office hours...

## TASKS
- [ ] Sign the lab safety form | 5:00 PM
- [ ] Pick a dentist slot
- [ ] Submit coursework project | 11:59 PM

## CALENDAR
Lecture | 10:00 AM
Office hours | 1:30 PM"""


def test_sample_brief():
    assert format_receipt(SAMPLE).preview.splitlines() == [
        "[B] Morning brief", "Thursday, November 12 2026  7:30 AM",
        "from Example Assistant", "- - - - - - - - - - - - - - - - - - - - - - - -", "",
        "Good morning. It's a light Thursday: one", "lecture, office hours...", "",
        "[H]  TASKS" + " " * 42,
        "[ ] Sign the lab safety form" + " " * 13 + "5:00 PM",
        "[ ] Pick a dentist slot",
        "[ ] Submit coursework project           11:59 PM", "",
        "[H]  CALENDAR" + " " * 39,
        "Lecture                                 10:00 AM",
        "Office hours                             1:30 PM"]



def test_sample_brief_narrow():
    assert format_receipt(SAMPLE, columns=32).preview.splitlines() == [
        "[B] Morning brief", "Thursday, November 12 2026  7:30", "AM",
        "from Example Assistant", "- - - - - - - - - - - - - - - -", "",
        "Good morning. It's a light", "Thursday: one lecture, office", "hours...", "",
        "[H]  TASKS" + " " * 26,
        "[ ] Sign the lab safety  5:00 PM", "    form", "[ ] Pick a dentist slot",
        "[ ] Submit coursework   11:59 PM", "    project", "",
        "[H]  CALENDAR" + " " * 23,
        "Lecture                 10:00 AM", "Office hours             1:30 PM"]


def test_image_without_dry_run(cups, monkeypatch):
    monkeypatch.setenv("PRINT_RECEIPT_QUEUE", "receipt")
    cups.side_effect = None
    cups.return_value = subprocess.CompletedProcess([], 0, "request id is receipt-1", "")
    result = server.print_receipt("## A", preview_image=True)
    assert result.structuredContent["job_id"] == "receipt-1"
    assert result.content[1].mimeType == "image/png"
    assert cups.call_count == 1


def test_synthetic_bold(tmp_path):
    import io
    from PIL import Image
    from receipt import render_receipt
    font = tmp_path / "mono.ttf"
    font.write_bytes(Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf").read_bytes())
    image = Image.open(io.BytesIO(render_receipt(format_receipt("**Bold**"), str(font))))
    assert image.size == (576, 24)
