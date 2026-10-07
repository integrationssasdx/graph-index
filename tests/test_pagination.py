"""End-to-end tests for stable list-root sorting and pagination.

Covers both `query-plan` (the four emitted paging keys and static validation)
and `query-exec` (typed ordering, tie-breaks, nulls-last, zero-based paging
and the InvalidQuery/VariablesError error split), plus the pageSize
complexity-bound fallback.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from graph_index.cli import main

SCHEMA = """\
type Query {
  users(id: ID, team_id: ID, page: Int, pageSize: Int,
        orderBy: String, sortDirection: String): [User!]!
  user(id: ID!): User
  teams(page: Int, pageSize: Int, orderBy: String, sortDirection: String): [Team!]!
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
  rank: Int!
  weight: Float!
  active: Boolean!
  role: Role!
  note: String
  team_id: ID
  team: Team @link(local: "team_id", target: "id")
}

type Team @entity(name: "teams", key: "id") {
  id: ID!
  members: [User!]! @link(local: "id", target: "team_id")
}
"""


def _event(op, entity, before, after):
    return json.dumps({"op": op, "entity": entity, "before": before,
                       "after": after})


# rank is declared non-null (a valid orderBy target); u3's snapshot still
# carries rank: null. Sorting tolerates that data null and places it last, so
# the rule is observable whenever rank is the sort key but not projected.
# Insert order: u1..u5.
EVENTS = "\n".join([
    _event("INSERT", "users", None,
           {"id": "u1", "name": "b", "rank": 2, "weight": 1.5,
            "active": True, "role": "MEMBER", "team_id": "t1"}),
    _event("INSERT", "users", None,
           {"id": "u2", "name": "b", "rank": 1, "weight": 2.0,
            "active": False, "role": "ADMIN", "team_id": "t1"}),
    _event("INSERT", "users", None,
           {"id": "u3", "name": "a", "rank": None, "weight": 1.5,
            "active": False, "role": "GUEST", "team_id": "t1"}),
    _event("INSERT", "users", None,
           {"id": "u4", "name": "a", "rank": 2, "weight": 9.0,
            "active": True, "role": "ADMIN", "team_id": "t1"}),
    _event("INSERT", "users", None,
           {"id": "u5", "name": "c", "rank": 1, "weight": -1.0,
            "active": False, "role": "MEMBER", "team_id": "t1"}),
    _event("INSERT", "teams", None, {"id": "t1"}),
]) + "\n"


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def run_plan(self, query, variables="{}", schema=SCHEMA):
        return self._run("query-plan", query, variables, schema, None)

    def run_exec(self, query, variables="{}", schema=SCHEMA, events=EVENTS,
                 limit=None):
        env = dict(os.environ)
        if limit is not None:
            env["GRAPHQL_QUERY_COMPLEXITY_LIMIT"] = str(limit)
        else:
            env.pop("GRAPHQL_QUERY_COMPLEXITY_LIMIT", None)
        old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            return self._run("query-exec", query, variables, schema, events)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def _run(self, command, query, variables, schema, events):
        schema_path = self._write("schema.graphql", schema)
        query_path = self._write("query.graphql", query)
        variables_path = self._write("variables.json", variables)
        argv = [command, "--schema", schema_path, "--query", query_path,
                "--variables", variables_path]
        if command == "query-exec":
            events_path = self._write("events.ndjson", events)
            argv += ["--events", events_path]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_error(self, result, expected_code):
        code, stdout, stderr = result
        self.assertEqual(code, 2, stdout)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["code"], expected_code)
        self.assertTrue(payload["message"])

    def assert_data(self, result):
        code, stdout, stderr = result
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        return json.loads(stdout)["data"]

    def assert_plan(self, result):
        code, stdout, stderr = result
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        return json.loads(stdout)


# ---------------------------------------------------------------------------
# query-plan output
# ---------------------------------------------------------------------------


class QueryPlanPagingTests(CliCase):
    def test_defaults_present_when_field_declares_args(self):
        plan = self.assert_plan(self.run_plan("{ users { id } }"))
        root = plan["roots"][0]
        self.assertEqual(root["page"], 0)
        self.assertIsNone(root["pageSize"])
        self.assertIsNone(root["orderBy"])
        self.assertEqual(root["sortDirection"], "ASC")

    def test_paging_values_emitted(self):
        query = ('{ users(page: 2, pageSize: 5, orderBy: "name", '
                 "sortDirection: DESC) { id } }")
        root = self.assert_plan(self.run_plan(query))["roots"][0]
        self.assertEqual(root["page"], 2)
        self.assertEqual(root["pageSize"], 5)
        self.assertEqual(root["orderBy"], "name")
        self.assertEqual(root["sortDirection"], "DESC")

    def test_enum_literal_direction_accepted(self):
        root = self.assert_plan(
            self.run_plan('{ users(orderBy: "name", sortDirection: ASC) { id } }')
        )["roots"][0]
        self.assertEqual(root["sortDirection"], "ASC")

    def test_variables_resolved_in_plan(self):
        query = (
            "query ($p: Int, $s: Int, $f: String, $d: String) "
            "{ users(page: $p, pageSize: $s, orderBy: $f, sortDirection: $d)"
            " { id } }"
        )
        variables = '{"p": 3, "s": 10, "f": "rank", "d": "DESC"}'
        root = self.assert_plan(self.run_plan(query, variables))["roots"][0]
        self.assertEqual(
            [root["page"], root["pageSize"], root["orderBy"],
             root["sortDirection"]],
            [3, 10, "rank", "DESC"],
        )

    def test_undeclared_paging_args_remain_unknown(self):
        self.assert_error(self.run_plan("{ plain(page: 0, pageSize: 1) { id } }"),
                          "UnknownField")

    def test_single_object_style_list_defaults_still_emitted(self):
        root = self.assert_plan(self.run_plan("{ teams { id } }"))["roots"][0]
        self.assertEqual(root["page"], 0)
        self.assertIsNone(root["pageSize"])


# ---------------------------------------------------------------------------
# query-exec ordering
# ---------------------------------------------------------------------------


class OrderingTests(CliCase):
    def ids(self, data):
        return [row["id"] for row in data["users"]]

    def test_no_order_by_keeps_insert_order(self):
        self.assertEqual(
            self.ids(self.assert_data(self.run_exec("{ users { id } }"))),
            ["u1", "u2", "u3", "u4", "u5"],
        )

    def test_int_asc_with_primary_key_tie_break(self):
        data = self.assert_data(self.run_exec('{ users(orderBy: "rank") { id } }'))
        # rank: u2/u5 = 1 (tie -> id u2<u5), u1/u4 = 2 (tie -> id),
        # u3 null last.
        self.assertEqual(self.ids(data), ["u2", "u5", "u1", "u4", "u3"])

    def test_int_desc(self):
        data = self.assert_data(
            self.run_exec('{ users(orderBy: "rank", sortDirection: DESC) { id } }')
        )
        self.assertEqual(self.ids(data), ["u1", "u4", "u2", "u5", "u3"])

    def test_string_asc_then_primary_key(self):
        data = self.assert_data(self.run_exec('{ users(orderBy: "name") { id } }'))
        # name a: u3,u4 (id u3<u4), b: u1,u2 (id u1<u2), c: u5. rank is null
        # only for u3 but is not the sort key here.
        self.assertEqual(self.ids(data), ["u3", "u4", "u1", "u2", "u5"])

    def test_float_asc(self):
        data = self.assert_data(
            self.run_exec('{ users(orderBy: "weight") { id } }')
        )
        self.assertEqual(self.ids(data), ["u5", "u1", "u3", "u2", "u4"])

    def test_boolean_false_before_true(self):
        data = self.assert_data(
            self.run_exec('{ users(orderBy: "active") { id } }')
        )
        self.assertEqual(self.ids(data), ["u2", "u3", "u5", "u1", "u4"])

    def test_enum_declaration_order(self):
        data = self.assert_data(
            self.run_exec('{ users(orderBy: "role") { id } }')
        )
        # ADMIN u2,u4 ; MEMBER u1,u5 ; GUEST u3.
        self.assertEqual(self.ids(data), ["u2", "u4", "u1", "u5", "u3"])

    def test_enum_desc(self):
        data = self.assert_data(
            self.run_exec(
                '{ users(orderBy: "role", sortDirection: DESC) { id } }'
            )
        )
        self.assertEqual(self.ids(data), ["u3", "u1", "u5", "u2", "u4"])

    def test_null_sorts_last_in_both_directions(self):
        asc = self.ids(self.assert_data(
            self.run_exec('{ users(orderBy: "rank") { id } }')))
        desc = self.ids(self.assert_data(
            self.run_exec(
                '{ users(orderBy: "rank", sortDirection: DESC) { id } }')))
        self.assertEqual(asc[-1], "u3")
        self.assertEqual(desc[-1], "u3")

    def test_equal_values_fall_back_to_insert_order(self):
        # Two rows share name "b" and id u1<u2; ordering respects PK, and the
        # remaining tie keeps insert order.
        data = self.assert_data(
            self.run_exec('{ users(orderBy: "name") { id name } }')
        )
        names = [(r["id"], r["name"]) for r in data["users"]]
        self.assertEqual(
            names,
            [("u3", "a"), ("u4", "a"), ("u1", "b"), ("u2", "b"),
             ("u5", "c")],
        )


class PaginationTests(CliCase):
    def test_page_slices_zero_based(self):
        query = '{ users(page: 1, pageSize: 2, orderBy: "name") { id } }'
        data = self.assert_data(self.run_exec(query))
        self.assertEqual([r["id"] for r in data["users"]], ["u1", "u2"])

    def test_first_page(self):
        query = '{ users(page: 0, pageSize: 2, orderBy: "name") { id } }'
        data = self.assert_data(self.run_exec(query))
        self.assertEqual([r["id"] for r in data["users"]], ["u3", "u4"])

    def test_last_partial_page(self):
        query = '{ users(page: 2, pageSize: 2, orderBy: "name") { id } }'
        data = self.assert_data(self.run_exec(query))
        self.assertEqual([r["id"] for r in data["users"]], ["u5"])

    def test_page_beyond_end_is_empty(self):
        query = '{ users(page: 9, pageSize: 2, orderBy: "name") { id } }'
        data = self.assert_data(self.run_exec(query))
        self.assertEqual(data["users"], [])

    def test_page_size_without_order_keeps_insert_order(self):
        data = self.assert_data(self.run_exec("{ users(pageSize: 2) { id } }"))
        self.assertEqual([r["id"] for r in data["users"]], ["u1", "u2"])

    def test_no_page_size_means_no_truncation(self):
        data = self.assert_data(
            self.run_exec('{ users(orderBy: "name") { id } }')
        )
        self.assertEqual(len(data["users"]), 5)

    def test_multiple_roots_paged_independently(self):
        schema = """
        type Query {
          a(page: Int, pageSize: Int, orderBy: String, sortDirection: String): [T!]!
          b(page: Int, pageSize: Int, orderBy: String, sortDirection: String): [T!]!
        }
        type T @entity(name: "ts", key: "id") { id: ID! v: Int! }
        """
        events = "\n".join([
            _event("INSERT", "ts", None, {"id": "x", "v": 2}),
            _event("INSERT", "ts", None, {"id": "y", "v": 1}),
            _event("INSERT", "ts", None, {"id": "z", "v": 3}),
        ]) + "\n"
        query = (
            '{ a(page: 0, pageSize: 1, orderBy: "v") { id } '
            'b(page: 0, pageSize: 2, orderBy: "v", sortDirection: DESC) { id } }'
        )
        data = self.assert_data(self.run_exec(query, schema=schema, events=events))
        self.assertEqual([r["id"] for r in data["a"]], ["y"])
        self.assertEqual([r["id"] for r in data["b"]], ["z", "x"])

    def test_nested_list_link_order_unaffected(self):
        query = (
            '{ users(page: 0, pageSize: 1, orderBy: "id") '
            "{ id team { id members { id } } } }"
        )
        data = self.assert_data(self.run_exec(query))
        root = data["users"][0]
        self.assertEqual(root["id"], "u1")
        # The nested @link list keeps target insert order, never root paging.
        self.assertEqual([m["id"] for m in root["team"]["members"]],
                         ["u1", "u2", "u3", "u4", "u5"])


class SnapshotOrderInteractionTests(CliCase):
    SCHEMA = (
        "type Query { things(page: Int, pageSize: Int, orderBy: String, "
        "sortDirection: String): [Thing!]! }\n"
        'type Thing @entity(name: "things", key: "id") '
        "{ id: ID! v: Int! }\n"
    )

    def events_for(self, rows):
        return "\n".join(rows) + "\n"

    def test_update_replaces_in_place_then_sorted(self):
        events = self.events_for([
            _event("INSERT", "things", None, {"id": "a", "v": 1}),
            _event("INSERT", "things", None, {"id": "b", "v": 2}),
            _event("UPDATE", "things", {"id": "a"}, {"id": "a", "v": 3}),
        ])
        data = self.assert_data(self.run_exec(
            '{ things(orderBy: "v") { id v } }',
            schema=self.SCHEMA, events=events))
        self.assertEqual(data["things"], [
            {"id": "b", "v": 2}, {"id": "a", "v": 3},
        ])

    def test_delete_reinsert_tie_goes_to_end(self):
        events = self.events_for([
            _event("INSERT", "things", None, {"id": "a", "v": 1}),
            _event("INSERT", "things", None, {"id": "b", "v": 1}),
            _event("DELETE", "things", {"id": "a"}, None),
            _event("INSERT", "things", None, {"id": "a", "v": 1}),
        ])
        data = self.assert_data(self.run_exec(
            '{ things(orderBy: "v") { id } }',
            schema=self.SCHEMA, events=events))
        # Same sort value and different PK, so PK "a"<"b" wins over insert
        # order here; insert order only breaks ties the PK cannot.
        self.assertEqual([r["id"] for r in data["things"]], ["a", "b"])


# ---------------------------------------------------------------------------
# Static (InvalidQuery) validation
# ---------------------------------------------------------------------------


class StaticValidationTests(CliCase):
    def test_page_without_page_size(self):
        self.assert_error(self.run_exec("{ users(page: 1) { id } }"),
                          "InvalidQuery")

    def test_negative_page(self):
        self.assert_error(
            self.run_exec("{ users(page: -1, pageSize: 2) { id } }"),
            "InvalidQuery",
        )

    def test_zero_page_size(self):
        self.assert_error(
            self.run_exec("{ users(page: 0, pageSize: 0) { id } }"),
            "InvalidQuery",
        )

    def test_negative_page_size(self):
        self.assert_error(
            self.run_exec("{ users(pageSize: -2) { id } }"), "InvalidQuery"
        )

    def test_float_page_size(self):
        # A fractional literal parses but fails the integer paging contract.
        self.assert_error(
            self.run_exec("{ users(page: 0, pageSize: 2.5) { id } }"),
            "InvalidQuery",
        )

    def test_order_by_unknown_field(self):
        self.assert_error(
            self.run_exec('{ users(orderBy: "nope") { id } }'), "InvalidQuery"
        )

    def test_order_by_nullable_field(self):
        # rank is nullable; team is a nullable object type.
        self.assert_error(
            self.run_exec('{ users(orderBy: "note") { id } }'), "InvalidQuery"
        )

    def test_order_by_object_field(self):
        self.assert_error(
            self.run_exec('{ users(orderBy: "team") { id } }'), "InvalidQuery"
        )

    def test_order_by_list_field(self):
        schema = SCHEMA + (
            "extend type User { tags: [String!]! }\n"
        )
        self.assert_error(
            self.run_exec('{ users(orderBy: "tags") { id } }', schema=schema),
            "InvalidQuery",
        )

    def test_bad_sort_direction(self):
        self.assert_error(
            self.run_exec('{ users(orderBy: "id", sortDirection: UP) { id } }'),
            "InvalidQuery",
        )

    def test_sort_direction_without_order_by(self):
        self.assert_error(
            self.run_exec("{ users(sortDirection: ASC) { id } }"),
            "InvalidQuery",
        )

    def test_undeclared_paging_args_still_unknown(self):
        self.assert_error(
            self.run_exec("{ plain(page: 0, pageSize: 1) { id } }"),
            "UnknownField",
        )

    def test_paging_args_rejected_on_nested_field(self):
        self.assert_error(
            self.run_exec(
                '{ users { id team { members(pageSize: 1) { id } } } }'
            ),
            "InvalidQuery",
        )

    def test_literal_null_page_is_invalid_query(self):
        self.assert_error(
            self.run_exec("{ users(page: null, pageSize: 2) { id } }"),
            "InvalidQuery",
        )


# ---------------------------------------------------------------------------
# Variable (VariablesError) validation
# ---------------------------------------------------------------------------


class VariableValidationTests(CliCase):
    def test_page_variable_wrong_type(self):
        query = "query ($p: Int) { users(page: $p, pageSize: 2) { id } }"
        self.assert_error(self.run_exec(query, '{"p": true}'), "VariablesError")

    def test_page_variable_negative(self):
        query = "query ($p: Int) { users(page: $p, pageSize: 2) { id } }"
        self.assert_error(self.run_exec(query, '{"p": -4}'), "VariablesError")

    def test_page_size_variable_zero(self):
        query = "query ($s: Int) { users(pageSize: $s) { id } }"
        self.assert_error(self.run_exec(query, '{"s": 0}'), "VariablesError")

    def test_order_by_variable_unknown_field(self):
        query = "query ($f: String) { users(orderBy: $f) { id } }"
        self.assert_error(
            self.run_exec(query, '{"f": "nope"}'), "VariablesError"
        )

    def test_order_by_variable_nullable_field(self):
        query = "query ($f: String) { users(orderBy: $f) { id } }"
        self.assert_error(
            self.run_exec(query, '{"f": "note"}'), "VariablesError"
        )

    def test_order_by_variable_wrong_type(self):
        query = "query ($f: Int) { users(orderBy: $f) { id } }"
        self.assert_error(self.run_exec(query, '{"f": 3}'), "VariablesError")

    def test_order_by_variable_null(self):
        query = "query ($f: String) { users(orderBy: $f) { id } }"
        self.assert_error(
            self.run_exec(query, '{"f": null}'), "VariablesError"
        )

    def test_sort_direction_variable_bad_value(self):
        query = (
            "query ($d: String) "
            '{ users(orderBy: "id", sortDirection: $d) { id } }'
        )
        self.assert_error(
            self.run_exec(query, '{"d": "SIDE"}'), "VariablesError"
        )

    def test_missing_required_page_variable(self):
        query = "query ($p: Int!) { users(page: $p, pageSize: 2) { id } }"
        self.assert_error(self.run_exec(query, "{}"), "VariablesError")

    def test_valid_variable_paging(self):
        query = (
            "query ($s: Int, $f: String) "
            '{ users(pageSize: $s, orderBy: $f) { id } }'
        )
        data = self.assert_data(
            self.run_exec(query, '{"s": 2, "f": "name"}')
        )
        self.assertEqual([r["id"] for r in data["users"]], ["u3", "u4"])


# ---------------------------------------------------------------------------
# Complexity bound fallback
# ---------------------------------------------------------------------------


class ComplexityBoundTests(CliCase):
    def test_page_size_sets_bound(self):
        result = self.run_exec(
            "{ users(page: 0, pageSize: 3) { id } }", limit=3
        )
        self.assertEqual(
            len(self.assert_data(result)["users"]), 3
        )

    def test_page_size_over_limit(self):
        code, stdout, stderr = self.run_exec(
            "{ users(page: 0, pageSize: 5) { id } }", limit=3
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertIsNone(payload["data"])
        self.assertEqual(
            payload["errors"][0]["extensions"]["code"],
            "QUERY_COMPLEXITY_EXCEEDED",
        )

    def test_limit_beats_page_size(self):
        result = self.run_exec(
            "{ users(limit: 3, pageSize: 9) { id } }", limit=3
        )
        self.assertEqual(code_ok(result), 0)

    def test_first_beats_limit_and_page_size(self):
        result = self.run_exec(
            "{ users(first: 3, limit: 9, pageSize: 9) { id } }", limit=3
        )
        self.assertEqual(code_ok(result), 0)

    def test_no_bound_falls_back_to_default(self):
        code, stdout, stderr = self.run_exec("{ users { id } }", limit=3)
        payload = json.loads(stdout)
        self.assertEqual(
            payload["errors"][0]["extensions"]["code"],
            "QUERY_COMPLEXITY_EXCEEDED",
        )


def code_ok(result):
    return result[0]


if __name__ == "__main__":
    unittest.main()
