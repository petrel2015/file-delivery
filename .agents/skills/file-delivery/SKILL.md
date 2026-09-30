---
name: file-delivery
description: Encrypt and deliver selected local files through the file-delivery CLI or MCP, including private Qiniu links, one-recipient email, saved status and retention cleanup.
---

# File delivery

Use the installed `file-delivery` CLI or configured `file-delivery` MCP server. Both share the same Python core and private SQLite ledgers. Read [host integration](references/host-integration.md) for installation, schemas and host diagnostics. CLI `--help` is authoritative for options.

## Carry out a requested delivery

- Determine the selected input paths and root, private state directory, requested destination and existing operation key. Ask only for missing paths, configuration or recipient and genuinely missing external-operation authority; never ask for raw credentials. Respect already authorized actions. Keep configuration, state, objects and output bundles outside the input root. Do not select an entire repository containing `.git`.
- `plan` validates paths and records hashes without modifying inputs. `pack` creates an AES256 ZIP with a random password in a new private bundle; `verify` decrypts and compares every file. A verified local bundle is `packaged`, not remote delivery.
- For local storage use `deliver_local` / CLI `deliver-local` with `paths,root,state_dir,store_dir,key`; `status_local` / CLI `status` reads state offline. `stored-local` means a verified local copy.
- For Qiniu use `deliver_qiniu` / CLI `deliver-qiniu` with `paths,root,state_dir,config_path,key`. Defaults are link TTL 604800 seconds and object retention 30 days; use requested values. Only `link-verified` proves a signed download was read and checked. `status_qiniu` / CLI `status-qiniu` reports persisted history, not a fresh availability probe.
- Preserve the original key for retries/recovery. Repeated identical delivery reuses the task, ZIP and password. `IDEMPOTENCY_CONFLICT` means the request changed: explain it and create a new operation only if the user wants a new delivery. Never regenerate a durable bundle after a transient verification failure.
- When the user requested a successful private delivery, read the existing `handoff_file` and `password_file` through the host's authorized file-reading tool and present link, password, ZIP size and expiry in that private reply. The reply/session intentionally retains the delivery secret. Keep ordinary diagnostics, audit logs and Git free of URLs/passwords; never reveal SMTP/Qiniu credentials. If the host cannot read those protected files, return their paths and explicitly report the missing presentation capability.
- If email was requested, call `send_email` / CLI `send-email` using the existing `delivery_key`, a stable `notification_key`, `smtp_config`, and one `recipient` (ASCII mailbox or Unicode contact alias with `contacts_path`). The core builds and sends the message; do not copy credentials/body into commands or implement another SMTP flow. `channel-accepted` proves SMTP acceptance only. Receipt and reading remain unverified.

## Diagnose without duplicating effects

`status_email` / CLI `status-email` needs only state and notification key. For `SMTP_UNKNOWN`, inspect that original notification and stop; never resend or invent a new key to bypass uncertainty. A saved `sending` state recovers to `unknown`. Accepted notifications never resend, even after the link expires. A confirmed `failed-before-send` can be retried explicitly with the same key after fixing its cause.

For `REMOTE_UNKNOWN`, inspect the original remote task before any further action; do not create a replacement key or blindly replay uploads. `LINK_EXPIRED` and `TASK_REVOKED` stop email. `STATE_INVALID` means preserve artifacts and repair/diagnose private state; do not erase the ledger. `BUSY` means another process holds a bounded lock; report it rather than run repeated polls. `CONFIG_INVALID`, `CONTACTS_INVALID`, `CONTACT_NOT_FOUND`, `RECIPIENT_INVALID` and `DEPENDENCY_MISSING` identify local setup issues before transport. Preserve error codes; exclude raw exception text/secrets from reports.

## Revoke and clean up

`revoke_qiniu` / CLI `revoke-qiniu` deletes only the ledger-owned object. Report `object-deleted` and original link availability separately: CDN caches and previously downloaded copies are not recalled. `cleanup_qiniu` / CLI `cleanup-qiniu` defaults to a dry run; inspect retention candidates first. Actual cleanup uses `dry_run=false` / `--execute` only within the user's deletion request. Link expiry and object retention are separate deadlines.

A configured tool list does not prove autonomous delivery. Host integration requires actual skill loading, MCP connection and a permitted call. Model-driven workflow testing requires an authorized inference resource; record it separately from deterministic tool tests and real provider acceptance.
