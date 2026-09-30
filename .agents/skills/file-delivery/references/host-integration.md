# Host integration and configuration

## Host-neutral entry points

The business core and this Skill have no ChatGPT/Codex, Hermes, Claude or model dependency. Use the installed CLI with any authorized command runner, or connect any compatible MCP stdio client to the same installed `file-delivery-mcp` command. Private file reads are required to present the protected URL/password to the intended recipient; do not put credentials in host prompts or MCP environment variables.

Install this Skill folder in the host's supported discovery location or provide its `SKILL.md` as workflow instructions. Skill directory layout, trust controls and invocation syntax differ by host; consult that host's actual configuration. Installing in Codex is one adapter, not a requirement for the core. Keep one canonical project Skill and copy it to selected host locations after validation. The Hermes commands below are a qualified host example, not a universal prerequisite or commands for Claude/Codex.

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

Cloud operations: `list` / `list-qiniu --config FILE [--state-dir DIR] [--prefix PREFIX] [--query TEXT] [--marker MARKER] [--limit 100]`; `download` / `download-qiniu --state-dir DIR --config FILE --key DELIVERY --output-path NEW_ZIP`; `renew-link --state-dir DIR --config FILE --delivery-key DELIVERY --key LINK_VERSION [--ttl-seconds 604800]`; `status-link --state-dir DIR --delivery-key DELIVERY --key LINK_VERSION`. Email accepts optional `--link-key LINK_VERSION`. MCP tools are `list_qiniu`, `download_qiniu`, `renew_link`, `status_link` and `send_email(link_key=...)`; 15 business tools in total. Signed-link versions are immutable and protected under `remote-links`; no source-path dependency or re-upload for an owned cloud selection.

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

## Deterministic host qualification

Run `scripts/verify_hermes_host.py --hermes-source /absolute/hermes-agent` using Hermes's own Python after trust and MCP setup. It reads the exact project Skill, connects only file-delivery, invokes temporary local plan/delivery/status, verifies duplicate reuse and TASK_NOT_FOUND, then closes its connection. It makes no inference or provider calls. Hermes may wrap text JSON under `result` and error JSON under `error`; unwrap those host envelopes when checking core codes. The previous host qualification had 11 business tools; the current server has 15 and requires refreshed host qualification. Hermes can register additional protocol utility tools.

## Claude adapters

Claude Code supports project `.claude/skills/file-delivery/SKILL.md` or user `~/.claude/skills/file-delivery/SKILL.md`; copy the whole canonical Skill folder, including references. Register the existing installed command with `claude mcp add --transport stdio --scope project file-delivery -- /absolute/project/.venv/bin/file-delivery-mcp`, then use `/mcp` to inspect connection and tools, followed by a permitted local `plan` call. Project server trust is a host action; do not bypass its controls. No model inference is required for configuration alone.

Claude Desktop uses its own `mcpServers` configuration, separate from Claude Code. A portable example is provided in the business project's `docs/examples/claude-mcp.json`; replace the command with the actual installed absolute path. The server stays stdio; credentials are not stored in the MCP config. Loading a Skill, protocol compatibility, actual Claude tool calls and model-driven delivery are separate evidence gates. The current machine has no discovered Claude executable/app, so no actual Claude host acceptance is claimed.

Official host references: [Claude Code MCP](https://code.claude.com/docs/en/mcp), [Claude Code Skills](https://code.claude.com/docs/en/skills).
