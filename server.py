"""Local CUPS MCP. Importing this module never contacts CUPS."""
from __future__ import annotations

import base64
import binascii
import logging
import os
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
import urllib.request
import urllib.error
from urllib.parse import urlsplit
import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from typing import Annotated
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


def fetch_pdf(url: str) -> bytes:
    """Exact-origin opt-in, no redirects/proxies/cookies, bounded bytes/time."""
    def origin(value: str) -> tuple[str, str, int]:
        if len(value) > 8192 or re.search(r"[\x00-\x20\x7f\\]", value):
            raise ValueError("Invalid PDF URL")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Invalid PDF URL")
        return parsed.scheme, parsed.hostname.lower(), parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)

    allowed = set()
    for entry in os.environ.get("PRINT_URL_ALLOWED_ORIGINS", "").split(","):
        if entry.strip():
            parsed = urlsplit(entry.strip())
            if parsed.path not in ("", "/") or parsed.query:
                raise ValueError("Allowlist entries must be origins")
            allowed.add(origin(entry.strip()))
    if origin(url) not in allowed:
        raise ValueError("PDF URL origin is not allowed")
    limit = int(os.environ.get("PRINT_URL_MAX_BYTES", str(50 * 1024 * 1024)))
    if limit <= 0:
        raise ValueError("PRINT_URL_MAX_BYTES must be positive")
    data = bytearray()
    deadline = time.monotonic() + 60
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None

    # urllib does not log request URLs, including capability credentials. Disable
    # ambient proxies and redirects; HTTPS retains the default certificate checks.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/pdf"})
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError("PDF download requires HTTP 200")
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
    except (OSError, urllib.error.URLError, http.client.HTTPException, UnicodeError):
        raise ValueError("PDF download failed; redirects are refused") from None
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
) -> dict[str, str]:
    """Fetch a PDF from an allowed origin and print its original layout. Consumes paper.

    Supply a short-lived PDF URL directly, never base64 through the model. page_range
    refers to pages in the downloaded PDF (already subset URLs start at page 1).
    Do not retry an uncertain submission; inspect list_jobs first.
    """
    _validate_options(copies, duplex, color, page_range, paper_size)
    pdf = fetch_pdf(url)
    with tempfile.TemporaryDirectory(prefix="fleet-print-") as directory:
        path = Path(directory) / "document.pdf"
        path.write_bytes(pdf)
        return _submit(path, copies, duplex, color, page_range, paper_size)


def printer_status() -> dict[str, object]:
    """Return local queue state/errors and IPP markers when CUPS reports them."""
    state = run("lpstat", "-h", CUPS, "-p", QUEUE, "-l")
    try:
        attributes = run("ipptool", "-T", "10", "-tv", f"ipp://{CUPS}/printers/{QUEUE}",
                         str(Path(__file__).with_name("status.test")))
        markers = [line.strip() for line in attributes.splitlines()
                   if re.search(r"marker-|printer-state(?:-reasons|-message)?\s", line)]
        return {"queue": QUEUE, "state": state, "ipp": markers,
                "markers_reported": any("marker-levels" in line for line in markers)}
    except (RuntimeError, OSError) as exc:
        return {"queue": QUEUE, "state": state, "ipp_error": str(exc), "markers_reported": False}


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
    for tool in (print_document, print_url, printer_status, list_jobs, cancel_job):
        mcp.tool()(adapt(tool))
    telemetry.install(mcp, "printmcp", "0.1.0")
    return mcp


class AccessJWTMiddleware:
    """Validate Access assertions before the MCP transport can dispatch any tool."""

    def __init__(self, app: ASGIApp, team_domain: str, audience: str) -> None:
        self.app = app
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        # Cache the set for five minutes, not individual keys indefinitely.
        self.jwks = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs",
                                   cache_jwk_set=True, cache_keys=False,
                                   lifespan=300, timeout=5)

    def validate(self, token: str) -> None:
        if len(token) > 16384 or jwt.get_unverified_header(token).get("alg") != "RS256":
            raise jwt.InvalidTokenError("Expected RS256 assertion")
        key = self.jwks.get_signing_key_from_jwt(token)
        jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audience,
                   issuer=self.issuer, options={"require": ["aud", "iss", "exp"]})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            tokens = Headers(scope=scope).getlist("cf-access-jwt-assertion")
            try:
                if len(tokens) != 1 or not tokens[0]:
                    raise jwt.InvalidTokenError("Missing assertion")
                # JWKS retrieval is blocking: keep it off the event loop.
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
    if team:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com", team):
            raise ValueError("ACCESS_TEAM_DOMAIN must be <team>.cloudflareaccess.com without a scheme or path")
        app.add_middleware(AccessJWTMiddleware, team_domain=team, audience=audience)
    else:
        logging.getLogger(__name__).warning(
            "Access JWT validation is DISABLED: ACCESS_TEAM_DOMAIN and ACCESS_AUD are unset; "
            "localhost-only mode. Configure both before exposing the service.")
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=8765)
