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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
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
