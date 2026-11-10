# printmcp

A Streamable HTTP MCP server for local CUPS printers, including Epson queues.
Tools: `print_document`, `printer_status`, `list_jobs`, and `cancel_job`. Print base64
PDFs or text/Markdown rendered as wrapped Unicode PDF; Markdown syntax is preserved.
No URL fetching or caller-supplied filesystem paths. Printing consumes paper.

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
