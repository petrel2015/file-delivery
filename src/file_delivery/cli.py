"""Command line entry: file-delivery plan PATH [PATH ...] --root ROOT [--json]."""

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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = planning.plan(args.paths, args.root)
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
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
