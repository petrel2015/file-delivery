"""Command line entry: file-delivery plan/pack/verify subcommands."""

from __future__ import annotations

import argparse
import json
import sys

from file_delivery import errors, planning


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file-delivery",
        description="Offline read-only delivery manifest planner.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser('list-qiniu', aliases=['list'], help='list one live bucket page and correlate original filenames')
    list_parser.add_argument('--config', required=True)
    list_parser.add_argument('--state-dir')
    list_parser.add_argument('--prefix', default='')
    list_parser.add_argument('--marker', default='')
    list_parser.add_argument('--limit', type=int, default=100)
    list_parser.add_argument('--query', default='')
    list_parser.add_argument('--json', action='store_true')
    download_parser = subparsers.add_parser('download-qiniu', aliases=['download'], help='download a selected owned encrypted archive')
    download_parser.add_argument('--state-dir', required=True)
    download_parser.add_argument('--config', required=True)
    download_parser.add_argument('--key', required=True)
    download_parser.add_argument('--output-path', required=True)
    download_parser.add_argument('--json', action='store_true')
    plan_parser = subparsers.add_parser(
        "plan",
        help="plan a delivery manifest for the given paths",
    )
    plan_parser.add_argument("paths", nargs="+", metavar="PATH", help="input files or directories")
    plan_parser.add_argument("--root", required=True, metavar="ROOT", help="root directory of all inputs")
    plan_parser.add_argument("--json", action="store_true", help="emit the manifest as JSON (default)")

    pack_parser = subparsers.add_parser(
        "pack",
        help="pack inputs into a local AES-256 ZIP bundle",
    )
    pack_parser.add_argument("paths", nargs="+", metavar="PATH", help="input files or directories")
    pack_parser.add_argument("--root", required=True, metavar="ROOT", help="root directory of all inputs")
    pack_parser.add_argument("--output-dir", required=True, metavar="DIR", help="new bundle output directory")
    pack_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify an existing bundle without extracting it",
    )
    verify_parser.add_argument("bundle_dir", metavar="DIR", help="bundle directory to verify")
    verify_parser.add_argument("--password-file", metavar="FILE", help="password file (default: DIR/password.txt)")
    verify_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    deliver_parser = subparsers.add_parser(
        "deliver-local",
        help="idempotently package inputs and store a verified local object",
    )
    deliver_parser.add_argument("paths", nargs="+", metavar="PATH", help="input files or directories")
    deliver_parser.add_argument("--root", required=True, metavar="ROOT", help="root directory of all inputs")
    deliver_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private ledger state directory")
    deliver_parser.add_argument("--store-dir", required=True, metavar="DIR", help="local object store directory")
    deliver_parser.add_argument("--key", required=True, metavar="KEY", help="idempotency key ([A-Za-z0-9_-]{1,64})")
    deliver_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    status_parser = subparsers.add_parser(
        "status",
        help="show the persisted ledger task for a key",
    )
    status_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private ledger state directory")
    status_parser.add_argument("--key", required=True, metavar="KEY", help="idempotency key")
    status_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    deliver_qiniu_parser = subparsers.add_parser(
        "deliver-qiniu",
        help="idempotently deliver an encrypted bundle to a private Qiniu bucket",
    )
    deliver_qiniu_parser.add_argument("paths", nargs="+", metavar="PATH", help="input files or directories")
    deliver_qiniu_parser.add_argument("--root", required=True, metavar="ROOT", help="root directory of all inputs")
    deliver_qiniu_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private remote ledger state directory")
    deliver_qiniu_parser.add_argument("--config", required=True, metavar="FILE", help="owner-only Qiniu config JSON file")
    deliver_qiniu_parser.add_argument("--key", required=True, metavar="KEY", help="idempotency key ([A-Za-z0-9_-]{1,64})")
    deliver_qiniu_parser.add_argument("--ttl-seconds", type=int, default=604800, metavar="N", help="signed link lifetime in seconds (default 604800)")
    deliver_qiniu_parser.add_argument("--retention-days", type=int, default=30, metavar="N", help="remote object retention in days (default 30)")
    deliver_qiniu_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    status_qiniu_parser = subparsers.add_parser(
        "status-qiniu",
        help="show the persisted remote delivery task for a key",
    )
    status_qiniu_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private remote ledger state directory")
    status_qiniu_parser.add_argument("--key", required=True, metavar="KEY", help="idempotency key")
    status_qiniu_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    revoke_qiniu_parser = subparsers.add_parser(
        "revoke-qiniu",
        help="revoke a delivered Qiniu object for a ledger key",
    )
    revoke_qiniu_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private remote ledger state directory")
    revoke_qiniu_parser.add_argument("--config", required=True, metavar="FILE", help="owner-only Qiniu config JSON file matching the task destination")
    revoke_qiniu_parser.add_argument("--key", required=True, metavar="KEY", help="idempotency key of the task to revoke")
    revoke_qiniu_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    cleanup_qiniu_parser = subparsers.add_parser(
        "cleanup-qiniu",
        help="preview (or with --execute, perform) retention cleanup of delivered Qiniu objects",
    )
    cleanup_qiniu_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private remote ledger state directory")
    cleanup_qiniu_parser.add_argument("--config", metavar="FILE", help="owner-only Qiniu config JSON file; required with --execute")
    cleanup_qiniu_parser.add_argument("--execute", action="store_true", help="actually delete due objects (default is a read-only dry run)")
    cleanup_qiniu_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")
    send_email_parser = subparsers.add_parser(
        "send-email",
        help="email one verified remote handoff to a single mailbox over TLS SMTP",
    )
    send_email_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private remote ledger state directory")
    send_email_parser.add_argument("--delivery-key", required=True, metavar="KEY", help="idempotency key of the delivered remote task")
    send_email_parser.add_argument("--smtp-config", required=True, metavar="FILE", help="owner-only SMTP config JSON file")
    send_email_parser.add_argument("--to", required=True, metavar="RECIPIENT", help="recipient mailbox or contact alias")
    send_email_parser.add_argument("--key", required=True, metavar="KEY", help="notification idempotency key ([A-Za-z0-9_-]{1,64})")
    send_email_parser.add_argument("--contacts", metavar="FILE", help="owner-only contacts JSON file for alias resolution")
    send_email_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")

    status_email_parser = subparsers.add_parser(
        "status-email",
        help="show the persisted email notification for a key (offline)",
    )
    status_email_parser.add_argument("--state-dir", required=True, metavar="DIR", help="private remote ledger state directory")
    status_email_parser.add_argument("--key", required=True, metavar="KEY", help="notification idempotency key")
    status_email_parser.add_argument("--json", action="store_true", help="emit the result as JSON (default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in ('list', 'list-qiniu'):
            from file_delivery import cloud
            result = cloud.list_files(args.config, args.state_dir, prefix=args.prefix,
                marker=args.marker, limit=args.limit, query=args.query)
        elif args.command in ('download', 'download-qiniu'):
            from file_delivery import cloud
            result = cloud.download(args.state_dir, args.config, args.key, args.output_path)
        elif args.command == "plan":
            result = planning.plan(args.paths, args.root)
        elif args.command == "pack":
            from file_delivery import archive
            result = archive.pack(args.paths, args.root, args.output_dir)
        elif args.command == "verify":
            from file_delivery import archive
            result = archive.verify(args.bundle_dir, args.password_file)
        elif args.command == "deliver-local":
            from file_delivery import ledger
            result = ledger.deliver_local(
                args.paths, args.root, args.state_dir, args.store_dir, args.key)
        elif args.command == "deliver-qiniu":
            from file_delivery import remote
            result = remote.deliver(
                args.paths, args.root, args.state_dir, args.config, args.key,
                ttl_seconds=args.ttl_seconds, retention_days=args.retention_days)
        elif args.command == "send-email":
            from file_delivery import notification
            result = notification.send(
                args.state_dir, args.delivery_key, args.smtp_config, args.to,
                args.key, contacts_path=args.contacts)
        elif args.command == "status-email":
            from file_delivery import notification
            result = notification.status(args.state_dir, args.key)
        elif args.command == "status-qiniu":
            from file_delivery import remote
            result = remote.status(args.state_dir, args.key)
        elif args.command == "revoke-qiniu":
            from file_delivery import remote
            result = remote.revoke(args.state_dir, args.config, args.key)
        elif args.command == "cleanup-qiniu":
            from file_delivery import remote
            result = remote.cleanup(args.state_dir, args.config,
                                    dry_run=not args.execute)
        else:
            from file_delivery import ledger
            result = ledger.status(args.state_dir, args.key)
    except errors.DeliveryError as exc:
        print(json.dumps({"schema_version": planning.SCHEMA_VERSION,
                          "status": "error",
                          "error": {"code": exc.code, "message": exc.message}}))
        return 2
    except OSError as exc:
        print(json.dumps({"schema_version": planning.SCHEMA_VERSION,
                          "status": "error",
                          "error": {"code": errors.IO_ERROR,
                                    "message": f"io failure: {exc.strerror or exc}"}}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
