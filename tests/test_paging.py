"""End-to-end tests for stable paging/sorting on list root fields.

Covers query-plan output keys/defaults and query-exec ordering/pagination
semantics plus the InvalidQuery vs VariablesError error split. The paging
arguments are only recognized on list root fields whose schema declares
them; subscription-push and undeclared roots keep their existing rules.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from graph_index.cli import main

SCHEMA = """\
type Query {
  users(
    status: String
    page: Int
    pageSize: Int
    orderBy: String
    sortDirection: String
  ): [User!]!
  user(id: ID!, page: Int, pageSize: Int): User
  plain: [User!]!
}

enum Role {
  ADMIN
  MEMBER
  GUEST
}

type User @entity(name: "users", key: "id") {
  id: ID!
  name: String!
  status: String
  age: Int!
  score: Float!
  active: Boolean!
  role: Role!
  tags: [String!]
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
  reviews: [Review!]! @link(local: "id", target: "user_id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  reviews: [Review!]! @link(local: "id", target: "team_id")
}

type Review @entity(name: "reviews", key: "id") {
  id: ID!
  user_id: ID
  team_id: ID
  stars: Int!
}
"""


def _event(op, entity, before, after):
    return json.dumps({"op": op, "entity": entity, "before": before,
                       "after": after})


EVENTS = "\n".join(
    [
        _event("INSERT", "users", None,
               {"id": 1, "name": "ada", "status": "on", "age": 30,
                "score": 1.5, "active": True, "role": "ADMIN",
                "tags": [], "team_id": 9}),
        _event("INSERT", "users", None,
               {"id": 2, "name": "bob", "status": "on", "age": 25,
                "score": 2.5, "active": False, "role": "MEMBER",
                "tags": [], "team_id": None}),
        _event("INSERT", "users", None,
               {"id": 3, "name": "carol", "status": "idle", "age": 40,
                "score": 9.0, "active": False, "role": "GUEST",
                "tags": [], "team_id": None}),
        _event("INSERT", "users", None,
               {"id": 10, "name": "dan", "status": "idle", "age": 28,
                "score": 1.5, "active": True, "role": "GUEST",
                "tags": [], "team_id": None}),
        _event("INSERT", "teams", None, {"id": 9}),
        _event("INSERT", "reviews", None,
               {"id": "r1", "user_id": 1, "team_id": 9, "stars": 9}),
        _event("INSERT", "reviews", None,
               {"id": "r2", "user_id": 1, "team_id": 9, "stars": 1}),
    ]
) + "\n"

# A snapshot stream where nullable orderBy positions carry null. The orderBy
# fields stay non-null in the schema (a nullable field is rejected statically).
NULL_EVENTS = "\n".join(
    [
        _event("INSERT", "users", None,
               {"id": 1, "name": "ada", "age": 30, "score": 1.5,
                "active": True, "role": "ADMIN", "tags": []}),
        _event("INSERT", "users", None,
               {"id": 2, "name": "bob", "age": None, "score": 2.5,
                "active": False, "role": "MEMBER", "tags": []}),
        _event("INSERT", "users", None,
               {"id": 3, "name": "carol", "age": 40, "score": None,
                "active": False, "role": "GUEST", "tags": []}),
        _event("INSERT", "users", None,
               {"id": 10, "name": "dan", "age": 28, "score": 1.5,
                "active": True, "role": "GUEST", "tags": []}),
    ]
) + "\n"


class _CliCase(unittest.TestCase):
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
                operation=None, limit=None):
        schema_path = self._write("schema.graphql", schema)
        var_path = self._write("variables.json", variables)
        if self.command == "query-plan":
            doc_path = self._write("doc.graphql", query)
            argv = [self.command, "--schema", schema_path,
                    "--query", doc_path, "--variables", var_path]
        else:
            doc_flag = ("--query" if self.command == "query-exec"
                        else "--subscription")
            doc_path = self._write("doc.graphql", query)
            events_path = self._write("events.ndjson", events)
            argv = [self.command, "--schema", schema_path, doc_flag, doc_path,
                    "--variables", var_path, "--events", events_path]
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

    def assert_error(self, code, stdout, stderr, expected_code):
        self.assertEqual(code, 2, stdout + stderr)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], expected_code)
        self.assertIsInstance(payload["message"], str)
        self.assertTrue(payload["message"])

    def assert_data(self, code, stdout, stderr):
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        return json.loads(stdout)["data"]


# ---------------------------------------------------------------------------
# query-plan
# ---------------------------------------------------------------------------


class PlanPagingCase(_CliCase):
    command = "query-plan"

    def _plan(self, query, variables="{}"):
        code, stdout, stderr = self.run_cli(query, variables)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        return json.loads(stdout)["roots"][0]

    def test_default_keys_when_no_paging_used(self):
        root = self._plan("{ users { id } }")
        self.assertEqual(root["page"], 0)
        self.assertIsNone(root["pageSize"])
        self.assertIsNone(root["orderBy"])
        self.assertEqual(root["sortDirection"], "ASC")

    def test_explicit_keys_echoed(self):
        root = self._plan(
            '{ users(page: 1, pageSize: 2, orderBy: "age", '
            'sortDirection: DESC) { id } }')
        self.assertEqual(root["page"], 1)
        self.assertEqual(root["pageSize"], 2)
        self.assertEqual(root["orderBy"], "age")
        self.assertEqual(root["sortDirection"], "DESC")

    def test_sort_direction_defaults_asc_with_order_by(self):
        root = self._plan('{ users(orderBy: "age") { id } }')
        self.assertEqual(root["orderBy"], "age")
        self.assertEqual(root["sortDirection"], "ASC")

    def test_single_object_root_carries_defaults_but_ignores_args(self):
        root = self._plan("{ user(id: 1) { id } }")
        self.assertEqual(root["page"], 0)
        self.assertIsNone(root["pageSize"])
        self.assertIsNone(root["orderBy"])
        self.assertEqual(root["sortDirection"], "ASC")

    def test_single_object_root_rejects_paging_arg_as_before(self):
        # Declared on the field, but a single-object root still treats page as
        # a non-filterable entity argument.
        self.assert_error(
            *self.run_cli("{ user(id: 1, page: 0) { id } }"), "UnknownField")

    def test_undeclared_root_keeps_unknown_argument_rule(self):
        self.assert_error(
            *self.run_cli("{ plain(page: 0, pageSize: 2) { id } }"),
            "UnknownField")

    def test_literal_validation_errors(self):
        cases = [
            ("{ users(page: 0) { id } }", "InvalidQuery"),
            ("{ users(page: -1, pageSize: 2) { id } }", "InvalidQuery"),
            ("{ users(pageSize: 0) { id } }", "InvalidQuery"),
            ("{ users(page: 0, pageSize: -2) { id } }", "InvalidQuery"),
            ("{ users(page: 1.5, pageSize: 2) { id } }", "InvalidQuery"),
            ("{ users(page: true, pageSize: 2) { id } }", "InvalidQuery"),
            ('{ users(orderBy: "nope") { id } }', "InvalidQuery"),
            ('{ users(orderBy: "status") { id } }', "InvalidQuery"),
            ('{ users(orderBy: "tags") { id } }', "InvalidQuery"),
            ('{ users(orderBy: "team") { id } }', "InvalidQuery"),
            ('{ users(orderBy: 5) { id } }', "InvalidQuery"),
            ('{ users(sortDirection: SIDE) { id } }', "InvalidQuery"),
            ("{ users(sortDirection: ASC) { id } }", "InvalidQuery"),
        ]
        for query, code in cases:
            with self.subTest(query=query):
                self.assert_error(*self.run_cli(query), code)

    def test_variable_validation_errors_are_variables_error(self):
        cases = [
            ("query ($p: Int) { users(page: $p, pageSize: 2) { id } }",
             '{"p": -1}'),
            ("query ($s: Int) { users(page: 0, pageSize: $s) { id } }",
             '{"s": 0}'),
            ('query ($o: String) { users(orderBy: $o) { id } }',
             '{"o": "nope"}'),
            ('query ($o: String) { users(orderBy: $o) { id } }',
             '{"o": "tags"}'),
            ('query ($d: String) { users(orderBy: "age", '
             'sortDirection: $d) { id } }', '{"d": "X"}'),
        ]
        for query, variables in cases:
            with self.subTest(query=query):
                self.assert_error(
                    *self.run_cli(query, variables), "VariablesError")

    def test_variable_value_type_mismatch_is_variables_error(self):
        query = "query ($s: Int) { users(page: 0, pageSize: $s) { id } }"
        self.assert_error(
            *self.run_cli(query, '{"s": "x"}'), "VariablesError")

    def test_missing_variable_is_variables_error(self):
        query = "query ($p: Int) { users(page: $p, pageSize: 2) { id } }"
        self.assert_error(*self.run_cli(query, "{}"), "VariablesError")


# ---------------------------------------------------------------------------
# query-exec: ordering
# ---------------------------------------------------------------------------


class OrderingCase(_CliCase):
    def ids(self, query, events=EVENTS, variables="{}"):
        data = self.assert_data(*self.run_cli(query, variables, events=events))
        return [row["id"] for row in data["users"]]

    def test_no_order_by_keeps_insert_order(self):
        self.assertEqual(self.ids("{ users { id } }"), [1, 2, 3, 10])

    def test_int_asc(self):
        self.assertEqual(self.ids('{ users(orderBy: "age") { id } }'),
                         [2, 10, 1, 3])

    def test_int_desc(self):
        self.assertEqual(
            self.ids('{ users(orderBy: "age", sortDirection: DESC) { id } }'),
            [3, 1, 10, 2])

    def test_float_desc(self):
        self.assertEqual(
            self.ids('{ users(orderBy: "score", sortDirection: DESC) { id } }'),
            [3, 2, 1, 10])

    def test_string_code_point_ordering(self):
        data = self.assert_data(
            *self.run_cli('{ users(orderBy: "name") { name } }'))
        self.assertEqual([r["name"] for r in data["users"]],
                         ["ada", "bob", "carol", "dan"])

    def test_id_code_point_tie_break(self):
        # id 1 and 10 share score 1.5; the ID tie-break orders by Unicode
        # code point, so "1" < "10" < "2" < "3".
        self.assertEqual(self.ids('{ users(orderBy: "score") { id } }'),
                         [1, 10, 2, 3])

    def test_boolean_false_before_true(self):
        self.assertEqual(self.ids('{ users(orderBy: "active") { id } }'),
                         [2, 3, 1, 10])

    def test_enum_follows_declaration_order(self):
        # Role order is ADMIN, MEMBER, GUEST.
        self.assertEqual(self.ids('{ users(orderBy: "role") { id } }'),
                         [1, 2, 10, 3])

    def test_nulls_last_asc(self):
        self.assertEqual(
            self.ids('{ users(orderBy: "age") { id } }', events=NULL_EVENTS),
            [10, 1, 3, 2])

    def test_nulls_last_desc(self):
        self.assertEqual(
            self.ids(
                '{ users(orderBy: "age", sortDirection: DESC) { id } }',
                events=NULL_EVENTS),
            [3, 1, 10, 2])

    def test_null_secondary_value_also_last(self):
        # score is null on id 3; nulls-last dominates regardless of type.
        self.assertEqual(
            self.ids('{ users(orderBy: "score") { id } }',
                     events=NULL_EVENTS),
            [1, 10, 2, 3])

    def test_update_reorders_by_new_value(self):
        events = EVENTS + _event(
            "UPDATE", "users", {"id": 2},
            {"id": 2, "name": "bob", "status": "on", "age": 99,
             "score": 2.5, "active": False, "role": "MEMBER",
             "tags": [], "team_id": None}) + "\n"
        self.assertEqual(
            self.ids('{ users(orderBy: "age") { id } }', events=events),
            [10, 1, 3, 2])

    def test_delete_then_reinsert_still_sorts_by_value(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "age": 10, "score": 1.0,
                    "active": True, "role": "ADMIN", "tags": []}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "b", "age": 20, "score": 1.0,
                    "active": True, "role": "ADMIN", "tags": []}),
            _event("DELETE", "users", {"id": 1}, None),
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "age": 10, "score": 1.0,
                    "active": True, "role": "ADMIN", "tags": []}),
        ]) + "\n"
        # Same age: PK (ID code point) tie-break keeps deterministic order;
        # the re-inserted row is not appended because it sorts by value/key.
        self.assertEqual(
            self.ids('{ users(orderBy: "age") { id } }', events=events),
            [1, 2])

    def test_nested_list_keeps_insert_order(self):
        data = self.assert_data(
            *self.run_cli('{ users(orderBy: "id") { id reviews { id } } }'))
        users = {row["id"]: row["reviews"] for row in data["users"]}
        self.assertEqual([r["id"] for r in users[1]], ["r1", "r2"])


# ---------------------------------------------------------------------------
# query-exec: pagination
# ---------------------------------------------------------------------------


class PaginationCase(_CliCase):
    def _page(self, page, size):
        query = ('{ users(page: %d, pageSize: %d, orderBy: "age") '
                 '{ id } }') % (page, size)
        data = self.assert_data(*self.run_cli(query))
        return [row["id"] for row in data["users"]]

    def test_zero_based_pages_over_sorted_rows(self):
        # ASC age order is 2, 10, 1, 3.
        self.assertEqual(self._page(0, 2), [2, 10])
        self.assertEqual(self._page(1, 2), [1, 3])

    def test_uneven_final_page(self):
        self.assertEqual(self._page(1, 3), [3])

    def test_page_beyond_range_is_empty(self):
        self.assertEqual(self._page(9, 2), [])

    def test_page_size_without_page_starts_at_zero(self):
        query = '{ users(pageSize: 2, orderBy: "age") { id } }'
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual([r["id"] for r in data["users"]], [2, 10])

    def test_no_page_size_returns_all_sorted_rows(self):
        query = '{ users(page: 0, orderBy: "age") { id } }'
        # page without pageSize is rejected; but orderBy without any paging
        # returns every sorted row.
        self.assert_error(*self.run_cli(query), "InvalidQuery")
        query = '{ users(orderBy: "age") { id } }'
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual([r["id"] for r in data["users"]], [2, 10, 1, 3])

    def test_multiple_roots_page_independently(self):
        # `users` is paged/sorted while the undeclared `plain` root is not a
        # paging field and returns every row in insert order.
        query = ('{ users(page: 0, pageSize: 1, orderBy: "age") { id } '
                 'plain { id } }')
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual([r["id"] for r in data["users"]], [2])
        self.assertEqual([r["id"] for r in data["plain"]], [1, 2, 3, 10])

    def test_paging_does_not_touch_nested_list_order(self):
        query = ('{ users(page: 0, pageSize: 1, orderBy: "id") '
                 '{ id reviews { id } } }')
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual(len(data["users"]), 1)
        self.assertEqual(
            [r["id"] for r in data["users"][0]["reviews"]], ["r1", "r2"])

    def test_paging_respects_filter(self):
        query = ('{ users(status: "on", page: 0, pageSize: 1, '
                 'orderBy: "age") { id } }')
        data = self.assert_data(*self.run_cli(query))
        self.assertEqual([r["id"] for r in data["users"]], [2])


# ---------------------------------------------------------------------------
# query-exec: error classification
# ---------------------------------------------------------------------------


class ExecPagingErrorCase(_CliCase):
    def test_literal_errors_are_invalid_query(self):
        cases = [
            "{ users(page: 0) { id } }",
            "{ users(page: -1, pageSize: 2) { id } }",
            "{ users(pageSize: 0) { id } }",
            ('{ users(orderBy: "missing") { id } }'),
            ('{ users(orderBy: "status") { id } }'),
            ('{ users(orderBy: "tags") { id } }'),
            ('{ users(sortDirection: SIDE) { id } }'),
            ("{ users(sortDirection: ASC) { id } }",),
        ]
        for case in cases:
            query = case[0] if isinstance(case, tuple) else case
            with self.subTest(query=query):
                self.assert_error(*self.run_cli(query), "InvalidQuery")

    def test_variable_errors_are_variables_error(self):
        cases = [
            ("query ($p: Int) { users(page: $p, pageSize: 2) { id } }",
             '{"p": -1}'),
            ("query ($s: Int) { users(page: 1, pageSize: $s) { id } }",
             '{"s": 2.5}'),
            ('query ($o: String) { users(orderBy: $o) { id } }',
             '{"o": "team"}'),
            ('query ($d: String) { users(orderBy: "age", '
             'sortDirection: $d) { id } }', '{"d": "DESC2"}'),
        ]
        for query, variables in cases:
            with self.subTest(query=query):
                self.assert_error(
                    *self.run_cli(query, variables), "VariablesError")

    def test_wrong_type_order_by_value_is_event_error(self):
        events = "\n".join([
            _event("INSERT", "users", None,
                   {"id": 1, "name": "a", "age": 30, "score": 1.0,
                    "active": True, "role": "ADMIN", "tags": []}),
            _event("INSERT", "users", None,
                   {"id": 2, "name": "b", "age": "old", "score": 1.0,
                    "active": True, "role": "ADMIN", "tags": []}),
        ]) + "\n"
        # id is projected, age only read for sorting: the malformed age still
        # surfaces as an EventError.
        self.assert_error(
            *self.run_cli('{ users(orderBy: "age") { id } }', events=events),
            "EventError")


# ---------------------------------------------------------------------------
# subscription-push: unchanged
# ---------------------------------------------------------------------------


class SubscriptionUnchangedCase(_CliCase):
    command = "subscription-push"

    SUB_SCHEMA = """\
type Query { users: [User!]! }
type Subscription {
  users(page: Int, pageSize: Int, orderBy: String, sortDirection: String): [User!]!
}
type User @entity(name: "users", key: "id") { id: ID! age: Int! }
"""

    SUB_EVENTS = _event(
        "INSERT", "users", None, {"id": 1, "age": 5}) + "\n"

    def test_paging_args_rejected_like_any_filter_arg(self):
        code, stdout, stderr = self.run_cli(
            "subscription { users(page: 0, pageSize: 1) { id } }",
            schema=self.SUB_SCHEMA, events=self.SUB_EVENTS)
        self.assert_error(code, stdout, stderr, "InvalidQuery")

    def test_plain_subscription_still_pushes(self):
        code, stdout, stderr = self.run_cli(
            "subscription { users { id } }",
            schema=self.SUB_SCHEMA, events=self.SUB_EVENTS)
        self.assertEqual(code, 0, stderr)
        rows = [json.loads(line) for line in stdout.splitlines() if line]
        self.assertEqual(rows[0]["data"], {"id": 1})


# ---------------------------------------------------------------------------
# complexity: pageSize becomes a list bound (query-exec only)
# ---------------------------------------------------------------------------


class PagingComplexityCase(_CliCase):
    def test_page_size_bounds_list(self):
        query = '{ users(page: 0, pageSize: 2, orderBy: "age") { id } }'
        data = self.assert_data(*self.run_cli(query, limit=2))
        self.assertEqual([r["id"] for r in data["users"]], [2, 10])

    def test_page_size_exceeded(self):
        query = '{ users(page: 0, pageSize: 3, orderBy: "age") { id } }'
        code, stdout, stderr = self.run_cli(query, limit=2)
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertIsNone(payload["data"])
        self.assertEqual(payload["errors"][0]["extensions"]["code"],
                         "QUERY_COMPLEXITY_EXCEEDED")

    def test_first_takes_priority_over_page_size(self):
        # Score uses first:1 even though pageSize:9 would score higher; the
        # request passes a limit of 1 (first/limit bound the score only, they
        # never truncate the executed rows).
        query = "{ users(first: 1, pageSize: 9) { id } }"
        data = self.assert_data(*self.run_cli(query, limit=1))
        self.assertEqual([r["id"] for r in data["users"]], [1, 2, 3, 10])

    def test_limit_takes_priority_over_page_size(self):
        query = "{ users(limit: 1, pageSize: 9) { id } }"
        data = self.assert_data(*self.run_cli(query, limit=1))
        self.assertEqual([r["id"] for r in data["users"]], [1, 2, 3, 10])

    def test_without_bound_uses_default(self):
        query = "{ users { id } }"
        code, stdout, stderr = self.run_cli(query, limit=500)
        payload = json.loads(stdout)
        self.assertIsNone(payload["data"])
        self.assertEqual(payload["errors"][0]["extensions"]["code"],
                         "QUERY_COMPLEXITY_EXCEEDED")

    def test_non_positive_page_size_rejected_before_gate(self):
        # Validation precedes the complexity gate, so a non-positive pageSize
        # ends as a VariablesError (the scorer itself would otherwise fall
        # back to the 1000 default, but validation wins).
        query = "query ($s: Int) { users(page: 0, pageSize: $s) { id } }"
        self.assert_error(
            *self.run_cli(query, '{"s": 0}', limit=1), "VariablesError")


if __name__ == "__main__":
    unittest.main()
