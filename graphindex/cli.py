"""Command line entry point for the Graph Index query planner.

Usage::

    graph-index query-plan \\
        --schema schema.graphql \\
        --query query.graphql \\
        --variables variables.json

On success the JSON plan is written to stdout and the exit code is 0.
Any failure is written to stderr as ``{"code": ..., "message": ...}``
with exit code 2; no plan is ever partially emitted.
"""

import argparse
import json
import sys

from .errors import IoError, ParseError, PlannerError
from .mapping import build_mapping
from .parser import parse
from .planner import build_plan


def _read_utf8(path: str, label: str) -> str:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise IoError(f"{label} file not found: {path}")
    except IsADirectoryError:
        raise IoError(f"{label} path is a directory: {path}")
    except OSError as exc:
        raise IoError(f"could not read {label} file {path}: {exc}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IoError(
            f"{label} file {path} is not valid UTF-8: {exc}") from exc


def run_query_plan(schema_path, query_path, variables_path,
                   operation_name=None, out=None, err=None):
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    schema_source = _read_utf8(schema_path, "schema")
    query_source = _read_utf8(query_path, "query")
    if variables_path is not None:
        variables_source = _read_utf8(variables_path, "variables")
    else:
        variables_source = None

    schema_document = parse(schema_source)
    query_document = parse(query_source)

    if variables_source is not None:
        try:
            variables = json.loads(variables_source)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"variables file is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})")
        if not isinstance(variables, dict):
            raise ParseError(
                "variables file must contain a JSON object")
    else:
        variables = {}

    mapping = build_mapping(schema_document)
    plan = build_plan(mapping, query_document, variables, operation_name)

    json.dump(plan, out, ensure_ascii=False, indent=2)
    out.write("\n")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="graph-index",
        description="Blockchain data indexing tooling: entity mapping and "
                    "GraphQL query planning.")
    subparsers = parser.add_subparsers(dest="command")

    qp = subparsers.add_parser(
        "query-plan",
        help="Compile a GraphQL operation into a table-scan/join plan.")
    qp.add_argument("--schema", required=True, metavar="PATH",
                    help="GraphQL SDL schema with @entity/@link directives")
    qp.add_argument("--query", required=True, metavar="PATH",
                    help="GraphQL document containing the operation")
    qp.add_argument("--variables", metavar="PATH",
                    help="JSON object with operation variable values")
    qp.add_argument("--operation-name", metavar="NAME", default=None,
                    help="operation to plan when the document has several")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    if args.command == "query-plan":
        try:
            return run_query_plan(
                args.schema, args.query, args.variables,
                operation_name=args.operation_name)
        except PlannerError as error:
            json.dump(error.to_dict(), sys.stderr, ensure_ascii=False)
            sys.stderr.write("\n")
            return 2

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
