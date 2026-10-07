"""graph-index command line interface."""

from __future__ import annotations

import argparse
import json
import sys

from .complexity import (
    QueryComplexityExceeded,
    complexity_error_payload,
    enforce_complexity,
    load_limit_from_env,
)
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

    planner = Planner(schema, variables, paging_enabled=True)
    return planner.plan(query_text, args.query, args.operation)


def _cmd_subscription_push(args, complexity_limit) -> list:
    schema_text = _read_text_file(args.schema)
    subscription_text = _read_text_file(args.subscription)
    variables_text = _read_text_file(args.variables)
    events_text = _read_text_file(args.events)

    schema = load_schema(schema_text, args.schema)
    variables = _load_variables(variables_text, args.variables)

    compiler = SubscriptionCompiler(
        schema, variables, bound_args_allowed=complexity_limit is not None
    )
    plan = compiler.compile(subscription_text, args.subscription, args.operation)
    if complexity_limit is not None:
        # Gate before any subscription is established or event is processed.
        enforce_complexity(
            schema,
            variables,
            subscription_text,
            args.subscription,
            args.operation,
            SubscriptionCompiler._select_operation,
            complexity_limit,
        )
    return push_events(plan, events_text, args.events)


def _cmd_query_exec(args, complexity_limit) -> dict:
    schema_text = _read_text_file(args.schema)
    query_text = _read_text_file(args.query)
    variables_text = _read_text_file(args.variables)
    events_text = _read_text_file(args.events)

    schema = load_schema(schema_text, args.schema)
    variables = _load_variables(variables_text, args.variables)

    compiler = QueryCompiler(
        schema, variables, bound_args_allowed=complexity_limit is not None
    )
    _operation_name, roots = compiler.compile(
        query_text, args.query, args.operation
    )
    if complexity_limit is not None:
        # Gate after the document/variables have passed ordinary validation but
        # before any indexed entity is read (events are folded only afterwards).
        enforce_complexity(
            schema,
            variables,
            query_text,
            args.query,
            args.operation,
            QueryCompiler._select_operation,
            complexity_limit,
            paging_enabled=True,
        )
    ctx = ExecContext(schema, compiler.entity_keys, compiler.key_types)
    # A query that only asks for introspection/meta-fields still requires the
    # events file to be readable, but its events are never parsed or folded.
    if any("response_key" in root for root in roots):
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
        # Read and validate the complexity limit at startup, before the command
        # opens anything: an invalid value fails with the unique diagnostic
        # code INVALID_QUERY_COMPLEXITY_LIMIT. Absent/0 disables the control.
        complexity_limit = load_limit_from_env()
    except PlanError as exc:
        sys.stderr.write(
            json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False)
            + "\n"
        )
        return 2
    try:
        if args.command == "query-plan":
            result = _cmd_query_plan(args)
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.command == "subscription-push":
            try:
                rows = _cmd_subscription_push(args, complexity_limit)
            except QueryComplexityExceeded as exc:
                sys.stdout.write(
                    json.dumps(
                        complexity_error_payload(exc.complexity, exc.limit),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                return 0
            for row in rows:
                sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
            return 0
        if args.command == "query-exec":
            try:
                result = _cmd_query_exec(args, complexity_limit)
            except QueryComplexityExceeded as exc:
                # Deterministic stop: data is null and the single error carries
                # QUERY_COMPLEXITY_EXCEEDED; no resolver ran and no entity was
                # read, so the response is still a normal GraphQL success exit.
                result = complexity_error_payload(exc.complexity, exc.limit)
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
