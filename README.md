# printmcp

A Streamable HTTP MCP server for local CUPS printers, including Epson queues.
Tools: `print_document`, `print_url`, `create_upload`, `print_upload`,
`upload_status`, `printer_status`, `list_jobs`, and `cancel_job`. Prefer `print_url`
when a source provides a public PDF link. For documents a connector can return as
text or Markdown, use `print_document(format="markdown")` for a formatted PDF.
Base64 `format="pdf"` suits small PDFs or exact layout: every base64 character
passes through the model's output, so large payloads are slow.
URL downloads accept public HTTPS links by default; no caller-supplied filesystem paths. Printing consumes paper.

## Setup

Install Python 3.12+, uv, CUPS client tools (`lp`, `lpstat`, `cancel`), `ipptool`, and
DejaVu Sans and DejaVu Sans Mono (`fonts-dejavu-core` usually supplies both). Configure a working CUPS queue separately, using any suitable IPP,
USB or vendor driver. This project does not change printer/CUPS configuration.

```sh
uv sync --locked
export PRINT_QUEUE=printer
uv run --locked python server.py
```

It binds only `127.0.0.1:8765`, endpoint `/mcp`. `.env.example` documents settings;
load them through your process manager or export them; dotenv is not automatic.

## Configuration

- `PRINT_QUEUE`: CUPS queue. Legacy default `epson` preserves existing deployments;
  set your own queue explicitly for other printers. No printer IP/model is required.
- `PRINT_CUPS_SERVER`: default `localhost:631`. Commands ignore ambient `CUPS_*`
  overrides and use only this explicit setting. Keep CUPS local/private.
- `PRINT_FONT`: TrueType font path, default
  `/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf`; used for wrapped text
  and Markdown code.
- `PRINT_BODY_FONT`: Markdown proportional TrueType font, default
  `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`. Optional
  `PRINT_BODY_FONT_BOLD`, `PRINT_BODY_FONT_ITALIC`, and
  `PRINT_BODY_FONT_BOLD_ITALIC` select its variants. Defaults use the corresponding
  DejaVu Sans files; a custom body font without variants uses the same face for
  all styles. Missing/invalid Markdown fonts fall back to Helvetica/Courier
  (with reduced Unicode coverage); missing variants use the body face.
- `PRINT_PUBLIC_HOST`: exact public hostname (optional port), without scheme/path.
  No public hostname is allowed by default; matching HTTPS Origin is allowed when set.
  Existing localhost host/origin checks remain enabled.
- `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD`: both or neither; when configured, every
  HTTP request requires a valid Cloudflare Access RS256 assertion with matching
  issuer/audience and expiry. JWKS retrieval fails closed. The listener stays loopback.

When migrating a deployment with a previously hard-coded public hostname, set
`PRINT_PUBLIC_HOST` to that same hostname before switching code. All tools, limits,
print options, queue defaults, loopback ports and Access behavior are preserved.

## Security and operation

Expose only through authenticated routing; without Access settings, localhost is
trusted and startup logs a warning. Never expose CUPS administration or the listener
directly. Cancellation accepts one job in the configured queue, never all jobs.
Temporary PDFs are removed after submission or failure; CUPS separately retains its
spool/history. A submission timeout may mean a job was accepted: inspect `list_jobs`
before retrying. PDFs are limited to 10 MiB, text to 200,000 characters, copies to
1–100, and commands to 30 seconds. Printer capabilities determine supported options.
Ink markers are reported only when available, never guessed. Credentials, client
assertions and documents belong outside the repo. No automated test submits a job.

