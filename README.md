# printmcp

A Streamable HTTP MCP server for local CUPS printers, including Epson queues.
Tools: `print_document`, `print_url`, `printer_status`, `list_jobs`, and `cancel_job`. Print base64
PDFs or text/Markdown rendered as wrapped Unicode PDF; Markdown syntax is preserved.
URL downloads require an explicit origin allowlist; no caller-supplied filesystem paths. Printing consumes paper.

## Setup

Install Python 3.12+, uv, CUPS client tools (`lp`, `lpstat`, `cancel`), `ipptool`, and
DejaVu Sans Mono. Configure a working CUPS queue separately, using any suitable IPP,
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
  `/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf`.
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

## Print a PDF URL

`print_url(url, page_range=None, copies=1, color="monochrome",
duplex="one-sided", paper_size="Letter")` downloads a PDF service-to-service and
uses the same CUPS submission as `print_document`. Copies, color, duplex, paper and
page range have the same validation and behavior. The temporary PDF is removed on
success or failure. Never automatically retry an uncertain submission.

- `PRINT_URL_ALLOWED_ORIGINS`: comma-separated exact HTTP(S) origins, including
  nondefault ports, e.g. `https://pdf.example.com,http://private-host:8788`.
  Empty by default (URL fetching disabled). No wildcards, paths or credentials.
- `PRINT_URL_MAX_BYTES`: positive byte cap (default 52428800). Existing
  `print_document` PDF limits remain unchanged.

Only HTTP 200 with bytes starting `%PDF-` is accepted. All redirects are rejected,
including redirects within an allowed origin. Ambient proxies are disabled; HTTPS
uses certificate verification. Reads have a ten-second socket timeout and a
60-second overall budget checked between bounded reads. Declared and streamed
sizes are checked. Allowlist only trusted services: allowing an origin authorizes
its addresses and all its paths, and DNS is resolved by the normal system resolver.
Private network origins are deliberately supported.

Pass a short-lived capability URL directly from its issuing tool. The fetcher
never logs URLs or echoes network error details. Treat URLs as credentials and
redact them in upstream logs. If the issuer subsets pages, `page_range` here refers
to pages of that subset, starting at 1; usually leave it unset.
