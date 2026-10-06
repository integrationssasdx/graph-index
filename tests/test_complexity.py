"""Tests for query complexity control (GRAPHQL_QUERY_COMPLEXITY_LIMIT).

The limit is computed from the same GraphQL document and variables as the
normal command, strictly before resolvers run / entities are read / a
subscription is established. These tests cover configuration validation, the
deterministic scoring rules, validation-error precedence and the exact
GraphQL over-limit response.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from graph_index.cli import main
from graph_index.complexity import (
    DEFAULT_LIST_BOUND,
    QueryComplexityExceeded,
    load_complexity_limit,
)
from graph_index.errors import PlanError

SCHEMA = """\
type Query {
  user(id: ID): User
  users: [User!]!
  things: [Thing!]!
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
  reviews: [Review!]! @link(local: "id", target: "user_id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String
}

type Review @entity(name: "reviews", key: "id") {
  id: ID!
  user_id: ID
  score: Int
}

type Thing @entity(name: "things", key: "id") {
  id: ID!
  owner: ID
}
"""

SUB_SCHEMA = """\
type Query { users: [User!]! }

type Subscription {
  user(id: ID!): User
  users: [User!]!
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String
}
"""

EVENTS = "\n".join(
    json.dumps(row)
    for row in [
        {"op": "INSERT", "entity": "users", "before": None,
         "after": {"id": 1, "name": "ada", "team_id": None}},
    ]
) + "\n"

SUB_EVENTS = "\n".join(
    json.dumps(row)
    for row in [
        {"op": "INSERT", "entity": "users", "before": None,
         "after": {"id": 1, "name": "ada"}},
    ]
) + "\n"


class LimitConfigCase(unittest.TestCase):
    def test_absent_or_zero_disables(self):
        self.assertIsNone(load_complexity_limit(None))
        self.assertIsNone(load_complexity_limit("0"))

    def test_positive_integer(self):
        self.assertEqual(load_complexity_limit("1"), 1)
        self.assertEqual(load_complexity_limit(" 42 "), 42)
        self.assertEqual(load_complexity_limit("+7"), 7)

    def test_negative_rejected(self):
        with self.assertRaises(PlanError) as caught:
            load_complexity_limit("-1")
        self.assertEqual(caught.exception.code,
                         "INVALID_QUERY_COMPLEXITY_LIMIT")

    def test_negative_zero_is_zero_and_disables(self):
        # int("-0") == 0: not negative, treated as the disabled value.
        self.assertIsNone(load_complexity_limit("-0"))

    def test_non_integer_or_unparseable_rejected(self):
        for raw in ("", "  ", "1.5", "abc", "1e3", "0x1"):
            with self.assertRaises(PlanError) as caught:
                load_complexity_limit(raw)
            self.assertEqual(
                caught.exception.code,
                "INVALID_QUERY_COMPLEXITY_LIMIT",
                f"raw={raw!r}",
            )


class CliCase(unittest.TestCase):
    command = "query-exec"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def run_cli(self, query, variables="{}", schema=SCHEMA, events=EVENTS,
                operation=None, limit=None, subscription=False):
        self._write("schema.graphql", schema)
        self._write("doc.graphql", query)
        self._write("variables.json", variables)
        self._write("events.ndjson", events)
        if subscription:
            argv = ["subscription-push", "--schema",
                    os.path.join(self.tmp.name, "schema.graphql"),
                    "--subscription",
                    os.path.join(self.tmp.name, "doc.graphql"),
                    "--variables",
                    os.path.join(self.tmp.name, "variables.json"),
                    "--events",
                    os.path.join(self.tmp.name, "events.ndjson")]
        else:
            argv = [self.command, "--schema",
                    os.path.join(self.tmp.name, "schema.graphql"),
                    "--query",
                    os.path.join(self.tmp.name, "doc.graphql"),
                    "--variables",
                    os.path.join(self.tmp.name, "variables.json"),
                    "--events",
                    os.path.join(self.tmp.name, "events.ndjson")]
        if operation is not None:
            argv += ["--operation", operation]
        env = {}
        if limit is not None:
            env["GRAPHQL_QUERY_COMPLEXITY_LIMIT"] = str(limit)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_complexity_error(self, code, stdout, stderr,
                                complexity=None, limit=None):
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertIsNone(payload["data"])
        errors = payload["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["extensions"]["code"],
                         "QUERY_COMPLEXITY_EXCEEDED")
        self.assertIsInstance(errors[0]["message"], str)
        self.assertTrue(errors[0]["message"])
        if complexity is not None:
            self.assertEqual(errors[0]["message"],
                             f"query complexity {complexity} exceeds the "
                             f"configured limit {limit}")

    def assert_data_ok(self, code, stdout, stderr):
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        return json.loads(stdout)

    # -- startup validation ---------------------------------------------------

    def test_invalid_env_fails_startup_before_work(self):
        argv = ["query-exec", "--schema",
                self._write("schema.graphql", SCHEMA),
                "--query", self._write("doc.graphql", "{ user { id } }"),
                "--variables", self._write("variables.json", "{}"),
                "--events", self._write("events.ndjson", EVENTS)]
        for raw in ("-1", "1.5", "nope", "1e3", "   "):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ,
                                 {"GRAPHQL_QUERY_COMPLEXITY_LIMIT": raw}):
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = main(argv)
            self.assertEqual(rc, 2, raw)
            self.assertEqual(out.getvalue(), "", raw)
            self.assertEqual(json.loads(err.getvalue())["code"],
                             "INVALID_QUERY_COMPLEXITY_LIMIT", raw)

    # -- boundary -------------------------------------------------------------

    def test_complexity_equal_to_limit_is_allowed(self):
        payload = self.assert_data_ok(
            *self.run_cli("{ user { id name } }", limit=2))
        self.assertEqual(payload["data"]["user"], {"id": 1, "name": "ada"})

    def test_complexity_above_limit_is_rejected(self):
        self.assert_complexity_error(
            *self.run_cli("{ user { id name } }", limit=1),
            complexity=2, limit=1)

    def test_limit_zero_disables_control(self):
        payload = self.assert_data_ok(
            *self.run_cli("{ users { id name } }", limit=0))
        self.assertEqual(len(payload["data"]["users"]), 1)

    def test_control_off_by_default(self):
        payload = self.assert_data_ok(
            *self.run_cli("{ users { id name } }", limit=None))
        self.assertEqual(len(payload["data"]["users"]), 1)

    # -- deterministic scoring ------------------------------------------------

    def test_repeated_fields_count_separately(self):
        # Execution merges the repeated response key, but complexity counts the
        # raw selections independently.
        self.assert_complexity_error(
            *self.run_cli("{ user { id id } }", limit=1),
            complexity=2, limit=1)

    def test_distinct_aliases_count_separately(self):
        self.assert_complexity_error(
            *self.run_cli("{ user { a: id b: id } }", limit=1),
            complexity=2, limit=1)

    def test_composite_field_only_sums_children(self):
        # id(1) + team{ name }(1) = 2; the object container itself costs 0.
        self.assert_data_ok(
            *self.run_cli(
                "{ user { id team { name } } }",
                events=EVENTS.replace('"team_id": null', '"team_id": 9')
                + json.dumps({"op": "INSERT", "entity": "teams",
                              "before": None,
                              "after": {"id": 9, "name": "core"}}) + "\n",
                limit=2))

    def test_fragment_inlined_once_at_position(self):
        query = "{ user { ...F } }\nfragment F on User { id name }\n"
        self.assert_complexity_error(
            *self.run_cli(query, limit=1), complexity=2, limit=1)
        self.assert_data_ok(*self.run_cli(query, limit=2))

    def test_inline_fragment_scored_like_its_selection(self):
        self.assert_complexity_error(
            *self.run_cli("{ user { ... on User { id name } } }", limit=1),
            complexity=2, limit=1)

    def test_introspection_fields_cost_zero(self):
        # Pure introspection passes even a limit of 1 and resolves to null.
        payload = self.assert_data_ok(
            *self.run_cli('{ __schema { queryType { name } } }', limit=1))
        self.assertIsNone(payload["data"]["__schema"])

    def test_introspection_zero_plus_entity_shares_limit(self):
        payload = self.assert_data_ok(
            *self.run_cli(
                '{ __type(name: "User") { name } user { id } }', limit=1))
        self.assertIsNone(payload["data"]["__type"])
        self.assertEqual(payload["data"]["user"], {"id": 1})

    # -- list bounds ----------------------------------------------------------

    def test_list_field_uses_default_bound(self):
        self.assert_complexity_error(
            *self.run_cli("{ users { id } }", limit=DEFAULT_LIST_BOUND - 1),
            complexity=DEFAULT_LIST_BOUND, limit=DEFAULT_LIST_BOUND - 1)
        self.assert_data_ok(
            *self.run_cli("{ users { id } }", limit=DEFAULT_LIST_BOUND))

    def test_list_bound_from_first_literal(self):
        self.assert_data_ok(
            *self.run_cli("{ users(first: 3) { id } }", limit=3))
        self.assert_complexity_error(
            *self.run_cli("{ users(first: 3) { id } }", limit=2),
            complexity=3, limit=2)

    def test_list_bound_from_limit_literal(self):
        self.assert_data_ok(
            *self.run_cli("{ users(limit: 4) { id } }", limit=4))

    def test_first_takes_priority_over_limit(self):
        # first:2 wins even though limit:9 would score higher.
        self.assert_data_ok(
            *self.run_cli("{ users(first: 2, limit: 9) { id } }", limit=2))

    def test_non_positive_or_non_integer_literal_falls_back_to_default(self):
        for arg in ("first: 0", "first: -3", "first: 2.5"):
            self.assert_complexity_error(
                *self.run_cli(
                    "{ users(" + arg + ") { id } }",
                    limit=DEFAULT_LIST_BOUND - 1),
                complexity=DEFAULT_LIST_BOUND,
                limit=DEFAULT_LIST_BOUND - 1)

    def test_list_bound_from_variable(self):
        query = "query ($n: Int) { users(first: $n) { id } }"
        self.assert_data_ok(
            *self.run_cli(query, variables='{"n": 3}', limit=3))
        self.assert_complexity_error(
            *self.run_cli(query, variables='{"n": 3}', limit=2),
            complexity=3, limit=2)

    def test_list_bound_uses_variable_default(self):
        query = "query ($n: Int = 2) { users(first: $n) { id } }"
        self.assert_data_ok(*self.run_cli(query, variables="{}", limit=2))

    def test_non_positive_variable_falls_back_to_default(self):
        query = "query ($n: Int) { users(first: $n) { id } }"
        self.assert_complexity_error(
            *self.run_cli(query, variables='{"n": -5}',
                          limit=DEFAULT_LIST_BOUND - 1),
            complexity=DEFAULT_LIST_BOUND, limit=DEFAULT_LIST_BOUND - 1)

    def test_string_typed_bound_variable_is_not_an_int(self):
        # The scorer only treats actual integers as bounds; a String "5" is not
        # type-checked against Int here (it is declared String), so the default
        # bound applies rather than raising a variable error.
        query = 'query ($n: String) { users(first: $n) { id } }'
        self.assert_complexity_error(
            *self.run_cli(query, variables='{"n": "5"}',
                          limit=DEFAULT_LIST_BOUND - 1),
            complexity=DEFAULT_LIST_BOUND, limit=DEFAULT_LIST_BOUND - 1)

    def test_nested_list_bounds_multiply(self):
        query = "{ users(first: 2) { reviews(first: 3) { id } } }"
        self.assert_data_ok(*self.run_cli(query, limit=6))
        self.assert_complexity_error(
            *self.run_cli(query, limit=5), complexity=6, limit=5)

    # -- validation errors keep precedence ------------------------------------

    def test_malformed_document_keeps_parse_error(self):
        code, _out, stderr = self.run_cli("{ user { id ", limit=1)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "ParseError")

    def test_unknown_field_keeps_existing_error(self):
        code, _out, stderr = self.run_cli("{ user { nope } }", limit=1)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "UnknownField")

    def test_wrong_variable_type_keeps_variables_error(self):
        query = "query ($n: Int) { users(first: $n) { id } }"
        code, _out, stderr = self.run_cli(
            query, variables='{"n": "x"}', limit=1)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "VariablesError")

    def test_fragment_cycle_keeps_existing_error(self):
        query = ("{ users { ...A } }\n"
                 "fragment A on User { ...B }\n"
                 "fragment B on User { ...A }\n")
        code, _out, stderr = self.run_cli(query, limit=10_000_000)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "InvalidQuery")

    def test_unselected_operation_is_not_scored(self):
        query = ("query A { users { id name } }\n"
                 "query B { user { id } }\n")
        payload = self.assert_data_ok(
            *self.run_cli(query, operation="B", limit=1))
        self.assertEqual(payload["data"]["user"], {"id": 1})

    # -- no work happens before the gate --------------------------------------

    def test_rejected_request_does_not_fold_events(self):
        # These events would fail validation/folding; an over-limit request
        # must stop with the complexity error instead, proving no entity read.
        bad_events = "{not json\n"
        code, _stdout, stderr = self.run_cli(
            "{ user { id name } }", events=bad_events, limit=1)
        self.assertEqual(code, 0, stderr)
        self.assert_complexity_error(
            code, _stdout, stderr, complexity=2, limit=1)

    # -- query-plan stays on its existing path --------------------------------

    def test_query_plan_not_gated(self):
        argv = ["query-plan",
                "--schema", self._write("p_schema.graphql", SCHEMA),
                "--query", self._write("p_query.graphql",
                                       "{ users { id name } }"),
                "--variables", self._write("p_vars.json", "{}")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ,
                             {"GRAPHQL_QUERY_COMPLEXITY_LIMIT": "1"}):
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = main(argv)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["operationType"], "query")

    def test_query_plan_does_not_accept_bound_args(self):
        argv = ["query-plan",
                "--schema", self._write("q_schema.graphql", SCHEMA),
                "--query", self._write("q_query.graphql",
                                       "{ users(first: 2) { id } }"),
                "--variables", self._write("q_vars.json", "{}")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ,
                             {"GRAPHQL_QUERY_COMPLEXITY_LIMIT": "2"}):
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err.getvalue())["code"], "UnknownField")


class SubscriptionGateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def _run(self, doc, variables="{}", limit=None, operation=None):
        argv = ["subscription-push",
                "--schema", self._write("schema.graphql", SUB_SCHEMA),
                "--subscription", self._write("doc.graphql", doc),
                "--variables", self._write("variables.json", variables),
                "--events", self._write("events.ndjson", SUB_EVENTS)]
        if operation is not None:
            argv += ["--operation", operation]
        env = {} if limit is None else {
            "GRAPHQL_QUERY_COMPLEXITY_LIMIT": str(limit)}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_complexity_error(self, code, stdout, stderr,
                                complexity=None, limit=None):
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertIsNone(payload["data"])
        self.assertEqual(len(payload["errors"]), 1)
        self.assertEqual(payload["errors"][0]["extensions"]["code"],
                         "QUERY_COMPLEXITY_EXCEEDED")
        if complexity is not None:
            self.assertEqual(
                payload["errors"][0]["message"],
                f"query complexity {complexity} exceeds the configured "
                f"limit {limit}")

    def test_equal_limit_establishes_and_pushes(self):
        code, stdout, stderr = self._run(
            "subscription { user(id: 1) { id name } }", limit=2)
        self.assertEqual(code, 0, stderr)
        rows = [json.loads(line) for line in stdout.splitlines() if line]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"], {"id": 1, "name": "ada"})

    def test_over_limit_does_not_open_subscription_or_push(self):
        code, stdout, stderr = self._run(
            "subscription { user(id: 1) { id name } }", limit=1)
        self.assert_complexity_error(
            code, stdout, stderr, complexity=2, limit=1)
        # No JSONL push output leaked before the GraphQL error.
        self.assertNotIn('"event"', stdout)

    def test_subscription_list_default_bound(self):
        code, stdout, stderr = self._run(
            "subscription { users { id } }", limit=DEFAULT_LIST_BOUND - 1)
        self.assert_complexity_error(
            code, stdout, stderr,
            complexity=DEFAULT_LIST_BOUND, limit=DEFAULT_LIST_BOUND - 1)

    def test_subscription_bound_variable(self):
        doc = "subscription ($n: Int) { users(first: $n) { id } }"
        code, _stdout, stderr = self._run(
            doc, variables='{"n": 2}', limit=2)
        self.assertEqual(code, 0, stderr)

    def test_only_selected_subscription_scored(self):
        doc = ("subscription A { user(id: 1) { id name } }\n"
               "subscription B { user(id: 1) { id } }\n")
        code, stdout, stderr = self._run(doc, limit=1, operation="B")
        self.assertEqual(code, 0, stderr)
        rows = [json.loads(line) for line in stdout.splitlines() if line]
        self.assertEqual(rows[0]["subscription"], "B")


class ExceptionShapeCase(unittest.TestCase):
    def test_exceeded_carries_code_and_numbers(self):
        exc = QueryComplexityExceeded(7, 6)
        self.assertEqual(exc.code, "QUERY_COMPLEXITY_EXCEEDED")
        self.assertEqual(exc.complexity, 7)
        self.assertEqual(exc.limit, 6)


if __name__ == "__main__":
    unittest.main()