## Development

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest -q
```

Tests mock CUPS and JWKS. MIT licensed. Printer setup, service accounts, systemd,
containers, tunnels, Access applications and deployment installers belong in the
operator's deployment repository.

## Telemetry

Enabled by default; set `MCP_TELEMETRY=0` (or `false`/`off`) to disable.
Stderr receives one `mcp_call ` prefix followed by a JSON object per dispatched
call, and one `mcp_start ` record when the MCP lifespan is ready:

```json
{"event":"mcp_call","v":1,"ts":"<UTC ISO>","service":"printmcp","tool":"<registered name>","outcome":"ok|tool_error|error|timeout|refused","error_class":null,"total_ms":0.0,"phase_ms":{"serialize":0.0},"req_bytes":0,"resp_bytes":0}
{"event":"mcp_start","v":1,"service":"printmcp","version":"<package version>","startup_ms":0.0}
```

Durations use a monotonic clock. Startup includes process imports on Linux
(import-to-ready fallback elsewhere). Byte counts measure UTF-8 JSON MCP params
and normalized results, excluding the JSON-RPC envelope. Total time includes
result normalization and a serialization pass; transport delivery is excluded.
Arguments, results and exception messages are never emitted. Unknown tool names
become `unknown`; unknown exception types become `exception`. Returned MCP error
results are `tool_error`; raised exceptions are `error`, with timeouts and
permission failures classified separately. Error classes are fixed:
`ValueError`, `TypeError`, `OSError`, `TimeoutError`, `PermissionError`, `Busy`,
`NotFound`, `ToolError`, `CancelledError`, or `exception` (null on success).
Only measured phases appear; nested occurrences of the same phase count once.

Call phases: `render`, `cups`, `status`, `serialize`.

## Markdown and text documents

`print_document(content, format="markdown", dry_run=True)` prepares a formatted
PDF and returns page count, selected pages, print options and prepared byte count
without calling CUPS. Set `dry_run=False` (the default) to print. The same dry-run
option works with `format="text"` and base64 `format="pdf"`; page ranges are checked
against the prepared document. Temporary files are removed after either path.

Markdown supports headings, paragraphs, bold/italic, inline code, nested bulleted
and numbered lists, block quotes, fenced code, horizontal rules and simple GFM
tables with repeated headers. Long content paginates; code lines wrap to fit.
Links print their visible text and images print their alt text. HTML is literal,
all user text is escaped for PDF markup, and images/URLs are never fetched by the
renderer. Unicode coverage depends on installed fonts; use DejaVu Sans for the
body and DejaVu Sans Mono for code. `format="text"` keeps the existing wrapped
monospace source behavior, including literal Markdown syntax.

## Print a PDF URL

`print_url(url, page_range=None, copies=1, color="monochrome",
duplex="one-sided", paper_size="Letter", dry_run=False)` prints any PDF from a
public https link, e.g. a short-lived download link another service gives you.
It uses the same CUPS submission as `print_document`. PDF bytes remain unchanged.

- `PRINT_URL_ALLOWED_ORIGINS`: optional comma-separated exact HTTPS origins,
  including nondefault ports. **Unset accepts any public HTTPS origin**; set
  restricts fetching to those origins. An explicitly empty value refuses all
  origins. This restriction never permits private addresses or HTTP.
- `PRINT_URL_MAX_BYTES`: positive byte cap (default 52428800). Existing
  `print_document` PDF limits remain unchanged.

Before connecting, resolve the hostname once and reject the entire answer set
if any address is non-global, multicast, reserved, or an IPv4-mapped/tunnel
form. Loopback, private, link-local, CGNAT and IPv6 ULA addresses are refused.
TCP connects directly to a vetted numeric address; TLS SNI, certificate checks
and HTTP Host retain the original hostname. No second hostname lookup occurs
when connecting, so DNS rebinding cannot substitute a private address.

No request authentication, redirects, proxies or cookies are supported. Only
HTTP 200 with bytes starting `%PDF-` is accepted. Reads have a ten-second socket
timeout and a 60-second budget checked between connection attempts and reads.
Declared and streamed sizes are checked. Network errors do not echo the URL.

`dry_run=True` fetches, tolerantly parses the PDF, counts/selects pages and returns
print options and prepared byte count **without invoking CUPS**. Encrypted,
empty, unparseable or over-10,000-page PDFs are rejected. Successful non-dry
calls submit through the shared path and remove temporary files on completion
or failure. Never automatically retry an uncertain submission. If the downloaded
PDF is already a subset, its page numbers start at 1.

Treat short-lived URLs as credentials and redact them in upstream logs. For a
private or authenticated source, upload its bytes through the generic upload
flow instead of passing source credentials to the printer.

## Fresh printer status and uncertain readings

`printer_status()` queries CUPS and, when `PRINT_STATUS_URI` is set, sends a fresh
IPP Get-Printer-Attributes directly to that configured device endpoint on every
call. Configure an `ipp://` or `ipps://` URI without credentials; it must target
the physical printer or its local IPP bridge, not the CUPS queue. No print, reset,
maintenance or queue-changing operation is used.

