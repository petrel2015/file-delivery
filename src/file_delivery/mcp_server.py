"""Optional stdio MCP adapter. Business state and retries belong to the core."""
import json
import sys

from . import archive, errors, ledger, notification, planning, remote


def create_server():
    from mcp.server import MCPServer
    from mcp.types import CallToolResult, TextContent, ToolAnnotations
    from pydantic import StrictBool, StrictInt, StrictStr

    server = MCPServer('file-delivery')
    known_codes = {v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str)}

    def call(fn, **kwargs):
        try:
            value = fn(**kwargs)
        except errors.DeliveryError as exc:
            code = exc.code if exc.code in known_codes else 'INTERNAL_ERROR'
            value = {'error': {'code': code, 'message': 'Delivery operation failed; inspect the error code and existing task status.'}}
            failed = True
        except Exception:
            value = {'error': {'code': 'INTERNAL_ERROR', 'message': 'Delivery operation failed.'}}
            failed = True
        else:
            failed = False
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(value, ensure_ascii=False))],
                              structuredContent=value, isError=failed)

    def register(fn, *, readonly=False, external=False):
        server.add_tool(fn, annotations=ToolAnnotations(readOnlyHint=readonly,
                        destructiveHint=not readonly, idempotentHint=readonly,
                        openWorldHint=external))
        # The qualified SDK 2.2.0 ignores extra kwargs by default. Forbid them
        # in its argument model and advertise the same schema to clients.
        tool = server._tool_manager.get_tool(fn.__name__)
        model = tool.fn_metadata.arg_model
        model.model_config['extra'] = 'forbid'
        model.model_rebuild(force=True)
        tool.parameters = model.model_json_schema(by_alias=True)

    def plan(paths: list[StrictStr], root: StrictStr) -> CallToolResult:
        """Validate selected paths and return a read-only SHA-256 manifest."""
        return call(planning.plan, paths=paths, root=root)

    def pack(paths: list[StrictStr], root: StrictStr, output_dir: StrictStr) -> CallToolResult:
        """Create a new AES ZIP bundle; return private paths, never password bytes."""
        return call(archive.pack, paths=paths, root=root, output_dir=output_dir)

    def verify(bundle_dir: StrictStr, password_file: StrictStr | None = None) -> CallToolResult:
        """Decrypt and verify an existing bundle without creating a new password."""
        return call(archive.verify, bundle_dir=bundle_dir, password_file=password_file)

    def deliver_local(paths: list[StrictStr], root: StrictStr, state_dir: StrictStr,
                      store_dir: StrictStr, key: StrictStr) -> CallToolResult:
        """Idempotent local delivery using a stable user operation key."""
        return call(ledger.deliver_local, paths=paths, root=root, state_dir=state_dir, store_dir=store_dir, key=key)

    def status_local(state_dir: StrictStr, key: StrictStr) -> CallToolResult:
        """Read persisted local delivery state, without accessing original inputs."""
        return call(ledger.status, state_dir=state_dir, key=key)

    def deliver_qiniu(paths: list[StrictStr], root: StrictStr, state_dir: StrictStr,
                      config_path: StrictStr, key: StrictStr,
                      ttl_seconds: StrictInt = 604800, retention_days: StrictInt = 30) -> CallToolResult:
        """Upload to private Qiniu storage and verify signed download; return protected handoff paths."""
        return call(remote.deliver, paths=paths, root=root, state_dir=state_dir,
                    config_path=config_path, key=key, ttl_seconds=ttl_seconds, retention_days=retention_days)

    def status_qiniu(state_dir: StrictStr, key: StrictStr) -> CallToolResult:
        """Read remote delivery state offline; this is not a new network probe."""
        return call(remote.status, state_dir=state_dir, key=key)

    def revoke_qiniu(state_dir: StrictStr, config_path: StrictStr, key: StrictStr) -> CallToolResult:
        """Delete the owned remote object and separately probe the original link."""
        return call(remote.revoke, state_dir=state_dir, config_path=config_path, key=key)

    def cleanup_qiniu(state_dir: StrictStr, config_path: StrictStr | None = None,
                      dry_run: StrictBool = True) -> CallToolResult:
        """List expired retention candidates; dry_run=false deletes eligible owned objects."""
        return call(remote.cleanup, state_dir=state_dir, config_path=config_path, dry_run=dry_run)

    def send_email(state_dir: StrictStr, delivery_key: StrictStr, smtp_config: StrictStr,
                   recipient: StrictStr, notification_key: StrictStr,
                   contacts_path: StrictStr | None = None) -> CallToolResult:
        """Send one recipient the existing protected delivery; SMTP_UNKNOWN must never be resubmitted."""
        return call(notification.send, state_dir=state_dir, delivery_key=delivery_key, smtp_config=smtp_config,
                    recipient=recipient, notification_key=notification_key, contacts_path=contacts_path)

    def status_email(state_dir: StrictStr, notification_key: StrictStr) -> CallToolResult:
        """Read persisted email state; channel acceptance does not prove receipt or reading."""
        return call(notification.status, state_dir=state_dir, notification_key=notification_key)

    for fn in (plan, verify, status_local, status_qiniu, status_email):
        register(fn, readonly=True)
    for fn in (pack, deliver_local):
        register(fn)
    for fn in (deliver_qiniu, revoke_qiniu, cleanup_qiniu, send_email):
        register(fn, external=True)
    return server


def main():
    try:
        server = create_server()
    except ImportError:
        print('MCP dependencies unavailable; install file-delivery[mcp].', file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
