"""Tests for GRAPHQL_QUERY_COMPLEXITY_LIMIT support."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from graph_index.cli import main
from graph_index.complexity import (
    ENV_VAR,
    load_complexity_limit,
    measure_complexity,
    resolve_list_bound,
)
from graph_index.errors import PlanError
from graph_index.gql import FieldNode, Var, parse_executable
from graph_index.planner import Planner
from graph_index.schema import load_schema

SCHEMA = """\
type Query {
  users(id: ID, name: String): [User!]!
  user(id: ID!): User
  teams: [Team!]!
}

enum Role {
  ADMIN
  MEMBER
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  role: Role
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
  reviews(first: Int, limit: Int): [Review!]!
    @link(local: "id", target: "user_id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  name: String!
}

type Review @entity(name: "reviews", key: "id") {
  id: ID!
  user_id: ID
  score: Int
}
"""

SUB_SCHEMA = """\
type Query {
  users: [User!]!
}

type Subscription {
  users(id: ID): [User!]!
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  reviews(first: Int): [Review!]! @link(local: "id", target: "user_id")
}

type Review @entity(name: "reviews", key: "id") {
  id: ID!
  user_id: ID
  score: Int
}
"""

EVENTS = "\n".join(
    [
        json.dumps(row)
        for row in [
            {"op": "INSERT", "entity": "reviews", "before": None,
             "after": {"id": "r1", "user_id": 1, "score": 10}},
            {"op": "INSERT", "entity": "users", "before": None,
             "after": {"id": 1, "name": "ada", "team_id": None}},
        ]
    ]
) + "\n"


# ---------------------------------------------------------------------------
# Startup configuration
# ---------------------------------------------------------------------------


class LimitConfigCase(unittest.TestCase):
    def limit(self, value):
        env = {} if value is None else {ENV_VAR: value}
        return load_complexity_limit(env)

    def test_unset_or_empty_disables(self):
        self.assertIsNone(self.limit(None))
        self.assertIsNone(self.limit(""))
        self.assertIsNone(self.limit("   "))

    def test_zero_disables(self):
        self.assertEqual(self.limit("0"), 0)

    def test_positive_integer(self):
        self.assertEqual(self.limit("1"), 1)
        self.assertEqual(self.limit(" 42 "), 42)
        self.assertEqual(self.limit("1000000"), 1_000_000)

    def test_negative_fails(self):
        for value in ("-1", "  -7 ", "-100"):
            with self.assertRaises(PlanError) as ctx:
                self.limit(value)
            self.assertEqual(ctx.exception.code, "INVALID_QUERY_COMPLEXITY_LIMIT")

    def test_minus_zero_is_value_zero_and_disables(self):
        # The rule is value-based: -0 parses to the integer 0.
        self.assertEqual(self.limit("-0"), 0)

    def test_non_integer_fails(self):
        for value in ("1.5", "1e3", "abc", "true", "0x1", "3px", "+1"):
            with self.assertRaises(PlanError) as ctx:
                self.limit(value)
            self.assertEqual(
                ctx.exception.code,
                "INVALID_QUERY_COMPLEXITY_LIMIT",
                f"value {value!r} must be rejected",
            )


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _measure(schema_text, query, variables=None, operation=None):
    schema = load_schema(schema_text)
    operations, fragments = parse_executable(query, "<query>")
    if operation is None:
        op = operations[0]
    else:
        op = next(o for o in operations if o.name == operation)
    var_map = {
        name: (type_ref, default, has_default)
        for name, type_ref, default, has_default in op.var_defs
    }
    checker = Planner(schema, variables or {})._check_type
    return measure_complexity(
        schema, op, fragments, variables or {}, var_map, checker
    )


class MeasurementCase(unittest.TestCase):
    def test_single_object_root_counts_leaves(self):
        self.assertEqual(_measure(SCHEMA, "{ user(id: 1) { id name } }"), 2)

    def test_list_root_uses_default_bound(self):
        self.assertEqual(_measure(SCHEMA, "{ users { id name } }"), 2000)

    def test_aliases_count_separately(self):
        self.assertEqual(
            _measure(SCHEMA, "{ user(id: 1) { a: id b: name c: name } }"), 3
        )

    def test_duplicate_fields_count_separately(self):
        # Measurement walks the raw AST; it does not merge response keys the
        # way the query planner does.
        self.assertEqual(
            _measure(SCHEMA, "{ user(id: 1) { id id name } }"), 3
        )

    def test_composite_field_sums_children(self):
        query = "{ user(id: 1) { id team { id name } } }"
        self.assertEqual(_measure(SCHEMA, query), 1 + 2)

    def test_fragment_expanded_once_in_place(self):
        query = """
        { user(id: 1) { id ...F } }
        fragment F on User { name team { id name } }
        """
        self.assertEqual(_measure(SCHEMA, query), 1 + 1 + 2)

    def test_fragment_spread_inside_list_is_amplified(self):
        query = """
        { users(first: 2) { ...F } }
        fragment F on User { id name }
        """
        self.assertEqual(_measure(SCHEMA, query), 4)

    def test_nested_list_multiplies(self):
        # users(first: 3) x (1 + reviews(default 1000) x 2)
        query = "{ users(first: 3) { id reviews { id score } } }"
        self.assertEqual(_measure(SCHEMA, query), 3 * (1 + 2000))

    def test_first_takes_priority_over_limit(self):
        query = "{ users(first: 4, limit: 9) { id } }"
        self.assertEqual(_measure(SCHEMA, query), 4)

    def test_bound_from_variable(self):
        query = "query ($n: Int) { users(limit: $n) { id name } }"
        self.assertEqual(_measure(SCHEMA, query, {"n": 5}), 10)

    def test_non_positive_bound_falls_back_to_default(self):
        query = "{ users(first: 0) { id } }"
        self.assertEqual(_measure(SCHEMA, query), 1000)
        query = "{ users(limit: -3) { id } }"
        self.assertEqual(_measure(SCHEMA, query), 1000)

    def test_non_integer_bound_falls_back_to_default(self):
        query = '{ users(first: "5") { id } }'
        self.assertEqual(_measure(SCHEMA, query), 1000)

    def test_bound_variable_is_validated_against_declared_type(self):
        # The declared Int variable holding a string is a VariablesError
        # during validation, never rewritten into a complexity error.
        schema = load_schema(SCHEMA)
        checker = Planner(schema, {})._check_type
        node = FieldNode("users", None, {"first": Var("n")}, None)
        var_map = {"n": (("named", "Int"), None, False)}
        with self.assertRaises(PlanError) as ctx:
            resolve_list_bound(node, var_map, {"n": "5"}, checker)
        self.assertEqual(ctx.exception.code, "VariablesError")

    def test_bound_variable_with_default_uses_default_value(self):
        query = "query ($n: Int = 7) { users(limit: $n) { id } }"
        self.assertEqual(_measure(SCHEMA, query, {}), 7)

    def test_variable_bound_non_positive_value_uses_default(self):
        query = "query ($n: Int) { users(limit: $n) { id } }"
        self.assertEqual(_measure(SCHEMA, query, {"n": 0}), 1000)

    def test_introspection_fields_cost_zero(self):
        # Measured directly: the frozen compilers reject introspection root
        # fields as unknown, but the complexity rule assigns them 0.
        query = "{ __schema { queryType { name } } __type(name: \"X\") { name } }"
        self.assertEqual(_measure(SCHEMA, query), 0)

    def test_only_selected_operation_is_counted(self):
        query = """
        query Small { user(id: 1) { id } }
        query Big { users { id name } }
        """
        self.assertEqual(_measure(SCHEMA, query, operation="Small"), 1)
        self.assertEqual(_measure(SCHEMA, query, operation="Big"), 2000)

    def test_unselected_operation_ignored_even_with_fragments(self):
        query = """
        query A { user(id: 1) { id } }
        query B { users { ...F } }
        fragment F on User { id name role }
        """
        self.assertEqual(_measure(SCHEMA, query, operation="A"), 1)


# ---------------------------------------------------------------------------
# End-to-end CLI behavior
# ---------------------------------------------------------------------------


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def _run(
        self,
        command,
        query,
        variables="{}",
        schema=SCHEMA,
        events=EVENTS,
        env=None,
        operation=None,
        events_path=None,
        var_file_name="variables.json",
        query_flag="--query",
    ):
        schema_path = self._write("schema.graphql", schema)
        query_path = self._write("query.graphql", query)
        variables_path = self._write(var_file_name, variables)
        if events_path is None:
            events_path = self._write("events.ndjson", events)
        argv = [command, "--schema", schema_path, query_flag, query_path,
                "--variables", variables_path]
        if command == "query-exec":
            argv += ["--events", events_path]
        else:
            argv += ["--events", events_path]
        if operation is not None:
            argv += ["--operation", operation]
        stdout, stderr = io.StringIO(), io.StringIO()
        environ = os.environ.copy()
        if env is not None:
            environ.update(env)
        with mock.patch.dict(os.environ, environ, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_query(self, query, **kwargs):
        kwargs.setdefault("query_flag", "--query")
        return self._run("query-exec", query, **kwargs)

    def run_subscription(self, query, **kwargs):
        kwargs["query_flag"] = "--subscription"
        kwargs.setdefault("schema", SUB_SCHEMA)
        return self._run("subscription-push", query, **kwargs)

    def assert_complexity_envelope(self, code, stdout, stderr):
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertIsNone(payload["data"])
        errors = payload["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0]["extensions"]["code"], "QUERY_COMPLEXITY_EXCEEDED"
        )
        self.assertTrue(errors[0]["message"])
        return payload

    # -- feature disabled ----------------------------------------------------

    def test_unset_limit_executes_normally(self):
        code, stdout, stderr = self.run_query("{ users { id name } }")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["data"]["users"],
                         [{"id": 1, "name": "ada"}])

    def test_zero_limit_executes_normally(self):
        code, stdout, stderr = self.run_query(
            "{ users { id name } }", env={ENV_VAR: "0"}
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("data", json.loads(stdout))

    # -- boundary ------------------------------------------------------------

    def test_complexity_equal_to_limit_is_allowed(self):
        # users(first: 2) { id name } -> 4
        code, stdout, stderr = self.run_query(
            "{ users(first: 2) { id name } }", env={ENV_VAR: "4"}
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["data"]["users"],
                         [{"id": 1, "name": "ada"}])

    def test_complexity_above_limit_is_rejected(self):
        result = self.assert_complexity_envelope(
            *self.run_query(
                "{ users(first: 2) { id name } }", env={ENV_VAR: "3"}
            )
        )
        # Ordinary data order/shape is irrelevant: data must be exactly null.
        self.assertNotIn("users", result)

    def test_bound_argument_never_filters_data(self):
        # first: 1 bounds complexity but all matching rows still come back.
        code, stdout, stderr = self.run_query(
            "{ users(first: 1) { id name } }", env={ENV_VAR: "100000"}
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(json.loads(stdout)["data"]["users"]), 1)
        events = EVENTS + json.dumps(
            {"op": "INSERT", "entity": "users", "before": None,
             "after": {"id": 2, "name": "bob", "team_id": None}}
        ) + "\n"
        code, stdout, stderr = self.run_query(
            "{ users(first: 1) { id name } }",
            env={ENV_VAR: "100000"}, events=events,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(json.loads(stdout)["data"]["users"]), 2)

    def test_variable_bound_gate(self):
        query = "query ($n: Int) { users(limit: $n) { id name } }"
        self.assert_complexity_envelope(
            *self.run_query(query, variables='{"n": 5}', env={ENV_VAR: "9"})
        )
        code, stdout, stderr = self.run_query(
            query, variables='{"n": 5}', env={ENV_VAR: "10"}
        )
        self.assertEqual(code, 0, stderr)

    def test_missing_bound_argument_uses_default_bound(self):
        # users { id } -> 1000; limit 999 rejects, 1000 allows.
        self.assert_complexity_envelope(
            *self.run_query("{ users { id } }", env={ENV_VAR: "999"})
        )
        code, _, stderr = self.run_query(
            "{ users { id } }", env={ENV_VAR: "1000"}
        )
        self.assertEqual(code, 0, stderr)

    # -- determinism / no side effects --------------------------------------

    def test_rejected_request_does_not_read_events(self):
        # A missing events file would normally produce IoError; the gate
        # fires first and never reaches it.
        code, stdout, stderr = self.run_query(
            "{ users { id name } }",
            env={ENV_VAR: "1"},
            events_path=os.path.join(self.tmp.name, "missing.ndjson"),
        )
        self.assert_complexity_envelope(code, stdout, stderr)

    def test_rejected_request_runs_no_resolvers(self):
        # Snapshot-incompatible events would raise EventError if folding ran;
        # rejection short-circuits before events are even read.
        bad_events = '{"op": "INSERT", "entity": "users", "before": null, "after": {"id": 1}}\n'
        code, stdout, stderr = self.run_query(
            "{ users(first: 2) { name } }",
            env={ENV_VAR: "1"}, events=bad_events,
        )
        self.assert_complexity_envelope(code, stdout, stderr)

    # -- validation errors are not rewritten --------------------------------

    def test_parse_error_precedes_complexity_error(self):
        code, stdout, stderr = self.run_query(
            "{ users {", env={ENV_VAR: "1"}
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["code"], "ParseError")

    def test_unknown_field_precedes_complexity_error(self):
        code, stdout, stderr = self.run_query(
            "{ users { nope } }", env={ENV_VAR: "1"}
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "UnknownField")

    def test_variable_type_error_precedes_complexity_error(self):
        query = "query ($n: Int) { users(first: $n) { id } }"
        code, stdout, stderr = self.run_query(
            query, variables='{"n": "x"}', env={ENV_VAR: "1000000"}
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "VariablesError")

    def test_undeclared_variable_in_bound_is_variables_error(self):
        query = "{ users(first: $n) { id } }"
        code, _, stderr = self.run_query(query, env={ENV_VAR: "1000000"})
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "VariablesError")

    def test_fragment_cycle_precedes_complexity_error(self):
        query = """
        { users(first: 1) { ...A } }
        fragment A on User { id ...B }
        fragment B on User { name ...A }
        """
        code, _, stderr = self.run_query(query, env={ENV_VAR: "1000000"})
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "InvalidQuery")

    # -- operation selection -------------------------------------------------

    def test_unselected_operation_not_counted(self):
        query = """
        query Small { user(id: 1) { id } }
        query Big { users { id name } }
        """
        code, stdout, stderr = self.run_query(
            query, operation="Small", env={ENV_VAR: "1"}
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["data"]["user"], {"id": 1})
        self.assert_complexity_envelope(
            *self.run_query(query, operation="Big", env={ENV_VAR: "1"})
        )

    # -- startup failure -----------------------------------------------------

    def test_invalid_limit_fails_startup(self):
        for value in ("-1", "1.0", "wat", "1e3"):
            code, stdout, stderr = self.run_query(
                "{ users { id } }", env={ENV_VAR: value}
            )
            self.assertEqual(code, 2, value)
            self.assertEqual(stdout, "", value)
            payload = json.loads(stderr)
            self.assertEqual(payload["code"], "INVALID_QUERY_COMPLEXITY_LIMIT")
            self.assertTrue(payload["message"])

    def test_invalid_limit_fails_before_reading_inputs(self):
        code, stdout, stderr = self.run_query(
            "{ users { id } }",
            env={ENV_VAR: "-5"},
            events_path=os.path.join(self.tmp.name, "nope.ndjson"),
        )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(stderr)["code"], "INVALID_QUERY_COMPLEXITY_LIMIT"
        )

    # -- query-plan is untouched ---------------------------------------------

    def test_query_plan_unaffected_by_limit(self):
        schema_path = self._write("p_schema.graphql", SCHEMA)
        query_path = self._write("p_query.graphql", "{ users { id name } }")
        variables_path = self._write("p_vars.json", "{}")
        argv = ["query-plan", "--schema", schema_path, "--query", query_path,
                "--variables", variables_path]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {ENV_VAR: "1"}, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)
        self.assertEqual(code, 0, stderr.getvalue())
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["operationType"], "query")

    # -- subscriptions --------------------------------------------------------

    def test_subscription_rejected_before_establishment(self):
        query = "subscription S { users { id name } }"
        # Complexity 2000 with the default bound; a missing events file proves
        # no event processing (no "subscription") was started.
        code, stdout, stderr = self.run_subscription(
            query,
            env={ENV_VAR: "1"},
            events_path=os.path.join(self.tmp.name, "missing.ndjson"),
        )
        self.assert_complexity_envelope(code, stdout, stderr)

    def test_subscription_equal_limit_establishes_and_pushes(self):
        query = "subscription S { users(first: 1) { id name } }"
        code, stdout, stderr = self.run_subscription(
            query, env={ENV_VAR: "2"}
        )
        self.assertEqual(code, 0, stderr)
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"], {"id": 1, "name": "ada"})

    def test_subscription_pushes_not_rebilled(self):
        # Every push carries the same shape; a limit that admits the
        # subscription once must admit every subsequent push unchanged.
        events = "\n".join([
            json.dumps({"op": "INSERT", "entity": "users", "before": None,
                        "after": {"id": i, "name": f"u{i}"}})
            for i in range(1, 4)
        ]) + "\n"
        query = "subscription S { users(first: 1) { id } }"
        code, stdout, stderr = self.run_subscription(
            query, env={ENV_VAR: "1"}, events=events
        )
        self.assertEqual(code, 0, stderr)
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(len(rows), 3)

    def test_subscription_validation_error_not_rewritten(self):
        code, stdout, stderr = self.run_subscription(
            "subscription S { users { nope } }", env={ENV_VAR: "1"}
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotEqual(
            json.loads(stderr)["code"], "QUERY_COMPLEXITY_EXCEEDED"
        )


if __name__ == "__main__":
    unittest.main()