The result separates `source` (`printer`, `cups-cache`, or `unavailable`), `fresh`,
`queried_at`, the CUPS queue's `state`/`queue_state_source`, and a `cups` snapshot.
A direct-query failure is explicit in `fresh_error`; cached CUPS readings remain
labeled. Typed XML plist output from ipptool preserves marker array positions,
commas in names and single-marker values. `supplies` pairs each marker's name,
color, type, reported level and optional `level_percent` by index. Unequal
name/level arrays are flagged and no percentage is assigned to a name. Negative
sentinels and unsupported/missing values remain unknown, never zero percent.

Printer reasons, `media_ready`, and `input_trays` are returned separately. Media
sizes and status reasons do not independently establish whether paper is present;
unknown tray levels remain unknown. Reasons are never silently removed.

Optional operator knowledge belongs in deployment configuration, not model-name
checks in this application:

- `PRINT_INK_LEVELS_ESTIMATED=1`: qualify ink markers as
  "estimated by printer, may be stale; visually check tank". Without this flag,
  measurement accuracy is explicitly unknown. Values are never recalibrated.
- `PRINT_MEDIA_REASONS_UNRELIABLE=1`: explain that media-empty reasons on this
  device may be sticky or misreported, while retaining the original reason.

The endpoint and flags are operator-set environment values, not tool arguments.
CUPS status remains available when no device endpoint is configured. Fresh-query
and CUPS subprocesses retain bounded time/output limits. No tests contact a
printer or create, cancel or modify a real print job.

## Single-use uploads on the print origin

Tools:

- `create_upload(content_type="pdf"|"png"|"jpeg", max_bytes=None)` returns
  `upload_id`, `url` (single-use PUT), `expires_at`, type and maximum bytes.
- `upload_status(upload_id)` returns `pending`, `received`, `printed` or `expired`;
  received uploads include byte count, MIME type and page count.
- `print_upload(upload_id, page_range=None, copies=1, color="monochrome",
  duplex="one-sided", paper_size="Letter", dry_run=False)` validates and submits
  through the same CUPS path as other tools. PNG/JPEG images are centered and fit
  inside the selected paper with 36-point margins; PDF layout is unchanged.

Configuration:

- `PRINT_UPLOAD_SECRET`: independent random HMAC signing secret, at least 32 bytes,
  supplied privately through the process environment. Never commit its value.
- `PRINT_UPLOAD_BASE_URL`: HTTP(S) print origin, with no path/query/credentials.
- `PRINT_UPLOAD_MAX_BYTES`: positive per-upload ceiling, default 52428800 (50 MiB).
- `PRINT_UPLOAD_MAX_PENDING`: active uploads (pending/receiving/received/printing),
  default 8. Terminal records are additionally bounded to four times this count.
- `PRINT_UPLOAD_ACCESS_AUD`: optional audience for a later separate Access app on
  `/upload/*`. When unset, upload PUTs require the existing `ACCESS_AUD` like MCP.
  When set, only upload-path requests use that audience; MCP keeps its own audience.

The external client sends the bytes directly with `PUT /upload/{token}` and the
matching `Content-Type` (`application/pdf`, `image/png`, or `image/jpeg`). The token
binds upload ID and a ten-minute expiry. Uploads also remain behind Access JWT
validation when Access is configured; possession of a URL alone does not bypass
it. No upload path is automatically exempted and no CORS permissions are added.

The receiver claims the token before reading, writes mode-0600 files into a
private mode-0700 temporary directory, checks declared and streamed size, then
sniffs and parses the declared type. Images must be single-frame and no larger
than 20 million pixels. Compressed HTTP bodies are rejected. A PUT gets a
60-second receive budget and cannot outlive the capability. Failed/partial PUTs
are consumed and discarded; invalid, expired and reused tokens return 404.
Concurrent PUT/print calls cannot overwrite or submit the same upload twice.

Submission or any preparation/submission failure deletes the file immediately.
A successful dry run retains the received upload until expiry so it can be
inspected before an authorized print. A 30-second lifespan sweep deletes expired
files; printed/expired status records are retained for ten minutes then removed.
Normal application shutdown removes all payloads. Storage and IDs are ephemeral,
for one server process; restarts invalidate pending uploads. Do not run multiple
workers with this in-memory store. A hard crash relies on the operating system's
private temporary-directory cleanup (the fleet unit uses `PrivateTmp`).

Treat PUT URLs as credentials. The server disables HTTP access logging; upstream
proxies/Access analytics must redact token paths. Never publish a PUT URL or put
it into shared logs. Validate using dry runs; a failed/uncertain CUPS submission
must never be automatically retried. Tests mock all CUPS commands and submit no
real jobs.
