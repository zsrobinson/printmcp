"""Local CUPS MCP. Importing this module never contacts CUPS."""
from __future__ import annotations

import base64
import binascii
import logging
import os
import io
import hmac
import hashlib
import secrets
from dataclasses import dataclass
from contextlib import asynccontextmanager
import plistlib
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import functools
import threading
import selectors
import time
import telemetry
from typing import Literal, Any, Callable

import anyio
import jwt
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit
import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from typing import Annotated
from PIL import Image as PILImage
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import pagesizes
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Preformatted, SimpleDocTemplate
from reportlab.lib.styles import ParagraphStyle

QUEUE = os.environ.get("PRINT_QUEUE", "epson")
CUPS = os.environ.get("PRINT_CUPS_SERVER", "localhost:631")
MAX_PDF = 10 * 1024 * 1024
PAPER = {"A4": pagesizes.A4, "Letter": pagesizes.letter,
         "Legal": pagesizes.legal, "A3": pagesizes.A3, "Tabloid": pagesizes.elevenSeventeen}


def run(*args: str) -> str:
    """No shell, fixed CUPS server, bounded output/time; no ambient CUPS overrides."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('CUPS_')}
    env.update(LC_ALL="C", CUPS_SERVER=CUPS)
    with telemetry.phase("status" if args[0] in ("lpstat", "ipptool") else "cups"):
        try:
            result = _bounded_run(args, timeout=30, check=True, env=env)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("CUPS command timed out; check list_jobs before retrying a print.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"CUPS command failed: {exc.stderr[:2000]}") from exc
    return result.stdout[:65536]


def _bounded_run(args: tuple[str, ...], *, timeout: float, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    output = bytearray()
    errors = bytearray()
    # Drain both pipes concurrently, retaining at most 64 KiB each.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, output)
            selector.register(proc.stderr, selectors.EVENT_READ, errors)
            try:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("CUPS command timed out; check list_jobs before retrying a print.")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            key.data.extend(chunk[:max(0, 65536 - len(key.data))])
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except BaseException:
                proc.kill()
                proc.wait()
                raise
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, args, stderr=errors.decode(errors="replace"))
    return subprocess.CompletedProcess(args, 0, output.decode(errors="replace"), errors.decode(errors="replace"))

_font_lock = threading.Lock()

@telemetry.timed("render")

def text_pdf(content: str, path: Path, paper: str) -> None:
    # Text and Markdown source are preserved as readable, wrapped Unicode text.
    font = os.environ.get("PRINT_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
    with _font_lock:
        if "Draft" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("Draft", font))
    width, _ = PAPER[paper]
    columns = int((width - 72) / 6.1)
    lines = []
    for line in content.expandtabs(4).splitlines():
        lines.extend(textwrap.wrap(line, columns, replace_whitespace=False,
                                   drop_whitespace=False) or [""])
    style = ParagraphStyle("Draft", fontName="Draft", fontSize=10, leading=14)
    SimpleDocTemplate(str(path), pagesize=PAPER[paper], leftMargin=36,
                      rightMargin=36, topMargin=36, bottomMargin=36).build(
        [Preformatted("\n".join(lines), style)])


def print_document(
    content: str,
    format: Literal["pdf", "text", "markdown"] = "text",
    copies: Annotated[int, Field(ge=1, le=100)] = 1,
    duplex: Literal["one-sided", "two-sided-long-edge", "two-sided-short-edge"] = "one-sided",
    color: Literal["color", "monochrome"] = "monochrome",
    page_range: str | None = None,
    paper_size: Literal["A4", "Letter", "Legal", "A3", "Tabloid"] = "Letter",
) -> dict[str, str]:
    """Print a base64 PDF or UTF-8 text/Markdown source as PDF. This consumes paper.

    Markdown is printed as wrapped source, not rendered HTML. No URLs are fetched.
    Do not automatically retry if the outcome is uncertain; inspect list_jobs first.
    """
    _validate_options(copies, duplex, color, page_range, paper_size)
    if not content or len(content) > 14 * 1024 * 1024:
        raise ValueError("Empty or oversized document")
    with tempfile.TemporaryDirectory(prefix="fleet-print-") as directory:
        path = Path(directory) / "document.pdf"
        if format == "pdf":
            try:
                pdf = base64.b64decode(content, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("Invalid base64 PDF") from exc
            if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_PDF:
                raise ValueError("Expected PDF up to 10 MiB")
            path.write_bytes(pdf)
        elif format in ("text", "markdown"):
            if len(content) > 200_000:
                raise ValueError("Text limit is 200,000 characters")
            text_pdf(content, path, paper_size)
        else:
            raise ValueError("Invalid document format")
        return _submit(path, copies, duplex, color, page_range, paper_size)


def _validate_options(copies: int, duplex: str, color: str, page_range: str | None, paper_size: str) -> None:
    if type(copies) is not int or not 1 <= copies <= 100:
        raise ValueError("copies must be 1–100")
    if duplex not in ("one-sided", "two-sided-long-edge", "two-sided-short-edge"):
        raise ValueError("Invalid duplex")
    if color not in ("color", "monochrome") or paper_size not in PAPER:
        raise ValueError("Invalid color or paper size")
    if page_range is not None:
        if not re.fullmatch(r"[1-9]\d*(?:-[1-9]\d*)?(?:,[1-9]\d*(?:-[1-9]\d*)?)*", page_range) or len(page_range) > 200:
            raise ValueError("Use page ranges such as 1-3,5")
        for item in page_range.split(','):
            bounds = item.split('-')
            if len(bounds) == 2 and int(bounds[0]) > int(bounds[1]):
                raise ValueError("Reversed page range")


def _submit(path: Path, copies: int, duplex: str, color: str, page_range: str | None, paper_size: str) -> dict[str, str]:
    args = ["lp", "-h", CUPS, "-d", QUEUE, "-n", str(copies),
            "-t", "Draft", "-o", f"sides={duplex}", "-o", f"print-color-mode={color}",
            "-o", f"media={paper_size}"]
    if page_range:
        args.extend(["-o", f"page-ranges={page_range}"])
    output = run(*args, str(path))
    match = re.search(rf"request id is ({re.escape(QUEUE)}-\d+)", output)
    if not match:
        raise RuntimeError("Submission outcome unknown; inspect list_jobs before retrying.")
    return {"job_id": match[1]}


def origin(value: str) -> tuple[str, str, int]:
    try:
        if len(value) > 8192 or re.search(r"[\x00-\x20\x7f\\]", value):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        return parsed.scheme, parsed.hostname.lower(), parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Invalid PDF URL") from None


def _public_addresses(host: str, port: int) -> list[tuple[int, tuple[Any, ...]]]:
    """Resolve once, fail closed if ANY answer is non-public, then pin sockets."""
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    endpoints: list[tuple[int, tuple[Any, ...]]] = []
    for family, _, _, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        if (family not in (socket.AF_INET, socket.AF_INET6) or
                not ip.is_global or ip.is_multicast or ip.is_reserved or
                (isinstance(ip, ipaddress.IPv4Address) and
                 ip in ipaddress.IPv4Network("192.0.0.0/24") and str(ip) not in ("192.0.0.9", "192.0.0.10")) or
                (isinstance(ip, ipaddress.IPv6Address) and
                 (ip.ipv4_mapped is not None or ip.is_site_local or ip.sixtofour is not None or ip.teredo is not None))):
            raise ValueError("PDF URL must resolve exclusively to public addresses")
        endpoints.append((family, address))
    if not endpoints:
        raise ValueError("PDF URL has no public addresses")
    return endpoints


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, endpoint: tuple[int, tuple[Any, ...]], timeout: float):
        self.tls_context = ssl.create_default_context()
        self.tls_context.set_alpn_protocols(["http/1.1"])
        super().__init__(host, port, timeout=timeout, context=self.tls_context)
        self.endpoint = endpoint
        self.socket_timeout = timeout

    def connect(self) -> None:
        # Never call create_connection(host): that would resolve DNS a second time.
        family, address = self.endpoint
        raw = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        raw.settimeout(self.socket_timeout)
        try:
            raw.connect(address)
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_pdf(url: str) -> bytes:
    """Public HTTPS only: pinned DNS, no credentials, redirects, proxies or cookies."""
    identity = origin(url)
    if identity[0] != "https" or not 1 <= identity[2] <= 65535:
        raise ValueError("PDF URL must use HTTPS")
    configured = os.environ.get("PRINT_URL_ALLOWED_ORIGINS")
    if configured is not None:
        allowed = set()
        for entry in configured.split(","):
            if not entry.strip():
                continue
            parsed = urlsplit(entry.strip())
            if parsed.path not in ("", "/") or parsed.query or parsed.scheme != "https":
                raise ValueError("Allowlist entries must be HTTPS origins")
            allowed.add(origin(entry.strip()))
        if identity not in allowed:
            raise ValueError("PDF URL origin is not allowed")
    limit = int(os.environ.get("PRINT_URL_MAX_BYTES", str(50 * 1024 * 1024)))
    if limit <= 0:
        raise ValueError("PRINT_URL_MAX_BYTES must be positive")
    data = bytearray()
    deadline = time.monotonic() + 60
    connection = None
    try:
        host = identity[1].encode("idna").decode("ascii")
        addresses = _public_addresses(host, identity[2])
        for endpoint in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("PDF exceeds download time limit")
            candidate = _PinnedHTTPSConnection(host, identity[2], endpoint, min(10.0, remaining))
            try:
                candidate.connect()
            except OSError:
                candidate.close()
                continue
            connection = candidate
            break
        if connection is None:
            raise ValueError("PDF download connection failed")
        parsed = urlsplit(url)
        target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        # HTTPConnection supplies Host for the original hostname. No auth, Cookie,
        # proxy or redirect machinery is installed, and TLS checks that hostname.
        connection.request("GET", target, headers={"Accept": "application/pdf"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError("PDF download requires HTTP 200; redirects are refused")
            declared = response.headers.get("content-length", "0")
            if not declared.isascii() or not declared.isdigit() or len(declared) > 20:
                raise ValueError("Invalid PDF download length")
            if int(declared) > limit:
                raise ValueError("PDF exceeds download size limit")
            while True:
                chunk = response.read1(65536)
                if len(data) + len(chunk) > limit or time.monotonic() > deadline:
                    raise ValueError("PDF exceeds download size or time limit")
                if not chunk:
                    break
                data.extend(chunk)
    except (OSError, http.client.HTTPException, UnicodeError):
        raise ValueError("PDF download failed") from None
    finally:
        if connection is not None:
            connection.close()
    if not data.startswith(b"%PDF-"):
        raise ValueError("Expected a PDF download")
    return bytes(data)


def print_url(
    url: str,
    page_range: str | None = None,
    copies: Annotated[int, Field(ge=1, le=100)] = 1,
    color: Literal["color", "monochrome"] = "monochrome",
    duplex: Literal["one-sided", "two-sided-long-edge", "two-sided-short-edge"] = "one-sided",
    paper_size: Literal["A4", "Letter", "Legal", "A3", "Tabloid"] = "Letter",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Print any PDF from a public https link, e.g. a short-lived download link another service gives you.

    Use dry_run=True to fetch, validate and count pages without any CUPS call.
    The default dry_run=False consumes paper.

    Supply a short-lived PDF URL directly, never base64 through the model. page_range
    refers to pages in the downloaded PDF (already subset URLs start at page 1).
    Do not retry an uncertain submission; inspect list_jobs first.
    """
    _validate_options(copies, duplex, color, page_range, paper_size)
    pdf = fetch_pdf(url)
    with tempfile.TemporaryDirectory(prefix="fleet-print-") as directory:
        path = Path(directory) / "document.pdf"
        path.write_bytes(pdf)
        return _print_file(path, "application/pdf", page_range, copies, color, duplex, paper_size, dry_run)


CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg"}


def _document_info(data: bytes, content_type: str) -> dict[str, Any]:
    """Sniff and parse, without trusting a suffix or logging document contents."""
    detected = ("application/pdf" if data.startswith(b"%PDF-") else
                "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
                "image/jpeg" if data.startswith(b"\xff\xd8\xff") else None)
    if detected != content_type:
        raise ValueError("Document magic does not match its declared type")
    try:
        if detected == "application/pdf":
            # Tolerate repairable publisher PDFs; submission keeps the original bytes.
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise ValueError()
            pages = len(reader.pages)
            if not 1 <= pages <= 10000:
                raise ValueError()
            return {"content_type": detected, "pages": pages, "bytes": len(data)}
        with PILImage.open(io.BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG") or getattr(image, "n_frames", 1) != 1:
                raise ValueError()
            if image.width * image.height > 20_000_000:
                raise ValueError()
            image.verify()
            return {"content_type": detected, "pages": 1, "bytes": len(data)}
    except Exception:
        raise ValueError("Invalid, encrypted or unsupported document") from None


def _print_file(path: Path, content_type: str, page_range: str | None, copies: int,
                color: str, duplex: str, paper_size: str, dry_run: bool) -> dict[str, Any]:
    _validate_options(copies, duplex, color, page_range, paper_size)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a boolean")
    data = path.read_bytes()
    info = _document_info(data, content_type)
    selected = set(range(1, info["pages"] + 1))
    if page_range:
        selected = set()
        for part in page_range.split(","):
            bounds = list(map(int, part.split("-")))
            if bounds[-1] > info["pages"]:
                raise ValueError("Page range outside document")
            selected.update(range(bounds[0], bounds[-1] + 1))
    with tempfile.TemporaryDirectory(prefix="print-prepare-") as directory:
        prepared = path
        if content_type != "application/pdf":
            prepared = Path(directory) / "image.pdf"
            width, height = PAPER[paper_size]
            with PILImage.open(io.BytesIO(data)) as image:
                image.load()
                scale = min((width - 72) / image.width, (height - 72) / image.height)
                canvas = Canvas(str(prepared), pagesize=(width, height))
                canvas.drawImage(ImageReader(image), (width - image.width * scale) / 2,
                                 (height - image.height * scale) / 2,
                                 width=image.width * scale, height=image.height * scale, mask="auto")
                canvas.showPage()
                canvas.save()
        if dry_run:
            return {"dry_run": True, **info, "selected_pages": sorted(selected),
                    "copies": copies, "color": color, "duplex": duplex, "paper_size": paper_size,
                    "prepared_pdf_bytes": prepared.stat().st_size}
        return _submit(prepared, copies, duplex, color, page_range, paper_size)


@dataclass
class Upload:
    upload_id: str
    content_type: str
    max_bytes: int
    expires_at: int
    state: str = "pending"
    path: Path | None = None
    info: dict[str, Any] | None = None
    terminal_at: int | None = None


class UploadStore:
    """Single-process ephemeral uploads. One lock owns state transitions, not I/O."""
    def __init__(self) -> None:
        self.secret = os.environ.get("PRINT_UPLOAD_SECRET", "").encode()
        self.base = os.environ.get("PRINT_UPLOAD_BASE_URL", "").rstrip("/")
        parsed = urlsplit(self.base)
        if len(self.secret) < 32 or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Uploads require PRINT_UPLOAD_SECRET (32+ bytes) and PRINT_UPLOAD_BASE_URL")
        origin(self.base)
        self.limit = int(os.environ.get("PRINT_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
        self.capacity = int(os.environ.get("PRINT_UPLOAD_MAX_PENDING", "8"))
        if self.limit <= 0 or self.capacity <= 0:
            raise ValueError("Upload limits must be positive")
        self.directory = tempfile.TemporaryDirectory(prefix="print-uploads-")
        os.chmod(self.directory.name, 0o700)
        self.lock = threading.RLock()
        self.entries: dict[str, Upload] = {}

    def close(self) -> None:
        self.directory.cleanup()

    def _discard(self, upload: Upload, state: str) -> None:
        if upload.path is not None:
            upload.path.unlink(missing_ok=True)
            upload.path = None
        upload.state = state
        upload.terminal_at = int(time.time())

    def sweep(self) -> None:
        with self.lock:
            now = int(time.time())
            for key, upload in list(self.entries.items()):
                # An in-flight operation owns cleanup; PUT also checks expiry on completion.
                if upload.state in ("pending", "received") and upload.expires_at <= now:
                    self._discard(upload, "expired")
                if upload.terminal_at is not None and upload.terminal_at + 600 <= now:
                    del self.entries[key]

    def create(self, content_type: str, max_bytes: int | None) -> dict[str, Any]:
        declared = CONTENT_TYPES.get(content_type, content_type)
        if declared not in CONTENT_TYPES.values():
            raise ValueError("content_type must be pdf, png or jpeg")
        maximum = self.limit if max_bytes is None else max_bytes
        if type(maximum) is not int or not 1 <= maximum <= self.limit:
            raise ValueError("max_bytes must be positive and within the upload cap")
        with self.lock:
            self.sweep()
            if (len(self.entries) >= self.capacity * 4 or
                    sum(u.state in ("pending", "receiving", "received", "printing") for u in self.entries.values()) >= self.capacity):
                raise ValueError("Upload capacity reached; wait for existing uploads to expire")
            key = secrets.token_hex(16)
            expires = int(time.time()) + 600
            self.entries[key] = Upload(key, declared, maximum, expires)
            payload = f"{key}.{expires}"
            signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            return {"upload_id": key, "url": f"{self.base}/upload/{payload}.{signature}",
                    "expires_at": expires, "max_bytes": maximum, "content_type": declared}

    def reserve(self, token: str) -> Upload:
        try:
            if len(token) > 200:
                raise ValueError()
            key, expires, signature = token.split(".")
            payload = f"{key}.{expires}"
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature) or int(expires) <= time.time():
                raise ValueError()
            with self.lock:
                self.sweep()
                upload = self.entries[key]
                if upload.expires_at != int(expires) or upload.state != "pending":
                    raise ValueError()
                upload.state = "receiving"
                upload.path = Path(self.directory.name) / key
                return upload
        except Exception:
            raise ValueError("Invalid or consumed upload") from None

    def finish(self, upload: Upload, info: dict[str, Any]) -> None:
        with self.lock:
            if upload.expires_at <= time.time():
                self._discard(upload, "expired")
                raise ValueError("Upload expired")
            upload.info = info
            upload.state = "received"

    def discard(self, upload: Upload) -> None:
        with self.lock:
            self._discard(upload, "expired")

    def status(self, upload_id: str) -> dict[str, Any]:
        with self.lock:
            self.sweep()
            upload = self.entries.get(upload_id)
            if upload is None:
                return {"upload_id": upload_id, "status": "expired"}
            state = "pending" if upload.state == "receiving" else "received" if upload.state == "printing" else upload.state
            return {"upload_id": upload_id, "status": state, "expires_at": upload.expires_at,
                    **(upload.info or {"content_type": upload.content_type})}

    def claim(self, upload_id: str) -> Upload:
        with self.lock:
            self.sweep()
            upload = self.entries.get(upload_id)
            if upload is None or upload.state != "received":
                raise ValueError("Upload is not received or has already been consumed")
            upload.state = "printing"
            return upload

    def complete(self, upload: Upload, dry_run: bool) -> None:
        with self.lock:
            if dry_run and upload.expires_at > time.time():
                upload.state = "received"
            else:
                self._discard(upload, "expired" if dry_run else "printed")


_uploads: UploadStore | None = None
_uploads_lock = threading.Lock()


def _get_uploads() -> UploadStore:
    global _uploads
    with _uploads_lock:
        if _uploads is None:
            _uploads = UploadStore()
        return _uploads


def create_upload(content_type: Literal["pdf", "png", "jpeg"], max_bytes: int | None = None) -> dict[str, Any]:
    """Create a ten-minute single-use PUT capability. No document goes through model arguments."""
    return _get_uploads().create(content_type, max_bytes)


def upload_status(upload_id: str) -> dict[str, Any]:
    """Return pending, received (bytes/type/pages), printed or expired. Never prints."""
    return _get_uploads().status(upload_id)


def print_upload(
    upload_id: str,
    page_range: str | None = None,
    copies: Annotated[int, Field(ge=1, le=100)] = 1,
    color: Literal["color", "monochrome"] = "monochrome",
    duplex: Literal["one-sided", "two-sided-long-edge", "two-sided-short-edge"] = "one-sided",
    paper_size: Literal["A4", "Letter", "Legal", "A3", "Tabloid"] = "Letter",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate and print a received upload. Use dry_run=True to avoid CUPS entirely.

    Submission or any failure deletes the upload. A successful dry run retains it
    until expiry. Never automatically retry an uncertain CUPS submission.
    """
    store = _get_uploads()
    upload = store.claim(upload_id)
    try:
        assert upload.path is not None
        result = _print_file(upload.path, upload.content_type, page_range, copies, color, duplex, paper_size, dry_run)
    except BaseException:
        store.discard(upload)
        raise
    store.complete(upload, dry_run)
    return result


async def _receive_upload(request: Request) -> Response:
    upload = None
    try:
        store = _get_uploads()
        hosts = request.headers.getlist("host")
        if len(hosts) != 1:
            return Response(status_code=404)
        target = f"{urlsplit(store.base).scheme}://{hosts[0]}"
        if urlsplit(target).path or origin(target) != origin(store.base):
            return Response(status_code=404)
        upload = store.reserve(request.path_params["token"])
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError()
        declared_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if declared_type != upload.content_type:
            raise ValueError()
        length = request.headers.get("content-length")
        if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 20 or int(length) > upload.max_bytes):
            raise ValueError()
        assert upload.path is not None
        size = 0
        # Claim before reading: concurrent PUTs cannot overwrite the same file.
        fd = os.open(upload.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            with anyio.fail_after(60):
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > upload.max_bytes or time.time() >= upload.expires_at:
                        raise ValueError()
                    await anyio.to_thread.run_sync(stream.write, chunk)
        if length is not None and size != int(length):
            raise ValueError()
        path = upload.path
        info = await anyio.to_thread.run_sync(lambda: _document_info(path.read_bytes(), upload.content_type))
        store.finish(upload, info)
        return JSONResponse({"status": "received", **info}, headers={"Cache-Control": "no-store"})
    except Exception:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    finally:
        if upload is not None and upload.state == "receiving":
            store.discard(upload)


STATUS_ATTRIBUTES = frozenset({
    "printer-state", "printer-state-reasons", "printer-state-message",
    "marker-names", "marker-levels", "marker-types", "marker-colors",
    "printer-input-tray", "media-ready",
})


def _status_attributes(text: str) -> dict[str, Any]:
    """Read typed IPP arrays; display text loses commas, escaping and types."""
    try:
        report = plistlib.loads(text.encode())
        tests = report["Tests"]
        if report.get("Successful") is not True or len(tests) != 1 or tests[0].get("Successful") is not True:
            raise ValueError("Unsuccessful IPP query")
        attributes: dict[str, Any] = {}
        for group in tests[0]["ResponseAttributes"]:
            for key, value in group.items():
                if key in STATUS_ATTRIBUTES:
                    if key in attributes:
                        raise ValueError("Duplicate IPP attribute")
                    attributes[key] = value
        return attributes
    except Exception:
        raise ValueError("Invalid or unsuccessful IPP status response") from None


def _values(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([] if value is None else [value])


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _summarize_status(attributes: dict[str, Any], source: str) -> dict[str, Any]:
    arrays = {key: _values(attributes.get(key)) for key in
              ("marker-names", "marker-levels", "marker-types", "marker-colors")}
    names, levels = arrays["marker-names"], arrays["marker-levels"]
    aligned = bool(names) and len(names) == len(levels)
    notes = ["IPP values are device reports, not independent physical measurements.",
             "media_ready lists reported media sizes; it does not prove a tray contains paper."]
    if source == "cups-cache":
        notes.append("CUPS attributes may be cached; they are not fresh device readings.")
    if not aligned and any(arrays.values()):
        notes.append("Marker name/level arrays are missing or unequal; levels cannot be assigned safely.")
    supplies = []
    for index, name in enumerate(names):
        marker_type = arrays["marker-types"][index] if len(arrays["marker-types"]) == len(names) else None
        color = arrays["marker-colors"][index] if len(arrays["marker-colors"]) == len(names) else None
        level = levels[index] if aligned and type(levels[index]) is int else None
        percent = level if level is not None and 0 <= level <= 100 else None
        level_status = ("reported" if percent is not None else
                        {-1: "unavailable", -2: "unknown", -3: "unknown, not yet at capacity"}.get(level, "unknown") if level is not None else "unknown")
        accuracy = "reported by printer; measurement accuracy unknown"
        if _enabled("PRINT_INK_LEVELS_ESTIMATED") and marker_type == "ink":
            accuracy = "estimated by printer, may be stale; visually check tank"
        supplies.append({"index": index, "name": name, "type": marker_type, "color": color,
                         "reported_level": level, "level_percent": percent,
                         "level_status": level_status, "accuracy": accuracy})
    reasons = []
    for reason in _values(attributes.get("printer-state-reasons")):
        note = "Reported by printer; not independently verified."
        if str(reason).startswith("media-empty"):
            note = "Printer reports media empty; this does not confirm an empty tray."
            if _enabled("PRINT_MEDIA_REASONS_UNRELIABLE"):
                note += " This device's media reasons may be sticky or misreported; check trays and printer display."
        reasons.append({"reason": reason, "note": note})
    trays = []
    for value in _values(attributes.get("printer-input-tray")):
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        fields = dict(part.split("=", 1) for part in str(value).split(";") if "=" in part)
        level_text = fields.get("level", "")
        tray_level = int(level_text) if re.fullmatch(r"-?\d{1,10}", level_text) else None
        trays.append({"reported": fields, "level": tray_level,
                      "level_known": tray_level is not None and tray_level >= 0})
    return {"source": source, "printer_state": attributes.get("printer-state"),
            "printer_state_message": attributes.get("printer-state-message"),
            "reasons": reasons, "supplies": supplies, "marker_mapping_reliable": aligned,
            "markers_reported": bool(names or levels),
            "media_ready": _values(attributes.get("media-ready")), "input_trays": trays,
            "notes": notes}


def _query_status(uri: str, source: str) -> dict[str, Any]:
    parsed = urlsplit(uri)
    if parsed.scheme not in ("ipp", "ipps") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Status URI must be an IPP(S) device URI without credentials")
    output = run("ipptool", "-T", "10", "-X", uri, str(Path(__file__).with_name("status.test")))
    result = _summarize_status(_status_attributes(output), source)
    result["queried_at"] = int(time.time())
    return result


def printer_status() -> dict[str, Any]:
    """Query fresh device IPP status when PRINT_STATUS_URI is configured, alongside CUPS.

    Preserve device-reported estimates and reasons with qualifications. Cached CUPS
    values are labeled explicitly and never passed off as fresh physical readings.
    """
    result: dict[str, Any] = {"queue": QUEUE, "queue_state_source": "cups"}
    try:
        result["state"] = run("lpstat", "-h", CUPS, "-p", QUEUE, "-l")
    except (RuntimeError, OSError, TimeoutError):
        result["state"] = "CUPS queue state unavailable"
    cached: dict[str, Any] = {}
    try:
        cached = _query_status(f"ipp://{CUPS}/printers/{QUEUE}", "cups-cache")
        result["cups"] = cached
    except (RuntimeError, OSError, TimeoutError, ValueError):
        result["ipp_error"] = "CUPS IPP status unavailable"
    uri = os.environ.get("PRINT_STATUS_URI", "").strip()
    if uri:
        try:
            fresh = _query_status(uri, "printer")
            return {**result, **fresh, "fresh": True}
        except (RuntimeError, OSError, TimeoutError, ValueError):
            result["fresh_error"] = "Fresh printer IPP query failed; do not treat CUPS values as fresh"
    else:
        result["fresh_error"] = "PRINT_STATUS_URI is not configured; only cached CUPS status is available"
    return {**result, **(cached or _summarize_status({}, "unavailable")), "fresh": False}


def list_jobs(completed: bool = False) -> dict[str, str]:
    """List jobs in the configured queue, optionally including completed jobs."""
    return {"jobs": run("lpstat", "-h", CUPS, "-W", "all" if completed else "not-completed", "-o", QUEUE)}


def cancel_job(job_id: str) -> dict[str, str]:
    """Cancel one job in the configured queue using its queue-N id."""
    if not re.fullmatch(rf"{re.escape(QUEUE)}-[1-9]\d{{0,9}}", job_id):
        raise ValueError("Expected a configured-queue job id")
    run("cancel", "-h", CUPS, job_id)
    return {"cancelled": job_id}


def create_server() -> FastMCP:
    public_host = os.environ.get("PRINT_PUBLIC_HOST", "").strip()
    mcp = FastMCP("Epson print", host="127.0.0.1", port=8765,
                  stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(
                      enable_dns_rebinding_protection=True,
                      allowed_hosts=["127.0.0.1:*", "localhost:*"] + ([public_host] if public_host else []),
                      allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] + (["https://" + public_host] if public_host else [])))
    limiter = anyio.CapacityLimiter(2)
    def adapt(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs), limiter=limiter)
            except BaseException as exc:
                telemetry.failure(exc)
                raise
        return call
    for tool in (print_document, print_url, create_upload, print_upload, upload_status, printer_status, list_jobs, cancel_job):
        mcp.tool()(adapt(tool))
    mcp.custom_route("/upload/{token}", methods=["PUT"])(_receive_upload)
    telemetry.install(mcp, "printmcp", "0.1.0")
    return mcp


class AccessJWTMiddleware:
    """Validate Access assertions before the MCP transport can dispatch any tool."""

    def __init__(self, app: ASGIApp, team_domain: str, audience: str, upload_audience: str = "") -> None:
        self.app = app
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        self.upload_audience = upload_audience
        # Cache the set for five minutes, not individual keys indefinitely.
        self.jwks = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs",
                                   cache_jwk_set=True, cache_keys=False,
                                   lifespan=300, timeout=5)

    def validate(self, token: str, audience: str | None = None) -> None:
        if len(token) > 16384 or jwt.get_unverified_header(token).get("alg") != "RS256":
            raise jwt.InvalidTokenError("Expected RS256 assertion")
        key = self.jwks.get_signing_key_from_jwt(token)
        jwt.decode(token, key.key, algorithms=["RS256"], audience=audience or self.audience,
                   issuer=self.issuer, options={"require": ["aud", "iss", "exp"]})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            tokens = Headers(scope=scope).getlist("cf-access-jwt-assertion")
            try:
                if len(tokens) != 1 or not tokens[0]:
                    raise jwt.InvalidTokenError("Missing assertion")
                # JWKS retrieval is blocking: keep it off the event loop.
                if scope["path"].startswith("/upload/") and self.upload_audience:
                    await anyio.to_thread.run_sync(self.validate, tokens[0], self.upload_audience)
                else:
                    await anyio.to_thread.run_sync(self.validate, tokens[0])
            except jwt.PyJWTError:
                # Do not log assertions or return validation/network details.
                response = JSONResponse({"error": "Invalid or missing Access assertion"},
                                        status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app() -> Starlette:
    team = os.environ.get("ACCESS_TEAM_DOMAIN", "").strip()
    audience = os.environ.get("ACCESS_AUD", "").strip()
    if bool(team) != bool(audience):
        raise ValueError("Set both ACCESS_TEAM_DOMAIN and ACCESS_AUD, or neither")
    app = create_server().streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Starlette) -> Any:
        global _uploads
        async def sweep() -> None:
            while True:
                await anyio.sleep(30)
                if _uploads is not None:
                    await anyio.to_thread.run_sync(_uploads.sweep)
        async with original_lifespan(application) as state:
            async with anyio.create_task_group() as group:
                group.start_soon(sweep)
                try:
                    yield state
                finally:
                    group.cancel_scope.cancel()
            with _uploads_lock:
                store, _uploads = _uploads, None
            if store is not None:
                await anyio.to_thread.run_sync(store.close)
    app.router.lifespan_context = lifespan
    if team:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com", team):
            raise ValueError("ACCESS_TEAM_DOMAIN must be <team>.cloudflareaccess.com without a scheme or path")
        app.add_middleware(AccessJWTMiddleware, team_domain=team, audience=audience,
                           upload_audience=os.environ.get("PRINT_UPLOAD_ACCESS_AUD", ""))
    else:
        logging.getLogger(__name__).warning(
            "Access JWT validation is DISABLED: ACCESS_TEAM_DOMAIN and ACCESS_AUD are unset; "
            "localhost-only mode. Configure both before exposing the service.")
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=8765, access_log=False)
