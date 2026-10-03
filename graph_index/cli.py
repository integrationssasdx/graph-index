"""graph-index command line interface."""

from __future__ import annotations

import argparse
import json
import sys

from .errors import PlanError
from .planner import Planner
from .schema import load_schema


def _read_text_file(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise PlanError("IoError", f"cannot read '{path}': {exc.strerror or exc}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PlanError("IoError", f"'{path}' is not valid UTF-8")


def _cmd_query_plan(args) -> dict:
    schema_text = _read_text_file(args.schema)
    query_text = _read_text_file(args.query)
    variables_text = _read_text_file(args.variables)

    schema = load_schema(schema_text, args.schema)

    try:
        variables = json.loads(variables_text)
    except json.JSONDecodeError as exc:
        raise PlanError(
            "ParseError",
            f"{args.variables}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}",
        )
    if not isinstance(variables, dict):
        raise PlanError(
            "VariablesError", f"'{args.variables}' must contain a JSON object"
        )

    planner = Planner(schema, variables)
    return planner.plan(query_text, args.query, args.operation)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graph-index",
        description="Blockchain data index and GraphQL query service tooling.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    query_plan = sub.add_parser(
        "query-plan", help="compile a GraphQL query into an entity scan/join plan"
    )
    query_plan.add_argument("--schema", required=True, help="GraphQL schema file (SDL)")
    query_plan.add_argument("--query", required=True, help="GraphQL query file")
    query_plan.add_argument("--variables", required=True, help="variables JSON file")
    query_plan.add_argument(
        "--operation",
        default=None,
        help="operation name to select when the document has several",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "query-plan":
            plan = _cmd_query_plan(args)
        else:  # pragma: no cover - argparse enforces the subcommand
            raise PlanError("InvalidRequest", f"unknown command '{args.command}'")
    except PlanError as exc:
        sys.stderr.write(
            json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False)
            + "\n"
        )
        return 2
    sys.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
