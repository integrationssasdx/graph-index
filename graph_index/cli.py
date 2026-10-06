"""graph-index command line interface."""

from __future__ import annotations

import argparse
import json
import sys

from .errors import PlanError
from .executor import ExecContext, QueryCompiler, execute, fold_events
from .planner import Planner
from .schema import load_schema
from .subscription import SubscriptionCompiler, push_events


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


def _load_variables(variables_text: str, source: str) -> dict:
    try:
        variables = json.loads(variables_text)
    except json.JSONDecodeError as exc:
        raise PlanError(
            "ParseError",
            f"{source}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}",
        )
    if not isinstance(variables, dict):
        raise PlanError("VariablesError", f"'{source}' must contain a JSON object")
    return variables


def _cmd_query_plan(args) -> dict:
    schema_text = _read_text_file(args.schema)
    query_text = _read_text_file(args.query)
    variables_text = _read_text_file(args.variables)

    schema = load_schema(schema_text, args.schema)
    variables = _load_variables(variables_text, args.variables)

    planner = Planner(schema, variables)
    return planner.plan(query_text, args.query, args.operation)


def _cmd_subscription_push(args) -> list:
    schema_text = _read_text_file(args.schema)
    subscription_text = _read_text_file(args.subscription)
    variables_text = _read_text_file(args.variables)
    events_text = _read_text_file(args.events)

    schema = load_schema(schema_text, args.schema)
    variables = _load_variables(variables_text, args.variables)

    compiler = SubscriptionCompiler(schema, variables)
    plan = compiler.compile(subscription_text, args.subscription, args.operation)
    return push_events(plan, events_text, args.events)


def _cmd_query_exec(args) -> dict:
    schema_text = _read_text_file(args.schema)
    query_text = _read_text_file(args.query)
    variables_text = _read_text_file(args.variables)
    events_text = _read_text_file(args.events)

    schema = load_schema(schema_text, args.schema)
    variables = _load_variables(variables_text, args.variables)

    compiler = QueryCompiler(schema, variables)
    _operation_name, roots = compiler.compile(
        query_text, args.query, args.operation
    )
    ctx = ExecContext(schema, compiler.entity_keys, compiler.key_types)
    fold_events(ctx, events_text, args.events)
    return execute(roots, ctx)


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
    subscription_push = sub.add_parser(
        "subscription-push",
        help="push entity change events matching a GraphQL subscription as JSONL",
    )
    subscription_push.add_argument(
        "--schema", required=True, help="GraphQL schema file (SDL)"
    )
    subscription_push.add_argument(
        "--subscription", required=True, help="GraphQL subscription file"
    )
    subscription_push.add_argument(
        "--variables", required=True, help="variables JSON file"
    )
    subscription_push.add_argument(
        "--events", required=True, help="entity change events file (NDJSON)"
    )
    subscription_push.add_argument(
        "--operation",
        default=None,
        help="operation name to select when the document has several",
    )
    query_exec = sub.add_parser(
        "query-exec",
        help="execute a GraphQL query over entity change events",
    )
    query_exec.add_argument(
        "--schema", required=True, help="GraphQL schema file (SDL)"
    )
    query_exec.add_argument("--query", required=True, help="GraphQL query file")
    query_exec.add_argument(
        "--variables", required=True, help="variables JSON file"
    )
    query_exec.add_argument(
        "--events", required=True, help="entity change events file (NDJSON)"
    )
    query_exec.add_argument(
        "--operation",
        default=None,
        help="operation name to select when the document has several",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "query-plan":
            result = _cmd_query_plan(args)
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "subscription-push":
            for row in _cmd_subscription_push(args):
                sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
            return 0
        if args.command == "query-exec":
            result = _cmd_query_exec(args)
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0
        raise PlanError("InvalidRequest", f"unknown command '{args.command}'")
    except PlanError as exc:
        sys.stderr.write(
            json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False)
            + "\n"
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
