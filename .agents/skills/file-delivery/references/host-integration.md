# Host integration and configuration

Python 3.11+; from the business project install into its dedicated environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[archive,qiniu,mcp]'
```

The official optional SDK is pinned to `mcp==2.2.0`. Base CLI planning needs no third-party runtime dependency. `file-delivery-mcp` speaks stdio only. Configure an absolute installed command; do not add credentials to MCP environment or arguments. Do not upgrade Hermes's own dependencies to install this server.

For Hermes versions supporting project skills and MCP:

```sh
hermes skills trust /absolute/business/project
hermes mcp add file-delivery --command /absolute/business/project/.venv/bin/file-delivery-mcp
hermes mcp test file-delivery
```

Preserve existing host configuration, back it up privately before edits, and verify the exact loaded skill path/content. Verify tool discovery and one permitted `plan` call, then a local delivery/status roundtrip. Modern SDK client tests and legacy initialize/tools/list/tools/call compatibility are separate protocol checks. Discovery alone is not a successful model-driven delivery. Before using a model, verify the user's authorized account/model/channel and save actual usage; unknown money remains unknown.

## CLI and MCP routing

MCP uses underscores, CLI uses hyphens. CLI input paths are positional; it accepts `--root`, `--state-dir`, `--store-dir`, `--key`, `--config`, `--ttl-seconds`, `--retention-days`, `--json` as applicable. `pack` adds `--output-dir`; `verify` accepts `--password-file`. `send-email` uses `--delivery-key`, `--smtp-config`, `--to`, `--key` (notification key), optional `--contacts`. Consult each command's `--help` rather than guessing flags. MCP rejects unknown/missing/wrong primitive parameters, including booleans in integer policy values. No tool accepts secret bytes or transport/store injection.

## Private configuration

Config/password files must be owner-private regular files (0600), not symlinks. State and artifact directories are private (0700). Qiniu config has exactly `access_key,secret_key,bucket,region,download_domain,timeout_seconds`; domain is an HTTPS origin without credentials/path/query. Supported regions: z0,z1,z2,na0,as0,cn-east-2. The bucket must be private. Ask for an existing config path, not its contents.

SMTP config:

```json
{
  "schema_version": 1,
  "host": "smtp.example.com",
  "port": 465,
  "tls": "implicit",
  "username": "sender@example.com",
  "password_file": "smtp-password.txt",
  "from_address": "sender@example.com",
  "timeout_seconds": 30
}
```

`tls` is `implicit` or mandatory `starttls`. Password path may be relative to config; its contents stay private and one terminal newline is removed. No plaintext TLS fallback. Timeout is finite 1–60 seconds. Contacts file: `{"schema_version":1,"contacts":{"同事":"reader@example.com"}}`; aliases match exactly, one mailbox per alias. No display names, address lists or header injection.

## Diagnosing setup

Missing MCP extra: server exits 2, sanitized stderr, no stdout garbage. Connection failure: verify absolute executable path, optional dependencies and host MCP diagnostics. Use `file-delivery plan` to separate core installation from MCP. Core tools return structured `{error:{code,message}}`; their diagnostic messages are sanitized. Never log SMTP body, signed URL, password, full configuration or raw provider replies. Do not substitute cached discovery for an actual permitted call.
